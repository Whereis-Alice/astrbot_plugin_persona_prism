"""前后端契约测试。

WebUI 是纯静态页 + register_web_api，两边对不上只会在浏览器控制台报错，
单测里抓不到运行时问题，所以这里用文本比对守住几条硬契约：

* 前端调用的每个接口后端都注册过；
* 前端引用的每个图标 symbol 都在 index.html 里定义过；
* 四套界面主题在 JS 选项与 CSS 变量里同时存在；
* 页面不往 innerHTML 里拼后端字符串（画像正文来自 LLM，必须当纯文本）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PAGE_DIR = PLUGIN_DIR / "pages" / "persona-prism"

MAIN_SRC = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
APP_JS = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (PAGE_DIR / "style.css").read_text(encoding="utf-8")
DASHBOARD_SRC = (PLUGIN_DIR / "prism" / "dashboard.py").read_text(encoding="utf-8")

_ROUTE_RE = re.compile(r'\(\s*"(dashboard/[a-z-]+)"\s*,\s*self\._api_')
_CALL_RE = re.compile(r'api(?:Get|Post)\(\s*"([^"]+)"')
_SYMBOL_RE = re.compile(r'<symbol[^>]*id="(i-[a-z0-9-]+)"')
_ICON_RE = re.compile(r'"(i-[a-z0-9-]+)"')
_HREF_RE = re.compile(r'href="#(i-[a-z0-9-]+)"')
_THEME_RE = re.compile(r'\[data-theme="([a-z]+)"\]')
_BACKEND_ICON_RE = re.compile(r'"icon":\s*"([a-z0-9-]+)"')

WEBUI_THEMES = ("nocturne", "daylight", "neon", "ink")


def _routes() -> set[str]:
    return set(_ROUTE_RE.findall(MAIN_SRC))


# ---------------------------------------------------------------------------
# 文件齐备
# ---------------------------------------------------------------------------


def test_page_bundle_is_complete():
    for name in ("_page.json", "index.html", "style.css", "app.js"):
        assert (PAGE_DIR / name).is_file(), name


def test_page_manifest_is_valid_json_with_i18n_keys():
    data = json.loads((PAGE_DIR / "_page.json").read_text(encoding="utf-8"))
    assert data["title"]["i18n_key"].startswith("pages.persona-prism.")
    assert data["description"]["i18n_key"].startswith("pages.persona-prism.")


def test_i18n_files_define_every_page_key():
    data = json.loads((PAGE_DIR / "_page.json").read_text(encoding="utf-8"))
    keys = [data["title"]["i18n_key"], data["description"]["i18n_key"]]
    for locale in ("zh-CN", "en-US"):
        path = PLUGIN_DIR / ".astrbot-plugin" / "i18n" / f"{locale}.json"
        assert path.is_file(), locale
        blob = json.loads(path.read_text(encoding="utf-8"))
        for key in keys:
            node = blob
            for part in key.split("."):
                assert isinstance(node, dict) and part in node, f"{locale} 缺少 {key}"
                node = node[part]
            assert isinstance(node, str) and node


def test_index_html_only_references_local_assets():
    refs = re.findall(r'(?:src|href)="([^"#]+)"', INDEX_HTML)
    assert refs, "页面应该至少引用 style.css 与 app.js"
    for ref in refs:
        assert ref.startswith("./"), ref
        assert (PAGE_DIR / ref[2:]).is_file(), ref


# ---------------------------------------------------------------------------
# 接口契约
# ---------------------------------------------------------------------------


def test_backend_registers_the_expected_routes():
    assert _routes() == {
        "dashboard/overview",
        "dashboard/records",
        "dashboard/groups",
        "dashboard/record",
        "dashboard/record-card",
        "dashboard/record-delete",
        "dashboard/records-purge",
        "dashboard/settings",
        "dashboard/prompts",
        "dashboard/optout",
        "dashboard/runs",
    }


def test_every_frontend_call_hits_a_registered_route():
    called = set(_CALL_RE.findall(APP_JS))
    assert called, "前端至少要调用一个接口"
    missing = sorted(called - _routes())
    assert missing == [], f"前端调用了未注册的接口：{missing}"


def test_frontend_calls_are_all_relative_to_the_plugin_namespace():
    for endpoint in _CALL_RE.findall(APP_JS):
        # 必须是相对后缀，桥接会自动拼上 /api/plug/<plugin>/。
        assert not endpoint.startswith("/"), endpoint
        assert endpoint.startswith("dashboard/"), endpoint


def test_routes_are_namespaced_under_the_plugin_id():
    assert 'f"/{PLUGIN_ID}/{suffix}"' in MAIN_SRC


def test_write_endpoints_are_not_called_with_apiget():
    get_calls = set(re.findall(r'apiGet\(\s*"([^"]+)"', APP_JS))
    for endpoint in ("dashboard/record-delete", "dashboard/records-purge", "dashboard/optout"):
        assert endpoint not in get_calls, endpoint


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------


def test_every_icon_used_is_defined():
    defined = set(_SYMBOL_RE.findall(INDEX_HTML))
    assert defined, "index.html 应该内联一份 sprite"
    used = set(_ICON_RE.findall(APP_JS)) | set(_HREF_RE.findall(INDEX_HTML))
    missing = sorted(used - defined)
    assert missing == [], f"缺少 symbol 定义：{missing}"


def test_no_orphan_icons_are_shipped():
    # 部分图标名是后端下发的（设置分组的 icon 字段），前端源码里搜不到，
    # 所以“已使用”集合要把 dashboard.py 声明的那批也算进来。
    defined = set(_SYMBOL_RE.findall(INDEX_HTML))
    used = (
        set(_ICON_RE.findall(APP_JS))
        | set(_HREF_RE.findall(INDEX_HTML))
        | {"i-" + name for name in _BACKEND_ICON_RE.findall(DASHBOARD_SRC)}
    )
    orphans = sorted(defined - used)
    assert orphans == [], f"sprite 里有没人用的 symbol：{orphans}"


def test_settings_group_icons_exist_as_symbols():
    from astrbot_plugin_persona_prism.prism.dashboard import GROUP_TITLES

    defined = set(_SYMBOL_RE.findall(INDEX_HTML))
    for meta in GROUP_TITLES.values():
        assert "i-" + meta["icon"] in defined, meta


# ---------------------------------------------------------------------------
# 界面主题
# ---------------------------------------------------------------------------


def test_css_defines_every_webui_theme():
    themes = set(_THEME_RE.findall(STYLE_CSS))
    assert set(WEBUI_THEMES) <= themes


def test_js_offers_auto_plus_every_theme():
    values = re.findall(r'value:\s*"([a-z]+)",\s*\n\s*label:', APP_JS)
    assert values[0] == "auto"
    assert set(values) == {"auto", *WEBUI_THEMES}


def test_theme_preference_key_is_namespaced():
    assert 'THEME_KEY = "persona-prism-theme"' in APP_JS
    assert "localStorage" in APP_JS


def test_auto_theme_follows_system_preference():
    assert "prefers-color-scheme" in APP_JS or "prefers-color-scheme" in STYLE_CSS


# ---------------------------------------------------------------------------
# 安全
# ---------------------------------------------------------------------------


def test_frontend_never_assigns_innerhtml():
    # 画像正文是模型生成的，一旦拼进 innerHTML 就等于把 XSS 面板送给群友。
    assert re.search(r"\.innerHTML\s*=", APP_JS) is None
    assert re.search(r"\.outerHTML\s*=", APP_JS) is None
    assert "insertAdjacentHTML" not in APP_JS


def test_frontend_has_no_inline_eval():
    assert re.search(r"\beval\s*\(", APP_JS) is None
    assert "new Function" not in APP_JS


def test_index_html_has_no_inline_script_body():
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", INDEX_HTML, re.S)
    assert [chunk for chunk in inline if chunk.strip()] == []


def test_pages_do_not_call_third_party_origins():
    # w3.org 的 SVG/XML 命名空间不是网络请求，只是字符串常量，得先排掉。
    for blob in (APP_JS, INDEX_HTML, STYLE_CSS):
        cleaned = blob.replace("http://www.w3.org/", "")
        assert "http://" not in cleaned
        assert "https://" not in cleaned
        for host in ("cdn.", "unpkg.com", "jsdelivr", "googleapis.com"):
            assert host not in cleaned


# ---------------------------------------------------------------------------
# 去冲突
# ---------------------------------------------------------------------------


def test_no_upstream_identifiers_leak_into_code():
    blobs = {
        "main.py": MAIN_SRC,
        "app.js": APP_JS,
        "index.html": INDEX_HTML,
        "style.css": STYLE_CSS,
    }
    for name in sorted((PLUGIN_DIR / "prism").glob("*.py")):
        blobs[name.name] = name.read_text(encoding="utf-8")
    for name, blob in blobs.items():
        for lineno, line in enumerate(blob.splitlines(), 1):
            if "portrayal" not in line.lower():
                continue
            # 只允许出现在致谢/来源说明里，绝不能是真实标识符。
            assert "NOTICE" in line or "上游" in line, f"{name}:{lineno} {line.strip()}"


def test_metadata_declares_the_new_identity():
    meta = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    assert "astrbot_plugin_persona_prism" in meta
    assert "portrayal" not in meta
    assert "astrbot_version" in meta
