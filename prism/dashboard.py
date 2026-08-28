"""WebUI 数据装配层。

这一层刻意做成"纯函数 + 只依赖 store/config"的形式，不碰 quart、不碰
AstrMessageEvent，好处是可以直接在单测里跑，也让 main.py 里的 HTTP handler
薄到一眼能看完。

约定：所有函数返回可直接 jsonify 的 dict/list，出错则抛 DashboardError，
由 main.py 统一转成 {"ok": false, "message": ...}。
"""

from __future__ import annotations

import re
import time
from typing import Any

from .cards import BACKEND_LABELS, THEME_CHOICES, THEMES, theme_label
from .config import DASHBOARD_WRITABLE, DEFAULTS, ConfigError
from .models import PROFILE_FIELD_LABELS
from .prompts import VALID_LAYOUTS, normalize_layout

#: 配置项的人类可读说明。WebUI 直接渲染这份表，省得前端硬编码文案。
FIELD_HINTS: dict[str, dict[str, str]] = {
    "llm.provider_id": {
        "label": "指定服务提供商",
        "hint": "留空则使用 AstrBot 当前会话正在用的提供商。",
    },
    "llm.model": {"label": "指定模型", "hint": "留空则用提供商的默认模型。"},
    "llm.retry_times": {"label": "失败重试次数", "hint": "解析失败或超时后的额外尝试次数。"},
    "collect.passive_capture": {
        "label": "被动采集群聊",
        "hint": "开启后插件会把群内可见消息存进本地库，非 QQ 平台也能画像。",
    },
    "collect.backfill_rounds": {
        "label": "历史回溯页数",
        "hint": "仅 QQ（aiocqhttp）有效。语料不足时向更早的消息翻多少页。",
    },
    "collect.max_messages": {"label": "单次分析上限", "hint": "喂给模型的最大消息条数。"},
    "collect.min_messages": {"label": "最少消息条数", "hint": "低于这个数量就提示样本不足。"},
    "collect.retention_days": {"label": "语料保留天数", "hint": "0 表示永久保留。"},
    "collect.sampling": {
        "label": "抽样策略",
        "hint": "仅在语料超过『单次分析上限』时才生效。layered=近期加权 + 全期覆盖；recent=只取最近的消息。",
    },
    "collect.cursor_field": {
        "label": "历史翻页方式",
        "hint": "各协议端对 get_group_msg_history 的翻页参数理解不一致：它认的编号可能是 message_seq 也可能是 message_id，返回的那一页可能最旧在前也可能最新在前。传错不会报错，只会反复返回同一批消息。auto 会把四种组合逐个试并记住本群可用的那种；群里发「棱镜诊断」可以当场实测是哪一种。",
    },
    "collect.filter_commands": {"label": "过滤指令消息", "hint": "丢掉以指令前缀开头的消息。"},
    "collect.strip_urls": {"label": "去除链接", "hint": "把 URL 从语料里摘掉，省 token。"},
    "collect.fold_repeats": {"label": "折叠重复刷屏", "hint": "同一句话重复多次时折叠成一条并标注次数。"},
    "render.backend": {"label": "渲染链路", "hint": "决定卡片图片由谁来出。"},
    "render.theme": {
        "label": "默认卡片主题",
        "hint": "群内可用「棱镜主题」单独覆盖。选「自动挡」就按每张画像的性子临场挑一套。",
    },
    "render.card_scale": {
        "label": "卡片清晰度",
        "hint": "以百分比放大卡片像素。200 = 两倍分辨率，越高越清晰也越大越慢。",
    },
    "render.image_format": {
        "label": "图片格式",
        "hint": "jpeg 体积小；png 无损、文字边缘最锐利但文件更大。",
    },
    "render.image_quality": {"label": "图片质量", "hint": "仅 jpeg 生效，越高越清晰也越大。"},
    "render.show_evidence": {"label": "展示原话证据", "hint": "关闭后卡片不再引用群友原话。"},
    "render.show_avatar": {"label": "展示头像", "hint": "关闭后只显示昵称首字。"},
    "render.footer_note": {"label": "卡片署名", "hint": "显示在卡片右下角。"},
    "render.font_family": {
        "label": "正文字体",
        "hint": "留空沿用主题自带字体栈。可写多个字体名，用逗号分隔。",
    },
    "render.font_title_family": {
        "label": "标题字体",
        "hint": "留空则跟随正文字体。",
    },
    "render.font_source": {
        "label": "自定义字体文件",
        "hint": "本地字体路径或 http(s) 地址，支持 ttf/otf/woff/woff2，上限 8MB；本地文件会自动内嵌。",
    },
    "limits.user_cooldown_sec": {"label": "同一发起人冷却", "hint": "单位秒，0 表示不限制。"},
    "limits.target_cooldown_sec": {"label": "同一目标冷却", "hint": "避免同一个人被反复刷画像。"},
    "limits.group_daily_quota": {"label": "单群每日上限", "hint": "0 表示不限制。"},
    "limits.max_concurrency": {"label": "并发上限", "hint": "同时进行的分析任务数。"},
    "privacy.include_profile_fields": {
        "label": "允许使用的资料字段",
        "hint": "只有勾选的字段才会进入提示词，手机号/邮箱等永远不会。",
    },
    "privacy.protected_user_ids": {"label": "保护名单", "hint": "名单内的用户不允许被画像。"},
    "privacy.allow_opt_out": {"label": "允许成员自助退出", "hint": "群成员可用「棱镜隐身」把自己排除。"},
    "privacy.redact_pii": {"label": "脱敏后再入库", "hint": "手机号、邮箱、身份证等在入库前打码。"},
    "inject.enabled": {
        "label": "把画像注入对话",
        "hint": "开启后与机器人聊天时会附带对方的最新画像摘要。",
    },
    "inject.max_chars": {"label": "注入字数上限", "hint": "超出会截断。"},
    "inject.max_age_days": {"label": "注入有效期", "hint": "超过这个天数的画像不再注入。"},
    "persona_clone.enabled": {
        "label": "启用人格克隆",
        "hint": "关掉后「棱镜克隆」「克隆人格」「切换人格」全部停用。",
    },
    "persona_clone.require_admin": {
        "label": "克隆仅限管理员",
        "hint": "关掉即恢复上游行为：全群成员都能触发人格克隆。",
    },
    "persona_clone.clear_history_on_switch": {
        "label": "切换人格时清空上下文",
        "hint": "不清空的话新人格会带着旧人格的记忆说话，容易串味。",
    },
    "compat.legacy_commands": {
        "label": "启用「画像」系列指令",
        "hint": "画像 / 正画像 / 负画像 / 克隆人格 / 找对象 / 查看画像 / 切换人格 / 恢复人格。与上游插件同名，同时装两个请关掉一边。",
    },
    "behavior.quiet_progress": {
        "label": "静默模式",
        "hint": "不发进度提示；样本不足时的采集诊断仍会保留，方便排查回溯有没有成功。",
    },
    "behavior.help_card": {
        "label": "帮助出卡片",
        "hint": "「棱镜帮助」渲染成指令速查卡；渲染失败会自动回落成纯文本。",
    },
    "behavior.history_limit": {"label": "每人保留历史条数", "hint": "超出的旧记录自动清理。"},
    "behavior.allow_self_only": {"label": "只允许画自己", "hint": "开启后不能对他人发起画像。"},
    "behavior.enabled_groups": {"label": "生效群列表", "hint": "留空表示所有群都生效。"},
}

