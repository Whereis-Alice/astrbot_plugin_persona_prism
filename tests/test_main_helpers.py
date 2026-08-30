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
    assert len(main.OWN_COMMANDS) == 32
    assert len(set(main.OWN_COMMANDS)) == 32


def test_builtin_commands_are_a_subset_of_own_commands():
    # 6 条棱镜系列 + 5 条兼容上游的画像系列
    assert len(main.BUILTIN_COMMANDS) == 11
    assert set(main.BUILTIN_COMMANDS) <= set(main.OWN_COMMANDS)


def test_builtin_commands_match_the_prompt_library():
    library = PromptLibrary()
    commands = {spec.command for spec in library.all_specs() if spec.builtin}
    assert commands == set(main.BUILTIN_COMMANDS)


#: 「画像」系列（兼容 astrbot_plugin_portrayal）与「今日人设」（兼容
#: astrbot_plugin_love_formula）是特意沿用的上游指令名，不带棱镜前缀。
COMPAT_COMMANDS = frozenset(main.LEGACY_KEYS) | {"查看画像", "切换人格", "恢复人格", "今日人设"}

#: 恋爱诊断这一支刻意不带棱镜前缀：它是独立玩法，名字要一眼看懂。
LOVE_COMMANDS = frozenset({"恋爱诊断", "恋爱诊断榜"})


def test_every_command_shares_the_prism_prefix():
    # 棱镜系列统一前缀是「不和其他插件抢指令」的关键；只有兼容指令例外。
    for command in main.OWN_COMMANDS:
        assert command.startswith("棱镜") or command in COMPAT_COMMANDS or command in LOVE_COMMANDS


def test_compat_commands_are_all_registered_as_own():
    assert set(main.OWN_COMMANDS) >= COMPAT_COMMANDS


def test_legacy_keys_map_to_builtin_prompts():
    library = PromptLibrary()
    for command, key in main.LEGACY_KEYS.items():
        spec = library.get(key)
        assert spec is not None, key
        assert spec.command == command
        assert spec.builtin is True


def test_clone_kinds_cover_both_series():
    assert main.CLONE_KINDS == ("legacy_clone", "clone")


def test_shared_preference_keys_are_namespaced():
    for key in (main._SP_BOT_BACKUP, main._SP_PERSONA_BACKUP, main._PERSONA_ID_PREFIX):
        assert key.startswith("persona_prism")


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


# ---------------------------------------------------------------------------
# 恋爱诊断的天数参数与统计窗口
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("恋爱诊断", 1),
        ("恋爱诊断 7", 7),
        ("恋爱诊断 7天", 7),
        ("恋爱诊断 30 日", 30),
        ("恋爱诊断 @某人 3", 3),
        ("恋爱诊断 0", 1),
        ("恋爱诊断 99", 30),
        ("恋爱诊断 abc", 1),
    ],
)
def test_parse_days_reads_the_trailing_number(text, expect):
    assert main._parse_days(text, "恋爱诊断") == expect


def test_parse_days_ignores_qq_ids():
    # 长数字是 QQ 号，不能被当成天数；两者可以同时出现。
    assert main._parse_days("恋爱诊断 123456789", "恋爱诊断") == 1
    assert main._parse_days("恋爱诊断 123456789 7", "恋爱诊断") == 7
    assert main._DIGITS_RE.search("恋爱诊断 123456789 7").group(1) == "123456789"


def test_parse_days_respects_custom_default_and_cap():
    assert main._parse_days("恋爱诊断", "恋爱诊断", default=7) == 7
    assert main._parse_days("恋爱诊断 20", "恋爱诊断", cap=14) == 14


class _WindowStub:
    """只为调 _love_window 拼的最小壳子：它只用到 config.int_of。"""

    _love_day = main.PersonaPrismStar._love_day
    _love_window = main.PersonaPrismStar._love_window

    def __init__(self, start_hour: int = 4) -> None:
        self.config = type("_C", (), {"int_of": staticmethod(lambda key: start_hour)})()


def test_love_window_one_day_equals_love_day():
    stub = _WindowStub()
    day, start, end = stub._love_day()
    assert stub._love_window(1) == (day, start, end, 1)


