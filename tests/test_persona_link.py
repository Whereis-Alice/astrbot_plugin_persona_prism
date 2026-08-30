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

    def test_block_mentions_name_and_keeps_rules(self):
        block = PersonaInfo(name="\u7231\u4e43", prompt="\u4f60\u662f\u4fee\u98ce\u8f66\u7684\u5c11\u5973").to_prompt_block()
        assert "\u7231\u4e43" in block
        assert "\u53e3\u543b\u53ea\u5f71\u54cd\u63aa\u8f9e" in block
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
