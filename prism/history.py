"""get_group_msg_history 的翻页策略与自检探针。

OneBot 的 get_group_msg_history 是本插件回溯群历史的唯一入口，而它的翻页语义在各家
协议端实现里并不统一，具体有两个自由度：

* 请求参数名叫 message_seq，但它实际认的编号可能是 message_seq，也可能是 message_id；
* 返回的那一页数组，有的实现是「最旧 → 最新」，有的是「最新 → 最旧」，于是「这一页最旧
  的那条」到底在 messages[0] 还是 messages[-1] 也不固定。

两个自由度组合出四种翻页方式。传错时协议端**不会报错**，而是原地返回同一批最新消息，
表现得就像「群历史已经翻到头了」。所以这里把四种方式枚举出来，由调用方逐个试，
并把实测可用的那种记进 scan_state 复用。

`probe_pagination` 是给「棱镜诊断」指令用的自检探针：它把四种方式各打一次，报出每次
返回了多少条、与首页重叠多少、时间有没有真的往前走，让人一眼看出协议端认哪一种。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "STRATEGIES",
    "STRATEGY_ALIASES",
    "STRATEGY_LABELS",
    "ProbeAttempt",
    "ProbeReport",
    "anchor_of",
    "cursor_of",
    "normalize_strategy",
    "page_ids",
    "page_time_range",
    "probe_pagination",
    "read_cursor",
    "render_probe",
    "rotate_strategies",
    "strategy_label",
    "to_int",
]

#: 四种翻页方式，按"先试哪个"排序。名字含义 = 取哪个字段 + 取这一页的哪一端。
STRATEGIES: tuple[str, ...] = ("seq_first", "id_first", "seq_last", "id_last")

#: v1.1.3 只有两种方式，配置和数据库里可能存着旧名字。
STRATEGY_ALIASES: dict[str, str] = {
    "message_seq": "seq_first",
    "message_id": "id_first",
}

#: 每种方式 = （取这一页的哪一端, 读哪个字段）。
_SPECS: dict[str, tuple[str, str]] = {
    "seq_first": ("first", "message_seq"),
    "id_first": ("first", "message_id"),
    "seq_last": ("last", "message_seq"),
    "id_last": ("last", "message_id"),
}

STRATEGY_LABELS: dict[str, str] = {
    "seq_first": "message_seq（取本页第一条）",
    "id_first": "message_id（取本页第一条）",
    "seq_last": "message_seq（取本页最后一条）",
    "id_last": "message_id（取本页最后一条）",
}

#: 读游标时的字段回退顺序。real_seq 是部分实现给 message_seq 起的别名。
_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "message_seq": ("message_seq", "real_seq"),
    "message_id": ("message_id",),
}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def to_int(value: Any) -> int | None:
    """宽松地把游标解析成整数。

    不能用 str.isdigit()：NapCat 的 message_id 可能是负数，isdigit() 会把它整条丢掉，
    结果就是回溯静默停在第一页。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def read_cursor(raw: Any, field: str = "") -> int | None:
    """从一条群历史消息里读出指定字段的游标值。

    field 留空时按 message_seq → real_seq → message_id 的顺序探测，只用于兜底；
    正常路径都应该显式指定，由调用方结合"这一页有没有真的前进"来判断该用哪种。
    """
    if not isinstance(raw, dict):
        return None
    keys = _FIELD_KEYS.get(field) or ("message_seq", "real_seq", "message_id")
    for key in keys:
        parsed = to_int(raw.get(key))
        if parsed is not None:
            return parsed
    return None


def normalize_strategy(value: Any) -> str:
    """把配置 / 数据库里的值归一成合法策略名；auto 或无法识别都返回空串。"""
    name = str(value or "").strip()
    name = STRATEGY_ALIASES.get(name, name)
    return name if name in STRATEGIES else ""


def strategy_label(name: Any) -> str:
    """给用户看的策略名称。"""
    key = normalize_strategy(name)
    return STRATEGY_LABELS.get(key, str(name or "未知"))


def _rows(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, (list, tuple)):
        return []
    return [raw for raw in messages if isinstance(raw, dict)]


def anchor_of(messages: Any, strategy: str) -> dict[str, Any] | None:
    """按策略取出这一页里当作"下一页起点"的那条消息。"""
    rows = _rows(messages)
    if not rows:
        return None
    end, _ = _SPECS.get(normalize_strategy(strategy) or "seq_first", ("first", "message_seq"))
    return rows[0] if end == "first" else rows[-1]


