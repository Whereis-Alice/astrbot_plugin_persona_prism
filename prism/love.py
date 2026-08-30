"""恋爱成分公式层。

这一层只做纯计算：把「一天的群语料 + 戳一戳/表情回应/撤回计数」折算成四维分数，
再归类到人设，最后拼成通用的 Portrait 结构，交给现有卡片渲染管线。
刻意不依赖 AstrBot 运行时，方便单测直接导入。

公式基线参考上游 astrbot_plugin_love_formula（见 NOTICE.md），并做了这些修正：
- 主动投入不再重复计入「平均字数」（原公式与长文指标撞车），改为计入回复/艾特等真实指向行为；
- 下头值从「撤回 + 自我复读」扩到跟读、连发刷屏、长文轰炸、纯表情、深夜刷屏；
- 昨日分只用来算趋势提示，不再按比例回灌当日分数（避免分数自我强化漂移）；
- 消息类指标全部从语料库现算，插件装上当天就有历史，不需要从零冷启动。
"""

from __future__ import annotations

import math
import re
import time
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import Dimension, Evidence, Portrait, Section, Tag, Term
from .scenes import rows_to_utterances

#: 归一化曲线的默认斜率。normalize(20)≈46、normalize(50)≈84。
DEFAULT_SLOPE = 0.05

#: 群里静默超过这么久，下一条发言算「开新话题」。
TOPIC_SILENCE_SECONDS = 900

#: 同一个人在这个间隔内的连续发言算「连发刷屏」。
BURST_SECONDS = 20

#: 超过这个字数算「长文轰炸」。
LONGPOST_CHARS = 120

#: 深夜时段（本地时区小时，左闭右开）。
NIGHT_HOURS = (0, 5)

#: 雷达图的五个轴，顺序即渲染顺序。
LOVE_DIMENSION_NAMES = ("纯爱值", "存在感", "白月光", "败犬值", "恋爱成分")


def normalize(value: float, *, slope: float = DEFAULT_SLOPE) -> int:
    """把无上限的原始分压到 0~100。"""
    if value <= 0:
        return 0
    squashed = 2.0 / (1.0 + math.exp(-slope * value)) - 1.0
    return max(0, min(100, int(100 * squashed)))


@dataclass(slots=True)
class LoveInputs:
    """一个人一天的原始行为计数。"""

    msg_sent: int = 0
    text_len_total: int = 0
    reply_sent: int = 0
    reply_received: int = 0
    at_sent: int = 0
    at_received: int = 0
    poke_sent: int = 0
    poke_received: int = 0
    reaction_sent: int = 0
    reaction_received: int = 0
    recall_count: int = 0
    repeat_count: int = 0
    echo_count: int = 0
    burst_count: int = 0
    longpost_count: int = 0
    emoji_only_count: int = 0
    night_count: int = 0
    image_sent: int = 0
    topic_count: int = 0
    partner_count: int = 0

    @property
    def avg_len(self) -> float:
        if self.msg_sent <= 0:
            return 0.0
        return self.text_len_total / self.msg_sent

    def merge(self, other: LoveInputs) -> LoveInputs:
        """把两份计数相加（互动对象数取较大值，不能简单相加）。"""
        merged = LoveInputs()
        for name in _COUNTER_FIELDS:
            setattr(merged, name, getattr(self, name) + getattr(other, name))
        merged.partner_count = max(self.partner_count, other.partner_count)
        return merged

    def to_dict(self) -> dict[str, int]:
        return {k: int(v) for k, v in asdict(self).items()}


_COUNTER_FIELDS: tuple[str, ...] = (
    "msg_sent", "text_len_total", "reply_sent", "reply_received", "at_sent", "at_received",
    "poke_sent", "poke_received", "reaction_sent", "reaction_received", "recall_count",
    "repeat_count", "echo_count", "burst_count", "longpost_count", "emoji_only_count",
    "night_count", "image_sent", "topic_count",
)


@dataclass(slots=True)
class LoveWeights:
    """四维公式的权重。默认值即公式基线，配置只调总体灵敏度。"""

    msg_sent: float = 1.0
    reply_sent: float = 1.5
    at_sent: float = 1.2
    poke_sent: float = 2.0
    reply_received: float = 3.0
    reaction_received: float = 2.0
    poke_received: float = 2.0
    at_received: float = 1.5
    recall: float = 5.0
    repeat: float = 3.0
    echo: float = 1.5
    burst: float = 1.2
    longpost: float = 2.0
    emoji_only: float = 0.5
    night: float = 0.8
    topic: float = 8.0
    image: float = 1.0
    partner: float = 2.0
    slope: float = DEFAULT_SLOPE


def _pick(options: Sequence[str], seed: str) -> str:
    """按种子在若干候选文案里稳定地挑一条：同一人同一天永远同一句，换天就换味。"""
    if not options:
        return ""
    index = zlib.crc32(seed.encode("utf-8")) % len(options)
    return options[index]


@dataclass(frozen=True, slots=True)
class LoveArchetype:
    """人设归类结果。"""

    key: str
    label: str
    tagline: str
    reason: str
    tags: tuple[str, ...] = ()
    advice: tuple[str, ...] = ()
    flavors: tuple[str, ...] = ()
    taglines: tuple[str, ...] = ()

    def pick_flavor(self, seed: str = "") -> str:
        """挑一版判词正文。没配 flavors 时退回 reason。"""
        return _pick(self.flavors, f"{seed}|flavor") or self.reason

    def pick_tagline(self, seed: str = "") -> str:
        """挑一版副标题。"""
        return _pick(self.taglines, f"{seed}|tagline") or self.tagline


