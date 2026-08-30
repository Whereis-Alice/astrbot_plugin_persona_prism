"""prism.history：六种翻页方式与「棱镜诊断」探针。

这个模块是纯函数 + 一个只依赖注入式 fetch 的协程，可以完全脱离 AstrBot 单测。
用户现场那个"我们只捞到 6 条、上游捞到 91 条"的问题就出在翻页方式选错，所以这里
把六种组合、别名兼容、以及探针的每个分支都盯死。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot_plugin_persona_prism.prism import history


def _row(seq: Any, mid: Any, ts: int) -> dict[str, Any]:
    return {"message_seq": seq, "message_id": mid, "time": ts}


def _asc_page(base: int, count: int = 3) -> list[dict[str, Any]]:
    """最旧在前的一页：seq 与 id 是两套编号。"""
    return [
        _row(base + offset, 900_000 + base + offset, 1_700_000_000 + base + offset)
        for offset in range(count)
    ]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def test_strategies_cover_both_degrees_of_freedom():
    assert history.STRATEGIES == (
        "seq_first",
        "id_first",
        "seq_last",
        "id_last",
        "seq_oldest",
        "id_oldest",
    )
    #: 每一种都要有给用户看的中文标签，否则诊断里会漏字。
    assert set(history.STRATEGY_LABELS) == set(history.STRATEGIES)


def test_to_int_accepts_negative_and_rejects_junk():
    #: NapCat 的 message_id 可能是负数，用 str.isdigit() 会把整条丢掉。
    assert history.to_int("-1234") == -1234
    assert history.to_int(" 88 ") == 88
    assert history.to_int(True) is None
    assert history.to_int("abc") is None
    assert history.to_int(None) is None


def test_read_cursor_field_selection():
    row = {"message_seq": 5, "message_id": 900_005}
    assert history.read_cursor(row, "message_seq") == 5
    assert history.read_cursor(row, "message_id") == 900_005
    #: real_seq 是部分实现给 message_seq 起的别名。
    assert history.read_cursor({"real_seq": "7"}, "message_seq") == 7
    #: 字段留空时才做顺序探测，只当兜底用。
    assert history.read_cursor(row) == 5
    assert history.read_cursor("不是字典", "message_seq") is None


def test_normalize_strategy_maps_legacy_names():
    assert history.normalize_strategy("message_seq") == "seq_first"
    assert history.normalize_strategy("message_id") == "id_first"
    assert history.normalize_strategy(" id_last ") == "id_last"
    #: auto / 空 / 乱填都退化成"没有指定"，由调用方自己试探。
    assert history.normalize_strategy("auto") == ""
    assert history.normalize_strategy("") == ""
    assert history.normalize_strategy(None) == ""
    assert history.normalize_strategy("random") == ""


def test_strategy_label_falls_back_to_raw_value():
    assert "取本页第一条" in history.strategy_label("message_seq")
    assert history.strategy_label("auto") == "auto"
    assert history.strategy_label("") == "未知"


def test_cursor_of_covers_four_combinations():
    page = _asc_page(300)
    assert history.cursor_of(page, "seq_first") == 300
    assert history.cursor_of(page, "id_first") == 900_300
    assert history.cursor_of(page, "seq_last") == 302
    assert history.cursor_of(page, "id_last") == 900_302
    #: 没指定策略就算不出游标 —— 不要偷偷猜。
    assert history.cursor_of(page, "auto") is None
    assert history.cursor_of([], "seq_first") is None


def test_cursor_of_returns_none_when_field_missing():
    page = [{"message_id": "5"}]
    assert history.cursor_of(page, "seq_first") is None
    assert history.cursor_of(page, "id_first") == 5


def test_anchor_of_picks_the_right_end():
    page = _asc_page(300)
    assert history.anchor_of(page, "seq_first") is page[0]
    assert history.anchor_of(page, "id_last") is page[-1]
    assert history.anchor_of([], "seq_first") is None


def test_anchor_of_oldest_uses_timestamps_not_position():
    #: \u6709\u7684\u534f\u8bae\u7aef\u8fd4\u56de\u7684\u4e00\u9875\u538b\u6839\u6ca1\u6392\u5e8f\uff0c\u9996\u5c3e\u90fd\u4e0d\u662f\u6700\u65e7\u7684\u90a3\u6761\u3002
    page = [
        {"message_seq": 20, "message_id": 900_020, "time": 300},
        {"message_seq": 11, "message_id": 900_011, "time": 100},
        {"message_seq": 15, "message_id": 900_015, "time": 200},
    ]
    assert history.anchor_of(page, "seq_oldest") is page[1]
    assert history.cursor_of(page, "seq_oldest") == 11
    assert history.cursor_of(page, "id_oldest") == 900_011
    #: \u6ca1\u6709\u4efb\u4f55\u65f6\u95f4\u6233\u65f6\u9000\u56de\u7b2c\u4e00\u6761\uff0c\u4e0d\u80fd\u76f4\u63a5\u62a5\u9519\u3002
    assert history.anchor_of([{"message_seq": 3}], "seq_oldest") == {"message_seq": 3}


def test_page_ids_prefers_message_id_then_falls_back():
    page = [{"message_id": "a"}, {"message_seq": 2}, {"real_seq": 3}, {"foo": 1}, "脏数据"]
    assert history.page_ids(page) == {"a", "2", "3"}
    assert history.page_ids(None) == set()


def test_page_time_range_ignores_missing_timestamps():
    page = [{"time": 30}, {"time": 0}, {"time": 10}, {}]
    assert history.page_time_range(page) == (10, 30)
    assert history.page_time_range([{}]) == (0, 0)


def test_rotate_strategies_skips_current_and_tried():
    assert history.rotate_strategies("seq_first", set()) == [
        "id_first",
        "seq_last",
        "id_last",
        "seq_oldest",
        "id_oldest",
    ]
    assert history.rotate_strategies("id_first", {"seq_first", "seq_last", "seq_oldest", "id_oldest"}) == [
        "id_last",
    ]
    assert history.rotate_strategies("id_last", set(history.STRATEGIES)) == []


# ---------------------------------------------------------------------------
# 探针
# ---------------------------------------------------------------------------


class FakeProtocol:
    """一个只认某一种翻页方式的假协议端。"""

    def __init__(self, accepts: str, *, reverse: bool = False) -> None:
        self.accepts = accepts
        self.reverse = reverse
        self.cursors: list[int] = []
        self.base = _asc_page(300)
        self.older = _asc_page(200)
        if reverse:
            self.base = list(reversed(self.base))
            self.older = list(reversed(self.older))

    async def __call__(self, cursor: int) -> Any:
        self.cursors.append(cursor)
        if cursor == 0:
            return self.base
        return self.older if cursor == history.cursor_of(self.base, self.accepts) else self.base


def test_probe_identifies_the_working_strategy():
    fetch = FakeProtocol("id_last", reverse=True)
    report = asyncio.run(history.probe_pagination(fetch))
    assert report.ok is True
    assert report.winner == "id_last"
    #: 首页信息要齐全，用户得能肉眼比对 seq 和 id 是不是同一套编号。
    assert report.base_total == 3
    assert report.first_seq == 302
    assert report.last_id == 900_300
    assert "message_seq" in report.base_keys
    advanced = [item.strategy for item in report.attempts if item.advanced]
    assert advanced == ["id_last"]
    #: 六种都要有交代（试过、跳过或报错），不能悄悄少一行。
    assert len(report.attempts) == len(history.STRATEGIES)


def test_probe_reports_no_winner_when_protocol_never_pages():
    async def frozen(cursor: int) -> Any:
        return _asc_page(300)

    report = asyncio.run(history.probe_pagination(frozen))
    assert report.ok is True
    assert report.winner == ""
    assert all(item.fresh == 0 for item in report.attempts if not item.skipped)
    text = "\n".join(history.render_probe(report, page_size=20))
    assert "六种方式都翻不动" in text


def test_probe_skips_strategies_without_the_field():
    async def id_only(cursor: int) -> Any:
        page = [{"message_id": 900_300 + offset, "time": 100 + offset} for offset in range(3)]
        if cursor == 900_300:
            return [{"message_id": 800_000 + offset, "time": 10 + offset} for offset in range(3)]
        return page

    report = asyncio.run(history.probe_pagination(id_only))
    skipped = {item.strategy: item.skipped for item in report.attempts if item.skipped}
    assert "seq_first" in skipped
    assert "message_seq" in skipped["seq_first"]
    assert report.winner == "id_first"


def test_probe_handles_empty_first_page():
    async def empty(cursor: int) -> Any:
        return []

    report = asyncio.run(history.probe_pagination(empty))
    assert report.ok is False
    assert "空的第一页" in report.error
    text = "\n".join(history.render_probe(report, page_size=20))
    assert "翻页自检失败" in text


def test_probe_reports_first_page_error():
    async def boom(cursor: int) -> Any:
        raise RuntimeError("接口不存在")

    report = asyncio.run(history.probe_pagination(boom, brief=lambda exc: str(exc)))
    assert report.ok is False
    assert report.error == "接口不存在"


def test_probe_records_per_attempt_errors():
    async def flaky(cursor: int) -> Any:
        if cursor == 0:
            return _asc_page(300)
        raise RuntimeError("权限不足")

    report = asyncio.run(history.probe_pagination(flaky, brief=lambda exc: str(exc)))
    assert report.ok is True
    assert report.winner == ""
    assert [item.error for item in report.attempts if item.error]
    text = "\n".join(history.render_probe(report, page_size=20))
    assert "[报错]" in text


def test_probe_does_not_repeat_identical_cursors():
    """seq 和 id 是同一套编号时不该白打两次请求。"""

    calls: list[int] = []

    async def same_numbering(cursor: int) -> Any:
        calls.append(cursor)
        page = [
            {"message_seq": 300 + offset, "message_id": 300 + offset, "time": 100 + offset}
            for offset in range(3)
        ]
        if cursor == 300:
            return [
                {"message_seq": 200 + offset, "message_id": 200 + offset, "time": 10 + offset}
                for offset in range(3)
            ]
        return page

    report = asyncio.run(history.probe_pagination(same_numbering))
    assert report.winner == "seq_first"
    skipped = [item for item in report.attempts if "相同" in item.skipped]
    #: seq/id 三对错位取值全部撞车（first/last/oldest）→ 共省四次请求。
    assert len(skipped) == 4
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_probe_includes_cleaning_funnel_when_filled():
    fetch = FakeProtocol("seq_first")
    report = asyncio.run(history.probe_pagination(fetch))
    report.parsed = 18
    report.kept = 4
    report.kept_loose = 15
    text = "\n".join(history.render_probe(report, page_size=20))
    assert "清洗漏斗" in text
    assert "最短发言长度" in text
    assert "可用" in text


def test_render_probe_omits_funnel_when_not_sampled():
    fetch = FakeProtocol("seq_first")
    report = asyncio.run(history.probe_pagination(fetch))
    text = "\n".join(history.render_probe(report, page_size=20))
    assert "清洗漏斗" not in text
    assert "不写入语料库" in text


# ---------------------------------------------------------------------------
# 请求体写法（参数名的第三个自由度）
# ---------------------------------------------------------------------------


def test_build_history_params_dual_writes_both_names():
    params = history.build_history_params("10086", 4321, 200)
    assert params["group_id"] == 10086
    assert params["count"] == 200
    #: 同一个游标值写进两个参数名，各家协议端各读自己认的那个。
    assert params["message_seq"] == 4321
    assert params["message_id"] == 4321
    #: 两种拼写的倒序开关都给上：老实现读驼峰，SnowLuma 读下划线。
    assert params["reverseOrder"] is True
    assert params["reverse_order"] is True


def test_build_history_params_single_styles():
    seq_only = history.build_history_params("1", 77, 20, style="seq")
    assert "message_id" not in seq_only
    assert "reverse_order" not in seq_only
    assert seq_only["message_seq"] == 77

    id_only = history.build_history_params("1", 77, 20, style="id")
    assert "message_seq" not in id_only
    assert "reverseOrder" not in id_only
    assert id_only["message_id"] == 77


def test_build_history_params_first_page_uses_zero():
    params = history.build_history_params("1", None, 20)
    assert params["message_seq"] == 0
    assert params["message_id"] == 0


def test_build_history_params_accepts_negative_anchor():
    #: SnowLuma 的 message_id 是 signed int32 hash，负数是常态。
    params = history.build_history_params("1", -1234567, 20, style="id")
    assert params["message_id"] == -1234567


def test_build_history_params_clamps_count():
    #: SnowLuma 明确把超过 200 的 count 截断，其它实现也是这个量级。
    assert history.build_history_params("1", 0, 500)["count"] == 200
    assert history.build_history_params("1", 0, 0)["count"] == 1
    assert history.build_history_params("1", 0, -5)["count"] == 1


def test_normalize_param_style_falls_back_to_dual():
    assert history.normalize_param_style("ID") == "id"
    assert history.normalize_param_style("seq") == "seq"
    assert history.normalize_param_style("") == "dual"
    assert history.normalize_param_style("whatever") == "dual"


def test_render_probe_shows_impl_and_param_style():
    report = history.ProbeReport(ok=True, base_total=20, impl="SnowLuma 1.4.0", param_style="id")
    text = "\n".join(history.render_probe(report, page_size=20))
    assert "SnowLuma 1.4.0" in text
    assert "只写 message_id" in text


def test_render_probe_hints_message_id_protocols():
    report = history.ProbeReport(ok=True, base_total=20, winner="id_first")
    text = "\n".join(history.render_probe(report, page_size=20))
    assert "SnowLuma" in text

