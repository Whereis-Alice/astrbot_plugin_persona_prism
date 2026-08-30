"""恋爱成分玩法测试：公式、归类、语料现算、notice 归一、卡片合并、持久层。"""

from __future__ import annotations

import sqlite3

import pytest
from astrbot_plugin_persona_prism.prism import love
from astrbot_plugin_persona_prism.prism.models import (
    CorpusMessage,
    Evidence,
    Portrait,
    Section,
    Tag,
    Utterance,
)
from astrbot_plugin_persona_prism.prism.store import PrismStore

PLATFORM = "aiocqhttp"
GROUP = "700"
DAY = "2026-08-30"


@pytest.fixture()
def store(tmp_path):
    db = PrismStore(tmp_path / "prism.db")
    try:
        yield db
    finally:
        db.close()


def _row(mid: str, uid: str, text: str = "在的", ts: int = 1700000000, **extra):
    row = {
        "message_id": mid,
        "user_id": uid,
        "user_name": "U" + uid,
        "text": text,
        "ts": ts,
        "is_reply": False,
        "reply_to": "",
        "images": 0,
        "at_ids": "",
    }
    row.update(extra)
    return row


# -- 归一化与权重 -----------------------------------------------------------


def test_normalize_clamps_and_is_monotonic() -> None:
    assert love.normalize(0) == 0
    assert love.normalize(-10) == 0
    assert love.normalize(10_000) == 100
    assert love.normalize(10) < love.normalize(50) < love.normalize(200)


def test_weights_from_sensitivity_uses_fifty_as_baseline() -> None:
    assert love.weights_from_sensitivity(50).slope == love.DEFAULT_SLOPE
    assert love.weights_from_sensitivity(100).slope > love.DEFAULT_SLOPE
    assert love.weights_from_sensitivity(1).slope < love.DEFAULT_SLOPE
    # 越界值要夹住，不能出现 0 或负斜率
    assert love.weights_from_sensitivity(0).slope > 0
    assert love.weights_from_sensitivity(9999).slope == love.weights_from_sensitivity(100).slope


def test_inputs_merge_sums_counters_but_keeps_max_partners() -> None:
    a = love.LoveInputs(msg_sent=3, text_len_total=30, partner_count=5)
    b = love.LoveInputs(msg_sent=2, text_len_total=10, partner_count=2)
    merged = a.merge(b)
    assert merged.msg_sent == 5
    assert merged.text_len_total == 40
    assert merged.partner_count == 5
    assert merged.avg_len == 8.0
    assert love.LoveInputs().avg_len == 0.0


# -- 四维公式 ---------------------------------------------------------------


def test_compute_metrics_all_zero_falls_back_to_neutral_total() -> None:
    metrics = love.compute_metrics(love.LoveInputs())
    assert metrics.total == 50
    assert (metrics.simp, metrics.vibe, metrics.ick, metrics.nostalgia) == (0, 0, 0, 0)
    assert metrics.archetype.key == "npc"


def test_compute_metrics_keeps_raw_and_scores_order() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=40, reply_received=10, recall_count=2))
    assert set(metrics.raw) == {"simp", "vibe", "ick", "nostalgia"}
    assert metrics.raw["simp"] == 40.0
    assert metrics.raw["vibe"] == 30.0
    assert metrics.raw["ick"] == 10.0
    assert metrics.scores() == [
        metrics.simp,
        metrics.vibe,
        metrics.nostalgia,
        metrics.ick,
        metrics.total,
    ]
    assert 0 <= metrics.total <= 100


def test_avg_len_is_not_double_counted_in_scores() -> None:
    """上游把平均长度既算进投入又算进下头，这里只作为展示项。"""
    short = love.compute_metrics(love.LoveInputs(msg_sent=10, text_len_total=20))
    long = love.compute_metrics(love.LoveInputs(msg_sent=10, text_len_total=2000))
    assert short.simp == long.simp
    assert short.ick == long.ick


