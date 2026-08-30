"""_backfill / _gather 的诊断记账。

纯文案分支在 test_scanning.py 里测过了，这里补的是"数字是不是真的对得上"：
翻了几页、看到多少条原始消息、新入库多少、断点有没有被正确推进。用假的协议端
和假的仓储直接驱动真实方法，不需要跑起 AstrBot。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, ClassVar

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
        base = {
            "oldest_seq": "",
            "newest_seq": "",
            "exhausted": False,
            "cursor_field": "",
            "depth_pages": 0,
        }
        base.update(state or {})
        self.state = base
        self.corpus = corpus
        self.writes: list[int] = []
        self.state_updates: list[dict[str, Any]] = []
        self.resets: list[tuple[str, str]] = []

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
        cursor_field: str = "",
        depth_pages: int = -1,
    ) -> None:
        self.state_updates.append(
            {
                "oldest_seq": oldest_seq,
                "newest_seq": newest_seq,
                "exhausted": exhausted,
                "cursor_field": cursor_field,
                "depth_pages": depth_pages,
            },
        )

    async def reset_scan_state(self, platform: str = "", group_id: str = "") -> int:
        self.resets.append((platform, group_id))
        return 1

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
        _is_plugin_echo=lambda text: False,
    )
    star._param_style = {}
    star._scan_plan = main.PersonaPrismStar._scan_plan.__get__(star)
    star._backfill = main.PersonaPrismStar._backfill.__get__(star)
    star._fetch_history_page = main.PersonaPrismStar._fetch_history_page.__get__(star)
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
    #: 断点够深（≥ TRUSTED_DEPTH）且刚判定过 → 相信「已挖到头」，只补拉最新一页。
    store = FakeStore(
        state={
            "oldest_seq": "100",
            "newest_seq": "500",
            "exhausted": True,
            "depth_pages": 5,
            "last_scan": int(time.time()),
        },
    )
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    event = _event([_page(600, 20), _page(700, 20)])
    report = _run_backfill(star, event)
    assert report.topup_only is True
    assert report.exhausted_recheck == ""
    assert report.pages == 1
    assert len(event.bot.api.calls) == 1
    #: 补拉不该动断点，否则下次又要从头挖。
    assert store.state_updates == []


def test_backfill_rechecks_shallow_exhausted_mark():
    """浅断点上的「已挖到头」不可信 —— 那正是"就一个活跃群采不到"的成因。"""
    store = FakeStore(state={"oldest_seq": "100", "newest_seq": "500", "exhausted": True})
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    event = _event([_page(600, 20), _page(700, 20)])
    report = _run_backfill(star, event)
    assert report.exhausted_recheck == "shallow"
    #: 复查时不能退化成补拉，要真的往前翻。
    assert report.topup_only is False
    assert report.pages == 2
    assert store.state_updates


def test_backfill_trusts_deep_and_fresh_exhausted_mark():
    store = FakeStore(
        state={"oldest_seq": "100", "exhausted": True, "depth_pages": 9, "last_scan": int(time.time())},
    )
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    event = _event([_page(600, 20)])
    report = _run_backfill(star, event)
    assert report.exhausted_recheck == ""
    assert report.topup_only is True


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


# ---------------------------------------------------------------------------
# 翻页方式自适应（v1.1.3 起，v1.1.4 扩成四种组合，v1.3.0 再加两种时间戳兜底）
# ---------------------------------------------------------------------------


def _dual_page(start_seq: int, count: int, *, user_id: str = "42") -> list[dict[str, Any]]:
    """造一页 message_id 和 message_seq 是两套编号的历史返回。

    真实世界里这两个字段常常互不相干，而 get_group_msg_history 的翻页参数只认其中
    一种。用同一个数值的假数据是测不出这个 bug 的。
    """
    page = _page(start_seq, count, user_id=user_id)
    for row in page:
        row["message_id"] = str(900_000 + int(row["message_seq"]))
    return page


class IdOnlyApi:
    """模拟"翻页参数其实是 message_id"的协议端。

    传 message_seq 的值它认不出来，于是**不报错**，直接原地返回最新一页 ——
    这正是用户现场遇到的现象（我们只捞到 6 条，上游捞到 91 条）。
    """

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append({"action": action, **kwargs})
        cursor = int(kwargs.get("message_seq") or 0)
        if cursor == 0:
            return {"messages": self.pages[0]}
        for index, page in enumerate(self.pages):
            if int(page[0]["message_id"]) == cursor:
                nxt = index + 1
                return {"messages": self.pages[nxt] if nxt < len(self.pages) else []}
        #: 认不出来的游标 → 原地返回最新一页。
        return {"messages": self.pages[0]}


class FrozenApi:
    """无论游标是什么都返回同一页：协议端压根没实现分页。"""

    def __init__(self, page: list[dict[str, Any]]) -> None:
        self.page = page
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append({"action": action, **kwargs})
        return {"messages": self.page}


def test_backfill_switches_cursor_field_when_seq_does_not_page():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    pages = [_dual_page(300, 20), _dual_page(200, 20), _dual_page(100, 20)]
    api = IdOnlyApi(pages)
    event = SimpleNamespace(bot=SimpleNamespace(api=api))
    report = _run_backfill(star, event)
    #: 关键：不能因为"第二页看起来一样"就判定挖到头，而要换 message_id 继续挖。
    assert report.cursor_switched is True
    assert report.cursor_field == "id_first"
    assert report.stalled is False
    assert report.pages == 3
    assert report.scanned == 60
    #: 实测可用的游标字段要落库，下次不用再试错。
    cursor_writes = [item for item in store.state_updates if item["cursor_field"]]
    assert cursor_writes[-1]["cursor_field"] == "id_first"
    assert cursor_writes[-1]["depth_pages"] == 3


def test_backfill_marks_stalled_instead_of_locking_exhausted():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    api = FrozenApi(_dual_page(300, 20))
    event = SimpleNamespace(bot=SimpleNamespace(api=api))
    report = _run_backfill(star, event)
    assert report.stalled is True
    #: 翻页卡住 ≠ 历史挖完。写 exhausted 会让这个群以后永远只补拉最新一页。
    assert report.exhausted is False
    assert all(update["exhausted"] is False for update in store.state_updates)
    #: 六种翻页方式都试过就收手，不要拿满配额去空转。
    #: 首页 1 次 + 六种各 1 次 = 7 次请求（最后一种失败后无路可换，直接认输）。
    assert len(api.calls) == 7


def test_backfill_respects_manually_locked_cursor_field():
    store = FakeStore()
    star = _star(
        FakeConfig(
            **{
                "collect.backfill_rounds": 4,
                "collect.page_size": 20,
                "collect.cursor_field": "message_id",
            },
        ),
        store,
    )
    pages = [_dual_page(300, 20), _dual_page(200, 20)]
    api = IdOnlyApi(pages)
    event = SimpleNamespace(bot=SimpleNamespace(api=api))
    report = _run_backfill(star, event)
    #: 锁死了就一次都不该试探，第一页之后直接用 message_id。
    #: v1.1.3 的旧值 message_id 要能继续用，归一成新名字 id_first。
    assert report.cursor_field == "id_first"
    assert report.cursor_switched is False
    assert report.pages == 2


def test_backfill_reuses_remembered_cursor_field():
    #: 库里存的是 v1.1.3 写下的旧名字，升级后不能因此重新试错一轮。
    store = FakeStore(state={"cursor_field": "message_id"})
    star = _star(FakeConfig(**{"collect.backfill_rounds": 4, "collect.page_size": 20}), store)
    pages = [_dual_page(300, 20), _dual_page(200, 20)]
    api = IdOnlyApi(pages)
    event = SimpleNamespace(bot=SimpleNamespace(api=api))
    report = _run_backfill(star, event)
    assert report.cursor_field == "id_first"
    assert report.cursor_switched is False
    assert report.pages == 2


def test_backfill_reuses_new_style_remembered_strategy():
    store = FakeStore(state={"cursor_field": "id_first"})
    star = _star(FakeConfig(**{"collect.backfill_rounds": 4, "collect.page_size": 20}), store)
    api = IdOnlyApi([_dual_page(300, 20), _dual_page(200, 20)])
    event = SimpleNamespace(bot=SimpleNamespace(api=api))
    report = _run_backfill(star, event)
    assert report.cursor_field == "id_first"
    assert report.cursor_switched is False
    assert report.pages == 2


class ReversedIdApi:
    """模拟"返回的一页是最新在前、翻页参数认 message_id"的协议端。

    这正是 v1.1.3 漏掉的那一维自由度：就算字段猜对了也翻不动，因为"本页最旧的
    那一条"在数组末尾而不是开头。上游和 v1.1.3 都只看 messages[0]。
    """

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append({"action": action, **kwargs})
        cursor = int(kwargs.get("message_seq") or 0)
        if cursor == 0:
            return {"messages": self.pages[0]}
        for index, page in enumerate(self.pages):
            if int(page[-1]["message_id"]) == cursor:
                nxt = index + 1
                return {"messages": self.pages[nxt] if nxt < len(self.pages) else []}
        return {"messages": self.pages[0]}


def test_backfill_finds_strategy_that_needs_the_last_row():
    """六种组合里那些靠后的也要能被试出来（用户现场的 6 条 vs 91 条）。"""
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    pages = [list(reversed(_dual_page(start, 20))) for start in (300, 200, 100)]
    api = ReversedIdApi(pages)
    event = SimpleNamespace(bot=SimpleNamespace(api=api))
    report = _run_backfill(star, event)
    assert report.cursor_switched is True
    assert report.cursor_field == "id_last"
    assert report.stalled is False
    assert report.pages == 3
    assert report.scanned == 60
    cursor_writes = [item for item in store.state_updates if item["cursor_field"]]
    assert cursor_writes[-1]["cursor_field"] == "id_last"
    assert cursor_writes[-1]["depth_pages"] == 3


def test_backfill_first_page_empty_does_not_lock_exhausted():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 5}), store)
    event = _event([[]])
    report = _run_backfill(star, event)
    assert report.exhausted is True
    #: 刚进群 / 历史被清理都会返回空页，别把这种群永久标记成"挖到头"。
    assert store.state_updates == []


def test_backfill_reports_depth_before_from_state():
    store = FakeStore(state={"oldest_seq": "300", "depth_pages": 7})
    star = _star(FakeConfig(**{"collect.backfill_rounds": 2, "collect.page_size": 20}), store)
    event = _event([_page(200, 20), _page(100, 20)])
    report = _run_backfill(star, event)
    assert report.depth_before == 7
    #: 断点深度要接着累加，而不是每次从 0 重新数。
    assert store.state_updates[-1]["depth_pages"] == 9


class SeqBlindApi:
    """传 message_seq 的值直接回一页空数组（协议端只认 message_id）。

    这就是「ある人」那个群的现场：第 2 页一空，v1.1.4 及以前就把 exhausted 永久
    写库，此后这个群只补拉最新一页，语料永远停在两百来条。
    """

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append({"action": action, **kwargs})
        cursor = int(kwargs.get("message_seq") or 0)
        if cursor == 0:
            return {"messages": self.pages[0]}
        for index, page in enumerate(self.pages):
            if int(page[0]["message_id"]) == cursor:
                nxt = index + 1
                return {"messages": self.pages[nxt] if nxt < len(self.pages) else []}
        return {"messages": []}


def test_backfill_rotates_cursor_field_when_page_comes_back_empty():
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    api = SeqBlindApi([_dual_page(300, 20), _dual_page(200, 20), _dual_page(100, 20)])
    event = SimpleNamespace(bot=SimpleNamespace(api=api))
    report = _run_backfill(star, event)
    #: 空页不等于挖到头 —— 先把没验过的翻页方式试完。
    assert report.cursor_switched is True
    assert report.cursor_field == "id_first"
    assert report.pages == 3
    assert report.scanned == 60


def test_backfill_does_not_rotate_after_cursor_proved_itself():
    """反过来：已经靠这个方式翻过几页了，空页就该按"真的到头"处理。"""
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 8, "collect.page_size": 20}), store)
    event = _event([_page(300, 20), _page(200, 20), _page(100, 20), []])
    report = _run_backfill(star, event)
    assert report.cursor_switched is False
    assert report.cursor_field == "seq_first"
    assert report.exhausted is True
    assert report.restarted is False
    #: 不该在空页之后再多打四次试探请求。
    assert len(event.bot.api.calls) == 4


class StaleCursorApi:
    """库里存的断点是上个协议端留下的：拿它翻只回空页，从最新一页重挖就正常。"""

    def __init__(self, pages: list[list[dict[str, Any]]], bad_cursor: int) -> None:
        self.pages = pages
        self.bad_cursor = bad_cursor
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append({"action": action, **kwargs})
        cursor = int(kwargs.get("message_seq") or 0)
        if cursor == self.bad_cursor:
            return {"messages": []}
        if cursor == 0:
            return {"messages": self.pages[0]}
        for index, page in enumerate(self.pages):
            if int(page[0]["message_seq"]) == cursor:
                nxt = index + 1
                return {"messages": self.pages[nxt] if nxt < len(self.pages) else []}
        return {"messages": []}


def test_backfill_restarts_when_remembered_cursor_is_dead():
    store = FakeStore(state={"oldest_seq": "9999", "depth_pages": 4})
    star = _star(FakeConfig(**{"collect.backfill_rounds": 4, "collect.page_size": 20}), store)
    api = StaleCursorApi([_page(300, 20), _page(200, 20)], bad_cursor=9999)
    event = SimpleNamespace(bot=SimpleNamespace(api=api))
    report = _run_backfill(star, event)
    assert report.restarted is True
    assert report.pages == 2
    #: 重挖是从最新一页开始的，深度要归零重新数。
    depth_writes = [item["depth_pages"] for item in store.state_updates if item["depth_pages"] >= 0]
    assert depth_writes == [1, 2]


# ---------------------------------------------------------------------------
# 协议端兼容：只认 message_id 的 SnowLuma / 拒收未知参数的严格实现
# ---------------------------------------------------------------------------


class SnowLumaApi:
    """只认 message_id 锚点的协议端，SnowLuma 就是这一类。

    行为按其源码复刻：message_seq 一律无视；message_id=0 取最新一页；锚点必须是它
    自己发出过的 message_id，否则返回空数组（不报错、也不退回最新页）；返回升序，
    锚点本身包含在这一页的末尾。
    """

    def __init__(self, total: int = 60, page: int = 20) -> None:
        #: message_id 用负数，贴近真实实现里的 signed int32 hash。
        self.timeline = [
            {
                "message_id": -(1000 + index),
                "message_seq": 500 + index,
                "time": 1_700_000_000 + index,
                "sender": {"user_id": "42", "nickname": "狐狸"},
                "message": [
                    {"type": "text", "data": {"text": f"第 {index} 条历史消息，说点有内容的话"}},
                ],
            }
            for index in range(total)
        ]
        self.page = page
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append({"action": action, **kwargs})
        anchor = int(kwargs.get("message_id") or 0)
        if anchor == 0:
            return {"messages": self.timeline[-self.page :]}
        found = next(
            (pos for pos, row in enumerate(self.timeline) if row["message_id"] == anchor),
            None,
        )
        if found is None:
            return {"messages": []}
        start = max(0, found + 1 - self.page)
        return {"messages": self.timeline[start : found + 1]}


def test_backfill_adapts_to_message_id_only_protocol():
    """SnowLuma 这类协议端：默认的 seq 游标一翻就空，应当自愈到 message_id。"""
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 4, "collect.page_size": 20}), store)
    api = SnowLumaApi()
    report = _run_backfill(star, SimpleNamespace(bot=SimpleNamespace(api=api)))
    assert report.error == ""
    assert report.cursor_switched is True
    assert report.cursor_field == "id_first"
    assert report.pages == 4
    #: 60 条时间线全部翻到了；每页含锚点本身，所以相邻页重叠 1 条，
    #: 63 = 60 + 3 个重叠锚点（真实库按 message_id 去重，假库只记条数）。
    assert report.scanned == 63
    assert sum(store.writes) == 63
    #: 可用的方式要落库，下次回溯直接用，不必再空翻一次。
    assert "id_first" in [item["cursor_field"] for item in store.state_updates]


def test_backfill_sends_both_anchor_names_by_default():
    star = _star(FakeConfig(**{"collect.backfill_rounds": 1, "collect.page_size": 20}), FakeStore())
    api = SnowLumaApi()
    _run_backfill(star, SimpleNamespace(bot=SimpleNamespace(api=api)))
    first = api.calls[0]
    assert first["message_seq"] == 0
    assert first["message_id"] == 0
    assert first["reverseOrder"] is True
    assert first["reverse_order"] is True


class StrictSeqApi:
    """只接受 message_seq 一套参数、收到未知键就报错的严格协议端。"""

    ALLOWED: ClassVar[set[str]] = {"group_id", "count", "message_seq", "reverseOrder"}

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.calls.append({"action": action, **kwargs})
        unknown = sorted(set(kwargs) - self.ALLOWED)
        if unknown:
            raise ValueError(f"unknown parameter: {unknown}")
        cursor = int(kwargs.get("message_seq") or 0)
        if cursor == 0:
            return {"messages": self.pages[0]}
        for index, page in enumerate(self.pages):
            if int(page[0]["message_seq"]) == cursor:
                nxt = index + 1
                return {"messages": self.pages[nxt] if nxt < len(self.pages) else []}
        return {"messages": []}


def test_backfill_falls_back_when_extra_params_rejected():
    """严格实现会拒收 message_id，此时应退回只写 message_seq 而不是整体失败。"""
    store = FakeStore()
    star = _star(FakeConfig(**{"collect.backfill_rounds": 3, "collect.page_size": 20}), store)
    api = StrictSeqApi([_page(300, 20), _page(280, 20), _page(260, 20)])
    report = _run_backfill(star, SimpleNamespace(bot=SimpleNamespace(api=api)))
    assert report.error == ""
    assert report.pages == 3
    assert star._param_style["aiocqhttp"] == "seq"
    #: 只在第一次请求上试过 dual 写法，之后不再重复试错。
    assert len([call for call in api.calls if "message_id" in call]) == 1