#: 人设表。判词写得刻薄一点，但不做人格攻击——毕竟是群里玩的。
#: 每个人设都备了多套判词与副标题，按「人 + 日期」轮换，同一个人隔天再测就换一套说法。
ARCHETYPES: dict[str, LoveArchetype] = {
    "the_ick": LoveArchetype(
        "the_ick", "下头选手", "热情有余，火候不足",
        "投入是真的多，翻车也是真的密。撤回、复读、连珠炮一起上，热情还没到岸就先把气氛按下去了。",
        ("倒贴狂魔", "自曝专业户", "撤回大师"),
        ("说出口之前先读一遍，能省下一半撤回", "一次说完一件事，比连发六条更有分量"),
        flavors=(
            "投入是真的多，翻车也是真的密。撤回、复读、连珠炮一起上，热情还没到岸就先把气氛按下去了。",
            "热情拉满，准头全无。每一次出手都很用力，可惜落点常常是自己脚背。",
            "案卷显示：动机纯良，执行灾难。撤回键被你按出了包浆。",
        ),
        taglines=("热情有余，火候不足", "全力出击，全程翻车", "用力过猛的现场事故"),
    ),
    "himbo": LoveArchetype(
        "himbo", "笨蛋美人", "群里越乱，人气越高",
        "翻车翻得理直气壮，居然还很受欢迎。这份人气不是靠说话质量赚来的，是靠可爱度硬顶的。",
        ("翻车体质", "人气担当", "反差可爱"),
        ("你的优势是氛围，不是论述，别硬聊技术流", "撤回前先想想：其实没人在意，留着更有梗"),
        flavors=(
            "翻车翻得理直气壮，居然还很受欢迎。这份人气不是靠说话质量赚来的，是靠可爱度硬顶的。",
            "逻辑掉线，人气在线。群友不是在听你说什么，是在看你怎么表演。",
            "本庭认定：翻车属实，但情节可爱，从轻处理。",
        ),
        taglines=("群里越乱，人气越高", "翻车也算才艺", "笨得很有市场"),
    ),
    "the_ex": LoveArchetype(
        "the_ex", "白月光", "话不多，但每次都被记住",
        "很少主动黏人，可每次开口都能把话题掀起来。别人在刷屏，你在定调——这就是白月光的段位。",
        ("话题制造机", "低频高压", "神隐美学"),
        ("保持这个频率，稀缺才是你的杀招", "偶尔回应一下别人的召唤，会更有人味"),
        flavors=(
            "很少主动黏人，可每次开口都能把话题掀起来。别人在刷屏，你在定调——这就是白月光的段位。",
            "出场次数不多，回忆浓度极高。群里的话题走向经常是你随手拨的。",
            "证据表明：你不追人，人追你。留白被你用成了武器。",
        ),
        taglines=("话不多，但每次都被记住", "低频高浓度选手", "留白就是杀招"),
    ),
    "the_simp": LoveArchetype(
        "the_simp", "纯爱战神", "用力过猛，回声寥寥",
        "发言量遥遥领先，回应量惨不忍睹。你在演一场独角戏，观众都在别的频道。",
        ("单向输出", "热情过载", "自问自答"),
        ("把三句话压成一句，回应率通常会翻倍", "试着接别人的话头，而不是一直开新话头"),
        flavors=(
            "发言量遥遥领先，回应量惨不忍睹。你在演一场独角戏，观众都在别的频道。",
            "输出功率满分，接收信号为零。你的热情正在对着空气做功。",
            "本庭认定：付出充分，回报不足，属于典型的单方面奔赴。",
        ),
        taglines=("用力过猛，回声寥寥", "独角戏演到落幕", "热情单向流动"),
    ),
    "golden_retriever": LoveArchetype(
        "golden_retriever", "黏人修勾", "又黏又受欢迎，罕见的双高",
        "主动得毫无保留，偏偏群里也真的吃这一套。热情给出去有人接住，这是最健康的一种高投入。",
        ("热情满格", "有求必应", "群宠"),
        ("状态很好，注意别把自己耗空", "偶尔留白，别人才有机会主动找你"),
        flavors=(
            "主动得毫无保留，偏偏群里也真的吃这一套。热情给出去有人接住，这是最健康的一种高投入。",
            "尾巴摇得停不下来，而且每一次都有人伸手摸。双向奔赴的样本，罕见。",
            "投入与回报同步走高，这在本庭的卷宗里属于稀有品种。",
        ),
        taglines=("又黏又受欢迎，罕见的双高", "热情有去有回", "双向奔赴的活体样本"),
    ),
    "the_charmer": LoveArchetype(
        "the_charmer", "纯欲天花板", "力气不大，气场很大",
        "投入是中等的，回应是顶配的。别人费半天劲换不来的关注，你一句话就拿到了。",
        ("低耗高回", "气场型", "众人所向"),
        ("这套节奏别改，改了就泄气", "被关注得多，记得偶尔把话筒递出去"),
        flavors=(
            "投入是中等的，回应是顶配的。别人费半天劲换不来的关注，你一句话就拿到了。",
            "能量守恒在你这儿失效了：出力一分，回响十分。",
            "本庭注意到，你几乎不用抢麦，麦克风会自己滚到你面前。",
        ),
        taglines=("力气不大，气场很大", "低耗高回的作弊体质", "一句话换十句回应"),
    ),
    "the_player": LoveArchetype(
        "the_player", "现充海王", "撒网面积惊人",
        "自己出手不多，回应却四面八方涌来。你不是在聊天，你是在巡视鱼塘。",
        ("广撒网", "被动收割", "人脉型"),
        ("偶尔认真回一个人，效果比雨露均沾更好", "别只在被叫到时才出现"),
        flavors=(
            "自己出手不多，回应却四面八方涌来。你不是在聊天，你是在巡视鱼塘。",
            "战线拉得比发言量还长。每个人都觉得跟你挺熟，细想又想不起你说过什么。",
            "卷宗显示：交集广泛，纵深有限。雨露均沾，专注欠奉。",
        ),
        taglines=("撒网面积惊人", "巡塘时间到", "广度惊人，深度待查"),
    ),
    "idol": LoveArchetype(
        "idol", "顶级偶像", "几乎不发言，热度不减",
        "存在感全靠别人维持。你出场像巡演，剩下时间群友替你营业。",
        ("神隐", "自带热度", "云端选手"),
        ("哪天想下凡了，随便一句都是惊喜", "偶尔冒个泡，热度才不会凉"),
        flavors=(
            "存在感全靠别人维持。你出场像巡演，剩下时间群友替你营业。",
            "本人几乎不上线，热度却一直挂着。这是粉丝在替你打卡。",
            "出勤率极低，讨论度极高。偶像的经典配置。",
        ),
        taglines=("几乎不发言，热度不减", "缺席仍在营业", "热度由群友代管"),
    ),
    "npc": LoveArchetype(
        "npc", "背景板 NPC", "在场，但几乎不在线",
        "既没主动出击，也没被谁记挂。数据上你像个环境音，稳定，安静，可跳过。",
        ("静音模式", "隐身在场", "潜水员"),
        ("先从接一句话开始，门槛比你想的低", "潜水不是问题，只是没数据可以分析"),
        flavors=(
            "既没主动出击，也没被谁记挂。数据上你像个环境音，稳定，安静，可跳过。",
            "在场证明有，参与证明没有。你像一段循环播放的环境音轨。",
            "本庭翻遍卷宗，只找到你的坐标，没找到你的台词。",
        ),
        taglines=("在场，但几乎不在线", "在线率极高，参与率极低", "环境音级别的存在"),
    ),
    "normal": LoveArchetype(
        "normal", "一般群友", "分寸感刚好",
        "投入和回应都在中段，没有明显的翻车也没有明显的爆点。健康，但故事性一般。",
        ("均衡", "稳定输出", "无功无过"),
        ("想拉高存在感就多接话，接话比开话省力", "偶尔玩一次大的，数据会好看很多"),
        flavors=(
            "投入和回应都在中段，没有明显的翻车也没有明显的爆点。健康，但故事性一般。",
            "各项指标都很体面，体面得没有八卦价值。",
            "本庭裁定：无罪，也无戏。这是最难写判词的一类。",
        ),
        taglines=("分寸感刚好", "健康但无戏", "标准群友配置"),
    ),
}

