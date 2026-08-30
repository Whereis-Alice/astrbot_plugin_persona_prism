"""让模型自己圈出「哪几句属于同一场对话」。

为什么需要这一步：群聊很少一问一答秒回。靠时间间隔（超过 N 秒就算换场）和条数
（前后各取 M 条）切窗口，在真实群里经常切歪 —— 有人隔了三分钟才回一句，有两
拨人同时在聊，也有人一口气刷五条弹幕。切歪的后果是模型看到一堆拼在一起的碎
片，把接话读成自言自语，卡片上的聊天现场也断断续续。

所以这里额外走一次很小的模型调用：把候选群聊编号列给模型看，只让它回一组编号
分组。原文一个字都不让模型写 —— 我们拿编号回本地记录取真实那一行，头像、时刻、
说话人全是真的。模型只做它擅长的那件事：判断这几句是不是在聊同一件事。

任何一步失败（模型没调通、编号对不上、场次里没有本人）都静默回落到 dialogue
模块的本地切法，不影响出卡。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import dialogue

#: 送进模型的候选行上限。再多就纯属烧 token —— 一场对话的判断只需要看得见前后文。
SHEET_LIMIT = 200

#: 候选行里，每条本人发言前后各铺几句。比正式渲染宽一点，给模型留判断余地。
SHEET_SPAN = 4

#: 单行正文在成绩单里的截断长度。长篇大论对「是不是同一场对话」的判断没有增益。
SHEET_TEXT_CAP = 90

#: 一场对话最少 / 最多几行。太短不成场，太长会把整个下午糊成一段。
SCENE_MIN_LINES = 2
SCENE_MAX_LINES = 12

#: 场次里必须出现本人的发言，否则这段与被分析者无关。


@dataclass(slots=True)
class ChainScene:
    """模型圈出的一场对话。indices 是 ordered 里的下标，已排序去重。"""

    indices: list[int] = field(default_factory=list)
    #: 模型给的理由。只写进日志，不上卡（普通用户不需要看模型的内心活动）。
    why: str = ""

    @property
    def start(self) -> int:
        return self.indices[0] if self.indices else -1

    def covers(self, index: int) -> bool:
        return index in self.indices


def candidate_indices(
    ordered: Sequence[dict[str, Any]],
    target_id: str,
    *,
    limit: int = SHEET_LIMIT,
    span: int = SHEET_SPAN,
) -> list[int]:
    """挑出要给模型过目的行。复用本地锚点分级，保证候选集围着本人转。"""
    if not ordered:
        return []
    tiers = dialogue.grade_anchors(ordered, target_id)
    if not tiers:
        return []
    picked = dialogue.select_lines(
        ordered,
        tiers,
        context=max(1, int(span)),
        max_lines=max(8, int(limit)),
        max_scenes=max(4, len(tiers)),
    )
    return picked


def build_sheet(
    ordered: Sequence[dict[str, Any]],
    indices: Sequence[int],
    target_id: str,
    *,
    names: dict[str, str] | None = None,
    self_id: str = "",
) -> tuple[str, dict[int, int]]:
    """渲染带编号的候选群聊，并返回「编号 → ordered 下标」的对照表。

    编号从 1 开始连续排，模型不用理解稀疏下标；正文照抄（只做长度截断），
    这样模型判断依据和我们回查的原文完全一致。
    """
    nick = dialogue.name_index(ordered, names)
    target = str(target_id or "")
    bot = str(self_id or "")
    lines: list[str] = []
    numbers: dict[int, int] = {}
    seq = 0
    for index in indices:
        if index < 0 or index >= len(ordered):
            continue
        row = ordered[index]
        body = dialogue.real_text(row.get("text") or "")
        if not body:
            continue
        if len(body) > SHEET_TEXT_CAP:
            body = body[:SHEET_TEXT_CAP] + "…"
        seq += 1
        numbers[seq] = index
        uid = str(row.get("user_id") or "")
        if uid and uid == target:
            label = dialogue.LABEL_TARGET
        elif bot and uid == bot:
            label = dialogue.LABEL_BOT
        else:
            label = dialogue.LABEL_OTHER
        who = nick.get(uid, "") or str(row.get("user_name") or "").strip() or "群友"
        ts = int(row.get("ts") or 0)
        clock = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "--:--"
        mark = ""
        reply_to = str(row.get("reply_to") or "")
        if reply_to:
            mark = "（回复了某条）"
        lines.append(f"#{seq} {clock} {label} {who}{mark}：{body}")
    return "\n".join(lines), numbers


CHAIN_SYSTEM = (
    "你在整理一段群聊记录。你的唯一任务是判断哪些行属于同一场对话（同一个话题、"
    "同一轮来回），并按要求只输出 JSON。你不需要点评任何人，也不要复述原文。"
)

_CHAIN_RULES = """请从下面带编号的群聊里，挑出最多 {limit} 场「{who} 参与过的完整对话」。

判断要点：
- 群聊不是一问一答秒回。隔了几分钟才回、中间插了别人的闲聊，只要话题接得上，就算同一场。
- 反过来，时间挨得很近但各说各的（两拨人同时聊不同事），要拆成不同场，或者干脆不选。
- 每一场都必须至少包含一条 {mark} 的发言，并且要能看出来 TA 在跟谁说话、说的是什么事。
- 一场 {low} 到 {high} 行；优先选来回清楚、信息量大的，宁缺毋滥。
- 只能使用下面出现过的编号，不要编号以外的任何内容，不要改写原文。

