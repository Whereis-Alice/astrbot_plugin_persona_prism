"""collector 层：OneBot 解析、清洗去重、分层抽样、统计锚点。

这一层是画像准确性的地基，所以测得比别处细。
"""

from __future__ import annotations

from astrbot_plugin_persona_prism.prism import collector
from astrbot_plugin_persona_prism.prism.models import CorpusMessage


def _msg(mid, text, ts, **kw):
    return CorpusMessage(message_id=str(mid), user_id="1", text=text, ts=ts, **kw)


# ---------------------------------------------------------------------------
# OneBot 解析
# ---------------------------------------------------------------------------


def test_parse_onebot_segments_keeps_mentions_and_marks_reply():
    segs = [
        {"type": "reply", "data": {"id": "1"}},
        {"type": "at", "data": {"qq": "10001", "name": "小明"}},
        {"type": "text", "data": {"text": " 说得对"}},
        {"type": "image", "data": {}},
    ]
    text, is_reply = collector.parse_onebot_segments(segs)
    assert is_reply is True
    assert "@小明" in text
    assert "[图片]" in text


def test_parse_onebot_segments_handles_at_all_and_junk():
    text, is_reply = collector.parse_onebot_segments(
        [
            {"type": "at", "data": {"qq": "all"}},
            "not-a-dict",
            {"type": "face", "data": {}},
            {"type": "record", "data": {}},
            {"type": "video", "data": {}},
        ],
    )
    assert "@全体成员" in text
    assert "[表情]" in text
    assert "[语音]" in text
    assert "[视频]" in text
    assert is_reply is False


def test_parse_onebot_segments_falls_back_to_qq_when_name_missing():
    text, _ = collector.parse_onebot_segments([{"type": "at", "data": {"qq": "10001"}}])
    assert text == "@10001"


def test_parse_onebot_segments_empty():
    assert collector.parse_onebot_segments(None) == ("", False)


def test_parse_history_page_skips_rows_without_message_id():
    page = [
        {
            "message_id": 101,
            "time": 1700000000,
            "sender": {"user_id": 5, "card": "阿伟"},
            "message": [{"type": "text", "data": {"text": "在的"}}],
        },
        {"time": 1700000001, "message": []},
        "junk",
    ]
    rows = collector.parse_history_page(page)
    assert len(rows) == 1
    assert rows[0]["message_id"] == "101"
    assert rows[0]["user_id"] == "5"
    assert rows[0]["user_name"] == "阿伟"
    assert rows[0]["ts"] == 1700000000


def test_parse_history_page_prefers_card_over_nickname():
    rows = collector.parse_history_page(
        [{"message_id": "1", "sender": {"user_id": "2", "card": "群名片", "nickname": "昵称"}}],
    )
    assert rows[0]["user_name"] == "群名片"


# ---------------------------------------------------------------------------
# 清洗 / 去重 / 排序
# ---------------------------------------------------------------------------


def test_clean_rows_dedupes_sorts_and_filters():
    rows = [
        {"message_id": "3", "user_id": "1", "text": "晚上一起打球吗", "ts": 300},
        {"message_id": "1", "user_id": "1", "text": "早上好呀各位", "ts": 100},
        {"message_id": "1", "user_id": "1", "text": "早上好呀各位", "ts": 100},
        {"message_id": "2", "user_id": "1", "text": "/help", "ts": 200},
        {"message_id": "4", "user_id": "1", "text": "。。。", "ts": 400},
    ]
    cleaned = collector.clean_rows(rows)
    assert [m.message_id for m in cleaned] == ["1", "3"]


def test_clean_rows_can_keep_commands():
    rows = [{"message_id": "1", "user_id": "1", "text": "/help 一下嘛", "ts": 1}]
    assert collector.clean_rows(rows, filter_commands=False)
    assert collector.clean_rows(rows) == []


