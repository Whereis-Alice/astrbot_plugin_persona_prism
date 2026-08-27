"""_backfill / _gather 的诊断记账。

纯文案分支在 test_scanning.py 里测过了，这里补的是"数字是不是真的对得上"：
翻了几页、看到多少条原始消息、新入库多少、断点有没有被正确推进。用假的协议端
和假的仓储直接驱动真实方法，不需要跑起 AstrBot。
"""

from __future__ import annotations

import asyncio
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


class FakeConfig:
    """按扁平路径取值的最小配置，默认值直接沿用 DEFAULTS。"""

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
    """只记账、不落盘的假仓储。"""

    def __init__(self, *, state: dict[str, Any] | None = None, corpus: int = 0) -> None:
        self.state = state or {"oldest_seq": "", "newest_seq": "", "exhausted": False}
        self.corpus = corpus
        self.writes: list[int] = []
        self.state_updates: list[dict[str, Any]] = []

    async def get_scan_state(self, platform: str, group_id: str) -> dict[str, Any]:
        return dict(self.state)

    async def set_scan_state(
        self,
        platform: str,
        group_id: str,
        *,
        oldest_seq: str = "",
        newest_seq: str = "",
        exhausted: bool = False,
    ) -> None:
        self.state_updates.append(
            {"oldest_seq": oldest_seq, "newest_seq": newest_seq, "exhausted": exhausted},
        )

    async def add_messages(self, platform: str, group_id: str, rows: list[Any]) -> int:
        self.writes.append(len(rows))
        self.corpus += len(rows)
        return len(rows)

    async def fetch_user_corpus(
        self,
        platform: str,
        group_id: str,
        user_id: str,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "message_id": f"m{index}",
                "user_id": user_id,
                "user_name": "狐狸",
                "text": f"这是第 {index} 条留档的发言，内容随便但够长",
                "ts": 1_700_000_000 + index,
                "is_reply": False,
            }
            for index in range(self.corpus)
        ]
        return rows[:limit] if limit else rows


def _page(start_seq: int, count: int, *, user_id: str = "42") -> list[dict[str, Any]]:
    """造一页 get_group_msg_history 风格的返回，seq 升序。"""
    return [
        {
            "message_id": str(start_seq + offset),
            "message_seq": start_seq + offset,
            "time": 1_700_000_000 + start_seq + offset,
            "sender": {"user_id": user_id, "nickname": "狐狸"},
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


def _star(config: FakeConfig, store: FakeStore) -> SimpleNamespace:
    """一个刚好够用的假 Star：真方法用 __get__ 绑到这个壳上。

    这样 _gather 内部对 self._scan_plan / self._backfill 的调用走的还是真实实现，
    只有配置和仓储被替换掉。
    """
    star = SimpleNamespace(
        config=config,
        astore=store,
        _is_own_command=lambda text: False,
    )
    star._scan_plan = main.PersonaPrismStar._scan_plan.__get__(star)
    star._backfill = main.PersonaPrismStar._backfill.__get__(star)
    return star


def _run_backfill(star: Any, event: Any, *, target_total: int = 400) -> Any:
    plan = main.PersonaPrismStar._scan_plan(star, "aiocqhttp", "10086")
    return asyncio.run(
        main.PersonaPrismStar._backfill(
            star,
            event,
            "aiocqhttp",
            "10086",
            "42",
            target_total=target_total,
            report=plan,
        ),
    )


# ---------------------------------------------------------------------------
# 计划底稿
# ---------------------------------------------------------------------------


def test_scan_plan_mirrors_config():
    star = _star(FakeConfig(**{"collect.backfill_rounds": 7, "collect.page_size": 150}), FakeStore())
    plan = main.PersonaPrismStar._scan_plan(star, "aiocqhttp", "10086")
    assert plan.supported is True
    assert plan.planned_rounds == 7
    assert plan.page_size == 150
    assert plan.attempted is False


def test_scan_plan_marks_private_chat_unsupported():
    star = _star(FakeConfig(), FakeStore())
    plan = main.PersonaPrismStar._scan_plan(star, "aiocqhttp", "")
    assert plan.is_group is False
    assert plan.supported is False


# ---------------------------------------------------------------------------
# 回溯记账
# ---------------------------------------------------------------------------


def test_backfill_counts_pages_and_writes():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 5, "collect.page_size": 20}), store)
    event = _event([_page(300, 20), _page(200, 20), _page(100, 20), []])
    report = _run_backfill(star, event)
    assert report.attempted is True
    assert report.pages == 3
    assert report.scanned == 60
    assert report.added == 60
    #: 第四轮拿到空页 → 判定挖到头，并写回 scan_state。
    assert report.exhausted is True
    assert report.error == ""
    assert store.state_updates[-1]["exhausted"] is True