GROUP_TITLES: dict[str, dict[str, str]] = {
    "llm": {"label": "模型", "icon": "spark"},
    "collect": {"label": "语料采集", "icon": "layers"},
    "render": {"label": "卡片渲染", "icon": "image"},
    "limits": {"label": "频率与配额", "icon": "gauge"},
    "privacy": {"label": "隐私", "icon": "shield"},
    "inject": {"label": "对话注入", "icon": "chat"},
    "persona_clone": {"label": "人格克隆", "icon": "mask"},
    "behavior": {"label": "行为", "icon": "sliders"},
    "compat": {"label": "上游兼容", "icon": "link"},
}

#: 提示词输出布局选项。前端下拉框直接渲染这份表。
LAYOUT_CHOICES: list[dict[str, str]] = [
    {
        "value": "card",
        "label": "结构化卡片",
        "hint": "要求模型返回 JSON，渲染成带雷达图、标签、证据的信息卡。最好看，也最依赖模型的指令遵循能力。",
    },
    {
        "value": "markdown",
        "label": "长文卡片",
        "hint": "让模型自由用 Markdown 写，再排版成长图卡。适合叙述型画像，对小模型更宽容。",
    },
    {
        "value": "text",
        "label": "纯文本",
        "hint": "原样发文字，不出图。适合人格提示词这类需要复制粘贴的输出。",
    },
]

