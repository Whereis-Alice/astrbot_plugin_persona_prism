"""采集诊断（prism/scanning.py）。

这一层的价值全在文案上：用户装完插件被告知"发言太少"时，必须能从回复里看出
瓶颈到底是群里没话、协议端不支持拉历史，还是配置把回溯关了。所以测试重点是
"每个分支都说对了话"，而不只是字段算得对。
"""

from __future__ import annotations

import time

from astrbot_plugin_persona_prism.prism import scanning


def _report(**kwargs) -> scanning.ScanReport:
    base = {
        "platform": "aiocqhttp",
        "is_group": True,
        "supported": True,
        "planned_rounds": 12,
        "page_size": 200,
    }
    base.update(kwargs)
    return scanning.ScanReport(**base)


# ---------------------------------------------------------------------------
# 基础判定
# ---------------------------------------------------------------------------


def test_supports_backfill_only_for_group_onebot():
    assert scanning.supports_backfill("aiocqhttp", "123") is True
    assert scanning.supports_backfill("aiocqhttp", "") is False
    assert scanning.supports_backfill("telegram", "123") is False


def test_brief_error_is_single_line_and_bounded():
    text = scanning.brief_error(RuntimeError("boom\n" + "x" * 500))
    assert "\n" not in text
    assert len(text) <= scanning.ERROR_BRIEF_MAX


def test_brief_error_falls_back_to_exception_type():
    assert scanning.brief_error(RuntimeError()) == "RuntimeError"


def test_blocked_and_fetched_are_mutually_exclusive():
    blocked = _report(attempted=True, pages=3, error="timeout")
    assert blocked.blocked is True
    assert blocked.fetched is False
    good = _report(attempted=True, pages=3)
    assert good.blocked is False
    assert good.fetched is True
    #: 没发起回溯时两者都不成立，进度提示应当保持安静。
    idle = _report(attempted=False)
    assert idle.blocked is False
    assert idle.fetched is False


def test_to_dict_covers_every_field():
    payload = _report().to_dict()
    assert set(payload) == set(scanning.ScanReport.__slots__)


# ---------------------------------------------------------------------------
# 开工提示
# ---------------------------------------------------------------------------


def test_intro_line_is_one_short_sentence():
    """群里只说在做什么。轮数、每轮条数这些参数改由 progress_log 写进日志。"""
    line = scanning.intro_line(_report(), target_name="狐狸", label="人格棱镜")
    assert line == "正在为 狐狸 生成人格棱镜…"
    assert "轮" not in line
    assert "\n" not in line


def test_intro_line_does_not_leak_scan_details():
    line = scanning.intro_line(
        _report(supported=False),
        target_name="狐狸",
        label="人格棱镜",
    )
    assert line == "正在为 狐狸 生成人格棱镜…"


def test_intro_line_stays_generic_when_rounds_disabled():
    line = scanning.intro_line(_report(planned_rounds=0), target_name="", label="人格棱镜")
    assert "TA" in line


# ---------------------------------------------------------------------------
# 带数字的进度
# ---------------------------------------------------------------------------


def test_progress_line_keeps_only_the_sample_count():
    """群里那句进度只留一个数字，翻页细节全部下沉到日志。"""
    line = scanning.progress_line(
        _report(attempted=True, pages=8, scanned=1600, added=430),
        target_name="狐狸",
        label="人格棱镜",
        sampled=300,
    )
    assert line == "已取到 300 条发言，正在分析…"
    assert "8 页" not in line
    assert "1600" not in line


def test_progress_line_is_empty_without_backfill():
    assert (
        scanning.progress_line(
            _report(attempted=False),
            target_name="狐狸",
            label="人格棱镜",
            sampled=300,
        )
        == ""
    )


def test_progress_line_explains_protocol_rejection():
    line = scanning.progress_line(
        _report(attempted=True, error="unsupported action"),
        target_name="狐狸",
        label="人格棱镜",
        sampled=42,
    )
    assert "拒绝" in line
    assert "42 条" in line
    #: 协议端的原始报错属于排查细节，写日志就好，别贴进群里。
    assert "unsupported action" not in line


