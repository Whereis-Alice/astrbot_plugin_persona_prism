"""聊天现场还原（scenes）测试：原话定位、上下文切片、气泡渲染、证供补全。"""

from __future__ import annotations

from astrbot_plugin_persona_prism.prism import scenes
from astrbot_plugin_persona_prism.prism.models import CorpusMessage, Evidence, Utterance

ME = "1001"


def _msg(mid: str, text: str, ts: int = 1700000000) -> CorpusMessage:
    return CorpusMessage(message_id=mid, user_id=ME, user_name="狐狸", text=text, ts=ts)


def _row(mid: str, uid: str, text: str, ts: int, **extra):
    row = {
        "message_id": mid,
        "user_id": uid,
        "user_name": "U" + uid,
        "text": text,
        "ts": ts,
    }
    row.update(extra)
    return row


class TestLocateQuote:
    def test_exact_match_wins(self):
        msgs = [_msg("1", "今天天气不错"), _msg("2", "我去睡了")]
        assert scenes.locate_quote("我去睡了", msgs).message_id == "2"

    def test_ignores_punctuation_and_case(self):
        msgs = [_msg("1", "OK，那就这样吧")]
        assert scenes.locate_quote("ok 那就这样吧！", msgs).message_id == "1"

    def test_truncated_quote_still_matches(self):
        msgs = [_msg("1", "这个方案我觉得可以，但是预算得再砍一刀")]
        assert scenes.locate_quote("这个方案我觉得可以", msgs).message_id == "1"

    def test_unrelated_quote_returns_none(self):
        msgs = [_msg("1", "今天天气不错")]
        assert scenes.locate_quote("我明天要去修风车了", msgs) is None

    def test_empty_inputs_return_none(self):
        assert scenes.locate_quote("   ", [_msg("1", "在")]) is None
        assert scenes.locate_quote("在", []) is None

    def test_blank_corpus_text_is_skipped(self):
        msgs = [_msg("1", "   "), _msg("2", "在的")]
        assert scenes.locate_quote("在的", msgs).message_id == "2"


class TestRowsToUtterances:
    def test_marks_mine_and_uses_names_override(self):
        rows = [
            _row("1", "2002", "你在吗", 100),
            _row("2", ME, "在的", 101),
        ]
        lines = scenes.rows_to_utterances(rows, ME, names={"2002": "阿狸"})
        assert [(u.speaker, u.text, u.mine) for u in lines] == [
            ("阿狸", "你在吗", False),
            ("U1001", "在的", True),
        ]

    def test_media_only_rows_render_placeholder(self):
        rows = [
            _row("1", ME, "", 100, images=1),
            _row("2", ME, "", 101, images=3),
            _row("3", ME, "", 102),
        ]
        lines = scenes.rows_to_utterances(rows, ME)
        assert [u.text for u in lines] == ["[图片]", "[图片×3]"]

    def test_falls_back_to_user_id_then_placeholder(self):
        rows = [_row("1", "2002", "哦", 100, user_name=""), _row("2", "", "?", 101, user_name="")]
        lines = scenes.rows_to_utterances(rows, ME)
        assert [u.speaker for u in lines] == ["2002", "群友"]
        assert lines[1].mine is False


class TestSliceAround:
    def test_centers_on_message_id(self):
        rows = [_row(str(i), ME, f"t{i}", 100 + i) for i in range(5)]
        got = scenes.slice_around(rows, message_id="2", center_ts=0, context=1)
        assert [r["message_id"] for r in got] == ["1", "2", "3"]

    def test_unknown_id_falls_back_to_nearest_ts(self):
        rows = [_row(str(i), ME, f"t{i}", 100 + i * 10) for i in range(5)]
        got = scenes.slice_around(rows, message_id="nope", center_ts=131, context=1)
        assert [r["message_id"] for r in got] == ["2", "3", "4"]

    def test_clamps_at_boundaries_and_sorts_by_ts(self):
        rows = [_row("b", ME, "b", 200), _row("a", ME, "a", 100), _row("c", ME, "c", 300)]
        got = scenes.slice_around(rows, message_id="a", context=2)
        assert [r["message_id"] for r in got] == ["a", "b", "c"]

    def test_empty_rows_or_no_anchor(self):
        assert scenes.slice_around([], message_id="1") == []
        assert scenes.slice_around([_row("1", ME, "x", 100)], message_id="", center_ts=0) == []

    def test_zero_context_returns_single_row(self):
        rows = [_row(str(i), ME, f"t{i}", 100 + i) for i in range(3)]
        got = scenes.slice_around(rows, message_id="1", context=0)
        assert [r["message_id"] for r in got] == ["1"]


