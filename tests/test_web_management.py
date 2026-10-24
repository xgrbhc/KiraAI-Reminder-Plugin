"""Web editor contracts, concurrent changes, and scheduler rollback."""

import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from _events import batch, message
from _loader import load_plugin_module

web = load_plugin_module("web_management")


@pytest.fixture
def plugin(reminder_main, tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)
    instance = reminder_main.ReminderPlugin(SimpleNamespace(), {})
    instance._scheduler = AsyncIOScheduler()
    return instance


async def seed(plugin, **extra):
    event = batch(message(group=None))
    assert "已添加" in await plugin.set_reminder(event, "test", "2030-01-01 10:00")
    async with plugin._storage.modify() as data:
        record = data[event.sid][0]
        record.update(extra)
    result = await plugin.api_get_reminders(event.sid)
    assert result["server_timezone"] and result["server_timezone"] != "None"
    item = result["data"][0]
    return {"session_id": event.sid, "job_id": item["job_id"], "revision": item["web_revision"]}


def test_edit_and_star_preserve_metadata_and_persist(plugin):
    async def run():
        payload = await seed(plugin, important=True)
        original = (await plugin._storage.load())[payload["session_id"]][0]
        result = await plugin.api_action_reminders("edit", dict(payload, changes={
            "content": "重要提醒：拒绝出错只是文本", "category": "生活", "repeat": "interval", "interval_minutes": 12,
        }))
        assert result["status"] == "ok"
        current = (await plugin._storage.load())[payload["session_id"]][0]
        for key in ("job_id", "creator_id", "owner_id", "source", "important", "time"):
            assert current.get(key) == original.get(key)
        assert current["interval_minutes"] == 12
        assert "web_revision" not in current
        assert plugin._scheduler.get_job(payload["job_id"]) is not None
        payload["revision"] = web.web_revision(current)
        assert (await plugin.api_action_reminders("importance", dict(payload, important=False)))["status"] == "ok"
        assert not (await plugin._storage.load())[payload["session_id"]][0]["important"]
    asyncio.run(run())


@pytest.mark.parametrize("changes,code", [
    ({"owner_id": "someone"}, "validation"), ({"content": " "}, "contentRequired"),
    ({"content": None}, "validation"), ({"time": "2030-02-30 10:00"}, "timeInvalid"),
    ({"repeat": "invalid"}, "validation"), ({"repeat": "interval", "interval_minutes": True}, "intervalInvalid"),
    ({"repeat": "interval", "interval_minutes": 1441}, "intervalInvalid"),
    ({"time": "2000-01-01 10:00"}, "futureRequired"), ({}, "validation"),
])
def test_invalid_edit_does_not_write(plugin, changes, code):
    async def run():
        payload = await seed(plugin)
        before = await plugin._storage.load()
        result = await plugin.api_action_reminders("edit", dict(payload, changes=changes))
        assert result["code"] == code
        assert await plugin._storage.load() == before
    asyncio.run(run())


@pytest.mark.parametrize("extra", [{"is_random": True, "time": "2000-01-01 10:00"}, {"source": "autonomous"}, {"managed_by": "autonomy"}])
def test_special_tasks_cannot_be_edited_but_can_be_starred(plugin, extra):
    async def run():
        # Use the actual domain constants rather than duplicating their spelling.
        autonomy = load_plugin_module("autonomy")
        if "source" in extra: extra["source"] = autonomy.AUTONOMOUS_SOURCE
        if "managed_by" in extra: extra["managed_by"] = autonomy.AUTONOMOUS_MANAGER
        payload = await seed(plugin, **extra)
        assert (await plugin.api_action_reminders("edit", dict(payload, changes={"content": "changed"})))["code"] == "readonly"
        assert (await plugin.api_action_reminders("importance", dict(payload, important=True)))["status"] == "ok"
    asyncio.run(run())


async def seed_random(plugin):
    payload = await seed(plugin, is_random=True, random_batch_id="batch-1", random_index=1,
                         random_total=3, time_range={"start": "2030-01-01 09:00", "end": "2030-01-01 18:00"})
    async with plugin._storage.modify() as data:
        first = data[payload["session_id"]][0]
        data[payload["session_id"]] += [
            dict(first, job_id="random-2", time="2030-01-01 11:00", random_index=2, paused=True),
            dict(first, job_id="past", time="2000-01-01 10:00", random_index=3),
            dict(first, job_id="other-batch", random_batch_id="batch-2"),
        ]
    return payload