def test_sensitivity_changes_slope_not_ranking() -> None:
    quiet = love.LoveInputs(msg_sent=5)
    loud = love.LoveInputs(msg_sent=60)
    for level in (1, 50, 100):
        weights = love.weights_from_sensitivity(level)
        a = love.compute_metrics(quiet, weights=weights)
        b = love.compute_metrics(loud, weights=weights)
        assert a.simp < b.simp


def test_trend_and_confidence() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=20), yesterday_total=30)
    assert metrics.trend == metrics.total - 30
    assert love.compute_metrics(love.LoveInputs(msg_sent=20)).trend is None
    thin = love.compute_metrics(love.LoveInputs(msg_sent=2))
    thick = love.compute_metrics(love.LoveInputs(msg_sent=80, reply_received=20))
    assert thin.confidence < thick.confidence <= 0.95


def test_yesterday_total_does_not_shift_today_score() -> None:
    """上游把昨日分回灌进今日公式，导致分数逐日漂移；这里只做趋势提示。"""
    inputs = love.LoveInputs(msg_sent=30, reply_received=8)
    assert love.compute_metrics(inputs).total == love.compute_metrics(inputs, yesterday_total=95).total


# -- 归类 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("simp", "vibe", "ick", "nostalgia", "expected"),
    [
        (60, 10, 70, 0, "the_ick"),
        (10, 60, 70, 0, "himbo"),
        (30, 10, 0, 80, "the_ex"),
        (80, 20, 0, 0, "the_simp"),
        (70, 70, 0, 0, "golden_retriever"),
        (40, 90, 0, 0, "the_charmer"),
        (30, 75, 0, 0, "the_player"),
        (10, 50, 0, 0, "idol"),
        (10, 10, 0, 0, "npc"),
        (50, 50, 0, 0, "normal"),
    ],
)
def test_classify_covers_every_archetype(simp, vibe, ick, nostalgia, expected) -> None:
    assert love.classify(simp, vibe, ick, nostalgia).key == expected


def test_archetype_table_is_complete() -> None:
    assert len(love.ARCHETYPES) == 10
    for key, item in love.ARCHETYPES.items():
        assert item.key == key
        assert item.label and item.tagline and item.reason
        assert item.tags and item.advice


# -- 从语料现算 -------------------------------------------------------------


def test_compute_day_inputs_counts_basics() -> None:
    rows = [
        _row("1", "A", "早", ts=1700000000),
        _row("2", "B", "早啊", ts=1700000100),
    ]
    stats = love.compute_day_inputs(rows)
    assert stats["A"].msg_sent == 1
    assert stats["A"].text_len_total == 1
    assert stats["B"].msg_sent == 1


def test_compute_day_inputs_resolves_reply_received() -> None:
    rows = [
        _row("1", "A", "有人在吗", ts=1700000000),
        _row("2", "B", "我在", ts=1700000060, is_reply=True, reply_to="1"),
    ]
    stats = love.compute_day_inputs(rows)
    assert stats["B"].reply_sent == 1
    assert stats["A"].reply_received == 1
    assert stats["A"].partner_count == 1
    assert stats["B"].partner_count == 1


def test_compute_day_inputs_counts_reply_without_known_target() -> None:
    rows = [_row("2", "B", "我在", ts=1700000060, is_reply=True, reply_to="missing")]
    stats = love.compute_day_inputs(rows)
    assert stats["B"].reply_sent == 1
    assert stats["B"].partner_count == 0


def test_compute_day_inputs_ignores_self_reply() -> None:
    rows = [
        _row("1", "A", "第一句", ts=1700000000),
        _row("2", "A", "补充一下", ts=1700000600, is_reply=True, reply_to="1"),
    ]
    stats = love.compute_day_inputs(rows)
    assert stats["A"].reply_received == 0
    assert stats["A"].reply_sent == 0


def test_compute_day_inputs_counts_at_both_sides() -> None:
    rows = [_row("1", "A", "@UB 来看", ts=1700000000, at_ids="B,A")]
    stats = love.compute_day_inputs(rows)
    assert stats["A"].at_sent == 1  # 艾特自己不计
    assert stats["B"].at_received == 1
    assert stats["A"].partner_count == 1


