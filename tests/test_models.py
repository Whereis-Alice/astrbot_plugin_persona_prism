"""数据模型测试：语料渲染、资料白名单、画像序列化、记录摘要。"""

from __future__ import annotations

import sqlite3

from astrbot_plugin_persona_prism.prism.models import (
    PROFILE_FIELD_LABELS,
    CorpusBundle,
    CorpusMessage,
    CorpusStats,
    Dimension,
    Evidence,
    MemberProfile,
    Portrait,
    PortraitRecord,
    Section,
    Tag,
)

# -- 语料 -------------------------------------------------------------------


def test_message_line_marks_reply_and_repeat() -> None:
    msg = CorpusMessage("1", "10001", "阿狸", "早上好", 1700000000, is_reply=True, repeat=3)
    line = msg.as_line(7)
    assert line.startswith("  7. [")
    assert "(回复他人) 早上好" in line
    assert "×3" in line


def test_message_line_without_timestamp_uses_placeholder() -> None:
    assert "[--]" in CorpusMessage("1", "10001", text="嗯").as_line()


def test_stats_prompt_block_hides_optional_lines_when_zero() -> None:
    block = CorpusStats(total=10, sampled=10, chars=40, avg_chars=4.0).to_prompt_block()
    assert "语料总量：10 条" in block
    assert "含表情" not in block
    assert "重复刷同一句话" not in block
    assert "活跃时段" not in block


def test_stats_prompt_block_shows_span_only_when_meaningful() -> None:
    short = CorpusStats(total=5, span_days=0.2, daily_rate=25.0).to_prompt_block()
    assert "时间跨度" not in short
    long = CorpusStats(total=5, span_days=9.0, daily_rate=0.5).to_prompt_block()
    assert "时间跨度约 9.0 天" in long


def test_stats_prompt_block_truncates_hot_hours_and_terms() -> None:
    block = CorpusStats(
        total=1,
        active_hours=[(h, 10 - h) for h in range(8)],
        top_terms=[(f"词{i}", 20 - i) for i in range(20)],
    ).to_prompt_block()
    assert block.count("点(") == 4
    assert "词12" not in block
    assert "词11" in block


def test_bundle_enough_and_transcript_numbering() -> None:
    empty = CorpusBundle()
    assert empty.enough is False
    assert empty.to_transcript() == ""
    bundle = CorpusBundle(
        messages=[
            CorpusMessage("1", "10001", text="第一句", ts=1700000000),
            CorpusMessage("2", "10001", text="第二句", ts=1700000060),
        ],
    )
    assert bundle.enough is True
    lines = bundle.to_transcript().splitlines()
    assert len(lines) == 2
    assert lines[0].strip().startswith("1.")
    assert lines[1].strip().startswith("2.")


# -- 成员资料 ---------------------------------------------------------------


def test_profile_field_labels_exclude_contact_information() -> None:
    for blocked in ("phone", "email", "address", "qid", "reg_time"):
        assert blocked not in PROFILE_FIELD_LABELS


def test_display_name_prefers_card_then_nickname_then_id() -> None:
    assert MemberProfile(user_id="1", nickname="昵称", card="名片").display_name == "名片"
    assert MemberProfile(user_id="1", nickname="昵称").display_name == "昵称"
    assert MemberProfile(user_id="1").display_name == "1"
    assert MemberProfile().display_name == "未知用户"


def test_profile_prompt_block_follows_allowed_order_and_skips_blanks() -> None:
    profile = MemberProfile(user_id="1", nickname="阿狸", card="", level="12")
    block = profile.to_prompt_block(["level", "card", "nickname", "ghost"])
    assert block.splitlines() == ["- 群等级：12", "- 昵称：阿狸"]


def test_profile_prompt_block_empty_when_nothing_allowed() -> None:
    assert MemberProfile(user_id="1", nickname="阿狸").to_prompt_block([]) == ""


# -- 画像 -------------------------------------------------------------------


def _portrait() -> Portrait:
    return Portrait(
        kind="portrait",
        headline="风车维修爱好者",
        tags=[Tag("动手派", "positive"), Tag("夜猫子")],
        dimensions=[Dimension("表达欲", 72, "长句多"), Dimension("互动性", 40)],
        sections=[Section("说话风格", "偏叙述，少反问")],
        evidence=[Evidence("今天把风车修好了", "体现动手倾向")],
        advice=["可以多参与话题发起"],
        confidence=0.7123,
    )


def test_portrait_dict_roundtrip_is_lossless() -> None:
    original = _portrait()
    restored = Portrait.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


