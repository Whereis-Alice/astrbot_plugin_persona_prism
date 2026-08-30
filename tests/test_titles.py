"""专属头衔测试：脏输入清洗、恋爱四维推导、玩法兜底、种子稳定性。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_prism.prism import titles


def _metrics(**kw):
    """鸭子类型的 LoveMetrics 替身：titles 只按属性名取值。"""
    data = {"simp": 0, "vibe": 0, "ick": 0, "nostalgia": 0, "total": 0, "archetype_key": "normal"}
    data.update(kw)
    key = data.pop("archetype_key")
    return SimpleNamespace(archetype=SimpleNamespace(key=key), **data)


def _dim(name: str, score: int):
    return SimpleNamespace(name=name, score=score)


# ---------------------------------------------------------------------------
# normalize_title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("深夜哲学家", "深夜哲学家"),
        ("头衔：深夜哲学家", "深夜哲学家"),
        ("称号 - 深夜哲学家", "深夜哲学家"),
        ("TITLE: Night Owl", "Night Owl"),
        ("「深夜哲学家」", "深夜哲学家"),
        ("《深夜哲学家》", "深夜哲学家"),
        ("  深夜   哲学家  ", "深夜 哲学家"),
        ("纯爱战神(反讽)", "纯爱战神（反讽）"),
        ("纯爱战神（反讽）", "纯爱战神（反讽）"),
    ],
)
def test_normalize_title_cleans_dirty_model_output(raw, want):
    assert titles.normalize_title(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "「」",
        "（反讽）",
        "这个人在群里非常活跃，属于话密型选手。",
        "他很爱说话！",
    ],
)
def test_normalize_title_rejects_non_titles(raw):
    """整句话、只剩括注、空值都判为不可用，交给兜底。"""
    assert titles.normalize_title(raw) == ""


def test_normalize_title_limits_length():
    long = "超" * 40
    got = titles.normalize_title(long)
    assert got == "超" * titles.MAX_LEN


def test_normalize_title_drops_an_overlong_note():
    """超长括注是模型在写解释，不是标注 —— 只留正文，不截半句。"""
    assert titles.normalize_title("称号（" + "注" * 20 + "）") == "称号"


def test_normalize_title_keeps_a_short_note():
    assert titles.normalize_title("称号（" + "注" * titles.MAX_NOTE_LEN + "）") == (
        "称号（" + "注" * titles.MAX_NOTE_LEN + "）"
    )


def test_normalize_title_never_leaves_an_unclosed_bracket():
    """限长截断不许留半个括号。"""
    got = titles.normalize_title("超长前缀名字（补充说明）")
    assert got.count("（") == got.count("）")


# ---------------------------------------------------------------------------
# 恋爱诊断
# ---------------------------------------------------------------------------


def test_love_title_matches_upstream_screenshot_case():
    """上游截图：纯爱值 72 / 存在感 0 → 头衔要带「反讽」。"""
    got = titles.love_title(_metrics(simp=72, vibe=0, archetype_key="the_simp"), seed="a")
    assert got.endswith("（反讽）")
    assert titles.normalize_title(got) == got


def test_love_title_uses_archetype_pool():
    got = titles.love_title(_metrics(archetype_key="npc", total=40), seed="a")
    assert got in titles.LOVE_TITLES["npc"]


def test_love_title_falls_back_to_normal_pool_for_unknown_archetype():
    got = titles.love_title(_metrics(archetype_key="who_knows", total=40), seed="a")
    assert got in titles.LOVE_TITLES["normal"]


def test_love_title_is_stable_for_the_same_seed():
    metrics = _metrics(archetype_key="the_charmer", total=50)
    assert titles.love_title(metrics, seed="x") == titles.love_title(metrics, seed="x")


def test_love_title_varies_across_seeds():
    metrics = _metrics(archetype_key="the_charmer", total=50)
    seen = {titles.love_title(metrics, seed=f"seed-{i}") for i in range(40)}
    assert len(seen) > 1


@pytest.mark.parametrize(
    ("kw", "note"),
    [
        ({"simp": 80, "vibe": 5}, "反讽"),
        ({"ick": 90, "simp": 60, "vibe": 50}, "戴罪立功"),
        ({"vibe": 90, "simp": 10}, "无本万利"),
        ({"nostalgia": 95, "simp": 10}, "本尊认证"),
        ({"total": 92}, "官方认证"),
        ({"total": 5}, "有待观察"),
    ],
)
def test_love_note_rules(kw, note):
    got = titles.love_title(_metrics(**kw), seed="s")
    assert got.endswith(f"（{note}）")


def test_love_title_has_no_note_for_ordinary_scores():
    got = titles.love_title(_metrics(simp=40, vibe=40, total=45), seed="s")
    assert "（" not in got


def test_every_archetype_key_has_a_pool():
    from astrbot_plugin_persona_prism.prism.love import _ARCHETYPE_POLARITY

    assert set(_ARCHETYPE_POLARITY) <= set(titles.LOVE_TITLES)


# ---------------------------------------------------------------------------
# 其余玩法的兜底
# ---------------------------------------------------------------------------


def test_fallback_title_uses_peak_dimension():
    got = titles.fallback_title(
        "portrait",
        seed="a",
        dimensions=[_dim("话密度", 96), _dim("阴阳怪气", 30)],
    )
    assert got == "话密度满格"


def test_fallback_title_peak_needs_a_clear_lead():
    """六个维度都很高时「哪个爆表」不成立，退回称号池。"""
    got = titles.fallback_title(
        "portrait",
        seed="a",
        dimensions=[_dim("话密度", 92), _dim("阴阳怪气", 90)],
    )
    assert got in titles.KIND_TITLES["portrait"]


def test_fallback_title_ignores_low_dimensions():
    got = titles.fallback_title("roast", seed="a", dimensions=[_dim("话密度", 60)])
    assert got in titles.KIND_TITLES["roast"]


def test_fallback_title_ignores_broken_dimensions():
    got = titles.fallback_title(
        "roast",
        seed="a",
        dimensions=[_dim("这个维度名字太长了放不进头衔", 99), _dim("坏值", "呃")],
    )
    assert got in titles.KIND_TITLES["roast"]


def test_fallback_title_unknown_kind_uses_default_pool():
    got = titles.fallback_title("我的自定义提示词", seed="a")
    assert got in titles.DEFAULT_TITLES


def test_fallback_title_is_stable_and_seed_sensitive():
    args = {"tags": [], "dimensions": [], "headline": "话很多"}
    first = titles.fallback_title("praise", seed="u1", **args)
    assert first == titles.fallback_title("praise", seed="u1", **args)
    seen = {titles.fallback_title("praise", seed=f"u{i}", **args) for i in range(40)}
    assert len(seen) > 1


def test_fallback_title_never_returns_empty_for_every_builtin_kind():
    for kind in titles.KIND_TITLES:
        got = titles.fallback_title(kind, seed="z")
        assert got
        assert len(got) <= titles.MAX_LEN


def test_all_pooled_titles_survive_normalization():
    """池子里的称号本身必须是合法头衔，否则会被自己的清洗规则判死。"""
    pools = [*titles.LOVE_TITLES.values(), *titles.KIND_TITLES.values(), titles.DEFAULT_TITLES]
    for pool in pools:
        for name in pool:
            assert titles.normalize_title(name) == name, name