def test_compute_day_inputs_counts_topic_burst_night_and_images() -> None:
    base = 1700000000
    rows = [
        _row("1", "A", "开个话题", ts=base),
        _row("2", "A", "再补一句", ts=base + 5),
        _row("3", "A", "过很久又开一个", ts=base + 4000),
        _row("4", "A", "看图", ts=base + 4100, images=2),
    ]
    stats = love.compute_day_inputs(rows)
    assert stats["A"].topic_count == 2
    assert stats["A"].burst_count == 1
    assert stats["A"].image_sent == 2
    night = love.compute_day_inputs([_row("1", "A", "睡不着", ts=1700000000 - 6 * 3600)])
    assert night["A"].night_count == 1


def test_compute_day_inputs_counts_repeat_echo_longpost_and_emoji() -> None:
    base = 1700000000
    rows = [
        _row("1", "A", "哈哈哈", ts=base),
        _row("2", "B", "哈哈哈", ts=base + 60),
        _row("3", "A", "哈哈哈", ts=base + 120),
        _row("4", "B", "[表情]", ts=base + 180),
        _row("5", "A", "长" * love.LONGPOST_CHARS, ts=base + 240),
    ]
    stats = love.compute_day_inputs(rows)
    assert stats["B"].echo_count == 1
    assert stats["A"].repeat_count == 1
    assert stats["B"].emoji_only_count == 1
    assert stats["A"].longpost_count == 1


def test_compute_day_inputs_accepts_list_at_ids_and_skips_blank_users() -> None:
    rows = [
        _row("1", "A", "喊人", ts=1700000000, at_ids=["B", "", "C"]),
        _row("2", "", "幽灵消息", ts=1700000100),
    ]
    stats = love.compute_day_inputs(rows)
    assert "" not in stats
    assert stats["A"].at_sent == 2
    assert stats["A"].partner_count == 2


def test_collect_names_keeps_latest_nickname() -> None:
    rows = [_row("1", "A"), _row("2", "A"), _row("3", "B")]
    rows[1]["user_name"] = "改名了"
    assert love.collect_names(rows) == {"A": "改名了", "B": "UB"}


# -- notice 归一 ------------------------------------------------------------


def test_parse_notice_rejects_non_notice_payloads() -> None:
    assert love.parse_notice(None) is None
    assert love.parse_notice("poke") is None
    assert love.parse_notice({"post_type": "message"}) is None
    assert love.parse_notice({"post_type": "notice", "notice_type": "group_upload"}) is None


def test_parse_notice_reads_poke() -> None:
    parsed = love.parse_notice({
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "user_id": 111,
        "target_id": 222,
    })
    assert parsed == {"kind": "poke", "actor": "111", "target": "222", "message_id": "", "count": 1}


def test_parse_notice_drops_self_poke() -> None:
    assert love.parse_notice({
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "user_id": 111,
        "target_id": 111,
    }) is None


def test_parse_notice_sums_likes_array() -> None:
    """上游只按事件个数记 1，实际 likes 里可能一次带多个表情。"""
    parsed = love.parse_notice({
        "post_type": "notice",
        "notice_type": "group_msg_emoji_like",
        "user_id": 111,
        "message_id": 9001,
        "likes": [{"emoji_id": "76", "count": 3}, {"emoji_id": "66"}],
    })
    assert parsed["kind"] == "reaction"
    assert parsed["count"] == 4
    assert parsed["message_id"] == "9001"
    assert parsed["target"] == ""


def test_parse_notice_reaction_without_likes_counts_one() -> None:
    parsed = love.parse_notice({
        "post_type": "notice",
        "notice_type": "group_reaction",
        "user_id": 111,
        "message_id": "9002",
    })
    assert parsed["count"] == 1


def test_parse_notice_reaction_removal_is_negative() -> None:
    parsed = love.parse_notice({
        "post_type": "notice",
        "notice_type": "reaction",
        "sub_type": "remove",
        "user_id": 111,
        "message_id": "9003",
        "likes": [{"emoji_id": "76", "count": 2}],
    })
    assert parsed["count"] == -2


