"""对话现场（dialogue）测试：锚点定位、上下文窗口、均匀抽样、社交信号统计。"""

from __future__ import annotations

from astrbot_plugin_persona_prism.prism import dialogue

TA = "1001"
BOT = "9999"
BASE = 1700000000


def _row(mid, uid, text, offset=0, **extra):
    row = {
        "message_id": mid,
        "user_id": uid,
        "user_name": {TA: "\u72d0\u72f8", "1002": "\u9e7f\u9e7f", "1003": "\u718a\u718a", BOT: "\u5c0f\u52a9\u624b"}.get(uid, "U" + uid),
        "text": text,
        "ts": BASE + offset,
    }
    row.update(extra)
    return row


class TestNameIndex:
    def test_uses_row_nickname(self):
        rows = [_row("1", TA, "\u5728")]
        assert dialogue.name_index(rows)[TA] == "\u72d0\u72f8"

    def test_external_names_win(self):
        rows = [_row("1", TA, "\u5728")]
        idx = dialogue.name_index(rows, {TA: "\u7fa4\u540d\u7247"})
        assert idx[TA] == "\u7fa4\u540d\u7247"

    def test_blank_external_name_is_ignored(self):
        rows = [_row("1", TA, "\u5728")]
        idx = dialogue.name_index(rows, {TA: "   "})
        assert idx[TA] == "\u72d0\u72f8"


class TestOrderRows:
    def test_sorted_by_timestamp(self):
        rows = [_row("2", TA, "b", 60), _row("1", TA, "a", 0)]
        assert [r["message_id"] for r in dialogue.order_rows(rows)] == ["1", "2"]

    def test_same_timestamp_falls_back_to_message_id(self):
        rows = [_row("b", TA, "x"), _row("a", TA, "y")]
        assert [r["message_id"] for r in dialogue.order_rows(rows)] == ["a", "b"]


class TestAnchorIndices:
    def test_own_messages_are_anchors(self):
        ordered = [_row("1", "1002", "a", 0), _row("2", TA, "b", 10)]
        assert dialogue.anchor_indices(ordered, TA) == [1]

    def test_reply_to_target_is_anchor(self):
        ordered = [
            _row("1", TA, "\u6211\u53bb\u4fee\u98ce\u8f66", 0),
            _row("2", "1002", "\u5389\u5bb3", 10, reply_to="1"),
        ]
        assert dialogue.anchor_indices(ordered, TA) == [0, 1]

    def test_at_target_is_anchor(self):
        ordered = [_row("1", "1002", "\u5728\u4e48", 0, at_ids=TA)]
        assert dialogue.anchor_indices(ordered, TA) == [0]

    def test_blank_target_has_no_anchor(self):
        ordered = [_row("1", TA, "a", 0)]
        assert dialogue.anchor_indices(ordered, "") == []


class TestPickEvenly:
    def test_returns_all_when_under_limit(self):
        assert dialogue.pick_evenly([1, 2, 3], 5) == [1, 2, 3]

    def test_covers_head_and_tail(self):
        picked = dialogue.pick_evenly(list(range(100)), 5)
        assert picked[0] == 0
        assert picked[-1] >= 80
        assert len(picked) == 5

    def test_result_is_sorted_and_unique(self):
        picked = dialogue.pick_evenly([5, 5, 6, 7, 8, 9], 3)
        assert picked == sorted(set(picked))

    def test_non_positive_limit_means_no_limit(self):
        assert dialogue.pick_evenly([1, 2, 3], 0) == [1, 2, 3]


class TestCollectWindows:
    def test_expands_around_anchor(self):
        ordered = [_row(str(i), TA, "x", i * 10) for i in range(10)]
        assert dialogue.collect_windows(ordered, [5], context=2, max_lines=50) == [3, 4, 5, 6, 7]

    def test_windows_are_merged(self):
        ordered = [_row(str(i), TA, "x", i * 10) for i in range(10)]
        picked = dialogue.collect_windows(ordered, [2, 3], context=1, max_lines=50)
        assert picked == [1, 2, 3, 4]

    def test_clamped_at_both_ends(self):
        ordered = [_row(str(i), TA, "x", i * 10) for i in range(3)]
        assert dialogue.collect_windows(ordered, [0, 2], context=5, max_lines=50) == [0, 1, 2]

    def test_over_budget_keeps_newer_lines(self):
        ordered = [_row(str(i), TA, "x", i * 10) for i in range(20)]
        picked = dialogue.collect_windows(ordered, list(range(20)), context=0, max_lines=4)
        assert picked == [16, 17, 18, 19]

    def test_no_anchor_gives_nothing(self):
        ordered = [_row("1", TA, "x", 0)]
        assert dialogue.collect_windows(ordered, [], context=2, max_lines=10) == []


