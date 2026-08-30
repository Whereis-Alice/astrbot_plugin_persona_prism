"""cards 层：主题归一、Jinja2 中和、雷达几何、卡片 HTML 自包含性。

这一层的产物会被发到远端 t2i 端点渲染，所以「不含 Jinja2 占位符」「不含
script」两条是硬约束，必须有回归测试兜着。
"""

from __future__ import annotations

from astrbot_plugin_persona_prism.prism import cards
from astrbot_plugin_persona_prism.prism.models import (
    Dimension,
    Evidence,
    Portrait,
    Section,
    Tag,
    Term,
    Utterance,
)


def _portrait(**kw):
    base: dict = {
        "kind": "portrait",
        "headline": "夜猫子技术流",
        "tags": [Tag("热心", "positive"), Tag("嘴硬", "negative"), Tag("话痨")],
        "dimensions": [
            Dimension("表达欲", 82, "长句多"),
            Dimension("攻击性", 24, "几乎不呛人"),
            Dimension("专业度", 71, "常聊技术"),
            Dimension("在线度", 90, "凌晨也在"),
        ],
        "sections": [Section("整体印象", "长期活跃，输出稳定。")],
        "evidence": [Evidence("这个 bug 我修过", "体现动手能力")],
        "advice": ["少熬夜"],
        "confidence": 0.72,
        "structured": True,
    }
    base.update(kw)
    return Portrait(**base)


def _ctx(**kw):
    base: dict = {
        "title": "人格画像",
        "kind_label": "人格画像",
        "target_name": "小明",
        "target_id": "10001",
        "group_name": "测试群",
        "avatar_url": "https://q1.qlogo.cn/g?b=qq&nk=10001&s=640",
        "sample_size": 240,
        "total_corpus": 980,
        "span_days": 12.5,
        "model": "test-model",
    }
    base.update(kw)
    return cards.CardContext(**base)


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------


def test_themes_have_six_entries_with_label_and_desc():
    assert len(cards.THEMES) == 6
    assert set(cards.THEMES) == {"aurora", "ink", "neon", "paper", "dossier", "sakura"}
    for meta in cards.THEMES.values():
        assert meta["label"]
        assert meta["desc"]


def test_default_theme_is_registered():
    assert cards.DEFAULT_THEME in cards.THEMES


def test_normalize_theme_falls_back_for_unknown_values():
    assert cards.normalize_theme("ink") == "ink"
    assert cards.normalize_theme("") == cards.DEFAULT_THEME
    assert cards.normalize_theme("不存在的主题") == cards.DEFAULT_THEME


def test_theme_label_returns_raw_name_when_unknown():
    assert cards.theme_label("neon") == "赛博霓虹"
    assert cards.theme_label("whatever") == "whatever"


def test_every_theme_has_its_own_css_block():
    for name in cards.THEMES:
        assert name in cards._THEME_CSS
        assert cards._THEME_CSS[name].strip()


# ---------------------------------------------------------------------------
# Jinja2 中和
# ---------------------------------------------------------------------------


def test_neutralize_jinja_breaks_all_three_token_kinds():
    raw = "a {{ 7*7 }} b {% for x in y %} c {# note #}"
    out = cards.neutralize_jinja(raw)
    assert "{{" not in out
    assert "{%" not in out
    assert "{#" not in out
    assert out.count("{<!-- -->") == 3


def test_neutralize_jinja_keeps_plain_braces_untouched():
    raw = "css { color: red } and {single}"
    assert cards.neutralize_jinja(raw) == raw


# ---------------------------------------------------------------------------
# 置信度
# ---------------------------------------------------------------------------


def test_confidence_label_thresholds():
    assert cards.confidence_label(1.0) == "高"
    assert cards.confidence_label(0.8) == "高"
    assert cards.confidence_label(0.79) == "中"
    assert cards.confidence_label(0.6) == "中"
    assert cards.confidence_label(0.59) == "低"
    assert cards.confidence_label(0.0) == "低"


# ---------------------------------------------------------------------------
# 雷达几何
# ---------------------------------------------------------------------------


def test_radar_geometry_degenerates_below_three_axes():
    for scores in ([], [50], [50, 50]):
        geo = cards.radar_geometry(scores)
        assert geo == {"polygon": "", "axes": [], "rings": [], "labels": []}


def test_radar_geometry_shapes_match_axis_count():
    geo = cards.radar_geometry([10, 50, 90, 70, 30])
    assert len(geo["axes"]) == 5
    assert len(geo["labels"]) == 5
    assert len(geo["polygon"].split(" ")) == 5
    assert len(geo["rings"]) == 4
    for ring in geo["rings"]:
        assert len(ring.split(" ")) == 5


def test_radar_geometry_first_axis_points_straight_up():
    geo = cards.radar_geometry([100, 100, 100], cx=130, cy=130, radius=96)
    first = geo["axes"][0]
    assert abs(first["x"] - 130) < 1e-6
    assert abs(first["y"] - 34) < 1e-6


