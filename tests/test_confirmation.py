"""Low-friction consent, provenance, transactional guards, and compatibility."""

import asyncio
import json
import pickle
from types import SimpleNamespace

import pytest

from core.chat.message_elements import Reply, Text
from core.chat.message_utils import MessageChain

from _events import batch, message, observe
from _loader import load_plugin_module

confirmation = load_plugin_module("confirmation")


@pytest.fixture
def plugin(reminder_main, tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)
    return reminder_main.ReminderPlugin(SimpleNamespace(), {
        "group_create_policy": "all", "admin_users": ["QQ:alice"],
    })


async def create(plugin, *, important=False, user="alice", group="group-1"):
    event = batch(message(user, group=group))
    assert "已添加" in await plugin.set_reminder(event, "private reminder", "2030-01-01 10:00")
    record = (await plugin._storage.load())[event.sid][-1]
    if important:
        await plugin.mark_reminder_important(event, record["job_id"])
    return event, record


async def request(plugin, operation="list_reminders", params=None, event=None):
    event = event or batch(message(), message("bob"))
    ref = json.loads(await plugin.list_message_sources(event))["sources"][0]["source_ref"]
    result = await getattr(plugin, operation)(event, source_ref=ref, **(params or {}))
    assert "待确认请求" in result
    items = json.loads(await plugin.list_pending_reminder_requests(event))
    return items[-1]["request_id"], result


@pytest.mark.parametrize("reply", ["确认", "同意", "确认！"])
def test_unique_request_needs_only_one_short_reply(plugin, reply):
    async def run():
        await create(plugin)
        event = batch(message(), message("bob"))
        before = pickle.dumps(event)
        key, result = await request(plugin, event=event)
        assert "private reminder" not in result
        assert "本群" in result
        assert pickle.dumps(event) == before
        approved = await observe(plugin, message(text=reply))
        mixed = batch(approved.messages[0], message("bob"))
        assert "private reminder" in await plugin.confirm_reminder_request(mixed, key)
        assert "确认未完成" in await plugin.confirm_reminder_request(mixed, key)
    asyncio.run(run())


@pytest.mark.parametrize("problem", ["no_hook", "wrong_user", "wrong_adapter", "wrong_group", "wrong_bot",
                                    "unmentioned", "negation", "question", "quoted", "old_message", "changed_text"])
def test_approval_cannot_be_fabricated_or_borrowed(plugin, problem):
    async def run():
        msg = message(text="确认")
        if problem == "old_message":
            await observe(plugin, msg)
        key, _ = await request(plugin)
        adapter = "QQ"
        if problem == "wrong_user":
            msg.sender.user_id = "bob"
        elif problem == "wrong_adapter":
            adapter = "Telegram"
        elif problem == "wrong_group":
            msg.group.group_id = "other"
        elif problem == "wrong_bot":
            msg.self_id = "other-bot"
        elif problem == "unmentioned":
            msg.is_mentioned = False
        elif problem == "negation":
            msg.chain = MessageChain([Text("不要确认")])
        elif problem == "question":
            msg.chain = MessageChain([Text("确认吗？")])
        elif problem == "quoted":
            msg.chain = MessageChain([Reply("quoted", chain=MessageChain([Text("确认")])), Text("再等等")])
        event = batch(msg, adapter=adapter) if problem == "no_hook" else await observe(plugin, msg, adapter)
        if problem == "changed_text":
            msg.chain = MessageChain([Text("不用了")])
        assert "确认未完成" in await plugin.confirm_reminder_request(event, key)
    asyncio.run(run())


def test_multiple_requests_require_number_and_do_not_approve_all(plugin):
    async def run():
        key1, _ = await request(plugin)
        key2, _ = await request(plugin, "list_delivery_issues")
        event = await observe(plugin, message(text="确认"))
        assert "确认未完成" in await plugin.confirm_reminder_request(event, key1)
        assert "确认未完成" in await plugin.confirm_reminder_request(event, key2)
        event = await observe(plugin, message(text=f"确认 {key1}"))
        assert "确认未完成" not in await plugin.confirm_reminder_request(event, key1)
        assert "确认未完成" in await plugin.confirm_reminder_request(event, key2)
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["pause_reminder", "resume_reminder", "edit_reminder",
                                       "mark_reminder_important", "delete_reminder"])