class TestRenderLines:
    def test_labels_target_others_and_bot(self):
        ordered = dialogue.order_rows(
            [
                _row("1", "1002", "\u5927\u5bb6\u5348\u5b89", 0),
                _row("2", TA, "\u5348\u5b89", 10),
                _row("3", BOT, "\u65e9\u5b89", 20),
            ]
        )
        lines = dialogue.render_lines(ordered, [0, 1, 2], TA, self_id=BOT)
        assert dialogue.LABEL_OTHER in lines[0]
        assert dialogue.LABEL_TARGET in lines[1]
        assert dialogue.LABEL_BOT in lines[2]

    def test_gap_mark_inserted_between_scenes(self):
        ordered = [_row(str(i), TA, "x" + str(i), i * 10) for i in range(6)]
        lines = dialogue.render_lines(ordered, [0, 4], TA)
        assert lines[1] == dialogue.GAP_MARK

    def test_gap_mark_never_leads_or_trails(self):
        ordered = [_row(str(i), TA, "x" + str(i), i * 10) for i in range(6)]
        lines = dialogue.render_lines(ordered, [0, 3], TA)
        assert lines[0] != dialogue.GAP_MARK
        assert lines[-1] != dialogue.GAP_MARK

    def test_long_time_gap_splits_scenes(self):
        ordered = [
            _row("1", TA, "a", 0),
            _row("2", TA, "b", dialogue.SCENE_GAP_SECONDS + 60),
        ]
        lines = dialogue.render_lines(ordered, [0, 1], TA)
        assert any(dialogue.is_gap_line(ln) for ln in lines)

    def test_gap_line_tells_how_long(self):
        ordered = [
            _row("1", TA, "a", 0),
            _row("2", TA, "b", 3600),
        ]
        lines = dialogue.render_lines(ordered, [0, 1], TA)
        assert "\u9694\u4e86" in lines[1]

    def test_quote_line_added_when_origin_not_shown(self):
        ordered = [
            _row("1", "1002", "\u665a\u4e0a\u5f00\u9ed1\u5417", 0),
            _row("2", "1003", "\u65e0\u5173\u7684\u8bdd", 10),
            _row("3", TA, "\u6765", 20, reply_to="1"),
        ]
        lines = dialogue.render_lines(ordered, [2], TA)
        assert lines[0].startswith(dialogue.QUOTE_PREFIX)
        assert "\u665a\u4e0a\u5f00\u9ed1\u5417" in lines[0]

    def test_no_quote_line_when_origin_shown(self):
        ordered = [
            _row("1", "1002", "\u665a\u4e0a\u5f00\u9ed1\u5417", 0),
            _row("2", TA, "\u6765", 20, reply_to="1"),
        ]
        lines = dialogue.render_lines(ordered, [0, 1], TA)
        assert not any(ln.startswith(dialogue.QUOTE_PREFIX) for ln in lines)

    def test_reply_mark_points_back_to_target(self):
        ordered = [
            _row("1", TA, "\u6211\u9192\u4e86", 0),
            _row("2", "1002", "\u65e9", 10, reply_to="1"),
        ]
        lines = dialogue.render_lines(ordered, [0, 1], TA)
        assert "\u56de\u5e94TA" in lines[1]

    def test_at_mark_rendered(self):
        ordered = [_row("1", "1002", "\u4f60\u5728\u5417", 0, at_ids=TA)]
        lines = dialogue.render_lines(ordered, [0], TA)
        assert "@TA" in lines[0]

    def test_image_only_row_is_dropped(self):
        # \u6a21\u578b\u770b\u4e0d\u89c1\u56fe\uff0c\u300c[\u56fe\u7247]\u300d\u8fdb\u73b0\u573a\u53ea\u4f1a\u5360\u7bc7\u5e45\u5e76\u8bf1\u5bfc\u5b83\u7f16\u56fe\u91cc\u6709\u4ec0\u4e48\u3002
        ordered = [_row("1", TA, "", 0, images=2)]
        assert dialogue.render_lines(ordered, [0], TA) == []

    def test_placeholder_is_stripped_but_real_words_stay(self):
        ordered = [_row("1", TA, "[\u56fe\u7247]\u7b11\u6b7b", 0)]
        line = dialogue.render_lines(ordered, [0], TA, with_clock=False)[0]
        assert line.endswith("\u7b11\u6b7b")
        assert "[\u56fe\u7247]" not in line

    def test_clock_can_be_disabled(self):
        ordered = [_row("1", TA, "a", 0)]
        line = dialogue.render_lines(ordered, [0], TA, with_clock=False)[0]
        assert line.startswith(dialogue.LABEL_TARGET)


