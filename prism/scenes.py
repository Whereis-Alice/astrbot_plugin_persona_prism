"""把「原话」还原成聊天现场。

卡片上的证供面板要长得像一张真实的聊天截图，就必须有别人的那几句话。但
送给 LLM 的语料只包含被画像者本人的发言（这是刻意的：范围越小越不容易被
旁人的话带偏，也更省 token），所以让模型自己写出别人说了什么等于请它编。

这里的做法是「模型只负责挑，程序负责还原」：

1. 模型输出 quote（本人的原话）+ reason；
2. 本模块把 quote 对回语料里的那条消息（允许截断和轻微改写）；
3. 再从本群语料里取这条消息前后各一句，拼成真实的对话气泡。

这样气泡里的每个字、每个昵称都来自数据库，模型没有机会虚构。
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Any

from .models import CorpusMessage, Evidence, Utterance

#: 比对原话时忽略的字符类别：Z=空白、P=标点、C=控制符。
#: 模型很爱顺手改标点、补句号，按字符类别归一化比维护标点表可靠。
_SKIP_CATEGORIES = frozenset({"Z", "P", "C"})

#: 相似度低于这个值就认为「对不上」。宁可不配对话，也不要配错人的话。
MATCH_FLOOR = 0.62


def _norm(text: str) -> str:
    return "".join(
        ch
        for ch in str(text or "").lower()
        if unicodedata.category(ch)[0] not in _SKIP_CATEGORIES
    )


def _media_text(row: dict[str, Any]) -> str:
    """纯图消息在气泡里也要占一行，否则对话会莫名断裂。"""
    images = int(row.get("images") or 0)
    if images > 1:
        return f"[图片×{images}]"
    return "[图片]" if images == 1 else ""


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
) -> list[Utterance]:
    """把一段连续语料渲染成气泡序列。"""
    nick = dict(names or {})
    out: list[Utterance] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            text = _media_text(row)
            if not text:
                continue
        uid = str(row.get("user_id") or "")
        name = nick.get(uid, "") or str(row.get("user_name") or "").strip() or uid or "群友"
        out.append(Utterance(speaker=name, text=text, mine=bool(uid) and uid == user_id))
    return out


def slice_around(
    rows: Sequence[dict[str, Any]],
    *,
    message_id: str = "",
    center_ts: int = 0,
    context: int = 1,
) -> list[dict[str, Any]]:
    """在一段本群语料里定位中心那条，取它前后各 context 条。"""
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: (int(r.get("ts") or 0), str(r.get("message_id") or "")))
    index = -1
    if message_id:
        for pos, row in enumerate(ordered):
            if str(row.get("message_id") or "") == message_id:
                index = pos
                break
    if index < 0 and center_ts:
        # 消息 ID 对不上（协议端改过 ID、或语料被清理过）时退回按时间就近。
        index = min(
            range(len(ordered)),
            key=lambda pos: abs(int(ordered[pos].get("ts") or 0) - int(center_ts)),
        )
    if index < 0:
        return []
    span = max(0, context)
    return list(ordered[max(0, index - span) : index + span + 1])


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
) -> bool:
    """给一条证供补上真实对话。补上了返回 True。"""
    if item.dialogue:
        return False
    hit = locate_quote(item.quote, messages)
    if hit is None:
        return False
    window = slice_around(
        rows,
        message_id=str(hit.message_id or ""),
        center_ts=int(hit.ts or 0),
        context=context,
    )
    dialogue = rows_to_utterances(window, user_id, names=names)
    if not any(line.mine for line in dialogue):
        # 没定位到本人那句就别硬拼，交给 Evidence.scene_lines 用 quote 兜底。
        return False
    item.dialogue = dialogue
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
        ):
            filled += 1
    return filled
