"""调用 LLM 并把返回值解析成结构化画像。

上游是"把模型返回的整段文字直接当图片文案"，好处是简单，坏处是没法做
维度雷达图、没法做证据区块、也没法在 WebUI 里检索。这里要求模型输出 JSON，
同时准备了三层兜底：

1. 正常解析；
2. JSON 有瑕疵（多了代码围栏、末尾多逗号、前后有寒暄）→ 尽力修复；
3. 实在解析不出来 → 退化成纯文本画像（structured=False），卡片照样能渲染，
   只是没有雷达图。绝不因为格式问题让用户看到一句 "生成失败"。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .models import CorpusBundle, MemberProfile, Portrait
from .prompts import PromptSpec, build_system_prompt, build_user_prompt
from .titles import fallback_title, normalize_title

#: 需要把「和谁互动过」喂给模型的玩法。姻缘/综合画像/红娘都要看社交痕迹。
_PARTNER_KEYS = frozenset({"match", "portrait", "legacy_match", "legacy_portrait", "love"})

_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_SMART_QUOTES = {
    "\u201c": '"',
    "\u201d": '"',
    "\u2018": "'",
    "\u2019": "'",
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


def parse_portrait_payload(text: str) -> dict[str, Any] | None:
    """尽最大努力把模型输出解析成 dict。失败返回 None。"""
    candidate = extract_json_object(text)
    if not candidate:
        return None
    attempts = [candidate]
    repaired = _TRAILING_COMMA_RE.sub(r"\1", candidate)
    if repaired != candidate:
        attempts.append(repaired)
    swapped = repaired
    for bad, good in _SMART_QUOTES.items():
        swapped = swapped.replace(bad, good)
    if swapped != repaired:
        attempts.append(swapped)
    for attempt in attempts:
        try:
            data = json.loads(attempt)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
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
    ) -> tuple[Portrait, str]:
        """执行一次分析，返回 (画像, 模型名)。"""
        provider = self.resolve_provider(umo)
        system_prompt = build_system_prompt()
        user_prompt = build_user_prompt(
            spec,
            bundle,
            target_name=target_name,
            group_name=group_name,
            profile=profile,
            profile_fields=self._config.profile_fields(),
            include_partners=spec.key in _PARTNER_KEYS or not spec.builtin,
            extra_facts=extra_facts,
        )
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
                payload = parse_portrait_payload(last_text)
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