#: 渲染链路选项的人话解释。
BACKEND_CHOICES: list[dict[str, str]] = [
    {
        "value": "auto",
        "label": "自动（推荐）",
        "hint": "先用 AstrBot 官方 t2i，失败再退本地 Playwright、本地文转图、纯文本。",
    },
    {
        "value": "local_first",
        "label": "本地优先",
        "hint": "先用本地 Playwright 截图，卡片内容不出网；失败再退官方 t2i。",
    },
    {
        "value": "t2i_only",
        "label": "只用官方 t2i",
        "hint": "不尝试本地渲染，失败直接发纯文本。",
    },
    {"value": "text_only", "label": "只发文字", "hint": "完全不渲染图片。"},
]

SAMPLING_CHOICES: list[dict[str, str]] = [
    {"value": "layered", "label": "分层抽样", "hint": "一半预算留给最近的消息，其余等距覆盖整个时间跨度。"},
    {"value": "recent", "label": "只看最近", "hint": "简单截取最近的 N 条。"},
]

CURSOR_FIELD_CHOICES: list[dict[str, str]] = [
    {
        "value": "auto",
        "label": "自动试探",
        "hint": "翻不动就依次换其它方式，并记住本群实测可用的那种（推荐）。",
    },
    {
        "value": "seq_first",
        "label": "message_seq · 取本页第一条",
        "hint": "协议端认独立序号、且返回的一页是最旧在前时用。",
    },
    {
        "value": "id_first",
        "label": "message_id · 取本页第一条",
        "hint": "协议端把翻页参数当消息 ID、且返回的一页是最旧在前时用。",
    },
    {
        "value": "seq_last",
        "label": "message_seq · 取本页最后一条",
        "hint": "协议端认独立序号、但返回的一页是最新在前时用。",
    },
    {
        "value": "id_last",
        "label": "message_id · 取本页最后一条",
        "hint": "协议端把翻页参数当消息 ID、但返回的一页是最新在前时用。",
    },
]


#: 自定义提示词的标识只允许 ASCII 安全字符：它会进 SQLite 主键、URL 查询串和
#: 前端 DOM 的 data 属性，用 str.isalnum() 判断会把中日韩文字也放进来。
_PROMPT_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")


class DashboardError(Exception):
    """WebUI 请求参数有问题。"""


