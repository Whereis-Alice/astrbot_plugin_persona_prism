"""卡片渲染。

设计取舍说明：

* HTML 在 Python 里一次性拼成**完整文档**（不留 Jinja2 占位符），这样同一份
  HTML 既能丢给 AstrBot 官方 t2i 端点渲染，也能交给本地 Playwright 渲染，
  两条链路的产出完全一致，不会出现"网络版好看、本地版错版"的情况。
* AstrBot 的 LocalRenderStrategy.render_custom_template 是直接
  raise NotImplementedError 的，所以"本地渲染 HTML"必须自己实现，这里用
  Playwright（装了就能用，没装就自动跳过这一层）。
* 渲染分四层兜底，任何一层挂了都还有下一层，最差也会回落成纯文本。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import math
import re
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .models import Portrait

#: 卡片主题。label 会显示在 WebUI 和 "棱镜主题" 命令里。
THEMES: dict[str, dict[str, str]] = {
    "aurora": {"label": "极光玻璃", "desc": "深空渐变 + 毛玻璃分层，默认主题"},
    "ink": {"label": "水墨宣纸", "desc": "宣纸底色 + 朱印，中式排版"},
    "neon": {"label": "赛博霓虹", "desc": "暗夜霓虹描边 + 扫描线"},
    "paper": {"label": "杂志排版", "desc": "浅色刊物风，衬线大标题"},
    "dossier": {"label": "机密档案", "desc": "牛皮纸档案袋 + 打字机字体"},
    "sakura": {"label": "恋色樱粉", "desc": "Y2K 恋爱游戏风，粉紫渐变 + 圆角糖果色"},
}

DEFAULT_THEME = "aurora"

_JINJA_TOKEN_RE = re.compile(r"\{(?=[{%#])")

_POLARITY_CLASS = {
    "positive": "pos",
    "negative": "neg",
    "neutral": "neu",
}


def theme_label(name: str) -> str:
    """主题名 → 中文名。认识 auto（自动挡），不认识的原样返回。"""
    meta = THEME_CHOICES.get(name)
    return meta["label"] if meta else name


def normalize_theme(name: str) -> str:
    return name if name in THEMES else DEFAULT_THEME


AUTO_THEME = "auto"

#: 「自动挡」不是第六套配色，而是「临场按这张画像的性子挑一套」。
#: 它只出现在配置项和「棱镜主题」里，落库的 record.theme 永远是被挑中的那套真主题，
#: 这样 WebUI 重看旧卡、重新渲染的时候不会变脸。
AUTO_THEME_META: dict[str, str] = {
    "label": "自动挡",
    "desc": "看画像的性子临场挑一套，同一个群连着画也不容易撞",
}

#: 可配置 / 可切换的全部档位 = 自动挡 + 五套真主题。THEMES 仍然只装真主题。
THEME_CHOICES: dict[str, dict[str, str]] = {AUTO_THEME: AUTO_THEME_META, **THEMES}

#: 自动挡的选题词表。刻意写成「意象词」而不是人格量表术语：
#: 画像里出现的是形容词和网络口语，不是 OCEAN 五因素的标准表述。
THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sakura": (
        "恋爱", "纯爱", "撒娇", "黏人", "暗恋", "心动", "甜", "娇", "萌", "可爱",
        "告白", "追人", "倒贴", "下头", "海王", "白月光", "偶像", "人气", "亲密", "浪漫",
    ),
    "neon": (
        "夜猫", "熬夜", "深夜", "凌晨", "作息颠倒",
        "玩梗", "抽象", "整活", "阴阳", "嘴炮", "话痨", "跳脱", "亢奋", "躁",
        "中二", "二次元", "游戏", "网瘾", "电子", "赛博", "弹幕", "刷屏", "锐利", "毒舌",
    ),
    "ink": (
        "沉稳", "内敛", "克制", "安静", "淡然", "平和", "温润", "慢热", "含蓄",
        "文艺", "古风", "诗", "书", "茶", "禅", "佛系", "留白", "分寸", "儒雅", "沉默",
    ),
    "paper": (
        "理性", "逻辑", "条理", "严谨", "客观", "专业", "技术", "科普", "干货",
        "分析", "总结", "效率", "认真", "务实", "钻研", "求证", "结构化", "输出", "科研", "工程",
    ),
    "dossier": (
        "神秘", "高冷", "潜水", "观察", "谜", "难以捉摸", "距离感", "深藏", "反差",
        "腹黑", "谨慎", "戒备", "低调", "隐身", "旁观", "沉底", "疏离", "捉摸不定", "试探", "边界感",
    ),
    "aurora": (
        "温暖", "热情", "元气", "活泼", "可爱", "亲和", "治愈", "共情", "体贴", "捧场",
        "情绪", "细腻", "浪漫", "分享", "社交", "氛围", "热心", "撒娇", "情感", "柔软",
    ),
}

#: 维度名命中左边的词时，按分数高低给主题加权：分高偏 high、分低偏 low。
#: 这是启发式而不是心理学结论——够用就行，真正决定性的还是标签和文字。
_DIMENSION_AXES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("活跃", "话", "表达", "发言", "输出", "外向", "参与"), "neon", "ink"),
    (("理性", "逻辑", "条理", "严谨", "专业", "知识", "信息"), "paper", "neon"),
    (("情绪", "感性", "共情", "亲和", "温", "善", "热"), "aurora", "dossier"),
    (("攻击", "锋", "毒", "刺", "冲", "梗", "玩"), "neon", "paper"),
    (("稳", "耐心", "沉", "自控", "克制"), "ink", "neon"),
    (("神秘", "距离", "隐", "潜", "谜", "反差", "深"), "dossier", "aurora"),
)

#: 内容证据的相对权重。这一组只决定「五套主题谁的气质更贴」，
#: 绝对大小无所谓 —— 算完会归一化，最贴的那套拿满分 _W_CONTENT。
_W_STRONG_HIT = 2.0   # 标题 / 标签 / 维度名 / 小节标题里命中一个词
_W_WEAK_HIT = 1.0     # 正文 / 原话理由 / 建议里命中一个词
_W_DIM_HIGH = 1.5     # 某个维度打了高分
_W_DIM_LOW = 1.2      # 某个维度打了低分
_W_POLARITY = 1.2     # 标签整体偏负 / 偏正
_W_CONFIDENCE = 0.8   # 置信度过低（资料少 → 「档案未完成」的气质）
_DIM_HIGH_AT = 68
_DIM_LOW_AT = 32

#: 归一化之后的三档权重，决定「内容 / 运气 / 避重复」谁说话更响：
#: 气质明显的画像（比如满屏熬夜玩梗）拿满 3.0，第二名通常不到 1.0，
#: 所以内容说得清时它稳赢；两三套主题打得难分时，抖动和避重复才决定结果。
_W_CONTENT = 3.0
_W_JITTER = 1.3
#: 本群最近用过的主题依次降权。故意调到「能翻盘势均力敌的局，翻不了一边倒的局」。
_AVOID_PENALTY = (2.0, 0.9)
#: 内容分领先第二名超过这个差距，就算「气质明摆着」，避重复不再对它降权——
#: 宁可连着撞一次主题，也不要把一个满屏熬夜玩梗的人渲染成水墨留白。
_AVOID_SKIP_MARGIN = 1.2


def is_auto_theme(name: str) -> bool:
    return str(name or "").strip().lower() == AUTO_THEME


def normalize_theme_choice(name: str) -> str:
    """校验「配置层」的主题值：允许 auto，其它未知值回落默认主题。"""
    value = str(name or "").strip().lower()
    return value if value in THEME_CHOICES else DEFAULT_THEME


def describe_theme_choice(choice: str, resolved: str = "") -> str:
    """给人看的主题说明。自动挡会顺带报出这次实际挑中的那套。"""
    if not is_auto_theme(choice):
        return theme_label(normalize_theme(choice))
    if resolved and normalize_theme(resolved) in THEMES:
        return f"{AUTO_THEME_META['label']} · 本次 {theme_label(normalize_theme(resolved))}"
    return AUTO_THEME_META["label"]


#: 「棱镜主题」允许的额外别名（除了主题名和中文名之外）。
THEME_ALIASES: dict[str, str] = {
    "自动": AUTO_THEME,
    "自动挡": AUTO_THEME,
    "随机": AUTO_THEME,
}


def match_theme_choice(text: str) -> str:
    """把用户输入认成一个档位。认不出来返回空串。"""
    wanted = str(text or "").strip().lower()
    if not wanted:
        return ""
    if wanted in THEME_CHOICES:
        return wanted
    for name, meta in THEME_CHOICES.items():
        if wanted == meta["label"].lower():
            return name
    return THEME_ALIASES.get(wanted, "")

def _theme_signal_text(portrait: Portrait | None) -> tuple[str, str]:
    """把画像压成两段文本：strong（标题/标签/维度名）与 weak（正文/原话/建议）。"""
    if portrait is None:
        return "", ""
    strong = [portrait.headline]
    strong += [tag.label for tag in portrait.tags]
    strong += [dim.name for dim in portrait.dimensions]
    strong += [section.title for section in portrait.sections]
    weak = [dim.note for dim in portrait.dimensions]
    weak += [section.body for section in portrait.sections]
    weak += [item.reason for item in portrait.evidence]
    weak += list(portrait.advice)
    if not portrait.structured:
        weak.append(portrait.raw_text)
    return " ".join(filter(None, strong)), " ".join(filter(None, weak))


def _stable_jitter(seed: str, theme: str) -> float:
    """0 ≤ x < 1 的稳定抖动。

    用 blake2b 而不是内置 hash()：后者对 str 加了进程级随机盐，
    换一次进程同一张画像就会换主题，测试也没法写。
    """
    digest = hashlib.blake2b(f"{seed}|{theme}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def theme_affinity(portrait: Portrait | None) -> dict[str, float]:
    """只看内容的「气质贴合度」原始分，不含运气也不含避重复。"""
    scores = dict.fromkeys(THEMES, 0.0)
    strong, weak = _theme_signal_text(portrait)
    for name, words in THEME_KEYWORDS.items():
        if name not in scores:
            continue
        for word in words:
            # 每个词最多计一次：复读一个词不该把主题硬拽过去。
            if word in strong:
                scores[name] += _W_STRONG_HIT
            elif word in weak:
                scores[name] += _W_WEAK_HIT
    if portrait is None:
        return scores
    for dim in portrait.dimensions:
        for words, high, low in _DIMENSION_AXES:
            if not any(word in dim.name for word in words):
                continue
            if dim.score >= _DIM_HIGH_AT:
                scores[high] += _W_DIM_HIGH
            elif dim.score <= _DIM_LOW_AT:
                scores[low] += _W_DIM_LOW
    tags = portrait.tags
    if tags:
        neg = sum(1 for tag in tags if tag.polarity == "negative") / len(tags)
        pos = sum(1 for tag in tags if tag.polarity == "positive") / len(tags)
        if neg >= 0.4:
            scores["dossier"] += _W_POLARITY
            scores["neon"] += _W_POLARITY / 2
        elif pos >= 0.6:
            scores["aurora"] += _W_POLARITY * 0.75
            scores["paper"] += _W_POLARITY / 3
    if 0 < portrait.confidence < 0.45:
        # 语料少、结论虚 → 「档案未完成」的气质刚好对上。
        scores["dossier"] += _W_CONFIDENCE
    return scores


def theme_scores(
    portrait: Portrait | None,
    *,
    seed: str = "",
    avoid: Sequence[str] = (),
) -> dict[str, float]:
    """自动挡的最终打分。分最高的那套就是结论。

    三部分相加：

    1. **内容分**：把 theme_affinity 的原始分归一化，最贴的那套得满分 _W_CONTENT。
       归一化很关键——画像长短差别很大，长画像随手就能命中十几个词，
       不归一化的话「文字多」会盖过「运气」和「避重复」，档位就退化成固定主题了。
    2. **抖动**：由 seed 和主题名算出的稳定伪随机数，负责在气质难分时拍板。
    3. **避重复**：本群最近用过的主题降权，让连着画的人不容易撞同一套。
       唯一的例外是内容分一边倒的时候（见 _AVOID_SKIP_MARGIN），此时以内容为准。
    """
    affinity = theme_affinity(portrait)
    top = max(affinity.values(), default=0.0)
    content = {
        name: (raw / top * _W_CONTENT if top > 0 else 0.0) for name, raw in affinity.items()
    }
    scores = {
        name: value + _stable_jitter(seed, name) * _W_JITTER for name, value in content.items()
    }
    ranked = sorted(content.values(), reverse=True)
    runner_up = ranked[1] if len(ranked) > 1 else 0.0
    for index, used in enumerate(list(avoid)[: len(_AVOID_PENALTY)]):
        if used not in scores:
            continue
        if content[used] - runner_up >= _AVOID_SKIP_MARGIN:
            continue
        scores[used] -= _AVOID_PENALTY[index]
    return scores


def pick_theme(
    portrait: Portrait | None,
    *,
    seed: str = "",
    avoid: Sequence[str] = (),
) -> str:
    """按画像内容挑一套主题。

    确定性的：同一张画像 + 同一个 seed 永远挑出同一套（重渲染不会变脸）。
    但画像内容每次分析都不一样，加上 avoid 排掉本群最近用过的，
    实际效果就是「一群人轮着画，主题一直在换」。
    """
    scores = theme_scores(portrait, seed=seed, avoid=avoid)
    # 先按分数，再按 THEMES 的固定顺序，保证结果可复现。
    order = list(THEMES)
    return max(order, key=lambda name: (scores[name], -order.index(name)))


def resolve_theme(
    choice: str,
    portrait: Portrait | None = None,
    *,
    seed: str = "",
    avoid: Sequence[str] = (),
) -> str:
    """把「配置层的档位」翻译成「真正用来渲染的主题」。"""
    if is_auto_theme(choice):
        return pick_theme(portrait, seed=seed, avoid=avoid)
    return normalize_theme(choice)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def neutralize_jinja(markup: str) -> str:
    """把 HTML 里可能被 Jinja2 误读的 {{ / {% / {# 打断。

    官方 t2i 端点会先用 Jinja2 渲染我们发过去的 HTML。卡片里出现群友原话，
    万一有人发了 "{{ 7*7 }}" 这种东西，不处理就会被远端当模板执行。
    """
    return _JINJA_TOKEN_RE.sub("{<!-- -->", markup)


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CardContext:
    """渲染一张卡片需要的全部非画像信息。"""

    title: str = "人格画像"
    kind_label: str = "人格画像"
    target_name: str = ""
    target_id: str = ""
    group_name: str = ""
    avatar_url: str = ""
    theme: str = DEFAULT_THEME
    footer_note: str = "人格棱镜 · Persona Prism"
    model: str = ""
    sample_size: int = 0
    total_corpus: int = 0
    span_days: float = 0.0
    show_evidence: bool = True
    show_avatar: bool = True
    created_at: int = field(default_factory=lambda: int(time.time()))
    #: CSS 层面的整体放大倍数。1.0 表示不放大。
    #: 官方 t2i 端点不接受 viewport / device_scale_factor，只能靠 CSS zoom 提清晰度。
    zoom: float = 1.0
    #: 正文 / 标题字体族覆盖（留空表示沿用主题自带字体栈）。
    font_family: str = ""
    font_title_family: str = ""
    #: 自定义字体的 @font-face src。可以是 http(s) URL，也可以是 data URI。
    font_src: str = ""
    font_name: str = ""


@dataclass(slots=True)
class RenderResult:
    """渲染产物。image_path 为空表示只能发纯文本。"""

    backend: str
    image_path: str = ""
    card_file: str = ""
    text: str = ""


# ---------------------------------------------------------------------------
# 雷达图
# ---------------------------------------------------------------------------


#: 雷达图画布。轴标签画在图形外侧，所以画布必须比 2×半径 明显宽，
#: 否则四周的标签会被 SVG 视口裁掉（表现为「作息规律」只剩「息规律」）。
RADAR_BOX_W = 300
RADAR_BOX_H = 268
RADAR_CX = 150.0
RADAR_CY = 132.0
RADAR_R = 84.0
RADAR_LABEL_GAP = 15.0
#: 轴标签最多显示几个字。维度名由模型生成，长名字会顶穿画布，
#: 完整名字在右侧的评分条里照样能看到，这里截断不丢信息。
RADAR_LABEL_MAX = 6


def radar_geometry(
    scores: list[int],
    *,
    cx: float = RADAR_CX,
    cy: float = RADAR_CY,
    radius: float = RADAR_R,
    label_gap: float = RADAR_LABEL_GAP,
) -> dict[str, Any]:
    """算出雷达图需要的所有坐标。

    刻意放在 Python 里算：远端 t2i 只跑 Jinja2 + 截图，不保证 JS 环境可靠，
    卡片里一行脚本都不写才是最稳的。
    """
    count = len(scores)
    if count < 3:
        return {"polygon": "", "axes": [], "rings": [], "labels": []}
    step = 2 * math.pi / count
    polygon: list[str] = []
    axes: list[dict[str, float]] = []
    labels: list[dict[str, float]] = []
    for index, score in enumerate(scores):
        angle = -math.pi / 2 + index * step
        ratio = max(0.06, min(1.0, score / 100.0))
        px = cx + radius * ratio * math.cos(angle)
        py = cy + radius * ratio * math.sin(angle)
        polygon.append(f"{px:.1f},{py:.1f}")
        axes.append(
            {
                "x": cx + radius * math.cos(angle),
                "y": cy + radius * math.sin(angle),
            },
        )
        labels.append(
            {
                "x": cx + (radius + label_gap) * math.cos(angle),
                "y": cy + (radius + label_gap) * math.sin(angle),
            },
        )
    rings = []
    for level in (0.25, 0.5, 0.75, 1.0):
        points = []
        for index in range(count):
            angle = -math.pi / 2 + index * step
            points.append(
                f"{cx + radius * level * math.cos(angle):.1f},{cy + radius * level * math.sin(angle):.1f}",
            )
        rings.append(" ".join(points))
    return {
        "polygon": " ".join(polygon),
        "axes": axes,
        "rings": rings,
        "labels": labels,
    }


def _radar_svg(portrait: Portrait) -> str:
    dims = portrait.dimensions[:8]
    if len(dims) < 3:
        return ""
    geo = radar_geometry([d.score for d in dims])
    parts: list[str] = [
        f'<svg class="radar" viewBox="0 0 {RADAR_BOX_W} {RADAR_BOX_H}"'
        f' width="{RADAR_BOX_W}" height="{RADAR_BOX_H}">',
    ]
    for ring in geo["rings"]:
        parts.append(f'<polygon class="ring" points="{ring}"/>')
    for axis in geo["axes"]:
        parts.append(
            f'<line class="axis" x1="{RADAR_CX:.1f}" y1="{RADAR_CY:.1f}"'
            f' x2="{axis["x"]:.1f}" y2="{axis["y"]:.1f}"/>',
        )
    parts.append(f'<polygon class="shape" points="{geo["polygon"]}"/>')
    for point in geo["polygon"].split(" "):
        if not point:
            continue
        x, _, y = point.partition(",")
        parts.append(f'<circle class="dot" cx="{x}" cy="{y}" r="3"/>')
    for dim, label in zip(dims, geo["labels"], strict=False):
        # 左右两侧的标签向外对齐，正上/正下居中，这样文字始终朝画布内侧生长。
        anchor = "middle"
        if label["x"] > RADAR_CX + 12:
            anchor = "start"
        elif label["x"] < RADAR_CX - 12:
            anchor = "end"
        name = dim.name if len(dim.name) <= RADAR_LABEL_MAX else dim.name[: RADAR_LABEL_MAX - 1] + "…"
        parts.append(
            f'<text class="radar-label" x="{label["x"]:.1f}" y="{label["y"]:.1f}"'
            f' text-anchor="{anchor}">{_esc(name)}</text>',
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------

_BASE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { background: var(--page-bg); }
body {
  font-family: var(--font-body);
  color: var(--ink);
  -webkit-font-smoothing: antialiased;
}
.wrap { padding: 44px; width: 968px; }
.card {
  position: relative;
  width: 880px;
  border-radius: var(--radius);
  background: var(--card-bg);
  border: var(--card-border);
  box-shadow: var(--card-shadow);
  overflow: hidden;
}
.card::before {
  content: "";
  position: absolute;
  inset: 0;
  background: var(--card-veil);
  pointer-events: none;
}
.inner { position: relative; padding: 38px 42px 30px; }

.hero { display: flex; align-items: center; gap: 24px; }
.avatar-box {
  width: 104px; height: 104px; flex: 0 0 104px;
  border-radius: var(--avatar-radius);
  overflow: hidden;
  background: var(--avatar-bg);
  border: var(--avatar-border);
  display: flex; align-items: center; justify-content: center;
  font-size: 42px; font-weight: 700; color: var(--accent-ink);
}
.avatar-box img { width: 100%; height: 100%; object-fit: cover; display: block; }
.who { flex: 1; min-width: 0; }
.kicker {
  font-family: var(--font-title);
  font-size: 13px; letter-spacing: .28em; text-transform: uppercase;
  color: var(--accent);
}
.who h1 {
  font-family: var(--font-title);
  font-size: 34px; line-height: 1.2; margin-top: 6px;
  color: var(--ink-strong);
  word-break: break-all;
}
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chip {
  font-size: 12px; padding: 4px 11px; border-radius: 999px;
  background: var(--chip-bg); color: var(--ink-dim);
  border: var(--chip-border);
}

.headline {
  margin-top: 26px; padding: 20px 24px;
  border-radius: 18px;
  background: var(--quote-bg);
  border-left: 4px solid var(--accent);
  font-family: var(--font-title);
  font-size: 22px; line-height: 1.55; color: var(--ink-strong);
}

.tags { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }
.tag {
  font-size: 15px; font-weight: 600;
  padding: 7px 16px; border-radius: 12px;
  border: 1px solid transparent;
}
.tag.pos { background: var(--tag-pos-bg); color: var(--tag-pos-ink); border-color: var(--tag-pos-line); }
.tag.neu { background: var(--tag-neu-bg); color: var(--tag-neu-ink); border-color: var(--tag-neu-line); }
.tag.neg { background: var(--tag-neg-bg); color: var(--tag-neg-ink); border-color: var(--tag-neg-line); }

.panel { margin-top: 30px; }
.panel-title {
  font-family: var(--font-title);
  font-size: 15px; letter-spacing: .16em;
  color: var(--accent);
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 16px;
}
.panel-title::after {
  content: ""; flex: 1; height: 1px; background: var(--rule);
}

.metrics { display: flex; gap: 20px; align-items: center; }
.radar-box { flex: 0 0 300px; }
.radar .ring { fill: none; stroke: var(--rule); stroke-width: 1; }
.radar .axis { stroke: var(--rule); stroke-width: 1; }
.radar .shape {
  fill: var(--radar-fill); stroke: var(--accent); stroke-width: 2;
}
.radar .dot { fill: var(--accent); }
.radar-label { font-size: 11.5px; fill: var(--ink-dim); font-family: var(--font-body); }

.dims { flex: 1; display: flex; flex-direction: column; gap: 13px; }
.dim-head { display: flex; justify-content: space-between; align-items: baseline; }
.dim-name { font-size: 15px; font-weight: 600; color: var(--ink-strong); }
.dim-score { font-family: var(--font-title); font-size: 15px; color: var(--accent); }
.bar { height: 7px; border-radius: 999px; background: var(--bar-bg); margin-top: 6px; overflow: hidden; }
.bar span { display: block; height: 100%; border-radius: 999px; background: var(--bar-fill); }
.dim-note { font-size: 12px; color: var(--ink-mute); margin-top: 5px; line-height: 1.5; }

.secs { display: flex; flex-direction: column; gap: 18px; }
.sec {
  padding: 18px 20px; border-radius: 16px;
  background: var(--block-bg); border: var(--block-border);
}
.sec h3 { font-family: var(--font-title); font-size: 17px; color: var(--ink-strong); margin-bottom: 8px; }
.sec p { font-size: 14.5px; line-height: 1.78; color: var(--ink); white-space: pre-wrap; }

.quotes { display: flex; flex-direction: column; gap: 12px; }
.quote {
  padding: 14px 18px; border-radius: 14px;
  background: var(--quote-bg); border-left: 3px solid var(--accent-soft);
}
.quote .q { font-size: 15px; color: var(--ink-strong); line-height: 1.6; }
.quote .r { font-size: 12.5px; color: var(--ink-mute); margin-top: 6px; }

.advice { display: flex; flex-direction: column; gap: 9px; }
.advice li { list-style: none; font-size: 14.5px; line-height: 1.7; padding-left: 22px; position: relative; }
.advice li::before {
  content: "";
  position: absolute; left: 4px; top: 9px;
  width: 8px; height: 8px; border-radius: 3px;
  background: var(--accent); transform: rotate(45deg);
}

.raw { font-size: 15px; line-height: 1.85; white-space: pre-wrap; color: var(--ink); }

.foot {
  margin-top: 32px; padding-top: 18px;
  border-top: 1px dashed var(--rule);
  display: flex; align-items: center; justify-content: space-between; gap: 18px;
}
.conf { flex: 1; }
.conf-head { display: flex; justify-content: space-between; font-size: 12px; color: var(--ink-mute); margin-bottom: 6px; }
.conf-bar { height: 6px; border-radius: 999px; background: var(--bar-bg); overflow: hidden; }
.conf-bar span { display: block; height: 100%; background: var(--bar-fill); }
.sign { text-align: right; font-size: 11.5px; color: var(--ink-mute); line-height: 1.6; }
.sign strong { display: block; font-family: var(--font-title); font-size: 13px; color: var(--accent); letter-spacing: .1em; }
.badge {
  position: absolute; top: 26px; right: 30px;
  font-family: var(--font-title); font-size: 11px; letter-spacing: .2em;
  padding: 5px 12px; border-radius: 999px;
  background: var(--badge-bg); color: var(--badge-ink); border: var(--badge-border);
}
"""

_THEME_CSS: dict[str, str] = {
    "aurora": """
:root {
  --page-bg: radial-gradient(1200px 700px at 12% -10%, #2a3d7a 0%, #131a33 45%, #0a0d1a 100%);
  --radius: 30px;
  --card-bg: linear-gradient(150deg, rgba(48,62,116,.72) 0%, rgba(20,26,50,.86) 62%, rgba(15,19,38,.92) 100%);
  --card-border: 1px solid rgba(150,180,255,.22);
  --card-shadow: 0 40px 90px rgba(4,7,20,.62);
  --card-veil: radial-gradient(620px 320px at 82% 4%, rgba(120,220,255,.16), transparent 70%),
               radial-gradient(520px 300px at 6% 96%, rgba(180,120,255,.14), transparent 72%);
  --ink: #d3dcf5;
  --ink-strong: #f2f6ff;
  --ink-dim: #a9b6d9;
  --ink-mute: #8592b8;
  --accent: #7fe3ff;
  --accent-soft: rgba(127,227,255,.55);
  --accent-ink: #9fe9ff;
  --rule: rgba(150,180,255,.22);
  --chip-bg: rgba(120,150,220,.16);
  --chip-border: 1px solid rgba(150,180,255,.2);
  --quote-bg: rgba(120,160,240,.13);
  --block-bg: rgba(18,24,48,.55);
  --block-border: 1px solid rgba(150,180,255,.15);
  --bar-bg: rgba(140,160,210,.2);
  --bar-fill: linear-gradient(90deg, #7fe3ff, #b58bff);
  --radar-fill: rgba(127,227,255,.2);
  --avatar-radius: 50%;
  --avatar-bg: rgba(120,150,220,.2);
  --avatar-border: 2px solid rgba(160,220,255,.5);
  --badge-bg: rgba(127,227,255,.14);
  --badge-ink: #9fe9ff;
  --badge-border: 1px solid rgba(127,227,255,.35);
  --tag-pos-bg: rgba(90,230,180,.16); --tag-pos-ink: #86f0c4; --tag-pos-line: rgba(90,230,180,.35);
  --tag-neu-bg: rgba(140,170,235,.16); --tag-neu-ink: #b7c8f5; --tag-neu-line: rgba(140,170,235,.34);
  --tag-neg-bg: rgba(255,130,160,.15); --tag-neg-ink: #ff9fb6; --tag-neg-line: rgba(255,130,160,.34);
  --font-title: "Noto Serif SC", "Songti SC", "Source Han Serif SC", serif;
  --font-body: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
}
""",
    "ink": """
:root {
  --page-bg: #ebe4d6;
  --radius: 6px;
  --card-bg: #f7f2e6;
  --card-border: 1px solid #ddd0b4;
  --card-shadow: 0 26px 60px rgba(90,70,40,.24);
  --card-veil: radial-gradient(700px 420px at 88% 8%, rgba(120,110,90,.09), transparent 68%),
               radial-gradient(520px 380px at 4% 92%, rgba(120,110,90,.07), transparent 70%);
  --ink: #4a4237;
  --ink-strong: #2b2620;
  --ink-dim: #6d6355;
  --ink-mute: #8b8071;
  --accent: #9c3b32;
  --accent-soft: rgba(156,59,50,.4);
  --accent-ink: #9c3b32;
  --rule: rgba(120,105,80,.28);
  --chip-bg: rgba(120,105,80,.1);
  --chip-border: 1px solid rgba(120,105,80,.2);
  --quote-bg: rgba(200,180,140,.2);
  --block-bg: rgba(255,252,244,.7);
  --block-border: 1px solid rgba(120,105,80,.18);
  --bar-bg: rgba(120,105,80,.16);
  --bar-fill: linear-gradient(90deg, #9c3b32, #c07a45);
  --radar-fill: rgba(156,59,50,.16);
  --avatar-radius: 8px;
  --avatar-bg: rgba(120,105,80,.14);
  --avatar-border: 1px solid rgba(120,105,80,.35);
  --badge-bg: #9c3b32;
  --badge-ink: #f7f2e6;
  --badge-border: 1px solid #7d2d26;
  --tag-pos-bg: rgba(90,130,90,.16); --tag-pos-ink: #3f6b45; --tag-pos-line: rgba(90,130,90,.35);
  --tag-neu-bg: rgba(120,105,80,.14); --tag-neu-ink: #6d6355; --tag-neu-line: rgba(120,105,80,.3);
  --tag-neg-bg: rgba(156,59,50,.13); --tag-neg-ink: #9c3b32; --tag-neg-line: rgba(156,59,50,.3);
  --font-title: "Noto Serif SC", "Songti SC", "STSong", serif;
  --font-body: "Noto Serif SC", "Songti SC", "STSong", serif;
}
.card { background-image: repeating-linear-gradient(0deg, rgba(150,130,100,.05) 0 1px, transparent 1px 4px); }
.badge { border-radius: 4px; }
""",
    "neon": """
:root {
  --page-bg: #05060d;
  --radius: 18px;
  --card-bg: linear-gradient(160deg, #0c1024 0%, #0a0b18 55%, #100a1e 100%);
  --card-border: 1px solid rgba(0,255,214,.3);
  --card-shadow: 0 0 0 1px rgba(255,0,140,.18), 0 30px 80px rgba(0,0,0,.8);
  --card-veil: repeating-linear-gradient(0deg, rgba(255,255,255,.028) 0 1px, transparent 1px 3px),
               radial-gradient(600px 300px at 92% 0%, rgba(255,0,140,.18), transparent 70%);
  --ink: #b9c6d8;
  --ink-strong: #eafcff;
  --ink-dim: #7f93ad;
  --ink-mute: #66788f;
  --accent: #00ffd6;
  --accent-soft: rgba(0,255,214,.5);
  --accent-ink: #00ffd6;
  --rule: rgba(0,255,214,.22);
  --chip-bg: rgba(0,255,214,.08);
  --chip-border: 1px solid rgba(0,255,214,.25);
  --quote-bg: rgba(0,255,214,.07);
  --block-bg: rgba(255,255,255,.03);
  --block-border: 1px solid rgba(0,255,214,.16);
  --bar-bg: rgba(255,255,255,.08);
  --bar-fill: linear-gradient(90deg, #00ffd6, #ff2e9a);
  --radar-fill: rgba(0,255,214,.16);
  --avatar-radius: 14px;
  --avatar-bg: rgba(0,255,214,.1);
  --avatar-border: 1px solid rgba(0,255,214,.5);
  --badge-bg: rgba(255,46,154,.14);
  --badge-ink: #ff7ec0;
  --badge-border: 1px solid rgba(255,46,154,.45);
  --tag-pos-bg: rgba(0,255,214,.12); --tag-pos-ink: #6ffff0; --tag-pos-line: rgba(0,255,214,.4);
  --tag-neu-bg: rgba(120,160,255,.12); --tag-neu-ink: #9fc0ff; --tag-neu-line: rgba(120,160,255,.35);
  --tag-neg-bg: rgba(255,46,154,.14); --tag-neg-ink: #ff8bc4; --tag-neg-line: rgba(255,46,154,.4);
  --font-title: "Rajdhani", "Noto Sans SC", "PingFang SC", sans-serif;
  --font-body: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.who h1 { text-shadow: 0 0 18px rgba(0,255,214,.45); }
.kicker { text-shadow: 0 0 12px rgba(0,255,214,.6); }
""",
    "paper": """
:root {
  --page-bg: #eceae4;
  --radius: 4px;
  --card-bg: #ffffff;
  --card-border: 1px solid #e2ded4;
  --card-shadow: 0 24px 60px rgba(40,35,25,.14);
  --card-veil: none;
  --ink: #3b3a36;
  --ink-strong: #16150f;
  --ink-dim: #6a6862;
  --ink-mute: #8d8b84;
  --accent: #b4451f;
  --accent-soft: rgba(180,69,31,.4);
  --accent-ink: #b4451f;
  --rule: rgba(30,28,22,.16);
  --chip-bg: #f3f1eb;
  --chip-border: 1px solid #e2ded4;
  --quote-bg: #f7f5f0;
  --block-bg: #fbfaf7;
  --block-border: 1px solid #eeebe4;
  --bar-bg: #ecebe6;
  --bar-fill: linear-gradient(90deg, #b4451f, #d99a52);
  --radar-fill: rgba(180,69,31,.14);
  --avatar-radius: 2px;
  --avatar-bg: #f3f1eb;
  --avatar-border: 1px solid #ddd8ce;
  --badge-bg: #16150f;
  --badge-ink: #f7f5f0;
  --badge-border: 1px solid #16150f;
  --tag-pos-bg: #eef5ee; --tag-pos-ink: #386b3f; --tag-pos-line: #d3e5d4;
  --tag-neu-bg: #f1f2f5; --tag-neu-ink: #4d5566; --tag-neu-line: #dfe2e8;
  --tag-neg-bg: #fbeeea; --tag-neg-ink: #a83f1c; --tag-neg-line: #f0d6cd;
  --font-title: "Playfair Display", "Noto Serif SC", "Songti SC", serif;
  --font-body: "Noto Sans SC", "Helvetica Neue", "PingFang SC", sans-serif;
}
.who h1 { font-size: 40px; letter-spacing: -.5px; }
.headline { border-radius: 0; border-left-width: 3px; }
.badge { border-radius: 2px; }
""",
    "dossier": """
:root {
  --page-bg: #3a3428;
  --radius: 3px;
  --card-bg: #ded2b4;
  --card-border: 1px solid #b8a880;
  --card-shadow: 0 28px 70px rgba(0,0,0,.5);
  --card-veil: repeating-linear-gradient(135deg, rgba(120,100,60,.05) 0 6px, transparent 6px 12px);
  --ink: #3d3524;
  --ink-strong: #221d12;
  --ink-dim: #5d5340;
  --ink-mute: #7b6f57;
  --accent: #8a2b1e;
  --accent-soft: rgba(138,43,30,.45);
  --accent-ink: #8a2b1e;
  --rule: rgba(80,68,44,.35);
  --chip-bg: rgba(90,76,48,.12);
  --chip-border: 1px dashed rgba(80,68,44,.4);
  --quote-bg: rgba(255,250,232,.5);
  --block-bg: rgba(252,246,228,.62);
  --block-border: 1px solid rgba(80,68,44,.22);
  --bar-bg: rgba(80,68,44,.18);
  --bar-fill: linear-gradient(90deg, #8a2b1e, #b3763a);
  --radar-fill: rgba(138,43,30,.15);
  --avatar-radius: 2px;
  --avatar-bg: rgba(80,68,44,.16);
  --avatar-border: 2px solid rgba(80,68,44,.5);
  --badge-bg: transparent;
  --badge-ink: #8a2b1e;
  --badge-border: 2px solid #8a2b1e;
  --tag-pos-bg: rgba(70,105,60,.14); --tag-pos-ink: #3f6135; --tag-pos-line: rgba(70,105,60,.35);
  --tag-neu-bg: rgba(80,68,44,.12); --tag-neu-ink: #5d5340; --tag-neu-line: rgba(80,68,44,.3);
  --tag-neg-bg: rgba(138,43,30,.12); --tag-neg-ink: #8a2b1e; --tag-neg-line: rgba(138,43,30,.32);
  --font-title: "Courier New", "Noto Sans Mono CJK SC", monospace;
  --font-body: "Courier New", "Noto Sans SC", monospace;
}
.kicker { letter-spacing: .34em; }
.badge { transform: rotate(6deg); font-weight: 700; }
.sec h3::before { content: "// "; color: var(--accent); }
""",
    "sakura": """
:root {
  --page-bg: radial-gradient(1100px 640px at 18% -12%, #ffd9ec 0%, #ffc6e3 38%, #f4a9d6 100%);
  --radius: 34px;
  --card-bg: linear-gradient(155deg, rgba(255,255,255,.94) 0%, rgba(255,238,247,.96) 58%, rgba(255,226,242,.97) 100%);
  --card-border: 2px solid rgba(255,255,255,.9);
  --card-shadow: 0 34px 80px rgba(196,86,148,.34);
  --card-veil: radial-gradient(560px 300px at 88% 6%, rgba(255,182,222,.42), transparent 70%),
               radial-gradient(480px 280px at 4% 94%, rgba(198,182,255,.34), transparent 72%);
  --ink: #6c3f5c;
  --ink-strong: #46203b;
  --ink-dim: #8a5b78;
  --ink-mute: #a97e97;
  --accent: #ff5fa2;
  --accent-soft: rgba(255,95,162,.4);
  --accent-ink: #e0357f;
  --rule: rgba(255,140,190,.32);
  --chip-bg: rgba(255,160,205,.2);
  --chip-border: 1px solid rgba(255,140,190,.36);
  --quote-bg: rgba(255,225,240,.72);
  --block-bg: rgba(255,247,251,.82);
  --block-border: 1px solid rgba(255,160,205,.3);
  --bar-bg: rgba(255,170,208,.26);
  --bar-fill: linear-gradient(90deg, #ff7fb6, #b48bff);
  --radar-fill: rgba(255,95,162,.2);
  --avatar-radius: 50%;
  --avatar-bg: rgba(255,180,215,.28);
  --avatar-border: 3px solid rgba(255,255,255,.92);
  --badge-bg: rgba(255,95,162,.14);
  --badge-ink: #e0357f;
  --badge-border: 1px solid rgba(255,95,162,.4);
  --tag-pos-bg: rgba(120,215,180,.2); --tag-pos-ink: #2f8f6c; --tag-pos-line: rgba(120,215,180,.42);
  --tag-neu-bg: rgba(190,170,255,.22); --tag-neu-ink: #7a5bd0; --tag-neu-line: rgba(190,170,255,.44);
  --tag-neg-bg: rgba(255,120,160,.2); --tag-neg-ink: #d63a72; --tag-neg-line: rgba(255,120,160,.42);
  --font-title: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  --font-body: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.kicker { letter-spacing: .26em; }
.badge { font-weight: 700; }
.sec h3::before { content: "♡ "; color: var(--accent); }
.tag { font-weight: 600; }
""",
}


# ---------------------------------------------------------------------------
# HTML 组装
# ---------------------------------------------------------------------------

#: 主题 CSS 之后追加的补丁样式（头像回退、无雷达图布局等）。
_EXTRA_CSS = """
.avatar-box { position: relative; }
.avatar-box .ini { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }
.avatar-box img { position: absolute; inset: 0; }
.metrics.solo .dims { flex: 1 1 100%; }
.note { margin-top: 20px; font-size: 12.5px; line-height: 1.65; color: var(--ink-mute); }
.empty { font-size: 14px; color: var(--ink-mute); }
"""

#: Markdown 卡片专用样式（"画像"系列的自由排版输出走这套）。
_MD_CSS = """
:root { --font-mono: "JetBrains Mono","Cascadia Code","DejaVu Sans Mono",Consolas,Menlo,monospace; }
.md-body { margin-top: 26px; font-size: 15px; line-height: 1.85; color: var(--ink); }
.md-body > *:first-child { margin-top: 0; }
.md-body h1, .md-body h2, .md-body h3, .md-body h4, .md-body h5, .md-body h6 {
  font-family: var(--font-title);
  color: var(--ink-strong);
  line-height: 1.35;
}
.md-body h1 { font-size: 25px; margin: 26px 0 12px; }
.md-body h2 {
  font-size: 20px;
  margin: 26px 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--rule);
}
.md-body h3 { font-size: 17px; margin: 20px 0 8px; color: var(--accent-ink); }
.md-body h4, .md-body h5, .md-body h6 { font-size: 15px; margin: 16px 0 6px; }
.md-body p { margin: 10px 0; }
.md-body strong { color: var(--ink-strong); font-weight: 700; }
.md-body em { color: var(--accent-ink); font-style: italic; }
.md-body del { color: var(--ink-mute); }
.md-body a { color: var(--accent); text-decoration: none; border-bottom: 1px dashed var(--accent-soft); }
.md-body ul.md-list, .md-body ol.md-list { margin: 10px 0 10px 4px; padding-left: 22px; }
.md-body ul.md-list { list-style: none; padding-left: 4px; }
.md-body ul.md-list > li { position: relative; padding-left: 20px; margin: 7px 0; }
.md-body ul.md-list > li::before {
  content: "";
  position: absolute;
  left: 3px;
  top: .68em;
  width: 7px;
  height: 7px;
  border-radius: 2px;
  background: var(--accent);
  opacity: .85;
}
.md-body ol.md-list > li { margin: 7px 0; }
.md-body ol.md-list > li::marker { color: var(--accent); font-family: var(--font-title); }
.md-body blockquote {
  margin: 14px 0;
  padding: 12px 18px;
  border-left: 3px solid var(--accent-soft);
  border-radius: 0 12px 12px 0;
  background: var(--quote-bg);
  color: var(--ink-dim);
  font-size: 14px;
}
.md-body hr.md-hr { margin: 22px 0; border: none; border-top: 1px solid var(--rule); }
.md-body code {
  font-family: var(--font-mono);
  font-size: .92em;
  padding: 2px 6px;
  border-radius: 6px;
  background: var(--chip-bg);
  color: var(--accent-ink);
}
.md-body .md-code {
  position: relative;
  margin: 16px 0;
  border-radius: 14px;
  border: 1px solid var(--rule);
  background: var(--quote-bg);
  overflow: hidden;
}
.md-body .md-code-lang {
  font-family: var(--font-title);
  font-size: 11px;
  letter-spacing: .16em;
  text-transform: uppercase;
  padding: 7px 16px;
  color: var(--ink-mute);
  border-bottom: 1px solid var(--rule);
}
.md-body .md-code pre { padding: 14px 18px; overflow: hidden; }
.md-body .md-code code {
  display: block;
  padding: 0;
  background: none;
  color: var(--ink);
  font-size: 13px;
  line-height: 1.72;
  white-space: pre-wrap;
  word-break: break-word;
}
.md-body table.md-table {
  width: 100%;
  margin: 16px 0;
  border-collapse: collapse;
  font-size: 13.5px;
}
.md-body table.md-table th, .md-body table.md-table td {
  padding: 9px 12px;
  border: 1px solid var(--rule);
  text-align: left;
  vertical-align: top;
}
.md-body table.md-table th {
  font-family: var(--font-title);
  color: var(--ink-strong);
  background: var(--chip-bg);
}
.foot.foot-md { justify-content: flex-end; }
"""

_CONF_LEVELS = ((0.8, "高"), (0.6, "中"), (0.0, "低"))


def confidence_label(value: float) -> str:
    """把 0~1 的置信度翻成人话。"""
    for floor, label in _CONF_LEVELS:
        if value >= floor:
            return label
    return "低"


def _initial(name: str, user_id: str) -> str:
    for char in (name or "").strip():
        if char.strip():
            return _esc(char)
    return _esc((user_id or "?")[:1])


def _chip_texts(ctx: CardContext) -> list[str]:
    chips: list[str] = []
    if ctx.group_name:
        chips.append(f"群 · {ctx.group_name}")
    if ctx.target_id:
        chips.append(f"ID · {ctx.target_id}")
    if ctx.sample_size:
        if ctx.total_corpus and ctx.total_corpus > ctx.sample_size:
            chips.append(f"样本 · {ctx.sample_size}/{ctx.total_corpus} 条")
        else:
            chips.append(f"样本 · {ctx.sample_size} 条")
    if ctx.span_days >= 0.05:
        chips.append(f"跨度 · {ctx.span_days:.1f} 天")
    chips.append(time.strftime("%Y-%m-%d %H:%M", time.localtime(ctx.created_at)))
    return chips


def _hero_html(ctx: CardContext) -> str:
    parts: list[str] = ['<div class="hero">']
    if ctx.show_avatar:
        avatar = f'<span class="ini">{_initial(ctx.target_name, ctx.target_id)}</span>'
        if ctx.avatar_url:
            avatar += f'<img src="{_esc(ctx.avatar_url)}" alt="" onerror="this.style.display=\'none\'"/>'
        parts.append(f'<div class="avatar-box">{avatar}</div>')
    chips = "".join(f'<span class="chip">{_esc(text)}</span>' for text in _chip_texts(ctx))
    parts.append(
        '<div class="who">'
        f'<div class="kicker">{_esc(ctx.kind_label)}</div>'
        f"<h1>{_esc(ctx.target_name or ctx.target_id or '匿名群友')}</h1>"
        f'<div class="chips">{chips}</div>'
        "</div>",
    )
    parts.append("</div>")
    return "".join(parts)


def _tags_html(portrait: Portrait) -> str:
    if not portrait.tags:
        return ""
    items = "".join(
        f'<span class="tag {_POLARITY_CLASS.get(tag.polarity, "neu")}">{_esc(tag.label)}</span>'
        for tag in portrait.tags[:12]
    )
    return f'<div class="tags">{items}</div>'


def _metrics_html(portrait: Portrait) -> str:
    dims = portrait.dimensions[:8]
    if not dims:
        return ""
    radar = _radar_svg(portrait)
    rows: list[str] = []
    for dim in dims:
        score = max(0, min(100, int(dim.score)))
        note = f'<div class="dim-note">{_esc(dim.note)}</div>' if dim.note else ""
        rows.append(
            '<div class="dim">'
            '<div class="dim-head">'
            f'<span class="dim-name">{_esc(dim.name)}</span>'
            f'<span class="dim-score">{score}</span>'
            "</div>"
            f'<div class="bar"><span style="width:{score}%"></span></div>'
            f"{note}"
            "</div>",
        )
    radar_box = f'<div class="radar-box">{radar}</div>' if radar else ""
    solo = "" if radar else " solo"
    return (
        '<div class="panel">'
        '<div class="panel-title">维度评分</div>'
        f'<div class="metrics{solo}">{radar_box}'
        f'<div class="dims">{"".join(rows)}</div></div>'
        "</div>"
    )


def _sections_html(portrait: Portrait) -> str:
    if not portrait.sections:
        return ""
    blocks = "".join(
        f'<div class="sec"><h3>{_esc(sec.title)}</h3><p>{_esc(sec.body)}</p></div>'
        for sec in portrait.sections[:8]
        if (sec.title or sec.body)
    )
    if not blocks:
        return ""
    return f'<div class="panel"><div class="panel-title">画像正文</div><div class="secs">{blocks}</div></div>'


def _quotes_html(portrait: Portrait, ctx: CardContext) -> str:
    if not ctx.show_evidence or not portrait.evidence:
        return ""
    items: list[str] = []
    for item in portrait.evidence[:5]:
        if not item.quote:
            continue
        reason = f'<div class="r">{_esc(item.reason)}</div>' if item.reason else ""
        items.append(
            f'<div class="quote"><div class="q">“{_esc(item.quote)}”</div>{reason}</div>',
        )
    if not items:
        return ""
    return (
        '<div class="panel">'
        '<div class="panel-title">原话证据</div>'
        f'<div class="quotes">{"".join(items)}</div>'
        "</div>"
    )


def _advice_html(portrait: Portrait) -> str:
    items = [line for line in portrait.advice[:6] if line.strip()]
    if not items:
        return ""
    body = "".join(f"<li>{_esc(line)}</li>" for line in items)
    return f'<div class="panel"><div class="panel-title">相处建议</div><ul class="advice">{body}</ul></div>'


def _foot_html(portrait: Portrait, ctx: CardContext) -> str:
    conf = max(0.0, min(1.0, float(portrait.confidence or 0.0)))
    percent = round(conf * 100)
    sign_lines = [f"<strong>{_esc(ctx.footer_note)}</strong>"]
    if ctx.model:
        sign_lines.append(_esc(f"模型 {ctx.model}"))
    sign_lines.append("内容由 AI 生成，仅供娱乐")
    return (
        '<div class="foot">'
        '<div class="conf">'
        '<div class="conf-head">'
        f"<span>结论可信度 · {confidence_label(conf)}</span><span>{percent}%</span>"
        "</div>"
        f'<div class="conf-bar"><span style="width:{percent}%"></span></div>'
        "</div>"
        f'<div class="sign">{"<br/>".join(sign_lines)}</div>'
        "</div>"
    )


#: 自定义字体缺省时的兜底字体栈（覆盖 Windows / macOS / Linux 常见中文字体）。
FONT_FALLBACK = (
    '"Noto Sans SC","Source Han Sans SC","PingFang SC","Microsoft YaHei",'
    '"Hiragino Sans GB","WenQuanYi Micro Hei",sans-serif'
)

#: 字体 URL 里一律剔除的字符，避免拼进 url("...") 时闭合引号或注入额外声明。
_FONT_SRC_BAD_RE = re.compile(r"""[\s"'()<>\\{}]""")
#: 字体族名只保留字母数字、中文、空格、逗号、连字符、下划线和点。
_FONT_FAMILY_BAD_RE = re.compile(r"""[^\w\u4e00-\u9fff .,\-]""")


def sanitize_font_src(value: str) -> str:
    """清洗自定义字体地址。只接受 http(s) URL 与 data URI。"""
    cleaned = _FONT_SRC_BAD_RE.sub("", str(value or "").strip())
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if lowered.startswith(("http://", "https://", "data:")):
        return cleaned
    return ""


def sanitize_font_family(value: str) -> str:
    """清洗字体族名，顺手补齐兜底字体栈。"""
    cleaned = _FONT_FAMILY_BAD_RE.sub("", str(value or "").strip()).strip(" ,")
    if not cleaned:
        return ""
    names = [part.strip() for part in cleaned.split(",") if part.strip()]
    if not names:
        return ""
    quoted = [name if name.isascii() and " " not in name else f'"{name}"' for name in names]
    return ", ".join(quoted)


#: 允许内嵌的字体扩展名 → data URI 的 MIME。
FONT_MIME_BY_SUFFIX: dict[str, str] = {
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".ttc": "font/collection",
}

#: 单个内嵌字体的体积上限。超过就拒绝，避免把几十 MB 塞进 HTML 传给 t2i。
FONT_MAX_BYTES = 8 * 1024 * 1024

#: 本地字体 → data URI 的内存缓存，键含 mtime / size，换字体会自动失效。
_FONT_CACHE: dict[tuple[str, int, int], str] = {}


def resolve_font_source(value: str, *, logger: Any = None) -> str:
    """把 render.font_source 解析成可直接写进 @font-face 的 src。

    * http(s) URL / data URI —— 原样使用。
    * 本地字体文件 —— 读成 base64 data URI 内嵌。必须内嵌，因为官方 t2i 端点
      在远端渲染，读不到本机磁盘上的字体文件。
    """
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://", "data:")):
        return sanitize_font_src(raw)

    path = Path(raw).expanduser()
    suffix = path.suffix.lower()
    mime = FONT_MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        if logger is not None:
            logger.warning(
                f"[persona_prism] 不支持的字体格式 {suffix or '(无扩展名)'}，"
                f"仅支持 {'/'.join(FONT_MIME_BY_SUFFIX)}",
            )
        return ""
    try:
        stat = path.stat()
    except OSError as exc:
        if logger is not None:
            logger.warning(f"[persona_prism] 读不到字体文件 {path}：{exc}")
        return ""
    if stat.st_size > FONT_MAX_BYTES:
        if logger is not None:
            logger.warning(
                f"[persona_prism] 字体文件过大（{stat.st_size / 1048576:.1f} MB > "
                f"{FONT_MAX_BYTES // 1048576} MB），已忽略：{path}",
            )
        return ""
    cache_key = (str(path), int(stat.st_mtime), int(stat.st_size))
    cached = _FONT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        if logger is not None:
            logger.warning(f"[persona_prism] 字体文件读取失败 {path}：{exc}")
        return ""
    encoded = f"data:{mime};base64,{payload}"
    _FONT_CACHE.clear()
    _FONT_CACHE[cache_key] = encoded
    return encoded


def _font_css(ctx: CardContext) -> str:
    """生成放在所有主题样式之后的字体覆盖片段。

    主题 CSS 用 --font-body / --font-title 两个变量描述字体，这里最后写一次
    同名变量即可覆盖，不需要改动任何主题。
    """
    src = sanitize_font_src(ctx.font_src)
    name = sanitize_font_family(ctx.font_name) or '"PrismCustomFont"'
    body = sanitize_font_family(ctx.font_family)
    title = sanitize_font_family(ctx.font_title_family)
    parts: list[str] = []
    if src:
        parts.append(
            f"@font-face{{font-family:{name};src:url({src});font-display:block;"
            f"font-weight:100 900;font-style:normal;}}",
        )
        if not body:
            body = f"{name}, {FONT_FALLBACK}"
        if not title:
            title = body
    decls: list[str] = []
    if body:
        decls.append(f"--font-body:{body};")
    if title:
        decls.append(f"--font-title:{title};")
    if decls:
        parts.append("".join([":root{", *decls, "}"]))
    return "".join(parts)


def _zoom_css(ctx: CardContext) -> str:
    """整体放大。t2i 端点不给 device_scale_factor，只能用 CSS zoom 换清晰度。"""
    try:
        zoom = float(ctx.zoom or 1.0)
    except (TypeError, ValueError):
        return ""
    zoom = max(1.0, min(4.0, zoom))
    if abs(zoom - 1.0) < 1e-6:
        return ""
    value = f"{zoom:.4f}".rstrip("0").rstrip(".")
    return f"html{{zoom:{value};}}"


def _document(ctx: CardContext, css: str, inner: str, *, badge: str = "") -> str:
    """统一的外层文档骨架。"""
    badge_html = f'<div class="badge">{_esc(badge)}</div>' if badge else ""
    markup = (
        "<!DOCTYPE html>"
        '<html lang="zh-CN"><head><meta charset="utf-8"/>'
        f"<title>{_esc(ctx.title)}</title>"
        f"<style>{css}{_font_css(ctx)}{_zoom_css(ctx)}</style></head><body>"
        '<div class="wrap"><div class="card">'
        f"{badge_html}"
        '<div class="inner">'
        f"{inner}"
        "</div></div></div></body></html>"
    )
    return neutralize_jinja(markup)


def build_card_html(portrait: Portrait, ctx: CardContext) -> str:
    """把画像渲染成一份自包含的完整 HTML 文档。

    产物不含任何 Jinja2 占位符与 JavaScript，因此网络 t2i 与本地 Playwright
    渲染出来的画面完全一致。
    """
    theme = normalize_theme(ctx.theme)
    css = _THEME_CSS[theme] + _BASE_CSS + _EXTRA_CSS

    if portrait.structured:
        body_parts = [
            f'<div class="headline">{_esc(portrait.headline)}</div>' if portrait.headline else "",
            _tags_html(portrait),
            _metrics_html(portrait),
            _sections_html(portrait),
            _quotes_html(portrait, ctx),
            _advice_html(portrait),
        ]
        body = "".join(part for part in body_parts if part)
        if not body:
            body = '<div class="empty">这次没有解析到可展示的内容。</div>'
    else:
        text = portrait.raw_text or portrait.headline
        body = f'<div class="panel"><div class="raw">{_esc(text)}</div></div>'

    if ctx.sample_size and ctx.sample_size < 20:
        body += '<div class="note">样本偏少，结论仅作参考；多聊几天后重新生成会更准。</div>'

    inner = f"{_hero_html(ctx)}{body}{_foot_html(portrait, ctx)}"
    return _document(ctx, css, inner, badge=theme_label(theme))


# ---------------------------------------------------------------------------
# Markdown 卡片（兼容"画像"系列的自由排版输出）
# ---------------------------------------------------------------------------

_MD_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#-]*)\s*$")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_MD_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_MD_OL_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_MD_TABLE_SPLIT_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_BOLD2_RE = re.compile(r"__(.+?)__", re.S)
_MD_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_MD_DEL_RE = re.compile(r"~~(.+?)~~", re.S)
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
_MD_SAFE_URL_RE = re.compile(r"^(?:https?://|mailto:)", re.I)


def _md_inline(text: str) -> str:
    """把一行 Markdown 行内标记转成 HTML。

    先整体转义再上标记，所以语料里任何 <script> 之类的东西都只会变成字面量。
    """
    escaped = _esc(text)
    slots: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        slots.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(slots) - 1}\x00"

    escaped = _MD_CODE_RE.sub(_stash, escaped)
    escaped = _MD_BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _MD_BOLD2_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _MD_ITALIC_RE.sub(r"<em>\1</em>", escaped)
    escaped = _MD_DEL_RE.sub(r"<del>\1</del>", escaped)

    def _link(match: re.Match[str]) -> str:
        label = match.group(1) or match.group(2)
        url = html.unescape(match.group(2))
        if not _MD_SAFE_URL_RE.match(url):
            return label
        return f'<a href="{_esc(url)}">{label}</a>'

    escaped = _MD_LINK_RE.sub(_link, escaped)
    for index, value in enumerate(slots):
        escaped = escaped.replace(f"\x00{index}\x00", value)
    return escaped


def _md_table(rows: list[str]) -> str:
    def cells(line: str) -> list[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [cell.strip() for cell in stripped.split("|")]

    head = cells(rows[0])
    body = [cells(line) for line in rows[2:]]
    width = max([len(head)] + [len(row) for row in body] or [0])
    parts = ["<table class=\"md-table\"><thead><tr>"]
    for index in range(width):
        parts.append(f"<th>{_md_inline(head[index] if index < len(head) else '')}</th>")
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>")
        for index in range(width):
            parts.append(f"<td>{_md_inline(row[index] if index < len(row) else '')}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def markdown_to_html(source: str) -> str:
    """一个刚好够用的 Markdown 子集渲染器。

    只支持标题、有序/无序列表、代码块、引用、分割线、简单表格和常见行内标记，
    不引入任何第三方依赖，且所有文本都会先经过 HTML 转义。
    """
    lines = str(source or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    quote: list[str] = []
    list_stack: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br/>".join(_md_inline(line) for line in para) + "</p>")
            para.clear()

    def flush_quote() -> None:
        if quote:
            out.append(
                "<blockquote>"
                + "<br/>".join(_md_inline(line) for line in quote)
                + "</blockquote>",
            )
            quote.clear()

    def flush_list() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    def flush_all() -> None:
        flush_para()
        flush_quote()
        flush_list()

    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]

        fence = _MD_FENCE_RE.match(line)
        if fence:
            flush_all()
            marker = fence.group(1)[0]
            lang = fence.group(2) or ""
            index += 1
            buffer: list[str] = []
            while index < total:
                candidate = _MD_FENCE_RE.match(lines[index])
                if candidate and candidate.group(1)[0] == marker:
                    index += 1
                    break
                buffer.append(lines[index])
                index += 1
            label = f'<div class="md-code-lang">{_esc(lang)}</div>' if lang else ""
            out.append(
                f'<div class="md-code">{label}<pre><code>{_esc(chr(10).join(buffer))}'
                "</code></pre></div>",
            )
            continue

        if not line.strip():
            flush_all()
            index += 1
            continue

        if _MD_HR_RE.match(line) and not _MD_UL_RE.match(line.rstrip()):
            flush_all()
            out.append('<hr class="md-hr"/>')
            index += 1
            continue

        heading = _MD_HEADING_RE.match(line)
        if heading:
            flush_all()
            level = min(6, max(1, len(heading.group(1))))
            out.append(f"<h{level}>{_md_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        quoted = _MD_QUOTE_RE.match(line)
        if quoted:
            flush_para()
            flush_list()
            quote.append(quoted.group(1))
            index += 1
            continue

        if (
            "|" in line
            and index + 1 < total
            and "|" in lines[index + 1]
            and _MD_TABLE_SPLIT_RE.match(lines[index + 1])
        ):
            flush_all()
            rows = [line, lines[index + 1]]
            index += 2
            while index < total and "|" in lines[index] and lines[index].strip():
                rows.append(lines[index])
                index += 1
            out.append(_md_table(rows))
            continue

        bullet = _MD_UL_RE.match(line)
        ordered = _MD_OL_RE.match(line) if not bullet else None
        if bullet or ordered:
            flush_para()
            flush_quote()
            tag = "ul" if bullet else "ol"
            content = bullet.group(2) if bullet else ordered.group(3)
            if not list_stack:
                out.append(f'<{tag} class="md-list">')
                list_stack.append(tag)
            elif list_stack[-1] != tag:
                out.append(f"</{list_stack.pop()}>")
                out.append(f'<{tag} class="md-list">')
                list_stack.append(tag)
            out.append(f"<li>{_md_inline(content.strip())}</li>")
            index += 1
            continue

        flush_quote()
        flush_list()
        para.append(line.strip())
        index += 1

    flush_all()
    return "".join(out)


def build_markdown_card_html(
    source: str,
    ctx: CardContext,
    *,
    footer_lines: list[str] | None = None,
) -> str:
    """把一段 Markdown 正文渲染成与画像卡片同主题的完整 HTML 文档。"""
    theme = normalize_theme(ctx.theme)
    css = _THEME_CSS[theme] + _BASE_CSS + _EXTRA_CSS + _MD_CSS
    body = markdown_to_html(source)
    if not body:
        body = '<div class="empty">这次没有解析到可展示的内容。</div>'
    lines = list(footer_lines or [])
    if not lines:
        lines = [ctx.footer_note or "人格棱镜 · Persona Prism"]
        if ctx.model:
            lines.append(f"模型 {ctx.model}")
        lines.append("内容由 AI 生成，仅供娱乐")
    sign = "<br/>".join(
        (f"<strong>{_esc(item)}</strong>" if pos == 0 else _esc(item))
        for pos, item in enumerate(lines)
    )
    inner = (
        f"{_hero_html(ctx)}"
        f'<div class="md-body">{body}</div>'
        f'<div class="foot foot-md"><div class="sign">{sign}</div></div>'
    )
    return _document(ctx, css, inner, badge=theme_label(theme))


# ---------------------------------------------------------------------------
# 指令速查卡（棱镜帮助）
# ---------------------------------------------------------------------------

#: 分类配色。刻意挑中等明度的色相：填色配白字在浅色主题（杂志 / 宣纸）上对比够，
#: 放到深色主题（极光 / 霓虹）里也不会糊掉。分类多于配色数时循环取用。
HELP_ACCENTS: tuple[str, ...] = (
    "#3b82f6",
    "#8b5cf6",
    "#06b6d4",
    "#10b981",
    "#f59e0b",
    "#ec4899",
    "#ef4444",
)

#: 分类里超过这么多条指令就横跨整行、内部再分两列，避免一列拉得又细又长。
HELP_WIDE_THRESHOLD = 5

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

#: 棱镜 logo：白光进、七色散出。做成内联 SVG 而不是引用图片文件，因为 t2i 是远端
#: 渲染，拿不到本地路径；内联既不额外增大体积，也保证两条渲染链路画面一致。
_HELP_LOGO_SVG = (
    '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" fill="none">'
    '<path d="M32 7 L59 53 H5 Z" fill="var(--radar-fill)" stroke="var(--accent)"'
    ' stroke-width="2.6" stroke-linejoin="round"/>'
    '<path d="M1 31 H21" stroke="var(--ink-strong)" stroke-width="2.6" stroke-linecap="round"/>'
    '<path d="M41 30 H63" stroke="#ef4444" stroke-width="2.2" stroke-linecap="round"/>'
    '<path d="M41 35 H61" stroke="#f59e0b" stroke-width="2.2" stroke-linecap="round"/>'
    '<path d="M41 40 H59" stroke="#10b981" stroke-width="2.2" stroke-linecap="round"/>'
    '<path d="M41 45 H57" stroke="#06b6d4" stroke-width="2.2" stroke-linecap="round"/>'
    '<path d="M41 50 H55" stroke="#8b5cf6" stroke-width="2.2" stroke-linecap="round"/>'
    "</svg>"
)

_HELP_CSS = """
.help-hero { display: flex; align-items: center; gap: 22px; }
.help-logo {
  width: 92px; height: 92px; flex: 0 0 92px;
  border-radius: 26px;
  background: var(--avatar-bg); border: var(--avatar-border);
  display: flex; align-items: center; justify-content: center;
}
.help-logo svg { width: 56px; height: 56px; display: block; }
.help-hero .who h1 { font-size: 32px; }
.help-sub { margin-top: 9px; font-size: 13.5px; line-height: 1.65; color: var(--ink-dim); }
.help-stats { display: flex; gap: 12px; margin-top: 24px; }
.help-stat {
  flex: 1; padding: 14px 16px; border-radius: 16px;
  background: var(--block-bg); border: var(--block-border);
}
.help-stat b {
  display: block; font-family: var(--font-title);
  font-size: 24px; line-height: 1.1; color: var(--ink-strong);
}
.help-stat span { display: block; margin-top: 5px; font-size: 11.5px; letter-spacing: .08em; color: var(--ink-mute); }
.help-spectrum {
  display: flex; height: 9px; margin-top: 20px;
  border-radius: 999px; overflow: hidden; background: var(--bar-bg);
}
.help-spectrum i { display: block; height: 100%; min-width: 10px; background: var(--cat); }
.help-legend { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 11px; }
.help-legend span { display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--ink-mute); }
.help-legend span::before { content: ""; width: 9px; height: 9px; border-radius: 3px; background: var(--cat); }
.help-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 26px; align-items: start; }
.help-cat {
  padding: 16px 18px 15px; border-radius: 18px;
  background: var(--block-bg); border: var(--block-border);
}
.help-cat.wide { grid-column: 1 / -1; }
.help-cat.wide .help-cmds { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 22px; }
.help-cat-head { display: flex; align-items: center; gap: 10px; }
.help-cat-mark {
  width: 26px; height: 26px; flex: 0 0 26px;
  border-radius: 9px; background: var(--cat); color: #fff;
  font-family: var(--font-title); font-size: 12px;
  display: flex; align-items: center; justify-content: center;
}
.help-cat-head h3 { flex: 1; font-family: var(--font-title); font-size: 16px; color: var(--ink-strong); }
.help-cat-head em { font-style: normal; font-size: 11px; color: var(--ink-mute); }
.help-cat-desc { margin-top: 7px; font-size: 12px; line-height: 1.55; color: var(--ink-mute); }
.help-cmds { margin-top: 12px; display: flex; flex-direction: column; gap: 10px; }
.help-cmd { display: flex; gap: 9px; }
.help-cmd .n {
  flex: 0 0 20px; padding-top: 2px; text-align: right;
  font-family: var(--font-title); font-size: 11px; color: var(--ink-mute);
}
.help-cmd .body { flex: 1; min-width: 0; }
.help-cmd .c {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  font-size: 14px; font-weight: 600; color: var(--ink-strong);
}
.help-cmd .al {
  font-size: 10.5px; font-weight: 400; padding: 2px 7px; border-radius: 999px;
  background: var(--chip-bg); color: var(--ink-dim); border: var(--chip-border);
}
.help-cmd .d { margin-top: 3px; font-size: 12px; line-height: 1.55; color: var(--ink-dim); }
.help-foot {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
  margin-top: 26px; padding-top: 18px; border-top: 1px dashed var(--rule);
}
.help-foot h4 { font-family: var(--font-title); font-size: 12px; letter-spacing: .14em; color: var(--accent); margin-bottom: 7px; }
.help-foot p { font-size: 11.5px; line-height: 1.7; color: var(--ink-mute); }
.help-note { margin-top: 18px; font-size: 11.5px; line-height: 1.6; color: var(--ink-mute); text-align: right; }
"""


@dataclass(slots=True)
class HelpItem:
    """速查卡里的一条指令。"""

    command: str
    label: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class HelpGroup:
    """一个指令分类。"""

    name: str
    desc: str = ""
    items: list[HelpItem] = field(default_factory=list)
    #: 留空表示按顺序从 HELP_ACCENTS 取色。
    accent: str = ""
    #: None 表示按条数自动决定要不要横跨整行。
    wide: bool | None = None


@dataclass(slots=True)
class HelpCard:
    """棱镜帮助卡片的全部内容。"""

    title: str = "人格棱镜"
    kicker: str = "PERSONA PRISM · 指令速查"
    subtitle: str = ""
    groups: list[HelpGroup] = field(default_factory=list)
    #: 顶部统计块：(数值, 说明)。
    stats: list[tuple[str, str]] = field(default_factory=list)
    #: 底部三栏：(小标题, 若干行)。
    footers: list[tuple[str, list[str]]] = field(default_factory=list)
    note: str = ""


def _help_accent(group: HelpGroup, index: int) -> str:
    """分类配色。外部可以指定，但只接受 #RGB / #RRGGBB，避免把任意串拼进 style。"""
    fallback = HELP_ACCENTS[index % len(HELP_ACCENTS)]
    candidate = str(group.accent or "").strip()
    return candidate if _HEX_COLOR_RE.match(candidate) else fallback


def _help_widths(groups: Sequence[HelpGroup]) -> list[bool]:
    """决定每个分类占整行（内部再分两列）还是只占半行。

    分类是两列瀑布式排布的，一个"半行"分类如果左右都没有同伴，就会在右侧留下一大块
    空白，把卡片无谓地拉长。所以这里先按条数挑出"可以只占半行"的候选，再让相邻候选
    两两成对；落单的候选升级成整行。显式设置了 HelpGroup.wide 的分类不参与配对，
    调用方的意图优先。
    """
    n = len(groups)
    #: None 表示"可以窄，但还没定"；True/False 是已经拍死的结果。
    resolved: list[bool | None] = []
    for group in groups:
        if group.wide is not None:
            resolved.append(bool(group.wide))
        else:
            resolved.append(None if len(group.items) <= HELP_WIDE_THRESHOLD else True)
    index = 0
    while index < n:
        if resolved[index] is not None:
            index += 1
            continue
        if index + 1 < n and resolved[index + 1] is None:
            resolved[index] = False
            resolved[index + 1] = False
            index += 2
            continue
        # 落单：宁可整行两列，也不要在右边空出半张卡的高度。
        resolved[index] = True
        index += 1
    return [bool(flag) for flag in resolved]


def build_help_card_html(card: HelpCard, ctx: CardContext) -> str:
    """把指令速查渲染成一份自包含 HTML（无 JS、无 Jinja 占位符）。

    与画像卡片共用主题变量，所以「棱镜主题」切到哪套，帮助卡就跟着变。
    """
    theme = normalize_theme(ctx.theme)
    css = _THEME_CSS[theme] + _BASE_CSS + _EXTRA_CSS + _HELP_CSS

    groups = [group for group in card.groups if group.items]
    accents = [_help_accent(group, pos) for pos, group in enumerate(groups)]

    stats_html = ""
    if card.stats:
        cells = "".join(
            f'<div class="help-stat"><b>{_esc(value)}</b><span>{_esc(name)}</span></div>'
            for value, name in card.stats
        )
        stats_html = f'<div class="help-stats">{cells}</div>'

    spectrum_html = ""
    if groups:
        # 每段宽度按该分类的指令条数分配，一眼看出各玩法的体量。
        bars = "".join(
            f'<i style="--cat:{accents[pos]};flex:{len(group.items)} 1 0"></i>'
            for pos, group in enumerate(groups)
        )
        legend = "".join(
            f'<span style="--cat:{accents[pos]}">{_esc(group.name)} · {len(group.items)}</span>'
            for pos, group in enumerate(groups)
        )
        spectrum_html = (
            f'<div class="help-spectrum">{bars}</div>'
            f'<div class="help-legend">{legend}</div>'
        )

    cats: list[str] = []
    widths = _help_widths(groups)
    for pos, group in enumerate(groups):
        wide = widths[pos]
        rows: list[str] = []
        for order, item in enumerate(group.items, start=1):
            chips = "".join(
                f'<span class="al">{_esc(alias)}</span>' for alias in item.aliases if alias
            )
            desc = f'<div class="d">{_esc(item.label)}</div>' if item.label else ""
            rows.append(
                '<div class="help-cmd">'
                f'<span class="n">{order:02d}</span>'
                '<div class="body">'
                f'<div class="c">{_esc(item.command)}{chips}</div>{desc}'
                "</div></div>",
            )
        desc_html = f'<div class="help-cat-desc">{_esc(group.desc)}</div>' if group.desc else ""
        cats.append(
            f'<div class="help-cat{" wide" if wide else ""}" style="--cat:{accents[pos]}">'
            '<div class="help-cat-head">'
            f'<span class="help-cat-mark">{pos + 1:02d}</span>'
            f"<h3>{_esc(group.name)}</h3><em>{len(group.items)} 条</em>"
            f'</div>{desc_html}'
            f'<div class="help-cmds">{"".join(rows)}</div>'
            "</div>",
        )
    grid_html = f'<div class="help-grid">{"".join(cats)}</div>' if cats else ""

    foot_cells = "".join(
        "<div><h4>" + _esc(title) + "</h4><p>" + "<br/>".join(_esc(line) for line in lines) + "</p></div>"
        for title, lines in card.footers
    )
    foot_html = f'<div class="help-foot">{foot_cells}</div>' if foot_cells else ""
    note_html = f'<div class="help-note">{_esc(card.note)}</div>' if card.note else ""

    hero = (
        '<div class="hero help-hero">'
        f'<div class="help-logo">{_HELP_LOGO_SVG}</div>'
        '<div class="who">'
        f'<div class="kicker">{_esc(card.kicker)}</div>'
        f"<h1>{_esc(card.title)}</h1>"
        + (f'<div class="help-sub">{_esc(card.subtitle)}</div>' if card.subtitle else "")
        + "</div></div>"
    )
    inner = f"{hero}{stats_html}{spectrum_html}{grid_html}{foot_html}{note_html}"
    return _document(ctx, css, inner, badge=theme_label(theme))

# ---------------------------------------------------------------------------
# 渲染器
# ---------------------------------------------------------------------------

_BACKEND_ORDER: dict[str, tuple[str, ...]] = {
    "auto": ("t2i", "playwright", "pil", "text"),
    "local_first": ("playwright", "t2i", "pil", "text"),
    "t2i_only": ("t2i", "text"),
    "text_only": ("text",),
}

BACKEND_LABELS: dict[str, str] = {
    "t2i": "AstrBot t2i",
    "playwright": "本地 Playwright",
    "pil": "本地文转图",
    "text": "纯文本",
}


class CardRenderer:
    """四层兜底的卡片渲染器。

    顺序由 render.backend 决定：

    * auto        —— 官方 t2i → 本地 Playwright → 本地文转图 → 纯文本
    * local_first —— 本地 Playwright → 官方 t2i → 本地文转图 → 纯文本
    * t2i_only    —— 官方 t2i → 纯文本
    * text_only   —— 纯文本

    渲染成功的图片会复制一份到 cards 目录，WebUI 的记录详情直接读这份，
    不依赖框架的临时文件生命周期。
    """

    def __init__(self, star: Any, config: Any, cards_dir: Any, logger: Any = None) -> None:
        self._star = star
        self._config = config
        self._cards_dir = Path(cards_dir)
        self._log = logger
        #: Playwright 缺失时不必每张卡片都重试一遍。
        self._playwright_unavailable = False

    # -- 工具 ---------------------------------------------------------------
    def _debug(self, message: str) -> None:
        if self._log is not None:
            self._log.debug(message)

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(message)

    def _quality(self) -> int:
        try:
            return max(60, min(100, int(self._config.int_of("render.image_quality"))))
        except Exception:
            return 92

    def _scale(self) -> float:
        """卡片放大倍数。100 = 原尺寸，200 = 两倍像素。"""
        try:
            percent = int(self._config.int_of("render.card_scale"))
        except Exception:
            percent = 200
        return max(100, min(300, percent)) / 100.0

    def _image_format(self) -> str:
        try:
            fmt = str(self._config.str_of("render.image_format") or "").strip().lower()
        except Exception:
            fmt = ""
        return "png" if fmt == "png" else "jpeg"

    def _shot_options(self) -> dict[str, Any]:
        """截图参数。png 是无损格式，传 quality 会被 Playwright 直接拒绝。"""
        fmt = self._image_format()
        options: dict[str, Any] = {"full_page": True, "type": fmt}
        if fmt == "jpeg":
            options["quality"] = self._quality()
        return options

    def _suffix(self) -> str:
        return ".png" if self._image_format() == "png" else ".jpg"

    def _ctx_for(self, ctx: CardContext, backend: str) -> CardContext:
        """按后端决定放大方式。

        官方 t2i 端点只接受 Playwright 的截图参数，没有 viewport /
        device_scale_factor，所以那一层只能靠 CSS zoom 提清晰度；本地 Playwright
        由我们自己开浏览器，用 device_scale_factor 更稳（完全不影响布局）。
        """
        zoom = self._scale() if backend == "t2i" else 1.0
        body, title, src = self._font_setting()
        return replace(
            ctx,
            zoom=zoom,
            font_family=body or ctx.font_family,
            font_title_family=title or ctx.font_title_family,
            font_src=src or ctx.font_src,
        )

    def _font_setting(self) -> tuple[str, str, str]:
        """读一次字体配置。任何异常都退回"用主题自带字体"。"""
        try:
            body = self._config.str_of("render.font_family")
            title = self._config.str_of("render.font_title_family")
            source = self._config.str_of("render.font_source")
        except Exception:
            return "", "", ""
        resolved = resolve_font_source(source, logger=self._log) if source else ""
        if source and not resolved:
            self._warn(f"[persona_prism] 自定义字体不可用，已回退默认字体：{source}")
        return body, title, resolved

    def _timeout(self) -> float:
        try:
            return float(max(20, min(300, int(self._config.int_of("llm.timeout_sec")))))
        except Exception:
            return 60.0

    def backends(self) -> tuple[str, ...]:
        try:
            mode = self._config.str_of("render.backend")
        except Exception:
            mode = "auto"
        return _BACKEND_ORDER.get(mode, _BACKEND_ORDER["auto"])

    def _persist(self, source: str, record_key: str) -> str:
        """把渲染结果落到 cards 目录，返回文件名。"""
        src = Path(source)
        if not src.is_file():
            return ""
        self._cards_dir.mkdir(parents=True, exist_ok=True)
        suffix = src.suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".jpg"
        stem = re.sub(r"[^0-9A-Za-z_-]", "_", str(record_key or "")) or f"card_{int(time.time() * 1000)}"
        target = self._cards_dir / f"{stem}{suffix}"
        try:
            if src.resolve() != target.resolve():
                shutil.copyfile(src, target)
        except Exception as exc:
            self._warn(f"[persona_prism] 卡片落盘失败：{exc}")
            return ""
        return target.name

    # -- 各层实现 -----------------------------------------------------------
    async def _render_network(self, markup: str) -> str:
        """走 AstrBot 官方 t2i 端点。

        return_url=False 会让框架把远端图片下载到本地并返回路径，正好是
        我们要的（后续还要复制进 cards 目录）。
        """
        if self._star is None:
            return ""
        options = self._shot_options()
        result = await asyncio.wait_for(
            self._star.html_render(markup, {}, return_url=False, options=options),
            timeout=self._timeout(),
        )
        return str(result or "")

    async def _render_playwright(self, markup: str) -> str:
        """本地 Chromium 截图。

        AstrBot 的 LocalRenderStrategy.render_custom_template 是
        NotImplementedError，所以这一层只能自己实现。
        """
        if self._playwright_unavailable:
            return ""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._playwright_unavailable = True
            self._debug("[persona_prism] 未安装 playwright，跳过本地 HTML 渲染层")
            return ""
        self._cards_dir.mkdir(parents=True, exist_ok=True)
        out = self._cards_dir / f"_tmp_{int(time.time() * 1000)}{self._suffix()}"
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(args=["--no-sandbox"])
                try:
                    page = await browser.new_page(
                        viewport={"width": 968, "height": 1400},
                        device_scale_factor=self._scale(),
                    )
                    await page.set_content(markup, wait_until="load")
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state("networkidle", timeout=6000)
                    await page.screenshot(path=str(out), **self._shot_options())
                finally:
                    await browser.close()
        except Exception:
            out.unlink(missing_ok=True)
            raise
        return str(out)

    async def _render_pil(self, text: str) -> str:
        """最后一层图片兜底：框架自带的本地 Markdown 文转图（纯 PIL）。"""
        from astrbot.api import html_renderer

        result = await asyncio.wait_for(
            html_renderer.render_t2i(text, use_network=False),
            timeout=self._timeout(),
        )
        return str(result or "")

    # -- 入口 ---------------------------------------------------------------
    async def _run(
        self,
        build: Any,
        text: str,
        ctx: CardContext,
        record_key: str,
    ) -> RenderResult:
        """按 backend 顺序逐层尝试。build(ctx) 负责产出该层要用的 HTML。"""
        temps: list[Path] = []
        try:
            for backend in self.backends():
                if backend == "text":
                    break
                try:
                    if backend == "t2i":
                        produced = await self._render_network(build(self._ctx_for(ctx, backend)))
                    elif backend == "playwright":
                        produced = await self._render_playwright(
                            build(self._ctx_for(ctx, backend)),
                        )
                    else:
                        produced = await self._render_pil(text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._warn(
                        f"[persona_prism] {BACKEND_LABELS.get(backend, backend)} 渲染失败：{exc}",
                    )
                    continue
                if not produced:
                    continue
                if backend == "playwright":
                    temps.append(Path(produced))
                card_file = self._persist(produced, record_key)
                if not card_file:
                    continue
                return RenderResult(
                    backend=backend,
                    image_path=str(self._cards_dir / card_file),
                    card_file=card_file,
                    text=text,
                )
        finally:
            for temp in temps:
                with contextlib.suppress(Exception):
                    temp.unlink(missing_ok=True)
        return RenderResult(backend="text", text=text)

    async def render(
        self,
        portrait: Portrait,
        ctx: CardContext,
        record_key: str = "",
    ) -> RenderResult:
        """渲染结构化画像卡片。"""
        return await self._run(
            lambda scoped: build_card_html(portrait, scoped),
            portrait.to_plain_text(ctx.title),
            ctx,
            record_key,
        )

    async def render_help(
        self,
        card: HelpCard,
        ctx: CardContext,
        text: str,
        record_key: str = "",
    ) -> RenderResult:
        """渲染指令速查卡。text 是纯文本兜底（图片全挂了就发它）。"""
        return await self._run(
            lambda scoped: build_help_card_html(card, scoped),
            str(text or ""),
            ctx,
            record_key,
        )

    async def render_markdown(
        self,
        source: str,
        ctx: CardContext,
        record_key: str = "",
        *,
        footer_lines: list[str] | None = None,
    ) -> RenderResult:
        """渲染 Markdown 正文卡片（"画像"系列玩法用）。"""
        return await self._run(
            lambda scoped: build_markdown_card_html(
                source,
                scoped,
                footer_lines=footer_lines,
            ),
            str(source or ""),
            ctx,
            record_key,
        )


__all__ = [
    "AUTO_THEME",
    "AUTO_THEME_META",
    "BACKEND_LABELS",
    "DEFAULT_THEME",
    "FONT_FALLBACK",
    "FONT_MAX_BYTES",
    "FONT_MIME_BY_SUFFIX",
    "HELP_ACCENTS",
    "THEMES",
    "THEME_ALIASES",
    "THEME_CHOICES",
    "THEME_KEYWORDS",
    "CardContext",
    "CardRenderer",
    "HelpCard",
    "HelpGroup",
    "HelpItem",
    "RenderResult",
    "build_card_html",
    "build_help_card_html",
    "build_markdown_card_html",
    "confidence_label",
    "describe_theme_choice",
    "is_auto_theme",
    "markdown_to_html",
    "match_theme_choice",
    "neutralize_jinja",
    "normalize_theme",
    "normalize_theme_choice",
    "pick_theme",
    "radar_geometry",
    "resolve_font_source",
    "resolve_theme",
    "sanitize_font_family",
    "sanitize_font_src",
    "theme_affinity",
    "theme_label",
    "theme_scores",
]
