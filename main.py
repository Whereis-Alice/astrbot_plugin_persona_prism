"""人格棱镜 · Persona Prism —— 群友人格画像插件主入口。

链路：被动采集 / 主动回溯 → 清洗抽样 → 统计锚点 → LLM 结构化分析
      → 高级卡片渲染（t2i / Playwright / 文转图 / 纯文本四层兜底）
      → 落库 → WebUI 可查可管。

上游灵感来自 Zhalslar/astrbot_plugin_portrayal，详见 NOTICE.md。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger, sp
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.star import StarTools
from astrbot.core.star.filter.event_message_type import EventMessageType
from quart import jsonify, request

from .prism import cards, collector, dashboard, history, love, scanning, scenes
from .prism.analyzer import AnalyzeError, PrismAnalyzer
from .prism.cards import CardContext, CardRenderer, RenderResult
from .prism.config import ConfigError, PrismConfig
from .prism.models import CorpusBundle, CorpusMessage, MemberProfile, PortraitRecord
from .prism.prompts import PromptLibrary, PromptSpec, normalize_layout
from .prism.store import AsyncStore, PrismStore

PLUGIN_ID = "astrbot_plugin_persona_prism"
PLUGIN_VERSION = "v1.2.2"

#: 内置提示词对应的指令（与 prompts/builtin_prompts.yaml 一一对应），
#: 用于「保留指令」校验与帮助表。前 6 条是本插件的结构化卡片玩法，
#: 后 5 条兼容上游 astrbot_plugin_portrayal 的长文玩法。
#: 「棱镜恋爱」「今日人设」是「恋爱诊断」的兼容别名，共用同一条提示词，只列在 OWN_COMMANDS 里。
BUILTIN_COMMANDS = (
    "棱镜画像",
    "棱镜赞赏",
    "棱镜锐评",
    "棱镜克隆",
    "棱镜姻缘",
    "恋爱诊断",
    "画像",
    "正画像",
    "负画像",
    "克隆人格",
    "找对象",
)

#: 本插件自己的所有指令。语料采集时会把这些消息剔除，免得画像里全是指令回声。
OWN_COMMANDS = (
    "棱镜画像",
    "棱镜赞赏",
    "棱镜锐评",
    "棱镜克隆",
    "棱镜姻缘",
    "恋爱诊断",
    "恋爱诊断榜",
    "棱镜恋爱",
    "棱镜恋爱榜",
    "棱镜帮助",
    "棱镜档案",
    "棱镜历史",
    "棱镜删除",
    "棱镜隐身",
    "棱镜现身",
    "棱镜拉黑",
    "棱镜放行",
    "棱镜缓存",
    "棱镜清缓存",
    "棱镜重扫",
    "棱镜诊断",
    "棱镜主题",
    "棱镜统计",
    "画像",
    "正画像",
    "负画像",
    "克隆人格",
    "找对象",
    "查看画像",
    "切换人格",
    "恢复人格",
    "今日人设",
)

#: 「画像」系列（兼容上游）用到的提示词 key。
LEGACY_KEYS: dict[str, str] = {
    "画像": "legacy_portrait",
    "正画像": "legacy_positive",
    "负画像": "legacy_negative",
    "克隆人格": "legacy_clone",
    "找对象": "legacy_match",
}

#: 可以拿来做人格克隆的记录类型，按优先级排列。
CLONE_KINDS = ("legacy_clone", "clone")

#: 共享偏好存储里的键。加插件前缀，避免和上游插件的备份互相覆盖。
_SP_BOT_BACKUP = "persona_prism_original_bot_info"
_SP_PERSONA_BACKUP = "persona_prism_persona_backup"
#: 克隆人格在 AstrBot 人格列表里的 ID 前缀。
_PERSONA_ID_PREFIX = "persona_prism_clone_"

_QQ_RE = re.compile(r"^\d{5,12}$")
_DIGITS_RE = re.compile(r"(?<!\d)(\d{5,12})(?!\d)")
_AVATAR_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
_PREFIX_CHARS = "/.#!！。 \t"
#: WebUI 预览用 data URL 的体积上限，超过就让前端别加载了。
_CARD_PREVIEW_LIMIT = 4 * 1024 * 1024
_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_MAINTENANCE_INTERVAL = 3600


def _as_int(value: Any) -> int | None:
    """把可能是 str / int / 负数的 message_seq 安全转成 int。

    上游用 str.isdigit() 判断，遇到 NapCat 返回的负数 message_id 直接失效，
    历史回溯会静默停在第一页。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _history_cursor(raw: Any, field: str = "") -> int | None:
    """从一条群历史消息里取出翻页游标（prism.history 的薄封装）。"""
    return history.read_cursor(raw, field)


def _page_ids(messages: Any) -> set[str]:
    """一页群历史里所有消息的唯一标识（prism.history 的薄封装）。"""
    return history.page_ids(messages)

def _strip_command(text: str, command: str) -> str:
    """去掉指令前缀（含唤醒前缀符）后剩下的参数部分。"""
    body = (text or "").lstrip(_PREFIX_CHARS)
    if command and body.startswith(command):
        body = body[len(command) :]
    return body.strip()


#: 指令尾巴上的天数。只认 1~2 位：5 位以上的数字按 QQ 号处理（见 _resolve_target），
#: 两者用长度区分开，「恋爱诊断 123456789 7」能同时解析出对象和天数。
_DAYS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:天|日)?(?!\d)")


def _parse_days(text: str, command: str, *, default: int = 1, cap: int = 30) -> int:
    """从指令参数里读天数：「恋爱诊断 7」「恋爱诊断 @某人 7天」都认。"""
    body = _strip_command(text or "", command)
    found = _DAYS_RE.search(body)
    if not found:
        return default
    days = int(found.group(1))
    if days <= 0:
        return default
    return min(days, max(1, cap))


def _fmt_ts(ts: Any, pattern: str = "%Y-%m-%d") -> str:
    number = _as_int(ts)
    if not number or number <= 0:
        return ""
    with contextlib.suppress(Exception):
        return time.strftime(pattern, time.localtime(number))
    return ""