def test_from_dict_drops_incomplete_items() -> None:
    portrait = Portrait.from_dict(
        {
            "tags": [{"label": ""}, {"polarity": "positive"}, {"label": "有效"}],
            "dimensions": [{"score": 10}, {"name": "有效", "score": 30}],
            "sections": [{}, {"title": "有效"}],
            "evidence": [{"reason": "缺原话"}, {"quote": "有效"}],
            "advice": ["  ", "有效"],
        },
    )
    assert [t.label for t in portrait.tags] == ["有效"]
    assert [d.name for d in portrait.dimensions] == ["有效"]
    assert [s.title for s in portrait.sections] == ["有效"]
    assert [e.quote for e in portrait.evidence] == ["有效"]
    assert portrait.advice == ["有效"]


def test_from_dict_tolerates_empty_payload() -> None:
    portrait = Portrait.from_dict({})
    assert portrait.kind == "portrait"
    assert portrait.confidence == 0.0
    assert portrait.structured is True


def test_to_plain_text_renders_all_blocks() -> None:
    text = _portrait().to_plain_text("【人格画像】阿狸")
    assert text.startswith("【人格画像】阿狸")
    assert "标签：动手派 / 夜猫子" in text
    assert "【说话风格】" in text
    assert "【原话依据】" in text
    assert "【建议】" in text
    assert "置信度 71%" in text


def test_to_plain_text_draws_bars_within_ten_cells() -> None:
    portrait = Portrait(dimensions=[Dimension("满分", 100), Dimension("零分", 0)])
    lines = [line for line in portrait.to_plain_text("标题").splitlines() if "█" in line or "░" in line]
    assert lines[0].count("█") == 10
    assert lines[1].count("░") == 10


def test_to_plain_text_uses_raw_text_when_unstructured() -> None:
    portrait = Portrait(raw_text="  他说话像在写日记。 ", structured=False)
    assert portrait.to_plain_text("标题") == "标题\n\n他说话像在写日记。"


# -- 记录 -------------------------------------------------------------------


def _record(**kwargs) -> PortraitRecord:
    base = {
        "id": 3,
        "platform": "aiocqhttp",
        "group_id": "700",
        "user_id": "10001",
        "kind": "portrait",
        "payload": {
            "headline": "锤" * 200,
            "tags": [{"label": f"标签{i}"} for i in range(9)],
        },
        "sample_size": 120,
        "confidence": 0.66666,
        "card_file": "abc.jpg",
        "created_at": 1700000000,
    }
    base.update(kwargs)
    return PortraitRecord(**base)


def test_summary_truncates_headline_and_tags() -> None:
    summary = _record().to_summary()
    assert len(summary["headline"]) == 160
    assert len(summary["tags"]) == 6
    assert summary["confidence"] == 0.667
    assert summary["has_card"] is True


def test_summary_falls_back_for_missing_names() -> None:
    summary = _record(group_name="", user_name="").to_summary()
    assert summary["group_name"] == "群 700"
    assert summary["user_name"] == "10001"
    assert summary["kind_label"] == "portrait"


def test_summary_marks_private_chat_when_no_group() -> None:
    assert _record(group_id="", group_name="").to_summary()["group_name"] == "私聊"


def test_detail_extends_summary() -> None:
    record = _record(group_name="风车研究会", text="正文", umo="aiocqhttp:GroupMessage:700")
    detail = record.to_detail()
    summary = record.to_summary()
    assert set(detail) == set(summary) | {"payload", "text", "umo"}
    assert detail["text"] == "正文"


def test_from_row_parses_sqlite_row_and_bad_json() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = (
        "id",
        "platform",
        "umo",
        "group_id",
        "group_name",
        "user_id",
        "user_name",
        "kind",
        "kind_label",
        "theme",
        "payload_json",
        "text",
        "sample_size",
        "corpus_chars",
        "confidence",
        "model",
        "card_file",
        "created_at",
    )
    conn.execute("CREATE TABLE t (" + ", ".join(columns) + ")")
    conn.execute(
        "INSERT INTO t VALUES (" + ", ".join("?" * len(columns)) + ")",
        (
            "5",
            "aiocqhttp",
            "umo",
            "700",
            "风车研究会",
            "10001",
            "阿狸",
            "praise",
            "群友赞赏",
            "ink",
            '{"headline": "很会修风车"}',
            "正文",
            "88",
            "512",
            "0.5",
            "gpt-x",
            "card.jpg",
            "1700000000",
        ),
    )
    conn.execute(
        "INSERT INTO t VALUES (" + ", ".join("?" * len(columns)) + ")",
        (
            6,
            "",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "{not json",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    rows = conn.execute("SELECT * FROM t ORDER BY CAST(id AS INTEGER)").fetchall()
    good = PortraitRecord.from_row(rows[0])
    assert good.id == 5
    assert good.sample_size == 88
    assert good.payload["headline"] == "很会修风车"
    broken = PortraitRecord.from_row(rows[1])
    assert broken.payload == {}
    assert broken.kind == "portrait"
    assert broken.created_at == 0
    conn.close()