def test_parse_notice_reaction_requires_message_id() -> None:
    assert love.parse_notice({
        "post_type": "notice",
        "notice_type": "reaction",
        "user_id": 111,
    }) is None


def test_parse_notice_recall_only_counts_self_delete() -> None:
    own = love.parse_notice({
        "post_type": "notice",
        "notice_type": "group_recall",
        "user_id": 111,
        "operator_id": 111,
    })
    assert own == {"kind": "recall", "actor": "111", "target": "", "message_id": "", "count": 1}
    assert love.parse_notice({
        "post_type": "notice",
        "notice_type": "group_recall",
        "user_id": 111,
        "operator_id": 999,
    }) is None


def test_notice_fields_map_to_store_columns() -> None:
    for sent, received in love.NOTICE_FIELDS.values():
        assert sent in PrismStore.INTERACTION_FIELDS
        assert received in PrismStore.INTERACTION_FIELDS


# -- 文案与卡片 -------------------------------------------------------------


def test_total_label_grades() -> None:
    assert love.total_label(0) == "冰点"
    assert love.total_label(50) == "常温"
    assert love.total_label(100) == "沸腾"


def test_trend_text_thresholds() -> None:
    inputs = love.LoveInputs(msg_sent=20, reply_received=6)
    metrics = love.compute_metrics(inputs)
    metrics.yesterday_total = metrics.total
    assert love.trend_text(metrics) == "和昨天基本持平"
    metrics.yesterday_total = metrics.total - 20
    assert "升温" in love.trend_text(metrics)
    metrics.yesterday_total = metrics.total + 20
    assert "降温" in love.trend_text(metrics)
    metrics.yesterday_total = None
    assert love.trend_text(metrics) == ""


def test_breakdown_lines_are_readable() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=4, text_len_total=40, reply_sent=2))
    lines = love.breakdown_lines(metrics)
    assert len(lines) == 5
    assert "发言 4" in lines[0]
    assert lines[1].endswith("无")
    assert "平均每条 10 字" in lines[-1]


def test_love_dimensions_match_names_and_scores() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=12, reply_received=4, recall_count=1))
    dims = love.love_dimensions(metrics)
    assert [d.name for d in dims] == list(love.LOVE_DIMENSION_NAMES)
    assert [d.score for d in dims] == metrics.scores()


def test_fallback_portrait_is_self_contained() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=30, reply_received=2))
    card = love.fallback_portrait(metrics, target_name="阿狸")
    assert card.kind == "love"
    assert card.structured is True
    assert card.headline == love.headline_of(metrics)
    assert [t.label for t in card.tags] == list(metrics.archetype.tags)
    assert card.advice
    titles = [s.title for s in card.sections]
    assert titles == ["判词", "行为诊断", "成分拆解"]
    assert "阿狸" in card.sections[0].body
    assert card.equation.startswith("L(")
    assert [t.code for t in card.glossary] == ["S", "V", "N", "I"]


def test_merge_portrait_without_llm_returns_formula_card() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    assert love.merge_portrait(metrics, None).to_dict() == love.fallback_portrait(metrics).to_dict()


def test_merge_portrait_uses_raw_text_when_model_output_unstructured() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    llm = Portrait(structured=False, raw_text="今天有点上头")
    card = love.merge_portrait(metrics, llm, target_name="阿狸")
    assert card.sections[0].body == "今天有点上头"
    assert [d.score for d in card.dimensions] == metrics.scores()


def test_merge_portrait_falls_back_when_raw_text_empty() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    card = love.merge_portrait(metrics, Portrait(structured=False, raw_text="   "))
    assert card.sections[0].body == love.fallback_portrait(metrics).sections[0].body


def test_merge_portrait_keeps_formula_scores_over_model_numbers() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=30, reply_received=9), yesterday_total=10)
    llm = Portrait(
        kind="love",
        headline="模型写的标题",
        tags=[Tag("嘴甜", "positive")],
        sections=[Section("判词", "模型的判词")],
        advice=["少发点表情"],
        structured=True,
    )
    card = love.merge_portrait(metrics, llm, target_name="阿狸")
    assert card.headline == "模型写的标题"
    assert [t.label for t in card.tags] == ["嘴甜"]
    assert [d.score for d in card.dimensions] == metrics.scores()
    titles = [s.title for s in card.sections]
    assert "成分拆解" in titles
    assert titles[-1] == "趋势"
    assert card.advice == ["少发点表情"]
    assert card.confidence == metrics.confidence


