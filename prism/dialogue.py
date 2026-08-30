"""把本地库里的多人对话还原成一段「聊天现场」文本，供 LLM 理解上下文。

为什么需要这个模块：群聊是对话，不是独白。只把被分析者本人的发言喂给模型
（v1.2.3 及以前的做法）会让模型看到一串没有前因后果的句子 —— 短回复型的人
很容易被误判成「自言自语」「话题跳跃」，而实际上 TA 是在接别人的话。

两条设计红线：

1. 别人的话只用于「理解上下文」，不作为被分析者的性格证据；这一点由
   prompts 里的硬规则约束。
2. 对话内容全部来自本地 SQLite 语料，逐字取用，不出网、不改写 —— 模型没有
   机会虚构谁说过什么。

输入的 rows 就是 store.window_rows() / store.context_rows() 的返回值，字段：
message_id / user_id / user_name / text / ts / is_reply / reply_to / images / at_ids。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .scenes import TURN_GAP_SECONDS, Turn, clip_turn, real_text, split_turns, turn_of

__all__ = [
    "TURN_GAP_SECONDS",
    "Turn",
    "clip_turn",
    "split_turns",
    "turn_of",
]

#: 对话行的说话人标签。故意用中文短标签而不是 [Target]/[Other]：
#: 中文模型对中文标签的指代把握更稳，也省 token。
LABEL_TARGET = "[TA]"
LABEL_OTHER = "[其他人]"
LABEL_BOT = "[你]"

#: 片段之间的断裂提示。中间跳过的内容不长、或算不出间隔时用这一句。
GAP_MARK = "……（中间略）……"

#: 引用行的前缀：被回复的那条原话离得太远、没被拉进现场时，单独贴一行给模型看。
QUOTE_PREFIX = "    ↳ 引用"

#: 时间间隔超过这个秒数就算两个不同的场景，即使索引相邻。
SCENE_GAP_SECONDS = 20 * 60

#: 回合切分（Turn / split_turns / turn_of / clip_turn / TURN_GAP_SECONDS）统一放在 scenes，
#: 提示词里的「对话现场」和卡片上的气泡才会切在同一处；这里只是转出来方便调用。

#: 一个回合最多展开多少行。回合本身比这短就整段拿走，超了就以锚点为中心裁。
TURN_LINE_CAP = 9

#: 判定「有人接话」的时间窗：TA 说完这么久之内有别人开口才算接上了。
#: 群聊不是一问一答，用时间口径比「下一条是不是别人说的」贴近真实体感。
RESPONSE_WINDOW_SECONDS = 5 * 60

#: 锚点等级。数字越小越优先展开：真实对话边 > 连续发言 > 孤零零一句。
TIER_EDGE = 0
TIER_RUN = 1
TIER_ALONE = 2


def _split_ids(raw: Any) -> list[str]:
    """把逗号分隔的 at_ids 拆成 uid 列表。"""
    text = str(raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _text_of(row: dict[str, Any]) -> str:
    """取一行的可读正文。图片、表情、语音这类占位符一律剔掉。

    模型看不见图，「[图片]」只是一个无信息的词，铺在现场里既占篇幅又会诱导它
    编造"TA 发了一张搞笑图"这种结论。剔干净后整行没字了就返回空串，让上层跳过
    这一行（跳过留下的空档会由「隔了 N 分钟」那套断裂提示照常补上）。
    """
    return real_text(row.get("text") or "")


def _name_of(row: dict[str, Any], names: dict[str, str]) -> str:
    uid = str(row.get("user_id") or "")
    return names.get(uid, "") or str(row.get("user_name") or "").strip() or uid or "群友"


def name_index(
    rows: Sequence[dict[str, Any]],
    names: dict[str, str] | None = None,
) -> dict[str, str]:
    """uid → 显示名。外部传入的 names（群名片）优先，其次用语料里带的昵称。"""
    out: dict[str, str] = {}
    for row in rows:
        uid = str(row.get("user_id") or "")
        if not uid or uid in out:
            continue
        label = str(row.get("user_name") or "").strip()
        if label:
            out[uid] = label
    for uid, label in (names or {}).items():
        text = str(label or "").strip()
        if text:
            out[str(uid)] = text
    return out


def order_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按时间从早到晚排好，时间相同时用 message_id 兜底保证稳定。"""
    return sorted(
        rows,
        key=lambda r: (int(r.get("ts") or 0), str(r.get("message_id") or "")),
    )


