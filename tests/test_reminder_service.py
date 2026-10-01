"""Behavioral contracts for reminder mutations during service extraction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


def make_plugin(reminder_main, path: Path):
    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin._storage = reminder_main.ReminderStorage(path)
    plugin._pending = {}
    plugin.config = SimpleNamespace(admin_users=[])
    plugin._get_sid = lambda _event: "qq:dm:10001"
    plugin._check_permission = lambda *_args: True
    plugin._check_create_permission = lambda _event: (True, "")
    plugin._check_action_permission = lambda _event, _action: (True, "")
    plugin._get_creator_info = lambda _event: {"creator_id": "10001", "creator_name": "Alice"}
    plugin._identity_fields_for_event = lambda _event: {"owner_type": "user", "owner_id": "10001"}
    removed = []
    scheduled = []
    plugin._scheduler = SimpleNamespace(remove_job=removed.append)
    plugin._add_job = lambda sid, record: scheduled.append((sid, record["job_id"]))
    return plugin, removed, scheduled


def test_important_flag_transitions_and_permission(reminder_main, tmp_path: Path):
    async def run():
        plugin, removed, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        await plugin._storage.save({"qq:dm:10001": [{"job_id": "job-1", "content": "test"}]})
        event = SimpleNamespace()

        assert await plugin.mark_reminder_important(event, "missing") == "找不到任务: missing"
        plugin._check_permission = lambda *_args: False
        assert await plugin.mark_reminder_important(event, "job-1") == (
            "❌ 权限拒绝：您无权操作该任务 (创建人: 未知)"
        )
        plugin._check_permission = lambda *_args: True
        assert await plugin.mark_reminder_important(event, "job-1") == "设为重要: test"
        assert await plugin.mark_reminder_important(event, "job-1") == "已设为重要: test"
        assert await plugin.unmark_reminder_important(event, "job-1") == "取消重要标记: test"
        assert await plugin.unmark_reminder_important(event, "job-1") == "并非重要提醒: test"
        assert (await plugin._storage.load())["qq:dm:10001"][0]["important"] is False
        assert removed == scheduled == []

    asyncio.run(run())


def test_pause_resume_preserves_scheduler_calls(reminder_main, tmp_path: Path):
    async def run():
        plugin, removed, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        sid = "qq:dm:10001"
        await plugin._storage.save({sid: [{
            "job_id": "job-1", "content": "test", "repeat": "daily", "time": "2026-10-01 08:30"
        }]})
        event = SimpleNamespace()

        plugin._check_permission = lambda *_args: False
        assert await plugin.pause_reminder(event, "job-1") == (
            "❌ 权限拒绝：您无权操作该任务 (创建人: 未知)"
        )
        plugin._check_permission = lambda *_args: True
        assert await plugin.pause_reminder(event, "job-1") == "已暂停: test"
        assert await plugin.pause_reminder(event, "job-1") == "已经暂停: test"
        assert removed == ["job-1"]
        assert await plugin.resume_reminder(event, "job-1") == "已恢复: test"
        assert await plugin.resume_reminder(event, "job-1") == "已经处于活动状态: test"
        assert scheduled == [(sid, "job-1")]
        assert (await plugin._storage.load())[sid][0]["paused"] is False

    asyncio.run(run())


def test_create_preserves_fields_validation_and_schedule(reminder_main, tmp_path: Path):
    async def run():
        plugin, removed, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        event = SimpleNamespace()
        assert await plugin.set_reminder(event, "test", "invalid") == (
            "❌ 时间格式错误，请使用 YYYY-MM-DD HH:MM 格式"
        )
        assert await plugin.set_reminder(event, "test", "2099-01-01 10:00", repeat="bad") == (
            "❌ repeat 参数无效"
        )
        assert await plugin.set_reminder(event, "test", "2000-01-01 10:00") == (
            "❌ 不能设置过去的时间"
        )
        assert await plugin.set_reminder(
            event, "test", "2099-01-01 10:00", repeat="interval", interval_minutes=30,
            category="work"
        ) == "已添加: test\n[2099-01-01 10:00 (每30分钟)]"
        records = (await plugin._storage.load())["qq:dm:10001"]
        assert len(records) == 1
        record = records[0]
        assert {key: record[key] for key in (
            "content", "time", "repeat", "interval_minutes", "category",
            "creator_id", "creator_name", "session_type", "owner_type", "owner_id"
        )} == {
            "content": "test", "time": "2099-01-01 10:00", "repeat": "interval",
            "interval_minutes": 30, "category": "work", "creator_id": "10001",
            "creator_name": "Alice", "session_type": "dm", "owner_type": "user",
            "owner_id": "10001",
        }
        assert record["job_id"].startswith("reminder_qq:dm:10001_")
        assert scheduled == [("qq:dm:10001", record["job_id"])]
        assert removed == []

    asyncio.run(run())


def test_edit_preserves_validation_and_rescheduling(reminder_main, tmp_path: Path):
    async def run():
        plugin, removed, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        sid = "qq:dm:10001"
        await plugin._storage.save({sid: [{
            "job_id": "job-1", "content": "old", "time": "2099-01-01 10:00", "repeat": "none"
        }]})
        event = SimpleNamespace()
        assert await plugin.edit_reminder(event, "job-1", time="bad") == (
            "时间格式需为 YYYY-MM-DD HH:MM"
        )
        plugin._check_permission = lambda *_args: False
        assert await plugin.edit_reminder(event, "job-1", content="new") == (
            "❌ 权限拒绝：您无权操作该任务 (创建人: 未知)"
        )
        plugin._check_permission = lambda *_args: True
        assert await plugin.edit_reminder(event, "job-1", content="new", time="2099-01-02 10:00") == (
            "已更新: new\n[2099-01-02 10:00]"
        )
        record = (await plugin._storage.load())[sid][0]
        assert record["content"] == "new"
        assert record["time"] == "2099-01-02 10:00"
        assert removed == ["job-1"]
        assert scheduled == [(sid, "job-1")]

    asyncio.run(run())


def test_random_batch_create_and_delete_preserve_record_shape(reminder_main, tmp_path: Path):
    async def run():
        plugin, removed, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        sid = "qq:dm:10001"
        event = SimpleNamespace()
        response = await plugin.set_reminder(
            event,
            "random test",
            "2099-01-01 10:00",
            time_range_end="2099-01-01 12:00",
            random_count=2,
        )
        assert response.startswith("已添加随机待办: random test (共2次)\n时间列表:\n")
        records = (await plugin._storage.load())[sid]
        assert len(records) == 2
        assert records[0]["random_batch_id"] == records[1]["random_batch_id"]
        assert [r["random_index"] for r in records] == [1, 2]
        assert all(r["random_total"] == 2 and r["is_random"] for r in records)
        assert all(r["time_range"] == {
            "start": "2099-01-01 10:00", "end": "2099-01-01 12:00"
        } for r in records)
        assert [job_id for _, job_id in scheduled] == [r["job_id"] for r in records]

        assert await plugin.delete_reminder(
            event, records[0]["job_id"], delete_batch=True
        ) == "已批量删除: random test (共2项)"
        assert (await plugin._storage.load())[sid] == []
        assert removed == [r["job_id"] for r in records]

    asyncio.run(run())
