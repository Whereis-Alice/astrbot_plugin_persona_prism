"""_cover_window：把「这段时间的发言」真正翻全。

恋爱诊断这类按时间窗结算的玩法，只补拉最新一页是不够的 —— 一页是全群最新的
page_size 条，本人今天说的话很容易被别人挤到一页之外，于是窗口里筛出 0 条。
这里用假协议端驱动真实方法，验证「按时间收敛」的停手条件、节流、换游标写法，
以及它不该去动画像用的深挖断点。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("astrbot", reason="需要已安装的 AstrBot 运行时")

from astrbot_plugin_persona_prism import main
from astrbot_plugin_persona_prism.prism.config import DEFAULTS

FLAT_DEFAULTS = {
    f"{group}.{key}": value
    for group, items in DEFAULTS.items()
    for key, value in items.items()
}

#: 窗口起点：假消息的时间戳围着它排布。
START = 1_700_000_000


class FakeConfig:
    def __init__(self, **overrides: Any) -> None:
        self.values = dict(FLAT_DEFAULTS)
        self.values.update(overrides)

    def int_of(self, key: str) -> int:
        return int(self.values[key])

    def bool_of(self, key: str) -> bool:
        return bool(self.values[key])

    def str_of(self, key: str) -> str:
        return str(self.values[key])


class FakeStore:
    def __init__(self, **state: Any) -> None:
        base = {
            "oldest_seq": "",
            "newest_seq": "",
            "exhausted": False,
            "cursor_field": "",
            "depth_pages": 0,
        }
        base.update(state)
        self.state = base
        self.writes: list[int] = []
        self.state_updates: list[dict[str, Any]] = []

    async def get_scan_state(self, platform: str, group_id: str) -> dict[str, Any]:
        return dict(self.state)

    async def set_scan_state(self, *args: Any, **kwargs: Any) -> None:
        self.state_updates.append(dict(kwargs))

    async def add_messages(self, platform: str, group_id: str, rows: list[Any]) -> int:
        self.writes.append(len(rows))
        return len(rows)


def _page(start_seq: int, count: int, *, base_ts: int) -> list[dict[str, Any]]:
    """造一页群历史，seq 与时间同向升序（协议端就是这么返回的）。"""
    return [
        {
            "message_id": str(start_seq + offset),
            "message_seq": start_seq + offset,
            "time": base_ts + offset,
            "sender": {"user_id": "42", "nickname": "狐狸"},
            "message": [
                {
                    "type": "text",
                    "data": {"text": f"第 {start_seq + offset} 条历史消息，说点有内容的话"},
                },
            ],
        }
        for offset in range(count)
    ]


class FakeApi:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append({"action": action, **kwargs})
        if not self.pages:
            return {"messages": []}
        nxt = self.pages.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return {"messages": nxt}


def _event(pages: list[Any] | None) -> SimpleNamespace:
    if pages is None:
        return SimpleNamespace(bot=None)
    return SimpleNamespace(bot=SimpleNamespace(api=FakeApi(pages)))


def _star(config: FakeConfig | None = None, store: FakeStore | None = None) -> SimpleNamespace:
    star = SimpleNamespace(
        config=config or FakeConfig(),
        astore=store or FakeStore(),
        _is_own_command=lambda text: False,
    )
    star._param_style = {}
    star._window_scan = {}
    for name in (
        "_cover_window",
        "_rotate_window_cursor",
        "_ingest_history_page",
        "_fetch_history_page",
    ):
        setattr(star, name, getattr(main.PersonaPrismStar, name).__get__(star))
    star._page_oldest_ts = main.PersonaPrismStar._page_oldest_ts
    return star


def _cover(star: Any, event: Any, start_ts: int = START) -> tuple[int, int, bool]:
    return asyncio.run(star._cover_window(event, "aiocqhttp", "10086", start_ts))


# -------------------------------------------------------------------------
# 按时间收敛
# -------------------------------------------------------------------------


def test_one_page_is_enough_when_it_reaches_past_the_window():
    #: 首页最早一条就早于窗口起点 → 窗口已被盖满，不该再出网。
    star = _star()
    event = _event([_page(300, 20, base_ts=START - 100)])
    pages, added, covered = _cover(star, event)
    assert (pages, added, covered) == (1, 20, True)
    assert len(event.bot.api.calls) == 1


def test_keeps_paging_in_a_busy_group_until_the_window_is_covered():
    #: 前两页都还在窗口内（别人刷了很多），第三页才翻过窗口起点。
    star = _star(FakeConfig(**{"collect.backfill_rounds": 30, "collect.page_size": 20}))
    event = _event(
        [
            _page(300, 20, base_ts=START + 400),
            _page(200, 20, base_ts=START + 200),
            _page(100, 20, base_ts=START - 50),
            _page(50, 20, base_ts=START - 500),
        ],
    )
    pages, added, covered = _cover(star, event)
    assert pages == 3
    assert added == 60
    assert covered is True
    #: 第四页不该被请求 —— 第三页已经证明窗口盖满了。
    assert len(event.bot.api.calls) == 3


def test_running_out_of_rounds_reports_not_covered():
    #: 轮数用完但还没翻到窗口起点 → covered=False，好让上层说清「可能不全」。
    star = _star(FakeConfig(**{"collect.backfill_rounds": 2, "collect.page_size": 20}))
    event = _event(
        [
            _page(300, 20, base_ts=START + 400),
            _page(200, 20, base_ts=START + 200),
            _page(100, 20, base_ts=START - 50),
        ],
    )
    pages, _added, covered = _cover(star, event)
    assert pages == 2
    assert covered is False


def test_empty_page_counts_as_covered():
    #: 群历史到头了，能覆盖的都覆盖了，不该再提示「可能不全」。
    star = _star()
    event = _event([[]])
    assert _cover(star, event) == (0, 0, True)


def test_history_error_is_swallowed_and_reported_as_incomplete():
    star = _star()
    event = _event([RuntimeError("retcode 1200: unsupported action")])
    assert _cover(star, event) == (0, 0, False)


# -------------------------------------------------------------------------
# 边界与节流
# -------------------------------------------------------------------------


def test_private_chat_and_missing_client_are_no_ops():
    star = _star()
    assert asyncio.run(star._cover_window(_event([]), "aiocqhttp", "", START)) == (0, 0, False)
    assert _cover(_star(), _event(None)) == (0, 0, False)


def test_second_call_within_ttl_reuses_the_cached_verdict():
    star = _star()
    event = _event([_page(300, 20, base_ts=START - 100)])
    assert _cover(star, event)[2] is True
    #: 同一个窗口紧接着再问一次（排行榜跟着单人诊断）→ 直接吃缓存，不再出网。
    assert _cover(star, event) == (0, 0, True)
    assert len(event.bot.api.calls) == 1


def test_expired_cache_scans_again():
    star = _star()
    event = _event([_page(300, 20, base_ts=START - 100), _page(200, 20, base_ts=START - 200)])
    _cover(star, event)
    key = next(iter(star._window_scan))
    star._window_scan[key] = (time.time() - main.WINDOW_SCAN_TTL - 1, True)
    assert _cover(star, event)[0] == 1
    assert len(event.bot.api.calls) == 2


def test_a_different_window_is_cached_separately():
    star = _star()
    event = _event([_page(300, 20, base_ts=START - 100), _page(200, 20, base_ts=START - 100)])
    _cover(star, event, START)
    #: 换成 7 天窗 → 另一个 key，要重新翻。
    assert _cover(star, event, START - 6 * 86400)[0] == 1


def test_window_scan_never_touches_the_portrait_cursor():
    #: 深挖断点是画像那条路的家当，窗口回溯只借读 cursor_field，不许写回。
    store = FakeStore(oldest_seq="100", cursor_field="id_first", depth_pages=9)
    star = _star(store=store)
    _cover(star, _event([_page(300, 20, base_ts=START - 100)]))
    assert store.state_updates == []
    assert store.state["oldest_seq"] == "100"


def test_window_scan_starts_from_the_latest_page():
    #: 哪怕库里有很深的断点，窗口回溯也要从最新一页起步，否则会漏掉今天的话。
    store = FakeStore(oldest_seq="100", depth_pages=9)
    star = _star(store=store)
    event = _event([_page(300, 20, base_ts=START - 100)])
    _cover(star, event)
    first = event.bot.api.calls[0]
    assert first.get("message_seq") in (0, None)


# -------------------------------------------------------------------------
# 游标翻不动时换写法
# -------------------------------------------------------------------------


def test_repeated_page_rotates_the_cursor_strategy():
    #: 协议端把游标当摆设、反复回同一批 → 换一种取锚点的方式再试。
    star = _star(FakeConfig(**{"collect.backfill_rounds": 5, "collect.page_size": 20}))
    same = _page(300, 20, base_ts=START + 400)
    event = _event([same, list(same), _page(100, 20, base_ts=START - 50)])
    pages, _added, covered = _cover(star, event)
    #: 中间那页整页重复，不计页数；换写法后第三页翻过了窗口起点。
    assert pages == 2
    assert covered is True


def test_locked_cursor_field_does_not_rotate():
    #: 管理员锁死了写法就尊重他，翻不动直接收工，别偷偷换。
    star = _star(
        FakeConfig(**{"collect.cursor_field": "seq_first", "collect.backfill_rounds": 5}),
    )
    same = _page(300, 20, base_ts=START + 400)
    event = _event([same, list(same), _page(100, 20, base_ts=START - 50)])
    pages, _added, covered = _cover(star, event)
    assert pages == 1
    assert covered is False


# -------------------------------------------------------------------------
# 入库
# -------------------------------------------------------------------------


def test_ingest_page_filters_our_own_commands():
    star = _star()
    star._is_own_command = lambda text: "恋爱诊断" in text
    page = _page(300, 3, base_ts=START)
    page[0]["message"][0]["data"]["text"] = "恋爱诊断 @某人"
    added = asyncio.run(star._ingest_history_page("aiocqhttp", "10086", page))
    assert added == 2


def test_ingest_page_returns_zero_when_nothing_survives_cleaning():
    star = _star()
    star._is_own_command = lambda text: True
    page = _page(300, 3, base_ts=START)
    assert asyncio.run(star._ingest_history_page("aiocqhttp", "10086", page)) == 0
    assert star.astore.writes == []


def test_page_oldest_ts_folds_millisecond_stamps():
    page = _page(300, 3, base_ts=START)
    for row in page:
        row["time"] = row["time"] * 1000
    assert main.PersonaPrismStar._page_oldest_ts(page) == START
    assert main.PersonaPrismStar._page_oldest_ts([]) == 0

# -------------------------------------------------------------------------
# 窗口没盖满时的说法
# -------------------------------------------------------------------------


def test_shortfall_note_says_so_when_the_window_was_not_covered():
    #: 翻不到窗口起点时，「只说了 N 句」本身就不可信，提示要如实说明而不是怪用户。
    star = _star()
    note = asyncio.run(
        main.PersonaPrismStar._corpus_shortfall_note(star, "aiocqhttp", "10086", 0, covered=False),
    )
    assert "没读全" in note
    assert "再试一次" in note
    #: 玩家看不懂也用不了的管理命令不往群里贴。
    assert "棱镜诊断" not in note