def test_radar_geometry_clamps_zero_score_off_the_center():
    geo = cards.radar_geometry([0, 0, 0], cx=130, cy=130, radius=96)
    xs = [float(point.split(",")[0]) for point in geo["polygon"].split(" ")]
    ys = [float(point.split(",")[1]) for point in geo["polygon"].split(" ")]
    # 最小 ratio 是 0.06，所以第一个点应该略高于圆心而不是压在圆心上。
    assert abs(xs[0] - 130) < 0.5
    assert 120 < ys[0] < 130


def test_radar_geometry_scales_with_score():
    low = cards.radar_geometry([20, 20, 20])
    high = cards.radar_geometry([100, 100, 100])
    low_y = float(low["polygon"].split(" ")[0].split(",")[1])
    high_y = float(high["polygon"].split(" ")[0].split(",")[1])
    # 分数越高，顶点越靠外（y 越小）。
    assert high_y < low_y


# ---------------------------------------------------------------------------
# 卡片 HTML
# ---------------------------------------------------------------------------


def test_build_card_html_is_a_self_contained_document():
    html_out = cards.build_card_html(_portrait(), _ctx())
    assert html_out.startswith("<!DOCTYPE html>")
    assert html_out.rstrip().endswith("</html>")
    assert "<style>" in html_out
    assert "<link" not in html_out


def test_build_card_html_never_emits_jinja_or_script():
    portrait = _portrait(
        headline="{{ 7*7 }}",
        sections=[Section("注入测试", "{% for x in y %}<script>alert(1)</script>")],
        evidence=[Evidence("{# hi #}", "原话里带模板语法")],
    )
    html_out = cards.build_card_html(portrait, _ctx())
    assert "{{" not in html_out
    assert "{%" not in html_out
    assert "{#" not in html_out
    assert "<script" not in html_out.lower()
    assert "&lt;script&gt;" in html_out


def test_build_card_html_shows_theme_badge():
    html_out = cards.build_card_html(_portrait(), _ctx(theme="ink"))
    assert cards.theme_label("ink") in html_out
    assert 'class="badge"' in html_out


def test_build_card_html_unknown_theme_uses_default_badge():
    html_out = cards.build_card_html(_portrait(), _ctx(theme="不存在"))
    assert cards.theme_label(cards.DEFAULT_THEME) in html_out


def test_build_card_html_renders_core_content():
    html_out = cards.build_card_html(_portrait(), _ctx())
    assert "夜猫子技术流" in html_out
    assert "热心" in html_out
    assert "表达欲" in html_out
    assert "整体印象" in html_out
    assert "少熬夜" in html_out
    assert "小明" in html_out
    assert "测试群" in html_out


def test_build_card_html_hides_evidence_when_disabled():
    quote = "这个 bug 我修过"
    shown = cards.build_card_html(_portrait(), _ctx(show_evidence=True))
    hidden = cards.build_card_html(_portrait(), _ctx(show_evidence=False))
    assert quote in shown
    assert quote not in hidden


def test_build_card_html_hides_avatar_when_disabled():
    url = "https://q1.qlogo.cn/g?b=qq&nk=10001&s=640"
    shown = cards.build_card_html(_portrait(), _ctx(show_avatar=True))
    hidden = cards.build_card_html(_portrait(), _ctx(show_avatar=False))
    assert "qlogo.cn" in shown
    assert url.replace("&", "&amp;") in shown
    assert "qlogo.cn" not in hidden


def test_build_card_html_adds_small_sample_note():
    small = cards.build_card_html(_portrait(), _ctx(sample_size=12))
    large = cards.build_card_html(_portrait(), _ctx(sample_size=240))
    assert 'class="notes"' in small
    assert 'class="notes"' not in large


def test_build_card_html_notes_sit_below_footer():
    html_out = cards.build_card_html(_portrait(), _ctx(sample_size=8))
    assert html_out.index('class="foot"') < html_out.index('class="notes"')


def test_build_card_html_renders_sample_note_from_portrait():
    portrait = _portrait()
    portrait.sample_note = "口径：近 7 天，共 42 句"
    html_out = cards.build_card_html(portrait, _ctx(sample_size=240))
    assert "口径：近 7 天，共 42 句" in html_out


def test_build_card_html_renders_evidence_as_chat_scene():
    portrait = _portrait(
        evidence=[
            Evidence(
                quote="我来修",
                reason="主动接活",
                title="23:11 · 深夜救火",
                dialogue=[
                    Utterance(speaker="阿光", text="这段代码崩了"),
                    Utterance(speaker="[本人]", text="我来修", mine=True),
                ],
            ),
        ],
    )
    html_out = cards.build_card_html(portrait, _ctx())
    assert cards.EVIDENCE_STYLE[cards.DEFAULT_THEME]["title"] in html_out
    assert "原话证据" not in html_out
    assert 'class="cbub"' in html_out
    assert 'class="crow right"' in html_out
    assert 'class="crow left"' in html_out
    assert "阿光" in html_out
    assert "23:11 · 深夜救火" in html_out
    assert "主动接活" in html_out