def test_merge_portrait_does_not_duplicate_breakdown() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    llm = Portrait(sections=[Section("成分拆解", "模型自己写的")], structured=True)
    card = love.merge_portrait(metrics, llm)
    assert [s.title for s in card.sections].count("成分拆解") == 1


def test_metrics_prompt_block_locks_numbers() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=15, reply_received=3), yesterday_total=20)
    block = love.metrics_prompt_block(metrics, target_name="阿狸")
    assert "阿狸" in block
    assert "请勿改动数字" in block
    assert str(metrics.total) in block
    assert metrics.archetype.label in block
    assert "趋势" in block


# -- 窗口口径与现场证供 -----------------------------------------------------


def test_span_label_reads_naturally() -> None:
    assert love.span_label(0) == "当日"
    assert love.span_label(1) == "当日"
    assert love.span_label(7) == "近 7 天"


def test_compute_metrics_multi_day_uses_daily_average() -> None:
    one_day = love.compute_metrics(love.LoveInputs(msg_sent=14))
    seven_days = love.compute_metrics(love.LoveInputs(msg_sent=98), days=7)
    # 7 天里发 98 条 = 每天 14 条，跟单日 14 条应当同档，不能因为基数大就人人沸腾。
    assert seven_days.days == 7
    assert abs(seven_days.simp - one_day.simp) <= 1


def test_evolution_equation_shows_the_span_and_result() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=42, reply_received=7), days=7)
    text = love.evolution_equation(metrics)
    assert text.startswith("L(近 7 天日均)")
    assert f"{metrics.total}%" in text
    assert love.total_label(metrics.total) in text


def test_evolution_equation_marks_empty_sample() -> None:
    metrics = love.compute_metrics(love.LoveInputs())
    assert "样本为空" in love.evolution_equation(metrics)


ME = "1001"
T0 = 1700000000  # 本地时间落在白天，不会被判成深夜时段


def test_build_scenes_wraps_the_quote_with_real_neighbours() -> None:
    rows = [
        _row("1", "2002", "你在吗", T0),
        _row("2", ME, "在的，刚回来", T0 + 5),
        _row("3", "2003", "带我一个", T0 + 9),
    ]
    scenes = love.build_scenes(rows, ME, names={"2002": "阿狸"})
    assert len(scenes) == 1
    scene = scenes[0]
    assert scene.quote == "在的，刚回来"
    assert [u.text for u in scene.dialogue] == ["你在吗", "在的，刚回来", "带我一个"]
    assert [u.mine for u in scene.dialogue] == [False, True, False]
    assert [u.speaker for u in scene.dialogue] == ["阿狸", "U1001", "U2003"]
    assert scene.title.endswith("日常片段")
    assert scene.reason


def test_build_scenes_labels_reply_and_topic() -> None:
    reply = love.build_scenes(
        [_row("1", "2002", "谁去修风车", T0), _row("2", ME, "我去", T0 + 5, reply_to="1")],
        ME,
    )
    assert reply[0].title.endswith("接话现场")
    topic = love.build_scenes(
        [_row("1", "2002", "……", T0), _row("2", ME, "话说今晚有流星雨", T0 + 4000)],
        ME,
    )
    assert topic[0].title.endswith("冷场破冰")


def test_build_scenes_prefers_flagged_moments() -> None:
    rows = [
        _row("1", "2002", "嗯", T0),
        _row("2", ME, "嗯", T0 + 5),
        _row("3", "2002", "哦", T0 + 9),
        _row("4", "2003", "谁来搭把手", T0 + 14),
        _row("5", ME, "我来", T0 + 18, reply_to="4"),
        _row("6", "2003", "谢了", T0 + 22),
    ]
    scenes = love.build_scenes(rows, ME, limit=1)
    assert len(scenes) == 1
    assert scenes[0].quote == "我来"


