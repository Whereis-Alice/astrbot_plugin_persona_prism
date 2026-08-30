"""_DialogueScope.window_hint：卡片气泡与提示词现场共用同一批群聊行。

模型圈出几场对话之后，证供还原不该再按时间硬切一次窗口 —— 那样卡片上的气泡
和模型读到的上下文就是两段不同的对话。这里验证取景函数的三种结果：命中场次、
不在任何场次、以及压根没有场次可用。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("astrbot", reason="需要已安装的 AstrBot 运行时")

from astrbot_plugin_persona_prism import main
from astrbot_plugin_persona_prism.prism.chain import ChainScene

TA = "1001"
BASE = 1_700_000_000


def _row(mid: str, uid: str, text: str, offset: int) -> dict[str, Any]:
    return {
        "message_id": mid,
        "user_id": uid,
        "user_name": "U" + uid,
        "text": text,
        "ts": BASE + offset,
    }


def _rows() -> list[dict[str, Any]]:
    return [
        _row("1", "2002", "有人在吗", 0),
        _row("2", "2003", "周末去修风车吗", 10),
        _row("3", TA, "去啊，零件都买好了", 20),
        _row("4", "2003", "那就星期六", 30),
        _row("5", "2004", "另一个话题", 900),
        _row("6", TA, "接另一个话题", 910),
    ]


def _scope(**kwargs: Any) -> main._DialogueScope:
    base: dict[str, Any] = {
        "rows": _rows(),
        "names": {},
        "chain_scenes": [ChainScene(indices=[1, 2, 3], why="约时间")],
    }
    base.update(kwargs)
    return main._DialogueScope(**base)


class TestWindowHint:
    def test_anchor_inside_a_scene_returns_the_whole_scene(self):
        hint = _scope().window_hint()
        assert hint is not None
        got = hint("3", BASE + 20)
        assert [row["message_id"] for row in got] == ["2", "3", "4"]

    def test_any_line_of_the_scene_resolves_to_the_same_scene(self):
        hint = _scope().window_hint()
        assert [row["message_id"] for row in hint("2", 0)] == ["2", "3", "4"]
        assert [row["message_id"] for row in hint("4", 0)] == ["2", "3", "4"]

    def test_anchor_outside_every_scene_returns_nothing(self):
        # 落在模型没圈的那一段里 —— 交回 scenes 走本地切法，别硬凑。
        hint = _scope().window_hint()
        assert hint("6", BASE + 910) == []

    def test_unknown_anchor_returns_nothing(self):
        hint = _scope().window_hint()
        assert hint("nope", 0) == []

    def test_timestamp_only_anchor_still_lands(self):
        # 协议端改过消息 ID 时退回按时间就近，仍能落回同一场。
        hint = _scope().window_hint()
        assert [row["message_id"] for row in hint("", BASE + 21)] == ["2", "3", "4"]

    def test_no_scenes_means_no_hint_at_all(self):
        assert _scope(chain_scenes=[]).window_hint() is None

    def test_no_rows_means_no_hint_at_all(self):
        assert _scope(rows=[]).window_hint() is None

    def test_default_scope_is_empty_and_harmless(self):
        assert main._DialogueScope().window_hint() is None

    def test_out_of_range_indices_are_skipped(self):
        scope = _scope(chain_scenes=[ChainScene(indices=[2, 99], why="越界")])
        hint = scope.window_hint()
        assert [row["message_id"] for row in hint("3", 0)] == ["3"]
