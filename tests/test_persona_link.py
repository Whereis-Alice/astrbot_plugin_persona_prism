"""人格联动（persona_link）测试：人格解析降级链、口吻段落、LLM 钩子容错。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot_plugin_persona_prism.prism import persona_link
from astrbot_plugin_persona_prism.prism.persona_link import PersonaInfo

UMO = "aiocqhttp:GroupMessage:700"


class FakePersonaManager:
    def __init__(self, by_id=None, default=None):
        self._by_id = by_id or {}
        self._default = default

    async def get_persona_v3_by_id(self, persona_id):
        return self._by_id.get(persona_id)

    async def get_default_persona_v3(self):
        return self._default


class FakeConversationManager:
    def __init__(self, cid="c1", persona_id=""):
        self._cid = cid
        self._persona_id = persona_id

    async def get_curr_conversation_id(self, umo):
        return self._cid

    async def get_conversation(self, umo, cid):
        obj = type("Conv", (), {})()
        obj.persona_id = self._persona_id
        return obj


class FakeContext:
    def __init__(self, persona_manager=None, conversation_manager=None):
        if persona_manager is not None:
            self.persona_manager = persona_manager
        if conversation_manager is not None:
            self.conversation_manager = conversation_manager


def _run(coro):
    return asyncio.run(coro)


class TestPersonaInfo:
    def test_blank_is_not_usable(self):
        assert not PersonaInfo().usable
        assert PersonaInfo(name="\u7231\u4e43", prompt="   ").to_prompt_block() == ""

    def test_block_carries_name_and_setting_only(self):
        """\u4eba\u683c\u5757\u53ea\u8d1f\u8d23\u4ea4\u4ee3\u300c\u4f60\u662f\u8c01\u300d\uff1b\u73b0\u5728\u7684\u94c1\u5f8b\u7531 prompts \u5728\u9996\u5c3e\u4e24\u7aef\u5206\u522b\u8865\u4e0a\u3002"""
        block = PersonaInfo(name="\u7231\u4e43", prompt="\u4f60\u662f\u4fee\u98ce\u8f66\u7684\u5c11\u5973").to_prompt_block()
        assert "\u7231\u4e43" in block
        assert "\u4f60\u662f\u4fee\u98ce\u8f66\u7684\u5c11\u5973" in block

    def test_long_prompt_is_truncated(self):
        block = PersonaInfo(name="X", prompt="\u5566" * 2000).to_prompt_block()
        assert len(block) < 2000
        assert block.endswith("\u2026\u2026")


class TestResolvePersona:
    def test_no_persona_manager_returns_empty(self):
        info = _run(persona_link.resolve_persona(FakeContext(), umo=UMO))
        assert not info.usable

    def test_explicit_persona_id_wins(self):
        mgr = FakePersonaManager(
            by_id={"a": {"name": "A", "prompt": "pa"}},
            default={"name": "D", "prompt": "pd"},
        )
        ctx = FakeContext(mgr, FakeConversationManager(persona_id="b"))
        info = _run(persona_link.resolve_persona(ctx, umo=UMO, persona_id="a"))
        assert info.name == "A"

    def test_conversation_persona_used_when_id_blank(self):
        mgr = FakePersonaManager(
            by_id={"b": {"name": "B", "prompt": "pb"}},
            default={"name": "D", "prompt": "pd"},
        )
        ctx = FakeContext(mgr, FakeConversationManager(persona_id="b"))
        info = _run(persona_link.resolve_persona(ctx, umo=UMO))
        assert info.name == "B"

    def test_falls_back_to_default_persona(self):
        mgr = FakePersonaManager(default={"name": "D", "prompt": "pd"})
        ctx = FakeContext(mgr, FakeConversationManager(persona_id=""))
        info = _run(persona_link.resolve_persona(ctx, umo=UMO))
        assert info.name == "D"

    def test_session_opt_out_is_respected(self):
        mgr = FakePersonaManager(default={"name": "D", "prompt": "pd"})
        ctx = FakeContext(
            mgr, FakeConversationManager(persona_id=persona_link.NO_PERSONA)
        )
        info = _run(persona_link.resolve_persona(ctx, umo=UMO))
        assert not info.usable

    def test_object_style_persona_is_supported(self):
        persona = type("P", (), {"name": "O", "prompt": "po"})()
        mgr = FakePersonaManager(by_id={"o": persona})
        ctx = FakeContext(mgr)
        info = _run(persona_link.resolve_persona(ctx, umo=UMO, persona_id="o"))
        assert info.name == "O"

    def test_manager_error_degrades_silently(self):
        class Boom:
            async def get_persona_v3_by_id(self, persona_id):
                raise RuntimeError("boom")

        info = _run(
            persona_link.resolve_persona(FakeContext(Boom()), umo=UMO, persona_id="x")
        )
        assert not info.usable

    def test_blank_umo_skips_conversation_lookup(self):
        mgr = FakePersonaManager(default={"name": "D", "prompt": "pd"})
        ctx = FakeContext(mgr, FakeConversationManager(persona_id="b"))
        info = _run(persona_link.resolve_persona(ctx, umo=""))
        assert info.name == "D"


class FakeResolvingManager(FakePersonaManager):
    """带 resolve_selected_persona 的新版人格管理器替身。"""

    def __init__(self, by_id=None, default=None, forced=None):
        super().__init__(by_id=by_id, default=default)
        self._forced = forced
        self.seen: dict[str, Any] = {}

    async def resolve_selected_persona(
        self,
        *,
        umo,
        conversation_persona_id,
        platform_name,
        provider_settings=None,
    ):
        self.seen = {
            "umo": umo,
            "conversation_persona_id": conversation_persona_id,
            "platform_name": platform_name,
            "provider_settings": provider_settings,
        }
        if self._forced is not None:
            return (self._forced, self._by_id.get(self._forced), self._forced, False)
        return (conversation_persona_id, self._by_id.get(conversation_persona_id), None, False)


class ConfigContext(FakeContext):
    def __init__(self, persona_manager=None, conversation_manager=None, conf=None):
        super().__init__(persona_manager, conversation_manager)
        self._conf = conf or {}

    def get_config(self, umo=None):
        return self._conf


class TestSessionScopedPersona:
    """会话级强制人格是 AstrBot 里优先级最高的一层，早期版本读不到。"""

    def test_session_forced_persona_beats_conversation(self):
        mgr = FakeResolvingManager(
            by_id={"a": {"name": "A", "prompt": "pa"}, "b": {"name": "B", "prompt": "pb"}},
            default={"name": "D", "prompt": "pd"},
            forced="a",
        )
        ctx = ConfigContext(mgr, FakeConversationManager(persona_id="b"))
        info = _run(persona_link.resolve_persona(ctx, umo=UMO))
        assert info.name == "A"
        assert info.origin == "\u4f1a\u8bdd\u751f\u6548"

    def test_resolver_receives_platform_and_provider_settings(self):
        mgr = FakeResolvingManager(by_id={"b": {"name": "B", "prompt": "pb"}})
        conf = {"provider_settings": {"default_personality": "D"}}
        ctx = ConfigContext(mgr, FakeConversationManager(persona_id="b"), conf=conf)
        _run(persona_link.resolve_persona(ctx, umo=UMO))
        assert mgr.seen["platform_name"] == "aiocqhttp"
        assert mgr.seen["provider_settings"] == {"default_personality": "D"}
        assert mgr.seen["conversation_persona_id"] == "b"

    def test_unset_conversation_persona_is_passed_as_none(self):
        mgr = FakeResolvingManager(default={"name": "D", "prompt": "pd"})
        ctx = ConfigContext(mgr, FakeConversationManager(persona_id=""))
        info = _run(persona_link.resolve_persona(ctx, umo=UMO))
        assert mgr.seen["conversation_persona_id"] is None
        assert info.name == "D"
        assert info.origin == "\u5168\u5c40\u9ed8\u8ba4"

    def test_resolver_opt_out_is_respected(self):
        mgr = FakeResolvingManager(default={"name": "D", "prompt": "pd"})
        ctx = ConfigContext(
            mgr, FakeConversationManager(persona_id=persona_link.NO_PERSONA)
        )
        info = _run(persona_link.resolve_persona(ctx, umo=UMO))
        assert not info.usable

    def test_config_persona_id_still_wins_over_session(self):
        mgr = FakeResolvingManager(
            by_id={"a": {"name": "A", "prompt": "pa"}, "z": {"name": "Z", "prompt": "pz"}},
            forced="a",
        )
        ctx = ConfigContext(mgr, FakeConversationManager(persona_id="a"))
        info = _run(persona_link.resolve_persona(ctx, umo=UMO, persona_id="z"))
        assert info.name == "Z"
        assert info.origin == "\u914d\u7f6e\u6307\u5b9a"
        assert mgr.seen == {}

    def test_default_persona_getter_accepting_umo(self):
        class UmoAware:
            def __init__(self):
                self.got = "sentinel"

            async def get_default_persona_v3(self, umo=None):
                self.got = umo
                return {"name": "D", "prompt": "pd"}

        mgr = UmoAware()
        info = _run(persona_link.resolve_persona(FakeContext(mgr), umo=UMO))
        assert info.name == "D"
        assert mgr.got == UMO


class FakeEvent:
    def __init__(self, stopped=False):
        self._stopped = stopped
        self.session_id = "700"
        self.continued = 0

    def is_stopped(self):
        return self._stopped

    def stop_event(self):
        self._stopped = True

    def continue_event(self):
        self._stopped = False
        self.continued += 1


class TestApplyLlmHooks:
    def test_no_event_returns_input(self):
        got = _run(persona_link.apply_llm_hooks(None, "sys", "user"))
        assert got == ("sys", "user")

    def test_hook_can_append_to_system_prompt(self):
        event = FakeEvent()

        async def fake_hook(evt, kind, req):
            req.system_prompt = req.system_prompt + "\n\u4e16\u754c\u6811\u8bcd\u6761"

        system, user = _run(
            persona_link.apply_llm_hooks(event, "sys", "user", dispatch=fake_hook)
        )
        assert "\u4e16\u754c\u6811\u8bcd\u6761" in system
        assert user == "user"

    def test_hook_failure_returns_input(self):
        event = FakeEvent()

        async def boom(evt, kind, req):
            raise RuntimeError("hook exploded")

        got = _run(persona_link.apply_llm_hooks(event, "sys", "user", dispatch=boom))
        assert got == ("sys", "user")

    def test_stop_flag_is_restored(self):
        event = FakeEvent()

        async def stopper(evt, kind, req):
            evt.stop_event()

        _run(persona_link.apply_llm_hooks(event, "sys", "user", dispatch=stopper))
        assert not event.is_stopped()
        assert event.continued == 1

    def test_already_stopped_event_is_left_alone(self):
        event = FakeEvent(stopped=True)

        async def noop(evt, kind, req):
            return None

        _run(persona_link.apply_llm_hooks(event, "sys", "user", dispatch=noop))
        assert event.is_stopped()
        assert event.continued == 0

    def test_blank_hook_result_keeps_original(self):
        event = FakeEvent()

        async def wipe(evt, kind, req):
            req.system_prompt = ""
            req.prompt = "   "

        got: tuple[Any, Any] = _run(
            persona_link.apply_llm_hooks(event, "sys", "user", dispatch=wipe)
        )
        assert got == ("sys", "user")