def classify(simp: int, vibe: int, ick: int, nostalgia: int) -> LoveArchetype:
    """按顺序短路判定人设。顺序即优先级，越靠前越「特殊」。"""
    if ick > 60 and simp > 50:
        return ARCHETYPES["the_ick"]
    if ick > 60 and vibe > 50:
        return ARCHETYPES["himbo"]
    if nostalgia > 70 and simp < 40:
        return ARCHETYPES["the_ex"]
    if simp > 70 and vibe < 30:
        return ARCHETYPES["the_simp"]
    if simp > 65 and vibe > 60:
        return ARCHETYPES["golden_retriever"]
    if vibe > 80 and 30 <= simp <= 65:
        return ARCHETYPES["the_charmer"]
    if simp < 40 and vibe > 70:
        return ARCHETYPES["the_player"]
    if simp < 15 and vibe > 40:
        return ARCHETYPES["idol"]
    if simp < 20 and vibe < 20:
        return ARCHETYPES["npc"]
    return ARCHETYPES["normal"]


@dataclass(slots=True)
class LoveMetrics:
    """四维分数 + 归类结果 + 原始明细。"""

    simp: int = 0
    vibe: int = 0
    ick: int = 0
    nostalgia: int = 0
    total: int = 50
    raw: dict[str, float] = field(default_factory=dict)
    inputs: LoveInputs = field(default_factory=LoveInputs)
    archetype: LoveArchetype = ARCHETYPES["normal"]
    yesterday_total: int | None = None
    days: int = 1

    @property
    def trend(self) -> int | None:
        if self.yesterday_total is None:
            return None
        return self.total - self.yesterday_total

    @property
    def confidence(self) -> float:
        """样本越多越可信；戳一戳/表情回应这类互动信号再加一点。"""
        span = max(1, self.days)
        base = 0.2 + 0.7 * min(1.0, self.inputs.msg_sent / (40.0 * span))
        signals = (
            self.inputs.reply_received
            + self.inputs.reaction_received
            + self.inputs.poke_received
            + self.inputs.at_received
        )
        base += 0.1 * min(1.0, signals / 10.0)
        return round(min(0.95, base), 3)

    def scores(self) -> list[int]:
        return [self.simp, self.vibe, self.nostalgia, self.ick, self.total]

    def to_dict(self) -> dict[str, Any]:
        return {
            "simp": self.simp,
            "vibe": self.vibe,
            "ick": self.ick,
            "nostalgia": self.nostalgia,
            "total": self.total,
            "archetype": self.archetype.key,
            "archetype_label": self.archetype.label,
            "yesterday_total": self.yesterday_total,
            "days": self.days,
            "raw": dict(self.raw),
            "inputs": self.inputs.to_dict(),
        }


