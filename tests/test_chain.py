"""LLM 选段（chain）测试。

这一层的铁律：模型只允许输出编号，原文一个字都不能由模型代写。所以测试重点在
「编号 → 本地真实行」的映射是否严丝合缝，以及各种脏输出能不能被挡回去。
"""

from __future__ import annotations

from astrbot_plugin_persona_prism.prism import chain, dialogue

TA = "1001"
BOT = "9999"
BASE = 1700000000


def _row(mid, uid, text, offset=0, **extra):
    row = {
        "message_id": mid,
        "user_id": uid,
        "user_name": {TA: "狐狸", "1002": "鹿鹿", "1003": "熊熊", BOT: "小助手"}.get(uid, "U" + uid),
        "text": text,
        "ts": BASE + offset,
    }
    row.update(extra)
    return row


def _talk():
    """一段有来有回的群聊，本人在 1 / 3 / 5 位置。"""
    return [
        _row("1", "1002", "周末去哪玩", 0),
        _row("2", TA, "我想去爬山", 30),
        _row("3", "1003", "爬山太累了吧", 90),
        _row("4", TA, "累才有意思", 150),
        _row("5", BOT, "我帮你们查了天气，周六晴", 200),
        _row("6", TA, "那就周六", 260),
    ]


# ---------------------------------------------------------------------------
# 候选行
# ---------------------------------------------------------------------------


class TestCandidateIndices:
    def test_empty_input_yields_nothing(self):
        assert chain.candidate_indices([], TA) == []

    def test_returns_nothing_when_target_never_spoke(self):
        ordered = [_row("1", "1002", "在吗", 0), _row("2", "1003", "在", 10)]
        assert chain.candidate_indices(ordered, TA) == []

    def test_covers_every_own_line(self):
        ordered = _talk()
        picked = chain.candidate_indices(ordered, TA)
        assert {1, 3, 5}.issubset(set(picked))

    def test_brings_neighbours_along(self):
        ordered = _talk()
        picked = chain.candidate_indices(ordered, TA)
        # 本人发言周围的别人发言也要在候选里，不然模型没法判断在跟谁说话。
        assert 0 in picked


# ---------------------------------------------------------------------------
# 成绩单
# ---------------------------------------------------------------------------


class TestBuildSheet:
    def test_numbers_start_at_one_and_map_back(self):
        ordered = _talk()
        sheet, numbers = chain.build_sheet(ordered, [0, 1, 2], TA)
        assert list(numbers) == [1, 2, 3]
        assert numbers[1] == 0
        assert sheet.splitlines()[0].startswith("#1 ")

    def test_labels_target_bot_and_others_apart(self):
        ordered = _talk()
        sheet, _ = chain.build_sheet(ordered, [0, 1, 4], TA, self_id=BOT)
        lines = sheet.splitlines()
        assert dialogue.LABEL_OTHER in lines[0]
        assert dialogue.LABEL_TARGET in lines[1]
        assert dialogue.LABEL_BOT in lines[2]

    def test_external_names_win_over_row_nickname(self):
        ordered = _talk()
        sheet, _ = chain.build_sheet(ordered, [1], TA, names={TA: "群名片"})
        assert "群名片" in sheet

    def test_out_of_range_indices_are_skipped(self):
        ordered = _talk()
        sheet, numbers = chain.build_sheet(ordered, [-1, 1, 999], TA)
        assert list(numbers) == [1]
        assert len(sheet.splitlines()) == 1

    def test_blank_rows_do_not_consume_a_number(self):
        ordered = [_row("1", TA, "   ", 0), _row("2", TA, "有内容", 10)]
        sheet, numbers = chain.build_sheet(ordered, [0, 1], TA)
        assert list(numbers) == [1]
        assert numbers[1] == 1
        assert "有内容" in sheet

    def test_long_text_is_truncated(self):
        ordered = [_row("1", TA, "啊" * 300, 0)]
        sheet, _ = chain.build_sheet(ordered, [0], TA)
        assert "…" in sheet
        assert len(sheet) < 300

    def test_reply_rows_are_marked(self):
        ordered = [_row("1", "1002", "问题", 0), _row("2", TA, "回答", 10, reply_to="1")]
        sheet, _ = chain.build_sheet(ordered, [0, 1], TA)
        assert "（回复了某条）" in sheet.splitlines()[1]


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_carries_sheet_and_name(self):
        text = chain.build_prompt("#1 …", target_name="狐狸", max_scenes=3)
        assert "狐狸" in text
        assert "#1 …" in text
        assert "3 场" in text

    def test_blank_name_falls_back_to_ta(self):
        assert "TA 参与过的完整对话" in chain.build_prompt("x", target_name="  ")

    def test_asks_for_json_only(self):
        text = chain.build_prompt("x")
        assert "只输出" in text
        assert '"scenes"' in text

    def test_system_prompt_forbids_commentary(self):
        assert "不要复述原文" in chain.CHAIN_SYSTEM


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


