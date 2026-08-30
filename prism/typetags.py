"""16 型人格标签：把模型给的四字母代码翻成一枚看得懂的徽章。

为什么值得做：四个字母（INTP）对没玩过的人是天书，配上一个中文别名（逻辑学家）
才能一眼认出这是「哪一类人」。三套命名对应三种口味：

- ``mbti``：通用的官方译名，最好懂；
- ``sbti``：网络上的玩梗版，适合整活群；
- ``acgti``：二次元角色版，适合动漫群。

代码本身在三套之间是通用的 —— 模型只判断一次，换档只换名字，不用重新分析。

思路参考了上游插件 astrbot_plugin_qq_group_daily_analysis 的「人格标签」玩法。
"""

from __future__ import annotations

#: 允许的档位。off = 不显示这枚徽章。
VALID_MODES = ("off", "mbti", "sbti", "acgti")

#: 四个维度的合法取值。顺序固定：能量朝向 / 认知方式 / 决策依据 / 生活节奏。
_AXES = ("EI", "NS", "FT", "JP")

MBTI_NAMES: dict[str, str] = {
    "INTJ": "建筑师",
    "INTP": "逻辑学家",
    "ENTJ": "指挥官",
    "ENTP": "辩论家",
    "INFJ": "提倡者",
    "INFP": "调停者",
    "ENFJ": "主人公",
    "ENFP": "竞选者",
    "ISTJ": "物流师",
    "ISFJ": "守卫者",
    "ESTJ": "总经理",
    "ESFJ": "执政官",
    "ISTP": "鉴赏家",
    "ISFP": "探险家",
    "ESTP": "企业家",
    "ESFP": "表演者",
}

SBTI_NAMES: dict[str, str] = {
    "INTJ": "拿捏者",
    "INTP": "思考者",
    "ENTJ": "领导者",
    "ENTP": "小丑",
    "INFJ": "多情者",
    "INFP": "孤儿",
    "ENFJ": "感恩者",
    "ENFP": "行者",
    "ISTJ": "哦不人",
    "ISFJ": "妈妈",
    "ESTJ": "愤世者",
    "ESFJ": "送钱者",
    "ISTP": "贫困者",
    "ISFP": "吗喽",
    "ESTP": "握草人",
    "ESFP": "尤物",
}

ACGTI_NAMES: dict[str, str] = {
    "INTJ": "Mortis",
    "INTP": "江户川柯南",
    "ENTJ": "丰川祥子",
    "ENTP": "藤原千花",
    "INFJ": "三角初华",
    "INFP": "后藤一里",
    "ENFJ": "月见八千代",
    "ENFP": "初音未来",
    "ISTJ": "若叶睦",
    "ISFJ": "长崎爽世",
    "ESTJ": "御坂美琴",
    "ESFJ": "千早爱音",
    "ISTP": "绫波丽",
    "ISFP": "洛天依",
    "ESTP": "明日香",
    "ESFP": "芙宁娜",
}

_TABLES: dict[str, dict[str, str]] = {
    "mbti": MBTI_NAMES,
    "sbti": SBTI_NAMES,
    "acgti": ACGTI_NAMES,
}


def normalize_mode(value: object) -> str:
    """把配置值收敛成合法档位。认不出来一律当 mbti。"""
    text = str(value or "").strip().lower()
    if text in {"", "off", "none", "false", "no", "关闭", "不显示"}:
        return "off" if text else "mbti"
    return text if text in VALID_MODES else "mbti"


def normalize_code(value: object) -> str:
    """洗干净模型给的代码。不是那 16 个组合之一就返回空串。"""
    text = "".join(ch for ch in str(value or "").upper() if ch.isalpha())
    if len(text) != 4:
        return ""
    for letter, axis in zip(text, _AXES, strict=True):
        if letter not in axis:
            return ""
    return text


def label_of(code: str, mode: str = "mbti") -> str:
    """徽章上的文字，例如 "INTP · 逻辑学家"。拿不到就返回空串。"""
    clean = normalize_code(code)
    if not clean:
        return ""
    table = _TABLES.get(normalize_mode(mode))
    if table is None:
        return ""
    alias = table.get(clean, "")
    return f"{clean} · {alias}" if alias else clean


def alias_of(code: str, mode: str = "mbti") -> str:
    """只要中文别名那一半，卡片上分两行排的时候用得上。"""
    clean = normalize_code(code)
    table = _TABLES.get(normalize_mode(mode))
    if not clean or table is None:
        return ""
    return table.get(clean, "")