def compute_metrics(
    inputs: LoveInputs,
    *,
    weights: LoveWeights | None = None,
    yesterday_total: int | None = None,
    days: int = 1,
) -> LoveMetrics:
    """把原始计数折算成四维分 + 综合分。

    days > 1 时按「日均强度」口径折算：先把计数摊到每天再归一化，
    这样查 7 天不会因为基数变大而人人沸腾，跨天与单日的分数可以直接比较。
    互动对象数是集合大小而不是频次，不参与摊分。
    """
    w = weights or LoveWeights()
    span = max(1, int(days))
    raw_simp = (
        inputs.msg_sent * w.msg_sent
        + inputs.reply_sent * w.reply_sent
        + inputs.at_sent * w.at_sent
        + inputs.poke_sent * w.poke_sent
    )
    raw_vibe = (
        inputs.reply_received * w.reply_received
        + inputs.reaction_received * w.reaction_received
        + inputs.poke_received * w.poke_received
        + inputs.at_received * w.at_received
    )
    raw_ick = (
        inputs.recall_count * w.recall
        + inputs.repeat_count * w.repeat
        + inputs.echo_count * w.echo
        + inputs.burst_count * w.burst
        + inputs.longpost_count * w.longpost
        + inputs.emoji_only_count * w.emoji_only
        + inputs.night_count * w.night
    )
    raw_nostalgia = (
        inputs.topic_count * w.topic
        + inputs.image_sent * w.image
    )
    if span > 1:
        raw_simp /= span
        raw_vibe /= span
        raw_ick /= span
        raw_nostalgia /= span
    raw_nostalgia += inputs.partner_count * w.partner
    simp = normalize(raw_simp, slope=w.slope)
    vibe = normalize(raw_vibe, slope=w.slope)
    ick = normalize(raw_ick, slope=w.slope)
    nostalgia = normalize(raw_nostalgia, slope=w.slope)
    if not any((raw_simp, raw_vibe, raw_ick, raw_nostalgia)):
        total = 50
    else:
        total = int(max(0, min(100, ((vibe + nostalgia) - (ick + simp) + 200) / 4)))
    return LoveMetrics(
        simp=simp,
        vibe=vibe,
        ick=ick,
        nostalgia=nostalgia,
        total=total,
        raw={
            "simp": round(raw_simp, 2),
            "vibe": round(raw_vibe, 2),
            "ick": round(raw_ick, 2),
            "nostalgia": round(raw_nostalgia, 2),
        },
        inputs=inputs,
        archetype=classify(simp, vibe, ick, nostalgia),
        yesterday_total=yesterday_total,
        days=span,
    )


def weights_from_sensitivity(sensitivity: int) -> LoveWeights:
    """把配置里的 1~100 灵敏度换成曲线斜率。50 = 默认基线。"""
    level = max(1, min(100, int(sensitivity)))
    return LoveWeights(slope=round(DEFAULT_SLOPE * level / 50.0, 5))


_PLACEHOLDER_ONLY = re.compile(r"^(?:\[[^\[\]]{1,8}\]|\s)+$")


def _is_symbol_only(text: str) -> bool:
    """纯表情 / 纯占位符 / 纯标点。"""
    body = text.strip()
    if not body:
        return False
    if _PLACEHOLDER_ONLY.match(body):
        return True
    return all(not (ch.isalnum() or "\u4e00" <= ch <= "\u9fff") for ch in body)


