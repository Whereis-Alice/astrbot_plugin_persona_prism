"""专属头衔：给每张卡片配一枚称号徽章。

来由：上游 astrbot_plugin_love_formula 在名字下方挂一枚称号（截图里是「纯爱战神(反讽)」），
一眼就能记住结论，比一堆分数更有传播力。这里把它做成全系列通用的能力 ——
恋爱诊断按四维实分推导，棱镜 / 画像系列优先用模型给的 title，模型没给就按玩法
和种子确定性地兜一个。

设计上刻意只依赖标准库：本模块被 love / analyzer / cards 共同引用，
不能反向 import 它们，metrics 一律按鸭子类型取字段。
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Sequence
from typing import Any

#: 头衔正文的字数上限（不含括注）。再长就挤爆卡片的名字行。
MAX_LEN = 12
#: 括注（如「反讽」）的字数上限。
MAX_NOTE_LEN = 6

#: 模型偶尔会把头衔写成「头衔：xxx」「称号 - xxx」，先剥掉这层。
_PREFIX_RE = re.compile(r"^\s*(?:专属)?(?:头衔|称号|title|badge)\s*[:：\-—]\s*", re.IGNORECASE)
#: 各种成对引号 / 书名号 / 方括号。故意不含括号：括注要留到后面单独解析。
_QUOTE_CHARS = "\"'`“”‘’「」『』《》【】[]<>〈〉"
#: 正文清洗时用的完整包裹字符集（此时括注已经摘掉，剩下的括号就是垃圾）。
_WRAP_CHARS = _QUOTE_CHARS + "（）()"
_SPACE_RE = re.compile(r"\s+")
#: 括注统一成中文全角括号，顺手吸收半角写法。长度不限，超长的在下面整条丢掉。
_NOTE_RE = re.compile(r"[（(]\s*([^（()）]+?)\s*[)）]\s*$")
#: 句读出现在头衔里，说明模型写的是一句话而不是称号。
_SENTENCE_CHARS = "。！？，；\n"


def _balanced(text: str) -> str:
    """限长截断可能切断括号，从最后一个落单的左括号处砍掉，别留半个括注。"""
    for opener, closer in (("（", "）"), ("(", ")")):
        if text.count(opener) > text.count(closer):
            text = text[: text.rfind(opener)].strip()
    return text


def _pick(options: Sequence[str], seed: str) -> str:
    """按种子稳定地挑一条：同一人同一天同一玩法永远同一枚头衔，换天就换味。"""
    if not options:
        return ""
    return options[zlib.crc32(seed.encode("utf-8")) % len(options)]


def normalize_title(raw: Any) -> str:
    """把模型给的头衔收拾干净：剥前缀、去引号、压空白、限长，括注单独限长。

    返回空串表示这个值不能用（上层应当走兜底），而不是硬塞一个残缺的头衔。
    """
    text = _SPACE_RE.sub(" ", str(raw or "")).strip()
    if not text:
        return ""
    #: 「头衔：xxx」和「「头衔：xxx」」都见过，所以剥前缀和剥引号要交替做到稳定。
    #: 括号一直留着 —— 先剥会把「纯爱战神（反讽）」削成不闭合的残句。
    for _ in range(3):
        stripped = _PREFIX_RE.sub("", text).strip().strip(_QUOTE_CHARS).strip()
        if stripped == text:
            break
        text = stripped
    if not text:
        return ""
    note = ""
    found = _NOTE_RE.search(text)
    if found:
        raw_note = found.group(1).strip()
        text = text[: found.start()].strip()
        #: 括注超长说明那是一句解释而不是标注，整条丢掉、只留正文。
        note = raw_note if len(raw_note) <= MAX_NOTE_LEN else ""
    text = text.strip(_WRAP_CHARS).strip()
    #: 整句话不是头衔。带句读的一律判为「模型写跑偏了」，交给兜底。
    if not text or any(ch in text for ch in _SENTENCE_CHARS):
        return ""
    text = _balanced(text[:MAX_LEN])
    if not text:
        return ""
    return f"{text}（{note}）" if note else text


# ---------------------------------------------------------------------------
# 恋爱诊断：按四维实分推导
# ---------------------------------------------------------------------------

#: 每个人设备几枚同义头衔，按种子轮换。第一枚一般就是人设名，保证和判词口径一致。
LOVE_TITLES: dict[str, tuple[str, ...]] = {
    "the_ick": ("下头选手", "翻车惯犯", "热情肇事者", "撤回键代言人"),
    "himbo": ("笨蛋美人", "可爱豁免权", "翻车吉祥物", "逻辑掉线人气王"),
    "the_ex": ("白月光", "群聊初恋", "低频高浓度", "留白艺术家"),
    "the_simp": ("纯爱战神", "单向奔赴之王", "热情供给方", "自费氛围组"),
    "golden_retriever": ("黏人修勾", "双高选手", "行走的贴贴", "人气黏合剂"),
    "the_charmer": ("纯欲天花板", "气场收割机", "四两拨千斤", "群聊磁场"),
    "the_player": ("现充海王", "撒网工程师", "群聊社交家", "多线程恋爱脑"),
    "idol": ("顶级偶像", "神隐顶流", "一句封神", "限量发言者"),
    "npc": ("背景板 NPC", "在场证明", "潜水冠军", "静默观察员"),
    "normal": ("分寸感选手", "标准群友", "刚好的距离", "稳定输出型"),
}


def _love_note(simp: int, vibe: int, ick: int, nostalgia: int, total: int) -> str:
    """挑一条括注：分数自相矛盾时补一句「这枚头衔该怎么读」。命中即止，最多一条。

    上游那张截图（纯爱值 72 / 存在感 0）挂的正是「纯爱战神(反讽)」—— 就是第一条。
    """
    checks = (
        (simp >= 60 and vibe <= 15, "反讽"),
        (ick >= 70 and simp >= 50, "戴罪立功"),
        (vibe >= 75 and simp <= 20, "无本万利"),
        (nostalgia >= 80 and simp <= 25, "本尊认证"),
        (total >= 85, "官方认证"),
        (total <= 15, "有待观察"),
    )
    for hit, note in checks:
        if hit:
            return note
    return ""


def love_title(metrics: Any, *, seed: str = "") -> str:
    """恋爱诊断的专属头衔：人设定基调，四维分数决定括注。"""
    key = str(getattr(getattr(metrics, "archetype", None), "key", "") or "normal")
    pool = LOVE_TITLES.get(key) or LOVE_TITLES["normal"]
    base = _pick(pool, f"{seed}|love-title|{key}")
    note = _love_note(
        int(getattr(metrics, "simp", 0) or 0),
        int(getattr(metrics, "vibe", 0) or 0),
        int(getattr(metrics, "ick", 0) or 0),
        int(getattr(metrics, "nostalgia", 0) or 0),
        int(getattr(metrics, "total", 0) or 0),
    )
    return f"{base}（{note}）" if note else base


# ---------------------------------------------------------------------------
# 其余玩法：模型没给就按玩法兜底
# ---------------------------------------------------------------------------

#: 按玩法准备的称号池。棱镜系列偏「观测」，画像系列偏「报告」，克隆偏「分身」。
KIND_TITLES: dict[str, tuple[str, ...]] = {
    "portrait": ("人格标本", "本我显影者", "性格光谱持有者", "棱镜下的真身", "群聊人格样片"),
    "praise": ("群里的定心丸", "氛围补给站", "隐形功臣", "值得被夸的人", "好评常驻嘉宾"),
    "roast": ("话密现行犯", "抬杠爱好者", "复读机荣誉会员", "深夜嘴替", "槽点富矿"),
    "match": ("缘分连接器", "社交枢纽", "接话高手", "话题搭子候补", "群聊红线持有者"),
    "clone": ("语气克隆体", "可复制人格", "离线分身", "人格拓本"),
    "legacy_portrait": ("全息侧写对象", "优劣并陈者", "长文体检报告", "群聊观察样本"),
    "legacy_merit": ("优点持有者", "长处显眼包", "闪光点富户", "被低估的好人"),
    "legacy_flaw": ("待修补人格", "缺点自查表", "改进空间充足", "问题清单在手"),
    "legacy_clone": ("语气克隆体", "可复制人格", "离线分身", "人格拓本"),
    "legacy_match": ("缘分连接器", "社交枢纽", "接话高手", "话题搭子候补"),
}

#: 自定义提示词没有对应池子，用中性的观测系称号。
DEFAULT_TITLES: tuple[str, ...] = (
    "棱镜观测对象",
    "待解析人格",
    "群聊切片",
    "本期研究样本",
)


def fallback_title(
    kind: str,
    *,
    seed: str = "",
    tags: Sequence[Any] = (),
    dimensions: Sequence[Any] = (),
    headline: str = "",
) -> str:
    """模型没给 title 时兜一枚。

    优先级：某个维度爆表（>=85）→ 拿维度名做成「XX 满格」这类实证头衔；
    否则按玩法的称号池 + 种子挑一枚。tags / headline 只作为种子扰动，
    不直接当头衔用 —— 标签在卡片上另有展示位，重复出现会显得偷懒。
    """
    pool = KIND_TITLES.get(kind) or DEFAULT_TITLES
    salt = "|".join(
        [
            seed,
            "title",
            kind,
            headline[:16],
            ",".join(str(getattr(t, "label", t) or "")[:8] for t in list(tags)[:3]),
        ],
    )
    peak = _peak_dimension(dimensions)
    if peak:
        name, score = peak
        suffix = "满格" if score >= 95 else "拉满"
        candidate = normalize_title(f"{name}{suffix}")
        if candidate:
            return candidate
    return normalize_title(_pick(pool, salt)) or DEFAULT_TITLES[0]


def _peak_dimension(dimensions: Sequence[Any]) -> tuple[str, int] | None:
    """找出唯一一个「明显爆表」的维度：>=85 分且比第二名高 15 分以上。

    要求领先幅度，是为了避免六个维度都 90 分时随手拿第一个来当头衔 ——
    那种情况下「哪个维度爆表」本身就不成立，不如退回称号池。
    """
    scored: list[tuple[str, int]] = []
    for dim in dimensions or ():
        name = str(getattr(dim, "name", "") or "").strip()
        try:
            score = int(getattr(dim, "score", 0) or 0)
        except (TypeError, ValueError):
            continue
        if name and 0 <= score <= 100 and len(name) <= 6:
            scored.append((name, score))
    if not scored:
        return None
    scored.sort(key=lambda item: item[1], reverse=True)
    top = scored[0]
    if top[1] < 85:
        return None
    if len(scored) > 1 and top[1] - scored[1][1] < 15:
        return None
    return top