def test_build_card_html_evidence_title_follows_theme():
    for theme, style in cards.EVIDENCE_STYLE.items():
        html_out = cards.build_card_html(_portrait(), _ctx(theme=theme))
        assert style["title"] in html_out


def test_build_card_html_renders_equation_and_glossary():
    portrait = _portrait()
    portrait.equation = "L = (V + N) - (I + S) + 200"
    portrait.glossary = [Term(name="纯爱值", code="S", brief="含糖量", detail="越高越腻")]
    html_out = cards.build_card_html(portrait, _ctx())
    assert "演化算式" in html_out
    assert "L = (V + N) - (I + S) + 200" in html_out
    assert "术语速查" in html_out
    assert "纯爱值" in html_out
    assert "越高越腻" in html_out


def test_build_card_html_falls_back_to_raw_block_when_unstructured():
    portrait = Portrait(
        kind="clone",
        headline="",
        raw_text="你是小明，说话简短。",
        structured=False,
    )
    html_out = cards.build_card_html(portrait, _ctx(kind_label="人格克隆"))
    assert 'class="raw"' in html_out
    assert "你是小明" in html_out
    assert 'class="empty"' not in html_out


def test_build_card_html_shows_empty_hint_when_nothing_parsed():
    portrait = Portrait(structured=True)
    html_out = cards.build_card_html(portrait, _ctx(sample_size=0))
    assert 'class="empty"' in html_out


def test_build_card_html_omits_radar_when_dimensions_too_few():
    few = _portrait(dimensions=[Dimension("表达欲", 80)])
    html_out = cards.build_card_html(few, _ctx())
    assert "<svg" not in html_out
    assert "表达欲" in html_out


def test_build_card_html_draws_radar_with_enough_dimensions():
    html_out = cards.build_card_html(_portrait(), _ctx())
    assert "<svg" in html_out
    assert 'class="radar"' in html_out


def test_build_card_html_is_stable_across_all_themes():
    for theme in cards.THEMES:
        html_out = cards.build_card_html(_portrait(), _ctx(theme=theme))
        assert html_out.startswith("<!DOCTYPE html>")
        assert "{{" not in html_out
        assert "<script" not in html_out.lower()
        assert len(html_out) > 2000


# ---------------------------------------------------------------------------
# 渲染链
# ---------------------------------------------------------------------------


def test_backend_order_covers_every_mode():
    assert set(cards._BACKEND_ORDER) == {"auto", "local_first", "t2i_only", "text_only"}
    assert cards._BACKEND_ORDER["auto"] == ("t2i", "playwright", "pil", "text")
    assert cards._BACKEND_ORDER["local_first"] == ("playwright", "t2i", "pil", "text")
    assert cards._BACKEND_ORDER["t2i_only"] == ("t2i", "text")
    assert cards._BACKEND_ORDER["text_only"] == ("text",)


def test_every_backend_has_a_human_label():
    used = {name for order in cards._BACKEND_ORDER.values() for name in order}
    assert used == set(cards.BACKEND_LABELS)


def test_all_backend_orders_end_with_text_fallback():
    for order in cards._BACKEND_ORDER.values():
        assert order[-1] == "text"


class _FakeConfig:
    def __init__(self, backend="auto"):
        self._backend = backend

    def str_of(self, path):
        if path == "render.backend":
            return self._backend
        return ""

    def int_of(self, path):
        return 85


def test_renderer_backends_follow_config(tmp_path):
    renderer = cards.CardRenderer(None, _FakeConfig("local_first"), tmp_path / "cards")
    assert renderer.backends()[0] == "playwright"


def test_renderer_backends_fall_back_to_auto_for_bad_mode(tmp_path):
    renderer = cards.CardRenderer(None, _FakeConfig("乱填"), tmp_path / "cards")
    assert renderer.backends() == cards._BACKEND_ORDER["auto"]


class _BrokenConfig:
    def str_of(self, path):
        raise RuntimeError("boom")

    def int_of(self, path):
        raise RuntimeError("boom")


def test_renderer_survives_broken_config(tmp_path):
    renderer = cards.CardRenderer(None, _BrokenConfig(), tmp_path / "cards")
    assert renderer.backends() == cards._BACKEND_ORDER["auto"]
    assert renderer._quality() == 92
    assert renderer._timeout() == 60.0
    # 配置全炸时清晰度旋钮也得有可用缺省，否则卡片会直接渲染失败。
    assert renderer._scale() == 2.0
    assert renderer._image_format() == "jpeg"
    assert renderer._font_setting() == ("", "", "")
