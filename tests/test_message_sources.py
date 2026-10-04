"""Current-batch provenance, conservative creation, and fail-closed entry points."""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
from types import SimpleNamespace

import pytest

from core.chat.message_elements import Text
from core.chat.message_utils import KiraIMMessage, KiraMessageBatchEvent, MessageChain
from core.chat.session import Group, Session, User
from core.prompt_manager import Prompt

from _loader import load_plugin_module

sources_module = load_plugin_module("message_sources")


def message(user_id="alice", *, mentioned=True, text="稍后提醒喝水", **kwargs):
    return KiraIMMessage(
        message_id=f"message-{user_id}", self_id="bot-1", timestamp=1,
        sender=User(user_id=user_id, nickname=user_id.title()),
        group=Group("group-1", "Test group"), chain=MessageChain([Text(text)]),
        is_mentioned=mentioned, **kwargs,
    )


def batch(*messages, adapter="QQ", **kwargs):
    return KiraMessageBatchEvent(
        message_types=[], timestamp=1, adapter=SimpleNamespace(name=adapter),
        session=Session(adapter, "gm", "group-1"), messages=list(messages), **kwargs,
    )


@pytest.fixture
def plugin(reminder_main, monkeypatch, tmp_path):
    monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)
    return reminder_main.ReminderPlugin(SimpleNamespace(), {
        "group_create_policy": "all", "admin_users": ["QQ:alice"],
    })


def rows(plugin, event):
    return json.loads(asyncio.run(plugin.list_message_sources(event)))["sources"]


def test_query_is_read_only_and_does_not_disclose_metadata(plugin):
    first = message(extra={"private": "secret-envelope"}, raw_message={"private": "secret-raw"})
    event = batch(first, message("bob"))
    before = pickle.dumps(event)
    result = json.loads(asyncio.run(plugin.list_message_sources(event)))
    assert pickle.dumps(event) == before
    assert result["complete"]
    assert len(result["sources"]) == 2
    assert result["sources"][0]["fragment"] == "稍后提醒喝水"
    assert result["sources"][0]["nickname"] == "Alice"
    assert result["sources"][0]["is_mentioned"]
    assert "user_id" not in json.dumps(result)
    assert "secret-envelope" not in json.dumps(result)
    assert "secret-raw" not in json.dumps(result)
    assert not plugin._storage.path.exists()
    assert not plugin._delivery_storage.path.exists()


@pytest.mark.parametrize("adapter", ["QQ", "Telegram"])
def test_selected_owner_is_not_the_final_sender(plugin, adapter):
    async def run():
        event = batch(message(), message("bob"), adapter=adapter)
        before = pickle.dumps(event)
        references = json.loads(await plugin.list_message_sources(event))["sources"]
        for index, row in enumerate(references):
            result = await plugin.set_reminder(
                event, content=f"test-{index}", time="2030-01-01 10:00", source_ref=row["source_ref"],
            )
            assert result.startswith("已添加:")
        records = (await plugin._storage.load())[event.sid]
        assert [record["owner_id"] for record in records] == ["alice", "bob"]
        assert all(record["owner_adapter_name"] == adapter for record in records)
        assert all(record["created_by_id"] == record["owner_id"] for record in records)
        assert pickle.dumps(event) == before
    asyncio.run(run())


def test_missing_source_never_falls_back_to_admin_last_sender(plugin):
    async def run():
        for messages in ((message(), message("bob")), (message("bob"), message())):
            event = batch(*messages)
            result = await plugin.set_reminder(event, content="test", time="2030-01-01 10:00")
            assert "list_message_sources" in result
            assert not plugin._get_principal(event).trusted
            assert not plugin._is_admin_user(event)
        assert not plugin._storage.path.exists()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["next_batch", "copy", "session", "adapter", "sender", "mention", "message_id", "replace", "append", "reset"])