# ---------------------------------------------------------------------------
# 社交信号
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SocialSignals:
    """从对话结构里数出来的「关系层」事实。全部是本地精确计数。"""

    #: TA 主动回复别人的条数
    replied_others: int = 0
    #: TA 主动 @ 别人的条数
    at_others: int = 0
    #: 别人回复 TA 的条数
    got_replies: int = 0
    #: 别人 @ TA 的条数
    got_at: int = 0
    #: 接过 TA 话的人（昵称 → 次数），按次数倒序
    responders: list[tuple[str, int]] = field(default_factory=list)
    #: TA 接过谁的话（昵称 → 次数），按次数倒序
    addressed: list[tuple[str, int]] = field(default_factory=list)
    #: TA 的发言总条数（本窗口内）
    mine: int = 0
    #: 窗口内所有人的发言条数
    total: int = 0
    #: TA 说完话后，5 分钟内有人接着开口的次数（按连续发言段结算）
    answered: int = 0
    #: TA 说完话后，窗口内迟迟没人出声的次数
    unanswered: int = 0

    @property
    def answer_rate(self) -> float:
        """TA 开口后被人接住的比例。群聊不是一问一答，所以用时间口径而不是"下一条是谁"。"""
        base = self.answered + self.unanswered
        if base <= 0:
            return 0.0
        return self.answered / float(base)

    @property
    def response_rate(self) -> float:
        """TA 每说一句，平均能收到多少次回应。0 表示基本没人接。"""
        if self.mine <= 0:
            return 0.0
        return (self.got_replies + self.got_at) / float(self.mine)

    def to_prompt_block(self) -> str:
        """渲染成事实清单。没有任何信号时返回空串（由调用方决定是否省略整段）。"""
        lines: list[str] = []
        if self.mine:
            lines.append(
                f"- 对话窗口里 TA 说了 {self.mine} 句，同窗口全群共 {self.total} 句",
            )
        if self.replied_others or self.at_others:
            lines.append(
                f"- TA 主动接话 {self.replied_others} 次，主动 @ 别人 {self.at_others} 次",
            )
        if self.got_replies or self.got_at:
            lines.append(
                f"- 别人回应 TA {self.got_replies} 次，@ TA {self.got_at} 次"
                f"（平均每 10 句能收到 {self.response_rate * 10:.1f} 次回应）",
            )
        elif self.mine >= 10:
            lines.append("- 窗口内没检测到别人回复或 @ TA（也可能是协议端没上报引用关系）")
        if self.answered or self.unanswered:
            lines.append(
                f"- TA 说完后 5 分钟内有人接着开口 {self.answered} 次，"
                f"没人立刻出声 {self.unanswered} 次（接话率 {self.answer_rate * 100:.0f}%）",
            )
        if self.responders:
            who = "、".join(f"{name}({count}次)" for name, count in self.responders[:5])
            lines.append(f"- 最常接 TA 话的人：{who}")
        if self.addressed:
            who = "、".join(f"{name}({count}次)" for name, count in self.addressed[:5])
            lines.append(f"- TA 最常回应的人：{who}")
        return "\n".join(lines)


