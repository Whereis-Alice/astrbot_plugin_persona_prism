"""分析层测试：JSON 抢救、置信度校正、样本折扣、provider 选取与三层兜底。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from astrbot_plugin_persona_prism.prism.analyzer import (
    AnalyzeError,
    PrismAnalyzer,
    build_portrait,
    clamp_confidence,
    extract_json_object,
    parse_portrait_payload,
    plain_portrait,
    sample_penalty,
    strip_code_fence,
)
from astrbot_plugin_persona_prism.prism.config import PrismConfig
from astrbot_plugin_persona_prism.prism.models import CorpusBundle, CorpusMessage, CorpusStats
from astrbot_plugin_persona_prism.prism.prompts import PromptSpec

FENCE = chr(96) * 3


# -- 文本抢救 ---------------------------------------------------------------


def test_strip_code_fence_removes_backtick_block() -> None:
    text = FENCE + 'json\n{"headline": "x"}\n' + FENCE
    assert strip_code_fence(text) == '{"headline": "x"}'


def test_strip_code_fence_removes_tilde_block() -> None:
    assert strip_code_fence('~~~\n{"a": 1}\n~~~') == '{"a": 1}'


def test_strip_code_fence_keeps_plain_text_untouched() -> None:
    assert strip_code_fence("  就是一段普通文字  ") == "就是一段普通文字"


def test_extract_json_object_ignores_surrounding_chatter() -> None:
    text = '好的，这是结果：\n{"headline": "很会修风车"}\n希望有帮助！'
    assert extract_json_object(text) == '{"headline": "很会修风车"}'


def test_extract_json_object_returns_empty_when_no_braces() -> None:
    assert extract_json_object("完全没有 JSON") == ""
    assert extract_json_object("} 顺序反了 {") == ""


def test_parse_payload_repairs_trailing_comma() -> None:
    payload = parse_portrait_payload('{"headline": "x", "advice": ["a",],}')
    assert payload == {"headline": "x", "advice": ["a"]}


def test_parse_payload_repairs_smart_quotes() -> None:
    payload = parse_portrait_payload("{\u201cheadline\u201d: \u201c风车\u201d}")
    assert payload == {"headline": "风车"}


def test_parse_payload_handles_fenced_json() -> None:
    text = "解析如下\n" + FENCE + 'json\n{"headline": "ok"}\n' + FENCE
    assert parse_portrait_payload(text) == {"headline": "ok"}


def test_parse_payload_returns_none_for_garbage() -> None:
    assert parse_portrait_payload("完全不是 JSON") is None
    assert parse_portrait_payload('[{"a": 1}, {"b": 2}]') is None


def test_parse_payload_unwraps_single_element_array() -> None:
    assert parse_portrait_payload('[{"headline": "x"}]') == {"headline": "x"}


# -- 置信度 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.8, 0.8),
        (85, 0.85),
        (100, 1.0),
        (250, 1.0),
        (-3, 0.0),
        ("0.4", 0.4),
        ("高", 0.0),
        (None, 0.0),
    ],
)
def test_clamp_confidence(raw: Any, expected: float) -> None:
    assert clamp_confidence(raw) == pytest.approx(expected)


def test_sample_penalty_no_discount_above_three_times_floor() -> None:
    assert sample_penalty(60, 20) == 1.0
    assert sample_penalty(600, 20) == 1.0


def test_sample_penalty_at_floor_is_three_quarters() -> None:
    assert sample_penalty(20, 20) == pytest.approx(0.75)


def test_sample_penalty_grows_monotonically() -> None:
    values = [sample_penalty(n, 20) for n in range(1, 61)]
    assert values == sorted(values)
    assert all(0.29 < v <= 1.0 for v in values)


def test_sample_penalty_has_floor_for_tiny_samples() -> None:
    assert sample_penalty(0, 20) == pytest.approx(0.3)
    assert sample_penalty(1, 500) == pytest.approx(0.3)


def test_sample_penalty_handles_zero_min_messages() -> None:
    assert sample_penalty(5, 0) == 1.0


# -- 画像组装 ---------------------------------------------------------------


def _bundle(sampled: int = 90) -> CorpusBundle:
    return CorpusBundle(
        messages=[CorpusMessage(str(i), "10001", text="第" + str(i) + "句") for i in range(3)],
        stats=CorpusStats(total=sampled, sampled=sampled, chars=sampled * 5),
    )


def test_build_portrait_clamps_dimension_scores() -> None:
    portrait = build_portrait(
        {
            "headline": "标题",
            "dimensions": [
                {"name": "超上限", "score": 900},
                {"name": "负数", "score": -20},
            ],
        },
        kind="portrait",
        bundle=_bundle(),
        min_messages=20,
    )
    assert [d.score for d in portrait.dimensions] == [100, 0]


def test_build_portrait_forces_kind_and_structured() -> None:
    portrait = build_portrait(
        {"kind": "hacked", "structured": False, "headline": "标题"},
        kind="praise",
        bundle=_bundle(),
        min_messages=20,
    )
    assert portrait.kind == "praise"
    assert portrait.structured is True


def test_build_portrait_defaults_missing_confidence() -> None:
    portrait = build_portrait(
        {"headline": "标题"},
        kind="portrait",
        bundle=_bundle(),
        min_messages=20,
    )
    assert portrait.confidence == pytest.approx(0.6)


def test_build_portrait_discounts_and_notes_small_sample() -> None:
    portrait = build_portrait(
        {"headline": "标题", "confidence": 0.9},
        kind="portrait",
        bundle=_bundle(sampled=8),
        min_messages=20,
    )
    assert portrait.confidence < 0.9
    assert portrait.sections == []
    assert "8 条" in portrait.sample_note
    assert "60 条" in portrait.sample_note


def test_build_portrait_keeps_sections_intact_for_large_sample() -> None:
    portrait = build_portrait(
        {"headline": "标题", "confidence": 0.9, "sections": [{"title": "风格", "body": "正文"}]},
        kind="portrait",
        bundle=_bundle(sampled=90),
        min_messages=20,
    )
    assert [s.title for s in portrait.sections] == ["风格"]
    assert portrait.sample_note == ""
    assert portrait.confidence == pytest.approx(0.9)


def test_build_portrait_keeps_raw_text_for_audit() -> None:
    portrait = build_portrait(
        {"headline": "标题"},
        kind="portrait",
        bundle=_bundle(),
        min_messages=20,
        raw_text="原始返回",
    )
    assert portrait.raw_text == "原始返回"


def test_plain_portrait_is_unstructured_with_low_confidence() -> None:
    portrait = plain_portrait("  他说话像写日记 ", kind="clone")
    assert portrait.structured is False
    assert portrait.kind == "clone"
    assert portrait.raw_text == "他说话像写日记"
    assert portrait.confidence == pytest.approx(0.35)


# -- 专属头衔 ---------------------------------------------------------------


def test_build_portrait_cleans_the_model_title() -> None:
    portrait = build_portrait(
        {"headline": "标题", "title": "「头衔：深夜哲学家」"},
        kind="portrait",
        bundle=_bundle(),
        min_messages=20,
    )
    assert portrait.title == "深夜哲学家"


def test_build_portrait_invents_a_title_when_the_model_skips_it() -> None:
    from astrbot_plugin_persona_prism.prism import titles

    portrait = build_portrait(
        {"headline": "标题"},
        kind="roast",
        bundle=_bundle(),
        min_messages=20,
        seed="g:u",
    )
    assert portrait.title in titles.KIND_TITLES["roast"]


def test_build_portrait_title_can_come_from_a_peak_dimension() -> None:
    portrait = build_portrait(
        {"headline": "标题", "dimensions": [{"name": "话密度", "score": 97}, {"name": "克制", "score": 20}]},
        kind="portrait",
        bundle=_bundle(),
        min_messages=20,
    )
    assert portrait.title == "话密度满格"


def test_build_portrait_can_leave_the_title_empty_for_love() -> None:
    """恋爱诊断自己按四维推头衔，这里必须留空，别被称号池顶掉。"""
    portrait = build_portrait(
        {"headline": "标题"},
        kind="love",
        bundle=_bundle(),
        min_messages=20,
        title_fallback=False,
    )
    assert portrait.title == ""


def test_plain_portrait_gets_a_title_too() -> None:
    assert plain_portrait("长文", kind="legacy_portrait", seed="s").title


def test_plain_portrait_title_can_be_suppressed() -> None:
    assert plain_portrait("长文", kind="love", title_fallback=False).title == ""


# -- provider 选取 ----------------------------------------------------------


class _Reply:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class _Provider:
    def __init__(self, *replies: Any) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def text_chat(self, **kwargs: Any) -> _Reply:
        self.calls.append(kwargs)
        reply = self._replies.pop(0) if self._replies else ""
        if isinstance(reply, Exception):
            raise reply
        return _Reply(str(reply))


class _Context:
    def __init__(self, *, by_id=None, using=None, all_providers=None) -> None:
        self._by_id = by_id or {}
        self._using = using
        self._all = all_providers or []

    def get_provider_by_id(self, provider_id: str) -> Any:
        return self._by_id.get(provider_id)

    def get_using_provider(self, umo: str = "") -> Any:
        return self._using

    def get_all_providers(self) -> list[Any]:
        return self._all


def _analyzer(context: Any, **overrides: Any) -> PrismAnalyzer:
    raw: dict[str, Any] = {"llm": {"retry_times": 0}, "collect": {"min_messages": 20}}
    for path, value in overrides.items():
        group, _, key = path.partition("__")
        raw.setdefault(group, {})[key] = value
    return PrismAnalyzer(context, PrismConfig(raw))


def test_resolve_provider_prefers_configured_id() -> None:
    wanted = _Provider()
    context = _Context(by_id={"mine": wanted}, using=_Provider())
    analyzer = _analyzer(context, llm__provider_id="mine")
    assert analyzer.resolve_provider("umo") is wanted


def test_resolve_provider_falls_back_when_configured_id_missing() -> None:
    using = _Provider()
    analyzer = _analyzer(_Context(using=using), llm__provider_id="ghost")
    assert analyzer.resolve_provider("umo") is using


def test_resolve_provider_falls_back_to_first_global_provider() -> None:
    first = _Provider()
    analyzer = _analyzer(_Context(all_providers=[first, _Provider()]))
    assert analyzer.resolve_provider() is first


def test_resolve_provider_raises_when_nothing_available() -> None:
    analyzer = _analyzer(_Context())
    with pytest.raises(AnalyzeError):
        analyzer.resolve_provider()


# -- analyze 主流程 ---------------------------------------------------------

_SPEC = PromptSpec("portrait", "棱镜画像", "人格画像", "请分析这个人")
_CLONE_SPEC = PromptSpec("clone", "棱镜克隆", "人格克隆", "模仿他说话", structured=False)


def _run(analyzer: PrismAnalyzer, spec: PromptSpec = _SPEC, **kwargs: Any):
    return asyncio.run(analyzer.analyze(spec, _bundle(), target_name="阿狸", **kwargs))


def test_analyze_returns_structured_portrait() -> None:
    provider = _Provider('{"headline": "很会修风车", "confidence": 0.8}')
    analyzer = _analyzer(_Context(using=provider), llm__model="gpt-x")
    portrait, model = _run(analyzer)
    assert portrait.structured is True
    assert portrait.headline == "很会修风车"
    assert model == "gpt-x"
    assert provider.calls[0]["model"] == "gpt-x"
    assert provider.calls[0]["contexts"] == []


def test_analyze_omits_model_kwarg_when_unset() -> None:
    provider = _Provider('{"headline": "ok"}')
    _, model = _run(_analyzer(_Context(using=provider)))
    assert model == ""
    assert "model" not in provider.calls[0]


def test_analyze_retries_after_bad_json_then_succeeds() -> None:
    provider = _Provider("不是 JSON", '{"headline": "第二次好了"}')
    analyzer = _analyzer(_Context(using=provider), llm__retry_times=1)
    portrait, _ = _run(analyzer)
    assert portrait.headline == "第二次好了"
    assert len(provider.calls) == 2


def test_analyze_retries_after_exception() -> None:
    provider = _Provider(RuntimeError("boom"), '{"headline": "恢复"}')
    analyzer = _analyzer(_Context(using=provider), llm__retry_times=1)
    portrait, _ = _run(analyzer)
    assert portrait.headline == "恢复"


def test_analyze_degrades_to_plain_text_when_json_never_arrives() -> None:
    provider = _Provider("这人挺有意思的。", "还是没有 JSON。")
    analyzer = _analyzer(_Context(using=provider), llm__retry_times=1)
    portrait, _ = _run(analyzer)
    assert portrait.structured is False
    assert portrait.raw_text == "还是没有 JSON。"


def test_analyze_skips_json_parsing_for_unstructured_spec() -> None:
    provider = _Provider("今天风车转得挺好的呢")
    portrait, _ = _run(_analyzer(_Context(using=provider)), _CLONE_SPEC)
    assert portrait.structured is False
    assert portrait.kind == "clone"
    assert portrait.confidence == pytest.approx(0.6)
    assert len(provider.calls) == 1


def test_analyze_raises_when_all_attempts_are_empty() -> None:
    provider = _Provider("", "   ")
    analyzer = _analyzer(_Context(using=provider), llm__retry_times=1)
    with pytest.raises(AnalyzeError):
        _run(analyzer)


def test_analyze_raises_when_provider_always_fails() -> None:
    provider = _Provider(RuntimeError("boom"), RuntimeError("boom again"))
    analyzer = _analyzer(_Context(using=provider), llm__retry_times=1)
    with pytest.raises(AnalyzeError):
        _run(analyzer)


def test_analyze_includes_partner_block_only_for_relevant_kinds() -> None:
    bundle = CorpusBundle(
        messages=[CorpusMessage("1", "10001", text="在的")],
        stats=CorpusStats(total=1, sampled=1),
        partners=[("小明", 4)],
    )
    provider = _Provider('{"headline": "a"}', '{"headline": "b"}')
    analyzer = _analyzer(_Context(using=provider))
    asyncio.run(analyzer.analyze(_SPEC, bundle, target_name="阿狸"))
    asyncio.run(
        analyzer.analyze(
            PromptSpec("praise", "棱镜赞赏", "群友赞赏", "夸夸他"),
            bundle,
            target_name="阿狸",
        ),
    )
    assert "# 互动痕迹" in provider.calls[0]["prompt"]
    assert "# 互动痕迹" not in provider.calls[1]["prompt"]


def test_analyze_passes_group_name_into_prompt() -> None:
    provider = _Provider('{"headline": "ok"}')
    analyzer = _analyzer(_Context(using=provider))
    _run(analyzer, group_name="风车研究会")
    assert "风车研究会" in provider.calls[0]["prompt"]
