"""自动挡（render.theme = auto）的选题逻辑。

这一档要同时满足两件看起来矛盾的事：

* **确定性**：同一张画像 + 同一个 seed 必须永远挑出同一套，否则 WebUI 重看旧卡、
  重新渲染就会变脸，落库的 theme 也就不可信了。
* **多样性**：一群人轮着画，主题得真的换着来，不能退化成「其实永远是 aurora」。

所以测试分两组：内容能说清时看它选得对不对，内容说不清时看它散得开不开。
"""

from __future__ import annotations

from collections import Counter

from astrbot_plugin_persona_prism.prism import cards
from astrbot_plugin_persona_prism.prism.models import Dimension, Portrait, Section, Tag


def _portrait(**kw) -> Portrait:
    base: dict = {"headline": "", "confidence": 0.7, "structured": True}
    base.update(kw)
    return Portrait(**base)


#: 五组「气质写得很明白」的画像，每组的期望主题就是它读起来的样子。
ARCHETYPES: dict[str, Portrait] = {
    "neon": _portrait(
        headline="凌晨三点还在玩梗的抽象战士",
        tags=[Tag("话痨"), Tag("毒舌", "negative"), Tag("中二")],
        dimensions=[Dimension("活跃度", 92), Dimension("攻击性", 74), Dimension("自控力", 21)],
        sections=[Section("整体印象", "熬夜刷屏，整活不断，弹幕式发言。")],
    ),
    "ink": _portrait(
        headline="话不多但句句克制的茶艺爱好者",
        tags=[Tag("沉稳", "positive"), Tag("内敛"), Tag("慢热")],
        dimensions=[Dimension("活跃度", 22), Dimension("自控力", 85), Dimension("攻击性", 8)],
        sections=[Section("整体印象", "平和安静，偏好古风与留白，很讲分寸。")],
    ),
    "paper": _portrait(
        headline="群里的技术答疑台",
        tags=[Tag("严谨", "positive"), Tag("专业", "positive"), Tag("认真", "positive")],
        dimensions=[Dimension("理性", 90), Dimension("专业度", 88), Dimension("情绪化", 18)],
        sections=[Section("整体印象", "输出干货，讲逻辑重条理，常做分析总结。")],
        confidence=0.88,
    ),
    "dossier": _portrait(
        headline="常年潜水的观察者",
        tags=[Tag("高冷", "negative"), Tag("神秘"), Tag("疏离", "negative")],
        dimensions=[Dimension("距离感", 88), Dimension("活跃度", 14), Dimension("表达欲", 20)],
        sections=[Section("整体印象", "多数时候旁观，难以捉摸，边界感很强。")],
        confidence=0.36,
    ),
    "aurora": _portrait(
        headline="群里的氛围发动机",
        tags=[Tag("热情", "positive"), Tag("体贴", "positive"), Tag("元气", "positive")],
        dimensions=[Dimension("亲和力", 91), Dimension("共情力", 86), Dimension("攻击性", 6)],
        sections=[Section("整体印象", "爱捧场爱分享，情绪细腻，很会治愈别人。")],
        confidence=0.8,
    ),
}


# ---------------------------------------------------------------------------
# 档位本身
# ---------------------------------------------------------------------------


def test_auto_is_a_choice_but_not_a_real_theme():
    # THEMES 只装真配色，自动挡只出现在「可选档位」里，
    # 这样凡是拿 THEMES 取 CSS 的地方都不会被自动挡噎住。
    assert cards.AUTO_THEME not in cards.THEMES
    assert cards.AUTO_THEME in cards.THEME_CHOICES
    assert list(cards.THEME_CHOICES) == [cards.AUTO_THEME, *cards.THEMES]
    assert list(cards.PORTRAIT_THEME_CHOICES) == [cards.AUTO_THEME, *cards.PORTRAIT_THEMES]
    assert list(cards.LOVE_THEME_CHOICES) == [cards.AUTO_THEME, *cards.LOVE_THEMES]
    assert cards.THEME_CHOICES[cards.AUTO_THEME]["label"]
    assert cards.THEME_CHOICES[cards.AUTO_THEME]["desc"]


