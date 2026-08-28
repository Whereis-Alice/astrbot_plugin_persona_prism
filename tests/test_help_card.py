"""指令速查卡（v1.1.5）。

两件事：卡片 HTML 本身要自包含且不带脚本；`棱镜帮助` 这条指令不能再崩 ——
v1.1.4 线上就是因为 `self.config.get("compat.legacy_commands", True)` 传了两个
参数（PrismConfig.get 只收一个）而整条指令抛 TypeError。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from astrbot_plugin_persona_prism.prism import cards
from astrbot_plugin_persona_prism.prism.config import DEFAULTS, PrismConfig
from astrbot_plugin_persona_prism.prism.prompts import PromptSpec


def _card() -> cards.HelpCard:
    return cards.HelpCard(
        title="人格棱镜 · 指令速查",
        kicker="PERSONA PRISM v1.1.5",
        subtitle="群友人格画像插件",
        groups=[
            cards.HelpGroup(
                name="棱镜系列",
                desc="结构化信息卡",
                items=[
                    cards.HelpItem("棱镜画像", "综合人格画像"),
                    cards.HelpItem("棱镜锐评", "毒舌版"),
                ],
            ),
            cards.HelpGroup(
                name="管理员",
                desc="维护语料",
                items=[cards.HelpItem("棱镜重扫", "重置断点", ("管理员",))],
            ),
        ],
        stats=[("9", "条指令"), ("5", "套主题")],
        footers=[("语料从哪来", ["平时聊天被动入库", "画像时回溯群历史"])],
        note="仅供娱乐",
    )


def _ctx(theme: str = "aurora") -> cards.CardContext:
    return cards.CardContext(
        title="人格棱镜 · 指令速查",
        kind_label="指令速查",
        theme=theme,
        show_avatar=False,
    )


# ---------------------------------------------------------------------------
# 卡片 HTML
# ---------------------------------------------------------------------------


def test_help_card_html_is_self_contained() -> None:
    html = cards.build_help_card_html(_card(), _ctx())
    assert html.lstrip().startswith("<!DOCTYPE html>")
    #: 渲染后端里有直接把 HTML 交给无头浏览器的分支，脚本一律不许出现。
    assert "<script" not in html
    #: t2i 走 Jinja 渲染，残留的占位符会把整张卡打崩。
    assert "{{" not in html and "{%" not in html


def test_help_card_html_renders_every_section() -> None:
    html = cards.build_help_card_html(_card(), _ctx())
    for fragment in (
        "help-stats",
        "help-spectrum",
        "help-legend",
        "help-grid",
        "help-cat-mark",
        "help-cmd",
        "help-foot",
        "help-note",
    ):
        assert fragment in html, fragment
    assert "棱镜系列" in html
    assert "棱镜画像" in html
    assert "管理员" in html
    assert "语料从哪来" in html


def test_help_card_spectrum_width_follows_item_count() -> None:
    html = cards.build_help_card_html(_card(), _ctx())
    #: 两条指令的分类占 2 份宽，一条的占 1 份 —— 体量差异要看得见。
    assert "flex:2 1 0" in html
    assert "flex:1 1 0" in html


def test_help_card_drops_empty_groups() -> None:
    card = _card()
    card.groups.append(cards.HelpGroup(name="空分类", desc="没有指令"))
    html = cards.build_help_card_html(card, _ctx())
    assert "空分类" not in html


def test_help_card_escapes_user_content() -> None:
    card = _card()
    card.groups[0].items.append(cards.HelpItem("<img onerror=x>", "<b>注入</b>"))
    html = cards.build_help_card_html(card, _ctx())
    assert "<img onerror" not in html
    assert "&lt;img" in html


def test_help_card_accent_rejects_arbitrary_strings() -> None:
    """分类配色会拼进 style，非法值必须退回预置色，不能原样透传。"""
    card = _card()
    card.groups[0].accent = "red;}#x{display:none"
    html = cards.build_help_card_html(card, _ctx())
    assert "display:none" not in html
    assert cards.HELP_ACCENTS[0] in html
    card.groups[0].accent = "#ff8800"
    assert "#ff8800" in cards.build_help_card_html(card, _ctx())


def test_help_card_follows_every_theme() -> None:
    for theme in cards.THEMES:
        html = cards.build_help_card_html(_card(), _ctx(theme))
        assert cards.theme_label(theme) in html


def _group(name: str, count: int, *, wide: bool | None = None) -> cards.HelpGroup:
    return cards.HelpGroup(
        name=name,
        desc="",
        items=[cards.HelpItem(f"{name}{index}", "说明") for index in range(count)],
        wide=wide,
    )


def test_help_card_marks_long_groups_as_wide() -> None:
    card = _card()
    card.groups[0].items = [
        cards.HelpItem(f"棱镜指令{index}", "说明") for index in range(cards.HELP_WIDE_THRESHOLD + 1)
    ]
    assert "help-cat wide" in cards.build_help_card_html(card, _ctx())


def test_wide_flag_on_the_group_wins_over_the_item_count() -> None:
    # 显式声明过的分类不参与自动配对，调用方说什么就是什么。
    long_but_narrow = _group("长但要窄", cards.HELP_WIDE_THRESHOLD + 3, wide=False)
    short_but_wide = _group("短但要宽", 1, wide=True)
    assert cards._help_widths([long_but_narrow, short_but_wide]) == [False, True]


def test_adjacent_narrow_groups_pair_up_into_one_row() -> None:
    widths = cards._help_widths([_group("甲", 2), _group("乙", 3)])
    assert widths == [False, False]


def test_a_lonely_narrow_group_is_promoted_to_full_width() -> None:
    """半行分类落单时右边会空掉半张卡的高度，所以升级成整行两列。"""
    widths = cards._help_widths(
        [
            _group("窄", 2),
            _group("宽", cards.HELP_WIDE_THRESHOLD + 1),
            _group("又窄", 1),
        ],
    )
    assert widths == [True, True, True]


def test_narrow_groups_pair_two_by_two_and_the_odd_one_out_goes_wide() -> None:
    widths = cards._help_widths([_group(f"第{index}", 2) for index in range(5)])
    assert widths == [False, False, False, False, True]


def test_help_grid_has_no_stranded_half_row() -> None:
    """回归：v1.1.5 之前真实指令集会排出三处半行空白，卡片高度虚增近三分之一。"""
    card = _card()
    card.groups = [
        _group("棱镜系列", 5),
        _group("画像系列", 8),
        _group("查询", 6),
        _group("隐私", 2),
        _group("管理员", 6),
    ]
    html = cards.build_help_card_html(card, _ctx())
    assert html.count("help-cat wide") == 5
    assert 'class="help-cat"' not in html


# ---------------------------------------------------------------------------
# 棱镜帮助 这条指令本身
# ---------------------------------------------------------------------------

main = pytest.importorskip("astrbot_plugin_persona_prism.main", reason="需要已安装的 AstrBot 运行时")


class FakeLibrary:
    def __init__(self, specs: list[PromptSpec]) -> None:
        self._specs = specs

    def all_specs(self) -> list[PromptSpec]:
        return list(self._specs)


class FakeRenderer:
    def __init__(self, *, image: str = "", boom: bool = False) -> None:
        self.image = image
        self.boom = boom
        self.calls: list[Any] = []

    def backends(self) -> list[str]:
        return ["AstrBot t2i", "纯文本"]

    async def render_help(self, card, ctx, text, record_key: str = ""):
        self.calls.append((card, ctx, record_key))
        if self.boom:
            raise RuntimeError("t2i 挂了")
        return SimpleNamespace(backend="t2i", image_path=self.image, text=text)


def _spec(key: str, command: str, label: str, *, builtin: bool = True) -> PromptSpec:
    return PromptSpec(
        key=key,
        command=command,
        label=label,
        prompt="随便",
        structured=True,
        builtin=builtin,
    )


def _help_star(renderer: FakeRenderer, **overrides: Any) -> SimpleNamespace:
    """一个刚够跑 cmd_help 的假 Star。配置用真的 PrismConfig，好把接口错用测出来。"""
    raw: dict[str, Any] = {group: dict(items) for group, items in DEFAULTS.items()}
    for path, value in overrides.items():
        group, _, key = path.partition(".")
        raw[group][key] = value
    specs = [
        _spec("overall", "棱镜画像", "综合人格画像"),
        _spec("legacy_portrait", "画像", "上游同款长文画像"),
        _spec("mine", "我的标签", "自定义模板", builtin=False),
    ]
    star = SimpleNamespace(
        config=PrismConfig(raw),
        library=FakeLibrary(specs),
        renderer=renderer,
        astore=SimpleNamespace(group_theme=_group_theme),
        _scope=lambda event: ("aiocqhttp", "10086"),
        _last_backend="",
    )
    return star


async def _group_theme(platform: str, group_id: str) -> str:
    return "neon"


class FakeEvent:
    def plain_result(self, text: str) -> tuple[str, str]:
        return ("text", text)

    def image_result(self, path: str) -> tuple[str, str]:
        return ("image", path)


def _run_help(star: Any) -> list[tuple[str, str]]:
    async def drive() -> list[tuple[str, str]]:
        return [item async for item in main.PersonaPrismStar.cmd_help(star, FakeEvent())]

    return asyncio.run(drive())


def test_cmd_help_renders_a_card() -> None:
    renderer = FakeRenderer(image="/tmp/help.png")
    star = _help_star(renderer)
    results = _run_help(star)
    assert results == [("image", "/tmp/help.png")]
    #: 卡片主题跟着本群设置走，而不是配置里的默认值。
    card, ctx, record_key = renderer.calls[0]
    assert ctx.theme == "neon"
    assert record_key == "help"
    names = [group.name for group in card.groups]
    assert names[0] == "棱镜系列"
    assert "画像系列" in names
    assert "自定义模板" in names
    assert star._last_backend == "t2i"


def test_cmd_help_reads_config_through_the_typed_helpers() -> None:
    """回归 v1.1.4 的线上崩溃：PrismConfig.get() 只收一个参数，不能传默认值。"""
    renderer = FakeRenderer(image="/tmp/help.png")
    star = _help_star(renderer)
    calls: list[tuple[Any, ...]] = []
    real_get = star.config.get

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real_get(*args, **kwargs)

    star.config = SimpleNamespace(
        get=spy,
        bool_of=star.config.bool_of,
        int_of=star.config.int_of,
        str_of=star.config.str_of,
        list_of=star.config.list_of,
    )
    _run_help(star)
    assert all(len(args) == 1 for args in calls), calls


def test_cmd_help_hides_legacy_group_when_disabled() -> None:
    renderer = FakeRenderer(image="/tmp/help.png")
    star = _help_star(renderer, **{"compat.legacy_commands": False})
    _run_help(star)
    card, _ctx, _key = renderer.calls[0]
    assert "画像系列" not in [group.name for group in card.groups]


def test_cmd_help_falls_back_to_text_when_card_is_off() -> None:
    renderer = FakeRenderer(image="/tmp/help.png")
    star = _help_star(renderer, **{"behavior.help_card": False})
    results = _run_help(star)
    assert renderer.calls == []
    kind, payload = results[0]
    assert kind == "text"
    assert "棱镜画像" in payload
    assert "棱镜重扫" in payload


def test_cmd_help_survives_a_broken_renderer() -> None:
    star = _help_star(FakeRenderer(boom=True))
    kind, payload = _run_help(star)[0]
    assert kind == "text"
    assert "指令一览" in payload


def test_cmd_help_falls_back_to_text_when_no_image_comes_back() -> None:
    star = _help_star(FakeRenderer(image=""))
    kind, payload = _run_help(star)[0]
    assert kind == "text"
    assert "棱镜画像" in payload