def test_refs_cannot_survive_batch_identity_or_metadata_changes(plugin, change):
    event = batch(message(), message("bob"))
    reference = rows(plugin, event)[0]["source_ref"]
    if change == "next_batch":
        event = batch(*event.messages)
    elif change == "copy":
        event = copy.deepcopy(event)
    elif change == "session":
        event.session.session_id = "another-group"
    elif change == "adapter":
        event.adapter.name = "Telegram"
    elif change == "sender":
        event.messages[0].sender.user_id = "another-user"
    elif change == "mention":
        event.messages[0].is_mentioned = False
    elif change == "message_id":
        event.messages[0].message_id = "different-original-message"
    elif change == "replace":
        event.messages[0] = message()
    elif change == "append":
        event.messages.append(message("carol"))
    elif change == "reset":
        plugin._source_resolver().reset()
    result = asyncio.run(plugin.set_reminder(
        event, content="test", time="2030-01-01 10:00", source_ref=reference,
    ))
    assert result.startswith("❌")
    assert not plugin._storage.path.exists()


@pytest.mark.parametrize("reference", ["fake-ref", "中文", "x" * 1000, ["not-a-string"]])
def test_forged_or_invalid_refs_fail_without_writes(plugin, reference):
    result = asyncio.run(plugin.set_reminder(
        batch(message(), message("bob")), content="test", time="2030-01-01 10:00", source_ref=reference,
    ))
    assert result.startswith("❌")
    assert not plugin._storage.path.exists()


def test_admin_only_checks_entire_batch_not_selected_admin(plugin):
    async def run():
        plugin.config.group_create_policy = "admin_only"
        for messages in ((message(), message("bob")), (message("bob"), message())):
            event = batch(*messages)
            refs = json.loads(await plugin.list_message_sources(event))["sources"]
            admin_ref = refs[0 if messages[0].sender.user_id == "alice" else 1]["source_ref"]
            result = await plugin.set_reminder(
                event, content="test", time="2030-01-01 10:00", source_ref=admin_ref,
            )
            assert "待确认请求" in result
        assert not plugin._storage.path.exists()
    asyncio.run(run())


def test_selected_user_still_needs_creation_permission(plugin):
    plugin.config.group_create_policy = "admin_only"
    event = batch(message(), message("bob"))
    result = asyncio.run(plugin.set_reminder(
        event, content="test", time="2030-01-01 10:00", source_ref=rows(plugin, event)[1]["source_ref"],
    ))
    assert "权限拒绝" in result
    assert not plugin._storage.path.exists()


def test_all_authorized_sources_can_create_in_admin_only(plugin):
    plugin.config.group_create_policy = "admin_only"
    plugin.config.authorized_users = ["QQ:bob"]
    event = batch(message(), message("bob"))
    result = asyncio.run(plugin.set_reminder(
        event, content="test", time="2030-01-01 10:00", source_ref=rows(plugin, event)[1]["source_ref"],
    ))
    assert result.startswith("已添加:")


def test_mention_is_per_message_even_for_same_user(plugin):
    plugin.config.group_create_policy = "mentioned_user"
    plugin.config.admin_users = []
    event = batch(message(mentioned=False), message(mentioned=True))
    assert sources_module.requires_source_selection(event)
    reference = rows(plugin, event)[1]["source_ref"]
    result = asyncio.run(plugin.set_reminder(
        event, content="test", time="2030-01-01 10:00", source_ref=reference,
    ))
    assert "待确认请求" in result
    assert not plugin._storage.path.exists()


def test_single_user_same_context_preserves_existing_experience(plugin):
    for event in (batch(message()), batch(message(), message())):
        assert not sources_module.requires_source_selection(event)
        result = asyncio.run(plugin.set_reminder(event, content="test", time="2030-01-01 10:00"))
        assert result.startswith("已添加:")


@pytest.mark.parametrize("internal_last", [False, True])
def test_internal_envelopes_never_grant_mixed_batch_capabilities(plugin, reminder_main, internal_last):
    envelope = plugin._identity.build_envelope(
        origin=reminder_main.EventOrigin.AUTONOMY_FOLLOWUP_DUE,
        principal_kind=reminder_main.PrincipalKind.BOT, principal_id="bot:QQ:bot-1",
        capabilities={"reminder.create", "reminder.manage_all", "intent.manage"},
    )
    internal = message("bot-1", extra=envelope, text="private internal prompt")
    event = batch(*( (message(), internal) if internal_last else (internal, message()) ))
    source_rows = rows(plugin, event)
    internal_row = source_rows[1 if internal_last else 0]
    assert internal_row["source_ref"] is None
    assert not internal_row["fragment"]
    assert envelope["reminder_plugin"]["nonce"] not in json.dumps(source_rows)
    reference = source_rows[0 if internal_last else 1]["source_ref"]
    result = asyncio.run(plugin.set_reminder(
        event, content="test", time="2030-01-01 10:00", source_ref=reference,
    ))
    assert "内部或缺失身份" in result
    assert plugin._get_principal(event).kind is reminder_main.PrincipalKind.LEGACY
    assert not plugin._storage.path.exists()


