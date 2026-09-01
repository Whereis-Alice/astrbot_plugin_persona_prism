"""配置读取层。

上游用反射式 ConfigNode 把 schema 再解析一遍，既慢又难排错。这里改成
一个薄包装：所有默认值显式写死在 DEFAULTS，取值时按 "组.键" 路径查找，
缺失就落回默认值。这样即使用户的配置文件是旧版本也不会 KeyError。
"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, dict[str, Any]] = {
    "llm": {
        "provider_id": "",
        "model": "",
        "retry_times": 2,
        "timeout_sec": 180,
    },
    "collect": {
        "passive_capture": True,
        "backfill_rounds": 12,
        "page_size": 200,
        "max_messages": 400,
        "min_messages": 20,
        "min_chars": 2,
        "filter_commands": True,
        "strip_urls": True,
        "fold_repeats": True,
        "retention_days": 30,
        "max_per_group": 20000,
        "sampling": "layered",
        "cursor_field": "auto",
    },
    "render": {
        "backend": "auto",
        "theme": "aurora",
        "card_scale": 200,
        "image_format": "jpeg",
        "image_quality": 92,
        "show_evidence": True,
        "show_avatar": True,
        "footer_note": "人格棱镜 · Persona Prism",
        "font_family": "",
        "font_title_family": "",
        "font_source": "",
    },
    "limits": {
        "user_cooldown_sec": 60,
        "target_cooldown_sec": 30,
        "group_daily_quota": 40,
        "max_concurrency": 2,
    },
    "privacy": {
        "include_profile_fields": [
            "nickname",
            "card",
            "sex",
            "long_nick",
            "join_time",
            "level",
        ],
        "protected_user_ids": [],
        "allow_opt_out": True,
        "redact_pii": True,
    },
    "inject": {
        "enabled": False,
        "max_chars": 300,
        "max_age_days": 14,
    },
    "persona_clone": {
        "enabled": True,
        "sync_bot_nickname": False,
        "sync_bot_avatar": False,
        "require_admin": True,
        "clear_history_on_switch": True,
    },
    "love": {
        "enabled": True,
        "min_messages": 5,
        "day_start_hour": 4,
        "sensitivity": 50,
        "llm_commentary": True,
        "show_trend": True,
        "leaderboard_size": 10,
        "notice_collect": True,
        "theme": "sakura",
    },
    "dialogue": {
        # 把多人对话上下文一起喂给模型：群聊本来就是对话，只看本人发言容易把
        # "接话" 误判成 "自言自语"。默认开启。
        "context_enabled": True,
        "context_span": 3,
        "max_scenes": 12,
        "max_lines": 80,
        "social_signals": True,
        # 让模型自己从带编号的群聊里挑出「哪几句构成一场对话」。群聊不是一问一答，
        # 纯靠时间和条数切窗口切不准；交给读得懂语义的模型挑，我们再按编号回库取原文。
        "llm_chain": True,
        "llm_chain_scenes": 5,
    },
    "match": {
        # 缘分榜上榜几个人：第 1 名做主角，其余当备选。
        "rank_size": 4,
    },
    "profile": {
        # 16 型人格徽章：off 关闭 / mbti 官方译名 / sbti 玩梗版 / acgti 二次元版。
        "type_tag": "mbti",
    },
    "persona": {
        # 借用 AstrBot 当前会话的人格来决定文案口吻（不影响结论与格式）。
        # 默认开：装了插件的人基本都给 bot 配了人格，让画像带上 bot 自己的口吻
        # 比一份中立报告有意思得多；不想要的可以在 WebUI 里关掉。
        "use_astrbot_persona": True,
        "persona_id": "",
        # 可选：允许其他插件的 @filter.on_llm_request 钩子（如世界树词条）往
        # 分析请求里补充设定。会稀释「只依据语料」的约束，所以默认关闭。
        "allow_llm_hooks": False,
    },
    "compat": {
        "legacy_commands": True,
        "love_commands": True,
    },
    "behavior": {
        "quiet_progress": False,
        "help_card": True,
        "history_limit": 20,
        "allow_self_only": False,
        "enabled_groups": [],
    },
}

#: 允许 WebUI 直接改写的配置路径白名单。没列进来的键 WebUI 一律拒绝，
#: 避免前端一个手滑就把敏感开关（比如同步机器人头像）打开。
DASHBOARD_WRITABLE: frozenset[str] = frozenset(
    {
        "llm.provider_id",
        "llm.model",
        "llm.retry_times",
        "collect.passive_capture",
        "collect.backfill_rounds",
        "collect.max_messages",
        "collect.min_messages",
        "collect.retention_days",
        "collect.sampling",
        "collect.cursor_field",
        "collect.filter_commands",
        "collect.strip_urls",
        "collect.fold_repeats",
        "render.backend",
        "render.theme",
        "render.card_scale",
        "render.image_format",
        "render.image_quality",
        "render.show_evidence",
        "render.show_avatar",
        "render.footer_note",
        "render.font_family",
        "render.font_title_family",
        "render.font_source",
        "limits.user_cooldown_sec",
        "limits.target_cooldown_sec",
        "limits.group_daily_quota",
        "limits.max_concurrency",
        "privacy.include_profile_fields",
        "privacy.protected_user_ids",
        "privacy.allow_opt_out",
        "privacy.redact_pii",
        "inject.enabled",
        "inject.max_chars",
        "inject.max_age_days",
        "behavior.quiet_progress",
        "behavior.help_card",
        "behavior.history_limit",
        "behavior.allow_self_only",
        "behavior.enabled_groups",
        "persona_clone.enabled",
        "persona_clone.require_admin",
        "persona_clone.clear_history_on_switch",
        "compat.legacy_commands",
        "compat.love_commands",
        "dialogue.context_enabled",
        "dialogue.context_span",
        "dialogue.max_scenes",
        "dialogue.max_lines",
        "dialogue.social_signals",
        "dialogue.llm_chain",
        "dialogue.llm_chain_scenes",
        "match.rank_size",
        "profile.type_tag",
        "persona.use_astrbot_persona",
        "persona.persona_id",
        "persona.allow_llm_hooks",
        "love.enabled",
        "love.min_messages",
        "love.day_start_hour",
        "love.sensitivity",
        "love.llm_commentary",
        "love.show_trend",
        "love.leaderboard_size",
        "love.notice_collect",
        "love.theme",
    },
)

VALID_THEMES = ("aurora", "ink", "neon", "paper", "dossier", "sakura")
#: 配置项 render.theme 还多一个 "auto"（自动挡）：它不是配色，而是「按画像临场挑一套」。
VALID_THEME_CHOICES = ("auto", *VALID_THEMES)
#: 恋爱卡单独一批主题：机密档案 / 杂志排版那套皮肤放恋爱诊断上完全不是一回事。
VALID_LOVE_THEMES = ("sakura", "moonlit", "dusk", "berry")
VALID_LOVE_THEME_CHOICES = ("auto", *VALID_LOVE_THEMES)
VALID_BACKENDS = ("auto", "local_first", "t2i_only", "text_only")
#: 16 型人格徽章的命名档位，见 prism.typetags。
VALID_TYPE_TAGS = ("off", "mbti", "sbti", "acgti")
VALID_SAMPLING = ("layered", "recent")
VALID_IMAGE_FORMATS = ("jpeg", "png")
#: 翻群历史时用哪种翻页方式。名字含义 = 读哪个字段 + 取这一页的哪一端，
#: 六种组合详见 prism.history；auto = 自动逐个试探并记住每个群实测可用的那种。
#: 末尾两个是 v1.1.3 的旧名字，保留是为了不让升级前存下来的配置被校验拒掉。
VALID_CURSOR_FIELDS = (
    "auto",
    "seq_first",
    "id_first",
    "seq_last",
    "id_last",
    "seq_oldest",
    "id_oldest",
    "message_seq",
    "message_id",
)

_ENUMS: dict[str, tuple[str, ...]] = {
    "render.theme": VALID_THEME_CHOICES,
    "render.backend": VALID_BACKENDS,
    "render.image_format": VALID_IMAGE_FORMATS,
    "collect.sampling": VALID_SAMPLING,
    "collect.cursor_field": VALID_CURSOR_FIELDS,
    "love.theme": VALID_LOVE_THEME_CHOICES,
    "profile.type_tag": VALID_TYPE_TAGS,
}

_RANGES: dict[str, tuple[int, int]] = {
    "dialogue.context_span": (0, 8),
    "dialogue.max_scenes": (1, 40),
    "dialogue.max_lines": (10, 300),
    "dialogue.llm_chain_scenes": (1, 12),
    "match.rank_size": (1, 6),
    "llm.retry_times": (0, 5),
    "collect.backfill_rounds": (0, 60),
    "collect.page_size": (20, 200),
    "collect.max_messages": (20, 3000),
    "collect.min_messages": (1, 500),
    "collect.min_chars": (1, 40),
    "collect.retention_days": (0, 3650),
    "collect.max_per_group": (500, 2000000),
    "render.card_scale": (100, 300),
    "render.image_quality": (60, 100),
    "limits.user_cooldown_sec": (0, 86400),
    "limits.target_cooldown_sec": (0, 86400),
    "limits.group_daily_quota": (0, 100000),
    "limits.max_concurrency": (1, 16),
    "inject.max_chars": (50, 4000),
    "inject.max_age_days": (1, 3650),
    "behavior.history_limit": (1, 500),
    "love.min_messages": (1, 200),
    "love.day_start_hour": (0, 12),
    "love.sensitivity": (1, 100),
    "love.leaderboard_size": (3, 30),
}


class ConfigError(ValueError):
    """WebUI 提交了非法配置。"""


def _coerce(value: Any, default: Any) -> Any:
    """按默认值的类型强制转换，转不动就用默认值。"""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace("\n", ",").split(",")]
            return [p for p in parts if p]
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if str(v).strip()]
        return list(default)
    if value is None:
        return default
    return str(value)


class PrismConfig:
    """对 AstrBotConfig 的只读友好包装（写入走 set / save）。"""

    __slots__ = ("_raw", "_save")

    def __init__(self, raw: Any, save: Any = None) -> None:
        self._raw = raw if raw is not None else {}
        self._save = save

    # -- 读取 ---------------------------------------------------------------
    def get(self, path: str) -> Any:
        group, _, key = path.partition(".")
        defaults = DEFAULTS.get(group, {})
        default = defaults.get(key)
        section = self._raw.get(group) if hasattr(self._raw, "get") else None
        if not isinstance(section, dict) or key not in section:
            return list(default) if isinstance(default, list) else default
        return _coerce(section.get(key), default)

    def int_of(self, path: str) -> int:
        value = int(self.get(path) or 0)
        low, high = _RANGES.get(path, (None, None))
        if low is not None:
            value = max(low, min(high, value))
        return value

    def bool_of(self, path: str) -> bool:
        return bool(self.get(path))

    def str_of(self, path: str) -> str:
        value = str(self.get(path) or "").strip()
        allowed = _ENUMS.get(path)
        if allowed and value not in allowed:
            group, _, key = path.partition(".")
            return str(DEFAULTS[group][key])
        return value

    def list_of(self, path: str) -> list[str]:
        value = self.get(path)
        return [str(v).strip() for v in value or [] if str(v).strip()]

    # -- 写入 ---------------------------------------------------------------
    def set(self, path: str, value: Any) -> Any:
        """写入单个配置项。返回归一化后的值。"""
        group, _, key = path.partition(".")
        if group not in DEFAULTS or key not in DEFAULTS[group]:
            raise ConfigError(f"未知配置项：{path}")
        default = DEFAULTS[group][key]
        coerced = _coerce(value, default)
        allowed = _ENUMS.get(path)
        if allowed and coerced not in allowed:
            raise ConfigError(f"{path} 只能是 {'/'.join(allowed)} 之一，收到 {coerced!r}")
        low, high = _RANGES.get(path, (None, None))
        if low is not None and isinstance(coerced, int) and not low <= coerced <= high:
            raise ConfigError(f"{path} 必须在 {low} 到 {high} 之间，收到 {coerced}")
        section = self._raw.setdefault(group, {})
        if not isinstance(section, dict):
            section = {}
            self._raw[group] = section
        section[key] = coerced
        return coerced

    def apply_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """批量写入 WebUI 提交的扁平补丁，返回实际生效的值。"""
        if not isinstance(patch, dict):
            raise ConfigError("请求体必须是对象。")
        rejected = [k for k in patch if k not in DASHBOARD_WRITABLE]
        if rejected:
            raise ConfigError("以下配置项不允许从 WebUI 修改：" + "、".join(sorted(rejected)))
        applied: dict[str, Any] = {}
        for path, value in patch.items():
            applied[path] = self.set(path, value)
        self.save()
        return applied

    def save(self) -> None:
        if callable(self._save):
            self._save()

    # -- 便捷派生 -----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """给 WebUI 的完整配置快照（只含白名单内的项）。"""
        return {path: self.get(path) for path in sorted(DASHBOARD_WRITABLE)}

    def profile_fields(self) -> list[str]:
        from .models import PROFILE_FIELD_LABELS

        blocked = {"phone", "email", "address", "qid", "reg_time"}
        fields = [
            f
            for f in self.list_of("privacy.include_profile_fields")
            if f in PROFILE_FIELD_LABELS and f not in blocked
        ]
        return fields or ["nickname", "card"]

    def group_allowed(self, group_id: str) -> bool:
        allowed = self.list_of("behavior.enabled_groups")
        return not allowed or str(group_id) in allowed

    def is_protected(self, user_id: str) -> bool:
        return str(user_id) in set(self.list_of("privacy.protected_user_ids"))