class TestSceneTitle:
    def test_without_ts_returns_label(self):
        assert scenes.scene_title(0, "现场片段") == "现场片段"

    def test_with_ts_prefixes_clock(self):
        title = scenes.scene_title(1700000000, "呈堂证供")
        assert title.endswith(" · 呈堂证供")
        assert len(title.split(" · ")[0]) == 5


class TestEnrichEvidence:
    def _fixture(self):
        rows = [
            _row("10", "2002", "这周还去修风车吗", 1000),
            _row("11", ME, "去啊，零件都买好了", 1001),
            _row("12", "2003", "带我一个", 1002),
        ]
        msgs = [_msg("11", "去啊，零件都买好了", 1001)]
        return rows, msgs

    def test_fills_dialogue_and_title(self):
        rows, msgs = self._fixture()
        item = Evidence(quote="去啊，零件都买好了", reason="行动力")
        assert scenes.enrich_evidence(item, msgs, rows, user_id=ME) is True
        assert [u.text for u in item.dialogue] == [
            "这周还去修风车吗",
            "去啊，零件都买好了",
            "带我一个",
        ]
        assert [u.mine for u in item.dialogue] == [False, True, False]
        assert item.title.endswith("现场片段")

    def test_keeps_existing_title(self):
        rows, msgs = self._fixture()
        item = Evidence(quote="去啊，零件都买好了", title="说到做到")
        assert scenes.enrich_evidence(item, msgs, rows, user_id=ME) is True
        assert item.title == "说到做到"

    def test_skips_when_dialogue_already_present(self):
        rows, msgs = self._fixture()
        item = Evidence(quote="去啊，零件都买好了", dialogue=[Utterance(speaker="狐狸", text="旧的")])
        assert scenes.enrich_evidence(item, msgs, rows, user_id=ME) is False
        assert [u.text for u in item.dialogue] == ["旧的"]

    def test_returns_false_when_quote_not_found(self):
        rows, msgs = self._fixture()
        item = Evidence(quote="完全不相干的一句话在这里")
        assert scenes.enrich_evidence(item, msgs, rows, user_id=ME) is False
        assert item.dialogue == []

    def test_returns_false_when_window_misses_self(self):
        rows, msgs = self._fixture()
        others = [r for r in rows if r["user_id"] != ME]
        item = Evidence(quote="去啊，零件都买好了")
        assert scenes.enrich_evidence(item, msgs, others, user_id=ME) is False
        assert item.dialogue == []


class TestEnrichAll:
    def test_counts_only_newly_filled(self):
        rows = [
            _row("10", "2002", "在吗", 1000),
            _row("11", ME, "在的", 1001),
        ]
        msgs = [_msg("11", "在的", 1001)]
        items = [
            Evidence(quote="在的"),
            Evidence(quote="彻底对不上的一句话"),
            Evidence(quote="在的", dialogue=[Utterance(speaker="狐狸", text="旧的")]),
        ]
        assert scenes.enrich_all(items, msgs, rows, user_id=ME, label="呈堂证供") == 1
        assert items[0].title.endswith("呈堂证供")
        assert items[1].dialogue == []

    def test_empty_list_returns_zero(self):
        assert scenes.enrich_all([], [], [], user_id=ME) == 0

class TestRealText:
    def test_strips_media_placeholders(self):
        assert scenes.real_text("[图片][表情]") == ""
        assert scenes.real_text("[图片×3]看这个") == "看这个"

    def test_keeps_plain_text(self):
        assert scenes.real_text("今天真热") == "今天真热"

    def test_handles_none_like_input(self):
        assert scenes.real_text("") == ""


class TestSceneHasSubstance:
    @staticmethod
    def _u(text: str, mine: bool = False) -> Utterance:
        return Utterance(speaker="U", text=text, clock="12:00", mine=mine)

    def test_empty_scene_is_rejected(self):
        assert scenes.scene_has_substance([]) is False

    def test_all_placeholder_scene_is_rejected(self):
        lines = [
            self._u("[图片]", mine=True),
            self._u("[表情]"),
            self._u("[图片×2]"),
        ]
        assert scenes.scene_has_substance(lines) is False

    def test_short_but_real_exchange_passes(self):
        #: 「在吗 / 在的」也是真对话，不能因为字少就丢掉。
        lines = [self._u("在吗", mine=True), self._u("在的")]
        assert scenes.scene_has_substance(lines) is True

    def test_rejected_when_only_others_talk(self):
        #: 本人那句没有真实文字时，这段撑不起「证据」。
        lines = [self._u("[图片]", mine=True), self._u("这个梗太强了")]
        assert scenes.scene_has_substance(lines) is False

    def test_floors_are_configurable(self):
        lines = [self._u("嗯", mine=True), self._u("好")]
        assert scenes.scene_has_substance(lines, own_floor=1, total_floor=2) is True
        assert scenes.scene_has_substance(lines, own_floor=1, total_floor=99) is False
