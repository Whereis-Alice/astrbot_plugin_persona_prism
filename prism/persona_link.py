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
    #: 这个人格是从哪一层解析出来的，只用于日志排查（"会话生效""当前对话""全局默认""配置指定"）。
    origin: str = ""

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
            f"这次的全部文案（头衔、判词、小节正文、建议）都要由「{who}」本人写出来，"
            "用 TA 的语气、用词习惯、自称和口癖，读起来要像 TA 在点评，而不是一份中立报告。\n"
            "同时守住三条：结论与评分只能来自语料，输出字段和格式一个都不能少，"
            "不要在文案里自我介绍、不要写成对话、也不要提到「人格」这两个字。\n"
            f"「{who}」的设定：\n{body}"
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

    优先级和 AstrBot 自己的对话链路保持一致，从高到低：

    1. 插件配置里写死的人格；
    2. **本会话生效的人格** —— 交给 AstrBot 的 ``resolve_selected_persona`` 判定，
       它会先看会话级强制人格（WebUI 会话管理里给某个群单独钉的那个），再看当前
       对话的 persona_id，最后回落到全局默认人格。早期版本只读了「当前对话」这一层，
       结果是「给群钉了人格却不生效」；
    3. 老版本 AstrBot 没有这个解析器时，退回「当前对话 → 全局默认」的旧链路。

    任何一步抛异常都静默降级成「不套用口吻」，绝不能让画像本身失败。
    """
    manager = getattr(context, "persona_manager", None)
    if manager is None:
        return PersonaInfo()
    wanted = str(persona_id or "").strip()
    persona: Any = None
    origin = ""
    try:
        if wanted:
            persona = await _by_id(manager, wanted)
            origin = "配置指定"
        else:
            persona = await _selected_persona(context, manager, umo)
            if persona is _OPT_OUT:
                if logger:
                    logger.info("[人格棱镜] 本会话显式关闭了人格，这次不套用口吻。")
                return PersonaInfo()
            if persona is not None:
                origin = "会话生效"
            if persona is None:
                persona = await _from_conversation(context, manager, umo)
                if persona is _OPT_OUT:
                    if logger:
                        logger.info("[人格棱镜] 本会话显式关闭了人格，这次不套用口吻。")
                    return PersonaInfo()
                if persona is not None:
                    origin = "当前对话"
            if persona is None:
                persona = await _default_persona(manager, umo)
                origin = "全局默认"
    except Exception as exc:  # 人格系统的实现随版本变动，失败就降级
        if logger:
            logger.warning("[人格棱镜] 读取 AstrBot 人格失败，这次不套用口吻：%s", exc)
        return PersonaInfo()
    if persona is None:
        if logger:
            logger.info("[人格棱镜] 没找到可用的 AstrBot 人格，这次不套用口吻。")
        return PersonaInfo()
    return PersonaInfo(
        name=_persona_field(persona, "name").strip(),
        prompt=_persona_field(persona, "prompt").strip(),
        origin=origin,
    )


async def _by_id(manager: Any, persona_id: str) -> Any:
    getter = getattr(manager, "get_persona_v3_by_id", None)
    if getter is None:
        return None
    result = getter(persona_id)
    if hasattr(result, "__await__"):
        result = await result
    return result


async def _default_persona(manager: Any, umo: str = "") -> Any:
    """全局默认人格。新版签名收 umo（支持按会话隔离配置），老版不收。"""
    getter = getattr(manager, "get_default_persona_v3", None)
    if getter is None:
        return None
    attempts: tuple[tuple[Any, ...], ...] = ((umo,), ()) if umo else ((),)
    for args in attempts:
        try:
            result = getter(*args)
        except TypeError:
            continue
        if hasattr(result, "__await__"):
            result = await result
        return result
    return None


async def _conversation_persona_id(context: Any, umo: str) -> str | None:
    """当前对话上钉的 persona_id。None = 没设置（该回落默认人格）。"""
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
    return raw or None


def _provider_settings(context: Any, umo: str) -> dict[str, Any]:
    """当前会话生效的 provider_settings，用来取全局默认人格名。"""
    getter = getattr(context, "get_config", None)
    if not callable(getter):
        return {}
    try:
        conf = getter(umo=umo) if umo else getter()
        settings = conf.get("provider_settings") or {}
    except Exception:
        return {}
    return dict(settings) if isinstance(settings, dict) else {}


async def _selected_persona(context: Any, manager: Any, umo: str) -> Any:
    """问 AstrBot：这个会话此刻到底在用哪个人格。

    这是唯一能看到「会话级强制人格」的入口 —— 那份设置存在 shared preferences 里，
    不在对话记录上，只读 conversation.persona_id 是看不到的。老版本没有这个方法时
    返回 None，由调用方走旧链路。
    """
    resolver = getattr(manager, "resolve_selected_persona", None)
    if resolver is None or not umo:
        return None
    conv_persona = await _conversation_persona_id(context, umo)
    if conv_persona == NO_PERSONA:
        return _OPT_OUT
    result = await resolver(
        umo=umo,
        conversation_persona_id=conv_persona,
        platform_name=str(umo).split(":")[0],
        provider_settings=_provider_settings(context, umo),
    )
    if not isinstance(result, (tuple, list)) or not result:
        return None
    chosen_id = str(result[0] or "")
    if chosen_id == NO_PERSONA:
        return _OPT_OUT
    persona = result[1] if len(result) > 1 else None
    if persona is None and chosen_id:
        persona = await _by_id(manager, chosen_id)
    return persona


async def _from_conversation(context: Any, manager: Any, umo: str) -> Any:
    """旧链路：顺着「会话 → 对话 → persona_id → 人格」找当前正在用的人格。"""
    raw = await _conversation_persona_id(context, umo)
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
