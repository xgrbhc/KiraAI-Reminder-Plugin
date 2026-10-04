"""Explicit audit probes: safety checks for previously observed defects.

Run with: python -m pytest tests/audit_probes.py -q -s
The probes assert corrected behavior, including the recurring start-date boundary.
This filename is excluded from pytest's normal test_* discovery.
All state is temporary; scheduler and publish callbacks are in-memory fakes.
"""

import asyncio
import datetime as dt
import json
import sys
from types import SimpleNamespace

import pytest

from conftest import attach_delivery
from test_autonomy import FakeScheduler, make_autonomy_plugin
from test_reminder_service import make_plugin


SID = "qq:dm:10001"


def observation(name, **details):
    print("AUDIT_OBSERVATION=" + json.dumps({"name": name, **details}, ensure_ascii=False))


def test_scheduler_rejection_no_longer_returns_creation_success(reminder_main, tmp_path):
    async def run():
        plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        calls = []

        def reject(*args, **kwargs):
            calls.append(kwargs["id"])
            raise RuntimeError("audit scheduler rejected registration")

        plugin._scheduler = SimpleNamespace(add_job=reject)
        plugin._add_job = lambda sid, record: reminder_main.ReminderPlugin._add_job(plugin, sid, record)
        result = await plugin.set_reminder(SimpleNamespace(), "audit", "2099-01-01 10:00")
        stored = await plugin._storage.load()
        assert len(calls) == 1 and stored == {}
        assert result.startswith("设置出错")
        observation("registration_failure", reported_success=False, stored_records=0, registered_jobs=0)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["pause_reminder", "delete_reminder"])
def test_failed_save_preserves_a_live_job(reminder_main, tmp_path, monkeypatch, operation):
    async def run():
        plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
        original = {"job_id": "audit-job", "content": "audit", "time": "2099-01-01 10:00", "repeat": "daily"}
        await plugin._storage.save({SID: [original]})
        scheduler = plugin._scheduler = FakeScheduler()
        scheduler.add_job(None, id=original["job_id"])

        def denied(*args):
            raise OSError("audit disk write denied")

        monkeypatch.setattr(plugin._storage, "_unsafe_save", denied)
        result = await getattr(plugin, operation)(SimpleNamespace(), original["job_id"])
        assert result.startswith("出错")
        assert (await plugin._storage.load())[SID] == [original]
        assert len(scheduler.jobs) == 1 and scheduler.jobs[0][1]["id"] == original["job_id"]
        observation(operation + "_save_failure", stored_record_unchanged=True, live_job_removed=False)

    asyncio.run(run())


def test_daily_cycle_preserves_a_concurrent_new_intent(reminder_main, tmp_path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        reached = asyncio.Event()
        release = asyncio.Event()

        async def publish(*args, **kwargs):
            reached.set()
            await release.wait()

        plugin._publish_autonomous_notice = publish
        coordinator = plugin._autonomy_coordinator()
        cycle = asyncio.create_task(coordinator.daily_cycle_job())
        await reached.wait()
        result = await coordinator.create_intent(SID, "created concurrently")
        assert result.startswith("已创建")
        before = await plugin._autonomy_storage.load()
        assert len(before["sessions"][SID]["intents"]) == 1
        release.set()
        await cycle
        after = await plugin._autonomy_storage.load()
        assert after["sessions"][SID]["intents"] == before["sessions"][SID]["intents"]
        assert after["sessions"][SID]["last_cycle_at"]
        observation("autonomy_concurrent_update_preserved", intents_before_cycle_save=1, intents_after_cycle_save=1)

    asyncio.run(run())


def test_invalid_update_preserves_the_original_intent(reminder_main, tmp_path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        coordinator = plugin._autonomy_coordinator()
        await coordinator.create_intent(SID, "original")
        original = (await plugin._autonomy_storage.load())["sessions"][SID]["intents"][0]
        result = await coordinator.update_intent(SID, original["id"], title="changed", status="invalid")
        after = (await plugin._autonomy_storage.load())["sessions"][SID]["intents"][0]
        assert result.startswith("❌") and after == original
        observation("partial_invalid_update", reported_failure=True, title_changed=False)

    asyncio.run(run())


def test_random_schedule_preserves_distinct_minute_slots(reminder_main, tmp_path, monkeypatch):
    async def run():
        plugin, _, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        time_utils = sys.modules[reminder_main.generate_multiple_random_times.__module__]
        monkeypatch.setattr(time_utils.random, "randint", lambda low, high: high)
        result = await plugin.set_reminder(
            SimpleNamespace(), "audit random", "2099-01-01 10:00",
            time_range_end="2099-01-01 10:03", random_count=2,
        )
        records = (await plugin._storage.load())[SID]
        assert result.startswith("已添加随机待办") and len(records) == len(scheduled) == 2
        assert len({record["time"] for record in records}) == 2
        assert all("2099-01-01 10:00" <= record["time"] < "2099-01-01 10:03" for record in records)
        assert records[0]["job_id"] != records[1]["job_id"]
        observation("random_minute_collision", stored_times=[item["time"] for item in records])

    asyncio.run(run())


def test_future_daily_start_date_is_respected(reminder_main, tmp_path):
    plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
    attach_delivery(plugin, reminder_main, tmp_path)
    plugin._scheduler = scheduler = FakeScheduler()
    reminder_main.ReminderPlugin._add_job(plugin, SID, {
        "job_id": "audit-future", "time": "2099-01-01 10:00", "repeat": "daily",
    })
    trigger = scheduler.jobs[0][1]["trigger"]
    now = dt.datetime(2026, 10, 4, 0, 0, tzinfo=trigger.timezone)
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire.strftime("%Y-%m-%d %H:%M") == "2099-01-01 10:00"
    observation("future_daily_start", requested_start="2099-01-01 10:00", next_fire=str(next_fire))


def test_atomic_replace_failure_keeps_old_file_and_cleans_temporary_file(reminder_main, tmp_path, monkeypatch):
    async def run():
        storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        original = {SID: [{"job_id": "original"}]}
        await storage.save(original)
        module = sys.modules[storage.__class__.__module__]

        def rejected(*args):
            raise PermissionError("audit replacement denied")

        monkeypatch.setattr(module.os, "replace", rejected)
        with pytest.raises(PermissionError):
            await storage.save({SID: []})
        assert await storage.load() == original
        assert list(tmp_path.glob("*.tmp")) == []
        observation("atomic_replace_protection", old_data_preserved=True, temporary_files=0)

    asyncio.run(run())


def test_nested_invalid_autonomy_data_is_preserved_on_write(reminder_main, tmp_path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        path = plugin._autonomy_storage.path
        path.write_text(json.dumps({"sessions": {SID: {"intents": "invalid nested shape"}}}), encoding="utf-8")
        original = path.read_bytes()
        with pytest.raises(reminder_main.ReminderStorageError):
            await plugin._autonomy_coordinator().create_intent(SID, "new")
        assert path.read_bytes() == original
        observation("nested_shape_overwrite", original_shape_rejected=True, original_value_replaced=False)

    asyncio.run(run())