class PersonaPrismStar(Star):
    """插件主类。所有指令与 WebUI 接口都挂在这里。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.raw_config = config
        self.config = PrismConfig(config, save=getattr(config, "save_config", None))

        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_ID)
        self.cards_dir: Path = self.data_dir / "cards"
        self.cards_dir.mkdir(parents=True, exist_ok=True)

        self.store = PrismStore(self.data_dir / "prism.db")
        self.astore = AsyncStore(self.store)

        self.library = PromptLibrary()
        self._reload_prompts()

        self.analyzer = PrismAnalyzer(context, self.config, logger)
        self.renderer = CardRenderer(self, self.config, self.cards_dir, logger)

        #: 冷却与并发控制。key 形态见 _execute。
        self._sender_cooldown: dict[str, float] = {}
        self._target_cooldown: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._last_backend = ""
        self._last_maintenance = 0.0
        #: 每个平台实测可用的请求体写法（history.PARAM_STYLES 之一）。
        #: 默认的 dual 写法覆盖所有已知协议端，只有遇到"未知参数直接报错"的严格实现
        #: 才会退到单参数写法，退成功后记在这里，同一进程内不再重复试错。
        self._param_style: dict[str, str] = {}
        #: 已做过时间戳自愈的「平台:群」，同一进程内不重复扫。
        self._ts_repaired: set[str] = set()
        #: 节流告警的上次时间。key 是告警类别。
        self._warn_at: dict[str, float] = {}

        self._register_dashboard_apis()
        logger.info("[人格棱镜] %s 已加载，数据目录：%s", PLUGIN_VERSION, self.data_dir)

    # ------------------------------------------------------------------ 基础

    def _warn_throttled(self, key: str, message: str, *, window: int = 300) -> None:
        """同一类问题最多每 window 秒吼一次。

        群消息是高频事件，出问题时直接 logger.warning 会把日志刷成瀑布；但整段
        suppress 又会让故障彻底隐身。折中成节流告警。
        """
        now = time.time()
        if now - self._warn_at.get(key, 0.0) < window:
            return
        self._warn_at[key] = now
        logger.warning("[人格棱镜] %s", message)

    def _reload_prompts(self) -> None:
        """把 SQLite 里的自定义提示词刷进内存库。"""
        try:
            self.library.load_custom(self.store.list_prompt_entries())
        except Exception as exc:  # 自定义条目坏了不能拖垮内置功能
            logger.warning("[人格棱镜] 读取自定义提示词失败：%s", exc)

    @staticmethod
    def _scope(event: AstrMessageEvent) -> tuple[str, str]:
        return event.get_platform_name() or "", event.get_group_id() or ""

    @staticmethod
    def _is_own_command(text: str) -> bool:
        body = (text or "").lstrip(_PREFIX_CHARS)
        return body.startswith(OWN_COMMANDS)

    async def _maintenance(self) -> None:
        """低频后台清理：按保留天数与单群上限修剪语料。"""
        now = time.time()
        if now - self._last_maintenance < _MAINTENANCE_INTERVAL:
            return
        self._last_maintenance = now
        try:
            removed = await self.astore.prune_corpus(
                retention_days=self.config.int_of("collect.retention_days"),
                max_per_group=self.config.int_of("collect.max_per_group"),
            )
            if removed:
                logger.debug("[人格棱镜] 语料清理完成，移除 %s 条。", removed)
        except Exception as exc:
            logger.warning("[人格棱镜] 语料清理失败：%s", exc)
        try:
            #: 互动计数只用于「今天/昨天」的恋爱成分，留 30 天足够做趋势。
            await self.astore.prune_interactions(retention_days=30)
        except Exception as exc:
            logger.warning("[人格棱镜] 互动计数清理失败：%s", exc)

    # ---------------------------------------------------------------- 目标解析

    async def _resolve_target(
        self,
        event: AstrMessageEvent,
        command: str = "",
    ) -> tuple[str, str]:
        """解析「要画谁」。

        依次尝试：@某人（排除 bot 自己与 @全体）→ 引用消息的作者 →
        文本里的裸 QQ 号 → 缺省指向发起人自己。

        上游的实现是 event.get_messages()[1:] 加只看第一个 At，导致
        「@某人 画像」这种把 At 放在最前面的写法完全解析不到目标；
        同时没有排除 @bot，@机器人触发时会给机器人自己画像。
        """
        self_id = str(event.get_self_id() or "")
        for seg in event.get_messages() or []:
            if isinstance(seg, Comp.At):
                at_id = str(getattr(seg, "qq", "") or "")
                if not at_id or at_id in {"all", "0"} or at_id == self_id:
                    continue
                return at_id, str(getattr(seg, "name", "") or "")
        for seg in event.get_messages() or []:
            if isinstance(seg, Comp.Reply):
                rid = str(getattr(seg, "sender_id", "") or "")
                if rid and rid != self_id:
                    return rid, str(getattr(seg, "sender_nickname", "") or "")

        body = _strip_command(event.get_message_str() or "", command)
        if body:
            if _QQ_RE.match(body):
                return body, ""
            found = _DIGITS_RE.search(body)
            if found:
                return found.group(1), ""
        return str(event.get_sender_id() or ""), event.get_sender_name() or ""

    async def _group_display_name(
        self,
        event: AstrMessageEvent,
        platform: str,
        group_id: str,
    ) -> str:
        if not group_id:
            return "私聊"
        with contextlib.suppress(Exception):
            group = await event.get_group(group_id)
            name = str(getattr(group, "group_name", "") or "")
            if name:
                return name
        cached = await self.astore.group_name(platform, group_id)
        return cached or f"群 {group_id}"

    async def _fetch_profile(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
    ) -> MemberProfile | None:
        """拉取群成员公开资料，只映射白名单字段。

        上游把 get_group_member_info 的整个字典塞进提示词，手机号、邮箱、
        国家地区等敏感字段会一起进模型。这里改成显式白名单映射。
        """
        client = getattr(event, "bot", None)
        if client is None or not group_id:
            return None
        try:
            raw = await client.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=False,
            )
        except Exception as exc:
            logger.debug("[人格棱镜] 获取群成员资料失败：%s", exc)
            return None
        if not isinstance(raw, dict):
            return None
        sex_map = {"male": "男", "female": "女"}
        level = raw.get("level") or raw.get("qq_level") or ""
        return MemberProfile(
            user_id=str(user_id),
            nickname=str(raw.get("nickname") or ""),
            card=str(raw.get("card") or ""),
            sex=sex_map.get(str(raw.get("sex") or ""), ""),
            age=str(raw.get("age") or "") if _as_int(raw.get("age")) else "",
            long_nick=str(raw.get("long_nick") or raw.get("longNick") or ""),
            join_time=_fmt_ts(raw.get("join_time")),
            last_sent_time=_fmt_ts(raw.get("last_sent_time")),
            level=str(level),
            title=str(raw.get("title") or ""),
            area=str(raw.get("area") or ""),
            role={"owner": "群主", "admin": "管理员", "member": "成员"}.get(str(raw.get("role") or ""), ""),
        )

    # ---------------------------------------------------------------- 语料采集

    async def _capture(self, event: AstrMessageEvent, platform: str, group_id: str) -> None:
        """被动记录当前这条群消息。

        上游只在触发指令时才去拉历史，非 QQ 平台完全用不了。被动采集让所有
        平台（Telegram / Discord / 微信…）都能积累语料。
        """
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return
        segments = event.get_messages() or []
        rich = collector.parse_segments_rich(segments)
        text = rich["text"]
        if not text or self._is_own_command(text):
            return
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not message_id:
            #: 少数协议端的消息事件不带 message_id。语料表拿它当主键，缺了就自己造一个
            #: 稳定值（同一条消息重复入库仍会被去重），否则这些平台一条都存不下来。
            digest = hashlib.md5(
                f"{user_id}|{text}".encode(),
                usedforsecurity=False,
            ).hexdigest()[:12]
            message_id = f"local-{int(time.time())}-{digest}"
        #: 协议端的时间戳可能是毫秒、可能缺失、也可能明显穿越，一律先做常识校验，
        #: 否则「今天」这个窗口筛不出这条消息，恋爱成分会永远显示 0 句。
        raw_ts = collector.sane_epoch(
            getattr(event.message_obj, "timestamp", 0),
            now=time.time(),
        )
        message = CorpusMessage(
            message_id=message_id,
            user_id=user_id,
            user_name=event.get_sender_name() or "",
            text=text,
            ts=raw_ts,
            is_reply=rich["is_reply"],
            reply_to=rich["reply_to"],
            images=rich["images"],
            at_ids=rich["at_ids"],
        )
        cleaned = collector.clean_rows(
            [
                {
                    "message_id": message.message_id,
                    "user_id": message.user_id,
                    "user_name": message.user_name,
                    "text": message.text,
                    "ts": message.ts,
                    "is_reply": message.is_reply,
                    "reply_to": message.reply_to,
                    "images": message.images,
                    "at_ids": message.at_ids,
                },
            ],
            min_chars=self.config.int_of("collect.min_chars"),
            filter_commands=self.config.bool_of("collect.filter_commands"),
            drop_urls=self.config.bool_of("collect.strip_urls"),
            redact=self.config.bool_of("privacy.redact_pii"),
            keep_media=True,
        )
        if not cleaned:
            return
        try:
            await self.astore.add_messages(platform, group_id, cleaned)
        except Exception as exc:
            #: 以前这里是整段 suppress，写库一直失败也毫无痕迹，只能靠「一条语料都没有」
            #: 反推。改成节流告警：坏了能看见，又不会把日志刷爆。
            self._warn_throttled("add_messages", f"语料入库失败：{exc}")


    async def _capture_notice(self, platform: str, group_id: str, raw: dict[str, Any]) -> None:
        """把戳一戳 / 表情回应 / 撤回记进当天的互动计数。

        协议端不下发这些通知时整段静默跳过，恋爱成分会自动退化成「只看聊天记录里的
        回复与 @」，功能不会因此报错。
        """
        if not self.config.bool_of("love.enabled") or not self.config.bool_of("love.notice_collect"):
            return
        event_info = love.parse_notice(raw)
        if not event_info:
            return
        day, _start, _end = self._love_day()
        kind = str(event_info["kind"])
        actor = str(event_info["actor"])
        target = str(event_info["target"])
        count = int(event_info["count"])
        try:
            if kind == "recall":
                await self.astore.bump_interaction(
                    platform, group_id, actor, day, "recall_count", count,
                )
                return
            if not target and event_info["message_id"]:
                target = await self.astore.message_owner(
                    platform,
                    group_id,
                    str(event_info["message_id"]),
                )
            sent_field, received_field = love.NOTICE_FIELDS[kind]
            await self.astore.bump_interaction(platform, group_id, actor, day, sent_field, count)
            if target and target != actor:
                await self.astore.bump_interaction(
                    platform, group_id, target, day, received_field, count,
                )
        except Exception as exc:
            logger.debug("[人格棱镜] 互动通知记账失败（%s）：%s", kind, exc)

    async def _fetch_history_page(
        self,
        client: Any,
        platform: str,
        group_id: str,
        cursor: int | None,
        count: int,
    ) -> Any:
        """拉一页群历史，返回 messages 列表（拿不到就返回 None / 空列表）。

        各家协议端认的锚点参数名不一样：老一批读 message_seq，SnowLuma 只读 message_id。
        默认的 dual 写法把同一个游标值同时写进两个名字，谁认哪个读哪个，不认的那个会被
        静默忽略（各家的参数校验都是宽进）。

        万一碰上对未知参数直接报错的严格实现，就依次退到只写 message_seq / 只写
        message_id，成功的写法记进 self._param_style，同一进程内不再重复试错。只有"看着
        像参数没被接受"的报错才换写法重试 —— 超时、动作不支持、权限不足换写法也救不了，
        重试只会让用户多等，那类异常直接抛出去按老路记进诊断。
        """
        settled = platform in self._param_style
        primary = history.normalize_param_style(self._param_style.get(platform))
        order = [primary]
        if not settled:
            order += [name for name in history.PARAM_STYLES if name != primary]
        for index, name in enumerate(order):
            params = history.build_history_params(group_id, cursor, count, style=name)
            try:
                payload = await client.api.call_action("get_group_msg_history", **params)
            except Exception as exc:
                #: 不像参数问题、或已经没有别的写法可试 —— 交给调用方按原样处理。
                if index + 1 >= len(order) or not history.is_param_error(exc):
                    raise
                logger.debug("[人格棱镜] 群历史请求被拒（写法 %s）：%s，换一种写法重试", name, exc)
                continue
            if name != primary:
                logger.info("[人格棱镜] 群历史请求体退回「%s」写法并成功", name)
            self._param_style[platform] = name
            if isinstance(payload, dict):
                return payload.get("messages")
            return payload
        return None

    def _scan_plan(self, platform: str, group_id: str) -> scanning.ScanReport:
        """按当前配置生成一份"回溯计划"。

        既用于开工前那句"最多回溯 N 轮"的提示，也作为采集诊断的底稿。
        """
        return scanning.ScanReport(
            platform=platform,
            is_group=bool(group_id),
            supported=scanning.supports_backfill(platform, group_id),
            passive_capture=self.config.bool_of("collect.passive_capture"),
            planned_rounds=max(0, self.config.int_of("collect.backfill_rounds")),
            page_size=max(20, self.config.int_of("collect.page_size")),
        )

    async def _topup_latest(self, event: AstrMessageEvent, platform: str, group_id: str) -> int:
        """无条件补拉最新一页群历史，返回新入库条数。

        为什么不复用 _backfill：那条路上有一堆前置门槛 —— 库里条数够了就整段跳过、
        断点被标成 exhausted 时要等冷却、还要按目标条数决定翻几页。这些门槛对「按天
        结算」是致命的：老群库里早就攒够几百条，于是永远不出网，今天说的话一条都进不来，
        表现就是「今天只说了 0 句」而画像却完全正常（画像不按时间筛）。

        这里只做一件事：拿最新一页、清洗、入库。不读也不写回溯断点，因此和主动回溯
        互不干扰，可以随时调用。
        """
        if not group_id or not scanning.supports_backfill(platform, group_id):
            return 0
        client = getattr(event, "bot", None)
        if client is None:
            return 0
        page_size = max(20, self.config.int_of("collect.page_size"))
        try:
            messages = await self._fetch_history_page(client, platform, group_id, None, page_size)
        except Exception as exc:
            logger.debug("[人格棱镜] 补拉最新一页群历史失败：%s", exc)
            return 0
        if not messages:
            return 0
        rows = collector.parse_history_page(messages)
        rows = [row for row in rows if not self._is_own_command(str(row.get("text") or ""))]
        cleaned = collector.clean_rows(
            rows,
            min_chars=self.config.int_of("collect.min_chars"),
            filter_commands=self.config.bool_of("collect.filter_commands"),
            drop_urls=self.config.bool_of("collect.strip_urls"),
            redact=self.config.bool_of("privacy.redact_pii"),
            keep_media=True,
        )
        added = 0
        if cleaned:
            with contextlib.suppress(Exception):
                added = int(await self.astore.add_messages(platform, group_id, cleaned) or 0)
        seen = len(messages) if isinstance(messages, (list, tuple)) else 0
        logger.info(
            "[人格棱镜] 群 %s 补拉最新一页群历史：看到 %s 条，新入库 %s 条",
            group_id,
            seen,
            added,
        )
        return added

    async def _backfill(
        self,
        event: AstrMessageEvent,
        platform: str,
        group_id: str,
        user_id: str,
        *,
        target_total: int,
        report: scanning.ScanReport,
    ) -> scanning.ScanReport:
        """向更早的历史翻页，直到攒够目标条数或真的翻到头。

        协议端的坑：get_group_msg_history 的翻页语义在各家 OneBot 实现里有两个自由度
        —— 锚点认的可能是 message_seq 也可能是 message_id，返回的那一页数组可能是最旧
        在前也可能最新在前。传错了**不会报错**，而是原地返回同一批最新消息。所以不能靠
        "游标没变"来判断是否挖到头，必须看这一页有没有出现新的消息。

        还有第三个坑是参数名：SnowLuma 只读 message_id、完全无视 message_seq。请求体的
        拼装交给 _fetch_history_page，默认两个参数名一起写。

        做法：
        * 每页用消息 ID 集合和上一页比对，出现新面孔才算真的前进；
        * 原地打转时依次换 prism.history 里的其它翻页方式重试（四种组合全试一遍），
          成功后把可用的那种写进 scan_state，之后这个群一直用它
          （collect.cursor_field 可以手动锁定，「棱镜诊断」可以实测是哪一种）；
        * 四种都翻不动就记 stalled 并在诊断里说清楚，**不写 exhausted** ——
          翻页卡住和历史挖完是两件事，混为一谈会让这个群永远只补拉最新一页。

        与上游的其他差异：断点持久化到 scan_state（重启后接着挖）、写库前剔除本插件
        自己的指令消息、过程逐项记进 ScanReport 供采集诊断使用。
        """
        client = getattr(event, "bot", None)
        if client is None or not group_id:
            #: 拿不到协议端客户端就等于不支持主动回溯，别让诊断误导用户。
            report.supported = False
            return report

        rounds = max(0, self.config.int_of("collect.backfill_rounds"))
        page_size = max(20, self.config.int_of("collect.page_size"))
        report.planned_rounds = rounds
        report.page_size = page_size
        state = await self.astore.get_scan_state(platform, group_id)
        #: 「已挖到头」这个判断本身可能是错的（协议端对不认的游标直接回空页），所以
        #: 隔一段时间就无视它、重新验证一次。代价是一次空请求，收益是救回被误锁的群。
        recheck = scanning.should_recheck_exhausted(state)
        report.exhausted_recheck = recheck
        #: 历史已经挖到最早一条。此时往前翻只会拿到空页，但机器人离线期间的新消息
        #: 被动采集是拿不到的，所以退化成"只补拉最新一页"，且不动断点。
        topup_only = bool(state.get("exhausted")) and not recheck
        report.topup_only = topup_only
        report.depth_before = _as_int(state.get("depth_pages")) or 0
        cursor = None if topup_only else (
            _as_int(state.get("oldest_seq")) if state.get("oldest_seq") else None
        )
        newest_seen = str(state.get("newest_seq") or "")
        depth = report.depth_before

        #: 翻页方式：配置写死就照办，auto 则沿用本群实测可用的那种，没有就从头试探。
        locked = history.normalize_strategy(self.config.str_of("collect.cursor_field"))
        auto = not locked
        strategy = locked or history.normalize_strategy(state.get("cursor_field")) or history.STRATEGIES[0]
        report.cursor_field = strategy
        #: 试过且失败的方式，避免在一次回溯里来回打转。
        tried: set[str] = set()

        def _switch(page: Any) -> tuple[int, str] | None:
            """当前方式翻不动时，换一种还没试过的方式、拿这一页重算游标。

            返回 (新游标, 新方式)；四种全试过或都算不出游标就返回 None。
            配置锁定了方式时不做任何切换 —— 用户说了算。
            """
            if not auto:
                return None
            tried.add(strategy)
            for cand in history.rotate_strategies(strategy, tried):
                value = history.cursor_of(page, cand)
                if value is not None and value != cursor:
                    return value, cand
            return None

        if topup_only:
            rounds = min(rounds, 1)
        report.attempted = rounds > 0

        prev_ids: set[str] = set()
        #: 上一页的原始消息列表。换翻页方式时要拿它重算一次游标。
        last_page: Any = None
        #: 允许"丢弃库里的断点、退回最新一页重挖"一次。库里的断点可能是上个协议端
        #: 留下的、或者对方重装后 seq 全变了，拿它去翻只会一直回空页。
        restart_allowed = not topup_only
        #: 当前翻页方式是否已被证明可用 —— 用它派生的游标真的换来过一页新消息。
        #: 第一页的游标是 None（"最新一页"），任何方式都能成功，所以那不算证明。
        #: 有了这个区分才能判断空页到底是"方式选错了"还是"真的挖到头了"：
        #: 方式没被证明过 → 换一种再试；已经靠它翻过好几页了 → 认这个空页。
        cursor_proven = False
        index = 0

        while index < rounds:
            index += 1
            try:
                messages = await self._fetch_history_page(
                    client,
                    platform,
                    group_id,
                    cursor,
                    page_size,
                )
            except Exception as exc:
                logger.debug("[人格棱镜] 拉取群历史失败（第 %s 页）：%s", index, exc)
                report.error = scanning.brief_error(exc)
                break

            if not messages:
                #: 空页有三种可能，从"最可能是我们的错"往"真的没有了"依次排除：
                #: 1) 游标字段喂错了 —— 协议端不认这个值，于是回空数组而不是报错；
                #: 2) 库里的断点已失效（换过协议端 / 群消息被清理）—— 一翻就空；
                #: 3) 确实翻到了群历史最早一条。
                #: 早先版本直接跳到第 3 种并把 exhausted 永久写库，一次误判就让这个群
                #: 从此只补拉最新一页，语料永远停在两百来条。
                switched = (
                    _switch(last_page)
                    if last_page is not None and not cursor_proven
                    else None
                )
                if switched is not None:
                    logger.info(
                        "[人格棱镜] 群 %s 用 %s 往前翻回了空页，改用 %s 再试",
                        group_id,
                        strategy,
                        switched[1],
                    )
                    cursor, strategy = switched
                    report.cursor_field = strategy
                    report.cursor_switched = True
                    cursor_proven = False
                    index -= 1  # 试探不算正式一轮，别白扣预算
                    continue
                if restart_allowed and cursor is not None and report.pages == 0:
                    #: 拿库里的断点第一翻就是空页 —— 断点本身不可信，丢掉重来。
                    logger.info(
                        "[人格棱镜] 群 %s 的回溯断点已失效，退回最新一页重新往前挖",
                        group_id,
                    )
                    restart_allowed = False
                    cursor = None
                    depth = 0
                    report.restarted = True
                    index -= 1  # 这一轮不算，别白扣一次预算
                    continue
                if not topup_only and (report.pages > 0 or cursor is not None):
                    await self.astore.set_scan_state(platform, group_id, exhausted=True)
                report.exhausted = True
                break

            page_ids = history.page_ids(messages)
            if prev_ids and not (page_ids - prev_ids):
                #: 整页都是上一页看过的消息 —— 游标没生效，协议端在原地打转。
                #: 换一种翻页方式，拿上一页重算游标再试；四种全试过才认输。
                switched = _switch(last_page)
                if switched is not None:
                    logger.debug(
                        "[人格棱镜] 群 %s 的 %s 翻不动，改用 %s 重试",
                        group_id,
                        strategy,
                        switched[1],
                    )
                    cursor, strategy = switched
                    report.cursor_field = strategy
                    report.cursor_switched = True
                    cursor_proven = False
                    index -= 1  # 同上：换方式重试不占正式轮次
                    continue
                report.stalled = True
                break

            report.pages += 1
            if cursor is not None:
                #: 这一页是拿"我们自己算出来的游标"换来的，说明方式选对了。
                cursor_proven = True
            if isinstance(messages, (list, tuple)):
                report.scanned += len(messages)
            rows = collector.parse_history_page(messages)
            rows = [row for row in rows if not self._is_own_command(str(row.get("text") or ""))]
            cleaned = collector.clean_rows(
                rows,
                min_chars=self.config.int_of("collect.min_chars"),
                filter_commands=self.config.bool_of("collect.filter_commands"),
                drop_urls=self.config.bool_of("collect.strip_urls"),
                redact=self.config.bool_of("privacy.redact_pii"),
            )
            if cleaned:
                report.added += await self.astore.add_messages(platform, group_id, cleaned)
            prev_ids = page_ids

            if topup_only:
                break

            last_page = messages
            if not newest_seen:
                #: 只是个书签，记下"这个群我们见过的最新一条"，两端都试一下取大的。
                marks = [
                    value
                    for value in (
                        history.read_cursor(messages[0]),
                        history.read_cursor(messages[-1]),
                    )
                    if value is not None
                ]
                if marks:
                    newest_seen = str(max(marks))

            oldest = history.cursor_of(messages, strategy)
            if oldest is None:
                #: 当前方式取不到游标（比如协议端压根没返回这个字段），换一种。
                switched = _switch(messages)
                if switched is None:
                    report.stalled = True
                    break
                oldest, strategy = switched
                report.cursor_field = strategy
                report.cursor_switched = True
                #: 换了方式，新游标还没被验证过。
                cursor_proven = False
            cursor = oldest
            depth += 1
            await self.astore.set_scan_state(
                platform,
                group_id,
                oldest_seq=str(cursor),
                newest_seq=newest_seen,
                cursor_field=strategy,
                depth_pages=depth,
            )

            have = len(
                await self.astore.fetch_user_corpus(
                    platform,
                    group_id,
                    user_id,
                    limit=target_total + 1,
                ),
            )
            if have >= target_total:
                break
        return report

    async def _gather(
        self,
        event: AstrMessageEvent,
        platform: str,
        group_id: str,
        user_id: str,
    ) -> tuple[CorpusBundle, scanning.ScanReport]:
        """取语料并打包，同时带回一份采集诊断。命中本地缓存就不出网。

        诊断跟着返回值走、不挂在 self 上：画像可以并发执行
        （limits.max_concurrency > 1），共享可变状态会串号。
        """
        max_messages = max(20, self.config.int_of("collect.max_messages"))
        depth = max(max_messages * 4, 1000)
        rows = await self.astore.fetch_user_corpus(platform, group_id, user_id, limit=depth)
        from_cache = True
        report = self._scan_plan(platform, group_id)
        report.local_before = len(rows)

        if report.supported and len(rows) < max_messages:
            report = await self._backfill(
                event,
                platform,
                group_id,
                user_id,
                target_total=max_messages,
                report=report,
            )
            if report.added:
                from_cache = False
                rows = await self.astore.fetch_user_corpus(
                    platform,
                    group_id,
                    user_id,
                    limit=depth,
                )

        bundle = collector.build_bundle(
            rows,
            max_messages=max_messages,
            min_chars=self.config.int_of("collect.min_chars"),
            filter_commands=self.config.bool_of("collect.filter_commands"),
            drop_urls=self.config.bool_of("collect.strip_urls"),
            redact=self.config.bool_of("privacy.redact_pii"),
            fold=self.config.bool_of("collect.fold_repeats"),
            sampling=self.config.str_of("collect.sampling"),
            scanned=len(rows),
            from_cache=from_cache,
        )
        return bundle, report

    # ---------------------------------------------------------------- 核心链路

    def _cooldown_left(self, bucket: dict[str, float], key: str, window: int) -> int:
        if window <= 0:
            return 0
        left = bucket.get(key, 0.0) + window - time.time()
        return int(left) + 1 if left > 0 else 0

    async def _restore_scenes(
        self,
        platform: str,
        group_id: str,
        portrait: Any,
        bundle: CorpusBundle,
        target_id: str,
    ) -> int:
        """把模型挑出的原话还原成聊天现场（呈堂证供的气泡）。

        模型只看得到目标本人的发言，所以别人那几句必须回库里捞 —— 捞不到就
        原样留着 quote，卡片会退化成单行引用，绝不让模型代笔别人的台词。
        """
        items = [e for e in (portrait.evidence or ()) if not e.dialogue and e.quote.strip()]
        if not items or not group_id:
            return 0
        stamps: list[int] = []
        for item in items[:5]:
            hit = scenes.locate_quote(item.quote, bundle.messages)
            if hit is not None and int(hit.ts or 0) > 0:
                stamps.append(int(hit.ts))
        if not stamps:
            return 0
        merged: dict[str, dict[str, Any]] = {}
        for stamp in sorted(set(stamps)):
            try:
                rows = await self.astore.context_rows(platform, group_id, stamp, span=900, limit=80)
            except Exception:  # 还原失败只是少了气泡，不能拖垮整张卡
                continue
            for row in rows:
                merged[str(row.get("message_id") or f"{row.get('ts')}:{row.get('user_id')}")] = row
        if not merged:
            return 0
        context = sorted(merged.values(), key=lambda r: int(r.get("ts") or 0))
        names = love.collect_names(context)
        filled = scenes.enrich_all(
            items,
            bundle.messages,
            context,
            user_id=target_id,
            names=names,
        )
        if filled:
            logger.info("[人格棱镜] 已为 %s/%s 条证供还原聊天现场。", filled, len(items))
        return filled

    async def _execute(self, event: AstrMessageEvent, spec: PromptSpec | None):
        """一次完整的画像流程。所有指令都收敛到这里，便于统一限流与埋点。"""
        if spec is None:
            yield event.plain_result("这个玩法的提示词不存在，请到 WebUI 的「提示词」页检查。")
            return

        platform, group_id = self._scope(event)
        if group_id and not self.config.group_allowed(group_id):
            return

        sender_id = str(event.get_sender_id() or "")
        target_id, target_hint = await self._resolve_target(event, spec.command)
        if not target_id:
            yield event.plain_result("没认出要画谁，@一下对方、回复对方的消息，或者直接跟上 QQ 号。")
            return

        is_admin = bool(event.is_admin())
        if self.config.bool_of("behavior.allow_self_only") and target_id != sender_id and not is_admin:
            yield event.plain_result("当前配置只允许给自己做画像。")
            return

        if self.config.is_protected(target_id) and not is_admin:
            yield event.plain_result("对方在保护名单里，不能被画像。")
            return

        if self.config.bool_of("privacy.allow_opt_out") and await self.astore.is_opted_out(
            platform,
            group_id,
            target_id,
        ):
            yield event.plain_result("对方已用「棱镜隐身」退出画像，尊重一下。")
            return

        sender_key = f"{platform}:{group_id}:{sender_id}"
        target_key = f"{platform}:{group_id}:{target_id}"
        left = self._cooldown_left(
            self._sender_cooldown,
            sender_key,
            self.config.int_of("limits.user_cooldown_sec"),
        )
        if left and not is_admin:
            yield event.plain_result(f"你刚刚才发起过分析，请再等 {left} 秒。")
            return
        left = self._cooldown_left(
            self._target_cooldown,
            target_key,
            self.config.int_of("limits.target_cooldown_sec"),
        )
        if left and not is_admin:
            yield event.plain_result(f"这位群友刚被分析过，请再等 {left} 秒。")
            return

        quota = self.config.int_of("limits.group_daily_quota")
        day = time.strftime("%Y-%m-%d")
        if quota > 0 and group_id:
            used = await self.astore.quota_used(group_id, day)
            if used >= quota and not is_admin:
                yield event.plain_result(f"本群今天的分析次数已用完（{used}/{quota}），明天再来。")
                return

        job_key = f"{platform}:{group_id}:{target_id}:{spec.key}"
        if job_key in self._inflight:
            yield event.plain_result("同样的分析正在进行中，稍等一下就好。")
            return

        group_name = await self._group_display_name(event, platform, group_id)
        self._inflight.add(job_key)
        started = time.perf_counter()
        ok = False
        backend = ""
        error = ""
        try:
            quiet = self.config.bool_of("behavior.quiet_progress")
            who = target_hint or target_id
            if not quiet:
                yield event.plain_result(
                    scanning.intro_line(
                        self._scan_plan(platform, group_id),
                        target_name=who,
                        label=spec.label,
                    ),
                )

            bundle, scan = await self._gather(event, platform, group_id, target_id)
            #: 群里只留短提示，翻页细节全部进后台日志 —— 排查靠 logger，不靠刷屏。
            logger.info(
                "[人格棱镜] %s",
                scanning.progress_log(
                    scan,
                    target_name=who,
                    label=spec.label,
                    sampled=bundle.stats.sampled,
                ),
            )
            logger.debug("[人格棱镜] 采集诊断 %s", scan.to_dict())
            min_messages = self.config.int_of("collect.min_messages")
            if not bundle.enough or bundle.stats.sampled < min_messages:
                error = "样本不足"
                logger.info(
                    "[人格棱镜] 样本不足（%s 条 < %s 条），完整诊断：%s",
                    bundle.stats.sampled,
                    min_messages,
                    " / ".join(scanning.diagnose(scan)),
                )
                #: 只说"发言太少"会让人以为回溯没跑。这里把诊断一并给出（群里只贴前几条）。
                yield event.plain_result(
                    scanning.shortfall_reply(
                        scan,
                        target_name=who,
                        label=spec.label,
                        sampled=bundle.stats.sampled,
                        min_messages=min_messages,
                    ),
                )
                return

            if not quiet:
                note = scanning.progress_line(
                    scan,
                    target_name=who,
                    label=spec.label,
                    sampled=bundle.stats.sampled,
                )
                if note:
                    yield event.plain_result(note)

            self._sender_cooldown[sender_key] = time.time()
            self._target_cooldown[target_key] = time.time()

            profile = await self._fetch_profile(event, group_id, target_id)
            target_name = target_hint or (profile.display_name if profile else "")
            if not target_name:
                target_name = await self.astore.latest_user_name(platform, group_id, target_id) or target_id

            portrait, model = await self.analyzer.analyze(
                spec,
                bundle,
                target_name=target_name,
                group_name=group_name,
                profile=profile,
                umo=event.unified_msg_origin,
            )
            with contextlib.suppress(Exception):
                await self._restore_scenes(platform, group_id, portrait, bundle, target_id)

            theme_choice = cards.normalize_theme_choice(
                await self.astore.group_theme(platform, group_id) or self.config.str_of("render.theme"),
            )
            # 自动挡：按这张画像的性子挑主题，并避开本群最近两张卡用过的那两套。
            # 落库存的是挑中的真主题，所以 WebUI 里重看这条记录不会变脸。
            theme = cards.resolve_theme(
                theme_choice,
                portrait,
                seed=f"{platform}:{group_id}:{target_id}",
                avoid=(
                    await self.astore.recent_themes(platform, group_id, limit=2)
                    if cards.is_auto_theme(theme_choice)
                    else ()
                ),
            )
            record = PortraitRecord(
                platform=platform,
                umo=event.unified_msg_origin,
                group_id=group_id,
                group_name=group_name,
                user_id=target_id,
                user_name=target_name,
                kind=spec.key,
                kind_label=spec.label,
                theme=theme,
                payload=portrait.to_dict(),
                text=portrait.to_plain_text(spec.label),
                sample_size=bundle.stats.sampled,
                corpus_chars=bundle.stats.chars,
                confidence=portrait.confidence,
                model=model,
            )
            record_id = await self.astore.save_portrait(
                record,
                history_limit=self.config.int_of("behavior.history_limit"),
            )

            ctx = CardContext(
                title=spec.label,
                kind_label=spec.label,
                target_name=target_name,
                target_id=target_id,
                group_name=group_name,
                avatar_url=_AVATAR_TEMPLATE.format(uid=target_id) if platform == "aiocqhttp" else "",
                theme=theme,
                footer_note=self.config.str_of("render.footer_note"),
                model=model,
                sample_size=bundle.stats.sampled,
                total_corpus=bundle.stats.total,
                span_days=bundle.stats.span_days,
                show_evidence=self.config.bool_of("render.show_evidence"),
                show_avatar=self.config.bool_of("render.show_avatar"),
            )
            record_key = f"{spec.key}_{record_id}"
            layout = normalize_layout(spec.layout, spec.structured)
            if layout == "card":
                result = await self.renderer.render(portrait, ctx, record_key=record_key)
            elif layout == "markdown":
                # 「画像」系列输出的是自由排版长文，走 Markdown 卡片，主题与结构化卡片一致。
                result = await self.renderer.render_markdown(
                    portrait.raw_text or record.text,
                    ctx,
                    record_key=record_key,
                )
            else:
                # 纯文本玩法（人格克隆）的产物要能整段复制，出图反而没法用。
                result = RenderResult(backend="text", text=record.text)
            backend = result.backend
            self._last_backend = result.backend
            if result.card_file:
                await self.astore.attach_card(record_id, result.card_file)

            if quota > 0 and group_id:
                await self.astore.bump_quota(group_id, day)

            await self.astore.touch_group(platform, group_id, group_name=group_name)
            ok = True
            if result.image_path:
                yield event.image_result(result.image_path)
            else:
                yield event.plain_result(result.text or record.text)
        except AnalyzeError as exc:
            error = str(exc)
            yield event.plain_result(f"分析失败：{exc}")
        except Exception as exc:  # 兜底：异常冒泡会打断整条消息管线
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("[人格棱镜] 生成%s时出错", spec.label)
            yield event.plain_result("生成失败了，日志里有详细堆栈。")
        finally:
            self._inflight.discard(job_key)
            with contextlib.suppress(Exception):
                await self.astore.log_run(
                    group_id=group_id,
                    user_id=target_id,
                    kind=spec.key,
                    ok=ok,
                    backend=backend,
                    error=error[:300],
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )

    # -------------------------------------------------------------- 恋爱诊断

    @staticmethod
    def _tz_offset() -> int:
        """本机时区相对 UTC 的小时数。用于判定「深夜发言」。"""
        with contextlib.suppress(Exception):
            local = time.localtime()
            seconds = time.altzone if (time.daylight and local.tm_isdst) else time.timezone
            return int(-seconds // 3600)
        return 8

    def _love_day(self, ts: float | None = None) -> tuple[str, int, int]:
        """按 love.day_start_hour 切「一天」，返回 (日期串, 窗口起点, 窗口终点)。

        默认 4 点换日：凌晨三点的发言算前一天，熬夜党不会被切成两半。
        """
        start_hour = max(0, min(12, self.config.int_of("love.day_start_hour")))
        now = int(ts if ts is not None else time.time())
        local = time.localtime(now)
        midnight = int(
            time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)),
        )
        start = midnight + start_hour * 3600
        if now < start:
            start -= 86400
        return time.strftime("%Y-%m-%d", time.localtime(start)), start, start + 86400

    def _love_window(self, days: int = 1) -> tuple[str, int, int, int]:
        """按天数给出统计窗口，返回 (结算日, 起点, 终点, 实际天数)。

        days=1 就是「今天」，days=7 是「含今天在内的最近 7 天」。结算日始终记今天：
        排行榜和趋势都按结算日存取，换口径看几天不会污染历史数据。
        """
        span = max(1, min(30, int(days or 1)))
        day, start, end = self._love_day()
        return day, start - (span - 1) * 86400, end, span

    async def _repair_corpus_ts(self, platform: str, group_id: str) -> int:
        """当日窗口一条都筛不出来时，检查并就地折算异常的语料时间戳。

        个别协议端把消息时间给成毫秒（或字段名不是 time），入库后这些行的时间戳会
        落在几万年后，「今天」的窗口自然永远是空的 —— 表现就是恋爱成分恒为 0 句，
        而画像却正常（画像不按时间筛）。这里一个进程只对同一个群自愈一次。
        """
        scope = f"{platform}:{group_id}"
        if scope in self._ts_repaired:
            return 0
        self._ts_repaired.add(scope)
        fixed = 0
        with contextlib.suppress(Exception):
            health = await self.astore.corpus_ts_health(platform, group_id)
            if health.get("future"):
                fixed = int(await self.astore.repair_corpus_ts(platform, group_id) or 0)
                logger.warning(
                    "[人格棱镜] 群 %s 有 %s 条语料时间戳量级异常（疑似毫秒），已折算 %s 条。",
                    group_id,
                    health.get("future"),
                    fixed,
                )
            elif health.get("missing"):
                logger.warning(
                    "[人格棱镜] 群 %s 有 %s 条语料没有时间戳，协议端可能没下发 time 字段，"
                    "这些语料不参与按天统计（画像不受影响）。",
                    group_id,
                    health.get("missing"),
                )
        return fixed

    async def _corpus_shortfall_note(
        self,
        platform: str,
        group_id: str,
        day_rows: int,
    ) -> str:
        """样本不足时给一行短诊断，帮用户区分「真没说话」和「采集没生效」。"""
        try:
            stats = await self.astore.corpus_stats(platform, group_id)
            health = await self.astore.corpus_ts_health(platform, group_id)
        except Exception:  # 诊断失败不能影响主流程
            return ""
        total = int(stats.get("total") or 0)
        if day_rows:
            return ""
        if not total:
            return "本群还没有语料，先让大家聊几句，或发「棱镜诊断」看采集是否正常。"
        bad = int(health.get("future") or 0) + int(health.get("missing") or 0)
        if bad:
            return f"本群 {total} 条语料里有 {bad} 条时间戳异常，已尝试修正，请再发一次。"
        newest = _fmt_ts(stats.get("newest")) if stats.get("newest") else "未知"
        return f"本群今天没采到发言（库里共 {total} 条，最近一条 {newest}）。"

    def _love_weights(self) -> love.LoveWeights:
        return love.weights_from_sensitivity(self.config.int_of("love.sensitivity"))

    async def _love_day_stats(
        self,
        platform: str,
        group_id: str,
        *,
        day: str,
        start: int,
        end: int,
        days: int = 1,
    ) -> tuple[dict[str, love.LoveInputs], list[dict[str, Any]]]:
        """算出窗口内每个人的行为计数，并带回原始语料行。

        计数是从语料库现算的，不依赖「插件装上之后才开始攒」的独立日表 ——
        这也是相对上游最实用的差别：装上当天就能出结果。

        戳一戳 / 表情回应 / 撤回这类 notice 拿不到时间戳窗口，只能按天累加，
        所以窗口跨了几天就把这几天的计数逐日加起来。
        """
        rows = await self.astore.window_rows(platform, group_id, start, end)
        if not rows and await self._repair_corpus_ts(platform, group_id):
            rows = await self.astore.window_rows(platform, group_id, start, end)
        stats = love.compute_day_inputs(rows, tz_offset=self._tz_offset())
        if self.config.bool_of("love.notice_collect"):
            span = max(1, int(days or 1))
            for offset in range(span):
                bucket = (
                    day
                    if offset == 0
                    else time.strftime("%Y-%m-%d", time.localtime(end - 86400 * (offset + 1)))
                )
                with contextlib.suppress(Exception):
                    extra = await self.astore.interaction_counts(platform, group_id, bucket)
                    for uid, counts in extra.items():
                        bonus = love.LoveInputs(**counts)
                        base = stats.get(uid)
                        stats[uid] = base.merge(bonus) if base else bonus
        return stats, rows

    async def _love_metrics(
        self,
        platform: str,
        group_id: str,
        user_id: str,
        *,
        day: str,
        start: int,
        stats: dict[str, love.LoveInputs],
        days: int = 1,
    ) -> love.LoveMetrics:
        previous: int | None = None
        #: 趋势只在「按天」口径下有意义。看近 7 天的时候拿昨天的单日总分来比，
        #: 得出的箭头是假的，索引直接不比。
        if days <= 1 and self.config.bool_of("love.show_trend"):
            yesterday = time.strftime("%Y-%m-%d", time.localtime(start - 86400))
            with contextlib.suppress(Exception):
                previous = await self.astore.love_total(platform, group_id, user_id, yesterday)
        return love.compute_metrics(
            stats.get(user_id) or love.LoveInputs(),
            weights=self._love_weights(),
            yesterday_total=previous,
            days=days,
        )

    async def _love_flow(self, event: AstrMessageEvent, days: int = 1, command: str = ""):
        """恋爱诊断卡的完整链路。与画像共用渲染、落库、限流与隐私开关。

        days 是统计窗口：1 就是今天，7 就是最近 7 天。四维分数由本地公式算，
        判词交给模型，模型翻车就整段退回公式文案。
        """
        spec = self.library.get("love")
        if spec is None:
            yield event.plain_result("恋爱诊断的提示词不存在，请到 WebUI 的「提示词」页检查。")
            return
        if not self.config.bool_of("love.enabled"):
            yield event.plain_result("恋爱诊断玩法已在配置里关闭。")
            return

        platform, group_id = self._scope(event)
        if not group_id:
            yield event.plain_result("恋爱诊断看的是群里的互动，私聊没得算。")
            return
        if not self.config.group_allowed(group_id):
            return

        sender_id = str(event.get_sender_id() or "")
        target_id, target_hint = await self._resolve_target(event, command or spec.command)
        if not target_id:
            yield event.plain_result("没认出要诊断谁，@一下对方或者跟上 QQ 号。")
            return

        is_admin = bool(event.is_admin())
        if self.config.bool_of("behavior.allow_self_only") and target_id != sender_id and not is_admin:
            yield event.plain_result("当前配置只允许诊断自己。")
            return
        if self.config.is_protected(target_id) and not is_admin:
            yield event.plain_result("对方在保护名单里，不参与这个玩法。")
            return
        if self.config.bool_of("privacy.allow_opt_out") and await self.astore.is_opted_out(
            platform,
            group_id,
            target_id,
        ):
            yield event.plain_result("对方已用「棱镜隐身」退出统计，尊重一下。")
            return

        sender_key = f"{platform}:{group_id}:{sender_id}"
        left = self._cooldown_left(
            self._sender_cooldown,
            sender_key,
            self.config.int_of("limits.user_cooldown_sec"),
        )
        if left and not is_admin:
            yield event.plain_result(f"你刚刚才发起过分析，请再等 {left} 秒。")
            return

        job_key = f"{platform}:{group_id}:{target_id}:love"
        if job_key in self._inflight:
            yield event.plain_result("同样的分析正在进行中，稍等一下就好。")
            return

        day, start, end, span = self._love_window(days)
        min_messages = max(1, self.config.int_of("love.min_messages"))
        group_name = await self._group_display_name(event, platform, group_id)
        self._inflight.add(job_key)
        started = time.perf_counter()
        ok = False
        backend = ""
        error = ""
        try:
            who = target_hint or target_id
            window = love.span_label(span)
            if not self.config.bool_of("behavior.quiet_progress"):
                yield event.plain_result(f"正在给 {who} 做{window}恋爱诊断…")

            stats, rows = await self._love_day_stats(
                platform,
                group_id,
                day=day,
                start=start,
                end=end,
                days=span,
            )
            mine = [row for row in rows if str(row.get("user_id") or "") == target_id]
            if len(mine) < min_messages:
                #: 窗口内的语料还没进库（刚装上、或库里已经攒够 max_messages 导致回溯不出网）。
                #: 无条件补拉最新一页群历史再重算 —— 这是「今天恒 0 句」最常见的成因。
                topped = await self._topup_latest(event, platform, group_id)
                if topped:
                    stats, rows = await self._love_day_stats(
                        platform,
                        group_id,
                        day=day,
                        start=start,
                        end=end,
                        days=span,
                    )
                    mine = [row for row in rows if str(row.get("user_id") or "") == target_id]

            logger.info(
                "[人格棱镜] 恋爱诊断：%s 于 %s（%s）有效发言 %s 条（同窗口本群 %s 条 / %s 人）",
                who,
                day,
                window,
                len(mine),
                len(rows),
                len(stats),
            )
            if len(mine) < min_messages:
                error = "样本不足"
                note = await self._corpus_shortfall_note(platform, group_id, len(rows))
                text = (
                    f"{who} {window}只说了 {len(mine)} 句，样本不够出诊断"
                    f"（至少 {min_messages} 句）。多聊几句，或者加个天数试试：恋爱诊断 7 天。"
                )
                if note:
                    text += "\n" + note
                yield event.plain_result(text)
                return

            self._sender_cooldown[sender_key] = time.time()
            metrics = await self._love_metrics(
                platform,
                group_id,
                target_id,
                day=day,
                start=start,
                stats=stats,
                days=span,
            )

            profile = await self._fetch_profile(event, group_id, target_id)
            target_name = target_hint or (profile.display_name if profile else "")
            if not target_name:
                target_name = love.collect_names(mine).get(target_id, "") or target_id

            bundle = collector.build_bundle(
                mine,
                max_messages=max(20, self.config.int_of("collect.max_messages")),
                min_chars=self.config.int_of("collect.min_chars"),
                filter_commands=self.config.bool_of("collect.filter_commands"),
                drop_urls=self.config.bool_of("collect.strip_urls"),
                redact=self.config.bool_of("privacy.redact_pii"),
                fold=self.config.bool_of("collect.fold_repeats"),
                sampling=self.config.str_of("collect.sampling"),
                scanned=len(mine),
                from_cache=True,
            )

            llm_portrait = None
            model = ""
            if self.config.bool_of("love.llm_commentary"):
                try:
                    llm_portrait, model = await self.analyzer.analyze(
                        spec,
                        bundle,
                        target_name=target_name,
                        group_name=group_name,
                        profile=profile,
                        umo=event.unified_msg_origin,
                        extra_facts=love.metrics_prompt_block(metrics, target_name=target_name),
                    )
                except AnalyzeError as exc:
                    #: 判词写不出来不影响分数，退回纯公式文案，别让用户白等。
                    logger.warning("[人格棱镜] 恋爱判词生成失败，回退公式文案：%s", exc)

            names = love.collect_names(rows)
            #: 变量名不能叫 scenes：会遮蔽同名模块，函数里后面就用不了 scenes.enrich_all。
            local_scenes = love.build_scenes(
                rows,
                target_id,
                names=names,
                tz_offset=self._tz_offset(),
            )
            if llm_portrait is not None:
                #: 模型只看得到本人的发言，它挑出的原话要回库里配上前后文才有气泡。
                pending = [e for e in llm_portrait.evidence if not e.dialogue and e.quote.strip()]
                if pending:
                    with contextlib.suppress(Exception):
                        scenes.enrich_all(
                            pending,
                            bundle.messages,
                            rows,
                            user_id=target_id,
                            names=names,
                            label="现场片段",
                        )
            sample_note = (
                f"取证范围：{window}本群 {len(rows)} 条发言，其中 {who} 名下 {len(mine)} 条。"
                "四维分数由本地公式实算，判词与证供解读由模型生成。"
            )
            portrait = love.merge_portrait(
                metrics,
                llm_portrait,
                target_name=target_name,
                seed=f"{target_id}|{day}|{span}",
                scenes=local_scenes,
                sample_note=sample_note,
            )

            theme_choice = cards.normalize_theme_choice(self.config.str_of("love.theme"))
            theme = cards.resolve_theme(
                theme_choice,
                portrait,
                seed=f"{platform}:{group_id}:{target_id}:{day}:{span}",
                avoid=(
                    await self.astore.recent_themes(platform, group_id, limit=2)
                    if cards.is_auto_theme(theme_choice)
                    else ()
                ),
            )
            record = PortraitRecord(
                platform=platform,
                umo=event.unified_msg_origin,
                group_id=group_id,
                group_name=group_name,
                user_id=target_id,
                user_name=target_name,
                kind="love",
                kind_label=spec.label,
                theme=theme,
                payload=portrait.to_dict(),
                text=portrait.to_plain_text(spec.label),
                sample_size=len(mine),
                corpus_chars=bundle.stats.chars,
                confidence=portrait.confidence,
                model=model,
            )
            record_id = await self.astore.save_portrait(
                record,
                history_limit=self.config.int_of("behavior.history_limit"),
            )

            ctx = CardContext(
                title=spec.label,
                kind_label=f"{spec.label} · {window}" if span > 1 else f"{spec.label} · {day}",
                target_name=target_name,
                target_id=target_id,
                group_name=group_name,
                avatar_url=_AVATAR_TEMPLATE.format(uid=target_id) if platform == "aiocqhttp" else "",
                theme=theme,
                footer_note=self.config.str_of("render.footer_note"),
                model=model,
                sample_size=len(mine),
                total_corpus=len(rows),
                span_days=span,
                show_evidence=self.config.bool_of("render.show_evidence"),
                show_avatar=self.config.bool_of("render.show_avatar"),
            )
            result = await self.renderer.render(portrait, ctx, record_key=f"love_{record_id}")
            backend = result.backend
            self._last_backend = result.backend
            if result.card_file:
                await self.astore.attach_card(record_id, result.card_file)
            if span <= 1:
                #: 排行榜与趋势都按「单日」口径存取，多天窗口的分数不能写进去污染历史。
                with contextlib.suppress(Exception):
                    await self.astore.set_love_total(platform, group_id, target_id, day, metrics.total)
            await self.astore.touch_group(platform, group_id, group_name=group_name)
            ok = True
            if result.image_path:
                yield event.image_result(result.image_path)
            else:
                yield event.plain_result(result.text or record.text)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("[人格棱镜] 生成恋爱诊断时出错")
            yield event.plain_result("诊断失败了，日志里有详细堆栈。")
        finally:
            self._inflight.discard(job_key)
            with contextlib.suppress(Exception):
                await self.astore.log_run(
                    group_id=group_id,
                    user_id=target_id,
                    kind="love",
                    ok=ok,
                    backend=backend,
                    error=error[:300],
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )

    @filter.command("恋爱诊断")
    async def cmd_love(self, event: AstrMessageEvent):
        """赛博恋爱演化诊断：出一张四维卡片。可跟天数，如「恋爱诊断 7」。"""
        days = _parse_days(event.get_message_str() or "", "恋爱诊断")
        async for result in self._love_flow(event, days, "恋爱诊断"):
            yield result

    @filter.command("棱镜恋爱")
    async def cmd_love_alias(self, event: AstrMessageEvent):
        """v1.2.1 及以前的旧指令名，保留为别名。"""
        if not self.config.bool_of("compat.love_commands"):
            yield event.plain_result("「棱镜恋爱」已在配置里关闭，请改用「恋爱诊断」。")
            return
        days = _parse_days(event.get_message_str() or "", "棱镜恋爱")
        async for result in self._love_flow(event, days, "棱镜恋爱"):
            yield result

    @filter.command("今日人设")
    async def cmd_love_legacy(self, event: AstrMessageEvent):
        """兼容上游 astrbot_plugin_love_formula 的指令名，走同一条链路。"""
        if not self.config.bool_of("compat.love_commands"):
            yield event.plain_result("「今日人设」已在配置里关闭，可以改用「恋爱诊断」。")
            return
        days = _parse_days(event.get_message_str() or "", "今日人设")
        async for result in self._love_flow(event, days, "今日人设"):
            yield result

    async def _love_board_flow(self, event: AstrMessageEvent, days: int = 1):
        """本群恋爱诊断排行。只跑公式不调模型，所以很快。"""
        if not self.config.bool_of("love.enabled"):
            yield event.plain_result("恋爱诊断玩法已在配置里关闭。")
            return
        platform, group_id = self._scope(event)
        if not group_id:
            yield event.plain_result("排行榜只在群里有意义。")
            return
        if not self.config.group_allowed(group_id):
            return

        day, start, end, span = self._love_window(days)
        window = love.span_label(span)
        stats, rows = await self._love_day_stats(
            platform,
            group_id,
            day=day,
            start=start,
            end=end,
            days=span,
        )
        #: 老群库里可能已经攒够 max_messages，回溯不会出网，先补拉最新一页再说。
        if not rows and await self._topup_latest(event, platform, group_id):
            stats, rows = await self._love_day_stats(
                platform,
                group_id,
                day=day,
                start=start,
                end=end,
                days=span,
            )
        if not rows:
            yield event.plain_result(f"本群{window}还没有攒到语料，先聊起来再说。")
            return

        hidden: set[str] = set()
        if self.config.bool_of("privacy.allow_opt_out"):
            with contextlib.suppress(Exception):
                hidden = set(await self.astore.opted_out_ids(platform, group_id))
        min_messages = max(1, self.config.int_of("love.min_messages"))
        weights = self._love_weights()
        names = love.collect_names(rows)
        ranked = [
            (uid, love.compute_metrics(item, weights=weights, days=span))
            for uid, item in stats.items()
            if item.msg_sent >= min_messages
            and uid not in hidden
            and not self.config.is_protected(uid)
        ]
        if not ranked:
            yield event.plain_result(
                f"本群{window}还没有人发言够 {min_messages} 句，榜单空着呢。",
            )
            return
        ranked.sort(key=lambda pair: (-pair[1].total, -pair[1].vibe, pair[0]))
        size = max(3, min(30, self.config.int_of("love.leaderboard_size")))
        medals = ("🥇", "🥈", "🥉")
        lines = [f"本群{window}恋爱诊断榜（{len(ranked)} 人在榜）"]
        for index, (uid, metrics) in enumerate(ranked[:size]):
            mark = medals[index] if index < len(medals) else f"{index + 1}."
            who = names.get(uid, uid)
            lines.append(
                f"{mark} {who} {metrics.total} 分 · {metrics.archetype.label}",
            )
        lines.append("发「恋爱诊断」看自己的详细卡片。")
        yield event.plain_result("\n".join(lines))

    @filter.command("恋爱诊断榜")
    async def cmd_love_board(self, event: AstrMessageEvent):
        """本群恋爱诊断排行，可跟天数，如「恋爱诊断榜 7」。"""
        days = _parse_days(event.get_message_str() or "", "恋爱诊断榜")
        async for result in self._love_board_flow(event, days):
            yield result

    @filter.command("棱镜恋爱榜")
    async def cmd_love_board_alias(self, event: AstrMessageEvent):
        """v1.2.1 及以前的旧指令名，保留为别名。"""
        if not self.config.bool_of("compat.love_commands"):
            yield event.plain_result("「棱镜恋爱榜」已在配置里关闭，请改用「恋爱诊断榜」。")
            return
        days = _parse_days(event.get_message_str() or "", "棱镜恋爱榜")
        async for result in self._love_board_flow(event, days):
            yield result

    # ------------------------------------------------------------------ 指令

    @filter.command("棱镜画像")
    async def cmd_portrait(self, event: AstrMessageEvent):
        """给群友生成一份结构化人格画像卡片。"""
        async for result in self._execute(event, self.library.get("portrait")):
            yield result

    @filter.command("棱镜赞赏")
    async def cmd_praise(self, event: AstrMessageEvent):
        """只挑优点，输出一份夸夸卡。"""
        async for result in self._execute(event, self.library.get("praise")):
            yield result

    @filter.command("棱镜锐评")
    async def cmd_roast(self, event: AstrMessageEvent):
        """毒舌但不越界的锐评卡。"""
        async for result in self._execute(event, self.library.get("roast")):
            yield result

    @filter.command("棱镜姻缘")
    async def cmd_match(self, event: AstrMessageEvent):
        """基于互动数据推测最合适的群内搭子。"""
        async for result in self._execute(event, self.library.get("match")):
            yield result

    @filter.command("棱镜克隆")
    async def cmd_clone(self, event: AstrMessageEvent):
        """把群友的说话风格提炼成一段可直接粘贴的人格提示词。"""
        async for result in self._clone_flow(event, "clone", "棱镜克隆"):
            yield result

    async def _clone_flow(self, event: AstrMessageEvent, key: str, command: str):
        """人格克隆的公共前置校验。

        权限用配置项而不是 @permission_type 装饰器控制：上游的「克隆人格」对所有人
        开放，而本插件默认只给管理员，两套指令必须走同一把锁，否则等于留了后门。
        """
        if not self.config.bool_of("persona_clone.enabled"):
            yield event.plain_result("人格克隆已在配置中关闭。")
            return
        if self.config.bool_of("persona_clone.require_admin") and not event.is_admin():
            yield event.plain_result("人格克隆目前仅限管理员使用。")
            return
        async for result in self._execute(event, self.library.get(key)):
            yield result
        async for extra in self._sync_bot_identity(event, command):
            yield extra

    async def _sync_bot_identity(self, event: AstrMessageEvent, command: str = "棱镜克隆"):
        """可选地把机器人的昵称/头像同步成克隆对象的。

        上游的「切换人格」会无条件改掉机器人的全局昵称与头像，而且没有恢复入口，
        属于高危副作用。这里两个开关默认都是关的，必须显式打开才会动。
        """
        want_nick = self.config.bool_of("persona_clone.sync_bot_nickname")
        want_avatar = self.config.bool_of("persona_clone.sync_bot_avatar")
        if not (want_nick or want_avatar):
            return
        client = getattr(event, "bot", None)
        if client is None:
            return
        target_id, target_name = await self._resolve_target(event, command)
        await self._backup_bot_identity(event, event.unified_msg_origin)
        done: list[str] = []
        if want_nick and target_name:
            with contextlib.suppress(Exception):
                await client.api.call_action("set_qq_profile", nickname=target_name)
                done.append("昵称")
        if want_avatar and target_id:
            with contextlib.suppress(Exception):
                await client.api.call_action(
                    "set_qq_avatar",
                    file=_AVATAR_TEMPLATE.format(uid=target_id),
                )
                done.append("头像")
        if done:
            yield event.plain_result(
                "已按配置同步机器人的" + "与".join(done) + "（这是全局改动，可在配置中关闭）。",
            )

    # -------------------------------------------- 画像系列（兼容上游 portrayal）

    def _legacy_gate(self) -> str:
        """「画像」系列的总开关。返回空串表示放行，否则返回给用户看的提示语。"""
        if self.config.bool_of("compat.legacy_commands"):
            return ""
        return "「画像」系列指令已在配置里关闭，可以改用「棱镜画像」等棱镜系列指令。"

    async def _legacy(self, event: AstrMessageEvent, command: str):
        """「画像」系列复用同一条生成链路，区别只在提示词输出长文而不是 JSON。"""
        blocked = self._legacy_gate()
        if blocked:
            yield event.plain_result(blocked)
            return
        async for result in self._execute(event, self.library.get(LEGACY_KEYS[command])):
            yield result

    @filter.command("画像")
    async def cmd_legacy_portrait(self, event: AstrMessageEvent):
        """上游同款的综合画像长文，渲染成 Markdown 卡片。"""
        async for result in self._legacy(event, "画像"):
            yield result

    @filter.command("正画像")
    async def cmd_legacy_positive(self, event: AstrMessageEvent):
        """只写优点的长文画像。"""
        async for result in self._legacy(event, "正画像"):
            yield result

    @filter.command("负画像")
    async def cmd_legacy_negative(self, event: AstrMessageEvent):
        """只写缺点的长文画像。"""
        async for result in self._legacy(event, "负画像"):
            yield result

    @filter.command("找对象")
    async def cmd_legacy_match(self, event: AstrMessageEvent):
        """在群里挑一个最合适的搭子。"""
        async for result in self._legacy(event, "找对象"):
            yield result

    @filter.command("克隆人格")
    async def cmd_legacy_clone(self, event: AstrMessageEvent):
        """把群友的说话风格提炼成人格提示词，供「切换人格」使用。"""
        blocked = self._legacy_gate()
        if blocked:
            yield event.plain_result(blocked)
            return
        async for result in self._clone_flow(event, "legacy_clone", "克隆人格"):
            yield result

    @filter.command("查看画像")
    async def cmd_legacy_latest(self, event: AstrMessageEvent):
        """以纯文本重发最近一次画像，方便直接复制。

        和「棱镜档案」的区别：那条重发卡片图片，这条给的是可复制的文字。
        """
        blocked = self._legacy_gate()
        if blocked:
            yield event.plain_result(blocked)
            return
        platform, group_id = self._scope(event)
        target_id, target_hint = await self._resolve_target(event, "查看画像")
        record = await self.astore.latest_portrait(platform, group_id, target_id)
        if record is None:
            yield event.plain_result("还没有这个人的画像记录，先用「画像」生成一份。")
            return
        stamp = _fmt_ts(record.created_at, "%Y-%m-%d %H:%M")
        who = record.user_name or target_hint or target_id
        head = f"{who} · {record.kind_label or record.kind}"
        if stamp:
            head = f"{head}（{stamp}）"
        yield event.plain_result(f"{head}\n\n{record.text or '（内容已丢失）'}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换人格")
    async def cmd_legacy_switch(self, event: AstrMessageEvent):
        """把当前对话切换成某人的克隆人格。

        上游是直接覆盖全局默认人格、顺手改掉机器人昵称头像且没有回退路径。
        这里只改当前会话所在的那条对话分支，并把原人格备份下来供「恢复人格」还原。
        """
        blocked = self._legacy_gate()
        if blocked:
            yield event.plain_result(blocked)
            return
        if not self.config.bool_of("persona_clone.enabled"):
            yield event.plain_result("人格克隆已在配置中关闭。")
            return
        platform, group_id = self._scope(event)
        target_id, target_hint = await self._resolve_target(event, "切换人格")
        if self.config.is_protected(target_id):
            yield event.plain_result("对方在保护名单里，不能被克隆。")
            return
        if self.config.bool_of("privacy.allow_opt_out") and await self.astore.is_opted_out(
            platform,
            group_id,
            target_id,
        ):
            yield event.plain_result("对方已经隐身，不能被克隆。")
            return
        prompt, record = await self._latest_clone_prompt(platform, group_id, target_id)
        if not prompt:
            yield event.plain_result("还没有这个人的人格克隆结果，先用「克隆人格」生成一份。")
            return
        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            yield event.plain_result("当前会话还没有对话，先发 /new 新建一个再切换。")
            return
        persona_id = f"{_PERSONA_ID_PREFIX}{target_id}"
        try:
            await self._upsert_persona(persona_id, prompt)
        except Exception as exc:
            logger.exception("[人格棱镜] 写入人格失败")
            yield event.plain_result(f"写入人格失败：{type(exc).__name__}: {exc}")
            return
        previous = ""
        with contextlib.suppress(Exception):
            conv = await conv_mgr.get_conversation(umo, cid, create_if_not_exists=False)
            previous = str(getattr(conv, "persona_id", "") or "")
        await sp.put_async(
            scope="umo",
            scope_id=umo,
            key=_SP_PERSONA_BACKUP,
            value={"persona_id": previous, "cid": cid},
        )
        await conv_mgr.update_conversation(umo, conversation_id=cid, persona_id=persona_id)
        notes: list[str] = []
        if self.config.bool_of("persona_clone.clear_history_on_switch"):
            with contextlib.suppress(Exception):
                await conv_mgr.update_conversation(umo, conversation_id=cid, history=[])
            notes.append("已清空当前对话的上下文")
        session_conf = await sp.get_async("umo", umo, "session_service_config", None)
        if isinstance(session_conf, dict) and session_conf.get("persona_id"):
            notes.append("本会话设置了会话级人格，优先级高于对话人格，可能盖掉这次切换")
        display = (record.user_name if record else "") or target_hint or target_id
        tail = ("\n" + "；".join(notes) + "。") if notes else ""
        yield event.plain_result(
            f"已切换到「{display}」的克隆人格，用「恢复人格」可以还原。{tail}",
        )
        async for extra in self._sync_bot_identity(event, "切换人格"):
            yield extra

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("恢复人格")
    async def cmd_legacy_restore(self, event: AstrMessageEvent):
        """还原「切换人格」的改动：对话人格、上下文、机器人昵称与头像。"""
        blocked = self._legacy_gate()
        if blocked:
            yield event.plain_result(blocked)
            return
        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        backup = await sp.get_async("umo", umo, _SP_PERSONA_BACKUP, None)
        cid = ""
        target_persona = ""
        if isinstance(backup, dict):
            cid = str(backup.get("cid") or "")
            target_persona = str(backup.get("persona_id") or "")
        if not cid:
            cid = await conv_mgr.get_curr_conversation_id(umo) or ""
        if not cid:
            yield event.plain_result("当前会话没有可恢复的对话。")
            return
        if not target_persona:
            # 没有备份就回落到全局默认人格；连默认人格都没配就显式关掉人格注入。
            with contextlib.suppress(Exception):
                conf = self.context.get_config(umo=umo)
                settings = conf.get("provider_settings") or {}
                target_persona = str(settings.get("default_personality") or "")
        if not target_persona:
            target_persona = "[%None]"
        await conv_mgr.update_conversation(umo, conversation_id=cid, persona_id=target_persona)
        notes: list[str] = []
        if self.config.bool_of("persona_clone.clear_history_on_switch"):
            with contextlib.suppress(Exception):
                await conv_mgr.update_conversation(umo, conversation_id=cid, history=[])
            notes.append("已清空对话上下文")
        notes.extend(await self._restore_bot_identity(event, umo))
        with contextlib.suppress(Exception):
            await sp.remove_async(scope="umo", scope_id=umo, key=_SP_PERSONA_BACKUP)
        label = (
            "无人格状态（不再注入人格）"
            if target_persona == "[%None]"
            else f"人格「{target_persona}」"
        )
        tail = ("\n" + "；".join(notes) + "。") if notes else ""
        yield event.plain_result(f"已恢复为{label}。{tail}")

    async def _latest_clone_prompt(
        self,
        platform: str,
        group_id: str,
        target_id: str,
    ) -> tuple[str, PortraitRecord | None]:
        """取最近一次可用的人格克隆文本，兼容两套指令产生的记录类型。"""
        for kind in CLONE_KINDS:
            record = await self.astore.latest_portrait(platform, group_id, target_id, kind)
            if record is None:
                continue
            prompt = str(record.payload.get("raw_text") or "").strip() or record.text.strip()
            if prompt:
                return prompt, record
        return "", None

    async def _upsert_persona(self, persona_id: str, prompt: str) -> None:
        """把克隆提示词写进 AstrBot 的人格列表：有则更新，无则新建。"""
        manager = self.context.persona_manager
        existing = None
        with contextlib.suppress(Exception):
            existing = await manager.get_persona(persona_id)
        if existing is None:
            await manager.create_persona(persona_id=persona_id, system_prompt=prompt)
            return
        kwargs: dict[str, Any] = {"persona_id": persona_id, "system_prompt": prompt}
        # tools / skills 不传等于「不修改」，所以只在确实有值时才回填。
        for name in ("tools", "skills"):
            value = getattr(existing, name, None)
            if value is not None:
                kwargs[name] = value
        await manager.update_persona(**kwargs)

    async def _backup_bot_identity(self, event: AstrMessageEvent, umo: str) -> None:
        """改机器人昵称/头像之前，先把原始身份备份到共享存储。"""
        want_nick = self.config.bool_of("persona_clone.sync_bot_nickname")
        want_avatar = self.config.bool_of("persona_clone.sync_bot_avatar")
        if not (want_nick or want_avatar):
            return
        existing = await sp.get_async("umo", umo, _SP_BOT_BACKUP, None)
        if isinstance(existing, dict) and existing:
            return  # 已有备份就不覆盖，否则连续切换会把真正的原始身份冲掉
        client = getattr(event, "bot", None)
        if client is None:
            return
        info: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            raw = await client.api.call_action("get_login_info") or {}
            info["nickname"] = str(raw.get("nickname") or "")
            info["user_id"] = str(raw.get("user_id") or "")
        if not info:
            return
        if want_avatar and info.get("user_id"):
            info["avatar_b64"] = await self._download_base64(
                _AVATAR_TEMPLATE.format(uid=info["user_id"]),
            )
        with contextlib.suppress(Exception):
            await sp.put_async(scope="umo", scope_id=umo, key=_SP_BOT_BACKUP, value=info)

    async def _restore_bot_identity(self, event: AstrMessageEvent, umo: str) -> list[str]:
        """还原机器人昵称与头像。

        头像必须用备份下来的 base64 回灌：同步时用的是「按 QQ 号取头像」的 URL，
        而那个 URL 现在指向的正是克隆对象，拿它恢复等于没恢复。
        """
        backup = await sp.get_async("umo", umo, _SP_BOT_BACKUP, None)
        if not isinstance(backup, dict) or not backup:
            return []
        client = getattr(event, "bot", None)
        if client is None:
            return []
        done: list[str] = []
        nickname = str(backup.get("nickname") or "")
        if nickname:
            with contextlib.suppress(Exception):
                await client.api.call_action("set_qq_profile", nickname=nickname)
                done.append("已还原机器人昵称")
        avatar_b64 = str(backup.get("avatar_b64") or "")
        if avatar_b64:
            with contextlib.suppress(Exception):
                await client.api.call_action("set_qq_avatar", file=f"base64://{avatar_b64}")
                done.append("已还原机器人头像")
        if done:
            with contextlib.suppress(Exception):
                await sp.remove_async(scope="umo", scope_id=umo, key=_SP_BOT_BACKUP)
        return done

    @staticmethod
    async def _download_base64(url: str, *, limit: int = 4 * 1024 * 1024) -> str:
        """下载一张图片并转成 base64；失败、非 200 或超限都返回空串。"""
        try:
            import aiohttp
        except ImportError:
            return ""
        raw = b""
        with contextlib.suppress(Exception):
            timeout = aiohttp.ClientTimeout(total=15)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url) as resp,
            ):
                if resp.status != 200:
                    return ""
                raw = await resp.content.read(limit + 1)
        if raw and len(raw) <= limit:
            return base64.b64encode(raw).decode("ascii")
        return ""

    @filter.command("棱镜帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """列出全部指令与当前渲染状态。

        默认渲染成一张分类速查卡（跟着本群卡片主题走）；渲染链路全挂或
        behavior.help_card 关掉时自动回落成纯文本，指令内容两者完全一致。
        """
        legacy_on = self.config.bool_of("compat.legacy_commands")
        prism_specs: list[PromptSpec] = []
        legacy_specs: list[PromptSpec] = []
        custom_specs: list[PromptSpec] = []
        love_spec: PromptSpec | None = None
        for spec in self.library.all_specs():
            if not spec.builtin:
                custom_specs.append(spec)
            elif spec.key in LEGACY_KEYS.values():
                legacy_specs.append(spec)
            elif spec.key == "love":
                # 恋爱诊断自成一支（有排行榜、能指定天数），单列一组更好找。
                love_spec = spec
            else:
                prism_specs.append(spec)

        platform, group_id = self._scope(event)
        theme_choice = cards.normalize_theme_choice(
            await self.astore.group_theme(platform, group_id) or self.config.str_of("render.theme"),
        )
        # 帮助卡没有画像可看，自动挡就单纯避开本群最近用过的那套，换个新鲜的。
        theme = cards.resolve_theme(
            theme_choice,
            None,
            seed=f"help:{platform}:{group_id}",
            avoid=(
                await self.astore.recent_themes(platform, group_id, limit=1)
                if cards.is_auto_theme(theme_choice)
                else ()
            ),
        )

        # 一份数据同时喂给卡片和纯文本，避免两边说的话对不上。
        groups: list[cards.HelpGroup] = [
            cards.HelpGroup(
                name="棱镜系列",
                desc="结构化信息卡：评分 + 雷达图 + 聊天现场证供",
                items=[cards.HelpItem(spec.command, spec.label) for spec in prism_specs],
            ),
        ]
        if love_spec is not None and self.config.bool_of("love.enabled"):
            groups.append(
                cards.HelpGroup(
                    name="恋爱诊断",
                    desc="四维互动指标 + 演化算式，可跟天数",
                    items=[
                        cards.HelpItem(love_spec.command, "出一张四维恋爱诊断卡"),
                        cards.HelpItem("恋爱诊断 7", "改看最近 7 天（1~30 天任填）"),
                        cards.HelpItem("恋爱诊断榜", "本群综合分排行，不调模型"),
                    ],
                ),
            )
        if legacy_on and legacy_specs:
            legacy_items = [cards.HelpItem(spec.command, spec.label) for spec in legacy_specs]
            legacy_items += [
                cards.HelpItem("查看画像", "用纯文本重发最近一次画像结果"),
                cards.HelpItem("切换人格", "让机器人扮演克隆出的人格", ("管理员",)),
                cards.HelpItem("恢复人格", "停止扮演，恢复原来的人格", ("管理员",)),
            ]
            groups.append(
                cards.HelpGroup(
                    name="画像系列",
                    desc="上游同款长文报告，改用本插件的卡片渲染",
                    items=legacy_items,
                ),
            )
        if custom_specs:
            groups.append(
                cards.HelpGroup(
                    name="自定义模板",
                    desc="在 WebUI 的「提示词」里增删改",
                    items=[cards.HelpItem(spec.command, spec.label) for spec in custom_specs],
                ),
            )
        groups += [
            cards.HelpGroup(
                name="查询",
                desc="不消耗模型额度",
                items=[
                    cards.HelpItem("棱镜档案", "重新发送最近一次画像卡片"),
                    cards.HelpItem("棱镜历史", "列出历史画像摘要"),
                    cards.HelpItem("棱镜缓存", "查看本群语料积累与回溯进度"),
                    cards.HelpItem("棱镜统计", "查看全局运行数据"),
                    cards.HelpItem("棱镜主题", "查看 / 切换本群卡片主题"),
                    cards.HelpItem("棱镜帮助", "就是本张卡"),
                ],
            ),
            cards.HelpGroup(
                name="隐私",
                desc="人人可用，随时可撤",
                items=[
                    cards.HelpItem("棱镜隐身", "把自己从画像范围内排除"),
                    cards.HelpItem("棱镜现身", "撤销隐身"),
                ],
            ),
            cards.HelpGroup(
                name="管理员",
                desc="维护语料与名单",
                items=[
                    cards.HelpItem("棱镜删除", "删除某人在本群的画像记录"),
                    cards.HelpItem("棱镜清缓存", "清空本群语料"),
                    cards.HelpItem("棱镜重扫", "重置历史回溯断点（不删语料）"),
                    cards.HelpItem("棱镜诊断", "实测协议端认哪种翻页方式"),
                    cards.HelpItem("棱镜拉黑", "把人加入保护名单"),
                    cards.HelpItem("棱镜放行", "从保护名单里移除"),
                ],
            ),
        ]

        total = sum(len(group.items) for group in groups)
        backends = " → ".join(self.renderer.backends())
        chain = f"{self.config.str_of('render.backend')}（{backends}）"

        lines = [
            f"人格棱镜 {PLUGIN_VERSION} · 指令一览",
            "",
            "目标写法通用：@对方 / 回复对方的消息 / 直接跟 QQ 号，都省略就是画自己。",
        ]
        for group in groups:
            lines += ["", f"【{group.name}】{group.desc}"]
            for item in group.items:
                tail = f"（{' '.join(item.aliases)}）" if item.aliases else ""
                lines.append(f"  {item.command} —— {item.label}{tail}")
        lines += [
            "",
            f"当前渲染链路：{chain}",
            f"本群卡片主题：{cards.describe_theme_choice(theme_choice, theme)}",
            "棱镜系列出结构化卡片，画像系列出上游同款长文卡，两者互不影响。",
            "更详细的配置与记录管理请打开 WebUI 的「人格棱镜」页面。",
        ]
        text = "\n".join(lines)

        if not self.config.bool_of("behavior.help_card"):
            yield event.plain_result(text)
            return

        card = cards.HelpCard(
            title="人格棱镜 · 指令速查",
            kicker=f"PERSONA PRISM {PLUGIN_VERSION}",
            subtitle=(
                "群友人格画像插件。目标写法通用：@对方 / 回复对方的消息 / 直接跟 QQ 号，"
                "都省略就是画自己。"
            ),
            groups=groups,
            stats=[
                (str(total), "条指令"),
                (str(len(groups)), "个分类"),
                (str(len(cards.THEMES)), "套卡片主题"),
                (cards.theme_label(theme_choice), "本群当前主题"),
            ],
            footers=[
                (
                    "语料从哪来",
                    [
                        "平时聊天被动入库",
                        "画像时自动回溯群历史",
                        "「棱镜缓存」查看积累进度",
                        "翻不动就发「棱镜诊断」",
                    ],
                ),
                (
                    "隐私与边界",
                    [
                        "「棱镜隐身」可随时退出",
                        "保护名单内的人不参与画像",
                        "语料按保留天数自动清理",
                        "结论由 AI 生成，仅供娱乐",
                    ],
                ),
                (
                    "工作台",
                    [
                        f"渲染链路 {chain}",
                        "「棱镜主题」切换本群主题",
                        "WebUI「人格棱镜」页管配置",
                        "记录按群 / 按人分类可查",
                    ],
                ),
            ],
            note="棱镜系列出结构化卡片，画像系列出上游同款长文卡，两者互不影响。",
        )
        ctx = CardContext(
            title="人格棱镜 · 指令速查",
            kind_label="指令速查",
            theme=theme,
            footer_note=self.config.str_of("render.footer_note"),
            show_avatar=False,
        )
        try:
            result = await self.renderer.render_help(card, ctx, text, record_key="help")
        except Exception as exc:  # 帮助指令永远不该因为渲染问题而失败
            logger.warning("[人格棱镜] 指令速查卡渲染失败，回落纯文本：%s", exc)
            yield event.plain_result(text)
            return
        self._last_backend = result.backend
        if result.image_path:
            yield event.image_result(result.image_path)
        else:
            yield event.plain_result(result.text or text)

    @filter.command("棱镜档案")
    async def cmd_latest(self, event: AstrMessageEvent):
        """重新发送最近一次的画像结果，不重复消耗模型额度。"""
        platform, group_id = self._scope(event)
        target_id, target_hint = await self._resolve_target(event, "棱镜档案")
        record = await self.astore.latest_portrait(platform, group_id, target_id)
        if record is None:
            yield event.plain_result("还没有这个人的画像记录，先用「棱镜画像」生成一份。")
            return
        card_path = self.cards_dir / record.card_file if record.card_file else None
        if card_path is not None and card_path.exists():
            yield event.image_result(str(card_path))
            return
        yield event.plain_result(record.text or f"{target_hint or target_id} 的画像内容已丢失。")

    @filter.command("棱镜历史")
    async def cmd_history(self, event: AstrMessageEvent):
        """列出某人在本群的历史画像摘要。"""
        platform, group_id = self._scope(event)
        target_id, target_hint = await self._resolve_target(event, "棱镜历史")
        rows = await self.astore.user_history(platform, group_id, target_id, limit=10)
        if not rows:
            yield event.plain_result("这个人还没有历史画像。")
            return
        lines = [f"{target_hint or target_id} 的画像历史（最近 {len(rows)} 条）："]
        for row in rows:
            summary = row.to_summary()
            stamp = _fmt_ts(summary["created_at"], "%m-%d %H:%M")
            headline = str(summary.get("headline") or "").strip() or "（无摘要）"
            conf = cards.confidence_label(float(summary.get("confidence") or 0))
            lines.append(
                f"  #{summary['id']} [{stamp}] {summary['kind_label']}·{conf} {headline}",
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("棱镜删除")
    async def cmd_purge(self, event: AstrMessageEvent):
        """删除某人在本群的全部画像记录（连带卡片文件）。"""
        _platform, group_id = self._scope(event)
        target_id, target_hint = await self._resolve_target(event, "棱镜删除")
        files = await self.astore.purge_records(group_id=group_id, user_id=target_id)
        self._unlink_cards(files)
        yield event.plain_result(f"已删除 {target_hint or target_id} 的 {len(files)} 条画像记录。")

    @filter.command("棱镜隐身")
    async def cmd_optout(self, event: AstrMessageEvent):
        """把自己排除在画像范围之外。"""
        if not self.config.bool_of("privacy.allow_opt_out"):
            yield event.plain_result("管理员关闭了自助退出功能。")
            return
        platform, group_id = self._scope(event)
        await self.astore.add_optout(
            platform,
            group_id,
            str(event.get_sender_id() or ""),
            user_name=event.get_sender_name() or "",
        )
        yield event.plain_result("已隐身：别人无法再对你发起画像。发送「棱镜现身」可恢复。")

    @filter.command("棱镜现身")
    async def cmd_optin(self, event: AstrMessageEvent):
        """撤销隐身。"""
        platform, group_id = self._scope(event)
        removed = await self.astore.remove_optout(platform, group_id, str(event.get_sender_id() or ""))
        yield event.plain_result("已恢复，可以被画像了。" if removed else "你本来就没有隐身。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("棱镜拉黑")
    async def cmd_protect(self, event: AstrMessageEvent):
        """把某人加入保护名单，任何人都不能给 TA 画像。"""
        target_id, target_hint = await self._resolve_target(event, "棱镜拉黑")
        current = self.config.list_of("privacy.protected_user_ids")
        if target_id in current:
            yield event.plain_result("对方已经在保护名单里了。")
            return
        current.append(target_id)
        self.config.set("privacy.protected_user_ids", current)
        self.config.save()
        yield event.plain_result(f"已把 {target_hint or target_id} 加入保护名单。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("棱镜放行")
    async def cmd_unprotect(self, event: AstrMessageEvent):
        """把某人从保护名单移除。"""
        target_id, target_hint = await self._resolve_target(event, "棱镜放行")
        current = self.config.list_of("privacy.protected_user_ids")
        if target_id not in current:
            yield event.plain_result("对方不在保护名单里。")
            return
        current = [item for item in current if item != target_id]
        self.config.set("privacy.protected_user_ids", current)
        self.config.save()
        yield event.plain_result(f"已把 {target_hint or target_id} 从保护名单移除。")

    @filter.command("棱镜缓存")
    async def cmd_cache(self, event: AstrMessageEvent):
        """查看本群语料积累情况。"""
        platform, group_id = self._scope(event)
        stats = await self.astore.corpus_stats(platform, group_id)
        state = await self.astore.get_scan_state(platform, group_id)
        span = ""
        if stats.get("oldest") and stats.get("newest"):
            span = f"{_fmt_ts(stats['oldest'])} ~ {_fmt_ts(stats['newest'])}"
        lines = [
            "本群语料状态：",
            f"  发言条数：{stats.get('total', 0)}",
            f"  参与人数：{stats.get('users', 0)}",
            f"  时间范围：{span or '暂无'}",
        ]
        lines.extend(scanning.describe_scan_state(state))
        with contextlib.suppress(Exception):
            health = await self.astore.corpus_ts_health(platform, group_id)
            bad = int(health.get("future") or 0) + int(health.get("missing") or 0)
            if bad:
                lines.append(f"  时间戳异常：{bad} 条（不参与按天统计，会自动尝试修正）")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("棱镜清缓存")
    async def cmd_clear_cache(self, event: AstrMessageEvent):
        """清空本群语料（画像记录不受影响）。"""
        platform, group_id = self._scope(event)
        if not group_id:
            yield event.plain_result("这条指令只在群里有效。")
            return
        removed = await self.astore.clear_group_corpus(platform, group_id)
        #: 语料没了，断点留着只会让回溯从半空中接着挖，直接清干净。
        await self.astore.reset_scan_state(platform, group_id)
        yield event.plain_result(f"已清空本群 {removed} 条语料，下次分析会重新回溯。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("棱镜重扫")
    async def cmd_rescan(self, event: AstrMessageEvent):
        """清掉本群的历史回溯断点，让下次画像从最新一页重新往前挖。

        语料本身一条都不删（画像记录同样保留），只是把"挖到哪儿了 / 用哪个游标 /
        是不是已经挖到头"这几个标记归零。换协议端、机器人重新入群，或者怀疑回溯
        被卡住时用它，比清空语料温和得多。
        """
        platform, group_id = self._scope(event)
        if not group_id:
            yield event.plain_result("这条指令只在群里有效。")
            return
        await self.astore.reset_scan_state(platform, group_id)
        yield event.plain_result(
            "已重置本群的回溯断点，语料一条没删。下次画像会从最新一页重新往前翻，"
            "翻完可以发「棱镜缓存」看进度。",
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("棱镜诊断")
    async def cmd_probe(self, event: AstrMessageEvent):
        """实测本群协议端认哪一种翻页方式，顺带看一眼语料清洗漏斗。

        get_group_msg_history 传错游标时不报错、只是反复返回同一批最新消息，光看回溯
        日志分不清\"翻到头了\"和\"翻页压根没生效\"。这条指令把四种翻页方式各打一次真实
        请求（每次只拉 20 条、一律不写入语料库），把条数、与首页的重叠、最早时间有没有
        真的前移都摊开来，能用的那种当场记进 scan_state 给之后的回溯复用。
        """
        platform, group_id = self._scope(event)
        if not group_id:
            yield event.plain_result("这条指令只在群里有效。")
            return
        if not scanning.supports_backfill(platform, group_id):
            yield event.plain_result(
                f"当前平台（{platform or '未知'}）没有可用的群历史接口，主动回溯本来就不会执行，"
                "语料只能靠被动采集积累。",
            )
            return
        client = getattr(event, "bot", None)
        if client is None:
            yield event.plain_result("拿不到协议端客户端，无法自检。")
            return

        probe_size = 20

        async def fetch(cursor: int) -> Any:
            return await self._fetch_history_page(client, platform, group_id, cursor, probe_size)

        #: 协议端自报的实现名，直接写进诊断结论里，省得再问一遍"你用的什么端"。
        impl = ""
        with contextlib.suppress(Exception):
            info = await client.api.call_action("get_version_info")
            if isinstance(info, dict):
                name = str(info.get("app_name") or "").strip()
                ver = str(info.get("app_version") or "").strip()
                impl = f"{name} {ver}".strip()

        report = await history.probe_pagination(fetch, brief=scanning.brief_error)
        report.impl = impl
        report.param_style = history.normalize_param_style(self._param_style.get(platform))

        #: 清洗漏斗：同一批首页消息，分别按当前配置和最宽松口径过一遍，
        #: 让\"群里很热闹但只提取到几条\"这类问题能区分是翻页问题还是清洗太严。
        if report.ok:
            try:
                base = await fetch(0)
            except Exception as exc:  #: 漏斗只是附加信息，失败不影响主结论
                logger.debug("[人格棱镜] 清洗漏斗取样失败：%s", exc)
            else:
                rows = collector.parse_history_page(base)
                report.parsed = len(rows)
                report.kept = len(
                    collector.clean_rows(
                        rows,
                        min_chars=self.config.int_of("collect.min_chars"),
                        filter_commands=self.config.bool_of("collect.filter_commands"),
                        drop_urls=self.config.bool_of("collect.strip_urls"),
                        redact=self.config.bool_of("privacy.redact_pii"),
                    ),
                )
                report.kept_loose = len(
                    collector.clean_rows(
                        rows,
                        min_chars=1,
                        filter_commands=False,
                        drop_urls=False,
                        redact=False,
                    ),
                )

        if report.winner:
            #: 之前很可能已经被错误游标顶到一个假断点，连带 exhausted 也可能是误判，
            #: 所以确定可用方式的同时把断点清空，让下次画像从最新一页老老实实重挖。
            await self.astore.reset_scan_state(platform, group_id)
            await self.astore.set_scan_state(platform, group_id, cursor_field=report.winner)

        yield event.plain_result("\n".join(history.render_probe(report, page_size=probe_size)))

    @filter.command("棱镜主题")
    async def cmd_theme(self, event: AstrMessageEvent):
        """查看或切换本群的卡片主题。"""
        platform, group_id = self._scope(event)
        argument = _strip_command(event.get_message_str() or "", "棱镜主题")
        current = cards.normalize_theme_choice(
            await self.astore.group_theme(platform, group_id) or self.config.str_of("render.theme"),
        )
        if not argument:
            lines = [f"当前主题：{cards.theme_label(current)}（{current}）", "", "可选主题："]
            lines += [
                f"  {name} · {meta['label']} —— {meta['desc']}" for name, meta in cards.THEME_CHOICES.items()
            ]
            lines += [
                "",
                "切换方式：棱镜主题 neon",
                "选 auto 就是自动挡：每次按画像的性子挑一套，还会避开本群最近用过的。",
            ]
            yield event.plain_result("\n".join(lines))
            return
        if not event.is_admin():
            yield event.plain_result("只有管理员可以切换本群主题。")
            return
        matched = cards.match_theme_choice(argument)
        if not matched:
            yield event.plain_result("没有这个主题，发送「棱镜主题」查看可选项。")
            return
        await self.astore.set_group_theme(platform, group_id, matched)
        if cards.is_auto_theme(matched):
            yield event.plain_result(
                "本群卡片主题已切换为自动挡：以后每张卡都按画像的性子现挑一套，连着画不容易撞。",
            )
            return
        yield event.plain_result(f"本群卡片主题已切换为 {cards.theme_label(matched)}。")

    @filter.command("棱镜统计")
    async def cmd_stats(self, event: AstrMessageEvent):
        """全局运行数据概览。"""
        data = await self.astore.overview()
        corpus = data.get("corpus") or {}
        lines = [
            f"人格棱镜 {PLUGIN_VERSION} 运行概览：",
            f"  画像总数：{data.get('portraits', 0)}（今日 {data.get('today', 0)}）",
            f"  覆盖群聊：{data.get('groups', 0)} 个，群友 {data.get('users', 0)} 人",
            f"  语料条数：{corpus.get('total', 0)}（{corpus.get('users', 0)} 人参与）",
            f"  近 7 天任务：{data.get('runs_7d', 0)}，"
            f"成功率 {round(float(data.get('success_rate', 1.0)) * 100)}%",
            f"  平均耗时：{round(data.get('avg_elapsed_ms', 0) / 1000, 1)} 秒",
            f"  隐身人数：{data.get('optouts', 0)}",
        ]
        if self._last_backend:
            lines.append(
                f"  上次渲染：{cards.BACKEND_LABELS.get(self._last_backend, self._last_backend)}",
            )
        yield event.plain_result("\n".join(lines))

    # -------------------------------------------------------- 被动采集与分发

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=100)
    async def collect_group_message(self, event: AstrMessageEvent):
        """只负责被动采集，优先级拉满，并且自己吞掉所有异常。

        为什么要和指令分发拆成两个 handler：AstrBot 的事件流水线是所有插件共享的，
        任何一个插件在我们之前 stop_event()，排在后面的 handler 就整段不执行。语料
        采集是这个插件的地基 —— 采不到就什么玩法都出不来，所以让它单独占一个高优先级
        handler，先把话记下来，再让别人去抢事件。
        """
        try:
            platform, group_id = self._scope(event)
            if not group_id or not self.config.group_allowed(group_id):
                return
            raw = getattr(event.message_obj, "raw_message", None)
            if isinstance(raw, dict) and str(raw.get("post_type") or "") == "notice":
                #: 戳一戳 / 表情回应 / 撤回都是 notice，不是消息，走单独的计数入口。
                await self._capture_notice(platform, group_id, raw)
                return
            if self.config.bool_of("collect.passive_capture"):
                await self._capture(event, platform, group_id)
        except Exception as exc:  # 采集永远不能把别人的消息链路带崩
            self._warn_throttled("capture", f"被动采集异常：{exc}")

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """分发 WebUI 里新增的自定义指令，并顺手跑一次后台维护。

        注意这里绝不能无条件 stop_event()：上游对所有群消息都走同一个通用
        handler 且处理完不停止事件，导致既抢不到优先级、又让后续插件重复处理。
        本实现只在真正命中自定义指令时才终止事件。
        """
        _platform, group_id = self._scope(event)
        if not group_id:
            return
        await self._maintenance()
        if not self.config.group_allowed(group_id):
            return

        text = (event.get_message_str() or "").lstrip(_PREFIX_CHARS).strip()
        if not text:
            return
        for command, spec in self.library.command_map().items():
            if text == command or text.startswith(command):
                event.stop_event()
                async for result in self._execute(event, spec):
                    yield result
                return

    @filter.on_llm_request()
    async def inject_portrait(self, event: AstrMessageEvent, req: Any):
        """可选地把发言人的最新画像摘要注入系统提示词，让机器人更懂人。"""
        if not self.config.bool_of("inject.enabled"):
            return
        platform, group_id = self._scope(event)
        record = await self.astore.latest_portrait(
            platform,
            group_id,
            str(event.get_sender_id() or ""),
            "portrait",
        )
        if record is None or not record.text:
            return
        max_age = self.config.int_of("inject.max_age_days")
        if max_age > 0 and record.created_at and time.time() - record.created_at > max_age * 86400:
            return
        limit = max(50, self.config.int_of("inject.max_chars"))
        digest = record.text.strip()
        if len(digest) > limit:
            digest = digest[:limit].rstrip() + "…"
        req.system_prompt = (
            (req.system_prompt or "") + "\n\n[对话者画像参考，仅供理解对方风格，不要主动复述]\n" + digest
        )

    # ------------------------------------------------------------------ WebUI

    def _unlink_cards(self, files: Any) -> int:
        """删除卡片文件，路径越界的一律忽略。"""
        removed = 0
        for name in files or []:
            if not name:
                continue
            candidate = (self.cards_dir / str(name)).resolve()
            try:
                candidate.relative_to(self.cards_dir.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                with contextlib.suppress(OSError):
                    candidate.unlink()
                    removed += 1
        return removed

    @staticmethod
    def _dashboard_error(message: str, status: int = 400):
        return jsonify({"ok": False, "error": message}), status

    def _register_dashboard_apis(self) -> None:
        routes = (
            ("dashboard/overview", self._api_overview, ["GET"], "人格棱镜 · 概览"),
            ("dashboard/records", self._api_records, ["GET"], "人格棱镜 · 记录列表"),
            ("dashboard/groups", self._api_groups, ["GET"], "人格棱镜 · 群与成员树"),
            ("dashboard/record", self._api_record, ["GET"], "人格棱镜 · 记录详情"),
            ("dashboard/record-card", self._api_record_card, ["GET"], "人格棱镜 · 卡片预览"),
            ("dashboard/record-delete", self._api_record_delete, ["POST"], "人格棱镜 · 删除记录"),
            ("dashboard/records-purge", self._api_records_purge, ["POST"], "人格棱镜 · 批量清理"),
            ("dashboard/settings", self._api_settings, ["GET", "POST"], "人格棱镜 · 配置"),
            ("dashboard/prompts", self._api_prompts, ["GET", "POST", "DELETE"], "人格棱镜 · 提示词"),
            ("dashboard/optout", self._api_optout, ["POST"], "人格棱镜 · 隐身名单"),
            ("dashboard/runs", self._api_runs, ["GET"], "人格棱镜 · 运行日志"),
        )
        for suffix, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_ID}/{suffix}",
                    handler,
                    methods,
                    desc,
                )
            except Exception as exc:
                logger.warning("[人格棱镜] 注册 WebUI 接口 %s 失败：%s", suffix, exc)

    async def _api_overview(self):
        return jsonify(
            dashboard.build_overview(
                self.store,
                self.config,
                prompt_count=len(self.library.all_specs()),
                version=PLUGIN_VERSION,
                backend_hint=self._last_backend,
            ),
        )

    async def _api_records(self):
        return jsonify(dashboard.build_records(self.store, request.args))

    async def _api_groups(self):
        return jsonify(dashboard.build_groups(self.store))

    async def _api_record(self):
        try:
            return jsonify(dashboard.build_record_detail(self.store, request.args.get("id")))
        except dashboard.DashboardError as exc:
            return self._dashboard_error(str(exc), 404)

    async def _api_record_card(self):
        """把卡片图片以 data URL 返回，避免额外开一条静态资源路由。"""
        record_id = _as_int(request.args.get("id"))
        if not record_id:
            return self._dashboard_error("缺少记录 id")
        record = self.store.get_record(record_id)
        if record is None:
            return self._dashboard_error("记录不存在", 404)
        if not record.card_file:
            return jsonify({"ok": True, "data_url": "", "reason": "该记录没有生成图片。"})
        target = (self.cards_dir / record.card_file).resolve()
        try:
            target.relative_to(self.cards_dir.resolve())
        except ValueError:
            return self._dashboard_error("非法的卡片路径", 400)
        if not target.is_file():
            return jsonify({"ok": True, "data_url": "", "reason": "卡片文件已被清理。"})
        size = target.stat().st_size
        if size > _CARD_PREVIEW_LIMIT:
            return jsonify({"ok": True, "data_url": "", "reason": "图片过大，已跳过预览。"})
        mime = _MIME_BY_SUFFIX.get(target.suffix.lower(), "image/jpeg")
        payload = base64.b64encode(target.read_bytes()).decode("ascii")
        return jsonify({"ok": True, "data_url": f"data:{mime};base64,{payload}", "bytes": size})

    async def _api_record_delete(self):
        body = await request.get_json(force=True, silent=True) or {}
        record_id = _as_int(body.get("id"))
        if not record_id:
            return self._dashboard_error("缺少记录 id")
        card_file = self.store.delete_record(record_id)
        if card_file:
            self._unlink_cards([card_file])
        return jsonify({"ok": True, "deleted": 1})

    async def _api_records_purge(self):
        body = await request.get_json(force=True, silent=True) or {}
        group_id = str(body.get("group_id") or "").strip()
        user_id = str(body.get("user_id") or "").strip()
        if not group_id and not user_id:
            return self._dashboard_error("为避免误删，必须指定群号或用户号")
        files = self.store.purge_records(group_id=group_id, user_id=user_id)
        self._unlink_cards(files)
        return jsonify({"ok": True, "deleted": len(files)})

    async def _api_settings(self):
        if request.method == "GET":
            return jsonify(dashboard.build_settings(self.store, self.config))
        body = await request.get_json(force=True, silent=True) or {}
        try:
            result = dashboard.apply_settings(self.config, body)
        except (ConfigError, dashboard.DashboardError) as exc:
            return self._dashboard_error(str(exc))
        self.analyzer = PrismAnalyzer(self.context, self.config, logger)
        self.renderer = CardRenderer(self, self.config, self.cards_dir, logger)
        return jsonify({"ok": True, **result})

    async def _api_prompts(self):
        if request.method == "GET":
            return jsonify(dashboard.build_prompts(self.store, self.library))
        body = await request.get_json(force=True, silent=True) or {}
        reserved = set(BUILTIN_COMMANDS) | set(OWN_COMMANDS)
        # 页面桥接只提供 apiGet / apiPost，所以删除同时支持 DELETE 与 POST+action。
        if request.method == "DELETE" or str(body.get("action") or "") == "delete":
            key = str(body.get("key") or "").strip()
            if not key:
                return self._dashboard_error("缺少提示词 key")
            removed = self.store.delete_prompt_entry(key)
            self._reload_prompts()
            return jsonify({"ok": True, "deleted": 1 if removed else 0})
        try:
            entry = dashboard.validate_prompt_payload(body, reserved=reserved)
        except dashboard.DashboardError as exc:
            return self._dashboard_error(str(exc))
        self.store.upsert_prompt_entry(
            entry["key"],
            command=entry["command"],
            label=entry["label"],
            prompt=entry["prompt"],
            structured=entry["structured"],
            layout=entry["layout"],
            enabled=entry["enabled"],
        )
        self._reload_prompts()
        return jsonify({"ok": True, "entry": entry})

    async def _api_optout(self):
        body = await request.get_json(force=True, silent=True) or {}
        action = str(body.get("action") or "").strip()
        platform = str(body.get("platform") or "aiocqhttp").strip()
        group_id = str(body.get("group_id") or "").strip()
        user_id = str(body.get("user_id") or "").strip()
        if not user_id:
            return self._dashboard_error("缺少用户号")
        if action == "remove":
            removed = self.store.remove_optout(platform, group_id, user_id)
            return jsonify({"ok": True, "removed": bool(removed)})
        if action == "add":
            self.store.add_optout(
                platform,
                group_id,
                user_id,
                user_name=str(body.get("user_name") or ""),
                reason="dashboard",
            )
            return jsonify({"ok": True, "added": True})
        return self._dashboard_error("action 必须是 add 或 remove")

    async def _api_runs(self):
        limit = _as_int(request.args.get("limit")) or 20
        return jsonify({"ok": True, "runs": self.store.recent_runs(limit=max(1, min(100, limit)))})

    # ---------------------------------------------------------------- 生命周期

    async def terminate(self) -> None:
        with contextlib.suppress(Exception):
            self.store.close()
        logger.info("[人格棱镜] 已卸载。")