def test_clean_rows_carries_reply_flag_and_name():
    cleaned = collector.clean_rows(
        [
            {
                "message_id": "1",
                "user_id": "9",
                "user_name": "阿伟",
                "text": "确实如此",
                "ts": 5,
                "is_reply": True,
            }
        ],
    )
    assert cleaned[0].is_reply is True
    assert cleaned[0].user_name == "阿伟"


def test_fold_repeats_marks_repeat_count():
    msgs = [_msg(i, "草草草草", i) for i in range(1, 4)] + [_msg(9, "今天天气真好", 9)]
    folded = collector.fold_repeats(msgs)
    assert len(folded) == 2
    counts = {m.text: m.repeat for m in folded}
    assert counts["草草草草"] == 3
    assert counts["今天天气真好"] == 1


# ---------------------------------------------------------------------------
# 抽样
# ---------------------------------------------------------------------------


def test_layered_sample_keeps_budget_and_time_order():
    msgs = [_msg(i, f"第 {i} 条发言内容", i * 60) for i in range(1, 101)]
    picked = collector.layered_sample(msgs, 20)
    assert len(picked) == 20
    assert picked == sorted(picked, key=lambda m: (m.ts, m.message_id))
    # 最新一条必须在样本里：近期发言最能代表「现在的这个人」。
    assert picked[-1].message_id == "100"
    # 同时也要覆盖到早期，否则长期习惯就丢了。
    assert picked[0].ts < msgs[len(msgs) // 2].ts


def test_layered_sample_passthrough_when_under_budget():
    msgs = [_msg(i, f"短句子 {i}", i) for i in range(1, 6)]
    assert collector.layered_sample(msgs, 50) == msgs
    assert collector.layered_sample(msgs, 0) == msgs


# ---------------------------------------------------------------------------
# 高频词
# ---------------------------------------------------------------------------


def test_extract_terms_drops_stopwords():
    terms = dict(collector.extract_terms(["我觉得可以的", "我觉得可以的"]))
    assert "觉得" not in terms
    assert "可以" not in terms


def test_extract_terms_drops_shifted_bigrams():
    terms = dict(collector.extract_terms(["打游戏打游戏"] * 3))
    assert "游戏" in terms
    # 「戏打」是 bigram 错位切出来的碎片，不能出现在高频词里。
    assert "戏打" not in terms


def test_extract_terms_needs_at_least_two_hits():
    assert collector.extract_terms(["摄影后期调色"]) == []


def test_extract_terms_handles_latin():
    terms = dict(collector.extract_terms(["rust is fun", "rust again"]))
    assert terms.get("rust") == 2
    assert "the" not in terms


def test_stopwords_are_single_chars_for_cjk():
    """回归测试：曾经把整行中文字符串 split() 当停用词，中文没空格 ⇒ 全部失效。"""
    for word in collector.STOPWORDS:
        if not word.isascii():
            assert len(word) == 1, word


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def test_compute_stats_reports_facts():
    msgs = [
        _msg(1, "这是什么情况？", 1700000000, is_reply=True),
        _msg(2, "@小明 你来解释一下", 1700086400),
        _msg(3, "我先去吃饭了", 1700172800, repeat=3),
    ]
    stats = collector.compute_stats(msgs, total=90)
    assert stats.total == 90
    assert stats.sampled == 3
    assert stats.chars == sum(len(m.text) for m in msgs)
    assert stats.question_ratio > 0
    assert stats.mention_ratio > 0
    assert 0 < stats.reply_ratio < 1
    assert stats.repeat_ratio > 0
    assert stats.span_days > 1.9
    assert stats.active_hours
    assert stats.to_prompt_block()


def test_compute_stats_empty_keeps_total():
    stats = collector.compute_stats([], total=7)
    assert stats.total == 7
    assert stats.sampled == 0


def test_extract_partners_ignores_at_all():
    msgs = [
        _msg(1, "@小明 来一把", 1),
        _msg(2, "@小明 快点", 2),
        _msg(3, "@全体成员 通知一下", 3),
    ]
    partners = dict(collector.extract_partners(msgs))
    assert partners.get("小明") == 2
    assert "全体成员" not in partners


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


def test_build_bundle_end_to_end():
    rows = [
        {
            "message_id": str(i),
            "user_id": "1",
            "user_name": "阿伟",
            "text": f"第 {i} 次聊到了摄影和后期",
            "ts": 1700000000 + i * 600,
        }
        for i in range(1, 60)
    ]
    rows.append({"message_id": "900", "user_id": "1", "text": "/help", "ts": 1700100000})
    bundle = collector.build_bundle(rows, max_messages=30, scanned=120)
    assert len(bundle.messages) == 30
    assert bundle.stats.total == 59
    assert bundle.scanned == 120
    assert bundle.from_cache is False
    assert bundle.enough is True
    assert bundle.to_transcript()


def test_build_bundle_recent_sampling_takes_tail():
    rows = [
        {"message_id": str(i), "user_id": "1", "text": f"随便说点什么 {i}", "ts": i} for i in range(1, 21)
    ]
    bundle = collector.build_bundle(rows, max_messages=5, sampling="recent")
    assert [m.message_id for m in bundle.messages] == ["16", "17", "18", "19", "20"]


def test_build_bundle_truncates_long_quotes():
    rows = [{"message_id": "1", "user_id": "1", "text": "长" * 500, "ts": 1}]
    bundle = collector.build_bundle(rows, quote_limit=50)
    assert len(bundle.messages[0].text) == 51


def test_build_bundle_scanned_falls_back_to_total():
    rows = [{"message_id": "1", "user_id": "1", "text": "就这一条有效发言", "ts": 1}]
    bundle = collector.build_bundle(rows)
    assert bundle.scanned == 1

# ---------------------------------------------------------------------------
# 时间戳归一化（协议端单位/字段名不统一）
# ---------------------------------------------------------------------------


def test_to_epoch_seconds_normalizes_units():
    assert collector.to_epoch_seconds(1700000000) == 1700000000
    assert collector.to_epoch_seconds(1700000000123) == 1700000000  # 毫秒
    assert collector.to_epoch_seconds(1700000000123456) == 1700000000  # 微秒
    assert collector.to_epoch_seconds(1700000000123456789) == 1700000000  # 纳秒
    assert collector.to_epoch_seconds("1700000000") == 1700000000
    assert collector.to_epoch_seconds("1700000000.5") == 1700000000


def test_to_epoch_seconds_rejects_garbage():
    for bad in (None, "", "  ", "abc", 0, -5, True, False, {}):
        assert collector.to_epoch_seconds(bad) == 0


def test_pick_epoch_falls_back_to_alias_keys():
    assert collector.pick_epoch({"time": 0, "msgTime": 1700000000000}) == 1700000000
    assert collector.pick_epoch({"timestamp": "1700000000"}) == 1700000000
    assert collector.pick_epoch({"nothing": 1}) == 0


def test_sane_epoch_replaces_impossible_stamps():
    now = 1700000000.0
    assert collector.sane_epoch(1699999000, now=now) == 1699999000
    assert collector.sane_epoch(1700000000123, now=now) == 1700000000  # 毫秒折算后可用
    assert collector.sane_epoch(0, now=now) == 1700000000  # 缺失 -> now
    assert collector.sane_epoch(4000000000, now=now) == 1700000000  # 穿越未来 -> now
    assert collector.sane_epoch(1000, now=now) == 1700000000  # 远古 -> now


def test_parse_history_page_accepts_millisecond_time():
    page = [
        {
            "message_id": "9",
            "sender": {"user_id": "1", "nickname": "阿狸"},
            "message": [{"type": "text", "data": {"text": "在的"}}],
            "time": 1700000000123,
        },
    ]
    assert collector.parse_history_page(page)[0]["ts"] == 1700000000


def test_clean_rows_normalizes_millisecond_ts():
    rows = [{"message_id": "1", "user_id": "1", "text": "今天天气不错", "ts": 1700000000123}]
    assert collector.clean_rows(rows)[0].ts == 1700000000
