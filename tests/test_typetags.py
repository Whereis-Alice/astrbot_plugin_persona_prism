"""16 型人格徽章：档位归一、代码清洗、三套命名齐全。

徽章上的字直接印在卡片顶部，所以「模型给了脏代码就不显示」比「猜一个」重要 ——
宁可少一枚徽章，也不能印一个错的四字母出去。
"""

from __future__ import annotations

from astrbot_plugin_persona_prism.prism import typetags

# ---------------------------------------------------------------------------
# 档位
# ---------------------------------------------------------------------------


def test_valid_modes_are_exactly_four():
    assert typetags.VALID_MODES == ("off", "mbti", "sbti", "acgti")


def test_normalize_mode_keeps_known_modes():
    for mode in typetags.VALID_MODES:
        assert typetags.normalize_mode(mode) == mode


def test_normalize_mode_accepts_case_and_padding():
    assert typetags.normalize_mode("  ACGTI ") == "acgti"
    assert typetags.normalize_mode("SBTI") == "sbti"


def test_normalize_mode_reads_off_synonyms_as_off():
    for value in ("off", "none", "false", "no", "关闭", "不显示"):
        assert typetags.normalize_mode(value) == "off"


def test_normalize_mode_falls_back_to_mbti_for_garbage():
    # 空值当「没配过」，走默认档而不是关掉；乱填的字符串同理。
    assert typetags.normalize_mode("") == "mbti"
    assert typetags.normalize_mode(None) == "mbti"
    assert typetags.normalize_mode("水瓶座") == "mbti"


# ---------------------------------------------------------------------------
# 代码清洗
# ---------------------------------------------------------------------------


def test_normalize_code_uppercases_and_strips():
    assert typetags.normalize_code(" intp ") == "INTP"
    assert typetags.normalize_code("e-n-t-p") == "ENTP"


def test_normalize_code_rejects_wrong_length():
    assert typetags.normalize_code("INT") == ""
    assert typetags.normalize_code("INTPX") == ""
    assert typetags.normalize_code("") == ""
    assert typetags.normalize_code(None) == ""


def test_normalize_code_rejects_letters_off_axis():
    # 第二位只能是 N/S，模型写成 INTP 以外的组合要能被挡下来。
    assert typetags.normalize_code("IXTP") == ""
    assert typetags.normalize_code("PNTI") == ""


def test_normalize_code_accepts_all_sixteen_combinations():
    codes = {typetags.normalize_code(code) for code in typetags.MBTI_NAMES}
    assert len(codes) == 16
    assert "" not in codes


# ---------------------------------------------------------------------------
# 命名表
# ---------------------------------------------------------------------------


def test_all_three_tables_cover_the_same_sixteen_codes():
    keys = set(typetags.MBTI_NAMES)
    assert len(keys) == 16
    assert set(typetags.SBTI_NAMES) == keys
    assert set(typetags.ACGTI_NAMES) == keys


def test_no_table_has_blank_or_duplicate_names():
    for table in (typetags.MBTI_NAMES, typetags.SBTI_NAMES, typetags.ACGTI_NAMES):
        names = list(table.values())
        assert all(name.strip() for name in names)
        assert len(set(names)) == 16


# ---------------------------------------------------------------------------
# 徽章文字
# ---------------------------------------------------------------------------


def test_label_of_joins_code_and_alias():
    assert typetags.label_of("intp", "mbti") == "INTP · 逻辑学家"
    assert typetags.label_of("ENTP", "sbti") == "ENTP · 小丑"
    assert typetags.label_of("ESFP", "acgti") == "ESFP · 芙宁娜"


def test_label_of_returns_blank_when_mode_is_off():
    assert typetags.label_of("INTP", "off") == ""


def test_label_of_returns_blank_for_bad_code():
    assert typetags.label_of("", "mbti") == ""
    assert typetags.label_of("XXXX", "mbti") == ""


def test_label_of_defaults_to_mbti():
    assert typetags.label_of("ISFP") == "ISFP · 探险家"


def test_label_of_every_code_has_a_separator_in_every_mode():
    for mode in ("mbti", "sbti", "acgti"):
        for code in typetags.MBTI_NAMES:
            label = typetags.label_of(code, mode)
            assert label.startswith(code + " · ")


def test_alias_of_returns_only_the_chinese_half():
    assert typetags.alias_of("INTJ", "mbti") == "建筑师"
    assert typetags.alias_of("INTJ", "sbti") == "拿捏者"
    assert typetags.alias_of("INTJ", "off") == ""
    assert typetags.alias_of("nope", "mbti") == ""