class TestBuildDialogueBlock:
    def _scene(self):
        return [
            _row("1", "1002", "\u4eca\u5929\u98ce\u5927\u5417", 0),
            _row("2", TA, "\u98ce\u8f66\u8f6c\u5f97\u98de\u8d77", 30),
            _row("3", "1003", "\u90a3\u5c31\u597d", 60, reply_to="2"),
        ]

    def test_block_contains_context_from_others(self):
        block = dialogue.build_dialogue_block(self._scene(), TA, context=1)
        assert dialogue.has_other_voices(block)
        assert "\u98ce\u8f66\u8f6c\u5f97\u98de\u8d77" in block

    def test_empty_rows_give_empty_block(self):
        assert dialogue.build_dialogue_block([], TA) == ""

    def test_target_absent_gives_empty_block(self):
        rows = [_row("1", "1002", "\u5728", 0)]
        assert dialogue.build_dialogue_block(rows, TA) == ""

    def test_zero_context_keeps_only_target_lines(self):
        rows = [
            _row("1", "1002", "\u4eca\u5929\u98ce\u5927\u5417", 0),
            _row("2", TA, "\u98ce\u8f66\u8f6c\u5f97\u98de\u8d77", 30),
            _row("3", "1003", "\u65e0\u5173\u7684\u8bdd", 60),
        ]
        block = dialogue.build_dialogue_block(rows, TA, context=0)
        assert dialogue.LABEL_TARGET in block
        assert not dialogue.has_other_voices(block)

    def test_max_lines_is_respected(self):
        rows = [_row(str(i), TA if i % 2 else "1002", "x" + str(i), i * 10) for i in range(40)]
        block = dialogue.build_dialogue_block(rows, TA, context=2, max_lines=6)
        real = [ln for ln in block.split("\n") if not dialogue.is_gap_line(ln)]
        assert len(real) <= 6

    def test_external_names_used_in_output(self):
        block = dialogue.build_dialogue_block(
            self._scene(), TA, names={"1002": "\u7fa4\u4e3b"}, context=1
        )
        assert "\u7fa4\u4e3b" in block


class TestSocialSignals:
    def test_counts_both_directions(self):
        rows = [
            _row("1", "1002", "\u5728\u5417", 0),
            _row("2", TA, "\u5728", 10, reply_to="1"),
            _row("3", "1002", "\u90a3\u6765\u73a9", 20, reply_to="2"),
            _row("4", "1003", "\u53eb\u4f60\u5462", 30, at_ids=TA),
        ]
        sig = dialogue.social_signals(rows, TA)
        assert sig.replied_others == 1
        assert sig.got_replies == 1
        assert sig.got_at == 1
        assert sig.mine == 1
        assert sig.total == 4

    def test_response_rate_zero_without_own_lines(self):
        rows = [_row("1", "1002", "\u5728\u5417", 0)]
        assert dialogue.social_signals(rows, TA).response_rate == 0.0

    def test_responders_sorted_by_count(self):
        rows = [
            _row("1", TA, "\u5927\u5bb6\u597d", 0),
            _row("2", "1002", "hi", 10, reply_to="1"),
            _row("3", "1002", "hi2", 20, reply_to="1"),
            _row("4", "1003", "hi3", 30, reply_to="1"),
        ]
        sig = dialogue.social_signals(rows, TA)
        assert sig.responders[0][1] == 2

    def test_bot_replies_count_as_a_real_interaction(self):
        rows = [
            _row("1", TA, "\u5728\u5417", 0),
            _row("2", BOT, "\u5728", 10, reply_to="1"),
        ]
        sig = dialogue.social_signals(rows, TA, self_id=BOT)
        #: \u673a\u5668\u4eba\u4e5f\u662f\u7fa4\u91cc\u7684\u4e00\u5458\uff0cTA \u627e\u5b83\u8bf4\u8bdd\u5e76\u88ab\u63a5\u4f4f\uff0c\u5c31\u662f\u4e00\u6b21\u771f\u5b9e\u4e92\u52a8\u3002
        assert sig.got_replies == 1
        assert sig.total == 2

    def test_prompt_block_is_plain_facts(self):
        rows = [
            _row("1", TA, "\u5728\u5417", 0),
            _row("2", "1002", "\u5728", 10, reply_to="1"),
        ]
        block = dialogue.social_signals(rows, TA).to_prompt_block()
        assert block
        assert "TA" in block

    def test_empty_rows_give_empty_prompt_block(self):
        assert dialogue.social_signals([], TA).to_prompt_block() == ""

