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

#: 对话行的说话人标签。故意用中文短标签而不是 [Target]/[Other]：
#: 中文模型对中文标签的指代把握更稳，也省 token。
LABEL_TARGET = "[TA]"
LABEL_OTHER = "[其他人]"
LABEL_BOT = "[机器人]"

#: 片段之间的断裂提示。
GAP_MARK = "……（中间略）……"

#: 时间间隔超过这个秒数就算两个不同的场景，即使索引相邻。
SCENE_GAP_SECONDS = 20 * 60


def _split_ids(raw: Any) -> list[str]:
    """把逗号分隔的 at_ids 拆成 uid 列表。"""
    text = str(raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _text_of(row: dict[str, Any]) -> str:
    """取一行的可读正文；纯图消息也要占位，否则对话会莫名断裂。"""
    text = str(row.get("text") or "").strip()
    if text:
        return text
    images = int(row.get("images") or 0)
    if images > 1:
        return f"[图片×{images}]"
    return "[图片]" if images == 1 else ""


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
            lines.append("- 窗口内没有检测到别人回复或 @ TA（可能确实少人接话，也可能协议端没上报引用关系）")
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
    """统计 TA 与别人的互动结构。只看 reply_to / at_ids，不猜。"""
    target = str(target_id or "")
    bot = str(self_id or "")
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
        if bot and uid == bot:
            continue
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
                if at_id and at_id != target and at_id != bot:
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


def render_lines(
    ordered: Sequence[dict[str, Any]],
    indices: Sequence[int],
    target_id: str,
    *,
    names: dict[str, str] | None = None,
    self_id: str = "",
    with_clock: bool = True,
) -> list[str]:
    """把选中的行渲染成带标签的对话行，断裂处插入省略提示。"""
    nick = name_index(ordered, names)
    target = str(target_id or "")
    bot = str(self_id or "")
    owner: dict[str, str] = {}
    for row in ordered:
        mid = str(row.get("message_id") or "")
        if mid:
            owner[mid] = str(row.get("user_id") or "")
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
            out.append(GAP_MARK)
        uid = str(row.get("user_id") or "")
        if bot and uid == bot:
            label = LABEL_BOT
        elif uid and uid == target:
            label = LABEL_TARGET
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
    while out and out[0] == GAP_MARK:
        out.pop(0)
    while out and out[-1] == GAP_MARK:
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

    context 指锚点前后各取几条；max_scenes 限制展开多少个锚点（均匀抽样，
    保证覆盖整段时间而不是全挤在最新那几分钟）；max_lines 是总行数上限。
    """
    ordered = order_rows(rows)
    if not ordered:
        return ""
    anchors = pick_evenly(anchor_indices(ordered, target_id), max_scenes)
    if not anchors:
        return ""
    indices = collect_windows(ordered, anchors, context=context, max_lines=max_lines)
    lines = render_lines(
        ordered,
        indices,
        target_id,
        names=names,
        self_id=self_id,
        with_clock=with_clock,
    )
    if not any(line != GAP_MARK for line in lines):
        return ""
    return "\n".join(lines)


def has_other_voices(block: str) -> bool:
    """对话块里是否真的出现了别人的话。只有 TA 自己时就不值得单独占一段。"""
    return LABEL_OTHER in (block or "")
