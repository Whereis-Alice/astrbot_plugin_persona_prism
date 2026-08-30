"""可选地把 AstrBot 自带人格、以及其他插件的 LLM 注入钩子接进分析链路。

两件事，都是「可选增强」，任何一步失败都必须静默降级成原来的行为：

1. **会话人格**：读取当前会话正在用的人格（AstrBot 的 persona），把它的设定
   作为「叙述口吻」附加到分析提示词里。注意不是替换我们的系统提示词 —— 否则
   人格设定里的自由发挥会把 JSON 输出契约和「不许编造」这些红线冲掉。人格只
   影响文案语气，不影响事实判断和输出格式。
2. **第三方注入钩子**：AstrBot 的 `@filter.on_llm_request()` 钩子（例如世界树
   词条插件 astrbot_plugin_worldtree_lore）会往 `req.system_prompt` 里追加设定。
   我们的分析是自己直连 provider.text_chat 的，本来不会触发这些钩子；这里手动
   分发一次 OnLLMRequestEvent，让这类插件也能给画像补充世界观信息。

为什么默认关闭：人格和词条会稀释「只依据语料」的约束，画像准确性优先。愿意
牺牲一点严谨换角色扮演味道的人，可以在 WebUI 里打开。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 人格设定截断长度。太长会挤掉语料预算，而且口吻提示本来只需要开头那几句。
PERSONA_LIMIT = 900

#: AstrBot 里表示「本会话显式不使用人格」的哨兵值。
NO_PERSONA = "[%None]"

#: 内部标记：会话里显式关掉了人格 —— 这时不该再回落到全局默认人格。
_OPT_OUT = object()


@dataclass(slots=True)
class PersonaInfo:
    """解析出来的人格信息。name 为空表示没拿到。"""

    name: str = ""
    prompt: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.prompt.strip())

    def to_prompt_block(self) -> str:
        """渲染成「叙述口吻」段落。"""
        if not self.usable:
            return ""
        body = self.prompt.strip()
        if len(body) > PERSONA_LIMIT:
            body = body[:PERSONA_LIMIT].rstrip() + "……"
        who = self.name or "你"
        return (
            f"请以「{who}」的语气和用词习惯来写这次的文案（标题、判词、正文、建议）。\n"
            "口吻只影响措辞，不影响结论：输出格式、字段、以及「只依据语料、禁止编造」"
            "这些规则一律不变；也不要在文案里自我介绍或扮演对话。\n"
            f"人格设定：\n{body}"
        )


def _persona_field(persona: Any, key: str) -> str:
    """人格对象在不同版本里可能是 dict、也可能是带属性的对象。"""
    if persona is None:
        return ""
    if isinstance(persona, dict):
        return str(persona.get(key) or "")
    return str(getattr(persona, key, "") or "")


async def resolve_persona(
    context: Any,
    *,
    umo: str = "",
    persona_id: str = "",
    logger: Any = None,
) -> PersonaInfo:
    """取出要用的人格。拿不到就返回空的 PersonaInfo，调用方照旧跑。

    顺序：配置里写死的 persona_id → 当前会话正在用的人格 → 全局默认人格。
    """
    manager = getattr(context, "persona_manager", None)
    if manager is None:
        return PersonaInfo()
    wanted = str(persona_id or "").strip()
    persona: Any = None
    try:
        if wanted:
            persona = await _by_id(manager, wanted)
        else:
            persona = await _from_conversation(context, manager, umo)
            if persona is _OPT_OUT:
                return PersonaInfo()
            if persona is None:
                persona = await _default_persona(manager)
    except Exception as exc:  # 人格系统的实现随版本变动，失败就降级
        if logger:
            logger.debug("读取 AstrBot 人格失败，本次不套用口吻：%s", exc)
        return PersonaInfo()
    if persona is None:
        return PersonaInfo()
    return PersonaInfo(
        name=_persona_field(persona, "name").strip(),
        prompt=_persona_field(persona, "prompt").strip(),
    )


async def _by_id(manager: Any, persona_id: str) -> Any:
    getter = getattr(manager, "get_persona_v3_by_id", None)
    if getter is None:
        return None
    result = getter(persona_id)
    if hasattr(result, "__await__"):
        result = await result
    return result


async def _default_persona(manager: Any) -> Any:
    getter = getattr(manager, "get_default_persona_v3", None)
    if getter is None:
        return None
    result = getter()
    if hasattr(result, "__await__"):
        result = await result
    return result


async def _from_conversation(context: Any, manager: Any, umo: str) -> Any:
    """顺着「会话 → 对话 → persona_id → 人格」找当前正在用的人格。"""
    if not umo:
        return None
    conv_mgr = getattr(context, "conversation_manager", None)
    if conv_mgr is None:
        return None
    cid = await conv_mgr.get_curr_conversation_id(umo)
    if not cid:
        return None
    conversation = await conv_mgr.get_conversation(umo, cid)
    raw = str(getattr(conversation, "persona_id", "") or "")
    if raw == NO_PERSONA:
        # 用户在这个会话里显式关掉了人格，尊重这个选择，也不回落默认人格。
        return _OPT_OUT
    if not raw:
        return None
    return await _by_id(manager, raw)


# ---------------------------------------------------------------------------
# 第三方 on_llm_request 钩子
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FallbackRequest:
    """AstrBot 的 ProviderRequest 拿不到时的替身，只需要这几个字段。"""

    prompt: str = ""
    system_prompt: str = ""
    session_id: str = ""


def _load_hook_bridge() -> tuple[Any, Any, Any]:
    """取出 (分发函数, 事件类型, 请求类)。任何一项缺失都返回三个 None。"""
    from astrbot.core.pipeline.context_utils import call_event_hook
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.star.star_handler import EventType

    return call_event_hook, EventType.OnLLMRequestEvent, ProviderRequest


def _make_request(request_cls: Any, system_prompt: str, user_prompt: str, session_id: str) -> Any:
    if request_cls is None:
        return _FallbackRequest(
            prompt=user_prompt, system_prompt=system_prompt, session_id=session_id
        )
    return request_cls(
        prompt=user_prompt,
        system_prompt=system_prompt,
        contexts=[],
        session_id=session_id,
    )


async def apply_llm_hooks(
    event: Any,
    system_prompt: str,
    user_prompt: str,
    *,
    logger: Any = None,
    dispatch: Any = None,
) -> tuple[str, str]:
    """手动分发一次 OnLLMRequestEvent，让别的插件有机会补充设定。

    返回 (system_prompt, user_prompt)。任何异常都返回原样，绝不让画像失败。
    钩子若把事件标记为 stopped，我们也只是忽略它 —— 这是一次内部请求，不该
    因为别的插件的拦截逻辑而中断用户的指令。

    dispatch 只为测试预留：签名 (event, event_type, req)，默认走 AstrBot 的
    call_event_hook。
    """
    if event is None:
        return system_prompt, user_prompt
    event_type: Any = None
    request_cls: Any = None
    try:
        bridge_dispatch, event_type, request_cls = _load_hook_bridge()
    except Exception as exc:
        if dispatch is None:
            if logger:
                logger.debug("当前 AstrBot 版本不支持手动分发 LLM 钩子：%s", exc)
            return system_prompt, user_prompt
    else:
        dispatch = dispatch or bridge_dispatch
    was_stopped = False
    try:
        was_stopped = bool(event.is_stopped())
    except Exception:
        was_stopped = False
    req = _make_request(
        request_cls,
        system_prompt,
        user_prompt,
        str(getattr(event, "session_id", "") or ""),
    )
    try:
        await dispatch(event, event_type, req)
    except Exception as exc:
        if logger:
            logger.debug("分发 LLM 钩子时出错，本次忽略：%s", exc)
        return system_prompt, user_prompt
    finally:
        _restore_stop_flag(event, was_stopped)
    new_system = str(getattr(req, "system_prompt", "") or "").strip()
    new_prompt = str(getattr(req, "prompt", "") or "").strip()
    return (new_system or system_prompt, new_prompt or user_prompt)


def _restore_stop_flag(event: Any, was_stopped: bool) -> None:
    """钩子可能顺手 stop 掉事件，会连带掐掉我们后面要发的卡片。"""
    if was_stopped:
        return
    try:
        if event.is_stopped():
            event.continue_event()
    except Exception:
        return
