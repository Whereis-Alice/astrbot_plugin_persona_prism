"""历史回溯的可观测性（采集诊断）。

上游画像时会把回溯过程刷在群里：「正在发起 30 轮查询来获取 XX 的聊天记录…」
「已从 6000 条群消息中提取到 300 条 XX 的聊天记录，正在画像…」。这种提示很吵，
但它有一个我们一开始丢掉的价值：让用户知道回溯到底跑了没有、拿到了多少。

我们原先只有一句不带数字的「正在翻聊天记录…」，样本不足时也只说「多聊几句再来」。
结果新装插件的人无法判断到底是：群里真的没话 / 协议端不支持拉历史 / 回溯轮数被调成
了 0 / 被动采集没开。这个模块把回溯过程收敛成一个 ScanReport，并提供纯函数把它渲染
成用户看得懂的中文诊断。

刻意不依赖 AstrBot：这里全部是 dataclass + 纯函数，可以直接单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import history

#: 协议端报错摘要的最大长度。异常里常见整段 traceback 或 JSON，直接贴群里会刷屏。
ERROR_BRIEF_MAX = 80

#: 已知支持主动拉取群历史（get_group_msg_history）的平台适配器。
#: OneBot v11 系（NapCat / Lagrange / LLOneBot / Shamrock / go-cqhttp / SnowLuma 及
#: Lucky Lillia Bot 等实现）在 AstrBot 里统一走 aiocqhttp 适配器。
BACKFILL_PLATFORMS: frozenset[str] = frozenset({"aiocqhttp"})

#: 断点深度低于这个值时，"已挖到群历史尽头"很可能是误判：协议端不认我们喂的游标，
#: 于是直接回一页空数组，看起来和"真的没有更早的消息"一模一样。
TRUSTED_DEPTH = 3

#: 复查窗口。浅断点疑点大、复查得勤；深断点大概率是真到头了，隔半天验一次就够。
#: 复查的代价只是一次会返回空页的请求，但能救回被误锁的群（症状：语料永远停在两百条）。
RECHECK_SHALLOW_SEC = 300
RECHECK_DEEP_SEC = 21600

#: 样本不足回复里最多贴几条诊断。全量诊断可能有 5～6 条，贴满等于又刷一屏。
SHORTFALL_ITEM_MAX = 3

#: should_recheck_exhausted 的返回值 → 给用户看的原因。
RECHECK_REASONS: dict[str, str] = {
    "shallow": "断点太浅，「已挖到头」很可能是协议端喂回空页造成的误判",
    "stale": "距上次判定已久，顺手复查一次有没有更早的历史",
}


def brief_error(exc: object) -> str:
    """把异常压成一行短摘要，避免把 traceback 或整段 JSON 贴进群聊。"""
    text = str(exc or "").strip()
    if not text:
        text = type(exc).__name__ if isinstance(exc, BaseException) else "未知错误"
    text = " ".join(text.split())
    if len(text) > ERROR_BRIEF_MAX:
        text = text[: ERROR_BRIEF_MAX - 1] + "…"
    return text


def supports_backfill(platform: str, group_id: str) -> bool:
    """只有群聊 + 已知的 OneBot 适配器才能主动翻历史。"""
    return bool(group_id) and platform in BACKFILL_PLATFORMS


def should_recheck_exhausted(state: dict[str, Any], *, now: float | None = None) -> str:
    """判断这次要不要无视库里的「已挖到头」标记，重新验证一遍。

    背景：协议端对不认识的游标常常直接回一页空数组，我们无法把它和"真的没有更早
    的消息"区分开，于是会把 exhausted 写进库。一旦写错，这个群从此只补拉最新一页，
    语料永远停在两百来条——正是用户遇到的"就一个活跃群采不到"。

    复查的代价只是一次多半会返回空页的请求，所以宁可多试：断点很浅（还没翻过几页
    就宣布到头）时疑点最大，隔几分钟就复查；断点已经很深时大概率是真到头了，隔半天
    验一次即可。返回值是原因 key（见 RECHECK_REASONS），空串表示这次不复查。
    """
    if not state.get("exhausted"):
        return ""
    try:
        depth = max(0, int(state.get("depth_pages") or 0))
    except (TypeError, ValueError):
        depth = 0
    try:
        last = int(state.get("last_scan") or 0)
    except (TypeError, ValueError):
        last = 0
    moment = time.time() if now is None else now
    # last <= 0：老版本留下的记录没写时间戳，当成"很久以前"，直接允许复查。
    elapsed = float("inf") if last <= 0 else moment - last
    if depth < TRUSTED_DEPTH:
        return "shallow" if elapsed >= RECHECK_SHALLOW_SEC else ""
    return "stale" if elapsed >= RECHECK_DEEP_SEC else ""


@dataclass(slots=True)
class ScanReport:
    """一次采集（本地取语料 + 可选的历史回溯）的诊断结果。

    刻意做成"值对象"由调用方逐层传递，而不是挂在 Star 实例上：画像可以并发
    执行（limits.max_concurrency > 1），共享可变状态会串号。
    """

    platform: str = ""
    #: 是否群聊场景。私聊没有群历史可翻。
    is_group: bool = False
    #: 当前平台 + 场景是否支持主动拉历史。
    supported: bool = False
    #: 本次是否真的发起了回溯请求。本地语料已经够多时不会触发。
    attempted: bool = False
    #: 配置里的计划轮数（collect.backfill_rounds）。
    planned_rounds: int = 0
    #: 每轮请求的条数（collect.page_size）。
    page_size: int = 0
    #: 实际成功拿到非空数据的页数。
    pages: int = 0
    #: 历史页里看到的原始消息总数（全群所有人，含被过滤掉的）。
    scanned: int = 0
    #: 新写进语料库的条数（去重后）。
    added: int = 0
    #: 已经翻到群历史最早一条。
    exhausted: bool = False
    #: 本群此前已挖到头，本次只补拉最新一页。
    topup_only: bool = False
    #: 被动采集开关（collect.passive_capture）。
    passive_capture: bool = True
    #: 回溯前本地已有的该用户语料条数。
    local_before: int = 0
    #: 本次实际用来翻页的字段名（message_seq / message_id），空串表示还没翻过页。
    cursor_field: str = ""
    #: 本次中途换过游标字段（先试的那种翻不动，自动切到另一种并成功了）。
    cursor_switched: bool = False
    #: 两种游标都试过，协议端依旧原地返回同一批消息 —— 翻页卡住了，不是真的挖到头。
    stalled: bool = False
    #: 本次无视了库里的"已挖到头"标记，重新验证了一遍（空串表示没有复查）。
    exhausted_recheck: str = ""
    #: 库里的断点一翻就是空页（已失效），本次退回最新一页重新往前挖。
    restarted: bool = False
    #: 本次之前这个群累计已往前翻过的页数（断点深度）。
    depth_before: int = 0
    #: 协议端报错摘要，空串表示没报错。
    error: str = ""

    @property
    def blocked(self) -> bool:
        """发起了回溯但被协议端拒绝。"""
        return self.attempted and bool(self.error)

    @property
    def fetched(self) -> bool:
        """确实翻到了东西（哪怕一条都没新入库，至少证明链路是通的）。"""
        return self.attempted and self.pages > 0 and not self.error

    def to_dict(self) -> dict[str, Any]:
        """给日志 / run 记录用的扁平字典。"""
        return {
            "platform": self.platform,
            "is_group": self.is_group,
            "supported": self.supported,
            "attempted": self.attempted,
            "planned_rounds": self.planned_rounds,
            "page_size": self.page_size,
            "pages": self.pages,
            "scanned": self.scanned,
            "added": self.added,
            "exhausted": self.exhausted,
            "topup_only": self.topup_only,
            "passive_capture": self.passive_capture,
            "local_before": self.local_before,
            "cursor_field": self.cursor_field,
            "cursor_switched": self.cursor_switched,
            "stalled": self.stalled,
            "exhausted_recheck": self.exhausted_recheck,
            "restarted": self.restarted,
            "depth_before": self.depth_before,
            "error": self.error,
        }


def human_since(seconds: float) -> str:
    """把"距今多久"压成一句中文。"""
    value = max(0, int(seconds))
    if value < 60:
        return "不到 1 分钟"
    if value < 3600:
        return f"{value // 60} 分钟"
    if value < 86400:
        return f"{value // 3600} 小时"
    return f"{value // 86400} 天"


def progress_line(
    report: ScanReport,
    *,
    target_name: str,
    label: str,
    sampled: int,
) -> str:
    """回溯之后、送进模型之前的那句进度提示（群聊里看到的版本）。

    刻意只留一个数字。早先这里把"翻了几页 / 看了多少条 / 新入库多少 / 提取到多少"
    全塞进群里，两条长句连着刷，比画像本身还占屏。翻页细节改由 progress_log() 写进
    AstrBot 后台日志，排查时去日志里看。

    没有发起回溯时返回空串，调用方跳过即可——本地缓存够用的情况下再刷一条纯属噪音。
    """
    if report.blocked:
        return f"拉群历史被协议端拒绝，改用本地已存的 {sampled} 条发言分析…"
    if not report.fetched:
        return ""
    return f"已取到 {sampled} 条发言，正在分析…"


def progress_log(
    report: ScanReport,
    *,
    target_name: str,
    label: str,
    sampled: int,
) -> str:
    """progress_line 的详细版，供 logger.info 使用。

    群里只留一句短提示，但排查"到底有没有翻到东西"仍然需要这些数字，所以完整版
    原封不动搬到日志里。
    """
    who = target_name or "TA"
    if report.blocked:
        return (
            f"[{label}] {who}：协议端拒绝拉取群历史（{report.error}），"
            f"退回本地语料 {sampled} 条"
        )
    if not report.attempted:
        return f"[{label}] {who}：未触发回溯（本地语料 {report.local_before} 条已够用），样本 {sampled} 条"
    scope = "补拉最新一页" if report.topup_only else f"翻了 {report.pages} 页"
    parts = [
        f"[{label}] {who}：{scope}群历史（计划 {report.planned_rounds} 轮 × {report.page_size} 条）",
        f"看到约 {report.scanned} 条，新入库 {report.added} 条",
        f"有效发言 {sampled} 条",
    ]
    if report.cursor_field:
        parts.append(f"翻页方式 {history.strategy_label(report.cursor_field)}")
    if report.cursor_switched:
        parts.append("中途切换过游标")
    if report.exhausted_recheck:
        parts.append(f"复查已挖到头标记（{report.exhausted_recheck}）")
    if report.restarted:
        parts.append("库里断点失效，已退回最新一页重挖")
    if report.stalled:
        parts.append("游标不生效，翻页原地打转")
    if report.exhausted:
        parts.append("已到群历史最早一条")
    return "；".join(parts)


def intro_line(
    report: ScanReport,
    *,
    target_name: str,
    label: str,
) -> str:
    """开工前那句提示。

    只说在做什么。计划轮数、每轮条数这些参数用户既改不动也不关心，写进日志即可。
    """
    who = target_name or "TA"
    return f"正在为 {who} 生成{label}…"


def shortfall_reply(
    report: ScanReport,
    *,
    target_name: str,
    label: str,
    sampled: int,
    min_messages: int,
) -> str:
    """样本不足时的回复：一句结论 + 采集诊断 + 可操作建议。

    上游这里只说"发言太少"，用户完全不知道回溯有没有跑。我们把判断依据摊开，
    让人一眼看出瓶颈在群里没话、协议端不支持，还是配置关掉了采集。
    """
    who = target_name or "TA"
    lines = [
        f"{who} 的有效发言只有 {sampled} 条，还不够生成{label}（至少需要 {min_messages} 条）。",
        "采集诊断：",
    ]
    # 诊断条目按"最可能的瓶颈"排过序，群里只贴前几条；完整列表写进后台日志。
    items = diagnose(report)
    lines.extend(f"  · {item}" for item in items[:SHORTFALL_ITEM_MAX])
    if len(items) > SHORTFALL_ITEM_MAX:
        lines.append(f"  · （另有 {len(items) - SHORTFALL_ITEM_MAX} 条细节已写入后台日志）")
    lines.append("建议：让 TA 多聊几句，或发「棱镜缓存」查看本群语料积累情况。")
    return "\n".join(lines)


def diagnose(report: ScanReport) -> list[str]:
    """按"最可能的瓶颈"顺序给出诊断条目。"""
    items: list[str] = []
    if not report.is_group:
        items.append("当前是私聊场景，没有群历史可翻，只能靠平时聊天被动积累语料。")
    elif not report.supported:
        platform = report.platform or "未知"
        items.append(
            f"当前平台适配器（{platform}）不支持主动拉取群历史，"
            "只能靠被动采集慢慢攒；QQ（aiocqhttp / OneBot v11）才能回溯。",
        )
    elif report.planned_rounds <= 0:
        items.append(
            "「历史回溯轮数」被设为 0，本次没有翻历史；把它调到 5～20 就会自动回溯。",
        )
    elif not report.attempted:
        items.append(
            f"本地语料已有 {report.local_before} 条，达到单次分析上限，本次没有触发回溯。",
        )
    elif report.error:
        items.append(
            f"翻历史失败：{report.error}",
        )
        items.append(
            "常见原因是协议端没实现 get_group_msg_history、机器人已不在群内，或权限被限制。",
        )
    else:
        scope = "补拉了最新一页" if report.topup_only else f"翻了 {report.pages} 页"
        items.append(
            f"回溯已执行：{scope}群历史，共看到约 {report.scanned} 条消息，"
            f"新入库 {report.added} 条。",
        )
        if report.pages == 0:
            items.append("协议端返回了空历史，通常说明机器人刚进群或该群消息已被清理。")
        elif report.stalled:
            #: 最难自查的一种失败：接口调通了、每页都有数据，但游标不生效，
            #: 于是每页都是同一批最新消息。必须说清楚，否则用户只会看到"发言太少"。
            items.append(
                "协议端的翻页游标不生效：往前翻时反复返回同一批最新消息，"
                "四种翻页方式（message_seq / message_id × 取本页第一条 / 最后一条）都试过了。",
            )
            items.append(
                "下一步请管理员在本群发「棱镜诊断」——它会把四种方式各实测一次，"
                "报出每次拿到多少条、跟第一页重叠多少、最早时间有没有真的前移，"
                "能用的那种会当场记下来；若四种都翻不动，就是协议端没实现分页，"
                "只能靠被动采集慢慢攒语料。",
            )
        elif report.exhausted:
            items.append("群历史已经翻到最早一条，能拿到的记录就这些了。")
        elif report.topup_only:
            items.append("本群此前已挖到历史尽头，所以本次只补拉最新一页。")
        elif report.pages < report.planned_rounds:
            items.append(
                f"计划 {report.planned_rounds} 轮，实际 {report.pages} 轮就停了"
                "（已攒够或协议端不再返回数据）。",
            )
        if report.cursor_switched and not report.stalled:
            items.append(
                f"本群的翻页方式已自动切换成「{history.strategy_label(report.cursor_field)}」"
                "（原先那种翻不动），这个选择已记下来，之后不会再试错。",
            )
        if report.restarted:
            items.append(
                "库里记的回溯断点已经失效（拿它去翻只回空页），本次已丢弃断点、"
                "从最新一页重新往前挖。",
            )
        if report.exhausted_recheck:
            reason = RECHECK_REASONS.get(report.exhausted_recheck, "按策略复查")
            items.append(f"本群此前被标记「已挖到头」，但{reason}，所以本次又验了一遍。")
    if not report.passive_capture:
        items.append("「被动采集」当前是关闭的，新消息不会入库，建议在配置里打开。")
    return items


def describe_scan_state(state: dict[str, Any], *, now: float | None = None) -> list[str]:
    """把 scan_state 表里的断点渲染成「棱镜缓存」里的几行。

    直接回答用户最关心的"到底有没有成功轮询过"。
    """
    moment = time.time() if now is None else now
    exhausted = "已挖到头" if state.get("exhausted") else "仍可继续回溯"
    depth = 0
    try:
        depth = max(0, int(state.get("depth_pages") or 0))
    except (TypeError, ValueError):
        depth = 0
    suffix = f"（已往前翻过 {depth} 页）" if depth else ""
    lines = [f"  历史回溯：{exhausted}{suffix}"]
    if state.get("exhausted") and depth < TRUSTED_DEPTH:
        # 只翻了一两页就宣布到头，多半是协议端回了空页导致的误判。告诉用户不用手动重扫。
        lines.append("  （断点很浅，「到头」可能是协议端回空页造成的误判，下次画像会自动复查）")
    last = 0
    try:
        last = int(state.get("last_scan") or 0)
    except (TypeError, ValueError):
        last = 0
    if last > 0:
        lines.append(f"  上次回溯：{human_since(moment - last)}前")
    else:
        lines.append("  上次回溯：还没有成功回溯过（发一次画像指令即会触发）")
    cursor = str(state.get("oldest_seq") or "")
    if cursor:
        field = history.strategy_label(state.get("cursor_field") or history.STRATEGIES[0])
        lines.append(f"  回溯断点：{field} = {cursor}")
    return lines