def test_render_time_normalizer_never_returns_auto():
    # 渲染层拿到 auto 只能回落成真主题，否则会去 _THEME_CSS 里找一个不存在的键。
    assert cards.normalize_theme(cards.AUTO_THEME) == cards.DEFAULT_THEME


def test_config_time_normalizer_keeps_auto():
    assert cards.normalize_theme_choice("auto") == "auto"
    assert cards.normalize_theme_choice(" AUTO ") == "auto"
    assert cards.normalize_theme_choice("ink") == "ink"
    assert cards.normalize_theme_choice("不存在") == cards.DEFAULT_THEME
    assert cards.normalize_theme_choice("") == cards.DEFAULT_THEME


def test_is_auto_theme_tolerates_case_and_spaces():
    assert cards.is_auto_theme("auto")
    assert cards.is_auto_theme(" Auto ")
    assert not cards.is_auto_theme("aurora")
    assert not cards.is_auto_theme("")


def test_theme_label_knows_auto():
    assert cards.theme_label(cards.AUTO_THEME) == "自动挡"


def test_match_theme_choice_accepts_names_labels_and_aliases():
    assert cards.match_theme_choice("neon") == "neon"
    assert cards.match_theme_choice("NEON") == "neon"
    assert cards.match_theme_choice("水墨宣纸") == "ink"
    assert cards.match_theme_choice("auto") == "auto"
    assert cards.match_theme_choice("自动挡") == "auto"
    assert cards.match_theme_choice("随机") == "auto"
    assert cards.match_theme_choice("不存在的主题") == ""
    assert cards.match_theme_choice("  ") == ""


def test_every_alias_points_at_a_real_choice():
    for target in cards.THEME_ALIASES.values():
        assert target in cards.THEME_CHOICES


# ---------------------------------------------------------------------------
# 内容说得清的时候：选得对
# ---------------------------------------------------------------------------


def test_each_archetype_picks_the_theme_it_reads_like():
    for expected, portrait in ARCHETYPES.items():
        assert cards.pick_theme(portrait, seed=f"t:{expected}") == expected


def test_archetypes_stay_on_theme_no_matter_the_seed():
    # 抖动只该在气质难分时说话；写得这么明白的画像不能被 seed 带跑。
    for expected, portrait in ARCHETYPES.items():
        picked = {cards.pick_theme(portrait, seed=f"seed-{i}") for i in range(40)}
        assert picked == {expected}


def test_affinity_is_pure_content_without_luck():
    # theme_affinity 不掺抖动，所以同一张画像的原始分必须逐位相等。
    portrait = ARCHETYPES["paper"]
    assert cards.theme_affinity(portrait) == cards.theme_affinity(portrait)
    assert set(cards.theme_affinity(portrait)) == set(cards.PORTRAIT_THEMES)
    assert cards.theme_affinity(portrait)["paper"] > cards.theme_affinity(portrait)["neon"]


def test_low_confidence_leans_towards_the_dossier_look():
    thin = _portrait(headline="资料太少，只能勉强下结论", confidence=0.2)
    thick = _portrait(headline="资料太少，只能勉强下结论", confidence=0.9)
    assert cards.theme_affinity(thin)["dossier"] > cards.theme_affinity(thick)["dossier"]


def test_mostly_negative_tags_lean_dark():
    harsh = _portrait(tags=[Tag("刻薄", "negative"), Tag("易怒", "negative"), Tag("普通")])
    warm = _portrait(tags=[Tag("大方", "positive"), Tag("耐心", "positive"), Tag("普通")])
    assert cards.theme_affinity(harsh)["dossier"] > cards.theme_affinity(warm)["dossier"]
    assert cards.theme_affinity(warm)["aurora"] > cards.theme_affinity(harsh)["aurora"]