def test_build_scenes_skips_empty_and_symbol_only_lines() -> None:
    rows = [
        _row("1", "2002", "在吗", T0),
        _row("2", ME, "。。。", T0 + 5),
        _row("3", ME, "", T0 + 9),
    ]
    assert love.build_scenes(rows, ME) == []


def test_build_scenes_needs_rows_and_a_target() -> None:
    assert love.build_scenes([], ME) == []
    assert love.build_scenes([_row("1", ME, "在的", T0)], "") == []


def test_build_scenes_respects_limit_and_keeps_order() -> None:
    rows = []
    for step in range(6):
        base = T0 + step * 60
        rows.append(_row(f"a{step}", "2002", "问题" + str(step), base))
        rows.append(_row(f"b{step}", ME, "回答" + str(step), base + 5, reply_to=f"a{step}"))
    scenes = love.build_scenes(rows, ME, limit=3)
    assert len(scenes) == 3
    quotes = [s.quote for s in scenes]
    assert quotes == sorted(quotes, key=lambda q: int(q[-1]))


def test_fallback_portrait_carries_scenes_and_sample_note() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=8), days=7)
    scenes = [Evidence(quote="在的", reason="随手回一句", title="12:00 · 日常片段")]
    card = love.fallback_portrait(metrics, scenes=scenes, sample_note="取证范围：近 7 天本群 80 条")
    assert card.evidence[0].quote == "在的"
    assert card.sample_note.startswith("取证范围")


def test_merge_portrait_uses_local_scenes_when_model_gives_none() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=8))
    scenes = [Evidence(quote="在的", title="12:00 · 日常片段")]
    llm = Portrait(sections=[Section("判词", "模型的判词")], structured=True)
    card = love.merge_portrait(metrics, llm, scenes=scenes, sample_note="样本说明")
    assert [e.quote for e in card.evidence] == ["在的"]
    assert card.sample_note == "样本说明"


def test_merge_portrait_keeps_verified_model_evidence_over_local_scenes() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=8))
    picked = Evidence(
        quote="模型挑的那句",
        dialogue=[Utterance(speaker="阿狸", text="模型挑的那句", mine=True)],
        verified=True,
    )
    llm = Portrait(evidence=[picked], structured=True)
    card = love.merge_portrait(metrics, llm, scenes=[Evidence(quote="本地裁的")])
    assert card.evidence[0].quote == "模型挑的那句"


def test_merge_portrait_drops_unverified_model_evidence() -> None:
    """模型自己编的气泡对不回本地记录时不能上卡——那种条目没有头像也没有时刻。"""
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=8))
    llm = Portrait(evidence=[Evidence(quote="模型挑的那句")], structured=True)
    card = love.merge_portrait(metrics, llm, scenes=[Evidence(quote="本地裁的")])
    assert [e.quote for e in card.evidence] == ["本地裁的"]

# -- 持久层 -----------------------------------------------------------------


def test_corpus_roundtrips_interaction_columns(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [
        CorpusMessage("1", "A", "UA", "问一句", 1700000000),
        CorpusMessage("2", "B", "UB", "答一句", 1700000060, True, 1, "1", 2, "A"),
    ])
    rows = store.window_rows(PLATFORM, GROUP, 1600000000, 1800000000)
    assert [r["message_id"] for r in rows] == ["1", "2"]
    assert rows[1]["reply_to"] == "1"
    assert rows[1]["images"] == 2
    assert rows[1]["at_ids"] == "A"
    mine = store.fetch_user_corpus(PLATFORM, GROUP, "B")
    assert mine[0]["reply_to"] == "1"


def test_window_rows_filters_by_timestamp(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [
        CorpusMessage("1", "A", "UA", "旧", 1000),
        CorpusMessage("2", "A", "UA", "新", 5000),
    ])
    rows = store.window_rows(PLATFORM, GROUP, 2000, 6000)
    assert [r["message_id"] for r in rows] == ["2"]
    assert store.window_rows(PLATFORM, GROUP, 2000, 6000, limit=0) == rows


