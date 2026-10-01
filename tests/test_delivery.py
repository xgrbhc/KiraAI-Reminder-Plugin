"""Delivery receipts and recovery decisions use only temporary files."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import attach_delivery


SID = "qq:dm:10001"


def _record(*, action: str = "", repeat: str = "none") -> dict:
    return {
        "job_id": "job-1",
        "content": "check the plan",
        "time": "2099-01-01 10:00",
        "repeat": repeat,
        "owner_type": "user",
        "owner_id": "10001",
        "action": action,
    }


def _plugin(reminder_main, tmp_path: Path):
    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
    attach_delivery(plugin, reminder_main, tmp_path)
    plugin._identity = reminder_main.IdentityResolver("test-secret")
    plugin.config = SimpleNamespace(
        admin_users=[], allowed_sessions=[SID], autonomy_mode="plan_only"
    )
    plugin._allowed_autonomy_sessions = lambda: [SID]
    plugin._is_autonomous_reminder = lambda _record: False
    return plugin


def _event(plugin, reminder_main, delivery_id: str = "", user_id: str = "10001"):
    extra = (
        plugin._identity.build_envelope(
            origin=reminder_main.EventOrigin.REMINDER_FIRE,
            principal_kind=reminder_main.PrincipalKind.USER,
            principal_id=user_id,
            delivery_id=delivery_id,
        )
        if delivery_id else {}
    )
    message = SimpleNamespace(
        sender=SimpleNamespace(user_id=user_id, nickname="owner"),
        self_id="bot-1", extra=extra, is_mentioned=True,
    )
    return SimpleNamespace(sid=SID, messages=[message])


def test_only_normal_model_response_acknowledges_one_time_reminder(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        await plugin._storage.save({SID: [record]})
        delivery_id = await plugin._delivery.begin(SID, record)
        event = _event(plugin, reminder_main, delivery_id)

        await plugin.acknowledge_delivery(
            event, reminder_main.LLMResponse("[ProviderError]", provider_call_succeeded=False)
        )
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "unconfirmed"
        assert (await plugin._storage.load())[SID] == [record]

        await plugin.acknowledge_delivery(event, reminder_main.LLMResponse(""))
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "llm_received"
        assert (await plugin._storage.load())[SID] == []

    asyncio.run(run())


def test_untrusted_or_unmatched_response_cannot_ack(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        await plugin._storage.save({SID: [record]})
        delivery_id = await plugin._delivery.begin(SID, record)
        await plugin.acknowledge_delivery(
            _event(plugin, reminder_main), reminder_main.LLMResponse("ok")
        )
        await plugin.acknowledge_delivery(
            _event(plugin, reminder_main, "not-the-id"), reminder_main.LLMResponse("ok")
        )
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "awaiting_llm"
        assert (await plugin._storage.load())[SID] == [record]

        forged = _event(plugin, reminder_main, delivery_id)
        forged.messages[0].extra["reminder_plugin"]["nonce"] = "wrong"
        await plugin.acknowledge_delivery(forged, reminder_main.LLMResponse("ok"))
        assert (await plugin._storage.load())[SID] == [record]

    asyncio.run(run())


def test_reconcile_keeps_overdue_without_replaying_and_expires_awaiting(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        old = _record()
        old["time"] = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        await plugin._storage.save({SID: [old]})
        await plugin._delivery.reconcile()
        issue = (await plugin._delivery_storage.load())[SID][0]
        assert issue["status"] == "legacy_unconfirmed"
        assert (await plugin._storage.load())[SID] == [old]

        async with plugin._delivery_storage.modify() as state:
            state[SID][0]["status"] = "awaiting_llm"
            state[SID][0]["created_at"] = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat()
        await plugin._delivery.reconcile()
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "unconfirmed"

    asyncio.run(run())


def test_recovery_scope_and_retry_rules(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        await plugin._storage.save({SID: [record]})
        delivery_id = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, delivery_id, "failed")
        owner = plugin._get_principal(_event(plugin, reminder_main, user_id="10001"))
        other = plugin._get_principal(_event(plugin, reminder_main, user_id="20002"))
        assert len(await plugin._delivery.list_issues(SID, owner, [], [SID])) == 1
        assert await plugin._delivery.list_issues(SID, other, [], [SID]) == []

        msg, old = await plugin._delivery.resolve(SID, delivery_id, owner, [], [SID], "retry")
        assert old is not None and msg == "已记录处理决定"
        state = (await plugin._delivery_storage.load())[SID]
        assert [entry["status"] for entry in state] == ["resolved", "awaiting_llm"]
        assert old["retry_delivery_id"] == state[1]["delivery_id"]

        await plugin._delivery.mark(SID, state[1]["delivery_id"], "unconfirmed")
        msg, result = await plugin._delivery.resolve(
            SID, state[1]["delivery_id"], owner, [], [SID], "retry"
        )
        assert result is None and "不能自动重试" in msg

    asyncio.run(run())


def test_autonomous_bot_gets_only_scoped_private_review(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record(action="perform an action")
        await plugin._storage.save({SID: [record]})
        delivery_id = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, delivery_id, "failed")
        bot = reminder_main.PrincipalContext(
            kind=reminder_main.PrincipalKind.BOT,
            principal_id="bot:qq:bot-1",
            origin=reminder_main.EventOrigin.AUTONOMY_DAILY_REFLECTION,
            session_id=SID,
            trusted=True,
            capabilities=frozenset({"intent.manage"}),
        )
        assert len(await plugin._delivery.list_issues(SID, bot, [], [SID])) == 1
        assert await plugin._delivery.list_issues(SID, bot, [], []) == []
        msg, result = await plugin._delivery.resolve(SID, delivery_id, bot, [], [SID], "retry")
        assert result is None and "不能自动重试" in msg

    asyncio.run(run())


def test_recovery_prompt_is_owner_scoped_and_skips_mixed_batch(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        await plugin._storage.save({SID: [record]})
        delivery_id = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, delivery_id, "failed")

        owner_request = SimpleNamespace(system_prompt=[])
        await plugin.inject_delivery_issues(_event(plugin, reminder_main), owner_request)
        assert len(owner_request.system_prompt) == 1
        assert delivery_id in owner_request.system_prompt[0].content

        other_request = SimpleNamespace(system_prompt=[])
        await plugin.inject_delivery_issues(
            _event(plugin, reminder_main, user_id="20002"), other_request
        )
        assert other_request.system_prompt == []

        mixed = _event(plugin, reminder_main)
        mixed.messages.insert(0, SimpleNamespace(sender=SimpleNamespace(user_id="20002")))
        mixed_request = SimpleNamespace(system_prompt=[])
        await plugin.inject_delivery_issues(mixed, mixed_request)
        assert mixed_request.system_prompt == []

    asyncio.run(run())


def test_web_review_of_uncertain_action_requires_manual_api_and_keeps_new_attempt(
    reminder_main, tmp_path: Path
):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record(action="perform an action")
        await plugin._storage.save({SID: [record]})
        delivery_id = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, delivery_id, "unconfirmed")
        published = []

        async def fake_fire(sid, reminder, delivery_id=None):
            published.append((sid, reminder, delivery_id))

        plugin._fire_reminder = fake_fire
        response = await plugin.api_get_deliveries(SID)
        assert response["status"] == "ok"
        assert response["data"][0]["status"] == "unconfirmed"

        result = await plugin.api_review_delivery(
            "retry", {"session_id": SID, "delivery_id": delivery_id}
        )
        assert result["status"] == "ok"
        assert "等待模型确认" in result["msg"]
        state = (await plugin._delivery_storage.load())[SID]
        assert [entry["status"] for entry in state] == ["resolved", "awaiting_llm"]
        assert published == [(SID, record, state[1]["delivery_id"])]

    asyncio.run(run())


def test_dismiss_cleanup_can_resume_after_interrupted_write(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        await plugin._storage.save({SID: [record]})
        delivery_id = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, delivery_id, "failed")
        owner = plugin._get_principal(_event(plugin, reminder_main))
        original_cleanup = plugin._delivery._cleanup_confirmed

        async def interrupted(_sid, _entry):
            raise OSError("simulated interruption")

        plugin._delivery._cleanup_confirmed = interrupted
        with pytest.raises(OSError):
            await plugin._delivery.resolve(SID, delivery_id, owner, [], [SID], "dismiss")
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "resolved"
        assert (await plugin._storage.load())[SID] == [record]

        plugin._delivery._cleanup_confirmed = original_cleanup
        await plugin._delivery.reconcile()
        assert (await plugin._storage.load())[SID] == []

    asyncio.run(run())


def test_completed_recurring_receipts_are_bounded(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record(repeat="daily")
        await plugin._storage.save({SID: [record]})
        entries = [
            {
                "delivery_id": f"closed-{index}",
                "job_id": "job-1",
                "status": "llm_received",
                "updated_at": f"2099-01-01T00:{index // 60:02d}:{index % 60:02d}",
                "reminder": record,
            }
            for index in range(110)
        ]
        entries.append({
            "delivery_id": "open", "job_id": "job-1", "status": "unconfirmed",
            "updated_at": "2099-01-01T02:00:00", "reminder": record,
        })
        await plugin._delivery_storage.save({SID: entries})
        await plugin._delivery._prune_closed(SID)
        remaining = (await plugin._delivery_storage.load())[SID]
        assert len(remaining) == 101
        assert any(entry["delivery_id"] == "open" for entry in remaining)

    asyncio.run(run())


def test_late_receipt_does_not_delete_an_edited_reminder(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        original = _record()
        await plugin._storage.save({SID: [original]})
        delivery_id = await plugin._delivery.begin(SID, original)
        edited = {**original, "content": "new plan"}
        await plugin._storage.save({SID: [edited]})

        await plugin.acknowledge_delivery(
            _event(plugin, reminder_main, delivery_id), reminder_main.LLMResponse("ok")
        )
        assert (await plugin._storage.load())[SID] == [edited]
        await plugin._delivery.reconcile()
        assert (await plugin._storage.load())[SID] == [edited]

    asyncio.run(run())


def test_late_receipt_keeps_an_overdue_edit_visible_for_review(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        original = _record()
        original["time"] = (dt.datetime.now() - dt.timedelta(minutes=6)).strftime(
            "%Y-%m-%d %H:%M"
        )
        await plugin._storage.save({SID: [original]})
        delivery_id = await plugin._delivery.begin(SID, original)
        edited = {**original, "content": "updated plan"}
        await plugin._storage.save({SID: [edited]})

        await plugin.acknowledge_delivery(
            _event(plugin, reminder_main, delivery_id), reminder_main.LLMResponse("ok")
        )
        await plugin._delivery.reconcile()
        assert (await plugin._storage.load())[SID] == [edited]
        assert [
            entry["status"] for entry in (await plugin._delivery_storage.load())[SID]
        ] == ["llm_received", "legacy_unconfirmed"]

    asyncio.run(run())


def test_edit_does_not_coalesce_with_an_older_open_repeat(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        original = _record(repeat="daily")
        await plugin._storage.save({SID: [original]})
        old_id = await plugin._delivery.begin(SID, original)
        edited = {**original, "content": "updated plan"}
        await plugin._storage.save({SID: [edited]})

        new_id = await plugin._delivery.begin(SID, edited)
        assert new_id and new_id != old_id
        assert [
            entry["status"] for entry in (await plugin._delivery_storage.load())[SID]
        ] == ["awaiting_llm", "awaiting_llm"]

    asyncio.run(run())


def test_reconcile_does_not_preempt_a_fresh_due_job(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        record["time"] = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M"
        )
        await plugin._storage.save({SID: [record]})

        await plugin._delivery.reconcile()
        assert await plugin._delivery_storage.load() == {}
        delivery_id = await plugin._delivery.begin(SID, record)
        assert delivery_id
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "awaiting_llm"

    asyncio.run(run())


def test_paused_reminder_cannot_be_retried(reminder_main, tmp_path: Path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        await plugin._storage.save({SID: [record]})
        delivery_id = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, delivery_id, "failed")
        paused = {**record, "paused": True}
        await plugin._storage.save({SID: [paused]})
        owner = plugin._get_principal(_event(plugin, reminder_main))

        message, result = await plugin._delivery.resolve(
            SID, delivery_id, owner, [], [SID], "retry"
        )
        assert result is None and "已暂停" in message
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "failed"

    asyncio.run(run())


def test_closed_retry_history_is_bounded_while_one_time_reminder_remains(
    reminder_main, tmp_path: Path
):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        await plugin._storage.save({SID: [record]})
        entries = [
            {
                "delivery_id": f"retry-{index}",
                "job_id": record["job_id"],
                "status": "resolved",
                "resolution": "retry",
                "updated_at": f"2099-01-01T00:{index // 60:02d}:{index % 60:02d}",
                "reminder": record,
            }
            for index in range(110)
        ]
        entries.append({
            "delivery_id": "current",
            "job_id": record["job_id"],
            "status": "unconfirmed",
            "updated_at": "2099-01-01T02:00:00",
            "reminder": record,
        })
        await plugin._delivery_storage.save({SID: entries})

        await plugin._delivery._prune_closed(SID)
        remaining = (await plugin._delivery_storage.load())[SID]
        assert len(remaining) == 101
        assert any(entry["delivery_id"] == "current" for entry in remaining)
        assert (await plugin._storage.load())[SID] == [record]

    asyncio.run(run())


@pytest.mark.parametrize("failure_mode", ["provider_error", "reload_timeout"])
def test_unconfirmed_delivery_survives_fresh_plugin_without_replay(
    reminder_main, tmp_path: Path, failure_mode: str
):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        plugin._fire_semaphore = asyncio.Semaphore(3)
        record = _record()
        record["time"] = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M"
        )
        await plugin._storage.save({SID: [record]})
        published = []

        async def publish(**kwargs):
            published.append(kwargs)

        plugin._publish_immediate_notice = publish
        await plugin._fire_reminder(SID, record)
        assert len(published) == 1
        delivery_id = published[0]["delivery_id"]
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "awaiting_llm"

        if failure_mode == "provider_error":
            await plugin.acknowledge_delivery(
                _event(plugin, reminder_main, delivery_id),
                reminder_main.LLMResponse(
                    "[ProviderError]", provider_call_succeeded=False
                ),
            )
        else:
            async with plugin._delivery_storage.modify() as state:
                state[SID][0]["created_at"] = (
                    dt.datetime.now() - dt.timedelta(minutes=6)
                ).isoformat()

        restarted = _plugin(reminder_main, tmp_path)
        restarted._identity = reminder_main.IdentityResolver("new-process-secret")
        scheduled = []
        restarted._scheduler = SimpleNamespace(
            add_job=lambda *args, **kwargs: scheduled.append((args, kwargs))
        )
        await restarted._restore_jobs()

        assert scheduled == []
        assert len(published) == 1
        assert (await restarted._storage.load())[SID] == [record]
        issues = (await restarted._delivery_storage.load())[SID]
        assert len(issues) == 1
        assert issues[0]["delivery_id"] == delivery_id
        assert issues[0]["status"] == "unconfirmed"
        response = await restarted.api_get_deliveries(SID)
        assert response["status"] == "ok"
        assert response["data"][0]["status"] == "unconfirmed"

    asyncio.run(run())
