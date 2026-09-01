"""缘分榜：把本地互动统计排成「最合得来的人」。

为什么要有这个模块：棱镜姻缘如果只把语料丢给模型，模型写出来的东西和人格画像
几乎一样 —— 它会去总结"这个人怎么样"，而不是回答"这个人和谁最搭"。所以配对
结果先在本地算出来（谁接过 TA 的话、TA 找过谁、两人有没有同场待着），再把这份
名单当成既成事实交给模型；卡片上那个搭子名字与匹配度是数出来的，不是编的。

打分口径（全部本地计算，可复现）：

* 双向往来最值钱：TA 找对方、对方也接住，才算真的聊起来了。
* 单向搭话记一半：热情是热情，但没形成来回，分数压在 74 以下。
* 只是同场出现的权重很低，只用来兜底 —— 很多协议端不上报引用关系，
  没有这条整张榜会空掉。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .dialogue import SocialLink
from .models import Pairing

#: 榜上最多显示几个人。第一名做主角，后面的当备选。
DEFAULT_LIMIT = 4

#: 单向往来的分数上限。没有来回就不该看起来像"天生一对"。
ONE_WAY_CAP = 74

#: 只是同场出现过的分数上限。
NEARBY_CAP = 58

#: 榜首分数区间。证据越足越靠上限，最低也给个能看的分。
TOP_FLOOR = 62
TOP_CEIL = 96


def _strength(link: SocialLink) -> float:
    """这条关系的证据有多硬，0~1。来回次数越多越接近 1。"""
    pair = min(link.mine, link.theirs)
    volume = (link.mine + link.theirs) / 12.0
    return max(0.0, min(1.0, volume + pair / 6.0))


def _note_of(link: SocialLink) -> str:
    """一句面向玩家的理由。不写字段名、不写内部术语。"""
    if link.mutual:
        return f"一来一回搭过 {link.mine + link.theirs} 次话"
    if link.mine:
        return f"TA 主动找过 {link.mine} 次"
    if link.theirs:
        return f"对方主动来搭话 {link.theirs} 次"
    if link.nearby:
        return f"同场聊过 {link.nearby} 回"
    return ""


def _cap_of(link: SocialLink) -> int:
    if link.mutual:
        return TOP_CEIL
    if link.mine or link.theirs:
        return ONE_WAY_CAP
    return NEARBY_CAP


def rank_pairings(
    links: Sequence[SocialLink],
    *,
    limit: int = DEFAULT_LIMIT,
    exclude_ids: Iterable[str] = (),
) -> list[Pairing]:
    """把往来计数排成缘分榜。links 已按权重倒序时结果稳定可复现。"""
    blocked = {str(uid) for uid in exclude_ids if str(uid)}
    usable = [
        link
        for link in links
        if link.name and link.user_id not in blocked and (link.mine or link.theirs or link.nearby)
    ]
    if not usable:
        return []
    ordered = sorted(usable, key=lambda link: (-link.weight, link.name))
    top = ordered[0]
    top_weight = max(top.weight, 1e-6)
    top_score = TOP_FLOOR + (TOP_CEIL - TOP_FLOOR) * _strength(top)
    out: list[Pairing] = []
    for link in ordered[: max(1, limit)]:
        ratio = (link.weight / top_weight) ** 0.5
        score = round(min(top_score * ratio, float(_cap_of(link))))
        out.append(
            Pairing(
                name=link.name,
                user_id=link.user_id,
                score=max(12, min(99, score)),
                note=_note_of(link),
                mutual=link.mutual,
            ),
        )
    return out


def pairings_from_mentions(
    partners: Sequence[tuple[str, int]],
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[Pairing]:
    """兜底榜：连引用关系都没有时，退回"TA 在正文里 @ 过谁"。"""
    usable = [(name, count) for name, count in partners if name and count > 0]
    if not usable:
        return []
    top = max(count for _, count in usable)
    out: list[Pairing] = []
    for name, count in usable[: max(1, limit)]:
        ratio = (count / float(top)) ** 0.5
        score = round(min(ONE_WAY_CAP * ratio, float(ONE_WAY_CAP)))
        out.append(
            Pairing(
                name=name,
                score=max(12, score),
                note=f"TA 在群里念过 {count} 次这个名字",
                mutual=False,
            ),
        )
    return out


def facts_block(pairings: Sequence[Pairing]) -> str:
    """把缘分榜渲染成提示词里的既成事实。模型只能引用，不能改名字和分数。"""
    if not pairings:
        return ""
    lines = ["缘分榜（本地按互动次数算好，名字与匹配度不得改动、不得新增人名）："]
    for index, item in enumerate(pairings, start=1):
        flag = "双向往来" if item.mutual else "暂时单向"
        note = f"，{item.note}" if item.note else ""
        lines.append(f"{index}. {item.name} — 匹配度 {item.score}%（{flag}{note}）")
    lines.append(f"其中第 1 名「{pairings[0].name}」就是本次的最佳搭子，headline 必须点到这个名字。")
    return "\n".join(lines)


def top_name(pairings: Sequence[Pairing]) -> str:
    return pairings[0].name if pairings else ""
