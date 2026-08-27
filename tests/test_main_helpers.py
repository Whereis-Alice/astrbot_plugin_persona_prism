"""main.py 里的纯函数与常量表。

导入 main 需要真实的 astrbot 运行时（Star / filter / quart 都在里面），
所以没装框架的环境直接跳过，而不是让整份测试红掉。
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("astrbot", reason="需要已安装的 AstrBot 运行时")

from astrbot_plugin_persona_prism import main
from astrbot_plugin_persona_prism.prism.prompts import PromptLibrary

# ---------------------------------------------------------------------------
# 常量表
# ---------------------------------------------------------------------------


def test_plugin_identity_is_renamed_everywhere():
    assert main.PLUGIN_ID == "astrbot_plugin_persona_prism"
    assert main.PLUGIN_VERSION.startswith("v")
    assert "portrayal" not in main.PLUGIN_ID


def test_own_commands_has_no_duplicates():
    assert len(main.OWN_COMMANDS) == 17
    assert len(set(main.OWN_COMMANDS)) == 17


def test_builtin_commands_are_a_subset_of_own_commands():
    assert len(main.BUILTIN_COMMANDS) == 5
    assert set(main.BUILTIN_COMMANDS) <= set(main.OWN_COMMANDS)


def test_builtin_commands_match_the_prompt_library():
    library = PromptLibrary()
    commands = {spec.command for spec in library.all_specs() if spec.builtin}
    assert commands == set(main.BUILTIN_COMMANDS)


def test_every_command_shares_the_prism_prefix():
    # 统一前缀是「不和其他插件抢指令」的关键，别人装了上游插件也不冲突。
    for command in main.OWN_COMMANDS:
        assert command.startswith("棱镜")


def test_avatar_template_is_a_qq_endpoint():
    url = main._AVATAR_TEMPLATE.format(uid="10001")
    assert url.startswith("https://")
    assert "10001" in url
    assert "{" not in url


def test_mime_table_covers_persisted_card_suffixes():
    assert main._MIME_BY_SUFFIX[".jpg"] == "image/jpeg"
    assert set(main._MIME_BY_SUFFIX) == {".jpg", ".jpeg", ".png", ".webp"}


def test_card_preview_limit_is_sane():
    assert main._CARD_PREVIEW_LIMIT == 4 * 1024 * 1024


def test_maintenance_interval_is_hourly():
    assert main._MAINTENANCE_INTERVAL == 3600


# ---------------------------------------------------------------------------
# QQ 号识别
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["10001", "123456789", "999999999999"])
def test_qq_re_accepts_plausible_ids(text):
    assert main._QQ_RE.match(text)


@pytest.mark.parametrize("text", ["", "123", "1234", "1234567890123", "10001a", "a10001"])
def test_qq_re_rejects_the_rest(text):
    assert main._QQ_RE.match(text) is None


def test_digits_re_extracts_standalone_ids_only():
    assert main._DIGITS_RE.findall("给 10001 画个像") == ["10001"]
    # 13 位数字整体不是合法 QQ 号，不应该被切出一段来当目标。
    assert main._DIGITS_RE.findall("1234567890123") == []
    assert main._DIGITS_RE.findall("2024 年") == []


# ---------------------------------------------------------------------------
# _as_int
# ---------------------------------------------------------------------------


def test_as_int_passes_through_integers():
    assert main._as_int(5) == 5
    assert main._as_int(0) == 0
    assert main._as_int(-42) == -42


def test_as_int_parses_strings_including_negatives():
    assert main._as_int("5") == 5
    assert main._as_int("  -1234  ") == -1234


def test_as_int_rejects_bools():
    # bool 是 int 的子类，不拦住的话 True 会变成 message_seq=1。
    assert main._as_int(True) is None
    assert main._as_int(False) is None


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "12a", [], {}, 1.5])
def test_as_int_returns_none_for_garbage(raw):
    assert main._as_int(raw) is None


# ---------------------------------------------------------------------------
# _strip_command
# ---------------------------------------------------------------------------


def test_strip_command_removes_prefix_and_command():
    assert main._strip_command("/棱镜画像 @小明", "棱镜画像") == "@小明"
    assert main._strip_command("棱镜主题 ink", "棱镜主题") == "ink"
    assert main._strip_command("！棱镜主题 ink", "棱镜主题") == "ink"
    assert main._strip_command("。棱镜统计", "棱镜统计") == ""


def test_strip_command_tolerates_missing_command_and_empty_text():
    assert main._strip_command("", "棱镜画像") == ""
    assert main._strip_command(None, "棱镜画像") == ""
    assert main._strip_command("/棱镜画像 abc", "") == "棱镜画像 abc"


def test_strip_command_keeps_inner_text_intact():
    assert main._strip_command("棱镜画像 10001 详细一点", "棱镜画像") == "10001 详细一点"


def test_strip_command_does_not_cut_other_commands():
    assert main._strip_command("棱镜锐评 x", "棱镜画像") == "棱镜锐评 x"


# ---------------------------------------------------------------------------
# _fmt_ts
# ---------------------------------------------------------------------------


def test_fmt_ts_formats_a_real_timestamp():
    now = int(time.time())
    assert main._fmt_ts(now) == time.strftime("%Y-%m-%d", time.localtime(now))


def test_fmt_ts_accepts_custom_pattern():
    now = int(time.time())
    out = main._fmt_ts(now, "%Y-%m-%d %H:%M")
    assert len(out) == 16


@pytest.mark.parametrize("raw", [None, "", 0, -1, "abc", True, False])
def test_fmt_ts_returns_empty_for_unusable_input(raw):
    assert main._fmt_ts(raw) == ""


# ---------------------------------------------------------------------------
# 自家指令识别（语料采集要把这些消息剔掉）
# ---------------------------------------------------------------------------


def test_is_own_command_matches_with_and_without_prefix():
    check = main.PersonaPrismStar._is_own_command
    assert check("棱镜画像 @小明") is True
    assert check("/棱镜统计") is True
    assert check("！棱镜帮助") is True
    assert check("   棱镜主题 ink") is True


def test_is_own_command_ignores_normal_chat():
    check = main.PersonaPrismStar._is_own_command
    assert check("今天天气不错") is False
    assert check("") is False
    assert check(None) is False
    assert check("我觉得棱镜画像挺好玩") is False


# ---------------------------------------------------------------------------
# WebUI 错误封装
# ---------------------------------------------------------------------------


def test_dashboard_error_is_a_staticmethod():
    # 忘了 @staticmethod 的话，self 会被当成 message 传进去。
    assert isinstance(
        main.PersonaPrismStar.__dict__["_dashboard_error"],
        staticmethod,
    )
