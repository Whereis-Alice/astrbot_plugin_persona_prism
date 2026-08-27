"""隐私与降噪处理。

上游把手机号、邮箱、地址一起塞进提示词，这里改成：
1. 资料字段走白名单（见 config.profile_fields）；
2. 语料文本在入库前就做 PII 脱敏，数据库里也不留原文。
"""

from __future__ import annotations

import re

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s\u4e00-\u9fff]+", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_IDCARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANKCARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]")
_CUSTOM_FACE_RE = re.compile(r"\[/[^\]]{1,12}\]")
_WHITESPACE_RE = re.compile(r"[ \t\u3000]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: 常见机器人指令前缀。命令类消息对人格判断几乎没有信息量，还容易把
#: 提示词带偏，所以默认过滤。
COMMAND_PREFIXES = ("/", ".", "#", "!", "！", "。", "／", "~", "、", "$", "%")

_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f0ff\U0000fe00-\U0000fe0f]",
)
_KAOMOJI_RE = re.compile(r"[（(][^）)]{0,8}[）)]$")


def redact_pii(text: str) -> str:
    """把手机号 / 邮箱 / 身份证 / 银行卡替换成占位符。"""
    text = _EMAIL_RE.sub("[邮箱]", text)
    text = _IDCARD_RE.sub("[身份证]", text)
    text = _PHONE_RE.sub("[手机号]", text)
    return _BANKCARD_RE.sub("[卡号]", text)


def strip_urls(text: str) -> str:
    return _URL_RE.sub("[链接]", text)


def normalize_text(
    text: str,
    *,
    redact: bool = True,
    drop_urls: bool = True,
) -> str:
    """把一段原始消息文本清洗成可入库的语料。"""
    if not text:
        return ""
    text = _CONTROL_RE.sub(" ", str(text))
    text = _CQ_CODE_RE.sub(" ", text)
    if drop_urls:
        text = strip_urls(text)
    if redact:
        text = redact_pii(text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def is_command_like(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and stripped[0] in COMMAND_PREFIXES


def has_emoji(text: str) -> bool:
    return bool(_EMOJI_RE.search(text)) or bool(_KAOMOJI_RE.search(text.strip()))


def informative(text: str, min_chars: int) -> bool:
    """判断一条发言是否带有信息量。

    只剩表情、纯符号、纯数字或者太短的内容一律丢弃 —— 它们撑不起任何
    人格判断，只会稀释真正有效的语料。
    """
    if not text:
        return False
    body = _EMOJI_RE.sub("", text)
    body = _CUSTOM_FACE_RE.sub("", body)
    body = re.sub(r"[\s\W_]+", "", body, flags=re.UNICODE)
    if len(body) < max(1, min_chars):
        return False
    return not body.isdigit()


def sanitize_for_prompt(text: str, limit: int = 600) -> str:
    """截断并中和可能被当成指令解析的标记。

    群友完全可能故意在群里发 "忽略上面的指令" 之类的注入语句。语料
    在提示词里是"数据"，这里额外把三反引号和角色标记打断。
    """
    text = text.replace("```", "'''")
    text = re.sub(r"(?i)\b(system|assistant|user)\s*:", r"\1：", text)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text
