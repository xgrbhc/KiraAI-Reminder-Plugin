"""Recurring start boundaries and read-only live schedule presentation."""

import asyncio
import copy
import datetime as dt
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from conftest import attach_delivery
from test_reminder_service import make_plugin
from test_scheduler import FakeScheduler


SID = "qq:dm:10001"
CALENDAR_REPEATS = ("daily", "weekly", "monthly", "yearly")
REPEATS = (*CALENDAR_REPEATS, "interval")


def record(repeat="daily", **fields):
    return {"job_id": "recurring", "content": "test", "time": "2099-01-01 10:15",
            "repeat": repeat, "creator_name": "Alice", **fields}


def captured_trigger(reminder_main, tmp_path, reminder):
    plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
    attach_delivery(plugin, reminder_main, tmp_path)
    plugin._scheduler = FakeScheduler()
    reminder_main.ReminderPlugin._add_job(plugin, SID, reminder)
    return plugin._scheduler.jobs[0][1]["trigger"]


@pytest.mark.parametrize("repeat", CALENDAR_REPEATS)
@pytest.mark.parametrize("at_boundary", [False, True])
def test_calendar_never_runs_before_start(reminder_main, tmp_path, repeat, at_boundary):
    reminder = record(repeat)
    before = copy.deepcopy(reminder)
    trigger = captured_trigger(reminder_main, tmp_path, reminder)
    now = trigger.start_date if at_boundary else dt.datetime(2026, 10, 4, tzinfo=trigger.timezone)
    first = trigger.get_next_fire_time(None, now)
    assert first == trigger.start_date
    assert first.strftime("%Y-%m-%d %H:%M") == reminder["time"]
    assert trigger.get_next_fire_time(first, first) > first
    assert reminder == before


@pytest.mark.parametrize("repeat,expected", [
    ("daily", "2026-10-05 10:15"), ("weekly", "2026-10-08 10:15"),
    ("monthly", "2026-11-01 10:15"), ("yearly", "2027-10-01 10:15"),
])
def test_past_anchor_continues_without_history_backfill(reminder_main, tmp_path, repeat, expected):
    trigger = captured_trigger(reminder_main, tmp_path, record(repeat, time="2026-10-01 10:15"))
    now = dt.datetime(2026, 10, 4, 11, tzinfo=trigger.timezone)
    assert trigger.get_next_fire_time(None, now).strftime("%Y-%m-%d %H:%M") == expected


@pytest.mark.parametrize("repeat,start,now,expected", [
    ("monthly", "2026-01-31 10:15", "2026-02-01 00:00", "2026-03-31 10:15"),
    ("yearly", "2024-02-29 10:15", "2025-01-01 00:00", "2028-02-29 10:15"),
    ("daily", "2026-12-31 23:59", "2027-01-01 00:00", "2027-01-01 23:59"),
    ("weekly", "2026-12-31 23:59", "2027-01-01 00:00", "2027-01-07 23:59"),
])
def test_calendar_edges_keep_existing_skip_rules(reminder_main, tmp_path, repeat, start, now, expected):
    trigger = captured_trigger(reminder_main, tmp_path, record(repeat, time=start))
    current = dt.datetime.strptime(now, "%Y-%m-%d %H:%M").replace(tzinfo=trigger.timezone)
    assert trigger.get_next_fire_time(None, current).strftime("%Y-%m-%d %H:%M") == expected


@pytest.mark.parametrize("repeat", CALENDAR_REPEATS)
def test_restore_keeps_anchor_and_does_not_register_paused(reminder_main, tmp_path, repeat):
    async def run():
        plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._scheduler = FakeScheduler()
        records = [record(repeat), record(repeat, job_id="paused", paused=True)]
        await plugin._storage.save({SID: records})
        await plugin._scheduler_service().restore_jobs()
        assert (await plugin._storage.load())[SID] == records
        assert len(plugin._scheduler.jobs) == 1
        trigger = plugin._scheduler.jobs[0][1]["trigger"]
        assert trigger.start_date.strftime("%Y-%m-%d %H:%M") == records[0]["time"]
    asyncio.run(run())


@pytest.mark.parametrize("repeat", REPEATS)
def test_live_metadata_is_not_persisted_and_tracks_advance(reminder_main, tmp_path, repeat):
    async def run():
        path = tmp_path / "reminders.json"
        plugin, _, _ = make_plugin(reminder_main, path)
        original = record(repeat, unknown={"keep": True})
        await plugin._storage.save({SID: [original]})
        before = path.read_bytes()
        next_time = dt.datetime(2099, 1, 2, 10, 15).astimezone()
        job = SimpleNamespace(next_run_time=next_time, pending=False)
        plugin._scheduler = SimpleNamespace(running=True, get_job=lambda job_id: job)
        result = await plugin.api_get_reminders(quote(SID, safe=""))
        row = result["data"][0]
        assert result["status"] == "ok"
        assert row == dict(original, schedule_status="scheduled", next_run_time="2099-01-02 10:15")
        row["content"] = "not a stored change"
        job.next_run_time += dt.timedelta(days=1)
        assert (await plugin.api_get_reminders(SID))["data"][0]["next_run_time"] == "2099-01-03 10:15"
        assert (await plugin._storage.load())[SID] == [original]
        assert path.read_bytes() == before
    asyncio.run(run())