def test_message_owner_reads_from_corpus(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [CorpusMessage("1", "A", "UA", "问一句", 1700000000)])
    assert store.message_owner(PLATFORM, GROUP, "1") == "A"
    assert store.message_owner(PLATFORM, GROUP, "404") == ""
    assert store.message_owner(PLATFORM, GROUP, "") == ""


def test_bump_interaction_accumulates(store: PrismStore) -> None:
    store.bump_interaction(PLATFORM, GROUP, "A", DAY, "poke_sent")
    store.bump_interaction(PLATFORM, GROUP, "A", DAY, "poke_sent", 2)
    store.bump_interaction(PLATFORM, GROUP, "A", DAY, "reaction_received", -1)
    counts = store.interaction_counts(PLATFORM, GROUP, DAY)
    assert counts["A"]["poke_sent"] == 3
    assert counts["A"]["reaction_received"] == -1
    assert counts["A"]["recall_count"] == 0


def test_bump_interaction_ignores_noop_and_rejects_unknown_field(store: PrismStore) -> None:
    store.bump_interaction(PLATFORM, GROUP, "A", DAY, "poke_sent", 0)
    store.bump_interaction(PLATFORM, "", "A", DAY, "poke_sent")
    assert store.interaction_counts(PLATFORM, GROUP, DAY) == {}
    with pytest.raises(ValueError, match="unknown interaction field"):
        store.bump_interaction(PLATFORM, GROUP, "A", DAY, "love_total")


def test_love_total_defaults_to_none_and_survives_bump(store: PrismStore) -> None:
    assert store.love_total(PLATFORM, GROUP, "A", DAY) is None
    store.bump_interaction(PLATFORM, GROUP, "A", DAY, "poke_sent")
    assert store.love_total(PLATFORM, GROUP, "A", DAY) is None
    store.set_love_total(PLATFORM, GROUP, "A", DAY, 66)
    assert store.love_total(PLATFORM, GROUP, "A", DAY) == 66
    store.bump_interaction(PLATFORM, GROUP, "A", DAY, "poke_sent")
    assert store.love_total(PLATFORM, GROUP, "A", DAY) == 66


def test_prune_interactions_drops_old_days(store: PrismStore) -> None:
    store.bump_interaction(PLATFORM, GROUP, "A", "2000-01-01", "poke_sent")
    store.bump_interaction(PLATFORM, GROUP, "B", "2999-01-01", "poke_sent")
    assert store.prune_interactions(retention_days=0) == 0
    assert store.prune_interactions(retention_days=30) == 1
    remaining = store.interaction_counts(PLATFORM, GROUP, "2999-01-01")
    assert set(remaining) == {"B"}


def test_clear_user_corpus_also_clears_interactions(store: PrismStore) -> None:
    store.add_messages(PLATFORM, GROUP, [CorpusMessage("1", "A", "UA", "问一句", 1700000000)])
    store.bump_interaction(PLATFORM, GROUP, "A", DAY, "poke_sent")
    store.clear_user_corpus(PLATFORM, GROUP, "A")
    assert store.interaction_counts(PLATFORM, GROUP, DAY) == {}


def test_opted_out_ids_lists_group_members(store: PrismStore) -> None:
    store.add_optout(PLATFORM, GROUP, "A", "UA")
    store.add_optout(PLATFORM, "800", "B", "UB")
    assert store.opted_out_ids(PLATFORM, GROUP) == ["A"]
    store.remove_optout(PLATFORM, GROUP, "A")
    assert store.opted_out_ids(PLATFORM, GROUP) == []