def cursor_of(messages: Any, strategy: str) -> int | None:
    """按策略算出请求下一页时要传的 message_seq 值。"""
    name = normalize_strategy(strategy)
    if not name:
        return None
    anchor = anchor_of(messages, name)
    if anchor is None:
        return None
    return read_cursor(anchor, _SPECS[name][1])


def page_ids(messages: Any) -> set[str]:
    """这一页所有消息的唯一标识，用来判断"翻页到底有没有前进"。

    只比较游标数值是不够的：游标不生效时协议端返回的是同一批消息，但个别实现会把
    message_seq 一起换成新值。直接看消息集合有没有出现新面孔最可靠。
    """
    ids: set[str] = set()
    for raw in _rows(messages):
        for key in ("message_id", "message_seq", "real_seq"):
            token = raw.get(key)
            if token is not None:
                ids.add(str(token))
                break
    return ids


def page_time_range(messages: Any) -> tuple[int, int]:
    """这一页的时间跨度 (最早, 最晚)，拿不到时间就返回 (0, 0)。"""
    stamps = [to_int(raw.get("time")) or 0 for raw in _rows(messages)]
    stamps = [ts for ts in stamps if ts > 0]
    if not stamps:
        return 0, 0
    return min(stamps), max(stamps)


def rotate_strategies(current: str, tried: set[str]) -> list[str]:
    """还没试过的策略，按默认顺序排列。"""
    return [name for name in STRATEGIES if name != current and name not in tried]


# --------------------------------------------------------------------------
# 自检探针
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ProbeAttempt:
    """一种翻页方式的实测结果。"""

    strategy: str
    #: 本次请求实际传给 message_seq 的值。
    cursor: int | None = None
    #: 没试的原因（比如协议端没返回这个字段）。
    skipped: str = ""
    error: str = ""
    total: int = 0
    #: 这一页里首页没出现过的消息条数。
    fresh: int = 0
    oldest: int = 0
    newest: int = 0
    #: 既有新消息、时间也确实更早 —— 这才叫翻页成功。
    advanced: bool = False


@dataclass(slots=True)
class ProbeReport:
    """一次「棱镜诊断」的完整结果。"""

    #: 首页（message_seq=0）的情况。首页都拿不到就没必要往下试了。
    ok: bool = False
    error: str = ""
    base_total: int = 0
    base_oldest: int = 0
    base_newest: int = 0
    #: 首页第一条原始消息里出现过的字段名，用来判断协议端到底给了什么。
    base_keys: list[str] = field(default_factory=list)
    #: 首末两条的两种编号，方便肉眼比对 seq 和 id 是不是同一套。
    first_seq: int | None = None
    first_id: int | None = None
    last_seq: int | None = None
    last_id: int | None = None
    attempts: list[ProbeAttempt] = field(default_factory=list)
    #: 实测可用的策略，空串表示四种都不行。
    winner: str = ""
    #: 清洗漏斗：解析出多少条、按当前配置留下多少、按最宽松口径留下多少。
    parsed: int = 0
    kept: int = 0
    kept_loose: int = 0


async def probe_pagination(
    fetch: Callable[[int], Awaitable[Any]],
    *,
    strategies: tuple[str, ...] = STRATEGIES,
    brief: Callable[[BaseException], str] | None = None,
) -> ProbeReport:
    """逐个实测四种翻页方式，返回可读的诊断结果。

    fetch(cursor) 负责真正调 get_group_msg_history 并返回 messages 列表；
    异常由本函数捕获并记进结果，不向外抛。
    """
    describe = brief or (lambda exc: f"{type(exc).__name__}: {exc}"[:120])
    report = ProbeReport()
    try:
        base = await fetch(0)
    except Exception as exc:  #: 探针本身不该把异常抛给指令层
        report.error = describe(exc)
        return report

    rows = _rows(base)
    if not rows:
        report.error = "协议端返回了空的第一页，机器人可能刚进群、不在群内，或该群消息已被清理。"
        return report

    report.ok = True
    report.base_total = len(rows)
    report.base_oldest, report.base_newest = page_time_range(base)
    report.base_keys = sorted(str(key) for key in rows[0])
    report.first_seq = read_cursor(rows[0], "message_seq")
    report.first_id = read_cursor(rows[0], "message_id")
    report.last_seq = read_cursor(rows[-1], "message_seq")
    report.last_id = read_cursor(rows[-1], "message_id")
    base_ids = page_ids(base)

    seen_cursors: dict[int, str] = {}
    for name in strategies:
        attempt = ProbeAttempt(strategy=name)
        report.attempts.append(attempt)
        cursor = cursor_of(base, name)
        if cursor is None:
            _, field_name = _SPECS[name]
            attempt.skipped = f"协议端没返回 {field_name}"
            continue
        attempt.cursor = cursor
        if cursor in seen_cursors:
            attempt.skipped = f"游标值与「{strategy_label(seen_cursors[cursor])}」相同，无需重复试"
            continue
        seen_cursors[cursor] = name
        try:
            page = await fetch(cursor)
        except Exception as exc:
            attempt.error = describe(exc)
            continue
        rows_here = _rows(page)
        attempt.total = len(rows_here)
        if not rows_here:
            continue
        attempt.fresh = len(page_ids(page) - base_ids)
        attempt.oldest, attempt.newest = page_time_range(page)
        earlier = attempt.oldest > 0 and report.base_oldest > 0 and attempt.oldest < report.base_oldest
        attempt.advanced = attempt.fresh > 0 and earlier
        if attempt.advanced and not report.winner:
            report.winner = name
    return report


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _stamp(ts: int) -> str:
    if ts <= 0:
        return "未知"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _num(value: int | None) -> str:
    return "无" if value is None else str(value)