@pytest.mark.parametrize("missing_id", ["", "unknown", "system"])
def test_unknown_sources_are_non_actionable_and_block_automatic_creation(plugin, missing_id):
    event = batch(message(), message(missing_id))
    source_rows = rows(plugin, event)
    assert source_rows[1]["source_ref"] is None
    result = asyncio.run(plugin.set_reminder(
        event, content="test", time="2030-01-01 10:00", source_ref=source_rows[0]["source_ref"],
    ))
    assert "内部或缺失身份" in result
    assert not plugin._storage.path.exists()


def test_mixed_batch_action_is_rejected_even_for_admin(plugin):
    event = batch(message(), message("bob"))
    result = asyncio.run(plugin.set_reminder(
        event, content="test", time="2030-01-01 10:00", action="perform an action",
        source_ref=rows(plugin, event)[0]["source_ref"],
    ))
    assert "待确认请求" in result
    assert not plugin._storage.path.exists()


@pytest.mark.parametrize("tool,params", [
    ("list_reminders", {}), ("delete_reminder", {"job_id": "private-job"}),
    ("edit_reminder", {"job_id": "private-job", "content": "changed"}),
    ("mark_reminder_important", {"job_id": "private-job"}),
    ("unmark_reminder_important", {"job_id": "private-job"}),
    ("pause_reminder", {"job_id": "private-job"}),
    ("resume_reminder", {"job_id": "private-job"}),
    ("confirm_delete_reminder", {"confirm_token": "existing-token"}),
    ("list_autonomous_intents", {}),
    ("list_delivery_issues", {}),
    ("review_delivery_issue", {"delivery_id": "private-delivery", "decision": "retry"}),
])
def test_other_entry_points_reject_before_reading_private_records(plugin, tool, params):
    plugin._reminder_service = lambda: pytest.fail("must not read reminder data")
    event = batch(message("bob"), message())
    result = asyncio.run(getattr(plugin, tool)(event, **params))
    assert result == sources_module.CONFIRMATION_REQUIRED or "确认未完成" in result
    assert not plugin._storage.path.exists()


@pytest.mark.parametrize("invalid_context", ["batch_adapter", "message_session", "group_id"])
def test_inconsistent_context_cannot_issue_actionable_refs(plugin, invalid_context):
    event = batch(message(), message("bob"))
    if invalid_context == "batch_adapter":
        event.adapter.name = "Telegram"
    elif invalid_context == "message_session":
        event.messages[1].session = Session("Telegram", "gm", "group-1")
    else:
        event.messages[1].group.group_id = "another-group"
    result = json.loads(asyncio.run(plugin.list_message_sources(event)))
    assert any(row["source_type"] == "unknown" for row in result["sources"])
    assert all(row["source_ref"] is None for row in result["sources"] if row["source_type"] == "unknown")


def test_query_bounds_and_large_batches_fail_closed(plugin):
    event = batch(*(message(str(index), text="x" * 1000) for index in range(sources_module.MAX_SOURCES + 1)))
    output = asyncio.run(plugin.list_message_sources(event))
    result = json.loads(output)
    assert not result["complete"]
    assert len(result["sources"]) == sources_module.MAX_SOURCES
    assert all(row["source_ref"] is None for row in result["sources"])
    assert all(row["fragment_truncated"] for row in result["sources"])
    assert all(len(row["fragment"]) <= sources_module.MAX_FRAGMENT for row in result["sources"])
    assert len(output) <= sources_module.MAX_RESULT_CHARS
    with pytest.raises(ValueError, match="截断"):
        plugin._source_resolver().select(event, "fake")