只输出这样一个 JSON 对象，不要解释，不要代码块：
{{"scenes": [{{"ids": [12, 13, 15], "why": "在聊周末去哪玩"}}]}}

群聊记录：
{sheet}"""


def build_prompt(
    sheet: str,
    *,
    target_name: str = "",
    max_scenes: int = 5,
) -> str:
    """拼出这次「挑对话」调用的用户提示。"""
    who = (target_name or "").strip() or "TA"
    return _CHAIN_RULES.format(
        limit=max(1, int(max_scenes)),
        who=who,
        mark=dialogue.LABEL_TARGET,
        low=SCENE_MIN_LINES,
        high=SCENE_MAX_LINES,
        sheet=sheet,
    )


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_NUM_RE = re.compile(r"\d{1,4}")


def _payload(text: str) -> Any:
    """从模型回复里抠出 JSON。带代码围栏、前后带客套话都能兜住。"""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    try:
        return json.loads(raw)
    except Exception:
        pass
    hit = _JSON_BLOCK.search(raw)
    if not hit:
        return None
    try:
        return json.loads(hit.group(0))
    except Exception:
        return None


def _ids_of(item: Any) -> list[int]:
    """把一场的 ids 读成整数列表。模型偶尔写成 "12,13" 或 "#12"，都收下。"""
    raw = item.get("ids") or item.get("lines") or item.get("id") if isinstance(item, dict) else item
    out: list[int] = []
    if isinstance(raw, (int, float)):
        return [int(raw)]
    if isinstance(raw, str):
        return [int(n) for n in _NUM_RE.findall(raw)]
    if isinstance(raw, Sequence):
        for value in raw:
            if isinstance(value, (int, float)):
                out.append(int(value))
            elif isinstance(value, str):
                out.extend(int(n) for n in _NUM_RE.findall(value))
    return out


def parse_chain(
    text: str,
    numbers: dict[int, int],
    ordered: Sequence[dict[str, Any]],
    target_id: str,
    *,
    max_scenes: int = 5,
) -> list[ChainScene]:
    """把模型回的编号分组翻译成 ordered 下标。不合格的场次直接丢掉。

    合格条件：编号都认识、行数够、并且这一场里真的有本人说话。
    """
    payload = _payload(text)
    if not isinstance(payload, dict):
        return []
    raw_scenes = payload.get("scenes")
    if not isinstance(raw_scenes, list):
        return []
    target = str(target_id or "")
    out: list[ChainScene] = []
    used: set[int] = set()
    for item in raw_scenes:
        picked = sorted({numbers[n] for n in _ids_of(item) if n in numbers})
        picked = [i for i in picked if i not in used]
        if len(picked) < SCENE_MIN_LINES:
            continue
        if len(picked) > SCENE_MAX_LINES:
            picked = picked[:SCENE_MAX_LINES]
        mine = any(str(ordered[i].get("user_id") or "") == target for i in picked if 0 <= i < len(ordered))
        if not mine:
            continue
        why = ""
        if isinstance(item, dict):
            why = str(item.get("why") or item.get("reason") or "").strip()
        used.update(picked)
        out.append(ChainScene(indices=picked, why=why[:40]))
        if len(out) >= max(1, int(max_scenes)):
            break
    out.sort(key=lambda s: s.start)
    return out


def render_block(
    ordered: Sequence[dict[str, Any]],
    chain: Sequence[ChainScene],
    target_id: str,
    *,
    names: dict[str, str] | None = None,
    self_id: str = "",
    max_lines: int = 80,
) -> str:
    """把模型挑中的场次渲染成「对话现场」。渲染逻辑与本地切法完全共用。"""
    picked: list[int] = []
    for scene in chain:
        picked.extend(scene.indices)
    picked = sorted(set(picked))
    if max_lines > 0 and len(picked) > max_lines:
        picked = picked[-max_lines:]
    if not picked:
        return ""
    lines = dialogue.render_lines(
        ordered,
        picked,
        target_id,
        names=names,
        self_id=self_id,
    )
    if not any(not dialogue.is_gap_line(line) for line in lines):
        return ""
    return "\n".join(lines)


def scene_rows(
    ordered: Sequence[dict[str, Any]],
    chain: Sequence[ChainScene],
    index: int,
) -> list[dict[str, Any]]:
    """给定某一行，返回它所在那一场对话的全部行。找不到就返回空列表。

    卡片上的聊天现场气泡就是靠这个对齐的：模型引用的那句原话落在哪一场，
    气泡就铺那一场，而不是再按时间硬切一次窗口。
    """
    for scene in chain:
        if scene.covers(index):
            return [ordered[i] for i in scene.indices if 0 <= i < len(ordered)]
    return []


__all__ = [
    "CHAIN_SYSTEM",
    "SCENE_MAX_LINES",
    "SCENE_MIN_LINES",
    "SHEET_LIMIT",
    "SHEET_SPAN",
    "ChainScene",
    "build_prompt",
    "build_sheet",
    "candidate_indices",
    "parse_chain",
    "render_block",
    "scene_rows",
]
