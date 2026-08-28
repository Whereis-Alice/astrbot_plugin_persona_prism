"""语料整理：去重、排序、抽样、统计。

这一层是纯函数，没有任何 AstrBot 依赖，便于直接单测。它修掉了上游最
影响画像质量的几个问题：

* 上游反向翻页时逐页 append，页内又是正序，整体顺序是乱的，但提示词
  却声称"按时间顺序排列"。这里统一按时间戳升序重排。
* 上游没有 message_id 去重，重复页会把同一条话喂两遍。
* 上游超预算时直接 texts[:max]，等于只看一段时间的表现。这里改成
  分层抽样：一半预算留给最近的发言，另一半在整个时间跨度上均匀取样。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from .models import CorpusBundle, CorpusMessage, CorpusStats
from .privacy import (
    has_emoji,
    informative,
    is_command_like,
    normalize_text,
    sanitize_for_prompt,
)

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
_QUESTION_RE = re.compile(r"[?？]|(?:^|[^\w])(?:吗|呢|么|咋|怎么|为什么|难道|是不是)")
_MENTION_RE = re.compile(r"@\S")

#: 无区分度的中文单字。extract_terms 用 bigram 提词，只要 bigram 的任一字
#: 命中这里就丢掉，所以这份表必须是"单字"粒度（这里曾经写成整行字符串再
#: .split()，结果每行被当成一个超长词，等于停用词完全失效）。
_CJK_STOP_CHARS = (
    "的了是我你他她它们在有和就不人都一上也很到说要去会着没看好自己这那个"
    "什么怎么可以但因所以如果而且还这我们你知道觉得"
    "时候现真其实应该能已经不"
)

#: 无区分度的英文词。
_LATIN_STOPWORDS: tuple[str, ...] = (
    "the",
    "and",
    "that",
    "you",
    "for",
    "was",
    "are",
    "with",
    "this",
    "have",
    "not",
    "but",
    "from",
    "they",
    "will",
    "what",
    "when",
    "your",
    "can",
    "just",
    "like",
    "about",
    "there",
    "their",
    "would",
    "could",
    "should",
)

#: 只挡掉最没有区分度的一批，保留"卧槽""草"这类有性格色彩的词。
STOPWORDS: frozenset[str] = frozenset(
    list(_CJK_STOP_CHARS) + list(_LATIN_STOPWORDS) + list("啊呀哦嗯哈呵吧嘛咯啦哇欸诶唉噢喔"),
)


# ---------------------------------------------------------------------------
# OneBot 历史消息解析
# ---------------------------------------------------------------------------


def parse_onebot_segments(segments: Iterable[Any]) -> tuple[str, bool]:
    """把 OneBot 消息段列表压成一行文本。

    上游只保留 text 段，@ 和引用直接丢掉。但"这个人爱不爱 @ 别人""是不是
    总在接话"本身就是重要的性格信号，所以这里保留成可读标记。
    """
    parts: list[str] = []
    is_reply = False
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        elif seg_type == "at":
            target = str(data.get("qq") or "")
            if target == "all":
                parts.append("@全体成员")
            else:
                name = str(data.get("name") or "").strip()
                parts.append(f"@{name or target}")
        elif seg_type == "reply":
            is_reply = True
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type in {"record", "video"}:
            parts.append("[语音]" if seg_type == "record" else "[视频]")
    return " ".join(p for p in parts if p).strip(), is_reply


def parse_history_page(page: Iterable[Any]) -> list[dict[str, Any]]:
    """把一页 get_group_msg_history 的返回整理成扁平记录。"""
    rows: list[dict[str, Any]] = []
    for raw in page or []:
        if not isinstance(raw, dict):
            continue
        message_id = raw.get("message_id")
        if message_id in (None, ""):
            continue
        sender = raw.get("sender") or {}
        text, is_reply = parse_onebot_segments(raw.get("message") or [])
        row: dict[str, Any] = {
            "message_id": str(message_id),
            "user_id": str(sender.get("user_id") or raw.get("user_id") or ""),
            "user_name": str(sender.get("card") or sender.get("nickname") or ""),
            "text": text,
            "ts": int(raw.get("time") or 0),
            "is_reply": is_reply,
        }
        # 顺手把 message_seq / real_seq 带上：get_group_msg_history 的翻页参数虽然
        # 叫 message_seq，但各协议端认的到底是它还是 message_id 并不统一，两个都留着
        # 才能让 prism.history 逐个试。哪一种可用由 _backfill / 「棱镜诊断」实测决定。
        seq = raw.get("message_seq")
        if seq in (None, ""):
            seq = raw.get("real_seq")
        if seq not in (None, ""):
            row["message_seq"] = str(seq)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 清洗 / 去重 / 排序
# ---------------------------------------------------------------------------


def clean_rows(
    rows: Iterable[dict[str, Any]],
    *,
    min_chars: int = 2,
    filter_commands: bool = True,
    drop_urls: bool = True,
    redact: bool = True,
) -> list[CorpusMessage]:
    """清洗成 CorpusMessage 列表，顺带按 message_id 去重并按时间升序排序。"""
    seen: set[str] = set()
    result: list[CorpusMessage] = []
    for row in rows:
        message_id = str(row.get("message_id") or "")
        if message_id and message_id in seen:
            continue
        raw_text = str(row.get("text") or "")
        if filter_commands and is_command_like(raw_text):
            continue
        text = normalize_text(raw_text, redact=redact, drop_urls=drop_urls)
        if not informative(text, min_chars):
            continue
        if message_id:
            seen.add(message_id)
        result.append(
            CorpusMessage(
                message_id=message_id,
                user_id=str(row.get("user_id") or ""),
                user_name=str(row.get("user_name") or ""),
                text=text,
                ts=int(row.get("ts") or 0),
                is_reply=bool(row.get("is_reply")),
            ),
        )
    result.sort(key=lambda m: (m.ts, m.message_id))
    return result


def fold_repeats(messages: Sequence[CorpusMessage]) -> list[CorpusMessage]:
    """把连续或高频重复的同一句话折叠为一条，重复次数记在 repeat 上。"""
    counts = Counter(m.text for m in messages)
    folded: list[CorpusMessage] = []
    emitted: set[str] = set()
    for msg in messages:
        total = counts[msg.text]
        if total == 1:
            folded.append(msg)
            continue
        if msg.text in emitted:
            continue
        emitted.add(msg.text)
        folded.append(
            CorpusMessage(
                message_id=msg.message_id,
                user_id=msg.user_id,
                user_name=msg.user_name,
                text=msg.text,
                ts=msg.ts,
                is_reply=msg.is_reply,
                repeat=total,
            ),
        )
    folded.sort(key=lambda m: (m.ts, m.message_id))
    return folded


def layered_sample(
    messages: Sequence[CorpusMessage],
    budget: int,
    *,
    recent_share: float = 0.5,
) -> list[CorpusMessage]:
    """超预算时的分层抽样。

    近期发言最能代表"现在的这个人"，但只看近期会漏掉长期习惯，所以：
    一半预算给最近的连续片段，另一半在剩下的历史里等距取样。
    结果重新按时间升序排列，保证提示词里的"按时间顺序"名副其实。
    """
    if budget <= 0 or len(messages) <= budget:
        return list(messages)
    recent_size = max(1, min(budget - 1, int(budget * recent_share)))
    recent = list(messages[-recent_size:])
    history = list(messages[:-recent_size])
    remaining = budget - recent_size
    if remaining <= 0 or not history:
        return recent
    if len(history) <= remaining:
        picked = history
    else:
        step = len(history) / remaining
        picked = [history[min(len(history) - 1, int(i * step))] for i in range(remaining)]
    merged = {id(m): m for m in picked + recent}
    result = list(merged.values())
    result.sort(key=lambda m: (m.ts, m.message_id))
    return result


def extract_terms(texts: Iterable[str], limit: int = 15) -> list[tuple[str, int]]:
    """无分词依赖的高频词提取。

    中文用 bigram（相邻两字），英文用单词。不引 jieba 是为了让插件保持
    零重依赖；bigram 对"口头禅"这类特征其实相当有效。
    """
    counter: Counter[str] = Counter()
    for text in texts:
        for word in _LATIN_WORD_RE.findall(text):
            lowered = word.lower()
            if lowered not in STOPWORDS:
                counter[lowered] += 1
        for run in _CJK_RUN_RE.findall(text):
            for i in range(len(run) - 1):
                gram = run[i : i + 2]
                if gram[0] in STOPWORDS or gram[1] in STOPWORDS:
                    continue
                if gram in STOPWORDS:
                    continue
                counter[gram] += 1
    ranked = [(term, count) for term, count in counter.most_common(limit * 4) if count >= 2]
    return _drop_shifted_bigrams(ranked)[:limit]


def _drop_shifted_bigrams(ranked: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """去掉 bigram 的"错位残渣"。

    "打游戏打游戏" 会同时产出 打游 / 游戏 / 戏打，只有"游戏"是真词。做法是按
    频次从高到低贪心保留：如果候选词的首尾字与某个已保留的更高频词首尾相接，
    就认为它是错位切出来的碎片，丢掉。
    代价是"游戏"与"戏剧"这类真实重叠词里频次低的那个也可能被误杀，但相比满屏
    碎片，这个取舍更划算。
    """
    kept: list[tuple[str, int]] = []
    for term, count in ranked:
        if len(term) == 2 and any(
            len(other) == 2 and other_count > count and (term[0] == other[1] or term[1] == other[0])
            for other, other_count in kept
        ):
            continue
        kept.append((term, count))
    return kept


def compute_stats(
    sampled: Sequence[CorpusMessage],
    *,
    total: int | None = None,
) -> CorpusStats:
    """算出交给模型的客观统计特征。

    让模型看到"日均 12 条、23 点最活跃、提问率 8%"这样的硬事实，比让它
    自己从几百行语料里估算要准得多，也能压掉一部分臆测。
    """
    if not sampled:
        return CorpusStats(total=total or 0)
    texts = [m.text for m in sampled]
    chars = sum(len(t) for t in texts)
    stamps = [m.ts for m in sampled if m.ts]
    span_days = ((max(stamps) - min(stamps)) / 86400.0) if len(stamps) >= 2 else 0.0
    count = len(sampled)
    hours = Counter((m.ts // 3600 + 8) % 24 for m in sampled if m.ts)
    return CorpusStats(
        total=total if total is not None else count,
        sampled=count,
        chars=chars,
        avg_chars=chars / count,
        span_days=span_days,
        daily_rate=(count / span_days) if span_days >= 0.5 else float(count),
        question_ratio=sum(1 for t in texts if _QUESTION_RE.search(t)) / count,
        mention_ratio=sum(1 for t in texts if _MENTION_RE.search(t)) / count,
        reply_ratio=sum(1 for m in sampled if m.is_reply) / count,
        repeat_ratio=sum(1 for m in sampled if m.repeat > 1) / count,
        emoji_ratio=sum(1 for t in texts if has_emoji(t)) / count,
        longest=max(len(t) for t in texts),
        active_hours=hours.most_common(6),
        top_terms=extract_terms(texts),
    )


def extract_partners(
    messages: Sequence[CorpusMessage],
    *,
    limit: int = 6,
) -> list[tuple[str, int]]:
    """统计这个人最常 @ 谁 —— 姻缘 / 社交类画像需要。"""
    counter: Counter[str] = Counter()
    for msg in messages:
        for name in re.findall(r"@([^\s@]{1,20})", msg.text):
            if name in {"全体成员"}:
                continue
            counter[name] += 1
    return counter.most_common(limit)


def build_bundle(
    rows: Iterable[dict[str, Any]],
    *,
    max_messages: int = 400,
    min_chars: int = 2,
    filter_commands: bool = True,
    drop_urls: bool = True,
    redact: bool = True,
    fold: bool = True,
    sampling: str = "layered",
    scanned: int = 0,
    from_cache: bool = False,
    quote_limit: int = 600,
) -> CorpusBundle:
    """一步到位：清洗 → 折叠 → 抽样 → 统计。"""
    cleaned = clean_rows(
        rows,
        min_chars=min_chars,
        filter_commands=filter_commands,
        drop_urls=drop_urls,
        redact=redact,
    )
    if fold:
        cleaned = fold_repeats(cleaned)
    total = len(cleaned)
    if sampling == "recent":
        sampled = cleaned[-max_messages:] if max_messages > 0 else list(cleaned)
    else:
        sampled = layered_sample(cleaned, max_messages)
    for msg in sampled:
        msg.text = sanitize_for_prompt(msg.text, quote_limit)
    return CorpusBundle(
        messages=sampled,
        stats=compute_stats(sampled, total=total),
        scanned=scanned or total,
        from_cache=from_cache,
        partners=extract_partners(sampled),
    )