@pytest.mark.parametrize("state", ["paused", "missing", "unavailable", "pending", "unknown", "raises"])
def test_schedule_unavailable_does_not_invent_a_date(reminder_main, state):
    def get_job(job_id):
        assert job_id == "recurring"
        if state == "raises":
            raise RuntimeError("test scheduler query failure")
        if state == "missing":
            return None
        return SimpleNamespace(pending=state == "pending", next_run_time=None)
    scheduler = SimpleNamespace(running=state != "unavailable", get_job=get_job)
    result = reminder_main.reminder_schedule_info(scheduler, record(paused=state == "paused"))
    expected = "unavailable" if state == "raises" else state
    assert result == {"next_run_time": None, "schedule_status": expected}


@pytest.mark.parametrize("repeat", ["none", "", "unsupported", None])
def test_nonrecurring_response_is_unchanged(reminder_main, repeat):
    assert reminder_main.reminder_schedule_info(None, record(repeat)) == {}


@pytest.mark.parametrize("repeat", REPEATS)
def test_list_distinguishes_start_and_next_after_permission_filter(reminder_main, tmp_path, repeat):
    async def run():
        plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
        original = record(repeat, interval_minutes=30)
        await plugin._storage.save({SID: [original, record(job_id="other", content="not visible")]})
        queried = []
        def get_job(job_id):
            queried.append(job_id)
            return SimpleNamespace(next_run_time=dt.datetime(2099, 1, 2, 10, 15).astimezone())
        plugin._scheduler = SimpleNamespace(running=True, get_job=get_job)
        plugin._is_admin_user = lambda event: False
        plugin._check_permission = lambda event, target, operation: target["job_id"] == "recurring"
        result = await plugin._reminder_service().list_reminders(SimpleNamespace())
        assert "开始: 2099-01-01 10:15" in result
        assert "下次: 2099-01-02 10:15" in result
        assert "not visible" not in result
        assert queried == ["recurring"]
    asyncio.run(run())


@pytest.mark.parametrize("repeat", CALENDAR_REPEATS)
def test_real_paused_scheduler_reports_registered_time_without_firing(reminder_main, tmp_path, repeat):
    plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
    attach_delivery(plugin, reminder_main, tmp_path)
    scheduler = plugin._scheduler = BackgroundScheduler()
    scheduler.start(paused=True)
    try:
        original = record(repeat)
        reminder_main.ReminderPlugin._add_job(plugin, SID, original)
        details = reminder_main.reminder_schedule_info(scheduler, original)
        assert details == {"next_run_time": original["time"], "schedule_status": "scheduled"}
        scheduler.pause_job(original["job_id"])
        assert reminder_main.reminder_schedule_info(scheduler, original)["schedule_status"] == "unknown"
        scheduler.remove_job(original["job_id"])
        assert reminder_main.reminder_schedule_info(scheduler, original)["schedule_status"] == "missing"
    finally:
        scheduler.shutdown(wait=False)


def test_repeated_delivery_receipt_keeps_original_anchor(reminder_main, tmp_path):
    async def run():
        plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        original = record(owner_type="user", owner_id="10001")
        await plugin._storage.save({SID: [original]})
        delivery_id = await plugin._delivery.begin(SID, original)
        await plugin._delivery.mark(SID, delivery_id, "llm_received")
        assert (await plugin._storage.load())[SID] == [original]
        assert (await plugin._delivery_storage.load())[SID][0]["reminder"] == original
    asyncio.run(run())


@pytest.mark.parametrize("repeat", CALENDAR_REPEATS)
def test_edit_pause_and_resume_preserve_start_until_time_is_explicitly_changed(reminder_main, tmp_path, repeat):
    async def run():
        plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        scheduler = plugin._scheduler = BackgroundScheduler()
        scheduler.start(paused=True)
        plugin._add_job = lambda sid, reminder: reminder_main.ReminderPlugin._add_job(plugin, sid, reminder)
        try:
            original = record(repeat)
            await plugin._storage.save({SID: [original]})
            plugin._add_job(SID, original)
            event = SimpleNamespace()
            assert (await plugin.edit_reminder(event, original["job_id"], content="new title")).startswith("已更新")
            assert scheduler.get_job(original["job_id"]).trigger.start_date.strftime("%Y-%m-%d %H:%M") == original["time"]
            assert (await plugin.pause_reminder(event, original["job_id"])).startswith("已暂停")
            assert scheduler.get_job(original["job_id"]) is None
            assert (await plugin.api_get_reminders(SID))["data"][0]["schedule_status"] == "paused"
            assert (await plugin.resume_reminder(event, original["job_id"])).startswith("已恢复")
            assert scheduler.get_job(original["job_id"]).trigger.start_date.strftime("%Y-%m-%d %H:%M") == original["time"]
            updated = "2099-02-02 12:30"
            assert (await plugin.edit_reminder(event, original["job_id"], time=updated)).startswith("已更新")
            assert scheduler.get_job(original["job_id"]).trigger.start_date.strftime("%Y-%m-%d %H:%M") == updated
            assert (await plugin._storage.load())[SID][0]["time"] == updated
            assert (await plugin.api_get_reminders(SID))["data"][0]["next_run_time"] == updated
        finally:
            scheduler.shutdown(wait=False)
    asyncio.run(run())