def _split_ids(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def compute_day_inputs(
    rows: Sequence[dict[str, Any]],
    *,
    tz_offset: int = 8,
    topic_silence: int = TOPIC_SILENCE_SECONDS,
) -> dict[str, LoveInputs]:
    """从一段群语料里算出每个人的行为计数。

    只看语料库里已有的消息，所以插件装上就能出结果——这也是相对上游最实用的差别：
    上游把计数写在独立日表里，装上当天必须从零开始攒。
    """
    ordered = sorted(rows, key=lambda r: (int(r.get("ts") or 0), str(r.get("message_id") or "")))
    owner: dict[str, str] = {}
    for row in ordered:
        mid = str(row.get("message_id") or "")
        if mid:
            owner[mid] = str(row.get("user_id") or "")
    stats: dict[str, LoveInputs] = {}
    partners: dict[str, set[str]] = {}
    prev_text: dict[str, str] = {}
    last_user = ""
    last_text = ""
    last_ts = 0

    def slot(uid: str) -> LoveInputs:
        item = stats.get(uid)
        if item is None:
            item = LoveInputs()
            stats[uid] = item
            partners[uid] = set()
        return item

    for row in ordered:
        uid = str(row.get("user_id") or "")
        if not uid:
            continue
        text = str(row.get("text") or "")
        ts = int(row.get("ts") or 0)
        me = slot(uid)
        me.msg_sent += 1
        me.text_len_total += len(text)
        me.image_sent += max(0, int(row.get("images") or 0))
        if len(text) >= LONGPOST_CHARS:
            me.longpost_count += 1
        if _is_symbol_only(text):
            me.emoji_only_count += 1
        if ts:
            hour = (ts // 3600 + tz_offset) % 24
            if NIGHT_HOURS[0] <= hour < NIGHT_HOURS[1]:
                me.night_count += 1
        # 破冰：群里安静了很久之后的第一句
        if not last_ts or ts - last_ts >= topic_silence:
            me.topic_count += 1
        # 复读自己 / 跟读别人 / 连发刷屏
        if text and prev_text.get(uid) == text:
            me.repeat_count += 1
        if text and last_user and last_user != uid and last_text == text:
            me.echo_count += 1
        if last_user == uid and last_ts and 0 <= ts - last_ts <= BURST_SECONDS:
            me.burst_count += 1
        # 艾特
        at_ids = _split_ids(row.get("at_ids"))
        for target in at_ids:
            if target == uid:
                continue
            me.at_sent += 1
            slot(target).at_received += 1
            partners[uid].add(target)
            partners[target].add(uid)
        # 回复
        reply_to = str(row.get("reply_to") or "")
        target_uid = owner.get(reply_to, "") if reply_to else ""
        if target_uid and target_uid != uid:
            me.reply_sent += 1
            slot(target_uid).reply_received += 1
            partners[uid].add(target_uid)
            partners[target_uid].add(uid)
        elif row.get("is_reply") and not target_uid:
            # 引用的原消息不在语料里（比如翻页没覆盖到），只能记「他回了一句」；
            # 引用自己的消息不算主动投入，否则自问自答会虚高纯爱值。
            me.reply_sent += 1
        prev_text[uid] = text
        last_user, last_text, last_ts = uid, text, ts

    for uid, item in stats.items():
        item.partner_count = len(partners.get(uid) or ())
    return stats


def collect_names(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """取每个人在这段语料里最后出现过的昵称。"""
    names: dict[str, str] = {}
    for row in rows:
        uid = str(row.get("user_id") or "")
        name = str(row.get("user_name") or "").strip()
        if uid and name:
            names[uid] = name
    return names


#: 人设整体偏正 / 偏负，只用来给卡片标签上色。
_ARCHETYPE_POLARITY: dict[str, str] = {
    "the_ick": "negative",
    "himbo": "neutral",
    "the_ex": "positive",
    "the_simp": "negative",
    "golden_retriever": "positive",
    "the_charmer": "positive",
    "the_player": "neutral",
    "idol": "positive",
    "npc": "negative",
    "normal": "neutral",
}

#: 综合分档位。
_TOTAL_GRADES: tuple[tuple[int, str], ...] = (
    (20, "冰点"),
    (40, "微凉"),
    (60, "常温"),
    (80, "升温"),
    (100, "沸腾"),
)


def total_label(total: int) -> str:
    for ceiling, label in _TOTAL_GRADES:
        if total <= ceiling:
            return label
    return "沸腾"


def _join(pairs: Sequence[tuple[str, int]]) -> str:
    parts = [f"{name} {value}" for name, value in pairs if value]
    return " · ".join(parts) if parts else "无"


def breakdown_lines(metrics: LoveMetrics) -> list[str]:
    """把四维的原始构成写成人看得懂的明细。"""
    i = metrics.inputs
    return [
        "主动投入：" + _join((
            ("发言", i.msg_sent), ("回复他人", i.reply_sent),
            ("艾特他人", i.at_sent), ("戳一戳", i.poke_sent),
        )),
        "被动关注：" + _join((
            ("被回复", i.reply_received), ("被表情回应", i.reaction_received),
            ("被戳", i.poke_received), ("被艾特", i.at_received),
        )),
        "话题引力：" + _join((
            ("破冰开话题", i.topic_count), ("图片", i.image_sent),
            ("互动对象", i.partner_count),
        )),
        "下头扣分：" + _join((
            ("撤回", i.recall_count), ("自我复读", i.repeat_count),
            ("跟读", i.echo_count), ("连发刷屏", i.burst_count),
            ("长篇大论", i.longpost_count), ("纯表情", i.emoji_only_count),
            ("深夜出没", i.night_count),
        )),
        f"平均每条 {i.avg_len:.0f} 字",
    ]


def love_dimensions(metrics: LoveMetrics) -> list[Dimension]:
    """恋爱卡固定的五个维度，顺序与 LOVE_DIMENSION_NAMES 一致。"""
    return [
        Dimension("纯爱值", metrics.simp, "主动投入的密度"),
        Dimension("存在感", metrics.vibe, "被回应、被点名的密度"),
        Dimension("白月光", metrics.nostalgia, "开话题与被记住的程度"),
        Dimension("败犬值", metrics.ick, "翻车与下头指数，越低越好"),
        Dimension("恋爱成分", metrics.total, f"综合折算：{total_label(metrics.total)}"),
    ]


#: 四维术语速查。卡片底部会渲染成一张小面板，避免新人看不懂"白月光"是什么。
GLOSSARY: tuple[Term, ...] = (
    Term(
        "纯爱值", "S", "主动投入的密度",
        "发言、回复他人、艾特、戳一戳折算而来。数值越高，说明你越往外扑。",
    ),
    Term(
        "存在感", "V", "被回应的密度",
        "被回复、被表情回应、被戳、被艾特折算而来。衡量群里对你的反馈强度。",
    ),
    Term(
        "白月光", "N", "话题引力",
        "冷场后的破冰发言、发图、互动对象数折算而来。衡量你能不能把场子带起来。",
    ),
    Term(
        "败犬值", "I", "翻车下头指数",
        "撤回、自我复读、跟读、连发刷屏、长文轰炸、纯表情、深夜出没折算而来，越低越好。",
    ),
)


def span_label(days: int) -> str:
    """窗口口径的中文说法。"""
    return "当日" if days <= 1 else f"近 {days} 天"


def evolution_equation(metrics: LoveMetrics) -> str:
    """把综合分的算法写成一行公式，让分数可复核而不是玄学。"""
    span = span_label(metrics.days)
    if metrics.days > 1:
        span = f"{span}日均"
    if not any(metrics.raw.values()):
        return f"L({span}) = 样本为空 ⇒ 取基准 50%"
    return (
        f"L({span}) = [ (V {metrics.vibe} + N {metrics.nostalgia})"
        f" − (I {metrics.ick} + S {metrics.simp}) + 200 ] ÷ 4"
        f" ⇒ {metrics.total}% · {total_label(metrics.total)}"
    )


#: 行为诊断的说法库。每组按分数带命中，再按种子在同组里换着说。
_DIAG_BALANCE: dict[str, tuple[str, ...]] = {
    "one_way": (
        "你在往外倒热情，群里在看别的方向。投入产出比接近做慈善。",
        "输出远大于回声，这条链路目前是单向的。",
        "热情送出去了，回单还没打印出来。",
    ),
    "received": (
        "出手不多，回响不少。你的每一句话都在被人接。",
        "群里对你的响应远超你的投入，这叫被偏爱的有恃无恐。",
        "别人抢麦，你等麦克风自己过来。",
    ),
    "even": (
        "给出去的和收回来的基本对等，链路是通的。",
        "投入与回应大致持平，这是最省心的一种状态。",
        "收支平衡，情绪没有赤字。",
    ),
}

_DIAG_ICK: dict[str, tuple[str, ...]] = {
    "high": (
        "翻车指标偏高，撤回与复读把气氛按下去了好几次。",
        "下头动作出现得太频繁，热情还没落地就先绊了一跤。",
        "败犬值居高不下，主要贡献来自那些「发出去就后悔」的瞬间。",
    ),
    "mid": (
        "偶有翻车，但都在可接受范围，不影响整体观感。",
        "小失误若干，不至于减分到伤筋动骨。",
    ),
    "low": (
        "几乎没有下头动作，说话前明显过了脑子。",
        "翻车记录干净，这一项可以放心。",
    ),
}

_DIAG_TOPIC: dict[str, tuple[str, ...]] = {
    "high": (
        "冷场时开口的人是你，话题走向也常常由你拨动。",
        "破冰次数很可观，群里安静下来第一个出声的多半是你。",
    ),
    "low": (
        "很少主动开新话题，基本是接别人的话头。",
        "话题引力偏弱，你更习惯当接球方。",
    ),
}


def diagnosis_lines(metrics: LoveMetrics, *, seed: str = "") -> list[str]:
    """行为诊断：把四维两两对照着讲清楚，而不是干巴巴念分数。"""
    lines: list[str] = []
    gap = metrics.simp - metrics.vibe
    if gap >= 25:
        key = "one_way"
    elif gap <= -25:
        key = "received"
    else:
        key = "even"
    lines.append(
        f"【纯爱值 {metrics.simp} × 存在感 {metrics.vibe}】"
        + _pick(_DIAG_BALANCE[key], f"{seed}|balance|{key}"),
    )
    ick_key = "high" if metrics.ick >= 55 else ("mid" if metrics.ick >= 25 else "low")
    lines.append(
        f"【败犬值 {metrics.ick}】" + _pick(_DIAG_ICK[ick_key], f"{seed}|ick|{ick_key}"),
    )
    topic_key = "high" if metrics.nostalgia >= 50 else "low"
    lines.append(
        f"【白月光 {metrics.nostalgia}】"
        + _pick(_DIAG_TOPIC[topic_key], f"{seed}|topic|{topic_key}"),
    )
    partners = metrics.inputs.partner_count
    if partners:
        lines.append(
            f"【互动面 {partners} 人】"
            + (
                "交集够广，社交半径不小。"
                if partners >= 5
                else "交集集中在少数几个人身上，深度大于广度。"
            ),
        )
    night = metrics.inputs.night_count
    if night >= 3:
        lines.append(f"【深夜出没 {night} 次】凌晨的发言最容易变成明天的撤回，注意时段。")
    return lines

def trend_text(metrics: LoveMetrics) -> str:
    """与昨天的对比。只做提示，不参与计分。"""
    delta = metrics.trend
    if delta is None:
        return ""
    if delta > 4:
        return f"比昨天升温 {delta} 分"
    if delta < -4:
        return f"比昨天降温 {abs(delta)} 分"
    return "和昨天基本持平"


def headline_of(metrics: LoveMetrics, *, seed: str = "") -> str:
    return f"{metrics.archetype.label}：{metrics.archetype.pick_tagline(seed)}"


def _breakdown_section(metrics: LoveMetrics) -> Section:
    return Section("成分拆解", "\n".join(breakdown_lines(metrics)))


#: 现场证供的场景标签：按这条发言的特征给一个短标题。
_SCENE_LABELS: tuple[tuple[str, str], ...] = (
    ("night", "深夜时段"),
    ("topic", "冷场破冰"),
    ("burst", "连发现场"),
    ("reply", "接话现场"),
    ("long", "长篇陈述"),
    ("plain", "日常片段"),
)

_SCENE_REASONS: dict[str, str] = {
    "night": "凌晨时段的发言，情绪浓度通常比白天高一档。",
    "topic": "群里安静之后由 TA 重新点火，白月光的主要来源。",
    "burst": "短时间内连着好几条，纯爱值与败犬值同时在这里累积。",
    "reply": "明确指向某个人的一次接话，主动投入的直接证据。",
    "long": "一次性倒出很多字，长文轰炸的取样。",
    "plain": "没有特别标签的一句日常，用来看基础语气。",
}


def _scene_kind(row: dict[str, Any], *, prev_uid: str, prev_ts: int, tz_offset: int) -> str:
    text = str(row.get("text") or "")
    ts = int(row.get("ts") or 0)
    uid = str(row.get("user_id") or "")
    if ts:
        hour = (ts // 3600 + tz_offset) % 24
        if NIGHT_HOURS[0] <= hour < NIGHT_HOURS[1]:
            return "night"
    if prev_ts and ts - prev_ts >= TOPIC_SILENCE_SECONDS:
        return "topic"
    if prev_uid == uid and prev_ts and 0 <= ts - prev_ts <= BURST_SECONDS:
        return "burst"
    if row.get("reply_to") or row.get("is_reply") or _split_ids(row.get("at_ids")):
        return "reply"
    if len(text) >= LONGPOST_CHARS:
        return "long"
    return "plain"


def build_scenes(
    rows: Sequence[dict[str, Any]],
    user_id: str,
    *,
    names: dict[str, str] | None = None,
    limit: int = 3,
    context: int = 1,
    tz_offset: int = 8,
) -> list[Evidence]:
    """从语料里裁出几段真实对话，作为公式版的现场证供。

    做法是先给目标的每条发言打分（有指向、有长度、不是纯表情的优先），
    再把命中的那条连着前后各一句一起拿出来，渲染成仿聊天记录的气泡。
    LLM 不可用或没给 dialogue 时，卡片照样有"证据"可看。
    """
    ordered = sorted(rows, key=lambda r: (int(r.get("ts") or 0), str(r.get("message_id") or "")))
    if not ordered or not user_id:
        return []
    label_of = dict(_SCENE_LABELS)
    nick = dict(names or {})
    for row in ordered:
        uid = str(row.get("user_id") or "")
        name = str(row.get("user_name") or "").strip()
        if uid and name and uid not in nick:
            nick[uid] = name
    candidates: list[tuple[float, int, str]] = []
    prev_uid, prev_ts = "", 0
    for index, row in enumerate(ordered):
        uid = str(row.get("user_id") or "")
        kind = _scene_kind(row, prev_uid=prev_uid, prev_ts=prev_ts, tz_offset=tz_offset)
        prev_uid, prev_ts = uid, int(row.get("ts") or 0)
        if uid != user_id:
            continue
        text = str(row.get("text") or "").strip()
        if not text or _is_symbol_only(text):
            continue
        score = min(40.0, len(text)) / 40.0
        if kind in {"topic", "reply", "burst", "night"}:
            score += 1.2
        if index + 1 < len(ordered) and str(ordered[index + 1].get("user_id") or "") != user_id:
            score += 0.6
        candidates.append((score, index, kind))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[int, str]] = []
    for _score, index, kind in candidates:
        if any(abs(index - taken) <= context for taken, _ in chosen):
            continue
        chosen.append((index, kind))
        if len(chosen) >= max(1, limit):
            break
    chosen.sort()
    scenes: list[Evidence] = []
    for index, kind in chosen:
        lo = max(0, index - context)
        hi = min(len(ordered), index + context + 1)
        # 气泡拼装与「棱镜」系列共用一份实现，保证两边的证供长得一模一样。
        dialogue = rows_to_utterances(ordered[lo:hi], user_id, names=nick)
        if not dialogue:
            continue
        stamp = int(ordered[index].get("ts") or 0)
        clock = time.strftime("%H:%M", time.localtime(stamp)) if stamp else ""
        label = label_of.get(kind, "日常片段")
        title = f"{clock} · {label}" if clock else label
        scenes.append(
            Evidence(
                quote=str(ordered[index].get("text") or "").strip(),
                reason=_SCENE_REASONS.get(kind, ""),
                title=title,
                dialogue=dialogue,
            ),
        )
    return scenes


def _diagnosis_section(metrics: LoveMetrics, seed: str) -> Section:
    return Section("行为诊断", "\n".join(diagnosis_lines(metrics, seed=seed)))


def fallback_portrait(
    metrics: LoveMetrics,
    *,
    target_name: str = "",
    seed: str = "",
    scenes: Sequence[Evidence] | None = None,
    sample_note: str = "",
) -> Portrait:
    """LLM 不可用时的纯公式版画像。判词按种子轮换，证供从语料现裁。"""
    polarity = _ARCHETYPE_POLARITY.get(metrics.archetype.key, "neutral")
    who = target_name or "这位群友"
    judgment = f"{who}：{metrics.archetype.pick_flavor(seed)}"
    trend = trend_text(metrics)
    if trend:
        judgment = f"{judgment}（{trend}）"
    sections = [
        Section("判词", judgment),
        _diagnosis_section(metrics, seed),
        _breakdown_section(metrics),
    ]
    return Portrait(
        kind="love",
        headline=headline_of(metrics, seed=seed),
        tags=[Tag(label, polarity) for label in metrics.archetype.tags],
        dimensions=love_dimensions(metrics),
        sections=sections,
        evidence=list(scenes or ()),
        advice=list(metrics.archetype.advice),
        equation=evolution_equation(metrics),
        glossary=list(GLOSSARY),
        sample_note=sample_note,
        confidence=metrics.confidence,
        structured=True,
    )


def merge_portrait(
    metrics: LoveMetrics,
    llm: Portrait | None,
    *,
    target_name: str = "",
    seed: str = "",
    scenes: Sequence[Evidence] | None = None,
    sample_note: str = "",
) -> Portrait:
    """把 LLM 的判词并进公式结果。

    分数永远是我们算的（可复现、可解释），文字才交给模型；模型翻车就整段退回公式版。
    模型没给现场证供时，用本地裁出来的 scenes 兜底。
    """
    base = fallback_portrait(
        metrics,
        target_name=target_name,
        seed=seed,
        scenes=scenes,
        sample_note=sample_note,
    )
    if llm is None:
        return base
    if not llm.structured:
        raw = (llm.raw_text or "").strip()
        if raw:
            base.sections = [Section("判词", raw), *base.sections[1:]]
        return base
    polarity = _ARCHETYPE_POLARITY.get(metrics.archetype.key, "neutral")
    sections = [s for s in llm.sections if (s.title or s.body).strip()]
    titles = {s.title for s in sections}
    if "行为诊断" not in titles:
        sections.append(_diagnosis_section(metrics, seed))
    if "成分拆解" not in titles:
        sections.append(_breakdown_section(metrics))
    trend = trend_text(metrics)
    if trend:
        sections.append(Section("趋势", trend))
    evidence = [e for e in llm.evidence if e.dialogue or e.quote.strip()]
    if not evidence:
        evidence = list(base.evidence)
    return Portrait(
        kind="love",
        headline=llm.headline.strip() or base.headline,
        tags=llm.tags or [Tag(label, polarity) for label in metrics.archetype.tags],
        dimensions=love_dimensions(metrics),
        sections=sections or base.sections,
        evidence=evidence,
        advice=llm.advice or list(metrics.archetype.advice),
        equation=evolution_equation(metrics),
        glossary=list(GLOSSARY),
        sample_note=sample_note,
        confidence=metrics.confidence,
        raw_text=llm.raw_text,
        structured=True,
    )


def metrics_prompt_block(metrics: LoveMetrics, *, target_name: str = "") -> str:
    """喂给模型的事实块。分数已经算好，模型只负责把话说得好听/难听。"""
    who = target_name or "分析对象"
    window = span_label(metrics.days)
    lines = [
        f"{who} {window}互动指标（已按公式算好，请勿改动数字）：",
        f"- 纯爱值 S（主动投入）：{metrics.simp}",
        f"- 存在感 V（被回应）：{metrics.vibe}",
        f"- 白月光 N（话题引力）：{metrics.nostalgia}",
        f"- 败犬值 I（翻车下头）：{metrics.ick}",
        f"- 恋爱成分 L（综合）：{metrics.total}（{total_label(metrics.total)}）",
        f"- 演化算式：{evolution_equation(metrics)}",
        f"- 公式归类：{metrics.archetype.label} —— {metrics.archetype.tagline}",
        "行为明细：",
    ]
    lines.extend(f"- {line}" for line in breakdown_lines(metrics))
    trend = trend_text(metrics)
    if trend:
        lines.append(f"- 趋势：{trend}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# OneBot 通知归一
# ---------------------------------------------------------------------------

#: notice → 互动计数字段。actor 记「送出」，target 记「收到」。
NOTICE_FIELDS: dict[str, tuple[str, str]] = {
    "poke": ("poke_sent", "poke_received"),
    "reaction": ("reaction_sent", "reaction_received"),
}


def parse_notice(raw: Any) -> dict[str, Any] | None:
    """把 OneBot 的 notice 事件归一成 {kind, actor, target, message_id, count}。

    返回 None 表示这条通知与恋爱成分无关。target 为空串时表示需要调用方
    按 message_id 反查被操作消息的作者。
    """
    if not isinstance(raw, dict):
        return None
    if str(raw.get("post_type") or "") != "notice":
        return None
    notice = str(raw.get("notice_type") or "")
    sub = str(raw.get("sub_type") or "")
    actor = str(raw.get("user_id") or "")
    if notice == "notify" and sub == "poke":
        target = str(raw.get("target_id") or "")
        if not actor or not target or actor == target:
            return None
        return {"kind": "poke", "actor": actor, "target": target, "message_id": "", "count": 1}
    if notice in {"group_msg_emoji_like", "reaction", "group_reaction"}:
        #: 各家协议端字段名不统一：新版 NapCat/Lagrange 走 likes 数组，
        #: 老实现只给一个 code/emoji_id。likes 里可能一次带多个表情，要按真实个数计。
        count = 0
        likes = raw.get("likes")
        if isinstance(likes, list):
            for item in likes:
                if isinstance(item, dict):
                    count += max(1, int(item.get("count") or 1))
                else:
                    count += 1
        if not count:
            count = 1
        if sub in {"remove", "unset", "cancel"}:
            count = -count
        message_id = str(raw.get("message_id") or "")
        if not actor or not message_id:
            return None
        return {
            "kind": "reaction",
            "actor": actor,
            "target": "",
            "message_id": message_id,
            "count": count,
        }
    if notice == "group_recall":
        #: user_id 是被撤回消息的作者，operator_id 才是操作者（管理员撤别人的不算他下头）。
        operator = str(raw.get("operator_id") or "")
        if not actor or (operator and operator != actor):
            return None
        return {"kind": "recall", "actor": actor, "target": "", "message_id": "", "count": 1}
    return None