def test_backfill_stops_once_local_corpus_is_enough():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 9, "collect.page_size": 20}), store)
    event = _event([_page(300, 20), _page(200, 20), _page(100, 20)])
    report = _run_backfill(star, event, target_total=25)
    #: 第二轮结束时本地已有 40 条 ≥ 25，不该再往前翻。
    assert report.pages == 2
    assert report.exhausted is False
    assert len(event.bot.api.calls) == 2


def test_backfill_records_protocol_error():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 5}), store)
    event = _event([RuntimeError("retcode 1200: unsupported action")])
    report = _run_backfill(star, event)
    assert report.attempted is True
    assert report.pages == 0
    assert "1200" in report.error
    assert report.blocked is True


def test_backfill_error_on_later_page_keeps_earlier_counts():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 5, "collect.page_size": 20}), store)
    event = _event([_page(300, 20), RuntimeError("timeout")])
    report = _run_backfill(star, event)
    assert report.pages == 1
    assert report.scanned == 20
    assert report.error == "timeout"


def test_backfill_skipped_when_rounds_is_zero():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 0}), store)
    event = _event([_page(300, 20)])
    report = _run_backfill(star, event)
    assert report.attempted is False
    assert report.planned_rounds == 0
    assert event.bot.api.calls == []


def test_backfill_without_client_reports_unsupported():
    store = FakeStore()
    star = _star(FakeConfig(), store)
    report = _run_backfill(star, _event(None))
    assert report.supported is False
    assert report.attempted is False


def test_backfill_degrades_to_topup_when_history_exhausted():
    store = FakeStore(state={"oldest_seq": "100", "newest_seq": "500", "exhausted": True})
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    event = _event([_page(600, 20), _page(700, 20)])
    report = _run_backfill(star, event)
    assert report.topup_only is True
    assert report.pages == 1
    assert len(event.bot.api.calls) == 1
    #: 补拉不该动断点，否则下次又要从头挖。
    assert store.state_updates == []


# ---------------------------------------------------------------------------
# _gather 的返回契约
# ---------------------------------------------------------------------------


def test_gather_returns_bundle_and_report():
    store = FakeStore()
    star = _star(
        FakeConfig(**{"collect.max_messages": 40, "collect.backfill_rounds": 3, "collect.page_size": 20}),
        store,
    )
    event = _event([_page(300, 20), _page(200, 20)])
    bundle, report = asyncio.run(
        main.PersonaPrismStar._gather(star, event, "aiocqhttp", "10086", "42"),
    )
    assert report.local_before == 0
    assert report.added == 40
    assert bundle.stats.sampled > 0
    assert bundle.from_cache is False


def test_gather_skips_backfill_when_cache_is_full():
    store = FakeStore(corpus=120)
    star = _star(FakeConfig(**{"collect.max_messages": 40}), store)
    event = _event([_page(300, 20)])
    bundle, report = asyncio.run(
        main.PersonaPrismStar._gather(star, event, "aiocqhttp", "10086", "42"),
    )
    assert report.attempted is False
    assert report.local_before == 120
    assert bundle.from_cache is True
    assert event.bot.api.calls == []


def test_gather_in_private_chat_never_calls_the_protocol():
    store = FakeStore(corpus=60)
    star = _star(FakeConfig(), store)
    event = _event([_page(300, 20)])
    _bundle, report = asyncio.run(
        main.PersonaPrismStar._gather(star, event, "aiocqhttp", "", "42"),
    )
    assert report.supported is False
    assert event.bot.api.calls == []