class TestParseChain:
    def _numbers(self, ordered):
        return chain.build_sheet(ordered, list(range(len(ordered))), TA)[1]

    def test_plain_json_becomes_indices(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        scenes = chain.parse_chain(
            '{"scenes":[{"ids":[1,2,3],"why":"在聊周末"}]}',
            numbers,
            ordered,
            TA,
        )
        assert len(scenes) == 1
        assert scenes[0].indices == [0, 1, 2]
        assert scenes[0].why == "在聊周末"

    def test_code_fence_and_chatter_are_tolerated(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        raw = '好的，这是结果：\n```json\n{"scenes":[{"ids":[1,2]}]}\n```'
        assert chain.parse_chain(raw, numbers, ordered, TA)[0].indices == [0, 1]

    def test_ids_written_as_string_are_accepted(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        scenes = chain.parse_chain('{"scenes":[{"ids":"#1, #2"}]}', numbers, ordered, TA)
        assert scenes[0].indices == [0, 1]

    def test_unknown_numbers_are_dropped(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        scenes = chain.parse_chain('{"scenes":[{"ids":[1,2,777]}]}', numbers, ordered, TA)
        assert scenes[0].indices == [0, 1]

    def test_scene_without_target_is_rejected(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        # #1 / #3 都是别人说的，这一场跟被分析者无关。
        assert chain.parse_chain('{"scenes":[{"ids":[1,3]}]}', numbers, ordered, TA) == []

    def test_too_short_scene_is_rejected(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        assert chain.parse_chain('{"scenes":[{"ids":[2]}]}', numbers, ordered, TA) == []

    def test_overlapping_scenes_do_not_reuse_lines(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        raw = '{"scenes":[{"ids":[1,2,3]},{"ids":[2,3,4,5]}]}'
        scenes = chain.parse_chain(raw, numbers, ordered, TA)
        seen: list[int] = []
        for scene in scenes:
            seen.extend(scene.indices)
        assert len(seen) == len(set(seen))

    def test_max_scenes_is_respected(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        raw = '{"scenes":[{"ids":[1,2]},{"ids":[3,4]},{"ids":[5,6]}]}'
        assert len(chain.parse_chain(raw, numbers, ordered, TA, max_scenes=2)) == 2

    def test_scenes_come_back_in_chat_order(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        raw = '{"scenes":[{"ids":[5,6]},{"ids":[1,2]}]}'
        scenes = chain.parse_chain(raw, numbers, ordered, TA)
        assert [s.start for s in scenes] == sorted(s.start for s in scenes)

    def test_garbage_replies_yield_nothing(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        for raw in ("", "抱歉我做不到", "[]", '{"foo":1}', '{"scenes":"nope"}'):
            assert chain.parse_chain(raw, numbers, ordered, TA) == []

    def test_why_is_clipped(self):
        ordered = _talk()
        numbers = self._numbers(ordered)
        raw = '{"scenes":[{"ids":[1,2],"why":"%s"}]}' % ("长" * 80)
        assert len(chain.parse_chain(raw, numbers, ordered, TA)[0].why) == 40


# ---------------------------------------------------------------------------
# 场次对象与渲染
# ---------------------------------------------------------------------------


class TestChainScene:
    def test_start_of_empty_scene_is_minus_one(self):
        assert chain.ChainScene().start == -1

    def test_covers_checks_membership(self):
        scene = chain.ChainScene(indices=[2, 3])
        assert scene.covers(3)
        assert not scene.covers(4)


class TestRenderBlock:
    def test_renders_picked_lines_only(self):
        ordered = _talk()
        scenes = [chain.ChainScene(indices=[0, 1])]
        block = chain.render_block(ordered, scenes, TA)
        assert "我想去爬山" in block
        assert "那就周六" not in block

    def test_empty_chain_renders_nothing(self):
        assert chain.render_block(_talk(), [], TA) == ""

    def test_bot_line_is_labelled_as_you(self):
        ordered = _talk()
        scenes = [chain.ChainScene(indices=[3, 4, 5])]
        block = chain.render_block(ordered, scenes, TA, self_id=BOT)
        assert dialogue.LABEL_BOT in block

    def test_max_lines_keeps_the_tail(self):
        ordered = _talk()
        scenes = [chain.ChainScene(indices=[0, 1, 2, 3, 4, 5])]
        block = chain.render_block(ordered, scenes, TA, max_lines=2)
        assert "周末去哪玩" not in block
        assert "那就周六" in block


class TestSceneRows:
    def test_returns_the_whole_scene_for_any_member(self):
        ordered = _talk()
        scenes = [chain.ChainScene(indices=[0, 1, 2])]
        rows = chain.scene_rows(ordered, scenes, 1)
        assert [r["message_id"] for r in rows] == ["1", "2", "3"]

    def test_line_outside_every_scene_yields_nothing(self):
        ordered = _talk()
        scenes = [chain.ChainScene(indices=[0, 1])]
        assert chain.scene_rows(ordered, scenes, 5) == []

    def test_out_of_range_members_are_dropped(self):
        ordered = _talk()
        scenes = [chain.ChainScene(indices=[0, 999])]
        assert len(chain.scene_rows(ordered, scenes, 0)) == 1