def test_legacy_corpus_without_interaction_columns_is_migrated(tmp_path) -> None:
    """v1.1.x 的旧库要能直接升级到带 reply_to/images/at_ids 的新 schema。"""
    path = tmp_path / "legacy.db"
    seeded = PrismStore(path)
    try:
        seeded.add_messages(PLATFORM, GROUP, [CorpusMessage("1", "A", "UA", "老语料", 1700000000)])
    finally:
        seeded.close()
    raw = sqlite3.connect(path)
    try:
        for column in ("reply_to", "images", "at_ids"):
            raw.execute(f"ALTER TABLE corpus DROP COLUMN {column}")
        raw.execute("DROP TABLE interactions")
        raw.commit()
    finally:
        raw.close()

    upgraded = PrismStore(path)
    try:
        rows = upgraded.window_rows(PLATFORM, GROUP, 0, 1800000000)
        assert [r["message_id"] for r in rows] == ["1"]
        assert rows[0]["reply_to"] == ""
        assert rows[0]["images"] == 0
        upgraded.bump_interaction(PLATFORM, GROUP, "A", DAY, "poke_sent")
        assert upgraded.interaction_counts(PLATFORM, GROUP, DAY)["A"]["poke_sent"] == 1
    finally:
        upgraded.close()


def test_day_inputs_from_store_rows_end_to_end(store: PrismStore) -> None:
    """采集 → 入库 → 现算：装上插件当天就该有分数，不必从零攒。"""
    base = 1700000000
    store.add_messages(PLATFORM, GROUP, [
        CorpusMessage("1", "A", "阿狸", "今天风好大", base),
        CorpusMessage("2", "B", "小北", "确实", base + 30, True, 1, "1"),
        CorpusMessage("3", "C", "阿宅", "@阿狸 你在吗", base + 90, False, 1, "", 0, "A"),
    ])
    rows = store.window_rows(PLATFORM, GROUP, base - 10, base + 1000)
    stats = love.compute_day_inputs(rows)
    assert stats["A"].reply_received == 1
    assert stats["A"].at_received == 1
    assert stats["A"].partner_count == 2
    metrics = love.compute_metrics(stats["A"], weights=love.weights_from_sensitivity(50))
    assert metrics.vibe > 0
    assert love.collect_names(rows)["A"] == "阿狸"


# ---------------------------------------------------------------------------
# 专属头衔
# ---------------------------------------------------------------------------


def test_fallback_portrait_carries_a_formula_title() -> None:
    from astrbot_plugin_persona_prism.prism import titles as prism_titles

    metrics = love.compute_metrics(love.LoveInputs(msg_sent=40, text_len_total=800))
    card = love.fallback_portrait(metrics, target_name="阿狸", seed="s")
    assert card.title == prism_titles.love_title(metrics, seed="s")
    assert card.title


def test_merge_portrait_prefers_the_model_title() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    llm = Portrait(structured=True, headline="上头了", title="头衔：夜聊冠军")
    card = love.merge_portrait(metrics, llm, seed="s")
    assert card.title == "夜聊冠军"


def test_merge_portrait_falls_back_when_model_title_is_a_sentence() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    llm = Portrait(structured=True, headline="上头了", title="这个人今天特别热情，主动搭话很多次。")
    card = love.merge_portrait(metrics, llm, seed="s")
    assert card.title == love.fallback_portrait(metrics, seed="s").title


def test_merge_portrait_keeps_formula_title_for_unstructured_model_output() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    card = love.merge_portrait(metrics, Portrait(structured=False, raw_text="上头"), seed="s")
    assert card.title == love.fallback_portrait(metrics, seed="s").title


def test_merge_portrait_carries_the_persona_and_type_code() -> None:
    # 恋爱卡的署名与人格徽章只能从模型那边来，公式版给不出，丢了卡片上就是空的。
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    llm = Portrait(structured=True, headline="上头了", persona="爱丽丝", type_code="ENTP")
    card = love.merge_portrait(metrics, llm, seed="s")
    assert card.persona == "爱丽丝"
    assert card.type_code == "ENTP"


def test_persona_survives_unstructured_model_output() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    llm = Portrait(structured=False, raw_text="上头", persona="爱丽丝", type_code="INFP")
    card = love.merge_portrait(metrics, llm, seed="s")
    assert (card.persona, card.type_code) == ("爱丽丝", "INFP")


def test_formula_only_card_has_no_persona() -> None:
    metrics = love.compute_metrics(love.LoveInputs(msg_sent=10))
    card = love.merge_portrait(metrics, None, seed="s")
    assert card.persona == ""
    assert card.type_code == ""
