"""调用 LLM 并把返回值解析成结构化画像。

上游是"把模型返回的整段文字直接当图片文案"，好处是简单，坏处是没法做
维度雷达图、没法做证据区块、也没法在 WebUI 里检索。这里要求模型输出 JSON，
同时准备了三层兜底：

1. 正常解析；
2. JSON 有瑕疵（代码围栏、末尾多逗号、全角标点、被 max_tokens 截断）→ 尽力修补；
3. 实在解析不出来 → 退化成纯文本画像（structured=False），卡片照样能渲染，
   只是没有雷达图。绝不因为格式问题让用户看到一句 "生成失败"。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .models import CorpusBundle, MemberProfile, Portrait
from .persona_link import apply_llm_hooks, resolve_persona
from .prompts import PromptSpec, build_system_prompt, build_user_prompt
from .titles import fallback_title, normalize_title

#: 需要把「和谁互动过」喂给模型的玩法。姻缘/综合画像/红娘都要看社交痕迹。
_PARTNER_KEYS = frozenset({"match", "portrait", "legacy_match", "legacy_portrait", "love"})

_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
#: 截断时常见的"键写了一半、值还没来"，例如 ... , "advice"  → 直接删掉这截。
_DANGLING_KEY_RE = re.compile(r",\s*\"[^\"]*\"\s*:?\s*$")
#: 逐段砍尾巴的最大尝试次数，防止超长残片把 CPU 磨光。
_MAX_TRUNCATE_TRIES = 40
_SMART_QUOTES = {
    "\u201c": '"',
    "\u201d": '"',
    "\u2018": "'",
    "\u2019": "'",
}
#: 只在 JSON 结构位置（字符串之外）纠正的全角符号。
_STRUCT_FIXES = {
    "\uff0c": ",",
    "\uff1a": ":",
    "\u201c": '"',
    "\u201d": '"',
}


class AnalyzeError(RuntimeError):
    """分析链路的可预期失败（没有 provider、模型报错等）。"""


def strip_code_fence(text: str) -> str:
    """去掉 markdown 代码围栏。

    模型很爱在 JSON 外面裹一层围栏，哪怕提示词里明说了不要。
    """
    body = text.strip()
    if not body.startswith("~~~") and "\u0060\u0060\u0060" not in body:
        return body
    lines = [line for line in body.splitlines() if not line.strip().startswith(("\u0060\u0060\u0060", "~~~"))]
    return "\n".join(lines).strip()


def extract_json_object(text: str) -> str:
    """从一段可能夹带寒暄的文本里抠出最外层 JSON 对象。"""
    body = strip_code_fence(text)
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        return ""
    return body[start : end + 1]


def json_fragment(text: str) -> str:
    """从 `{` 开始一路取到结尾，不要求右括号闭合。

    模型被 max_tokens 截断时，输出往往停在半句话上。这种残片交给 _repair_json
    补齐，通常还能救回 headline / 维度 / 前几段正文，比整份退化成纯文本划算得多。
    """
    body = strip_code_fence(text)
    start = body.find("{")
    if start == -1:
        return ""
    return body[start:]


def _repair_json(text: str) -> str:
    """字符串感知的 JSON 修补。

    做三件事：字符串外把全角逗号/冒号/引号换回半角；字符串内把裸换行、制表符
    转成合法转义；最后补上没闭合的引号与括号，并抹掉尾随逗号。

    刻意不做「全文替换中文标点」那种一刀切修补：证据区要逐字保留群友原话，
    把正文里的中文逗号改成英文逗号等于篡改证据。
    """
    out: list[str] = []
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            if ch in "\n\r":
                out.append("\\n")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        mapped = _STRUCT_FIXES.get(ch, ch)
        if mapped == '"':
            in_string = True
            out.append(mapped)
            continue
        if mapped in "{[":
            stack.append("}" if mapped == "{" else "]")
        elif mapped in "}]" and stack and stack[-1] == mapped:
            stack.pop()
        out.append(mapped)
    if escaped:
        out.pop()  # 结尾悬空的反斜杠，直接丢掉
    if in_string:
        out.append('"')
    body = "".join(out).rstrip()
    body = _DANGLING_KEY_RE.sub("", body)
    while body.endswith(","):
        body = body[:-1].rstrip()
    while stack:
        body += stack.pop()
    return _TRAILING_COMMA_RE.sub(r"\1", body)


def _cut_points(text: str) -> list[int]:
    """字符串外的逗号位置，用来一段段砍掉被截断的尾巴。"""
    points: list[int] = []
    in_string = False
    escaped = False
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == ",":
            points.append(index)
    return points


def _json_attempts(text: str) -> list[tuple[str, str]]:
    """按"改动从小到大"的顺序列出候选，返回 (策略名, 文本)。"""
    attempts: list[tuple[str, str]] = []
    seen: set[str] = set()

    def push(name: str, body: str) -> None:
        body = body.strip()
        if not body or body in seen:
            return
        seen.add(body)
        attempts.append((name, body))

    closed = extract_json_object(text)
    push("strict", closed)
    if closed:
        repaired = _TRAILING_COMMA_RE.sub(r"\1", closed)
        push("trailing_comma", repaired)
        swapped = repaired
        for bad, good in _SMART_QUOTES.items():
            swapped = swapped.replace(bad, good)
        push("smart_quotes", swapped)
    #: 模型返回顶层数组时不做残片抢救：我们的契约是一个对象，
    #: 硬抢救只会把数组里的第一个元素当成画像，属于猜测而不是修补。
    fragment = "" if strip_code_fence(text).lstrip().startswith("[") else json_fragment(text)
    if fragment:
        push("repair", _repair_json(fragment))
        for index in reversed(_cut_points(fragment)[-_MAX_TRUNCATE_TRIES:]):
            push("truncate", _repair_json(fragment[:index]))
    return attempts


def parse_portrait_payload(text: str, report: list[str] | None = None) -> dict[str, Any] | None:
    """尽最大努力把模型输出解析成 dict。失败返回 None。

    传入 report 时，会把最终生效的策略名追加进去，方便上层写日志。
    """
    for name, attempt in _json_attempts(text):
        try:
            data = json.loads(attempt)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data:
            if report is not None:
                report.append(name)
            return data
    return None


def clamp_confidence(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:
        # 模型偶尔会写 85 而不是 0.85。
        number = number / 100.0 if number <= 100.0 else 1.0
    return max(0.0, min(1.0, number))


def sample_penalty(sampled: int, min_messages: int) -> float:
    """样本不足时的置信度折扣系数。

    模型自己往往意识不到"只有 8 条发言"意味着什么，所以本地再压一道：
    刚好达到下限 → 0.75 倍，达到下限三倍以上 → 不打折。
    """
    floor = max(1, min_messages)
    if sampled >= floor * 3:
        return 1.0
    if sampled >= floor:
        ratio = (sampled - floor) / float(floor * 2)
        return 0.75 + 0.25 * ratio
    return max(0.3, 0.75 * (sampled / float(floor)))


def build_portrait(
    payload: dict[str, Any],
    *,
    kind: str,
    bundle: CorpusBundle,
    min_messages: int,
    raw_text: str = "",
    seed: str = "",
    title_fallback: bool = True,
) -> Portrait:
    """把解析出来的 dict 收敛成 Portrait，并做本地校正。

    title_fallback=False 表示"模型没给头衔就留空"。恋爱诊断走这一路：
    它的头衔由四维实分推导（prism.titles.love_title），不该被玩法称号池顶掉。
    """
    payload = dict(payload)
    payload["kind"] = kind
    payload["structured"] = True
    portrait = Portrait.from_dict(payload)
    portrait.raw_text = raw_text or portrait.raw_text

    # 维度分数越界的情况相当常见，直接夹紧。
    for dim in portrait.dimensions:
        dim.score = max(0, min(100, int(dim.score)))

    # 专属头衔：模型给的先洗一遍（剥前缀、限长、判跑偏），洗不出来就按玩法兜一枚。
    portrait.title = normalize_title(portrait.title)
    if not portrait.title and title_fallback:
        portrait.title = fallback_title(
            kind,
            seed=seed,
            tags=portrait.tags,
            dimensions=portrait.dimensions,
            headline=portrait.headline,
        )

    portrait.confidence = clamp_confidence(portrait.confidence) or 0.6
    penalty = sample_penalty(bundle.stats.sampled, min_messages)
    portrait.confidence = round(portrait.confidence * penalty, 3)
    if penalty < 1.0 and not portrait.sample_note:
        # 样本说明不再插进正文（会打断阅读），改成挂在卡片最底部的小字。
        portrait.sample_note = (
            f"本次仅采集到 {bundle.stats.sampled} 条有效发言"
            f"（建议至少 {max(1, min_messages) * 3} 条），结论仅供参考。"
        )
    return portrait


def plain_portrait(
    text: str,
    *,
    kind: str,
    confidence: float = 0.35,
    seed: str = "",
    title_fallback: bool = True,
) -> Portrait:
    """结构化解析彻底失败时的兜底画像。

    纯文本玩法（画像系列的自由排版）也照样配一枚头衔 —— 这条路没有 JSON，
    只能按玩法 + 种子兜一个，起码卡片抬头不空。
    """
    return Portrait(
        kind=kind,
        headline="",
        title=fallback_title(kind, seed=seed) if title_fallback else "",
        confidence=confidence,
        raw_text=text.strip(),
        structured=False,
    )


class PrismAnalyzer:
    """provider 选取 + 重试 + 解析。故意不碰事件对象，方便单测。"""

    __slots__ = ("_config", "_context", "_logger", "_semaphore")

    def __init__(self, context: Any, config: Any, logger: Any = None) -> None:
        self._context = context
        self._config = config
        self._logger = logger
        self._semaphore = asyncio.Semaphore(max(1, config.int_of("limits.max_concurrency")))

    # -- provider ----------------------------------------------------------
    def resolve_provider(self, umo: str = "") -> Any:
        """按 "配置指定 → 当前会话正在用的 → 全局第一个" 的顺序找 provider。"""
        provider_id = self._config.str_of("llm.provider_id")
        if provider_id:
            provider = self._context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
            if self._logger:
                self._logger.warning(
                    "配置里指定的 provider_id=%s 不存在，回落到当前会话的提供商。",
                    provider_id,
                )
        provider = None
        if umo:
            try:
                provider = self._context.get_using_provider(umo=umo)
            except TypeError:
                provider = self._context.get_using_provider()
        if provider is None:
            try:
                provider = self._context.get_using_provider()
            except Exception:  # 不同版本签名有差异，兜底即可
                provider = None
        if provider is None:
            providers = self._context.get_all_providers() or []
            provider = providers[0] if providers else None
        if provider is None:
            raise AnalyzeError(
                "没有可用的文本模型提供商，请先在 AstrBot 里配置一个 LLM provider。",
            )
        return provider

    # -- AstrBot 人格 ------------------------------------------------------
    async def persona_note(self, umo: str = "") -> str:
        """可选：取当前会话人格，作为文案口吻提示。默认关闭。

        这里刻意留了 info 级日志：口吻生效与否在卡片上很难肉眼分辨，出问题时
        先看一眼日志就能分清「开关没开」「人格没解析到」和「模型没照做」。
        """
        if not self._config.bool_of("persona.use_astrbot_persona"):
            return ""
        info = await resolve_persona(
            self._context,
            umo=umo,
            persona_id=self._config.str_of("persona.persona_id"),
            logger=self._logger,
        )
        block = info.to_prompt_block()
        if block and self._logger:
            self._logger.info(
                "[人格棱镜] 本次文案套用人格「%s」（来源：%s，设定 %s 字）。",
                info.name or "未命名",
                info.origin or "未知",
                len(info.prompt),
            )
        return block

    # -- 主流程 ------------------------------------------------------------
    async def analyze(
        self,
        spec: PromptSpec,
        bundle: CorpusBundle,
        *,
        target_name: str,
        group_name: str = "",
        profile: MemberProfile | None = None,
        umo: str = "",
        extra_facts: str = "",
        seed: str = "",
        title_fallback: bool = True,
        dialogue_block: str = "",
        social_block: str = "",
        event: Any = None,
    ) -> tuple[Portrait, str]:
        """执行一次分析，返回 (画像, 模型名)。"""
        provider = self.resolve_provider(umo)
        persona_note = await self.persona_note(umo)
        #: 口吻要同时写进系统提示，否则会被系统提示开头那句「冷静、克制的观察者」压掉，
        #: 表现为「开了人格但文案还是中性报告」。
        system_prompt = build_system_prompt(persona_note=persona_note)
        user_prompt = build_user_prompt(
            spec,
            bundle,
            target_name=target_name,
            group_name=group_name,
            profile=profile,
            profile_fields=self._config.profile_fields(),
            include_partners=spec.key in _PARTNER_KEYS or not spec.builtin,
            extra_facts=extra_facts,
            dialogue_block=dialogue_block,
            social_block=social_block,
            persona_note=persona_note,
        )
        if self._config.bool_of("persona.allow_llm_hooks"):
            hooked_system, hooked_user = await apply_llm_hooks(
                event,
                system_prompt,
                user_prompt,
                logger=self._logger,
            )
            if self._logger and (hooked_system != system_prompt or hooked_user != user_prompt):
                self._logger.info("[人格棱镜] 其他插件的 LLM 钩子给本次分析补充了设定。")
            system_prompt, user_prompt = hooked_system, hooked_user
        model = self._config.str_of("llm.model")
        timeout = max(30, self._config.int_of("llm.timeout_sec"))
        retries = max(0, self._config.int_of("llm.retry_times"))
        min_messages = self._config.int_of("collect.min_messages")

        last_error: Exception | None = None
        last_text = ""
        async with self._semaphore:
            for attempt in range(retries + 1):
                try:
                    text = await asyncio.wait_for(
                        self._call(provider, system_prompt, user_prompt, model),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError as exc:
                    last_error = exc
                    if self._logger:
                        self._logger.warning("画像分析超时（第 %s 次尝试）。", attempt + 1)
                    continue
                except Exception as exc:  # provider 实现五花八门
                    last_error = exc
                    if self._logger:
                        self._logger.warning("画像分析调用失败：%s", exc)
                    continue
                last_text = text or ""
                if not last_text.strip():
                    last_error = AnalyzeError("模型返回了空内容。")
                    continue
                if not spec.structured:
                    return (
                        plain_portrait(
                            last_text,
                            kind=spec.key,
                            confidence=0.6,
                            seed=seed,
                            title_fallback=title_fallback,
                        ),
                        model,
                    )
                repair_report: list[str] = []
                payload = parse_portrait_payload(last_text, repair_report)
                if payload and repair_report and repair_report[0] != "strict" and self._logger:
                    self._logger.info(
                        "[人格棱镜] 模型输出的 JSON 有瑕疵，已就地修补后使用（策略：%s）。",
                        repair_report[0],
                    )
                if payload:
                    portrait = build_portrait(
                        payload,
                        kind=spec.key,
                        bundle=bundle,
                        min_messages=min_messages,
                        raw_text=last_text,
                        seed=seed,
                        title_fallback=title_fallback,
                    )
                    if portrait.headline or portrait.sections:
                        return portrait, model
                last_error = AnalyzeError("模型没有按约定输出 JSON。")
                if self._logger:
                    self._logger.warning(
                        "第 %s 次尝试未拿到合法 JSON，%s。",
                        attempt + 1,
                        "准备重试" if attempt < retries else "退化为纯文本",
                    )

        if last_text.strip():
            # 内容是有的，只是格式不对。宁可少一张雷达图，也别让用户白等。
            return plain_portrait(last_text, kind=spec.key, seed=seed, title_fallback=title_fallback), model
        raise AnalyzeError(str(last_error) if last_error else "分析失败，未知原因。")

    async def _call(
        self,
        provider: Any,
        system_prompt: str,
        user_prompt: str,
        model: str,
    ) -> str:
        kwargs: dict[str, Any] = {
            "prompt": user_prompt,
            "system_prompt": system_prompt,
            "contexts": [],
        }
        if model:
            kwargs["model"] = model
        response = await provider.text_chat(**kwargs)
        return str(getattr(response, "completion_text", "") or "")
