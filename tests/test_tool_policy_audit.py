"""Characterize tool restrictions and known boundary gaps without live execution."""

import asyncio
from types import SimpleNamespace

import pytest

from core.agent.func_tool_manager import FuncToolManager
from core.provider import LLMRequest, LLMResponse
from _events import batch, message


@pytest.fixture
def subject(reminder_main):
    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin.config = reminder_main.ReminderConfig(autonomy_mode="plan_only")
    plugin._identity = reminder_main.IdentityResolver("audit-secret")
    return plugin


def internal(subject, module, *, kind="BOT", secret=None):
    resolver = module.IdentityResolver(secret) if secret else subject._identity
    envelope = resolver.build_envelope(
        origin=(module.EventOrigin.AUTONOMY_RANDOM_CHECK if kind == "BOT" else module.EventOrigin.REMINDER_FIRE),
        principal_kind=getattr(module.PrincipalKind, kind),
        principal_id="bot:QQ:bot-1",
    )
    return message("bot-1", extra=envelope, is_notice=True)


def manager():
    mgr = FuncToolManager(SimpleNamespace(get_config=lambda *_: None))
    async def harmless(_event, **_kwargs):
        return "audit tool executed"
    for name in ("exec", "poke", "schedule_intent_followup", "list_reminders"):
        mgr.register_tool(name, name, {"type": "object", "properties": {}}, harmless)
    return mgr


@pytest.mark.parametrize("mode,extra,exec_allowed", [
    ("trusted_admin", [], True),
    ("trusted_admin", ["exec"], True),
    ("plan_only", ["exec"], False),
    ("act_with_confirm", ["exec"], False),
    ("observe", ["exec"], False),
    ("off", ["exec"], False),
])
def test_mode_allowlist_matrix(subject, reminder_main, mode, extra, exec_allowed):
    subject.config.autonomy_mode = mode
    subject.config.autonomy_allowed_tools += extra
    req = LLMRequest(tool_set=manager().build_tool_set())
    subject._filter_internal_event_tools(batch(internal(subject, reminder_main)), req)
    assert ("exec" in req.tool_set) is exec_allowed
    assert "poke" not in req.tool_set
    assert ("schedule_intent_followup" in req.tool_set) is (mode not in {"observe", "off"})
    assert ("list_reminders" in req.tool_set) is (mode != "off")


@pytest.mark.parametrize("kind,restricted", [("SYSTEM", True), ("USER", False)])
def test_other_trusted_principals(subject, reminder_main, kind, restricted):
    req = LLMRequest(tool_set=manager().build_tool_set())
    subject._filter_internal_event_tools(batch(internal(subject, reminder_main, kind=kind)), req)
    assert (len(req.tool_set.tools) == 0) is restricted


def test_removal_does_not_pollute_other_requests(subject, reminder_main):
    mgr = manager()
    first, second = LLMRequest(tool_set=mgr.build_tool_set()), LLMRequest(tool_set=mgr.build_tool_set())
    subject._filter_internal_event_tools(batch(internal(subject, reminder_main)), first)
    assert "exec" not in first.tool_set
    assert "exec" in second.tool_set and "exec" in mgr.build_tool_set()
    subject._filter_internal_event_tools(batch(message()), second)
    assert "exec" in second.tool_set


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_internal_batch_is_rejected(subject, reminder_main, reverse):
    messages = [message(), internal(subject, reminder_main)]
    if reverse:
        messages.reverse()
    event = batch(*messages)
    assert subject._get_principal(event).kind is reminder_main.PrincipalKind.LEGACY
    req = LLMRequest(tool_set=manager().build_tool_set())
    subject._filter_internal_event_tools(event, req)
    assert event.is_stopped


def test_previous_instance_envelope_is_rejected(subject, reminder_main):
    event = batch(internal(subject, reminder_main, secret="previous-instance"))
    assert not subject._get_principal(event).trusted
    req = LLMRequest(tool_set=manager().build_tool_set())
    subject._filter_internal_event_tools(event, req)
    assert event.is_stopped


def test_missing_tool_errors_repeat_but_allowed_tool_executes(subject, reminder_main, monkeypatch):
    try:
        from core.plugin.handlers import event_handler_reg
    except ModuleNotFoundError:
        from core.plugin.plugin_handlers import event_handler_reg
    monkeypatch.setattr(event_handler_reg, "get_handlers", lambda *a, **kw: [])
    mgr = manager()
    event = batch(internal(subject, reminder_main))
    req = LLMRequest(tool_set=mgr.build_tool_set())
    subject._filter_internal_event_tools(event, req)
    async def run():
        for name in ("exec", "exec", "schedule_intent_followup", "poke"):
            resp = LLMResponse("")
            resp.tool_calls = [{"id": name, "function": {"name": name, "arguments": "{}"}}]
            await mgr.execute_tool(event, resp, tool_set=req.tool_set)
            content = resp.tool_results[0]["content"]
            if name == "schedule_intent_followup":
                assert content == "audit tool executed"
            else:
                assert f"Tool {name} not implemented" in content
    asyncio.run(run())


