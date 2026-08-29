"""持久层测试：幂等写入、按群分区、历史裁剪、配额、退出名单、异步代理。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

import pytest
from astrbot_plugin_persona_prism.prism.models import CorpusMessage, PortraitRecord
from astrbot_plugin_persona_prism.prism.store import AsyncStore, PrismStore

PLATFORM = "aiocqhttp"
GROUP = "700"
USER = "10001"


@pytest.fixture()
def store(tmp_path):
    db = PrismStore(tmp_path / "nested" / "prism.db")
    try:
        yield db
    finally:
        db.close()


def _msg(mid: str, *, user: str = USER, name: str = "阿狸", text: str = "在的", ts: int = 1700000000):
    return CorpusMessage(mid, user, name, text, ts)


def _record(**kwargs: Any) -> PortraitRecord:
    base: dict[str, Any] = {
        "platform": PLATFORM,
        "group_id": GROUP,
        "group_name": "风车研究会",
        "user_id": USER,
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


# -- 建库 -------------------------------------------------------------------


def test_init_creates_parent_directory(tmp_path) -> None:
    db = PrismStore(tmp_path / "a" / "b" / "prism.db")
    try:
        assert db.path.is_file()
    finally:
        db.close()


def test_reopen_keeps_data(tmp_path) -> None:
    path = tmp_path / "prism.db"
    first = PrismStore(path)
    first.add_messages(PLATFORM, GROUP, [_msg("1")])
    first.close()
    second = PrismStore(path)
    try:
        assert second.corpus_stats()["total"] == 1
    finally:
        second.close()


# -- 语料 -------------------------------------------------------------------


def test_add_messages_is_idempotent(store: PrismStore) -> None:
    rows = [_msg("1"), _msg("2", text="第二句")]
    assert store.add_messages(PLATFORM, GROUP, rows) == 2
    assert store.add_messages(PLATFORM, GROUP, rows) == 0
    assert store.corpus_stats()["total"] == 2


def test_add_messages_returns_zero_for_empty_input(store: PrismStore) -> None:
    assert store.add_messages(PLATFORM, GROUP, []) == 0


def test_add_messages_drops_rows_without_ids(store: PrismStore) -> None:
    assert store.add_messages(PLATFORM, GROUP, [_msg(""), _msg("9", user="")]) == 0


def test_add_messages_refreshes_only_non_empty_user_name(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [_msg("1", name="阿狸")])
    store.add_messages(PLATFORM, GROUP, [_msg("1", name="")])
    assert store.latest_user_name(PLATFORM, GROUP, USER) == "阿狸"
    store.add_messages(PLATFORM, GROUP, [_msg("1", name="爱乃")])
    assert store.latest_user_name(PLATFORM, GROUP, USER) == "爱乃"


def test_same_message_id_in_other_group_is_kept(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [_msg("1")])
    assert store.add_messages(PLATFORM, "800", [_msg("1")]) == 1
    assert store.corpus_stats(PLATFORM, GROUP)["total"] == 1
    assert store.corpus_stats(PLATFORM, "800")["total"] == 1


def test_fetch_user_corpus_is_time_ascending(store: PrismStore) -> None:
    store.add_messages(
        PLATFORM,
        GROUP,
        [
            _msg("3", text="第三", ts=1700000300),
            _msg("1", text="第一", ts=1700000100),
            _msg("2", text="第二", ts=1700000200),
        ],
    )
    texts = [row["text"] for row in store.fetch_user_corpus(PLATFORM, GROUP, USER)]
    assert texts == ["第一", "第二", "第三"]


def test_fetch_user_corpus_limit_keeps_newest_window(store: PrismStore) -> None:
    store.add_messages(
        PLATFORM,
        GROUP,
        [_msg(str(i), text="第" + str(i) + "句", ts=1700000000 + i) for i in range(5)],
    )
    texts = [row["text"] for row in store.fetch_user_corpus(PLATFORM, GROUP, USER, limit=2)]
    assert texts == ["第3句", "第4句"]


def test_fetch_user_corpus_isolates_users_and_groups(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [_msg("1"), _msg("2", user="20002", name="小明")])
    store.add_messages(PLATFORM, "800", [_msg("3", text="别群")])
    assert len(store.fetch_user_corpus(PLATFORM, GROUP, USER)) == 1
    assert len(store.fetch_user_corpus(PLATFORM, GROUP, "20002")) == 1
    assert store.fetch_user_corpus(PLATFORM, GROUP, "30003") == []


def test_corpus_stats_scope_and_bounds(store: PrismStore) -> None:
    store.add_messages(
        PLATFORM,
        GROUP,
        [_msg("1", ts=1700000000), _msg("2", user="20002", ts=1700009000)],
    )
    stats = store.corpus_stats(PLATFORM, GROUP)
    assert stats == {"total": 2, "users": 2, "oldest": 1700000000, "newest": 1700009000}
    assert store.corpus_stats(PLATFORM, "999")["total"] == 0


def test_corpus_stats_empty_table(store: PrismStore) -> None:
    assert store.corpus_stats() == {"total": 0, "users": 0, "oldest": 0, "newest": 0}


def test_prune_corpus_by_retention_keeps_zero_timestamps(store: PrismStore) -> None:
    now = int(time.time())
    store.add_messages(
        PLATFORM,
        GROUP,
        [
            _msg("old", ts=now - 100 * 86400),
            _msg("new", ts=now - 3600),
            _msg("nots", ts=0),
        ],
    )
    assert store.prune_corpus(retention_days=30, max_per_group=0) == 1
    ids = {row["message_id"] for row in store.fetch_user_corpus(PLATFORM, GROUP, USER)}
    assert ids == {"new", "nots"}


def test_prune_corpus_by_group_cap_drops_oldest(store: PrismStore) -> None:
    store.add_messages(
        PLATFORM,
        GROUP,
        [_msg(str(i), text="第" + str(i), ts=1700000000 + i) for i in range(6)],
    )
    assert store.prune_corpus(retention_days=0, max_per_group=2) == 4
    texts = [row["text"] for row in store.fetch_user_corpus(PLATFORM, GROUP, USER)]
    assert texts == ["第4", "第5"]


def test_prune_corpus_noop_when_both_limits_disabled(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [_msg("1", ts=1)])
    assert store.prune_corpus(retention_days=0, max_per_group=0) == 0
    assert store.corpus_stats()["total"] == 1


def test_clear_user_corpus_only_touches_that_user(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [_msg("1"), _msg("2", user="20002")])
    assert store.clear_user_corpus(PLATFORM, GROUP, USER) == 1
    assert store.corpus_stats(PLATFORM, GROUP)["total"] == 1


def test_clear_group_corpus_also_resets_scan_state(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [_msg("1")])
    store.set_scan_state(PLATFORM, GROUP, oldest_seq="123", newest_seq="456")
    assert store.clear_group_corpus(PLATFORM, GROUP) == 1
    assert store.get_scan_state(PLATFORM, GROUP)["oldest_seq"] == ""


# -- 群元信息 ---------------------------------------------------------------


def test_touch_group_ignores_empty_group_id(store: PrismStore) -> None:
    store.touch_group(PLATFORM, "", "不该写入")
    assert store.group_name(PLATFORM, "") == ""


def test_touch_group_never_overwrites_name_with_blank(store: PrismStore) -> None:
    store.touch_group(PLATFORM, GROUP, "风车研究会")
    store.touch_group(PLATFORM, GROUP, "")
    assert store.group_name(PLATFORM, GROUP) == "风车研究会"
    store.touch_group(PLATFORM, GROUP, "新名字")
    assert store.group_name(PLATFORM, GROUP) == "新名字"


def test_group_theme_roundtrip_and_default(store: PrismStore) -> None:
    assert store.group_theme(PLATFORM, GROUP) == ""
    store.set_group_theme(PLATFORM, GROUP, "ink")
    assert store.group_theme(PLATFORM, GROUP) == "ink"


def test_group_theme_and_name_do_not_clobber_each_other(store: PrismStore) -> None:
    store.touch_group(PLATFORM, GROUP, "风车研究会")
    store.set_group_theme(PLATFORM, GROUP, "neon")
    assert store.group_name(PLATFORM, GROUP) == "风车研究会"
    store.touch_group(PLATFORM, GROUP, "风车研究会二群")
    assert store.group_theme(PLATFORM, GROUP) == "neon"


# -- 扫描游标 ---------------------------------------------------------------


def test_scan_state_defaults(store: PrismStore) -> None:
    assert store.get_scan_state(PLATFORM, GROUP) == {
        "oldest_seq": "",
        "newest_seq": "",
        "exhausted": False,
        "last_scan": 0,
        "cursor_field": "",
        "depth_pages": 0,
    }


def test_scan_state_partial_update_keeps_other_cursor(store: PrismStore) -> None:
    store.set_scan_state(PLATFORM, GROUP, oldest_seq="100", newest_seq="200")
    store.set_scan_state(PLATFORM, GROUP, oldest_seq="050")
    state = store.get_scan_state(PLATFORM, GROUP)
    assert state["oldest_seq"] == "050"
    assert state["newest_seq"] == "200"


def test_scan_state_exhausted_flag_can_be_cleared(store: PrismStore) -> None:
    store.set_scan_state(PLATFORM, GROUP, exhausted=True)
    assert store.get_scan_state(PLATFORM, GROUP)["exhausted"] is True
    store.set_scan_state(PLATFORM, GROUP, exhausted=False)
    assert store.get_scan_state(PLATFORM, GROUP)["exhausted"] is False


def test_scan_state_records_timestamp(store: PrismStore) -> None:
    store.set_scan_state(PLATFORM, GROUP, oldest_seq="1")
    assert store.get_scan_state(PLATFORM, GROUP)["last_scan"] > 0


def test_scan_state_remembers_cursor_field_and_depth(store: PrismStore) -> None:
    store.set_scan_state(PLATFORM, GROUP, oldest_seq="1", cursor_field="message_id", depth_pages=3)
    state = store.get_scan_state(PLATFORM, GROUP)
    assert state["cursor_field"] == "message_id"
    assert state["depth_pages"] == 3
    # 空串 / 负数表示「保留旧值」，方便只更新游标而不动记忆。
    store.set_scan_state(PLATFORM, GROUP, oldest_seq="0")
    state = store.get_scan_state(PLATFORM, GROUP)
    assert state["cursor_field"] == "message_id"
    assert state["depth_pages"] == 3


def test_reset_scan_state_scoped_and_global(store: PrismStore) -> None:
    store.set_scan_state(PLATFORM, GROUP, oldest_seq="1", exhausted=True)
    store.set_scan_state(PLATFORM, "800", oldest_seq="2", exhausted=True)
    assert store.reset_scan_state(PLATFORM, GROUP) == 1
    assert store.get_scan_state(PLATFORM, GROUP)["exhausted"] is False
    assert store.get_scan_state(PLATFORM, "800")["exhausted"] is True
    assert store.reset_scan_state() == 1
    assert store.get_scan_state(PLATFORM, "800")["exhausted"] is False


def test_legacy_db_without_cursor_field_unlocks_exhausted(tmp_path) -> None:
    """v1.1.2 误锁的旧库在升级打开时应当自动解锁，否则永远只能补拉最新一页。"""
    path = tmp_path / "legacy.db"
    seeded = PrismStore(path)
    try:
        seeded.set_scan_state(PLATFORM, GROUP, oldest_seq="123", newest_seq="456", exhausted=True)
    finally:
        seeded.close()
    # 把库还原成旧 schema：去掉 v1.1.3 新增的两列。
    raw = sqlite3.connect(path)
    try:
        raw.execute("ALTER TABLE scan_state DROP COLUMN cursor_field")
        raw.execute("ALTER TABLE scan_state DROP COLUMN depth_pages")
        raw.commit()
    finally:
        raw.close()

    upgraded = PrismStore(path)
    try:
        state = upgraded.get_scan_state(PLATFORM, GROUP)
        assert state["exhausted"] is False
        assert state["oldest_seq"] == ""
        assert state["depth_pages"] == 0
        # 最新一侧的游标要留着，避免被动采集的增量重复拉取。
        assert state["newest_seq"] == "456"
    finally:
        upgraded.close()


# -- 画像记录 ---------------------------------------------------------------


def test_save_portrait_returns_row_id_and_roundtrips(store: PrismStore) -> None:
    rid = store.save_portrait(_record())
    assert rid > 0
    record = store.get_record(rid)
    assert record is not None
    assert record.payload == {"headline": "很会修风车"}
    assert record.user_name == "阿狸"
    assert record.created_at > 0


def test_same_user_in_two_groups_is_not_overwritten(store: PrismStore) -> None:
    store.save_portrait(_record(group_id="700", payload={"headline": "A群形象"}))
    store.save_portrait(_record(group_id="800", payload={"headline": "B群形象"}))
    a = store.latest_portrait(PLATFORM, "700", USER)
    b = store.latest_portrait(PLATFORM, "800", USER)
    assert a is not None and b is not None
    assert a.payload["headline"] == "A群形象"
    assert b.payload["headline"] == "B群形象"


def test_history_limit_trims_oldest_of_that_scope_only(store: PrismStore) -> None:
    for i in range(5):
        store.save_portrait(
            _record(payload={"headline": "第" + str(i)}, created_at=1700000000 + i),
            history_limit=3,
        )
    store.save_portrait(_record(group_id="800"), history_limit=3)
    history = store.user_history(PLATFORM, GROUP, USER, limit=10)
    assert [h.payload["headline"] for h in history] == ["第4", "第3", "第2"]
    assert len(store.user_history(PLATFORM, "800", USER)) == 1


def test_history_limit_zero_keeps_everything(store: PrismStore) -> None:
    for i in range(4):
        store.save_portrait(_record(created_at=1700000000 + i), history_limit=0)
    assert len(store.user_history(PLATFORM, GROUP, USER, limit=99)) == 4


def test_latest_portrait_can_filter_by_kind(store: PrismStore) -> None:
    store.save_portrait(_record(kind="portrait", created_at=1700000000))
    store.save_portrait(_record(kind="roast", created_at=1700000100))
    assert store.latest_portrait(PLATFORM, GROUP, USER).kind == "roast"
    assert store.latest_portrait(PLATFORM, GROUP, USER, kind="portrait").kind == "portrait"
    assert store.latest_portrait(PLATFORM, GROUP, USER, kind="match") is None


def test_attach_card_updates_only_target_row(store: PrismStore) -> None:
    rid = store.save_portrait(_record())
    other = store.save_portrait(_record(created_at=1700000001))
    store.attach_card(rid, "card-1.jpg")
    assert store.get_record(rid).card_file == "card-1.jpg"
    assert store.get_record(other).card_file == ""


def test_list_records_pagination_is_newest_first(store: PrismStore) -> None:
    for i in range(5):
        store.save_portrait(
            _record(payload={"headline": "第" + str(i)}, created_at=1700000000 + i), history_limit=0
        )
    page1, total = store.list_records(limit=2, offset=0)
    page2, _ = store.list_records(limit=2, offset=2)
    assert total == 5
    assert [r.payload["headline"] for r in page1] == ["第4", "第3"]
    assert [r.payload["headline"] for r in page2] == ["第2", "第1"]


def test_list_records_filters_combine(store: PrismStore) -> None:
    store.save_portrait(_record(kind="portrait"), history_limit=0)
    store.save_portrait(_record(kind="roast"), history_limit=0)
    store.save_portrait(_record(group_id="800", kind="roast"), history_limit=0)
    _, total = store.list_records(group_id=GROUP, kind="roast")
    assert total == 1
    _, total = store.list_records(user_id=USER)
    assert total == 3
    _, total = store.list_records(user_id="ghost")
    assert total == 0


def test_list_records_query_matches_names_ids_and_text(store: PrismStore) -> None:
    store.save_portrait(_record(user_name="爱乃", text="喜欢风车"), history_limit=0)
    store.save_portrait(_record(user_id="20002", user_name="小明", text="喜欢摄影"), history_limit=0)
    assert store.list_records(query="爱乃")[1] == 1
    assert store.list_records(query="20002")[1] == 1
    assert store.list_records(query="摄影")[1] == 1
    assert store.list_records(query="风车研究会")[1] == 2
    assert store.list_records(query="不存在")[1] == 0


def test_list_records_clamps_limit(store: PrismStore) -> None:
    for i in range(3):
        store.save_portrait(_record(created_at=1700000000 + i), history_limit=0)
    assert len(store.list_records(limit=0)[0]) == 1
    assert len(store.list_records(limit=9999)[0]) == 3


def test_get_record_missing_returns_none(store: PrismStore) -> None:
    assert store.get_record(4242) is None


def test_delete_record_returns_card_file(store: PrismStore) -> None:
    rid = store.save_portrait(_record(card_file="card-9.jpg"))
    assert store.delete_record(rid) == "card-9.jpg"
    assert store.get_record(rid) is None
    assert store.delete_record(rid) == ""


def test_purge_records_scoped_returns_card_files(store: PrismStore) -> None:
    store.save_portrait(_record(card_file="a.jpg"), history_limit=0)
    store.save_portrait(_record(card_file="", created_at=1700000001), history_limit=0)
    store.save_portrait(_record(group_id="800", card_file="b.jpg"), history_limit=0)
    cards = store.purge_records(group_id=GROUP)
    assert cards == ["a.jpg"]
    assert store.list_records()[1] == 1


def test_purge_records_without_scope_wipes_all(store: PrismStore) -> None:
    store.save_portrait(_record(card_file="a.jpg"), history_limit=0)
    store.save_portrait(_record(group_id="800", card_file="b.jpg"), history_limit=0)
    assert sorted(store.purge_records()) == ["a.jpg", "b.jpg"]
    assert store.list_records()[1] == 0


# -- WebUI 聚合 -------------------------------------------------------------


def test_group_tree_nests_members_with_names(store: PrismStore) -> None:
    store.save_portrait(_record(created_at=1700000100), history_limit=0)
    store.save_portrait(
        _record(user_id="20002", user_name="小明", created_at=1700000200),
        history_limit=0,
    )
    store.save_portrait(
        _record(group_id="800", group_name="摄影群", created_at=1700000300),
        history_limit=0,
    )
    tree = store.group_tree()
    assert [g["group_id"] for g in tree] == ["800", "700"]
    windmill = next(g for g in tree if g["group_id"] == "700")
    assert windmill["group_name"] == "风车研究会"
    assert windmill["total"] == 2
    assert [m["user_name"] for m in windmill["members"]] == ["小明", "阿狸"]


def test_group_tree_falls_back_to_groups_meta_name(store: PrismStore) -> None:
    store.touch_group(PLATFORM, GROUP, "元信息里的群名")
    store.save_portrait(_record(group_name=""), history_limit=0)
    assert store.group_tree()[0]["group_name"] == "元信息里的群名"


def test_group_tree_labels_private_chat(store: PrismStore) -> None:
    store.save_portrait(_record(group_id="", group_name=""), history_limit=0)
    assert store.group_tree()[0]["group_name"] == "私聊"


def test_group_tree_member_falls_back_to_user_id(store: PrismStore) -> None:
    store.save_portrait(_record(user_name=""), history_limit=0)
    assert store.group_tree()[0]["members"][0]["user_name"] == USER


def test_overview_on_empty_database(store: PrismStore) -> None:
    data = store.overview()
    assert data["portraits"] == 0
    assert data["kinds"] == []
    assert data["runs_7d"] == 0
    assert data["success_rate"] == 1.0


def test_overview_counts_and_success_rate(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [_msg("1")])
    store.save_portrait(_record(kind="portrait"), history_limit=0)
    store.save_portrait(_record(kind="roast"), history_limit=0)
    store.save_portrait(_record(group_id="800", user_id="20002", kind="roast"), history_limit=0)
    store.add_optout(PLATFORM, GROUP, "30003")
    store.log_run(group_id=GROUP, user_id=USER, kind="portrait", ok=True, elapsed_ms=1000)
    store.log_run(group_id=GROUP, user_id=USER, kind="portrait", ok=False, elapsed_ms=3000)
    data = store.overview()
    assert data["portraits"] == 3
    assert data["groups"] == 2
    assert data["users"] == 2
    assert data["today"] == 3
    assert data["corpus"]["total"] == 1
    assert {k["kind"]: k["total"] for k in data["kinds"]} == {"portrait": 1, "roast": 2}
    assert data["optouts"] == 1
    assert data["runs_7d"] == 2
    assert data["success_rate"] == pytest.approx(0.5)
    assert data["avg_elapsed_ms"] == 2000


def test_recent_runs_is_newest_first_and_clamped(store: PrismStore) -> None:
    for i in range(3):
        store.log_run(group_id=GROUP, user_id=USER, kind="k" + str(i), ok=True, backend="t2i")
    runs = store.recent_runs(limit=0)
    assert len(runs) == 1
    assert runs[0]["kind"] == "k2"
    assert runs[0]["ok"] is True
    assert runs[0]["backend"] == "t2i"


def test_log_run_truncates_long_error(store: PrismStore) -> None:
    store.log_run(group_id=GROUP, user_id=USER, kind="portrait", ok=False, error="炸" * 900)
    assert len(store.recent_runs()[0]["error"]) == 400


# -- 退出名单 ---------------------------------------------------------------


def test_optout_lifecycle(store: PrismStore) -> None:
    assert store.is_opted_out(PLATFORM, GROUP, USER) is False
    store.add_optout(PLATFORM, GROUP, USER, "阿狸")
    assert store.is_opted_out(PLATFORM, GROUP, USER) is True
    assert store.remove_optout(PLATFORM, GROUP, USER) is True
    assert store.remove_optout(PLATFORM, GROUP, USER) is False


def test_optout_is_per_group(store: PrismStore) -> None:
    store.add_optout(PLATFORM, GROUP, USER)
    assert store.is_opted_out(PLATFORM, "800", USER) is False


def test_optout_upsert_updates_name_and_reason(store: PrismStore) -> None:
    store.add_optout(PLATFORM, GROUP, USER, "旧名", reason="self")
    store.add_optout(PLATFORM, GROUP, USER, "新名", reason="admin")
    entries = store.list_optouts()
    assert len(entries) == 1
    assert entries[0]["user_name"] == "新名"
    assert entries[0]["reason"] == "admin"


def test_list_optouts_falls_back_to_user_id(store: PrismStore) -> None:
    store.add_optout(PLATFORM, GROUP, USER)
    assert store.list_optouts()[0]["user_name"] == USER


# -- 配额 -------------------------------------------------------------------


def test_quota_counts_per_group_and_day(store: PrismStore) -> None:
    assert store.bump_quota(GROUP, "2026-08-27") == 1
    assert store.bump_quota(GROUP, "2026-08-27") == 2
    assert store.bump_quota("800", "2026-08-27") == 1
    assert store.quota_used(GROUP, "2026-08-27") == 2
    assert store.quota_used(GROUP, "2026-08-28") == 0


def test_bump_quota_evicts_previous_days(store: PrismStore) -> None:
    store.bump_quota(GROUP, "2026-08-26")
    store.bump_quota(GROUP, "2026-08-27")
    assert store.quota_used(GROUP, "2026-08-26") == 0


def test_release_quota_never_goes_negative(store: PrismStore) -> None:
    store.bump_quota(GROUP, "2026-08-27")
    store.release_quota(GROUP, "2026-08-27")
    store.release_quota(GROUP, "2026-08-27")
    assert store.quota_used(GROUP, "2026-08-27") == 0


# -- 自定义提示词 -----------------------------------------------------------


def test_prompt_entry_upsert_and_list(store: PrismStore) -> None:
    store.upsert_prompt_entry("mouth", command="棱镜嘴替", label="嘴替", prompt="帮他说人话")
    store.upsert_prompt_entry(
        "mouth",
        command="棱镜嘴替",
        label="嘴替 v2",
        prompt="帮他说人话，短一点",
        structured=False,
        enabled=False,
    )
    entries = store.list_prompt_entries()
    assert len(entries) == 1
    assert entries[0]["label"] == "嘴替 v2"
    assert entries[0]["structured"] is False
    assert entries[0]["enabled"] is False
    assert entries[0]["updated_at"] > 0


def test_prompt_entries_sorted_by_key(store: PrismStore) -> None:
    for key in ("zeta", "alpha", "mid"):
        store.upsert_prompt_entry(key, command="命令" + key, label=key, prompt="正文")
    assert [e["key"] for e in store.list_prompt_entries()] == ["alpha", "mid", "zeta"]


def test_delete_prompt_entry_reports_whether_it_existed(store: PrismStore) -> None:
    store.upsert_prompt_entry("mouth", command="棱镜嘴替", label="嘴替", prompt="正文")
    assert store.delete_prompt_entry("mouth") is True
    assert store.delete_prompt_entry("mouth") is False


# -- 异步代理 ---------------------------------------------------------------


def test_async_store_proxies_methods(store: PrismStore) -> None:
    api = AsyncStore(store)

    async def scenario() -> dict[str, int]:
        await api.add_messages(PLATFORM, GROUP, [_msg("1")])
        return await api.corpus_stats()

    assert asyncio.run(scenario())["total"] == 1


def test_async_store_exposes_non_callable_attributes(store: PrismStore) -> None:
    api = AsyncStore(store)
    assert api.sync is store
    assert api.path == store.path


def test_async_store_propagates_missing_attribute(store: PrismStore) -> None:
    with pytest.raises(AttributeError):
        _ = AsyncStore(store).no_such_method

def test_corpus_ts_health_and_repair_fix_millisecond_rows(store):
    store.add_messages(PLATFORM, GROUP, [_msg("1", ts=1700000000)])
    #: 模拟协议端给毫秒时间戳的历史行（老版本入库时没有归一化）。
    store.add_messages(PLATFORM, GROUP, [_msg("2", ts=1700000000123)])
    store.add_messages(PLATFORM, GROUP, [_msg("3", ts=0)])

    health = store.corpus_ts_health(PLATFORM, GROUP)
    assert health == {"total": 3, "missing": 1, "future": 1}

    assert store.repair_corpus_ts(PLATFORM, GROUP) == 1
    assert store.corpus_ts_health(PLATFORM, GROUP)["future"] == 0
    rows = store.window_rows(PLATFORM, GROUP, 1699999999, 1700000001)
    assert {row["message_id"] for row in rows} == {"1", "2"}


def test_repair_corpus_ts_leaves_other_groups_alone(store):
    store.add_messages(PLATFORM, GROUP, [_msg("1", ts=1700000000123)])
    store.add_messages(PLATFORM, "999", [_msg("2", ts=1700000000123)])
    assert store.repair_corpus_ts(PLATFORM, GROUP) == 1
    assert store.corpus_ts_health(PLATFORM, "999")["future"] == 1

def test_reopen_repairs_millisecond_ts_from_older_versions(tmp_path):
    path = tmp_path / "prism.db"
    db = PrismStore(path)
    try:
        db.add_messages(PLATFORM, GROUP, [_msg("1", ts=1700000000)])
        #: 绕过归一化，直接伪造 v1.2.0 之前入库的毫秒行。
        with db._lock:
            db._conn.execute("UPDATE corpus SET ts = 1700000000123")
            db._conn.commit()
    finally:
        db.close()

    again = PrismStore(path)
    try:
        assert again.corpus_ts_health(PLATFORM, GROUP)["future"] == 0
        assert again.window_rows(PLATFORM, GROUP, 1699999999, 1700000001)
    finally:
        again.close()