def social_signals(
    rows: Sequence[dict[str, Any]],
    target_id: str,
    *,
    names: dict[str, str] | None = None,
    self_id: str = "",
) -> SocialSignals:
    """统计 TA 与别人的互动结构。只看 reply_to / at_ids，不猜。

    机器人自己也算群里的一员：TA 找机器人搭话、机器人接住 TA 的话，都是真实互动，
    没有理由从统计里抠掉（self_id 保留只为兼容旧调用）。
    """
    _ = self_id
    target = str(target_id or "")
    ordered = order_rows(rows)
    nick = name_index(ordered, names)
    owner: dict[str, str] = {}
    for row in ordered:
        mid = str(row.get("message_id") or "")
        if mid:
            owner[mid] = str(row.get("user_id") or "")
    sig = SocialSignals()
    responders: dict[str, int] = {}
    addressed: dict[str, int] = {}
    for row in ordered:
        uid = str(row.get("user_id") or "")
        sig.total += 1
        reply_to = str(row.get("reply_to") or "")
        ats = _split_ids(row.get("at_ids"))
        if uid and uid == target:
            sig.mine += 1
            parent = owner.get(reply_to, "")
            if reply_to and parent and parent != target:
                sig.replied_others += 1
                addressed[nick.get(parent, "") or parent] = (
                    addressed.get(nick.get(parent, "") or parent, 0) + 1
                )
            for at_id in ats:
                if at_id and at_id != target:
                    sig.at_others += 1
                    addressed[nick.get(at_id, "") or at_id] = (
                        addressed.get(nick.get(at_id, "") or at_id, 0) + 1
                    )
            continue
        who = _name_of(row, nick)
        if reply_to and owner.get(reply_to, "") == target:
            sig.got_replies += 1
            responders[who] = responders.get(who, 0) + 1
        if target and target in ats:
            sig.got_at += 1
            responders[who] = responders.get(who, 0) + 1
    sig.responders = sorted(responders.items(), key=lambda kv: (-kv[1], kv[0]))
    sig.addressed = sorted(addressed.items(), key=lambda kv: (-kv[1], kv[0]))
    # 时间口径的「有人接话吗」：只在 TA 一段连续发言的最后一句结算，
    # 免得刷屏 5 条被算成 5 次；窗口最末一句不判定，那是取样边界不是冷场。
    people = list(ordered)
    for index, row in enumerate(people):
        if str(row.get("user_id") or "") != target or not target:
            continue
        nxt = people[index + 1] if index + 1 < len(people) else None
        if nxt is None or str(nxt.get("user_id") or "") == target:
            continue
        ts = int(row.get("ts") or 0)
        nts = int(nxt.get("ts") or 0)
        if ts and nts and nts - ts <= RESPONSE_WINDOW_SECONDS:
            sig.answered += 1
        else:
            sig.unanswered += 1
    return sig


# ---------------------------------------------------------------------------
# 对话现场
# ---------------------------------------------------------------------------


def anchor_indices(
    ordered: Sequence[dict[str, Any]],
    target_id: str,
) -> list[int]:
    """挑出「值得展开上下文」的位置：TA 自己说话、被回复、被 @。"""
    target = str(target_id or "")
    if not target:
        return []
    owner: dict[str, str] = {}
    for row in ordered:
        mid = str(row.get("message_id") or "")
        if mid:
            owner[mid] = str(row.get("user_id") or "")
    hits: list[int] = []
    for index, row in enumerate(ordered):
        uid = str(row.get("user_id") or "")
        if uid == target:
            hits.append(index)
            continue
        if owner.get(str(row.get("reply_to") or ""), "") == target:
            hits.append(index)
            continue
        if target in _split_ids(row.get("at_ids")):
            hits.append(index)
    return hits


def pick_evenly(items: Sequence[int], limit: int) -> list[int]:
    """从候选里均匀抽样，保证覆盖整个时间跨度而不是挤在一头。

    锚点下标和时间戳都用它抽 —— 都是「有序整数序列取代表」这同一件事。
    limit <= 0 视为不限量，原样返回。
    """
    total = len(items)
    if limit <= 0 or total <= limit:
        return list(items)
    step = total / float(limit)
    picked = [items[min(total - 1, int(i * step))] for i in range(limit)]
    return sorted(dict.fromkeys(picked))


def collect_windows(
    ordered: Sequence[dict[str, Any]],
    anchors: Sequence[int],
    *,
    context: int,
    max_lines: int,
) -> list[int]:
    """把锚点扩成上下文窗口并求并集，返回排好序的行下标。"""
    if not ordered or not anchors:
        return []
    span = max(0, int(context))
    keep: set[int] = set()
    for anchor in anchors:
        low = max(0, anchor - span)
        high = min(len(ordered) - 1, anchor + span)
        for index in range(low, high + 1):
            keep.add(index)
    picked = sorted(keep)
    if max_lines > 0 and len(picked) > max_lines:
        # 超预算时优先保留靠后（更新）的内容：近期语料更能代表当下的状态。
        picked = picked[-max_lines:]
    return picked


def message_owners(ordered: Sequence[dict[str, Any]]) -> dict[str, str]:
    """message_id → 发送者 uid，用来把 reply_to 还原成「回的是谁」。"""
    owner: dict[str, str] = {}
    for row in ordered:
        mid = str(row.get("message_id") or "")
        if mid:
            owner[mid] = str(row.get("user_id") or "")
    return owner


