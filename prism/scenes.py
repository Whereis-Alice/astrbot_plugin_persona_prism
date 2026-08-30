"""把「原话」还原成聊天现场。

卡片上的证供面板要长得像一张真实的聊天截图，就必须有别人的那几句话。模型
虽然从 v1.2.4 起能看到一段「对话现场」（见 dialogue 模块）用于理解上下文，
但气泡里的每个字都不能交给它转述 —— 让模型自己写别人说了什么等于请它编。

这里的做法是「模型只负责挑，程序负责还原」：

1. 模型输出 quote（本人的原话）+ reason；
2. 本模块把 quote 对回语料里的那条消息（允许截断和轻微改写）；
3. 再从本群语料里取这条消息前后各一句，拼成真实的对话气泡。

这样气泡里的每个字、每个昵称都来自数据库，模型没有机会虚构。
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .models import CorpusMessage, Evidence, Utterance

#: 比对原话时忽略的字符类别：Z=空白、P=标点、C=控制符。
#: 模型很爱顺手改标点、补句号，按字符类别归一化比维护标点表可靠。
_SKIP_CATEGORIES = frozenset({"Z", "P", "C"})

#: 相似度低于这个值就认为「对不上」。宁可不配对话，也不要配错人的话。
MATCH_FLOOR = 0.62

#: 为了凑出「有来有回」最多往外扩到前后各几条。再多气泡就挤爆卡片了。
MAX_SPAN = 4

#: 一段现场最多显示几个气泡（含本人那句）。奇数，好让本人那句居中。
SCENE_LIMIT = 7

#: 气泡里的媒体占位符。一屏「[图片][表情]」拼不出可信的现场，得先剔掉再数字数。
_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情|动画表情|贴纸|语音|视频|文件|音乐|分享|转发|合并转发|红包|位置|卡片|json|xml)"
    r"(?:×\d+)?\]",
)

#: 本人那句至少要有这么多真实文字，否则这段现场撑不起「证据」两个字。
OWN_TEXT_FLOOR = 2

#: 整段现场的真实文字合计下限。低于这个数基本是表情包刷屏。
#: 只卡到 4：「在吗 / 在的」这种也是真对话，不能因为字少就丢掉。
SCENE_TEXT_FLOOR = 4


#: 静默多久算「这一轮聊完了」。和提示词里那份「对话现场」用同一个口径，
#: 卡片上看到的气泡才和模型读到的上下文是同一段对话。
TURN_GAP_SECONDS = 8 * 60


@dataclass(slots=True, frozen=True)
class Turn:
    """一段「对话回合」：中间没有长时间静默的连续消息，start/end 均含。"""

    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def split_turns(
    ordered: Sequence[dict[str, Any]],
    *,
    gap: int = TURN_GAP_SECONDS,
) -> list[Turn]:
    """按静默时长把群历史切成一段段对话回合。

    群聊的一次来回往往横跨十几条、几分钟，中间还夹着别人插话；按「前后各 N 条」
    切会把一次完整的对话切断，也会把两个不相干的话题粘在一起。按静默切更贴近
    真实的聊天节奏：安静下来了，这一轮就算聊完了。
    """
    turns: list[Turn] = []
    if not ordered:
        return turns
    start = 0
    prev_ts = int(ordered[0].get("ts") or 0)
    for index in range(1, len(ordered)):
        ts = int(ordered[index].get("ts") or 0)
        if prev_ts and ts and ts - prev_ts > max(0, int(gap)):
            turns.append(Turn(start, index - 1))
            start = index
        prev_ts = ts or prev_ts
    turns.append(Turn(start, len(ordered) - 1))
    return turns


def turn_of(turns: Sequence[Turn], index: int) -> Turn | None:
    """找出某一行属于哪个回合。"""
    for turn in turns:
        if turn.start <= index <= turn.end:
            return turn
    return None


def clip_turn(turn: Turn, anchor: int, cap: int) -> list[int]:
    """回合太长时，以锚点为中心裁出一段，尽量把上下文对称留在两边。"""
    if cap <= 0 or turn.size <= cap:
        return list(range(turn.start, turn.end + 1))
    half = cap // 2
    low = max(turn.start, anchor - half)
    high = low + cap - 1
    if high > turn.end:
        high = turn.end
        low = high - cap + 1
    return list(range(low, high + 1))


def _norm(text: str) -> str:
    return "".join(
        ch
        for ch in str(text or "").lower()
        if unicodedata.category(ch)[0] not in _SKIP_CATEGORIES
    )


def _media_text(row: dict[str, Any]) -> str:
    """纯图消息的占位文本。只在少数需要"这里确实有条消息"的判断里用得上。"""
    images = int(row.get("images") or 0)
    if images > 1:
        return f"[图片×{images}]"
    return "[图片]" if images == 1 else ""


def real_text(text: str) -> str:
    """去掉媒体占位符后剩下的真实文字。"""
    return _PLACEHOLDER_RE.sub("", str(text or "")).strip()


def scene_has_substance(
    lines: Sequence[Utterance],
    *,
    own_floor: int = OWN_TEXT_FLOOR,
    total_floor: int = SCENE_TEXT_FLOOR,
) -> bool:
    """这段现场有没有真东西可看。

    只数真实文字：全是图片和表情包的窗口拼出来的「证据」既看不懂也不好看，
    不如退回单气泡。本人那句和整段各有一个下限。
    """
    if not lines:
        return False
    own = max((len(real_text(line.text)) for line in lines if line.mine), default=0)
    total = sum(len(real_text(line.text)) for line in lines)
    return own >= max(0, own_floor) and total >= max(0, total_floor)


def locate_quote(quote: str, messages: Sequence[CorpusMessage]) -> CorpusMessage | None:
    """把模型给的原话对回语料里的那条消息。对不上返回 None。"""
    needle = _norm(quote)
    if not needle:
        return None
    best: CorpusMessage | None = None
    best_score = 0.0
    for msg in messages:
        hay = _norm(msg.text)
        if not hay:
            continue
        if hay == needle:
            return msg
        if needle in hay:
            score = 0.75 + 0.25 * (len(needle) / len(hay))
        elif hay in needle:
            score = 0.70 + 0.25 * (len(hay) / len(needle))
        else:
            score = SequenceMatcher(None, needle, hay).ratio()
        if score > best_score:
            best, best_score = msg, score
    return best if best_score >= MATCH_FLOOR else None


def rows_to_utterances(
    rows: Sequence[dict[str, Any]],
    user_id: str,
    *,
    names: dict[str, str] | None = None,
    self_id: str = "",
) -> list[Utterance]:
    """把一段连续语料渲染成气泡序列。

    每个气泡都带上说话人的 uid，卡片才能给在场的每一个人取真头像 —— 只有主角
    有头像、其他人是灰圆牌的现场，看着就不像真的聊天。
    """
    nick = dict(names or {})
    out: list[Utterance] = []
    for row in rows:
        #: 图片、表情、语音一律不进气泡：卡片上一串「[图片]」既看不懂又像 bug，
        #: 剔掉后整条没字了就跳过这条消息。
        text = real_text(row.get("text") or "")
        if not text:
            continue
        uid = str(row.get("user_id") or "")
        name = nick.get(uid, "") or str(row.get("user_name") or "").strip() or uid or "群友"
        ts = int(row.get("ts") or 0)
        clock = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
        out.append(
            Utterance(
                speaker=name,
                text=text,
                mine=bool(uid) and uid == user_id,
                clock=clock,
                user_id=uid,
                is_bot=bool(self_id) and uid == str(self_id) and uid != str(user_id),
            ),
        )
    return out


def _visible(row: dict[str, Any]) -> bool:
    """这一行在气泡里会不会真的显示出来（纯图 / 纯表情不算）。"""
    return bool(real_text(row.get("text") or ""))


def _others_in(rows: Sequence[dict[str, Any]], user_id: str) -> int:
    """窗口里有几条是别人说的（且真的会显示）。"""
    target = str(user_id or "")
    return sum(
        1
        for row in rows
        if str(row.get("user_id") or "") != target and _visible(row)
    )


def _locate_index(
    ordered: Sequence[dict[str, Any]],
    *,
    message_id: str = "",
    center_ts: int = 0,
) -> int:
    """在已排好序的语料里定位锚点那条，找不到返回 -1。"""
    if message_id:
        for pos, row in enumerate(ordered):
            if str(row.get("message_id") or "") == message_id:
                return pos
    if center_ts and ordered:
        # 消息 ID 对不上（协议端改过 ID、或语料被清理过）时退回按时间就近。
        return min(
            range(len(ordered)),
            key=lambda pos: abs(int(ordered[pos].get("ts") or 0) - int(center_ts)),
        )
    return -1


def slice_around(
    rows: Sequence[dict[str, Any]],
    *,
    message_id: str = "",
    center_ts: int = 0,
    context: int = 1,
    user_id: str = "",
    min_others: int = 0,
    max_span: int = MAX_SPAN,
) -> list[dict[str, Any]]:
    """在一段本群语料里定位中心那条，取它前后各 context 条。

    min_others > 0 时会继续往外扩窗，直到窗口里至少有这么多条别人的发言
    （上限 max_span）—— 一个人连着说好几句时，只取 ±1 会拼出「三个气泡都是
    他自己」的假对话，看不出这是在跟谁说话。
    """
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: (int(r.get("ts") or 0), str(r.get("message_id") or "")))
    index = _locate_index(ordered, message_id=message_id, center_ts=center_ts)
    if index < 0:
        return []
    span = max(0, context)

    def window(width: int) -> list[dict[str, Any]]:
        return list(ordered[max(0, index - width) : index + width + 1])

    picked = window(span)
    if min_others > 0 and user_id:
        limit = max(span, int(max_span))
        while span < limit and _others_in(picked, user_id) < min_others:
            span += 1
            wider = window(span)
            if len(wider) == len(picked):
                break  # 已经把整段语料吃完了，再扩也没有新内容
            picked = wider
    return picked


def scene_window(
    rows: Sequence[dict[str, Any]],
    *,
    message_id: str = "",
    center_ts: int = 0,
    user_id: str = "",
    min_others: int = 1,
    cap: int = SCENE_LIMIT,
    turn_gap: int = TURN_GAP_SECONDS,
) -> list[dict[str, Any]]:
    """取出锚点所在的那一整轮对话，作为卡片上要还原的现场。

    「前后各 N 条」切出来的现场经常是半截话：一次来回在群里往往横跨十几条，
    中间还夹着别人插话。这里改成先按静默把群历史切成一轮轮对话，取锚点所在的
    那一轮，太长再以锚点为中心裁 —— 和模型读到的上下文口径一致。

    整轮里仍然只有本人一个人在说（自言自语的那种）时，退回按条数往外扩，
    至少凑出一句别人的话，不然气泡看着像在对空气讲。
    """
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: (int(r.get("ts") or 0), str(r.get("message_id") or "")))
    index = _locate_index(ordered, message_id=message_id, center_ts=center_ts)
    if index < 0:
        return []
    turns = split_turns(ordered, gap=turn_gap)
    turn = turn_of(turns, index)
    picked: list[dict[str, Any]] = []
    if turn is not None:
        picked = [ordered[pos] for pos in clip_turn(turn, index, max(1, int(cap)))]
    if picked and (min_others <= 0 or _others_in(picked, user_id) >= min_others):
        return picked
    widened = slice_around(
        ordered,
        message_id=message_id,
        center_ts=center_ts,
        context=1,
        user_id=user_id,
        min_others=min_others,
    )
    #: 两条路都没凑出别人的话时，取内容更多的那一段，别把现场缩成一个孤零零的气泡。
    if _others_in(widened, user_id) > _others_in(picked, user_id):
        return widened
    return picked or widened


def center_scene(lines: Sequence[Utterance], limit: int = SCENE_LIMIT) -> list[Utterance]:
    """气泡太多时以本人那句为中心裁剪，别把主角裁掉。"""
    items = list(lines)
    if limit <= 0 or len(items) <= limit:
        return items
    center = next((i for i, line in enumerate(items) if line.mine), len(items) // 2)
    half = limit // 2
    start = max(0, min(center - half, len(items) - limit))
    return items[start : start + limit]


def scene_title(ts: int, label: str = "现场片段") -> str:
    """给证供配一个带时间的小标题，像截图上的时间戳。"""
    if not ts:
        return label
    clock = time.strftime("%H:%M", time.localtime(int(ts)))
    return f"{clock} · {label}"


def enrich_evidence(
    item: Evidence,
    messages: Sequence[CorpusMessage],
    rows: Sequence[dict[str, Any]],
    *,
    user_id: str,
    names: dict[str, str] | None = None,
    context: int = 1,
    label: str = "现场片段",
    min_others: int = 1,
    self_id: str = "",
) -> bool:
    """给一条证供补上真实对话。补上了返回 True。

    min_others 默认为 1：宁可多翻两条，也要让这段现场看得出是在跟人说话。
    """
    if item.dialogue:
        return False
    hit = locate_quote(item.quote, messages)
    if hit is None:
        return False
    window = scene_window(
        rows,
        message_id=str(hit.message_id or ""),
        center_ts=int(hit.ts or 0),
        user_id=user_id,
        min_others=min_others,
        cap=max(SCENE_LIMIT, 2 * max(0, context) + 1),
    )
    dialogue = rows_to_utterances(window, user_id, names=names, self_id=self_id)
    if not any(line.mine for line in dialogue):
        # 没定位到本人那句就别硬拼，交给 Evidence.scene_lines 用 quote 兜底。
        return False
    picked = center_scene(dialogue, SCENE_LIMIT)
    if not scene_has_substance(picked):
        # 整段都是表情包/图片，拼出来也读不出东西，退回单气泡。
        return False
    item.dialogue = picked
    if not item.title:
        item.title = scene_title(int(hit.ts or 0), label)
    return True


def enrich_all(
    items: Sequence[Evidence],
    messages: Sequence[CorpusMessage],
    rows: Sequence[dict[str, Any]],
    *,
    user_id: str,
    names: dict[str, str] | None = None,
    context: int = 1,
    label: str = "现场片段",
    min_others: int = 1,
    self_id: str = "",
) -> int:
    """批量补全，返回成功补上对话的条数。"""
    filled = 0
    for item in items:
        if enrich_evidence(
            item,
            messages,
            rows,
            user_id=user_id,
            names=names,
            context=context,
            label=label,
            min_others=min_others,
            self_id=self_id,
        ):
            filled += 1
    return filled