def test_progress_log_spells_out_every_detail():
    """详细版给后台日志：页数、条数、翻页方式、复查/重挖等都要在。"""
    detail = scanning.progress_log(
        _report(
            attempted=True,
            pages=8,
            scanned=1600,
            added=430,
            cursor_field="id_first",
            cursor_switched=True,
            exhausted_recheck="shallow",
            restarted=True,
            exhausted=True,
        ),
        target_name="狐狸",
        label="人格棱镜",
        sampled=300,
    )
    assert "狐狸" in detail
    assert "8 页" in detail
    assert "1600 条" in detail
    assert "430 条" in detail
    assert "300 条" in detail
    assert "切换" in detail
    assert "复查" in detail
    assert "重挖" in detail
    assert "\n" not in detail


def test_progress_log_covers_topup_and_blocked():
    topup = scanning.progress_log(
        _report(attempted=True, pages=1, scanned=200, added=3, topup_only=True, exhausted=True),
        target_name="狐狸",
        label="人格棱镜",
        sampled=90,
    )
    assert "补拉最新一页" in topup
    assert "最早一条" in topup
    blocked = scanning.progress_log(
        _report(attempted=True, error="unsupported action"),
        target_name="",
        label="人格棱镜",
        sampled=12,
    )
    assert "unsupported action" in blocked
    assert "TA" in blocked


def test_progress_log_notes_when_backfill_never_ran():
    detail = scanning.progress_log(
        _report(attempted=False, local_before=300),
        target_name="狐狸",
        label="人格棱镜",
        sampled=300,
    )
    assert "未触发回溯" in detail
    assert "300 条" in detail


# ---------------------------------------------------------------------------
# 样本不足时的诊断
# ---------------------------------------------------------------------------


def test_shortfall_reply_always_has_counts_and_advice():
    text = scanning.shortfall_reply(
        _report(attempted=True, pages=8, scanned=1600, added=430),
        target_name="狐狸",
        label="人格棱镜",
        sampled=6,
        min_messages=20,
    )
    assert "6 条" in text
    assert "20 条" in text
    assert "采集诊断：" in text
    assert "棱镜缓存" in text
    #: 回溯确实跑过就要把数字摊出来，否则用户还是不知道有没有轮询成功。
    assert "1600" in text


def test_diagnose_private_chat():
    items = scanning.diagnose(_report(is_group=False, supported=False))
    assert any("私聊" in item for item in items)


def test_diagnose_unsupported_platform_names_it():
    items = scanning.diagnose(_report(platform="telegram", supported=False))
    assert any("telegram" in item for item in items)


def test_diagnose_rounds_disabled():
    items = scanning.diagnose(_report(planned_rounds=0))
    assert any("历史回溯轮数" in item for item in items)


def test_diagnose_skipped_because_local_corpus_is_enough():
    items = scanning.diagnose(_report(attempted=False, local_before=520))
    assert any("520" in item for item in items)


def test_diagnose_api_error_gives_reasons():
    items = scanning.diagnose(_report(attempted=True, error="retcode 1200"))
    assert any("retcode 1200" in item for item in items)
    assert any("get_group_msg_history" in item for item in items)


def test_diagnose_empty_history_page():
    items = scanning.diagnose(_report(attempted=True, pages=0, exhausted=True))
    assert any("空历史" in item for item in items)


def test_diagnose_exhausted_history():
    items = scanning.diagnose(_report(attempted=True, pages=5, scanned=900, exhausted=True))
    assert any("最早一条" in item for item in items)


def test_diagnose_warns_when_passive_capture_is_off():
    items = scanning.diagnose(_report(attempted=True, pages=5, passive_capture=False))
    assert any("被动采集" in item for item in items)


def test_diagnose_never_returns_empty():
    assert scanning.diagnose(_report(attempted=True, pages=3, scanned=600))


# ---------------------------------------------------------------------------
# 「棱镜缓存」里的回溯状态
# ---------------------------------------------------------------------------


