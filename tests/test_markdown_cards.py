"""Markdown 卡片渲染与布局归一化。

「画像」系列（兼容上游 astrbot_plugin_portrayal）输出的是自由排版长文，
走 markdown_to_html -> build_markdown_card_html 这条链路，所以这里重点验证
转义安全、块级结构和链接白名单。
"""

from __future__ import annotations

from astrbot_plugin_persona_prism.prism.cards import (
    CardContext,
    build_markdown_card_html,
    markdown_to_html,
    theme_label,
)
from astrbot_plugin_persona_prism.prism.prompts import (
    JSON_CONTRACT,
    MARKDOWN_CONTRACT,
    VALID_LAYOUTS,
    PromptSpec,
    build_user_prompt,
    normalize_layout,
)

# ---------------------------------------------------------------------------
# 布局归一化
# ---------------------------------------------------------------------------


def test_valid_layouts_are_exactly_three() -> None:
    assert VALID_LAYOUTS == ("card", "markdown", "text")


def test_normalize_layout_accepts_valid_values() -> None:
    for value in VALID_LAYOUTS:
        assert normalize_layout(value) == value
        assert normalize_layout(value.upper() + "  ") == value


def test_normalize_layout_falls_back_by_structured_flag() -> None:
    for bad in ("", None, 0, "ghost", "json", [], {}):
        assert normalize_layout(bad, structured=True) == "card"
        assert normalize_layout(bad, structured=False) == "text"


# ---------------------------------------------------------------------------
# Markdown -> HTML
# ---------------------------------------------------------------------------


def test_markdown_escapes_html_before_marking_up() -> None:
    out = markdown_to_html("<script>alert(1)</script> **粗**")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>粗</strong>" in out


def test_markdown_renders_headings_and_lists() -> None:
    out = markdown_to_html("## 一、性格\n\n- 条目甲\n- 条目乙\n\n1. 第一\n2. 第二")
    assert "<h2>" in out and "一、性格" in out
    assert out.count("<li>") == 4
    assert "<ul" in out or "<ul>" in out
    assert "<ol" in out or "<ol>" in out


def test_markdown_renders_quote_and_rule() -> None:
    out = markdown_to_html("> 原话在此\n\n---\n\n正文")
    assert "<blockquote>" in out
    assert "<hr" in out


def test_markdown_fenced_code_is_literal() -> None:
    fence = "`" * 3
    out = markdown_to_html(fence + "text\n**不该加粗** <b>x</b>\n" + fence)
    assert "<pre>" in out
    assert "<strong>" not in out
    assert "&lt;b&gt;" in out


def test_markdown_inline_code_survives_other_marks() -> None:
    out = markdown_to_html("看 `**字面**` 和 **真粗体**")
    assert "<code>**字面**</code>" in out
    assert "<strong>真粗体</strong>" in out


def test_markdown_table_pads_short_rows() -> None:
    source = "| 维度 | 说明 | 备注 |\n| --- | --- | --- |\n| 表达 | 直球 |\n"
    out = markdown_to_html(source)
    assert out.count("<th>") == 3
    assert out.count("<td>") == 3


def test_markdown_links_only_allow_http_and_mailto() -> None:
    out = markdown_to_html("[点我](https://example.com) [坏的](javascript:alert(1))")
    assert '<a href="https://example.com">点我</a>' in out
    assert "javascript:" not in out
    assert "坏的" in out


def test_markdown_empty_source_returns_empty_string() -> None:
    assert markdown_to_html("") == ""
    assert markdown_to_html("   \n\n  ") == ""


# ---------------------------------------------------------------------------
# 完整文档
# ---------------------------------------------------------------------------


def _ctx(**kwargs: object) -> CardContext:
    base: dict[str, object] = {
        "title": "画像·综合",
        "kind_label": "画像·综合",
        "target_name": "阿狸",
        "target_id": "10001",
        "group_name": "风车研究会",
        "model": "gpt-test",
    }
    base.update(kwargs)
    return CardContext(**base)  # type: ignore[arg-type]


def test_markdown_card_is_a_complete_document() -> None:
    html = build_markdown_card_html("## 一、性格\n\n- 直球", _ctx())
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "md-body" in html
    assert "阿狸" in html
    assert "gpt-test" in html
    assert "仅供娱乐" in html


def test_markdown_card_shows_placeholder_when_body_is_empty() -> None:
    html = build_markdown_card_html("   ", _ctx())
    assert "没有解析到可展示的内容" in html


def test_markdown_card_uses_custom_footer_lines() -> None:
    html = build_markdown_card_html("正文", _ctx(), footer_lines=["自定义脚注", "第二行"])
    assert "<strong>自定义脚注</strong>" in html
    assert "第二行" in html
    assert "仅供娱乐" not in html


def test_markdown_card_honours_theme_and_zoom() -> None:
    html = build_markdown_card_html("正文", _ctx(theme="neon", zoom=2.0))
    assert "html{zoom:2;}" in html
    assert theme_label("neon") in html


def test_markdown_card_without_zoom_has_no_zoom_rule() -> None:
    assert "zoom:" not in build_markdown_card_html("正文", _ctx())


def test_markdown_card_applies_custom_font() -> None:
    html = build_markdown_card_html(
        "正文",
        _ctx(font_family="My Sans", font_src="https://example.com/f.woff2", font_name="My Sans"),
    )
    assert "@font-face" in html
    assert "My Sans" in html


def test_markdown_card_escapes_hostile_target_name() -> None:
    html = build_markdown_card_html("正文", _ctx(target_name="<img onerror=x>"))
    assert "<img onerror=x>" not in html
    assert "&lt;img" in html


# ---------------------------------------------------------------------------
# 提示词契约
# ---------------------------------------------------------------------------


def _sample_bundle():
    from astrbot_plugin_persona_prism.prism.models import (
        CorpusBundle,
        CorpusMessage,
        CorpusStats,
    )

    return CorpusBundle(
        messages=[CorpusMessage("1", "10001", "阿狸", "今天把风车修好了", 1700000000)],
        stats=CorpusStats(total=10, sampled=1, chars=8, avg_chars=8.0),
        scanned=10,
    )


def _prompt_for(layout: str, structured: bool = False) -> str:
    spec = PromptSpec(
        "legacy_portrait",
        "画像",
        "画像·综合",
        "分析这个人",
        structured=structured,
        layout=layout,
    )
    return build_user_prompt(spec, _sample_bundle(), target_name="阿狸")


def test_markdown_layout_prompt_uses_markdown_contract() -> None:
    text = _prompt_for("markdown")
    assert MARKDOWN_CONTRACT.strip() in text
    assert JSON_CONTRACT.strip() not in text


def test_card_layout_prompt_uses_json_contract() -> None:
    text = _prompt_for("card", structured=True)
    assert JSON_CONTRACT.strip() in text
    assert MARKDOWN_CONTRACT.strip() not in text


def test_text_layout_prompt_asks_for_plain_text_only() -> None:
    text = _prompt_for("text")
    assert "直接输出正文纯文本" in text
    assert JSON_CONTRACT.strip() not in text
    assert MARKDOWN_CONTRACT.strip() not in text


def test_output_contract_is_the_last_block() -> None:
    """契约必须压在语料之后，否则长语料会把格式要求冲掉。"""
    text = _prompt_for("markdown")
    assert text.rsplit("\n\n", 1)[-1].strip()
    assert text.index("# 语料") < text.index("# 输出格式")