class TestTurns:
    def test_quiet_gap_splits_turns(self):
        ordered = [_row("1", TA, "a", 0), _row("2", TA, "b", dialogue.TURN_GAP_SECONDS + 60)]
        turns = dialogue.split_turns(ordered)
        assert [(t.start, t.end) for t in turns] == [(0, 0), (1, 1)]

    def test_dense_rows_stay_one_turn(self):
        ordered = [_row(str(i), TA, "x", i * 30) for i in range(5)]
        assert len(dialogue.split_turns(ordered)) == 1

    def test_empty_rows_have_no_turn(self):
        assert dialogue.split_turns([]) == []

    def test_turn_of_finds_owner(self):
        ordered = [_row("1", TA, "a", 0), _row("2", TA, "b", dialogue.TURN_GAP_SECONDS + 60)]
        turns = dialogue.split_turns(ordered)
        assert dialogue.turn_of(turns, 1).start == 1
        assert dialogue.turn_of(turns, 9) is None

    def test_clip_turn_centers_on_anchor(self):
        turn = dialogue.Turn(0, 19)
        picked = dialogue.clip_turn(turn, 10, 5)
        assert picked == [8, 9, 10, 11, 12]

    def test_clip_turn_keeps_short_turn_whole(self):
        assert dialogue.clip_turn(dialogue.Turn(2, 4), 3, 9) == [2, 3, 4]

    def test_clip_turn_shifts_back_at_tail(self):
        assert dialogue.clip_turn(dialogue.Turn(0, 9), 9, 3) == [7, 8, 9]

class TestGradeAnchors:
    def test_reply_to_other_is_edge(self):
        ordered = [
            _row("1", "1002", "\u5728\u5417", 0),
            _row("2", TA, "\u5728", 10, reply_to="1"),
        ]
        assert dialogue.grade_anchors(ordered, TA)[1] == dialogue.TIER_EDGE

    def test_being_replied_is_edge(self):
        ordered = [
            _row("1", TA, "\u6211\u56de\u6765\u4e86", 0),
            _row("2", "1002", "\u6b22\u8fce", 10, reply_to="1"),
        ]
        assert dialogue.grade_anchors(ordered, TA)[1] == dialogue.TIER_EDGE

    def test_consecutive_own_lines_are_run(self):
        ordered = [_row("1", TA, "a", 0), _row("2", TA, "b", 10)]
        tiers = dialogue.grade_anchors(ordered, TA)
        assert tiers[0] == dialogue.TIER_RUN
        assert tiers[1] == dialogue.TIER_RUN

    def test_isolated_line_is_alone(self):
        ordered = [
            _row("1", "1002", "a", 0),
            _row("2", TA, "b", 10),
            _row("3", "1003", "c", 20),
        ]
        assert dialogue.grade_anchors(ordered, TA)[1] == dialogue.TIER_ALONE

    def test_blank_target_has_no_tier(self):
        rows = [_row("1", TA, "a", 0)]
        assert dialogue.grade_anchors(rows, "") == {}