def test_describe_scan_state_reports_recent_scan():
    now = time.time()
    lines = scanning.describe_scan_state(
        {"exhausted": False, "last_scan": int(now - 3700), "oldest_seq": "88123"},
        now=now,
    )
    blob = "\n".join(lines)
    assert "仍可继续回溯" in blob
    assert "1 小时前" in blob
    assert "88123" in blob


def test_describe_scan_state_when_never_scanned():
    blob = "\n".join(scanning.describe_scan_state({}))
    assert "还没有成功回溯过" in blob
    #: 没有断点时不要凭空造一行 message_seq。
    assert "message_seq" not in blob


def test_describe_scan_state_tolerates_dirty_values():
    blob = "\n".join(scanning.describe_scan_state({"exhausted": 1, "last_scan": "oops"}))
    assert "已挖到头" in blob
    assert "还没有成功回溯过" in blob


def test_human_since_scales():
    assert scanning.human_since(5) == "不到 1 分钟"
    assert scanning.human_since(120) == "2 分钟"
    assert scanning.human_since(7200) == "2 小时"
    assert scanning.human_since(200000) == "2 天"


# ---------------------------------------------------------------------------
# 「已挖到头」标记的复查（v1.1.5）
# ---------------------------------------------------------------------------


def test_should_recheck_returns_empty_when_not_exhausted():
    assert scanning.should_recheck_exhausted({"exhausted": False, "depth_pages": 1}) == ""


def test_should_recheck_flags_shallow_marks():
    now = 1_700_000_000.0
    state = {"exhausted": True, "depth_pages": 1, "last_scan": int(now - 600)}
    assert scanning.should_recheck_exhausted(state, now=now) == "shallow"


def test_should_recheck_waits_out_the_shallow_window():
    now = 1_700_000_000.0
    state = {"exhausted": True, "depth_pages": 1, "last_scan": int(now - 10)}
    assert scanning.should_recheck_exhausted(state, now=now) == ""


def test_should_recheck_trusts_deep_marks_for_longer():
    now = 1_700_000_000.0
    fresh = {"exhausted": True, "depth_pages": 9, "last_scan": int(now - 3600)}
    assert scanning.should_recheck_exhausted(fresh, now=now) == ""
    stale = {"exhausted": True, "depth_pages": 9, "last_scan": int(now - 86400)}
    assert scanning.should_recheck_exhausted(stale, now=now) == "stale"


def test_should_recheck_treats_missing_timestamp_as_ancient():
    #: 老版本写下的记录没有 last_scan，不能因此永远不复查。
    assert scanning.should_recheck_exhausted({"exhausted": True}) == "shallow"
    assert scanning.should_recheck_exhausted({"exhausted": True, "depth_pages": 9}) == "stale"


def test_should_recheck_survives_garbage_values():
    state = {"exhausted": True, "depth_pages": "毁灭吧", "last_scan": None}
    assert scanning.should_recheck_exhausted(state) == "shallow"


def test_recheck_reasons_cover_every_return_value():
    assert set(scanning.RECHECK_REASONS) == {"shallow", "stale"}


def test_diagnose_explains_recheck_and_restart():
    items = scanning.diagnose(
        _report(attempted=True, pages=2, scanned=400, added=100, restarted=True, exhausted_recheck="shallow"),
    )
    joined = " / ".join(items)
    assert "断点" in joined
    assert "复查" in joined or "验了一遍" in joined


def test_describe_scan_state_warns_about_shallow_exhausted_mark():
    text = "\n".join(
        scanning.describe_scan_state(
            {"exhausted": True, "depth_pages": 1, "last_scan": int(time.time()), "oldest_seq": "123"},
        ),
    )
    assert "断点很浅" in text
    #: 断点够深就不该再唱衰，免得每个群都挂一句免责声明。
    deep = "\n".join(
        scanning.describe_scan_state(
            {"exhausted": True, "depth_pages": 9, "last_scan": int(time.time()), "oldest_seq": "123"},
        ),
    )
    assert "断点很浅" not in deep