def grade_anchors(
    ordered: Sequence[dict[str, Any]],
    target_id: str,
) -> dict[int, int]:
    """给每个锚点分级：哪些位置最值得花预算展开成对话现场。

    TIER_EDGE 是真的有来有往（TA 回别人 / 别人回 TA / 互相 @），展开它最能看清
    TA 在关系里的样子；TIER_RUN 是 TA 连着说的一串，多半是在讲一件事；
    TIER_ALONE 是孤零零一句，信息量最低，只在预算有余时才展开。
    """
    target = str(target_id or "")
    if not target:
        return {}
    owner = message_owners(ordered)
    tiers: dict[int, int] = {}
    for index, row in enumerate(ordered):
        if not _text_of(row):
            #: 纯图 / 纯表情不能当锚点：它渲染不出内容，围着它铺一段现场等于
            #: 白花预算，还会让模型对着一行空气找上下文。
            continue
        uid = str(row.get("user_id") or "")
        reply_to = str(row.get("reply_to") or "")
        parent = owner.get(reply_to, "")
        ats = _split_ids(row.get("at_ids"))
        if uid and uid == target:
            # 回了谁（哪怕原话没在库里）、或 @ 了别人，都算一条对话边。
            outward = bool(reply_to and parent != target) or any(a and a != target for a in ats)
            tiers[index] = TIER_EDGE if outward else TIER_ALONE
            continue
        if (reply_to and parent == target) or target in ats:
            tiers[index] = TIER_EDGE
    total = len(ordered)
    for index, tier in list(tiers.items()):
        if tier != TIER_ALONE:
            continue
        for step in (-1, 1):
            near = index + step
            if 0 <= near < total and str(ordered[near].get("user_id") or "") == target:
                tiers[index] = TIER_RUN
                break
    return tiers



def select_lines(
    ordered: Sequence[dict[str, Any]],
    tiers: dict[int, int],
    *,
    context: int,
    max_lines: int,
    max_scenes: int,
    turn_gap: int = TURN_GAP_SECONDS,
) -> list[int]:
    """按锚点等级和回合边界挑出要渲染的行。

    context <= 0 时退回老行为（只要 TA 那几句，不带上下文），方便需要「纯语料」
    的场合复用。否则按回合展开：优先 TIER_EDGE，其次 TIER_RUN，最后 TIER_ALONE；
    每一级内部均匀抽样，保证覆盖整段时间而不是全挤在最新那几分钟。
    """
    if not ordered or not tiers:
        return []
    span = max(0, int(context))
    if span <= 0:
        anchors = pick_evenly(sorted(tiers), max_scenes)
        if max_lines > 0 and len(anchors) > max_lines:
            anchors = anchors[-max_lines:]
        return anchors
    turns = split_turns(ordered, gap=turn_gap)
    cap = max(2 * span + 1, TURN_LINE_CAP)
    budget = max_lines if max_lines > 0 else len(ordered)
    slots = max_scenes if max_scenes > 0 else len(tiers)
    keep: set[int] = set()
    used: set[int] = set()
    for tier in (TIER_EDGE, TIER_RUN, TIER_ALONE):
        if slots <= 0 or len(keep) >= budget:
            break
        anchors = [index for index, value in sorted(tiers.items()) if value == tier]
        anchors = [index for index in anchors if (turn_of(turns, index) or Turn(-1, -1)).start not in used]
        if not anchors:
            continue
        for anchor in pick_evenly(anchors, slots):
            turn = turn_of(turns, anchor)
            if turn is None or turn.start in used:
                continue
            used.add(turn.start)
            keep.update(clip_turn(turn, anchor, cap))
            slots -= 1
            if slots <= 0 or len(keep) >= budget:
                break
    picked = sorted(keep)
    if max_lines > 0 and len(picked) > max_lines:
        picked = picked[-max_lines:]
    return picked


def gap_mark(seconds: int) -> str:
    """把两段现场之间的空档写成人话。模型对「隔了多久」比「中间略」敏感得多。"""
    sec = max(0, int(seconds or 0))
    if sec >= 2 * 86400:
        return f"……（隔了 {sec // 86400} 天）……"
    if sec >= 90 * 60:
        return f"……（隔了 {sec // 3600} 小时）……"
    if sec >= 5 * 60:
        return f"……（隔了 {sec // 60} 分钟）……"
    return GAP_MARK


def is_gap_line(line: str) -> bool:
    """判断一行是不是空档提示（含 GAP_MARK 和「隔了 N 分钟」这类）。"""
    text = (line or "").strip()
    return text.startswith("……（") and text.endswith("）……")


