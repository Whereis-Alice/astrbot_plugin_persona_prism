"""v1.1 兼容层：翻页游标、store 迁移、布局选项、新增配置项。

这些都是为了兼容上游「画像」系列玩法和 LLBot（幸运莉莉娅）而新加的东西，
单独放一份测试，方便日后回归时一眼看出坏在哪一层。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from astrbot_plugin_persona_prism.prism import dashboard
from astrbot_plugin_persona_prism.prism.collector import parse_history_page
from astrbot_plugin_persona_prism.prism.config import DASHBOARD_WRITABLE, DEFAULTS, PrismConfig
from astrbot_plugin_persona_prism.prism.prompts import VALID_LAYOUTS
from astrbot_plugin_persona_prism.prism.store import PrismStore

PLUGIN_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 翻页游标：LLBot 上 message_id 与 message_seq 是两套编号
# ---------------------------------------------------------------------------


def _cursor(raw: object) -> int | None:
    main = pytest.importorskip("astrbot_plugin_persona_prism.main")
    return main._history_cursor(raw)


def test_history_cursor_prefers_message_seq() -> None:
    assert _cursor({"message_seq": 520, "real_seq": 99, "message_id": 12345}) == 520


def test_history_cursor_falls_back_to_real_seq_then_message_id() -> None:
    assert _cursor({"real_seq": "99", "message_id": 12345}) == 99
    assert _cursor({"message_id": "12345"}) == 12345


def test_history_cursor_accepts_negative_message_id() -> None:
    """NapCat 会返回负数 message_id，上游用 isdigit() 判断直接翻不动页。"""
    assert _cursor({"message_id": -1234567}) == -1234567


def test_history_cursor_returns_none_without_usable_field() -> None:
    assert _cursor({}) is None
    assert _cursor({"message_id": ""}) is None
    assert _cursor("not a dict") is None


def test_parse_history_page_extracts_message_seq() -> None:
    rows = parse_history_page(
        [
            {
                "message_id": 111,
                "message_seq": 520,
                "time": 1700000000,
                "sender": {"user_id": 10001, "nickname": "阿狸"},
                "message": [{"type": "text", "data": {"text": "修风车"}}],
            },
        ],
    )
    assert rows[0]["message_seq"] == "520"
    assert rows[0]["message_id"] == "111"


def test_parse_history_page_falls_back_to_real_seq() -> None:
    rows = parse_history_page(
        [
            {
                "message_id": 111,
                "real_seq": "77",
                "time": 1700000000,
                "sender": {"user_id": 10001},
                "message": [{"type": "text", "data": {"text": "修风车"}}],
            },
        ],
    )
    assert rows[0]["message_seq"] == "77"


def test_parse_history_page_omits_seq_when_absent() -> None:
    rows = parse_history_page(
        [
            {
                "message_id": 111,
                "time": 1700000000,
                "sender": {"user_id": 10001},
                "message": [{"type": "text", "data": {"text": "修风车"}}],
            },
        ],
    )
    assert "message_seq" not in rows[0]


# ---------------------------------------------------------------------------
# store：prompt_entries.layout 与旧库迁移
# ---------------------------------------------------------------------------


def test_prompt_entry_layout_roundtrip(tmp_path) -> None:
    store = PrismStore(tmp_path / "prism.db")
    try:
        store.upsert_prompt_entry(
            "tarot",
            command="棱镜塔罗",
            label="塔罗解读",
            prompt="抽一张牌",
            structured=False,
            layout="markdown",
        )
        entry = store.list_prompt_entries()[0]
        assert entry["layout"] == "markdown"
        assert entry["structured"] is False
        store.upsert_prompt_entry(
            "tarot",
            command="棱镜塔罗",
            label="塔罗解读",
            prompt="抽一张牌",
            structured=True,
            layout="card",
        )
        assert store.list_prompt_entries()[0]["layout"] == "card"
    finally:
        store.close()


def test_prompt_entry_layout_defaults_to_empty(tmp_path) -> None:
    store = PrismStore(tmp_path / "prism.db")
    try:
        store.upsert_prompt_entry("k", command="棱镜甲", label="甲", prompt="正文")
        assert store.list_prompt_entries()[0]["layout"] == ""
    finally:
        store.close()


def _legacy_db(path: Path) -> None:
    """建一个 v1.0 结构的 prompt_entries（没有 layout 列）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE prompt_entries (
                key TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                label TEXT NOT NULL,
                prompt TEXT NOT NULL,
                structured INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL
            )
            """,
        )
        conn.execute(
            "INSERT INTO prompt_entries VALUES ('old', '棱镜旧', '旧条目', '正文', 1, 1, 1700000000)",
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_adds_layout_column_to_old_db(tmp_path) -> None:
    db = tmp_path / "prism.db"
    _legacy_db(db)
    store = PrismStore(db)
    try:
        entries = store.list_prompt_entries()
        assert [item["key"] for item in entries] == ["old"]
        assert entries[0]["layout"] == ""
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path) -> None:
    db = tmp_path / "prism.db"
    _legacy_db(db)
    for _ in range(3):
        store = PrismStore(db)
        store.close()
    store = PrismStore(db)
    try:
        store.upsert_prompt_entry("new", command="棱镜新", label="新", prompt="正文", layout="text")
        got = {item["key"]: item["layout"] for item in store.list_prompt_entries()}
        assert got == {"old": "", "new": "text"}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# dashboard 布局选项
# ---------------------------------------------------------------------------


def test_layout_choices_match_valid_layouts() -> None:
    assert [item["value"] for item in dashboard.LAYOUT_CHOICES] == list(VALID_LAYOUTS)
    for item in dashboard.LAYOUT_CHOICES:
        assert item["label"]
        assert item["hint"]


def test_validate_prompt_payload_normalizes_layout() -> None:
    base = {
        "key": "tarot",
        "command": "棱镜塔罗",
        "label": "塔罗解读",
        "prompt": "抽一张牌并解读，至少写四十个字才够长。",
    }
    reserved: set[str] = set()
    got = dashboard.validate_prompt_payload({**base, "layout": "markdown"}, reserved=reserved)
    # layout 合法时以它为准，structured 只是派生标记
    assert got["layout"] == "markdown"
    assert got["structured"] is False
    got = dashboard.validate_prompt_payload({**base, "layout": "card"}, reserved=reserved)
    assert got["structured"] is True
    # 非法或缺省 layout 时按 structured 反推，兼容只发 structured 的老客户端
    got = dashboard.validate_prompt_payload(
        {**base, "layout": "ghost", "structured": True},
        reserved=reserved,
    )
    assert got["layout"] == "card"
    got = dashboard.validate_prompt_payload({**base, "structured": False}, reserved=reserved)
    assert got["layout"] == "text"


# ---------------------------------------------------------------------------
# 新增配置项
# ---------------------------------------------------------------------------


def test_new_config_defaults_are_conservative() -> None:
    clone = DEFAULTS["persona_clone"]
    assert clone["require_admin"] is True
    assert clone["clear_history_on_switch"] is True
    assert DEFAULTS["compat"]["legacy_commands"] is True


def test_render_quality_defaults() -> None:
    render = DEFAULTS["render"]
    assert render["card_scale"] == 200
    assert render["image_format"] == "jpeg"
    assert 60 <= render["image_quality"] <= 100
    assert render["font_family"] == ""
    assert render["font_title_family"] == ""
    assert render["font_source"] == ""


def test_config_reads_new_keys() -> None:
    cfg = PrismConfig({"compat": {"legacy_commands": False}})
    assert cfg.get("compat.legacy_commands") is False
    assert cfg.get("persona_clone.require_admin") is True


def test_conf_schema_covers_every_default_group() -> None:
    """_conf_schema.json 漏了分组，AstrBot 面板上就点不到这些开关。"""
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    for group, values in DEFAULTS.items():
        assert group in schema, group
        items = schema[group].get("items", {})
        for key in values:
            assert key in items, f"{group}.{key}"


def test_dashboard_writable_keys_are_grouped_in_ui() -> None:
    """每个可写项都要落在某个已命名分组里，否则前端会渲染出无标题面板。"""
    for path in DASHBOARD_WRITABLE:
        group = path.split(".", 1)[0]
        assert group in dashboard.GROUP_TITLES, path