def test_content_score_is_normalised_so_long_portraits_do_not_dominate():
    # 长画像随手能命中十几个词。如果不归一化，「文字多」会盖过抖动与避重复，
    # 自动挡就退化成固定主题了。归一化之后最贴的那套永远正好拿满分。
    wordy = ARCHETYPES["neon"]
    terse = _portrait(headline="夜猫子")
    for portrait in (wordy, terse):
        scores = cards.theme_scores(portrait, seed="x")
        jitter = {
            name: cards._stable_jitter("x", name) * cards._W_JITTER
            for name in cards.PORTRAIT_THEMES
        }
        content = {name: scores[name] - jitter[name] for name in cards.PORTRAIT_THEMES}
        assert max(content.values()) == cards._W_CONTENT


# ---------------------------------------------------------------------------
# 内容说不清的时候：散得开
# ---------------------------------------------------------------------------


def test_blank_portrait_still_gets_a_real_theme():
    for candidate in (None, _portrait(), _portrait(structured=False, raw_text="就是个普通群友")):
        assert cards.pick_theme(candidate, seed="blank") in cards.PORTRAIT_THEMES


def test_featureless_portraits_spread_across_all_five_themes():
    counter: Counter[str] = Counter()
    for index in range(200):
        portrait = _portrait(headline=f"群友{index}", tags=[Tag("普通")])
        counter[cards.pick_theme(portrait, seed=f"aiocqhttp:900:{10000 + index}")] += 1
    assert set(counter) == set(cards.PORTRAIT_THEMES)
    # 不追求严格均匀，只要没有哪一套吃掉半壁江山就算散开了。
    assert max(counter.values()) < 100


def test_same_portrait_and_seed_always_pick_the_same_theme():
    portrait = ARCHETYPES["ink"]
    first = cards.pick_theme(portrait, seed="stable")
    assert all(cards.pick_theme(portrait, seed="stable") == first for _ in range(5))


def test_jitter_is_a_stable_hash_not_pythons_randomised_one():
    # 内置 hash() 对 str 加了进程级随机盐，用它同一张画像换个进程就换主题。
    value = cards._stable_jitter("seed", "ink")
    assert value == cards._stable_jitter("seed", "ink")
    assert 0.0 <= value < 1.0
    assert value != cards._stable_jitter("seed", "neon")


# ---------------------------------------------------------------------------
# 避重复
# ---------------------------------------------------------------------------


def test_recently_used_themes_are_penalised():
    portrait = _portrait(headline="群友甲", tags=[Tag("普通")])
    plain = cards.theme_scores(portrait, seed="s")
    avoided = cards.theme_scores(portrait, seed="s", avoid=["ink", "neon"])
    assert avoided["ink"] == plain["ink"] - cards._AVOID_PENALTY[0]
    assert avoided["neon"] == plain["neon"] - cards._AVOID_PENALTY[1]
    assert avoided["paper"] == plain["paper"]


def test_avoiding_the_winner_changes_the_pick_when_the_race_is_close():
    portrait = _portrait(headline="群友乙", tags=[Tag("普通")])
    first = cards.pick_theme(portrait, seed="close")
    second = cards.pick_theme(portrait, seed="close", avoid=[first])
    assert second != first


def test_avoid_cannot_override_an_obvious_personality():
    # 「避免连着撞主题」是锦上添花，不能把明摆着的气质判反。
    for expected, portrait in ARCHETYPES.items():
        for seed in ("a", "b", "c", "d", "e"):
            assert cards.pick_theme(portrait, seed=seed, avoid=[expected]) == expected


def test_avoid_penalty_is_skipped_only_for_a_runaway_winner():
    runaway = ARCHETYPES["paper"]
    plain = cards.theme_scores(runaway, seed="s")
    avoided = cards.theme_scores(runaway, seed="s", avoid=["paper", "ink"])
    # 内容分一边倒 → 领跑的那套免降权，陪跑的照旧降权。
    assert avoided["paper"] == plain["paper"]
    assert avoided["ink"] == plain["ink"] - cards._AVOID_PENALTY[1]