def render_lines(
    ordered: Sequence[dict[str, Any]],
    indices: Sequence[int],
    target_id: str,
    *,
    names: dict[str, str] | None = None,
    self_id: str = "",
    with_clock: bool = True,
) -> list[str]:
    """把选中的行渲染成带标签的对话行。

    两处细节：断裂处按真实时长写成「隔了 N 分钟」，模型才知道这不是连着说的；
    被回复的原话如果没被选进来，就在那行之前补一条引用行。
    """
    nick = name_index(ordered, names)
    target = str(target_id or "")
    bot = str(self_id or "")
    owner = message_owners(ordered)
    slot: dict[str, int] = {}
    for index, row in enumerate(ordered):
        mid = str(row.get("message_id") or "")
        if mid:
            slot[mid] = index
    shown = set(indices)
    out: list[str] = []
    prev_index = -1
    prev_ts = 0
    for index in indices:
        row = ordered[index]
        body = _text_of(row)
        if not body:
            continue
        ts = int(row.get("ts") or 0)
        if prev_index >= 0 and (
            index - prev_index > 1 or (prev_ts and ts and ts - prev_ts > SCENE_GAP_SECONDS)
        ):
            out.append(gap_mark(ts - prev_ts if prev_ts and ts else 0))
        # 被回复的原话没被拉进现场时单独贴一行，否则「回应某人」是句空话。
        reply_id = str(row.get("reply_to") or "")
        origin = slot.get(reply_id, -1)
        if reply_id and origin >= 0 and origin not in shown:
            quoted = _text_of(ordered[origin])
            if quoted:
                owner_id = str(ordered[origin].get("user_id") or "")
                quoted_who = "TA" if owner_id and owner_id == target else _name_of(ordered[origin], nick)
                clipped = quoted if len(quoted) <= 40 else quoted[:40] + "…"
                out.append(f"{QUOTE_PREFIX} {quoted_who}：{clipped}")
        uid = str(row.get("user_id") or "")
        if uid and uid == target:
            label = LABEL_TARGET
        elif bot and uid == bot:
            label = LABEL_BOT
        else:
            label = LABEL_OTHER
        who = _name_of(row, nick)
        head = ""
        if with_clock and ts:
            head = time.strftime("[%m-%d %H:%M] ", time.localtime(ts))
        marks: list[str] = []
        parent = owner.get(str(row.get("reply_to") or ""), "")
        if parent:
            parent_name = "TA" if parent == target else nick.get(parent, "") or "某人"
            marks.append(f"回应{parent_name}")
        ats = [a for a in _split_ids(row.get("at_ids")) if a and a != uid]
        if ats:
            at_names = "、".join(("TA" if a == target else nick.get(a, "") or "某人") for a in ats[:2])
            marks.append(f"@{at_names}")
        joined = "/".join(marks)
        tag = f"（{joined}）" if marks else ""
        out.append(f"{head}{label} {who}{tag}：{body}")
        prev_index = index
        prev_ts = ts
    while out and is_gap_line(out[0]):
        out.pop(0)
    while out and is_gap_line(out[-1]):
        out.pop()
    return out


def build_dialogue_block(
    rows: Sequence[dict[str, Any]],
    target_id: str,
    *,
    names: dict[str, str] | None = None,
    context: int = 3,
    max_scenes: int = 12,
    max_lines: int = 80,
    self_id: str = "",
    with_clock: bool = True,
) -> str:
    """拼出「对话现场」文本块。没有可用内容时返回空串。

    context 决定一段现场铺多宽（回合太长时以锚点为中心裁 2*context+1 行起）；
    max_scenes 是最多展开几段现场（按时间均匀抽样，覆盖整段跨度而不是只看最新
    那几分钟）；max_lines 是总行数上限。
    """
    ordered = order_rows(rows)
    if not ordered:
        return ""
    tiers = grade_anchors(ordered, target_id)
    if not tiers:
        return ""
    indices = select_lines(
        ordered,
        tiers,
        context=context,
        max_lines=max_lines,
        max_scenes=max_scenes,
    )
    lines = render_lines(
        ordered,
        indices,
        target_id,
        names=names,
        self_id=self_id,
        with_clock=with_clock,
    )
    if not any(not is_gap_line(line) for line in lines):
        return ""
    return "\n".join(lines)


def has_other_voices(block: str) -> bool:
    """对话块里是否真的出现了别人的话。只有 TA 自己时就不值得单独占一段。"""
    return LABEL_OTHER in (block or "")
