"""dashboard 层：WebUI 数据装配、参数夹取、设置白名单、提示词校验。"""

from __future__ import annotations

from typing import Any

import pytest
from astrbot_plugin_persona_prism.prism import dashboard
from astrbot_plugin_persona_prism.prism.config import DASHBOARD_WRITABLE, PrismConfig
from astrbot_plugin_persona_prism.prism.models import CorpusMessage, PortraitRecord
from astrbot_plugin_persona_prism.prism.prompts import VALID_LAYOUTS, PromptLibrary
from astrbot_plugin_persona_prism.prism.store import PrismStore

PLATFORM = "aiocqhttp"


@pytest.fixture()
def store(tmp_path):
    db = PrismStore(tmp_path / "prism.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def config():
    return PrismConfig({})


def _record(**kwargs: Any) -> PortraitRecord:
    base: dict[str, Any] = {
        "platform": PLATFORM,
        "group_id": "700",
        "group_name": "风车研究会",
        "user_id": "10001",
        "user_name": "阿狸",
        "kind": "portrait",
        "kind_label": "人格画像",
        "theme": "aurora",
        "payload": {"headline": "很会修风车"},
        "text": "正文",
        "sample_size": 120,
        "corpus_chars": 800,
        "confidence": 0.72,
        "model": "gpt-x",
    }
    base.update(kwargs)
    return PortraitRecord(**base)


class _Args(dict):
    """模拟 quart 的 request.args。"""


# ---------------------------------------------------------------------------
# 静态元数据
# ---------------------------------------------------------------------------


def test_group_titles_cover_every_writable_group():
    groups = {path.split(".")[0] for path in DASHBOARD_WRITABLE}
    assert groups <= set(dashboard.GROUP_TITLES)


def test_group_titles_all_have_label_and_bare_icon_name():
    for meta in dashboard.GROUP_TITLES.values():
        assert meta["label"]
        icon = meta["icon"]
        assert icon and "-" not in icon and "#" not in icon


def test_field_hints_cover_every_writable_path():
    missing = sorted(path for path in DASHBOARD_WRITABLE if path not in dashboard.FIELD_HINTS)
    assert missing == []


def test_field_hints_do_not_describe_unwritable_paths():
    extra = sorted(path for path in dashboard.FIELD_HINTS if path not in DASHBOARD_WRITABLE)
    assert extra == []


def test_backend_and_sampling_choices_are_well_formed():
    assert [c["value"] for c in dashboard.BACKEND_CHOICES] == [
        "auto",
        "local_first",
        "t2i_only",
        "text_only",
    ]
    assert [c["value"] for c in dashboard.SAMPLING_CHOICES] == ["layered", "recent"]
    for choice in dashboard.BACKEND_CHOICES + dashboard.SAMPLING_CHOICES:
        assert choice["label"]
        assert choice["hint"]


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------


def test_build_overview_on_empty_store(store, config):
    data = dashboard.build_overview(store, config, prompt_count=5, version="v1.0.0")
    assert data["ok"] is True
    assert data["version"] == "v1.0.0"
    assert data["server_time"] > 0
    assert data["prompts"]["total"] == 5
    assert data["stats"]["portraits"] == 0
    assert data["runs"] == []


def test_build_overview_reports_render_and_flags(store, config):
    config.set("render.backend", "local_first")
    config.set("render.theme", "ink")
    data = dashboard.build_overview(store, config, backend_hint="t2i")
    assert data["render"]["backend"] == "local_first"
    assert data["render"]["backend_label"] == "本地优先"
    assert data["render"]["theme"] == "ink"
    assert data["render"]["last_backend"] == "t2i"
    assert "t2i" in data["render"]["backend_labels"]
    assert set(data["flags"]) == {
        "passive_capture",
        "inject_enabled",
        "allow_opt_out",
        "redact_pii",
        "clone_enabled",
        "sync_bot_nickname",
        "sync_bot_avatar",
    }
    assert all(isinstance(value, bool) for value in data["flags"].values())


def test_build_overview_backend_label_falls_back(store, config):
    # 配置里被塞了非法值时，config 层就会把它夹回默认的 auto，
    # 所以 WebUI 永远看不到空标签。
    config._raw.setdefault("render", {})["backend"] = "乱填的值"
    data = dashboard.build_overview(store, config)
    assert data["render"]["backend"] == "auto"
    assert data["render"]["backend_label"] == "自动（推荐）"


def test_build_overview_counts_real_data(store, config):
    store.save_portrait(_record())
    store.log_run(group_id="700", user_id="10001", kind="portrait", ok=True, backend="t2i")
    data = dashboard.build_overview(store, config)
    assert data["stats"]["portraits"] == 1
    assert data["stats"]["groups"] == 1
    assert len(data["runs"]) == 1


# ---------------------------------------------------------------------------
# 记录列表
# ---------------------------------------------------------------------------


def test_build_records_defaults_page_and_size(store):
    data = dashboard.build_records(store, _Args())
    assert data["page"] == 1
    assert data["size"] == 20
    assert data["total"] == 0
    assert data["pages"] == 1
    assert data["items"] == []


def test_build_records_clamps_size(store):
    assert dashboard.build_records(store, _Args(size="1"))["size"] == 5
    assert dashboard.build_records(store, _Args(size="9999"))["size"] == 100
    assert dashboard.build_records(store, _Args(size="不是数字"))["size"] == 20


def test_build_records_clamps_page(store):
    assert dashboard.build_records(store, _Args(page="0"))["page"] == 1
    assert dashboard.build_records(store, _Args(page="-3"))["page"] == 1


def test_build_records_paginates(store):
    for index in range(7):
        store.save_portrait(_record(user_id=str(9000 + index), user_name=f"成员{index}"))
    first = dashboard.build_records(store, _Args(size="5", page="1"))
    second = dashboard.build_records(store, _Args(size="5", page="2"))
    assert first["total"] == 7
    assert first["pages"] == 2
    assert len(first["items"]) == 5
    assert len(second["items"]) == 2
    ids = {item["id"] for item in first["items"]} | {item["id"] for item in second["items"]}
    assert len(ids) == 7


def test_build_records_filters_by_group_and_user(store):
    store.save_portrait(_record(group_id="700", user_id="1"))
    store.save_portrait(_record(group_id="800", user_id="2"))
    only_700 = dashboard.build_records(store, _Args(group_id="700"))
    assert only_700["total"] == 1
    assert only_700["items"][0]["group_id"] == "700"
    only_user2 = dashboard.build_records(store, _Args(user_id="2"))
    assert only_user2["total"] == 1


def test_build_records_accepts_plain_object_without_get(store):
    data = dashboard.build_records(store, object())
    assert data["page"] == 1
    assert data["size"] == 20


def test_build_records_items_expose_names(store):
    store.save_portrait(_record())
    item = dashboard.build_records(store, _Args())["items"][0]
    assert item["group_name"] == "风车研究会"
    assert item["user_name"] == "阿狸"


# ---------------------------------------------------------------------------
# 群树
# ---------------------------------------------------------------------------


def test_build_groups_on_empty_store(store):
    data = dashboard.build_groups(store)
    assert data == {"ok": True, "total_groups": 0, "total_members": 0, "groups": []}


def test_build_groups_counts_groups_and_members(store):
    store.save_portrait(_record(group_id="700", user_id="1", user_name="甲"))
    store.save_portrait(_record(group_id="700", user_id="2", user_name="乙"))
    store.save_portrait(_record(group_id="800", group_name="另一个群", user_id="3", user_name="丙"))
    data = dashboard.build_groups(store)
    assert data["total_groups"] == 2
    assert data["total_members"] == 3
    names = {node["group_id"]: node["group_name"] for node in data["groups"]}
    assert names["800"] == "另一个群"
    member_names = {member["user_name"] for node in data["groups"] for member in node["members"]}
    assert member_names == {"甲", "乙", "丙"}


# ---------------------------------------------------------------------------
# 记录详情
# ---------------------------------------------------------------------------


def test_build_record_detail_returns_payload_and_theme_label(store):
    record_id = store.save_portrait(_record(theme="ink"))
    data = dashboard.build_record_detail(store, record_id)
    assert data["ok"] is True
    assert data["record"]["id"] == record_id
    assert data["record"]["payload"]["headline"] == "很会修风车"
    assert data["record"]["text"] == "正文"
    assert data["record"]["theme_label"] == "水墨宣纸"


def test_build_record_detail_keeps_unknown_theme_as_is(store):
    record_id = store.save_portrait(_record(theme="奇怪主题"))
    data = dashboard.build_record_detail(store, record_id)
    assert data["record"]["theme_label"] == "奇怪主题"


def test_build_record_detail_rejects_missing_id(store):
    for raw in (None, "", 0, "abc"):
        with pytest.raises(dashboard.DashboardError):
            dashboard.build_record_detail(store, raw)


def test_build_record_detail_rejects_unknown_id(store):
    with pytest.raises(dashboard.DashboardError):
        dashboard.build_record_detail(store, 999999)


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------


def test_build_settings_exposes_only_whitelisted_paths(store, config):
    data = dashboard.build_settings(store, config)
    paths = {field["path"] for section in data["sections"] for field in section["fields"]}
    assert paths == set(DASHBOARD_WRITABLE)


def test_build_settings_sections_follow_group_order(store, config):
    data = dashboard.build_settings(store, config)
    order = [section["group"] for section in data["sections"]]
    expected = [g for g in dashboard.GROUP_TITLES if g in set(order)]
    assert order == expected


def test_build_settings_field_types_are_known(store, config):
    data = dashboard.build_settings(store, config)
    kinds = {field["type"] for section in data["sections"] for field in section["fields"]}
    assert kinds <= {"bool", "int", "list", "text", "choice", "multi"}


def test_build_settings_marks_enum_fields_as_choice(store, config):
    data = dashboard.build_settings(store, config)
    fields = {field["path"]: field for section in data["sections"] for field in section["fields"]}
    assert fields["render.backend"]["type"] == "choice"
    assert fields["collect.sampling"]["type"] == "choice"
    assert fields["render.theme"]["type"] == "choice"
    theme_values = [choice["value"] for choice in fields["render.theme"]["choices"]]
    # 6 套真主题 + 自动挡
    assert len(theme_values) == 7
    assert theme_values[0] == "auto"
    assert fields["love.theme"]["type"] == "choice"
    assert [c["value"] for c in fields["love.theme"]["choices"]] == theme_values
    assert fields["privacy.include_profile_fields"]["type"] == "multi"


def test_build_settings_multi_choices_exclude_sensitive_fields(store, config):
    data = dashboard.build_settings(store, config)
    fields = {field["path"]: field for section in data["sections"] for field in section["fields"]}
    values = {c["value"] for c in fields["privacy.include_profile_fields"]["choices"]}
    assert values.isdisjoint({"phone", "email", "address", "qid", "reg_time"})
    assert "nickname" in values or values


def test_build_settings_reports_readonly_clone_switches(store, config):
    data = dashboard.build_settings(store, config)
    assert set(data["readonly"]) == {
        "persona_clone.sync_bot_nickname",
        "persona_clone.sync_bot_avatar",
    }
    assert data["readonly"]["persona_clone.sync_bot_nickname"] is False
    assert data["readonly"]["persona_clone.sync_bot_avatar"] is False


def test_build_settings_lists_optouts(store, config):
    store.add_optout(PLATFORM, "700", "10001", user_name="阿狸")
    data = dashboard.build_settings(store, config)
    assert len(data["optouts"]) == 1
    assert data["optouts"][0]["user_id"] == "10001"


def test_build_settings_values_is_full_snapshot(store, config):
    data = dashboard.build_settings(store, config)
    # snapshot 是扁平的 path -> value 形式，前端直接按 path 取值。
    assert data["values"]["render.theme"]
    assert "collect.max_messages" in data["values"]
    assert set(data["values"]) == set(DASHBOARD_WRITABLE)


# ---------------------------------------------------------------------------
# 写设置
# ---------------------------------------------------------------------------


def test_apply_settings_accepts_wrapped_values(config):
    data = dashboard.apply_settings(config, {"values": {"render.theme": "neon"}})
    assert data["ok"] is True
    assert data["applied"] == {"render.theme": "neon"}
    assert data["values"]["render.theme"] == "neon"


def test_apply_settings_accepts_flat_payload(config):
    data = dashboard.apply_settings(config, {"render.theme": "paper"})
    assert config.str_of("render.theme") == "paper"
    assert data["applied"] == {"render.theme": "paper"}


def test_apply_settings_rejects_non_dict_payload(config):
    for payload in (None, [], "x", 3):
        with pytest.raises(dashboard.DashboardError):
            dashboard.apply_settings(config, payload)


def test_apply_settings_translates_config_error(config):
    with pytest.raises(dashboard.DashboardError):
        dashboard.apply_settings(config, {"values": {"render.theme": "不存在的主题"}})


def test_apply_settings_rejects_unwritable_path(config):
    with pytest.raises(dashboard.DashboardError):
        dashboard.apply_settings(config, {"values": {"persona_clone.sync_bot_avatar": True}})


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def test_build_prompts_lists_builtin_and_reserved(store):
    library = PromptLibrary()
    data = dashboard.build_prompts(store, library)
    assert data["ok"] is True
    # 6 条棱镜系列 + 5 条兼容上游的画像系列
    assert len(data["builtin"]) == 11
    assert data["custom"] == []
    assert len(data["reserved_commands"]) == 11
    assert data["reserved_commands"] == sorted(data["reserved_commands"])
    assert [item["value"] for item in data["layouts"]] == list(VALID_LAYOUTS)
    assert all(entry.get("layout") in VALID_LAYOUTS for entry in data["builtin"])


def test_build_prompts_includes_custom_entries(store):
    library = PromptLibrary()
    store.upsert_prompt_entry(
        "tarot",
        command="棱镜塔罗",
        label="塔罗解读",
        prompt="根据聊天记录抽一张塔罗牌并解读。",
    )
    data = dashboard.build_prompts(store, library)
    assert [entry["key"] for entry in data["custom"]] == ["tarot"]


def _payload(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": "tarot",
        "command": "棱镜塔罗",
        "label": "塔罗解读",
        "prompt": "根据聊天记录抽一张塔罗牌并解读。",
    }
    base.update(kwargs)
    return base


def test_validate_prompt_payload_happy_path():
    data = dashboard.validate_prompt_payload(_payload(), reserved=set())
    assert data["key"] == "tarot"
    assert data["command"] == "棱镜塔罗"
    assert data["structured"] is True
    assert data["enabled"] is True


def test_validate_prompt_payload_defaults_label_to_command():
    data = dashboard.validate_prompt_payload(_payload(label=""), reserved=set())
    assert data["label"] == "棱镜塔罗"


def test_validate_prompt_payload_keeps_explicit_flags():
    data = dashboard.validate_prompt_payload(
        _payload(structured=False, enabled=False),
        reserved=set(),
    )
    assert data["structured"] is False
    assert data["enabled"] is False


def test_validate_prompt_payload_rejects_bad_key():
    for key in ("", "  ", "有中文", "with space", "a.b"):
        with pytest.raises(dashboard.DashboardError):
            dashboard.validate_prompt_payload(_payload(key=key), reserved=set())


def test_validate_prompt_payload_accepts_dash_and_underscore_keys():
    for key in ("my_key", "my-key", "k1"):
        data = dashboard.validate_prompt_payload(_payload(key=key), reserved=set())
        assert data["key"] == key


def test_validate_prompt_payload_rejects_empty_or_spaced_command():
    with pytest.raises(dashboard.DashboardError):
        dashboard.validate_prompt_payload(_payload(command=""), reserved=set())
    with pytest.raises(dashboard.DashboardError):
        dashboard.validate_prompt_payload(_payload(command="棱镜 塔罗"), reserved=set())


def test_validate_prompt_payload_rejects_reserved_command():
    with pytest.raises(dashboard.DashboardError):
        dashboard.validate_prompt_payload(
            _payload(command="棱镜画像"),
            reserved={"棱镜画像"},
        )


def test_validate_prompt_payload_enforces_prompt_length():
    with pytest.raises(dashboard.DashboardError):
        dashboard.validate_prompt_payload(_payload(prompt="太短"), reserved=set())
    with pytest.raises(dashboard.DashboardError):
        dashboard.validate_prompt_payload(_payload(prompt="长" * 8001), reserved=set())
    ok = dashboard.validate_prompt_payload(_payload(prompt="长" * 8000), reserved=set())
    assert len(ok["prompt"]) == 8000


def test_validate_prompt_payload_rejects_non_dict():
    for payload in (None, [], "x"):
        with pytest.raises(dashboard.DashboardError):
            dashboard.validate_prompt_payload(payload, reserved=set())


def test_dashboard_error_message_is_human_readable(store):
    with pytest.raises(dashboard.DashboardError) as excinfo:
        dashboard.build_record_detail(store, 0)
    assert str(excinfo.value)


def test_corpus_message_import_is_used_for_group_name_fallback(store):
    """群名在语料侧也能被记住，WebUI 才不会显示成裸群号。"""
    store.touch_group(PLATFORM, "900", "临时群")
    store.add_messages(PLATFORM, "900", [CorpusMessage("1", "5", "路人", "在", 1700000000)])
    assert store.group_name(PLATFORM, "900") == "临时群"
