"""人格棱镜的数据模型。

这一层刻意不依赖 AstrBot 运行时，方便单测直接导入。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 语料
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CorpusMessage:
    """一条参与画像的发言。"""

    message_id: str
    user_id: str
    user_name: str = ""
    text: str = ""
    ts: int = 0
    is_reply: bool = False
    repeat: int = 1
    reply_to: str = ""
    images: int = 0
    at_ids: str = ""

    def as_line(self, index: int | None = None) -> str:
        """渲染成喂给模型的一行语料。"""
        stamp = time.strftime("%m-%d %H:%M", time.localtime(self.ts)) if self.ts else "--"
        prefix = f"[{stamp}]"
        if index is not None:
            prefix = f"{index:>3}. {prefix}"
        body = self.text
        if self.is_reply:
            body = f"(回复他人) {body}"
        if self.repeat > 1:
            body = f"{body} ×{self.repeat}"
        return f"{prefix} {body}"


@dataclass(slots=True)
class CorpusStats:
    """本地算出来的客观统计特征，作为模型的事实锚点。"""

    total: int = 0
    sampled: int = 0
    chars: int = 0
    avg_chars: float = 0.0
    span_days: float = 0.0
    daily_rate: float = 0.0
    question_ratio: float = 0.0
    mention_ratio: float = 0.0
    reply_ratio: float = 0.0
    repeat_ratio: float = 0.0
    emoji_ratio: float = 0.0
    longest: int = 0
    active_hours: list[tuple[int, int]] = field(default_factory=list)
    top_terms: list[tuple[str, int]] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        """把统计特征渲染成简洁的事实清单。"""
        lines = [
            f"- 语料总量：{self.total} 条（送入本次分析 {self.sampled} 条，{self.chars} 字）",
            f"- 平均每条 {self.avg_chars:.1f} 字，最长 {self.longest} 字",
        ]
        if self.span_days >= 0.5:
            lines.append(
                f"- 时间跨度约 {self.span_days:.1f} 天，日均 {self.daily_rate:.1f} 条",
            )
        lines.append(
            f"- 提问句占比 {self.question_ratio:.0%}，"
            f"@他人占比 {self.mention_ratio:.0%}，"
            f"回复他人占比 {self.reply_ratio:.0%}",
        )
        if self.emoji_ratio > 0:
            lines.append(f"- 含表情/颜文字的发言占比 {self.emoji_ratio:.0%}")
        if self.repeat_ratio > 0:
            lines.append(f"- 重复刷同一句话的发言占比 {self.repeat_ratio:.0%}")
        if self.active_hours:
            hot = "、".join(f"{hour:02d}点({count}条)" for hour, count in self.active_hours[:4])
            lines.append(f"- 活跃时段集中在 {hot}")
        if self.top_terms:
            terms = "、".join(f"{term}({count})" for term, count in self.top_terms[:12])
            lines.append(f"- 高频用词：{terms}")
        return "\n".join(lines)


@dataclass(slots=True)
class CorpusBundle:
    """一次分析所需的全部语料上下文。"""

    messages: list[CorpusMessage] = field(default_factory=list)
    stats: CorpusStats = field(default_factory=CorpusStats)
    scanned: int = 0
    from_cache: bool = False
    partners: list[tuple[str, int]] = field(default_factory=list)

    @property
    def enough(self) -> bool:
        return bool(self.messages)

    def to_transcript(self) -> str:
        return "\n".join(msg.as_line(i + 1) for i, msg in enumerate(self.messages))


# ---------------------------------------------------------------------------
# 成员资料
# ---------------------------------------------------------------------------

#: 允许暴露给 LLM 的字段白名单。手机号 / 邮箱 / 地址等一律不在此列。
PROFILE_FIELD_LABELS: dict[str, str] = {
    "nickname": "昵称",
    "card": "群名片",
    "sex": "性别",
    "age": "年龄",
    "long_nick": "个性签名",
    "birthday": "生日",
    "join_time": "入群时间",
    "last_sent_time": "最后发言时间",
    "level": "群等级",
    "title": "群头衔",
    "area": "地区",
    "role": "群身份",
}


@dataclass(slots=True)
class MemberProfile:
    """群成员的公开资料，字段全部可选。"""

    user_id: str = ""
    nickname: str = ""
    card: str = ""
    sex: str = ""
    age: str = ""
    long_nick: str = ""
    birthday: str = ""
    join_time: str = ""
    last_sent_time: str = ""
    level: str = ""
    title: str = ""
    area: str = ""
    role: str = ""

    @property
    def display_name(self) -> str:
        return self.card or self.nickname or self.user_id or "未知用户"

    def to_prompt_block(self, allowed: list[str]) -> str:
        """按白名单渲染资料块；不在白名单内的字段直接不出现。"""
        parts: list[str] = []
        for key in allowed:
            label = PROFILE_FIELD_LABELS.get(key)
            if not label:
                continue
            value = str(getattr(self, key, "") or "").strip()
            if not value:
                continue
            parts.append(f"- {label}：{value}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 画像结果
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Tag:
    label: str
    polarity: str = "neutral"


@dataclass(slots=True)
class Dimension:
    name: str
    score: int = 50
    note: str = ""


@dataclass(slots=True)
class Section:
    title: str
    body: str = ""


@dataclass(slots=True)
class Evidence:
    quote: str
    reason: str = ""


@dataclass(slots=True)
class Portrait:
    """结构化画像结果。解析失败时退化为只有 headline + sections 的形态。"""

    kind: str = "portrait"
    headline: str = ""
    tags: list[Tag] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    confidence: float = 0.0
    raw_text: str = ""
    structured: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "headline": self.headline,
            "tags": [{"label": t.label, "polarity": t.polarity} for t in self.tags],
            "dimensions": [{"name": d.name, "score": d.score, "note": d.note} for d in self.dimensions],
            "sections": [{"title": s.title, "body": s.body} for s in self.sections],
            "evidence": [{"quote": e.quote, "reason": e.reason} for e in self.evidence],
            "advice": list(self.advice),
            "confidence": round(self.confidence, 3),
            "structured": self.structured,
            "raw_text": self.raw_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Portrait:
        return cls(
            kind=str(data.get("kind") or "portrait"),
            headline=str(data.get("headline") or ""),
            tags=[
                Tag(str(t.get("label") or ""), str(t.get("polarity") or "neutral"))
                for t in data.get("tags") or []
                if isinstance(t, dict) and t.get("label")
            ],
            dimensions=[
                Dimension(
                    str(d.get("name") or ""),
                    int(d.get("score") or 0),
                    str(d.get("note") or ""),
                )
                for d in data.get("dimensions") or []
                if isinstance(d, dict) and d.get("name")
            ],
            sections=[
                Section(str(s.get("title") or ""), str(s.get("body") or ""))
                for s in data.get("sections") or []
                if isinstance(s, dict) and (s.get("title") or s.get("body"))
            ],
            evidence=[
                Evidence(str(e.get("quote") or ""), str(e.get("reason") or ""))
                for e in data.get("evidence") or []
                if isinstance(e, dict) and e.get("quote")
            ],
            advice=[str(a) for a in data.get("advice") or [] if str(a).strip()],
            confidence=float(data.get("confidence") or 0.0),
            raw_text=str(data.get("raw_text") or ""),
            structured=bool(data.get("structured", True)),
        )

    def to_plain_text(self, title: str) -> str:
        """纯文本兜底输出。"""
        if not self.structured and self.raw_text:
            return f"{title}\n\n{self.raw_text.strip()}"
        lines: list[str] = [title]
        if self.headline:
            lines.append("")
            lines.append(self.headline)
        if self.tags:
            lines.append("")
            lines.append("标签：" + " / ".join(t.label for t in self.tags))
        if self.dimensions:
            lines.append("")
            for dim in self.dimensions:
                bar_len = max(0, min(10, round(dim.score / 10)))
                bar = "█" * bar_len + "░" * (10 - bar_len)
                lines.append(f"{dim.name:<6} {bar} {dim.score}")
        for section in self.sections:
            lines.append("")
            lines.append(f"【{section.title}】")
            lines.append(section.body.strip())
        if self.evidence:
            lines.append("")
            lines.append("【原话依据】")
            for item in self.evidence:
                lines.append(f"· 「{item.quote}」 {item.reason}".rstrip())
        if self.advice:
            lines.append("")
            lines.append("【建议】")
            for item in self.advice:
                lines.append(f"· {item}")
        lines.append("")
        lines.append(f"置信度 {self.confidence:.0%}")
        return "\n".join(lines)


@dataclass(slots=True)
class PortraitRecord:
    """落库后的一条画像记录。"""

    id: int = 0
    platform: str = ""
    umo: str = ""
    group_id: str = ""
    group_name: str = ""
    user_id: str = ""
    user_name: str = ""
    kind: str = "portrait"
    kind_label: str = ""
    theme: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    sample_size: int = 0
    corpus_chars: int = 0
    confidence: float = 0.0
    model: str = ""
    card_file: str = ""
    created_at: int = 0

    @classmethod
    def from_row(cls, row: Any) -> PortraitRecord:
        payload: dict[str, Any] = {}
        raw = row["payload_json"]
        if raw:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = {}
        return cls(
            id=int(row["id"]),
            platform=row["platform"] or "",
            umo=row["umo"] or "",
            group_id=row["group_id"] or "",
            group_name=row["group_name"] or "",
            user_id=row["user_id"] or "",
            user_name=row["user_name"] or "",
            kind=row["kind"] or "portrait",
            kind_label=row["kind_label"] or "",
            theme=row["theme"] or "",
            payload=payload,
            text=row["text"] or "",
            sample_size=int(row["sample_size"] or 0),
            corpus_chars=int(row["corpus_chars"] or 0),
            confidence=float(row["confidence"] or 0.0),
            model=row["model"] or "",
            card_file=row["card_file"] or "",
            created_at=int(row["created_at"] or 0),
        )

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "group_id": self.group_id,
            "group_name": self.group_name or (f"群 {self.group_id}" if self.group_id else "私聊"),
            "user_id": self.user_id,
            "user_name": self.user_name or self.user_id,
            "kind": self.kind,
            "kind_label": self.kind_label or self.kind,
            "theme": self.theme,
            "headline": str(self.payload.get("headline") or "")[:160],
            "tags": [
                str(t.get("label") or "") for t in self.payload.get("tags") or [] if isinstance(t, dict)
            ][:6],
            "sample_size": self.sample_size,
            "corpus_chars": self.corpus_chars,
            "confidence": round(self.confidence, 3),
            "model": self.model,
            "has_card": bool(self.card_file),
            "created_at": self.created_at,
        }

    def to_detail(self) -> dict[str, Any]:
        detail = self.to_summary()
        detail["payload"] = self.payload
        detail["text"] = self.text
        detail["umo"] = self.umo
        return detail