def test_missing_batch_id_and_excessive_serialized_output_issue_no_refs(plugin, monkeypatch):
    event = batch(message(), message("bob"))
    event.event_id = None
    result = json.loads(asyncio.run(plugin.list_message_sources(event)))
    assert not result["complete"]
    assert "批次标识" in result["limitation"]
    event.event_id = "test-batch"
    monkeypatch.setattr(sources_module, "MAX_RESULT_CHARS", 400)
    result = json.loads(asyncio.run(plugin.list_message_sources(event)))
    assert not result["complete"]
    assert "长度上限" in result["limitation"]
    assert all(row["source_ref"] is None for row in result["sources"])


def test_no_dynamic_source_table_is_added_to_request_prompts(plugin, reminder_main):
    async def run():
        event = batch(message(), message("bob"))
        before = pickle.dumps(event)
        prompt = Prompt("static-prefix", name="output")
        request = SimpleNamespace(system_prompt=[prompt], user_prompt=[], tool_set=None)
        await plugin.inject_usage_prompt(event, request)
        await plugin.enforce_autonomy_tool_policy(event, request)
        await plugin.inject_delivery_issues(event, request)
        assert request.system_prompt[0] is prompt
        assert len(request.system_prompt) == 2
        assert request.system_prompt[1].content == plugin._get_usage_prompt()
        assert "src_" not in str(request.system_prompt)
        assert request.user_prompt == []
        await plugin.list_message_sources(event)
        assert len(request.system_prompt) == 2
        assert pickle.dumps(event) == before
    asyncio.run(run())


def test_terminate_invalidates_refs_without_new_background_tasks(plugin):
    event = batch(message(), message("bob"))
    reference = rows(plugin, event)[0]["source_ref"]
    asyncio.run(plugin.terminate())
    with pytest.raises(ValueError, match="失效"):
        plugin._source_resolver().select(event, reference)


def test_parent_internal_envelope_cannot_hide_user_and_internal_mix(plugin, reminder_main):
    envelope = plugin._identity.build_envelope(
        origin=reminder_main.EventOrigin.REMINDER_FIRE,
        principal_kind=reminder_main.PrincipalKind.BOT, principal_id="bot:QQ:bot-1",
        capabilities={"reminder.create"},
    )
    event = batch(message("bot-1"), message("bot-1", extra=envelope), extra=envelope)
    assert sources_module.requires_source_selection(event)
    assert plugin._get_principal(event).kind is reminder_main.PrincipalKind.LEGACY


def test_empty_query_and_long_non_text_chains_are_bounded(plugin):
    empty = batch()
    result = json.loads(asyncio.run(plugin.list_message_sources(empty)))
    assert not result["complete"]
    assert not result["sources"]
    item = message()
    item.chain = MessageChain([SimpleNamespace() for _ in range(1000)] + [Text("hidden-text")])
    result = json.loads(asyncio.run(plugin.list_message_sources(batch(item))))
    assert result["sources"][0]["fragment_truncated"]
    assert result["sources"][0]["fragment"] == ""


def test_registered_source_tool_uses_optional_static_schema(reminder_main):
    from core.plugin.plugin_registry import _plugin_components
    tools = _plugin_components["reminder_plugin"].tools
    assert "list_message_sources" in tools
    schema = tools["set_reminder"]["parameters"]
    assert schema["properties"]["source_ref"]["type"] == "string"
    assert "source_ref" not in schema["required"]


def test_permissions_are_rechecked_after_source_query(plugin):
    plugin.config.group_create_policy = "admin_only"
    plugin.config.authorized_users = ["QQ:bob"]
    event = batch(message(), message("bob"))
    reference = rows(plugin, event)[0]["source_ref"]
    plugin.config.admin_users = []
    result = asyncio.run(plugin.set_reminder(
        event, content="test", time="2030-01-01 10:00", source_ref=reference,
    ))
    assert "权限拒绝" in result
    assert not plugin._storage.path.exists()


def test_conflicting_event_sid_and_session_sid_is_not_actionable(plugin):
    original = batch(message(), message("bob"))
    event = SimpleNamespace(**vars(original), sid="QQ:gm:another-group")
    result = json.loads(asyncio.run(plugin.list_message_sources(event)))
    assert all(row["source_ref"] is None for row in result["sources"])
    assert all(row["source_type"] == "unknown" for row in result["sources"])
