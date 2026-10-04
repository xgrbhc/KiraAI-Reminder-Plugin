"""Existing scheduler behavior preserved during the background-task split."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from conftest import attach_delivery


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, **kwargs):
        self.jobs.append((func, kwargs))


def test_retry_does_not_publish_a_changed_reminder(reminder_main, tmp_path: Path):
    async def run():
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._fire_semaphore = asyncio.Semaphore(3)
        sid = "qq:dm:10001"
        original = {"job_id": "job-1", "content": "original", "repeat": "none"}
        await plugin._storage.save({sid: [original]})
        delivery_id = await plugin._delivery.begin(sid, original)
        await plugin._storage.save({sid: [dict(original, action="not approved")]})
        await plugin._fire_reminder(sid, original, delivery_id=delivery_id)
        entry = (await plugin._delivery_storage.load())[sid][0]
        assert entry["status"] == "failed"
        assert "changed after retry" in entry["last_error"]
        assert (await plugin._storage.load())[sid]
    asyncio.run(run())


def test_trigger_types_and_registration_options(reminder_main, tmp_path: Path):
    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
    attach_delivery(plugin, reminder_main, tmp_path)
    plugin._scheduler = FakeScheduler()
    sid = "qq:dm:10001"
    expected = {
        "none": DateTrigger,
        "daily": CronTrigger,
        "weekly": CronTrigger,
        "monthly": CronTrigger,
        "yearly": CronTrigger,
        "interval": IntervalTrigger,
    }
    for repeat, trigger_type in expected.items():
        record = {
            "job_id": f"job-{repeat}",
            "time": "2099-01-01 10:15",
            "repeat": repeat,
            "interval_minutes": 30,
        }
        plugin._add_job(sid, record)
        callback, options = plugin._scheduler.jobs[-1]
        assert callback.__self__ is plugin
        assert callback.__name__ == "_fire_reminder"
        assert isinstance(options["trigger"], trigger_type)
        assert options["id"] == record["job_id"]
        assert options["args"] == [sid, record]
        assert options["replace_existing"] is True
        assert options["misfire_grace_time"] == 300
    assert len(plugin._scheduler.jobs) == len(expected)


def test_restore_keeps_paused_bad_and_overdue_records(reminder_main, tmp_path: Path):
    async def run():
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._scheduler = FakeScheduler()
        now = dt.datetime.now()
        future = (now + dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        past = (now - dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        sid = "qq:dm:10001"
        records = [
            {"job_id": "future", "time": future, "repeat": "none"},
            {"job_id": "expired", "time": past, "repeat": "none"},
            {"job_id": "repeat", "time": past, "repeat": "daily"},
            {"job_id": "paused", "time": future, "repeat": "none", "paused": True},
            {"job_id": "invalid", "time": "bad", "repeat": "none"},
        ]
        await plugin._storage.save({sid: records})
        await plugin._restore_jobs()
        assert [r["job_id"] for r in (await plugin._storage.load())[sid]] == [
            "future", "expired", "repeat", "paused", "invalid"
        ]
        assert [options["id"] for _, options in plugin._scheduler.jobs] == ["future", "repeat"]
        assert (await plugin._delivery_storage.load())[sid][0]["status"] == "legacy_unconfirmed"

    asyncio.run(run())


def test_health_check_restarts_stopped_scheduler_once(reminder_main, tmp_path: Path, monkeypatch):
    class RestartableScheduler(FakeScheduler):
        def __init__(self):
            super().__init__()
            self.running = False
            self.starts = 0

        def start(self):
            self.running = True
            self.starts += 1

    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
    attach_delivery(plugin, reminder_main, tmp_path)
    plugin._scheduler = RestartableScheduler()
    sleep_calls = 0

    async def stop_after_restart(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(reminder_main.asyncio, "sleep", stop_after_restart)
    asyncio.run(plugin._health_check_loop())
    assert plugin._scheduler.starts == 1
    assert sleep_calls == 2


def test_one_time_delivery_waits_for_model_receipt(reminder_main, tmp_path: Path):
    async def run():
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._fire_semaphore = asyncio.Semaphore(3)
        plugin.config = SimpleNamespace(autonomy_mode="plan_only")
        plugin._is_autonomous_reminder = lambda _record: False
        published = []

        async def publish(**kwargs):
            published.append(kwargs)

        plugin._publish_immediate_notice = publish
        sid = "qq:dm:10001"
        record = {
            "job_id": "job-1", "content": "test", "time": "2099-01-01 10:00",
            "repeat": "none", "category": "work", "action": "do something",
            "owner_type": "user", "owner_id": "10001",
        }
        await plugin._storage.save({sid: [record]})
        await plugin._fire_reminder(sid, record)
        assert len(published) == 1
        assert published[0]["session"] == sid
        assert published[0]["chain"][0].text == (
            "⏰ [work] 提醒：test\n👉 自动动作指令：do something\n"
            "(请优先执行上述动作建议并回复结果)"
        )
        assert published[0]["principal_kind"] is reminder_main.PrincipalKind.USER
        assert published[0]["principal_id"] == "10001"
        assert published[0]["origin"] is reminder_main.EventOrigin.REMINDER_FIRE
        assert (await plugin._storage.load())[sid] == [record]
        delivery_id = published[0]["delivery_id"]
        assert (await plugin._delivery_storage.load())[sid][0]["status"] == "awaiting_llm"
        await plugin._delivery.mark(sid, delivery_id, "llm_received")
        assert await plugin._storage.load() == {sid: []}

    asyncio.run(run())


def test_delivery_retries_three_times_and_records_failure(reminder_main, tmp_path: Path, monkeypatch):
    attempts = []

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(reminder_main.asyncio, "sleep", no_delay)

    async def run():
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._fire_semaphore = asyncio.Semaphore(3)
        plugin.config = SimpleNamespace(autonomy_mode="plan_only")
        plugin._is_autonomous_reminder = lambda _record: False

        async def publish(**_kwargs):
            attempts.append(1)
            raise RuntimeError("offline")

        plugin._publish_immediate_notice = publish
        sid = "qq:dm:10001"
        record = {"job_id": "job-1", "content": "test", "repeat": "none"}
        await plugin._storage.save({sid: [record]})
        await plugin._fire_reminder(sid, record)
        stored = (await plugin._storage.load())[sid][0]
        assert len(attempts) == 3
        assert stored["job_id"] == "job-1"
        assert (await plugin._delivery_storage.load())[sid][0]["status"] == "failed"

    asyncio.run(run())


def test_autonomous_followup_delivery_keeps_bot_identity(reminder_main, tmp_path: Path):
    async def run():
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._fire_semaphore = asyncio.Semaphore(3)
        plugin.config = SimpleNamespace(autonomy_mode="trusted_admin")
        plugin._is_autonomous_reminder = lambda _record: True
        plugin._build_autonomous_notice_text = lambda **_kwargs: "internal follow-up"
        marked = []
        published = []

        async def mark(sid, record):
            marked.append((sid, record["job_id"]))

        async def publish(**kwargs):
            published.append(kwargs)

        plugin._mark_autonomous_followup_fired = mark
        plugin._publish_immediate_notice = publish
        sid = "qq:dm:10001"
        record = {
            "job_id": "job-1", "content": "follow up", "repeat": "none",
            "owner_type": "bot", "owner_id": "10001", "intent_id": "intent-1",
        }
        await plugin._storage.save({sid: [record]})
        await plugin._fire_reminder(sid, record)
        assert published[0]["chain"][0].text == "internal follow-up"
        assert published[0]["principal_kind"] is reminder_main.PrincipalKind.BOT
        assert published[0]["origin"] is reminder_main.EventOrigin.AUTONOMY_FOLLOWUP_DUE
        assert "reminder.manage_all" in published[0]["capabilities"]
        assert marked == []
        assert (await plugin._storage.load())[sid] == [record]
        await plugin._delivery.mark(sid, published[0]["delivery_id"], "llm_received")
        assert await plugin._storage.load() == {sid: []}

    asyncio.run(run())