def _int_arg(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _str_arg(raw: Any, limit: int = 120) -> str:
    return str(raw or "").strip()[:limit]


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------


def build_overview(
    store: Any,
    config: Any,
    *,
    prompt_count: int = 0,
    version: str = "",
    backend_hint: str = "",
) -> dict[str, Any]:
    data = store.overview()
    return {
        "ok": True,
        "version": version,
        "server_time": int(time.time()),
        "stats": data,
        "render": {
            "backend": config.str_of("render.backend"),
            "backend_label": next(
                (c["label"] for c in BACKEND_CHOICES if c["value"] == config.str_of("render.backend")),
                "自动",
            ),
            "theme": config.str_of("render.theme"),
            "theme_label": theme_label(config.str_of("render.theme")),
            "last_backend": backend_hint,
            "backend_labels": BACKEND_LABELS,
        },
        "prompts": {"total": prompt_count},
        "flags": {
            "passive_capture": config.bool_of("collect.passive_capture"),
            "inject_enabled": config.bool_of("inject.enabled"),
            "allow_opt_out": config.bool_of("privacy.allow_opt_out"),
            "redact_pii": config.bool_of("privacy.redact_pii"),
            "clone_enabled": config.bool_of("persona_clone.enabled"),
            "sync_bot_nickname": config.bool_of("persona_clone.sync_bot_nickname"),
            "sync_bot_avatar": config.bool_of("persona_clone.sync_bot_avatar"),
        },
        "runs": store.recent_runs(12),
    }


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


def build_records(store: Any, args: Any) -> dict[str, Any]:
    """args 为 dict-like（quart 的 request.args 直接可用）。"""
    getter = args.get if hasattr(args, "get") else (lambda *_: None)
    page = _int_arg(getter("page"), 1, 1, 100000)
    size = _int_arg(getter("size"), 20, 5, 100)
    records, total = store.list_records(
        group_id=_str_arg(getter("group_id")),
        user_id=_str_arg(getter("user_id")),
        kind=_str_arg(getter("kind"), 40),
        query=_str_arg(getter("q"), 60),
        offset=(page - 1) * size,
        limit=size,
    )
    return {
        "ok": True,
        "page": page,
        "size": size,
        "total": total,
        "pages": max(1, (total + size - 1) // size),
        "items": [record.to_summary() for record in records],
    }


def build_groups(store: Any) -> dict[str, Any]:
    tree = store.group_tree()
    return {
        "ok": True,
        "total_groups": len(tree),
        "total_members": sum(len(node["members"]) for node in tree),
        "groups": tree,
    }


def build_record_detail(store: Any, raw_id: Any) -> dict[str, Any]:
    record_id = _int_arg(raw_id, 0, 0, 10**12)
    if not record_id:
        raise DashboardError("缺少记录 id。")
    record = store.get_record(record_id)
    if record is None:
        raise DashboardError("记录不存在，可能已被删除。")
    detail = record.to_detail()
    detail["theme_label"] = THEMES.get(record.theme, {}).get("label", record.theme)
    return {"ok": True, "record": detail}


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------


def _field_meta(path: str) -> dict[str, Any]:
    group, _, key = path.partition(".")
    default = DEFAULTS[group][key]
    hint = FIELD_HINTS.get(path, {})
    if isinstance(default, bool):
        kind = "bool"
    elif isinstance(default, int):
        kind = "int"
    elif isinstance(default, list):
        kind = "list"
    else:
        kind = "text"
    meta: dict[str, Any] = {
        "path": path,
        "group": group,
        "key": key,
        "type": kind,
        "label": hint.get("label", key),
        "hint": hint.get("hint", ""),
        "default": list(default) if isinstance(default, list) else default,
    }
    if path == "render.backend":
        meta["type"] = "choice"
        meta["choices"] = BACKEND_CHOICES
    elif path == "render.image_format":
        meta["type"] = "choice"
        meta["choices"] = [
            {"value": "jpeg", "label": "JPEG", "hint": "体积小，适合日常发群"},
            {"value": "png", "label": "PNG", "hint": "无损，文字最锐利"},
        ]
    elif path == "collect.sampling":
        meta["type"] = "choice"
        meta["choices"] = SAMPLING_CHOICES
    elif path == "collect.cursor_field":
        meta["type"] = "choice"
        meta["choices"] = CURSOR_FIELD_CHOICES
    elif path == "render.theme":
        meta["type"] = "choice"
        meta["choices"] = [
            {"value": name, "label": info["label"], "hint": info["desc"]}
            for name, info in THEME_CHOICES.items()
        ]
    elif path == "privacy.include_profile_fields":
        meta["type"] = "multi"
        meta["choices"] = [
            {"value": field, "label": label}
            for field, label in PROFILE_FIELD_LABELS.items()
            if field not in {"phone", "email", "address", "qid", "reg_time"}
        ]
    return meta


def build_settings(store: Any, config: Any) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for path in sorted(DASHBOARD_WRITABLE):
        meta = _field_meta(path)
        bucket = groups.setdefault(
            meta["group"],
            {
                "group": meta["group"],
                "label": GROUP_TITLES.get(meta["group"], {}).get("label", meta["group"]),
                "icon": GROUP_TITLES.get(meta["group"], {}).get("icon", "sliders"),
                "fields": [],
            },
        )
        bucket["fields"].append(meta)
    order = list(GROUP_TITLES)
    sections = sorted(
        groups.values(),
        key=lambda g: order.index(g["group"]) if g["group"] in order else 99,
    )
    return {
        "ok": True,
        "values": config.snapshot(),
        "sections": sections,
        "readonly": {
            "persona_clone.sync_bot_nickname": config.bool_of("persona_clone.sync_bot_nickname"),
            "persona_clone.sync_bot_avatar": config.bool_of("persona_clone.sync_bot_avatar"),
        },
        "optouts": store.list_optouts(),
    }


def apply_settings(config: Any, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DashboardError("请求体必须是对象。")
    patch = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    try:
        applied = config.apply_patch(patch)
    except ConfigError as exc:
        raise DashboardError(str(exc)) from exc
    return {"ok": True, "applied": applied, "values": config.snapshot()}


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def build_prompts(store: Any, library: Any) -> dict[str, Any]:
    custom = store.list_prompt_entries()
    builtin = [spec.to_dict() for spec in library.all_specs() if spec.builtin]
    # 老库里的自定义条目没有 layout，这里按 structured 推导补齐，前端就不用再猜。
    for row in custom:
        row["layout"] = normalize_layout(row.get("layout"), bool(row.get("structured", True)))
    return {
        "ok": True,
        "builtin": builtin,
        "custom": custom,
        "layouts": LAYOUT_CHOICES,
        "reserved_commands": sorted({spec.command for spec in library.all_specs() if spec.builtin}),
    }


def validate_prompt_payload(payload: Any, *, reserved: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DashboardError("请求体必须是对象。")
    key = _str_arg(payload.get("key"), 40)
    command = _str_arg(payload.get("command"), 40)
    label = _str_arg(payload.get("label"), 40) or command or key
    prompt = str(payload.get("prompt") or "").strip()
    if not _PROMPT_KEY_RE.match(key):
        raise DashboardError("标识只能用字母、数字、下划线和连字符，且不能为空。")
    if not command:
        raise DashboardError("请填写触发指令。")
    if " " in command:
        raise DashboardError("触发指令里不能有空格。")
    if command in reserved:
        raise DashboardError(f"指令「{command}」已被内置模板占用，请换一个。")
    if len(prompt) < 10:
        raise DashboardError("提示词内容太短，至少写 10 个字。")
    if len(prompt) > 8000:
        raise DashboardError("提示词内容过长（上限 8000 字）。")
    # layout 是新字段，给出合法值时以它为准（structured 只是它的派生标记）；
    # 老客户端只发 structured，这时再按 structured 反推 layout。
    raw_layout = str(payload.get("layout") or "").strip().lower()
    if raw_layout in VALID_LAYOUTS:
        layout = raw_layout
        structured = layout == "card"
    else:
        structured = bool(payload.get("structured", True))
        layout = normalize_layout(raw_layout, structured)
    return {
        "key": key,
        "command": command,
        "label": label,
        "prompt": prompt,
        "structured": structured,
        "layout": layout,
        "enabled": bool(payload.get("enabled", True)),
    }


__all__ = [
    "BACKEND_CHOICES",
    "CURSOR_FIELD_CHOICES",
    "FIELD_HINTS",
    "GROUP_TITLES",
    "LAYOUT_CHOICES",
    "SAMPLING_CHOICES",
    "DashboardError",
    "apply_settings",
    "build_groups",
    "build_overview",
    "build_prompts",
    "build_record_detail",
    "build_records",
    "build_settings",
    "validate_prompt_payload",
]