def render_probe(report: ProbeReport, *, page_size: int) -> list[str]:
    """把探针结果渲染成「棱镜诊断」的回复内容。"""
    if not report.ok:
        return [
            "翻页自检失败。",
            f"原因：{report.error}",
            "如果是权限或接口不存在，说明当前协议端拿不到群历史，只能靠被动采集攒语料。",
        ]

    lines = [
        f"翻页自检（本群，每次只拉 {page_size} 条，不写入语料库）",
        "",
        f"第一页：{report.base_total} 条，时间 {_stamp(report.base_oldest)} ~ {_stamp(report.base_newest)}",
        f"  首条 message_seq={_num(report.first_seq)} / message_id={_num(report.first_id)}",
        f"  末条 message_seq={_num(report.last_seq)} / message_id={_num(report.last_id)}",
    ]
    if report.base_keys:
        keys = "、".join(report.base_keys[:12])
        lines.append(f"  返回字段：{keys}")
    lines.append("")
    lines.append("往前翻一页的四种方式：")
    for attempt in report.attempts:
        label = strategy_label(attempt.strategy)
        if attempt.skipped:
            lines.append(f"  · [跳过] {label}：{attempt.skipped}")
            continue
        if attempt.error:
            lines.append(f"  · [报错] {label}：{attempt.error}")
            continue
        head = f"  · {label}：传 {attempt.cursor} → "
        if attempt.total == 0:
            lines.append(head + "0 条（协议端认为没有更早的消息了）")
        elif attempt.advanced:
            lines.append(
                head
                + f"{attempt.total} 条，其中 {attempt.fresh} 条是新的，"
                + f"最早时间前移到 {_stamp(attempt.oldest)} ✅ 可用",
            )
        elif attempt.fresh == 0:
            lines.append(head + f"{attempt.total} 条，但全是第一页看过的消息（原地打转）")
        else:
            lines.append(
                head
                + f"{attempt.total} 条，有 {attempt.fresh} 条新消息，"
                + f"但时间没有往前走（{_stamp(attempt.oldest)}），不算翻页成功",
            )

    lines.append("")
    if report.winner:
        lines.append(f"结论：本群可用「{strategy_label(report.winner)}」，已记下来给下次回溯用。")
        lines.append("现在发一次画像指令，再发「棱镜缓存」，「已往前翻过 N 页」应该会涨起来。")
    else:
        lines.append("结论：四种方式都翻不动，当前协议端的 get_group_msg_history 没有实现分页。")
        lines.append("只能靠被动采集慢慢攒语料；换 NapCat / Lagrange / go-cqhttp 之类的协议端可解决。")

    if report.parsed > 0:
        lines.append("")
        lines.append(
            f"顺带看一眼清洗漏斗：第一页 {report.base_total} 条里解析出 {report.parsed} 条文本，"
            f"按当前配置入库 {report.kept} 条；若把长度与指令过滤放到最宽松则是 {report.kept_loose} 条。",
        )
        if report.kept_loose > report.kept:
            lines.append(
                "两者差得多说明群里短消息 / 表情 / 指令占比高，可以把「最短发言长度」调到 1，"
                "或关掉「过滤指令消息」来多留一些语料。",
            )
    return lines
