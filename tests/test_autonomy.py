"""Autonomous state and random-plan contracts for structural extraction."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytest

from conftest import attach_delivery


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, **kwargs):
        self.jobs.append((func, kwargs))

    def remove_job(self, job_id):
        self.jobs = [(func, kwargs) for func, kwargs in self.jobs if kwargs["id"] != job_id]


def make_autonomy_plugin(reminder_main, tmp_path: Path):
    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
    plugin._autonomy_storage = reminder_main.ReminderStorage(
        tmp_path / "autonomous_state.json", validator=reminder_main.validate_autonomy_state,
    )
    attach_delivery(plugin, reminder_main, tmp_path)
    plugin._scheduler = FakeScheduler()
    plugin.config = SimpleNamespace(
        autonomy_enabled=True,
        autonomy_mode="plan_only",
        allowed_sessions=["qq:dm:10001"],
        daily_reflection_enabled=True,
        followup_due_enabled=True,
        daily_reflection_hour=10,
        random_check_enabled=False,
        random_check_daily_count=1,
        random_check_start_hour=10,
        random_check_end_hour=23,
        visible_output_policy="necessary_only",
    )
    return plugin


def test_state_defaults_and_malformed_lists_are_not_normalized(reminder_main):
    state = {}
    session = reminder_main.ReminderPlugin._ensure_autonomy_session(state, "qq:dm:10001")
    assert state == {"sessions": {"qq:dm:10001": session}}
    assert session == {
        "enabled": True,
        "last_cycle_at": "",
        "cooldown_until": "",
        "random_check_plan_date": "",
        "random_check_window": "",
        "random_check_times": [],
        "intents": [],
    }
    session["intents"] = "legacy-invalid"
    session["random_check_times"] = None
    original = copy.deepcopy(state)
    with pytest.raises(reminder_main.ReminderStorageError):
        reminder_main.ReminderPlugin._ensure_autonomy_session(state, "qq:dm:10001")
    assert state == original


def test_autonomous_marker_and_optional_time(reminder_main):
    plugin = reminder_main.ReminderPlugin
    assert plugin._is_autonomous_reminder({"source": "autonomous_intent_loop"})
    assert plugin._is_autonomous_reminder({"managed_by": "reminder_plugin.autonomous"})
    assert not plugin._is_autonomous_reminder({"source": "user"})
    assert plugin._parse_optional_time("") is None
    assert plugin._parse_optional_time("bad") is None
    assert plugin._parse_optional_time("2099-01-01 10:15") == dt.datetime(2099, 1, 1, 10, 15)


def test_random_plan_generation_and_id(reminder_main, monkeypatch):
    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin.config = SimpleNamespace(
        random_check_start_hour=10,
        random_check_end_hour=12,
        random_check_daily_count=2,
    )
    monkeypatch.setattr(reminder_main.random, "sample", lambda _population, _count: [30, 0])
    assert plugin._random_check_window() == (10, 12)
    assert plugin._generate_random_check_times(dt.datetime(2099, 1, 1, 9, 0)) == [
        "2099-01-01 10:00", "2099-01-01 10:30"
    ]
    assert plugin._random_job_id("qq:dm:10001", "2099-01-01 10:00") == (
        "reminder_autonomous_random_check_qq_dm_10001_20990101_1000"
    )
    plugin.config.random_check_end_hour = 9
    assert plugin._random_check_window() == (10, 23)


def test_loading_state_only_normalizes_in_memory(reminder_main, tmp_path: Path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        state = await plugin._load_autonomy_state()
        assert state == {"sessions": {}}
        assert not plugin._autonomy_storage.path.exists()

    asyncio.run(run())


def test_autonomous_job_registration_keeps_ids_and_triggers(reminder_main, tmp_path: Path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await plugin._start_autonomous_jobs()
        assert [options["id"] for _, options in plugin._scheduler.jobs] == [
            "reminder_autonomous_daily_cycle", "reminder_autonomous_followup_due"
        ]
        assert isinstance(plugin._scheduler.jobs[0][1]["trigger"], CronTrigger)
        assert isinstance(plugin._scheduler.jobs[1][1]["trigger"], IntervalTrigger)
        assert plugin._scheduler.jobs[0][1]["misfire_grace_time"] == 600
        assert plugin._scheduler.jobs[1][1]["misfire_grace_time"] == 300

    asyncio.run(run())


def test_daily_and_due_jobs_update_state_after_publish(reminder_main, tmp_path: Path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        sid = "qq:dm:10001"
        intent = {
            "id": "intent-1", "title": "test", "status": "active",
            "next_check_at": "2000-01-01 10:00", "next_check_job_id": "old-job",
        }
        await plugin._autonomy_storage.save({"sessions": {sid: {"intents": [intent]}}})
        await plugin._storage.save({sid: []})
        published = []

        async def publish(*args, **kwargs):
            published.append((args, kwargs))

        plugin._publish_autonomous_notice = publish
        await plugin._autonomous_daily_cycle_job()
        await plugin._autonomous_followup_due_job()
        assert [args[1] for args, _ in published] == ["daily_reflection", "followup_due"]
        session = (await plugin._autonomy_storage.load())["sessions"][sid]
        assert session["last_cycle_at"]
        updated = session["intents"][0]
        assert updated["last_followup_source"] == "fallback_due_job"
        assert updated["next_check_at"] == ""
        assert updated["next_check_job_id"] == ""

    asyncio.run(run())


def test_random_check_records_schedule_and_publish(reminder_main, tmp_path: Path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        plugin.config.random_check_enabled = True
        sid = "qq:dm:10001"
        await plugin._autonomy_storage.save({"sessions": {sid: {"intents": []}}})
        published = []

        async def publish(*args, **kwargs):
            published.append((args, kwargs))

        plugin._publish_autonomous_notice = publish
        await plugin._autonomous_random_check_job(sid, scheduled_time="2099-01-01 10:00")
        session = (await plugin._autonomy_storage.load())["sessions"][sid]
        assert session["last_random_check_at"]
        assert session["last_random_check_scheduled_at"] == "2099-01-01 10:00"
        assert published[0][0] == (sid, "random_check")

    asyncio.run(run())


def test_random_plan_persists_and_registers_future_job(reminder_main, tmp_path: Path, monkeypatch):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        plugin.config.random_check_enabled = True
        sid = "qq:dm:10001"
        autonomy = sys.modules[reminder_main.AutonomyCoordinator.__module__]
        monkeypatch.setattr(autonomy, "get_local_now", lambda: dt.datetime(2099, 1, 1, 9, 0))
        monkeypatch.setattr(
            autonomy, "generate_random_check_times",
            lambda _config, _now: ["2099-01-01 10:00"],
        )

        await plugin._schedule_autonomous_random_checks()
        state = await plugin._autonomy_storage.load()
        session = state["sessions"][sid]
        assert session["random_check_plan_date"] == "2099-01-01"
        assert session["random_check_times"] == ["2099-01-01 10:00"]
        func, options = plugin._scheduler.jobs[0]
        assert func == plugin._autonomous_random_check_job
        assert options["id"] == plugin._random_job_id(sid, "2099-01-01 10:00")
        assert isinstance(options["trigger"], DateTrigger)
        assert options["kwargs"] == {"sid": sid, "scheduled_time": "2099-01-01 10:00"}

    asyncio.run(run())


def test_fired_followup_updates_intent_and_cleanup_only_removes_autonomous(
    reminder_main, tmp_path: Path
):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        sid = "qq:dm:10001"
        reminder = {
            "job_id": "auto-job", "intent_id": "intent-1",
            "source": "autonomous_intent_loop",
        }
        ordinary = {"job_id": "user-job", "intent_id": "intent-1", "source": "user"}
        await plugin._autonomy_storage.save({
            "sessions": {sid: {"intents": [{
                "id": "intent-1", "next_check_at": "2099-01-01 10:00",
                "next_check_job_id": "auto-job",
            }]}}
        })
        await plugin._storage.save({sid: [reminder, ordinary]})
        plugin._scheduler.add_job(lambda: None, id="auto-job")

        await plugin._mark_autonomous_followup_fired(sid, reminder)
        intent = (await plugin._autonomy_storage.load())["sessions"][sid]["intents"][0]
        assert intent["last_followup_job_id"] == "auto-job"
        assert intent["next_check_at"] == ""
        assert intent["next_check_job_id"] == ""

        assert await plugin._remove_autonomous_reminders(sid, intent_id="intent-1") == 1
        assert (await plugin._storage.load())[sid] == [ordinary]
        assert plugin._scheduler.jobs == []

    asyncio.run(run())


def test_intent_crud_keeps_text_and_state_shape(reminder_main, tmp_path: Path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        coordinator = plugin._autonomy_coordinator()
        sid = "qq:dm:10001"

        assert await coordinator.create_intent(sid, "  ") == "❌ 意图标题不能为空"
        created = await coordinator.create_intent(sid, "  整理计划  ", "  背景  ", 2.0)
        intent_id = created.rsplit("id: ", 1)[1]
        assert created == f"已创建自主意图: 整理计划\nid: {intent_id}"
        state = await plugin._autonomy_storage.load()
        intent = state["sessions"][sid]["intents"][0]
        assert intent["priority"] == 1.0
        assert intent["notes"] == "背景"
        assert intent["status"] == "active"

        assert await coordinator.update_intent(
            sid, intent_id, title="新计划", status="paused", priority=0.3
        ) == "已更新自主意图: 新计划"
        assert "新计划" in await coordinator.list_intents(sid)
        assert await coordinator.close_intent(sid, intent_id, cancel_followup=False) == (
            f"已关闭自主意图: {intent_id}"
        )
        assert await coordinator.list_intents(sid) == "当前会话没有自主意图"
        assert "status: closed" in await coordinator.list_intents(sid, include_closed=True)

    asyncio.run(run())


def test_schedule_intent_followup_keeps_reminder_identity_and_cleanup(
    reminder_main, tmp_path: Path
):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        coordinator = plugin._autonomy_coordinator()
        sid = "qq:dm:10001"
        created = await coordinator.create_intent(sid, "检查事项")
        intent_id = created.rsplit("id: ", 1)[1]
        principal = reminder_main.PrincipalContext(
            kind=reminder_main.PrincipalKind.BOT,
            principal_id="bot:qq:123",
            origin=reminder_main.EventOrigin.AUTONOMY_DAILY_REFLECTION,
            session_id=sid,
            bot_id="123",
        )
        result = await coordinator.schedule_intent_followup(
            sid, principal, intent_id, "2099-01-01 10:00"
        )
        assert result.startswith("已安排自主跟进: 检查事项\n时间: 2099-01-01 10:00\njob_id: ")
        reminder = (await plugin._storage.load())[sid][0]
        assert reminder["creator_id"] == "bot:qq:123"
        assert reminder["owner_type"] == "bot"
        assert reminder["identity_schema"] == 3
        assert reminder["owner_adapter_name"] == "qq"
        assert reminder["created_by_adapter_name"] == "qq"
        assert reminder["source"] == "autonomous_intent_loop"
        assert reminder["managed_by"] == "reminder_plugin.autonomous"
        assert reminder["intent_id"] == intent_id
        assert plugin._scheduler.jobs[0][1]["id"] == reminder["job_id"]
        intent = (await plugin._autonomy_storage.load())["sessions"][sid]["intents"][0]
        assert intent["next_check_job_id"] == reminder["job_id"]

        assert await coordinator.close_intent(sid, intent_id) == (
            f"已关闭自主意图: {intent_id}，已取消 1 个后续检查提醒"
        )
        assert (await plugin._storage.load())[sid] == []
        assert plugin._scheduler.jobs == []

    asyncio.run(run())


def test_lifecycle_reinitialization_registers_one_set_of_jobs(
    reminder_main, tmp_path: Path, monkeypatch
):
    class LifecycleScheduler(FakeScheduler):
        instances = []

        def __init__(self):
            super().__init__()
            self.running = False
            self.instances.append(self)

        def start(self):
            self.running = True

        def shutdown(self, wait=False):
            self.running = False

    monkeypatch.setattr(reminder_main, "AsyncIOScheduler", LifecycleScheduler)

    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        plugin._scheduler = None
        plugin._pending = {}
        plugin._health_task = None

        async def no_migration():
            return None

        async def idle_health_check():
            await asyncio.Event().wait()

        plugin._migrate_identity_schema = no_migration
        plugin._initialize_adapter_acl = no_migration
        plugin._health_check_loop = idle_health_check
        for _ in range(2):
            await plugin.initialize()
            scheduler = plugin._scheduler
            assert scheduler.running
            assert [options["id"] for _, options in scheduler.jobs] == [
                "reminder_autonomous_daily_cycle", "reminder_autonomous_followup_due"
            ]
            await plugin.terminate()
            assert not scheduler.running
            assert plugin._health_task is None
        assert len(LifecycleScheduler.instances) == 2

    asyncio.run(run())
