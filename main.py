"""人格棱镜 · Persona Prism —— 群友人格画像插件主入口。

链路：被动采集 / 主动回溯 → 清洗抽样 → 统计锚点 → LLM 结构化分析
      → 高级卡片渲染（t2i / Playwright / 文转图 / 纯文本四层兜底）
      → 落库 → WebUI 可查可管。

上游灵感来自 Zhalslar/astrbot_plugin_portrayal，详见 NOTICE.md。
"""

from __future__ import annotations

import base64
import contextlib
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

from .prism import cards, collector, dashboard
from .prism.analyzer import AnalyzeError, PrismAnalyzer
from .prism.cards import CardContext, CardRenderer, RenderResult
from .prism.config import ConfigError, PrismConfig
from .prism.models import CorpusBundle, CorpusMessage, MemberProfile, PortraitRecord
from .prism.prompts import PromptLibrary, PromptSpec, normalize_layout
from .prism.store import AsyncStore, PrismStore

PLUGIN_ID = "astrbot_plugin_persona_prism"
PLUGIN_VERSION = "v1.1.1"

#: 内置提示词对应的指令，用于「保留指令」校验与帮助表。
#: 前 5 条是本插件的结构化卡片玩法，后 5 条兼容上游 astrbot_plugin_portrayal 的长文玩法。
BUILTIN_COMMANDS = (
    "棱镜画像",
    "棱镜赞赏",
    "棱镜锐评",
    "棱镜克隆",
    "棱镜姻缘",
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


def _history_cursor(raw: Any) -> int | None:
    """从一条群历史消息里取出翻页游标。

    get_group_msg_history 的 message_seq 参数期望的是**序号**，不是消息 ID。
    go-cqhttp / NapCat / Lagrange / LLBot（幸运莉莉娅）都会在返回里附带
    message_seq，而 LLBot 上 message_id 与 seq 完全是两套编号，拿 message_id
    当游标会导致回溯永远停在第一页。所以这里优先 seq，缺失才回落 message_id。
    """
    if not isinstance(raw, dict):
        return None
    for key in ("message_seq", "real_seq", "message_id"):
        parsed = _as_int(raw.get(key))
        if parsed is not None:
            return parsed
    return None


def _strip_command(text: str, command: str) -> str:
    """去掉指令前缀（含唤醒前缀符）后剩下的参数部分。"""
    body = (text or "").lstrip(_PREFIX_CHARS)
    if command and body.startswith(command):
        body = body[len(command) :]
    return body.strip()


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

        self._register_dashboard_apis()
        logger.info("[人格棱镜] %s 已加载，数据目录：%s", PLUGIN_VERSION, self.data_dir)

    # ------------------------------------------------------------------ 基础

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
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not user_id or not message_id:
            return
        segments = event.get_messages() or []
        text, is_reply = collector.parse_onebot_segments(segments)
        if not text or self._is_own_command(text):
            return
        raw_ts = _as_int(getattr(event.message_obj, "timestamp", 0)) or int(time.time())
        message = CorpusMessage(
            message_id=message_id,
            user_id=user_id,
            user_name=event.get_sender_name() or "",
            text=text,
            ts=raw_ts,
            is_reply=is_reply,
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
                },
            ],
            min_chars=self.config.int_of("collect.min_chars"),
            filter_commands=self.config.bool_of("collect.filter_commands"),
            drop_urls=self.config.bool_of("collect.strip_urls"),
            redact=self.config.bool_of("privacy.redact_pii"),
        )
        if not cleaned:
            return
        with contextlib.suppress(Exception):
            await self.astore.add_messages(platform, group_id, cleaned)

    async def _backfill(
        self,
        event: AstrMessageEvent,
        platform: str,
        group_id: str,
        user_id: str,
        *,
        target_total: int,
    ) -> int:
        """向更早的历史翻页，直到攒够目标条数或翻到头。

        与上游的差异：
        * 游标优先用 message_seq，缺失才回落 message_id，并用 _as_int 解析，
          兼容负数 message_id（NapCat / Lagrange）与独立 seq 编号（LLBot）。
        * 断点记在 scan_state 表里，重启后接着挖而不是从头再来；已经挖到群历史
          尽头的群只补拉最新一页（补齐机器人离线期间漏掉的消息），不再无意义地
          继续往前翻。
        * 每页写库前先过滤本插件自己的指令消息。
        """
        client = getattr(event, "bot", None)
        if client is None or not group_id:
            return 0

        rounds = max(0, self.config.int_of("collect.backfill_rounds"))
        page_size = max(20, self.config.int_of("collect.page_size"))
        state = await self.astore.get_scan_state(platform, group_id)
        #: 历史已经挖到最早一条。此时往前翻只会拿到空页，但机器人离线期间的新消息
        #: 被动采集是拿不到的，所以退化成"只补拉最新一页"，且不动断点。
        topup_only = bool(state.get("exhausted"))
        cursor = None if topup_only else (
            _as_int(state.get("oldest_seq")) if state.get("oldest_seq") else None
        )
        newest_seen = str(state.get("newest_seq") or "")
        added = 0
        if topup_only:
            rounds = min(rounds, 1)

        for index in range(rounds):
            seq = 0 if (index == 0 and cursor is None) else (cursor or 0)
            try:
                payload = await client.api.call_action(
                    "get_group_msg_history",
                    group_id=int(group_id),
                    message_seq=seq,
                    count=page_size,
                    reverseOrder=True,
                )
            except Exception as exc:
                logger.debug("[人格棱镜] 拉取群历史失败（第 %s 页）：%s", index + 1, exc)
                break

            messages = (payload or {}).get("messages") if isinstance(payload, dict) else payload
            if not messages:
                if not topup_only:
                    await self.astore.set_scan_state(platform, group_id, exhausted=True)
                break

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
                added += await self.astore.add_messages(platform, group_id, cleaned)

            if topup_only:
                break

            oldest = _history_cursor(messages[0])
            if not newest_seen:
                newest_seen = str(_history_cursor(messages[-1]) or "")
            if oldest is None or oldest == cursor:
                await self.astore.set_scan_state(platform, group_id, exhausted=True)
                break
            cursor = oldest
            await self.astore.set_scan_state(
                platform,
                group_id,
                oldest_seq=str(cursor),
                newest_seq=newest_seen,
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
        return added

    async def _gather(
        self,
        event: AstrMessageEvent,
        platform: str,
        group_id: str,
        user_id: str,
    ) -> CorpusBundle:
        """取语料并打包。命中本地缓存就不出网。"""
        max_messages = max(20, self.config.int_of("collect.max_messages"))
        depth = max(max_messages * 4, 1000)
        rows = await self.astore.fetch_user_corpus(platform, group_id, user_id, limit=depth)
        from_cache = True

        if group_id and platform == "aiocqhttp" and len(rows) < max_messages:
            fetched = await self._backfill(
                event,
                platform,
                group_id,
                user_id,
                target_total=max_messages,
            )
            if fetched:
                from_cache = False
                rows = await self.astore.fetch_user_corpus(
                    platform,
                    group_id,
                    user_id,
                    limit=depth,
                )

        return collector.build_bundle(
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

    # ---------------------------------------------------------------- 核心链路

    def _cooldown_left(self, bucket: dict[str, float], key: str, window: int) -> int:
        if window <= 0:
            return 0
        left = bucket.get(key, 0.0) + window - time.time()
        return int(left) + 1 if left > 0 else 0

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
            if not self.config.bool_of("behavior.quiet_progress"):
                yield event.plain_result(f"正在翻聊天记录，为 {target_hint or target_id} 生成{spec.label}…")

            bundle = await self._gather(event, platform, group_id, target_id)
            min_messages = self.config.int_of("collect.min_messages")
            if not bundle.enough or bundle.stats.sampled < min_messages:
                error = "样本不足"
                yield event.plain_result(
                    f"有效发言只有 {bundle.stats.sampled} 条（至少需要 {min_messages} 条），"
                    "多聊几句再来试试。",
                )
                return

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

            theme = cards.normalize_theme(
                await self.astore.group_theme(platform, group_id) or self.config.str_of("render.theme"),
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
        """列出全部指令与当前渲染状态。"""
        legacy_on = bool(self.config.get("compat.legacy_commands", True))
        prism_specs: list[PromptSpec] = []
        legacy_specs: list[PromptSpec] = []
        custom_specs: list[PromptSpec] = []
        for spec in self.library.all_specs():
            if not spec.builtin:
                custom_specs.append(spec)
            elif spec.key in LEGACY_KEYS.values():
                legacy_specs.append(spec)
            else:
                prism_specs.append(spec)
        lines = [
            f"人格棱镜 {PLUGIN_VERSION} · 指令一览",
            "",
            "目标写法通用：@对方 / 回复对方的消息 / 直接跟 QQ 号，都省略就是画自己。",
            "",
            "【棱镜系列】结构化信息卡（评分、雷达图、原话引用）",
        ]
        for spec in prism_specs:
            lines.append(f"  {spec.command} —— {spec.label}")
        if legacy_on and legacy_specs:
            lines += ["", "【画像系列】上游同款长文报告，改用本插件的卡片渲染"]
            for spec in legacy_specs:
                lines.append(f"  {spec.command} —— {spec.label}")
            lines += [
                "  查看画像 —— 用纯文本重发最近一次画像结果",
                "  切换人格 / 恢复人格 —— 让机器人扮演/停止扮演克隆出的人格（管理员）",
            ]
        if custom_specs:
            lines += ["", "【自定义模板】在 WebUI 里增删改"]
            for spec in custom_specs:
                lines.append(f"  {spec.command} —— {spec.label}")
        lines += [
            "",
            "【查询】",
            "  棱镜档案 —— 重新发送最近一次画像卡片",
            "  棱镜历史 —— 列出历史画像摘要",
            "  棱镜缓存 —— 查看本群语料积累情况",
            "  棱镜统计 —— 查看全局运行数据",
            "  棱镜主题 —— 查看 / 切换本群卡片主题",
            "",
            "【隐私】",
            "  棱镜隐身 —— 把自己从画像范围内排除",
            "  棱镜现身 —— 撤销隐身",
            "",
            "【管理员】",
            "  棱镜删除 —— 删除某人在本群的画像记录",
            "  棱镜清缓存 —— 清空本群语料",
            "  棱镜拉黑 / 棱镜放行 —— 维护保护名单",
            "",
            f"当前渲染链路：{self.config.str_of('render.backend')}"
            f"（可用后端 {' → '.join(self.renderer.backends())}）",
            "棱镜系列出结构化卡片，画像系列出上游同款长文卡，两者互不影响。",
            "更详细的配置与记录管理请打开 WebUI 的「人格棱镜」页面。",
        ]
        yield event.plain_result("\n".join(lines))

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
            f"  历史回溯：{'已挖到头' if state.get('exhausted') else '仍可继续回溯'}",
        ]
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
        await self.astore.set_scan_state(platform, group_id, exhausted=False)
        yield event.plain_result(f"已清空本群 {removed} 条语料，下次分析会重新回溯。")

    @filter.command("棱镜主题")
    async def cmd_theme(self, event: AstrMessageEvent):
        """查看或切换本群的卡片主题。"""
        platform, group_id = self._scope(event)
        argument = _strip_command(event.get_message_str() or "", "棱镜主题")
        current = await self.astore.group_theme(platform, group_id) or self.config.str_of(
            "render.theme",
        )
        if not argument:
            lines = [f"当前主题：{cards.theme_label(current)}（{current}）", "", "可选主题："]
            lines += [f"  {name} · {meta['label']} —— {meta['desc']}" for name, meta in cards.THEMES.items()]
            lines.append("")
            lines.append("切换方式：棱镜主题 neon")
            yield event.plain_result("\n".join(lines))
            return
        if not event.is_admin():
            yield event.plain_result("只有管理员可以切换本群主题。")
            return
        wanted = argument.strip().lower()
        matched = ""
        for name, meta in cards.THEMES.items():
            if wanted in {name, meta["label"]}:
                matched = name
                break
        if not matched:
            yield event.plain_result("没有这个主题，发送「棱镜主题」查看可选项。")
            return
        await self.astore.set_group_theme(platform, group_id, matched)
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

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """被动积累语料，并分发 WebUI 里新增的自定义指令。

        注意这里绝不能无条件 stop_event()：上游对所有群消息都走同一个通用
        handler 且处理完不停止事件，导致既抢不到优先级、又让后续插件重复处理。
        本实现只在真正命中自定义指令时才终止事件。
        """
        platform, group_id = self._scope(event)
        if not group_id:
            return
        allowed = self.config.group_allowed(group_id)
        if allowed and self.config.bool_of("collect.passive_capture"):
            await self._capture(event, platform, group_id)
        await self._maintenance()
        if not allowed:
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