def test_targeted_tools_execute_saved_parameters_once(plugin, operation):
    async def run():
        event, record = await create(plugin)
        if operation == "resume_reminder":
            await plugin.pause_reminder(event, record["job_id"])
        params = {"job_id": record["job_id"]}
        if operation == "edit_reminder":
            params["content"] = "approved content"
        key, _ = await request(plugin, operation, params)
        approved = await observe(plugin, message(text="确认"))
        result = await plugin.confirm_reminder_request(approved, key, content="injected", confirmed=True)
        assert "❌" not in result and "injected" not in result
        assert "确认未完成" in await plugin.confirm_reminder_request(approved, key)
        data = (await plugin._storage.load())[event.sid]
        if operation == "delete_reminder":
            assert not data
        if operation == "edit_reminder":
            assert data[0]["content"] == "approved content"
    asyncio.run(run())


@pytest.mark.parametrize("change", ["content", "owner", "deleted", "batch_member", "revoked_admin"])
def test_waiting_target_or_permission_changes_reject_execution(plugin, change):
    async def run():
        event, record = await create(plugin, user="bob")
        async with plugin._storage.modify() as data:
            data[event.sid][0]["random_batch_id"] = "test-batch"
        key, _ = await request(plugin, "delete_reminder", {"job_id": record["job_id"], "delete_batch": True})
        if change == "revoked_admin":
            plugin.config.admin_users = []
        else:
            async with plugin._storage.modify() as data:
                if change == "deleted":
                    data[event.sid] = []
                elif change == "batch_member":
                    added = dict(data[event.sid][0], job_id="new-member")
                    data[event.sid].append(added)
                else:
                    data[event.sid][0]["owner_id" if change == "owner" else "content"] = "changed"
        before = await plugin._storage.load()
        approved = await observe(plugin, message(text="确认"))
        result = await plugin.confirm_reminder_request(approved, key)
        assert "已删除" not in result
        assert await plugin._storage.load() == before
    asyncio.run(run())


def test_guard_checks_after_storage_lock_acquisition(plugin):
    async def run():
        event, record = await create(plugin)
        key, _ = await request(plugin, "pause_reminder", {"job_id": record["job_id"]})
        approved = await observe(plugin, message(text="确认"))
        async with plugin._storage.modify() as data:
            task = asyncio.create_task(plugin.confirm_reminder_request(approved, key))
            await asyncio.sleep(0)
            data[event.sid][0]["content"] = "changed while waiting"
        assert "已变化" in await task
        assert not (await plugin._storage.load())[event.sid][0].get("paused")
    asyncio.run(run())


@pytest.mark.parametrize("end", ["cancel", "expiry", "reload"])
def test_cancel_expiry_and_terminate_invalidate_requests(plugin, end):
    async def run():
        now = [0]
        plugin._confirmation_routes().pending = confirmation.PendingRequests(clock=lambda: now[0])
        key, _ = await request(plugin)
        approved = await observe(plugin, message(text="确认"))
        if end == "cancel":
            await observe(plugin, message(text="取消"))
        elif end == "expiry":
            now[0] = 300
        else:
            await plugin.terminate()
        assert "确认未完成" in await plugin.confirm_reminder_request(approved, key)
    asyncio.run(run())


def test_duplicates_limits_and_concurrent_consumption(plugin, monkeypatch):
    async def run():
        key, _ = await request(plugin)
        same, _ = await request(plugin)
        assert key == same
        monkeypatch.setattr(confirmation, "MAX_PER_ACTOR", 1)
        event = batch(message(), message("bob"))
        ref = json.loads(await plugin.list_message_sources(event))["sources"][0]["source_ref"]
        assert "过多" in await plugin.list_delivery_issues(event, source_ref=ref)
        approved = await observe(plugin, message(text="确认"))
        results = await asyncio.gather(*(plugin.confirm_reminder_request(approved, key) for _ in range(2)))
        assert sum("确认未完成" not in result for result in results) == 1
    asyncio.run(run())