@pytest.mark.parametrize("origin,label", [
    ("AUTONOMY_RANDOM_CHECK", "随机自主检查"),
    ("REMINDER_FIRE", "定时任务"),
])
def test_notice_is_routed_without_consuming_pending_user(subject, reminder_main, origin, label):
    from core.chat.message_utils import KiraMessageEvent
    from core.chat.session_buffer import SessionBufferManager
    from core.workflow.src.im.stages.receive import RouteStage

    async def run():
        published = []
        async def publish(event):
            published.append(event)
        adapter = SimpleNamespace(info=SimpleNamespace(name="QQ"), message_types=[], config={"self_id": "bot-1"})
        subject.ctx = SimpleNamespace(
            adapter_mgr=SimpleNamespace(get_adapter=lambda _: adapter),
            event_bus=SimpleNamespace(publish=publish),
        )
        await subject._publish_immediate_notice(
            "QQ:gm:group-1", reminder_main.MessageChain([reminder_main.Text("audit notice")]),
            principal_kind=(reminder_main.PrincipalKind.USER if origin == "REMINDER_FIRE"
                            else reminder_main.PrincipalKind.BOT),
            principal_id="owner-1" if origin == "REMINDER_FIRE" else "",
            origin=getattr(reminder_main.EventOrigin, origin),
        )
        notice = published.pop()
        assert notice.process_strategy == "trigger"
        assert notice.message.chain[0].text == f"[{label}｜系统触发]\n"
        buffers = SessionBufferManager()
        buffers.get_buffer(notice.session.sid).add(KiraMessageEvent(
            message_types=[], timestamp=notice.timestamp, message=message(), adapter=adapter.info,
        ))
        await RouteStage().run(SimpleNamespace(
            services=SimpleNamespace(message_buffer=buffers, event_bus=subject.ctx.event_bus),
            event=notice, sid=notice.session.sid,
        ))
        event = published.pop()
        assert len(event.messages) == 1
        assert buffers.get_buffer(notice.session.sid).get_length() == 1
        req = LLMRequest(tool_set=manager().build_tool_set())
        subject._filter_internal_event_tools(event, req)
        assert ("exec" in req.tool_set) is (origin == "REMINDER_FIRE")
    asyncio.run(run())


def test_mode_switch_preserves_custom_list_and_revokes_exec(subject):
    subject.config.autonomy_allowed_tools += ["custom_tool", "exec"]
    saved = list(subject.config.autonomy_allowed_tools)
    for mode in ("trusted_admin", "observe", "plan_only", "act_with_confirm", "off", "trusted_admin"):
        subject.config.autonomy_mode = mode
        tools = subject.config.effective_autonomy_tools()
        assert ("exec" in tools) is (mode == "trusted_admin")
        assert ("manage_background_exec" in tools) is (mode == "trusted_admin")
        assert ("custom_tool" in tools) is (mode == "trusted_admin")
        assert subject.config.autonomy_allowed_tools == saved


@pytest.mark.parametrize("kind", ["USER", "BOT", "SYSTEM"])
def test_internal_event_policy_does_not_inject_behavior_prompts(subject, reminder_main, kind):
    event = batch(internal(subject, reminder_main, kind=kind))
    req = LLMRequest(tool_set=manager().build_tool_set())
    async def run():
        await subject.enforce_autonomy_tool_policy(event, req)
        await subject.enforce_autonomy_tool_policy(event, req)
    asyncio.run(run())
    assert req.user_prompt == []
    assert req.system_prompt == []


def test_plain_user_request_gets_no_scheduled_context(subject):
    req = LLMRequest(tool_set=manager().build_tool_set())
    asyncio.run(subject.enforce_autonomy_tool_policy(batch(message()), req))
    assert req.user_prompt == []
    assert "exec" in req.tool_set


def test_trusted_mode_does_not_restore_tools_removed_by_core(subject, reminder_main):
    subject.config.autonomy_mode = "trusted_admin"
    req = LLMRequest(tool_set=manager().build_tool_set())
    req.tool_set.remove("exec")
    subject._filter_internal_event_tools(batch(internal(subject, reminder_main)), req)
    assert "exec" not in req.tool_set


def test_forged_marker_cannot_inject_trusted_source_context(subject, reminder_main):
    event = batch(internal(subject, reminder_main, secret="forged"))
    req = LLMRequest(tool_set=manager().build_tool_set())
    asyncio.run(subject.enforce_autonomy_tool_policy(event, req))
    assert event.is_stopped
    assert req.user_prompt == []