class TestSelectLines:
    def test_zero_context_returns_anchors_only(self):
        ordered = [
            _row("1", "1002", "a", 0),
            _row("2", TA, "b", 10),
        ]
        tiers = dialogue.grade_anchors(ordered, TA)
        picked = dialogue.select_lines(ordered, tiers, context=0, max_lines=10, max_scenes=5)
        assert picked == [1]

    def test_edge_tier_wins_the_only_slot(self):
        rows = []
        for i in range(3):
            base = i * (dialogue.TURN_GAP_SECONDS * 3)
            rows.append(_row("a" + str(i), "1002", "\u95f2\u804a", base))
            rows.append(_row("b" + str(i), TA, "\u55ef", base + 30))
        rows[-1]["reply_to"] = "a2"
        ordered = dialogue.order_rows(rows)
        tiers = dialogue.grade_anchors(ordered, TA)
        picked = dialogue.select_lines(ordered, tiers, context=2, max_lines=40, max_scenes=1)
        assert picked == [4, 5]

    def test_line_budget_keeps_recent_tail(self):
        ordered = [_row(str(i), TA if i % 2 else "1002", "x", i * 10) for i in range(20)]
        tiers = dialogue.grade_anchors(ordered, TA)
        picked = dialogue.select_lines(ordered, tiers, context=3, max_lines=4, max_scenes=6)
        assert len(picked) == 4
        assert picked == sorted(picked)

    def test_no_tier_gives_nothing(self):
        ordered = [_row("1", "1002", "a", 0)]
        assert dialogue.select_lines(ordered, {}, context=2, max_lines=10, max_scenes=3) == []

class TestGapMark:
    def test_minutes(self):
        assert "\u5206\u949f" in dialogue.gap_mark(20 * 60)

    def test_hours(self):
        assert "\u5c0f\u65f6" in dialogue.gap_mark(3 * 3600)

    def test_days(self):
        assert "\u5929" in dialogue.gap_mark(3 * 86400)

    def test_short_gap_falls_back(self):
        assert dialogue.gap_mark(30) == dialogue.GAP_MARK

    def test_is_gap_line_rejects_normal_line(self):
        assert not dialogue.is_gap_line("[TA] \u72d0\u72f8\uff1a\u5728")

    def test_is_gap_line_accepts_all_marks(self):
        assert dialogue.is_gap_line(dialogue.GAP_MARK)
        assert dialogue.is_gap_line(dialogue.gap_mark(3600))


class TestAnswerRate:
    def test_quick_reply_counts_as_answered(self):
        rows = [
            _row("1", TA, "\u5728\u5417", 0),
            _row("2", "1002", "\u5728", 30),
        ]
        sig = dialogue.social_signals(rows, TA)
        assert sig.answered == 1
        assert sig.unanswered == 0
        assert sig.answer_rate == 1.0

    def test_late_reply_counts_as_cold(self):
        rows = [
            _row("1", TA, "\u5728\u5417", 0),
            _row("2", "1002", "x", dialogue.RESPONSE_WINDOW_SECONDS + 600),
        ]
        assert dialogue.social_signals(rows, TA).unanswered == 1

    def test_burst_settles_once(self):
        rows = [
            _row("1", TA, "a", 0),
            _row("2", TA, "b", 5),
            _row("3", TA, "c", 10),
            _row("4", "1002", "\u6536\u5230", 20),
        ]
        sig = dialogue.social_signals(rows, TA)
        assert sig.answered == 1
        assert sig.unanswered == 0

    def test_last_line_in_window_is_not_judged(self):
        rows = [_row("1", "1002", "a", 0), _row("2", TA, "b", 10)]
        sig = dialogue.social_signals(rows, TA)
        assert sig.answered == 0
        assert sig.unanswered == 0

    def test_bot_reply_counts_as_an_answer(self):
        rows = [
            _row("1", TA, "\u5728\u5417", 0),
            _row("2", BOT, "\u5728", 5),
        ]
        sig = dialogue.social_signals(rows, TA, self_id=BOT)
        assert sig.answered == 1
        assert sig.unanswered == 0

    def test_answer_rate_appears_in_prompt_block(self):
        rows = [
            _row("1", TA, "\u5728\u5417", 0),
            _row("2", "1002", "\u5728", 30),
        ]
        block = dialogue.social_signals(rows, TA).to_prompt_block()
        assert "\u63a5\u8bdd\u7387" in block


class TestBotLabel:
    def test_bot_lines_are_labelled_as_you(self):
        ordered = dialogue.order_rows([_row("1", TA, "a", 0), _row("2", BOT, "b", 5)])
        lines = dialogue.render_lines(ordered, [0, 1], TA, self_id=BOT, with_clock=False)
        assert any("[\u4f60]" in line for line in lines)
        assert "[\u673a\u5668\u4eba]" not in "\n".join(lines)

    def test_target_label_wins_when_the_bot_itself_is_analysed(self):
        ordered = dialogue.order_rows([_row("1", BOT, "a", 0), _row("2", "1002", "b", 5)])
        lines = dialogue.render_lines(ordered, [0, 1], BOT, self_id=BOT, with_clock=False)
        assert "[TA]" in lines[0]