def test_love_window_extends_backwards_only():
    stub = _WindowStub()
    day, start, end = stub._love_day()
    assert stub._love_window(7) == (day, start - 6 * 86400, end, 7)


@pytest.mark.parametrize(("days", "span"), [(0, 1), (1, 1), (30, 30), (99, 30), (-5, 1)])
def test_love_window_clamps_span(days, span):
    stub = _WindowStub()
    assert stub._love_window(days)[3] == span


def test_love_day_start_hour_shifts_the_boundary():
    early = _WindowStub(0)._love_day()
    late = _WindowStub(4)._love_day()
    assert late[1] - early[1] in {4 * 3600, 4 * 3600 - 86400}
    assert late[2] - late[1] == 86400


# ---------------------------------------------------------------------------
# 棱镜诊断结尾的「文案口吻」自检行
# ---------------------------------------------------------------------------


class _ProbeConfig:
    def __init__(self, enabled: bool, persona_id: str = ""):
        self._enabled = enabled
        self._persona_id = persona_id

    def bool_of(self, path):
        assert path == "persona.use_astrbot_persona"
        return self._enabled

    def str_of(self, path):
        assert path == "persona.persona_id"
        return self._persona_id


class _ProbeEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:900"


def _probe(enabled, persona_manager=None, persona_id=""):
    import asyncio
    from types import SimpleNamespace

    fake_self = SimpleNamespace(
        config=_ProbeConfig(enabled, persona_id),
        context=SimpleNamespace(persona_manager=persona_manager),
    )
    return asyncio.run(main.PersonaPrismStar._persona_probe(fake_self, _ProbeEvent()))


def test_persona_probe_says_neutral_when_switch_is_off():
    lines = _probe(False)
    assert any("\u4e2d\u7acb\u53d9\u8ff0" in line for line in lines)


def test_persona_probe_flags_enabled_but_unresolved():
    # 开关开了却没解析到人格 —— 这一行的存在就是为了当场区分这种情况。
    lines = _probe(True, persona_manager=None)
    joined = "".join(lines)
    assert "\u5df2\u5f00\u542f" in joined
    assert "\u6ca1\u89e3\u6790\u5230" in joined


def test_persona_probe_names_the_persona_in_use():
    class Manager:
        def get_persona_v3_by_id(self, persona_id):
            return {"name": "\u7231\u4e43", "prompt": "\u4fee\u98ce\u8f66\u7684\u5c11\u5973"}

    lines = _probe(True, persona_manager=Manager(), persona_id="aino")
    joined = "".join(lines)
    assert "\u7231\u4e43" in joined
    assert "\u914d\u7f6e\u6307\u5b9a" in joined


# ---------------------------------------------------------------------------
# 插件自身文案识别（机器人发言当正常人入库后的唯一防线）
# ---------------------------------------------------------------------------


def _echo_shell():
    from types import SimpleNamespace

    shell = SimpleNamespace(_own_echo={})
    for name in ("_remember_echo", "_is_plugin_echo"):
        setattr(shell, name, getattr(main.PersonaPrismStar, name).__get__(shell))
    shell._echo_digest = main.PersonaPrismStar._echo_digest
    return shell


def test_plain_chat_from_the_bot_is_kept():
    shell = _echo_shell()
    assert shell._is_plugin_echo("我也觉得这番好看") is False


def test_remembered_output_is_recognised_even_after_whitespace_changes():
    shell = _echo_shell()
    shell._remember_echo("正在给 小明 做今日恋爱诊断…")
    assert shell._is_plugin_echo("正在给  小明  做今日恋爱诊断…")


def test_fixed_notices_are_recognised_without_memory():
    # 进程重启后回溯到几天前的群历史，摘要表是空的，靠固定文案特征兑上。
    shell = _echo_shell()
    assert shell._is_plugin_echo("小明 的有效发言只有 6 条，还不够生成画像·综合。")


def test_short_output_is_not_remembered():
    shell = _echo_shell()
    shell._remember_echo("好")
    assert shell._own_echo == {}


def test_echo_memory_is_bounded():
    shell = _echo_shell()
    for index in range(main._ECHO_MEMORY + 30):
        shell._remember_echo(f"提示文案 {index}")
    assert len(shell._own_echo) <= main._ECHO_MEMORY
