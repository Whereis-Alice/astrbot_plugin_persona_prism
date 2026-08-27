"""配置层测试：默认值回落、类型强转、枚举/范围校验、WebUI 白名单。"""

from __future__ import annotations

import pytest
from astrbot_plugin_persona_prism.prism.config import (
    DASHBOARD_WRITABLE,
    DEFAULTS,
    ConfigError,
    PrismConfig,
)


def test_missing_section_falls_back_to_default() -> None:
    cfg = PrismConfig({})
    assert cfg.get("render.theme") == "aurora"
    assert cfg.int_of("collect.max_messages") == 400
    assert cfg.bool_of("collect.passive_capture") is True


def test_unknown_path_returns_none() -> None:
    cfg = PrismConfig({})
    assert cfg.get("render.not_exist") is None
    assert cfg.get("nope.nope") is None


def test_list_default_is_copied_not_shared() -> None:
    cfg = PrismConfig({})
    first = cfg.get("privacy.protected_user_ids")
    first.append("999")
    assert cfg.get("privacy.protected_user_ids") == []


def test_string_to_bool_coercion() -> None:
    cfg = PrismConfig({"collect": {"passive_capture": "no"}})
    assert cfg.bool_of("collect.passive_capture") is False
    cfg2 = PrismConfig({"collect": {"passive_capture": "ON"}})
    assert cfg2.bool_of("collect.passive_capture") is True


def test_string_to_list_coercion_supports_comma_and_newline() -> None:
    cfg = PrismConfig({"behavior": {"enabled_groups": "111, 222\n333 ,, "}})
    assert cfg.list_of("behavior.enabled_groups") == ["111", "222", "333"]


def test_int_of_clamps_into_range() -> None:
    cfg = PrismConfig({"collect": {"max_messages": 999999}})
    assert cfg.int_of("collect.max_messages") == 3000
    cfg2 = PrismConfig({"collect": {"max_messages": 1}})
    assert cfg2.int_of("collect.max_messages") == 20


def test_int_of_survives_garbage_value() -> None:
    cfg = PrismConfig({"limits": {"max_concurrency": "abc"}})
    assert cfg.int_of("limits.max_concurrency") == 2


def test_str_of_rejects_illegal_enum() -> None:
    cfg = PrismConfig({"render": {"theme": "rainbow", "backend": "magic"}})
    assert cfg.str_of("render.theme") == "aurora"
    assert cfg.str_of("render.backend") == "auto"


def test_set_writes_back_into_raw_dict() -> None:
    raw: dict = {}
    cfg = PrismConfig(raw)
    assert cfg.set("render.theme", "neon") == "neon"
    assert raw["render"]["theme"] == "neon"


def test_set_rejects_unknown_path() -> None:
    cfg = PrismConfig({})
    with pytest.raises(ConfigError):
        cfg.set("render.blur_radius", 3)


def test_set_rejects_illegal_enum_and_out_of_range() -> None:
    cfg = PrismConfig({})
    with pytest.raises(ConfigError):
        cfg.set("collect.sampling", "random")
    with pytest.raises(ConfigError):
        cfg.set("render.image_quality", 5)


def test_apply_patch_rejects_non_whitelisted_key() -> None:
    cfg = PrismConfig({})
    with pytest.raises(ConfigError):
        cfg.apply_patch({"persona_clone.sync_bot_avatar": True})


def test_apply_patch_rejects_non_dict() -> None:
    cfg = PrismConfig({})
    with pytest.raises(ConfigError):
        cfg.apply_patch(["render.theme"])  # type: ignore[arg-type]


def test_apply_patch_saves_once_and_returns_normalized() -> None:
    calls: list[int] = []
    cfg = PrismConfig({}, save=lambda: calls.append(1))
    applied = cfg.apply_patch({"render.theme": "ink", "limits.group_daily_quota": "50"})
    assert applied == {"render.theme": "ink", "limits.group_daily_quota": 50}
    assert calls == [1]


def test_apply_patch_does_not_save_when_rejected() -> None:
    calls: list[int] = []
    cfg = PrismConfig({}, save=lambda: calls.append(1))
    with pytest.raises(ConfigError):
        cfg.apply_patch({"render.theme": "sparkle"})
    assert calls == []


def test_snapshot_covers_whole_whitelist() -> None:
    cfg = PrismConfig({})
    snap = cfg.snapshot()
    assert set(snap) == set(DASHBOARD_WRITABLE)


def test_dangerous_paths_are_not_writable_from_webui() -> None:
    for path in (
        "persona_clone.sync_bot_nickname",
        "persona_clone.sync_bot_avatar",
        "collect.page_size",
        "collect.min_chars",
        "collect.max_per_group",
    ):
        assert path not in DASHBOARD_WRITABLE


def test_persona_clone_switches_are_writable_from_webui() -> None:
    """克隆开关允许在 WebUI 调；改机器人昵称/头像这类副作用大的仍只走配置文件。"""
    for path in (
        "persona_clone.enabled",
        "persona_clone.require_admin",
        "persona_clone.clear_history_on_switch",
        "compat.legacy_commands",
    ):
        assert path in DASHBOARD_WRITABLE


def test_whitelist_paths_all_exist_in_defaults() -> None:
    for path in DASHBOARD_WRITABLE:
        group, _, key = path.partition(".")
        assert group in DEFAULTS, path
        assert key in DEFAULTS[group], path


def test_profile_fields_filters_sensitive_and_unknown() -> None:
    cfg = PrismConfig(
        {"privacy": {"include_profile_fields": ["nickname", "phone", "email", "ghost", "level"]}},
    )
    assert cfg.profile_fields() == ["nickname", "level"]


def test_profile_fields_falls_back_when_empty() -> None:
    cfg = PrismConfig({"privacy": {"include_profile_fields": []}})
    assert cfg.profile_fields() == ["nickname", "card"]


def test_group_allowed_defaults_to_open() -> None:
    cfg = PrismConfig({})
    assert cfg.group_allowed("12345") is True


def test_group_allowed_respects_allowlist() -> None:
    cfg = PrismConfig({"behavior": {"enabled_groups": ["100", "200"]}})
    assert cfg.group_allowed("100") is True
    assert cfg.group_allowed(200) is True
    assert cfg.group_allowed("300") is False


def test_is_protected_compares_as_string() -> None:
    cfg = PrismConfig({"privacy": {"protected_user_ids": [10001]}})
    assert cfg.is_protected("10001") is True
    assert cfg.is_protected("10002") is False


def test_save_is_optional() -> None:
    PrismConfig({}).save()