@pytest.mark.parametrize("decision", ["retry", "dismiss", "defer"])
def test_delivery_confirmation_keeps_sensitive_record_web_only(plugin, decision):
    async def run():
        event, record = await create(plugin)
        delivery_id = await plugin._delivery.begin(event.sid, record)
        await plugin._delivery.mark(event.sid, delivery_id, "unconfirmed")
        key, _ = await request(plugin, "review_delivery_issue", {"delivery_id": delivery_id, "decision": decision})
        approved = await observe(plugin, message(text="确认"))
        result = await plugin.confirm_reminder_request(approved, key)
        assert {"retry": "重试", "dismiss": "WebUI", "defer": "已延后"}[decision] in result
        assert (await plugin._storage.load())[event.sid]
    asyncio.run(run())


def test_changed_delivery_refuses_confirmed_retry(plugin):
    async def run():
        event, record = await create(plugin)
        delivery_id = await plugin._delivery.begin(event.sid, record)
        await plugin._delivery.mark(event.sid, delivery_id, "failed")
        key, _ = await request(plugin, "review_delivery_issue", {"delivery_id": delivery_id, "decision": "retry"})
        await plugin._delivery.mark(event.sid, delivery_id, "unconfirmed")
        approved = await observe(plugin, message(text="确认"))
        assert "已变化" in await plugin.confirm_reminder_request(approved, key)
        assert len((await plugin._delivery_storage.load())[event.sid]) == 1
    asyncio.run(run())


def test_failed_safe_delivery_can_retry_once_after_confirmation(plugin):
    async def run():
        event, record = await create(plugin)
        delivery_id = await plugin._delivery.begin(event.sid, record)
        await plugin._delivery.mark(event.sid, delivery_id, "failed")
        fired = []
        async def fire(*args, **kwargs):
            fired.append(kwargs["delivery_id"])
        plugin._fire_reminder = fire
        key, _ = await request(plugin, "review_delivery_issue", {"delivery_id": delivery_id, "decision": "retry"})
        approved = await observe(plugin, message(text="确认"))
        assert "已记录" in await plugin.confirm_reminder_request(approved, key)
        assert "确认未完成" in await plugin.confirm_reminder_request(approved, key)
        assert len(fired) == 1
    asyncio.run(run())


def test_web_token_rejects_changed_target(plugin):
    async def run():
        event, record = await create(plugin, important=True)
        result = await plugin.api_action_reminders("delete", {"session_id": event.sid, "job_id": record["job_id"]})
        token = result["msg"].rsplit(" ", 1)[-1]
        async with plugin._storage.modify() as data:
            data[event.sid][0]["content"] = "new target"
        result = await plugin.api_confirm_delete_reminder({"session_id": event.sid, "confirm_token": token})
        assert result["status"] == "error"
        assert (await plugin._storage.load())[event.sid]
    asyncio.run(run())


def test_internal_and_unknown_messages_cannot_confirm(plugin):
    async def run():
        key, _ = await request(plugin)
        internal = message(text="确认", is_notice=True)
        approved = await observe(plugin, internal)
        assert "确认未完成" in await plugin.confirm_reminder_request(approved, key)
        unknown = message("unknown", "确认")
        approved = await observe(plugin, unknown)
        assert "确认未完成" in await plugin.confirm_reminder_request(approved, key)
    asyncio.run(run())


def test_request_params_are_copied_and_bounded(plugin):
    async def run():
        context = load_plugin_module("message_sources").inspect_user_message(batch(message()), message())
        params = {"content": "original"}
        item = await plugin._confirmation_routes().pending.create(context, "set_reminder", params)
        params["content"] = "changed"
        assert item.params["content"] == "original"
        with pytest.raises(ValueError, match="过长"):
            await plugin._confirmation_routes().pending.create(context, "set_reminder", {"content": "x" * 4097})
    asyncio.run(run())


def test_explicit_number_and_earlier_batch_proof_cannot_approve_new_request(plugin):
    async def run():
        key, _ = await request(plugin)
        approved = await observe(plugin, message(text=f"确认 {key}"))
        unrelated = batch(message())
        assert "确认未完成" in await plugin.confirm_reminder_request(unrelated, key)
        assert "确认未完成" not in await plugin.confirm_reminder_request(approved, key)
        new_key, _ = await request(plugin)
        assert new_key != key
        assert "确认未完成" in await plugin.confirm_reminder_request(approved, new_key)
    asyncio.run(run())