def test_avoid_ignores_garbage_and_extra_entries():
    portrait = ARCHETYPES["aurora"]
    picked = cards.pick_theme(
        portrait,
        seed="a",
        avoid=["", "auto", "不存在的主题", "ink", "neon", "paper", "dossier"],
    )
    assert picked in cards.PORTRAIT_THEMES


# ---------------------------------------------------------------------------
# 对外的解析入口
# ---------------------------------------------------------------------------


def test_resolve_theme_passes_fixed_choices_through():
    assert cards.resolve_theme("ink", ARCHETYPES["neon"]) == "ink"
    assert cards.resolve_theme("不存在", ARCHETYPES["neon"]) == cards.DEFAULT_THEME


def test_resolve_theme_delegates_auto_to_the_picker():
    portrait = ARCHETYPES["dossier"]
    assert cards.resolve_theme("auto", portrait, seed="r") == cards.pick_theme(portrait, seed="r")


def test_describe_theme_choice_spells_out_what_auto_landed_on():
    assert cards.describe_theme_choice("ink") == "水墨宣纸"
    assert cards.describe_theme_choice("ink", "neon") == "水墨宣纸"
    assert cards.describe_theme_choice("auto", "neon") == "自动挡 · 本次 赛博霓虹"
    assert cards.describe_theme_choice("auto") == "自动挡"


def test_auto_theme_renders_a_real_card():
    # 端到端兜底：自动挡的产物必须能真的渲成 HTML，不能只是个字符串。
    portrait = ARCHETYPES["paper"]
    theme = cards.resolve_theme("auto", portrait, seed="render")
    html_out = cards.build_card_html(portrait, cards.CardContext(theme=theme, target_name="小明"))
    assert cards.theme_label(theme) in html_out
    assert "{{" not in html_out


# ---------------------------------------------------------------------------
# 恋爱卡专属皮肤
# ---------------------------------------------------------------------------


def test_love_auto_only_lands_on_love_skins():
    counter: Counter[str] = Counter()
    for index in range(120):
        portrait = _portrait(headline=f"群友{index}", tags=[Tag("普通")])
        picked = cards.resolve_love_theme(cards.AUTO_THEME, portrait, seed=f"seed{index}")
        counter[picked] += 1
    assert set(counter) <= set(cards.LOVE_THEMES)
    # 四套都有机会上场，不会永远只出樱粉
    assert len(counter) >= 2


def test_love_theme_rejects_portrait_only_skins():
    portrait = _portrait()
    assert cards.resolve_love_theme("dossier", portrait) == cards.DEFAULT_LOVE_THEME
    assert cards.resolve_love_theme("paper", portrait) == cards.DEFAULT_LOVE_THEME
    assert cards.resolve_love_theme("moonlit", portrait) == "moonlit"
    assert cards.resolve_love_theme("berry", portrait) == "berry"


def test_portrait_theme_rejects_love_only_skins():
    portrait = _portrait()
    assert cards.normalize_love_theme_choice("moonlit") == "moonlit"
    assert cards.normalize_love_theme_choice("aurora") == cards.DEFAULT_LOVE_THEME
    assert cards.normalize_theme_choice("moonlit") == cards.DEFAULT_THEME
    assert cards.normalize_theme_choice("ink") == "ink"
    assert cards.resolve_theme("moonlit", portrait) == cards.PORTRAIT_THEMES[0]


def test_love_skins_are_fully_registered():
    for name in cards.LOVE_THEMES:
        assert name in cards._THEME_CSS
        assert name in cards.EVIDENCE_STYLE
        assert name in cards.TITLE_BADGE_STYLE
        assert name in cards.THEME_KEYWORDS