@pytest.mark.parametrize("edit_batch", [False, True])
def test_random_basic_edit_preserves_times_and_history(plugin, edit_batch):
    async def run():
        payload = await seed_random(plugin)
        before = await plugin._storage.load()
        rows = (await plugin.api_get_reminders(payload["session_id"]))["data"]
        assert rows[0]["web_batch_editable_count"] == 2
        assert rows[2]["web_editable"] is False
        if edit_batch:
            payload.update(edit_batch=True, revision=rows[0]["web_batch_revision"])
        result = await plugin.api_action_reminders("edit", dict(payload, changes={"content": "new", "category": "", "action": ""}))
        assert result["status"] == "ok"
        expected = copy.deepcopy(before)
        expected[payload["session_id"]][0].update(content="new", category="", action="")
        if edit_batch:
            expected[payload["session_id"]][1].update(content="new", category="", action="")
        assert await plugin._storage.load() == expected
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"time": "2030-01-02 12:00"}, {"repeat": "daily"}, {"interval_minutes": 20}])
def test_random_schedule_edit_rejected_in_web_and_native_tool(plugin, changes):
    async def run():
        payload = await seed_random(plugin)
        before = await plugin._storage.load()
        assert (await plugin.api_action_reminders("edit", dict(payload, changes=changes)))["status"] == "error"
        result = await plugin.edit_reminder(batch(message(group=None)), payload["job_id"], **changes)
        assert "随机任务仅支持" in result
        assert await plugin._storage.load() == before
    asyncio.run(run())


def test_random_batch_native_tool_checks_every_owner_before_any_write(plugin):
    async def run():
        payload = await seed_random(plugin)
        event = batch(message(group=None))
        assert "共2项" in await plugin.edit_reminder(event, payload["job_id"], content="native", edit_batch=True)
        async with plugin._storage.modify() as data:
            data[event.sid][1]["owner_id"] = "bob"
        before = await plugin._storage.load()
        assert "权限拒绝" in await plugin.edit_reminder(event, payload["job_id"], content="denied", edit_batch=True)
        assert await plugin._storage.load() == before
    asyncio.run(run())


def test_random_batch_conflicts_when_a_sibling_changes(plugin, monkeypatch):
    async def run():
        payload = await seed_random(plugin)
        rows = (await plugin.api_get_reminders(payload["session_id"]))["data"]
        payload.update(edit_batch=True, revision=rows[0]["web_batch_revision"], changes={"content": "web"})
        original = plugin._storage.modify
        @asynccontextmanager
        async def race(**kwargs):
            async with original() as data:
                data[payload["session_id"]][1]["content"] = "other editor"
            async with original(**kwargs) as data:
                yield data
        monkeypatch.setattr(plugin._storage, "modify", race)
        assert (await plugin.api_action_reminders("edit", payload))["code"] == "conflict"
        assert (await plugin._storage.load())[payload["session_id"]][0]["content"] == "test"
    asyncio.run(run())


def test_random_batch_scheduler_failure_rolls_back_all_records(plugin, monkeypatch):
    async def run():
        payload = await seed_random(plugin)
        async with plugin._storage.modify() as data:
            data[payload["session_id"]][1]["paused"] = False
        before = await plugin._storage.load()
        for record in before[payload["session_id"]][:2]:
            plugin._add_job(payload["session_id"], record)
        original_job = plugin._scheduler.get_job(payload["job_id"])
        original_add = plugin._add_job
        def fail_second(sid, record):
            if record["job_id"] == "random-2": raise RuntimeError("second job failed")
            original_add(sid, record)
        monkeypatch.setattr(plugin, "_add_job", fail_second)
        row = (await plugin.api_get_reminders(payload["session_id"]))["data"][0]
        payload.update(edit_batch=True, revision=row["web_batch_revision"], changes={"content": "new"})
        assert (await plugin.api_action_reminders("edit", payload))["status"] == "error"
        assert await plugin._storage.load() == before
        restored = plugin._scheduler.get_job(payload["job_id"])
        assert restored.args == original_job.args
        assert str(restored.trigger) == str(original_job.trigger)
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["edit", "importance"])
def test_stale_snapshot_and_race_inside_transaction(plugin, operation, monkeypatch):
    async def run():
        payload = await seed(plugin)
        payload.update(changes={"content": "web edit"}, important=True)
        storage = plugin._storage
        original_modify = storage.modify

        @asynccontextmanager
        async def racing_modify(**kwargs):
            async with original_modify() as data:
                data[payload["session_id"]][0]["content"] = "LLM edit"
            async with original_modify(**kwargs) as data:
                yield data

        monkeypatch.setattr(storage, "modify", racing_modify)
        result = await plugin.api_action_reminders(operation, payload)
        assert result["code"] == "conflict"
        current = (await storage.load())[payload["session_id"]][0]
        assert current["content"] == "LLM edit" and not current.get("important")
        assert (await plugin.api_action_reminders(operation, payload))["code"] == "conflict"
    asyncio.run(run())


def test_scheduler_failure_rolls_back_edit(plugin, monkeypatch):
    async def run():
        payload = await seed(plugin)
        before = await plugin._storage.load()
        def fail(*args, **kwargs):
            raise RuntimeError("scheduler unavailable")
        monkeypatch.setattr(plugin, "_add_job", fail)
        result = await plugin.api_action_reminders("edit", dict(payload, changes={"content": "changed"}))
        assert result["status"] == "error"
        assert await plugin._storage.load() == before
    asyncio.run(run())