def test_delete_wording_does_not_authorize_a_different_operation(plugin):
    async def run():
        key, _ = await request(plugin)
        approved = await observe(plugin, message(text="确认删除"))
        assert "确认未完成" in await plugin.confirm_reminder_request(approved, key)
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["close", "replace", "remove"])
def test_autonomous_cleanup_cannot_remove_important_followups(plugin, operation):
    async def run():
        coordinator = plugin._autonomy_coordinator()
        sid = "QQ:gm:group-1"
        await coordinator.create_intent(sid, "test")
        state = await plugin._autonomy_storage.load()
        intent_id = state["sessions"][sid]["intents"][0]["id"]
        principal = plugin._get_principal(batch(message()))
        await coordinator.schedule_intent_followup(sid, principal, intent_id, "2030-01-01 10:00")
        async with plugin._storage.modify() as data:
            data[sid][0]["important"] = True
        before = await plugin._storage.load()
        if operation == "close":
            result = await coordinator.close_intent(sid, intent_id)
            assert "重要" in result
            assert (await plugin._autonomy_storage.load())["sessions"][sid]["intents"][0]["status"] == "active"
        elif operation == "replace":
            assert "重要" in await coordinator.schedule_intent_followup(sid, principal, intent_id, "2030-01-02 10:00")
        else:
            with pytest.raises(ValueError, match="重要"):
                await coordinator.remove_autonomous_reminders(sid, intent_id)
        assert await plugin._storage.load() == before
    asyncio.run(run())


@pytest.mark.parametrize("group", [None, "group-1"])
def test_important_delete_and_unmark_do_not_allow_model_self_confirmation(plugin, group):
    async def run():
        event, record = await create(plugin, important=True, group=group)
        result = await plugin.delete_reminder(event, record["job_id"])
        key = json.loads(await plugin.list_pending_reminder_requests(event))[0]["request_id"]
        assert "确认删除" in result and "private reminder" not in result
        assert "确认未完成" in await plugin.confirm_delete_reminder(event, key)
        approved = await observe(plugin, message(text="确认", group=group))
        assert "确认未完成" in await plugin.confirm_delete_reminder(approved, key)
        approved = await observe(plugin, message(text="确认删除", group=group))
        assert "已删除" in await plugin.confirm_delete_reminder(approved, key)
        event, record = await create(plugin, important=True, group=group)
        result = await plugin.unmark_reminder_important(event, record["job_id"])
        key = json.loads(await plugin.list_pending_reminder_requests(event))[0]["request_id"]
        assert "待确认请求" in result
        assert (await plugin._storage.load())[event.sid][0]["important"]
        approved = await observe(plugin, message(text="确认", group=group))
        assert "取消重要标记" in await plugin.confirm_reminder_request(approved, key)
    asyncio.run(run())


def test_authenticated_web_deletion_still_uses_existing_token_without_chat(plugin):
    async def run():
        event, record = await create(plugin, important=True)
        result = await plugin.api_action_reminders("delete", {"session_id": event.sid, "job_id": record["job_id"]})
        token = result["msg"].rsplit(" ", 1)[-1]
        assert token in plugin._pending
        result = await plugin.api_confirm_delete_reminder({"session_id": event.sid, "confirm_token": token})
        assert result["status"] == "ok"
        assert not (await plugin._storage.load())[event.sid]
    asyncio.run(run())


@pytest.mark.parametrize("policy", ["all", "admin_only", "mentioned_user"])
def test_confirmation_rechecks_creation_and_action_policy(plugin, policy):
    async def run():
        event = batch(message(), message("bob"))
        plugin.config.group_create_policy = policy
        ref = json.loads(await plugin.list_message_sources(event))["sources"][0]["source_ref"]
        result = await plugin.set_reminder(event, "test", "2030-01-01 10:00", action="do something", source_ref=ref)
        assert "待确认请求" in result
        key = json.loads(await plugin.list_pending_reminder_requests(event))[0]["request_id"]
        plugin.config.admin_users = []
        plugin.config.action_policy = "admin_only"
        approved = await observe(plugin, message(text="确认"))
        assert "已添加" not in await plugin.confirm_reminder_request(approved, key)
        assert not plugin._storage.path.exists()
    asyncio.run(run())
