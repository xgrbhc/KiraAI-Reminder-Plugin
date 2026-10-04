"""Storage failures must never replace an unreadable reminder document."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import attach_delivery


def storage_error_type(reminder_main):
    storage_module = sys.modules[reminder_main.ReminderStorage.__module__]
    return storage_module.ReminderStorageError


@pytest.mark.parametrize("payload", [b"", b"{", b"\xff", b"[]", b"null", b"42"])
def test_invalid_existing_json_blocks_load_modify_and_save(
    reminder_main, tmp_path: Path, payload: bytes
):
    async def run():
        path = tmp_path / "reminders.json"
        path.write_bytes(payload)
        storage = reminder_main.ReminderStorage(path)
        storage_error = storage_error_type(reminder_main)

        with pytest.raises(storage_error):
            await storage.load()
        with pytest.raises(storage_error):
            async with storage.modify() as data:
                data["qq:dm:10001"] = []
        with pytest.raises(storage_error):
            await storage.save({"qq:dm:10001": []})

        assert path.read_bytes() == payload
        assert list(tmp_path.glob("*.tmp")) == []

    asyncio.run(run())


@pytest.mark.parametrize("read_error", [PermissionError("read denied"), FileNotFoundError("vanished")])
def test_read_error_blocks_all_writes(reminder_main, tmp_path: Path, monkeypatch, read_error):
    async def run():
        path = tmp_path / "reminders.json"
        original = b'{"qq:dm:10001": []}'
        path.write_bytes(original)
        storage = reminder_main.ReminderStorage(path)
        storage_error = storage_error_type(reminder_main)
        real_read_text = Path.read_text

        def denied_read(candidate, *args, **kwargs):
            if candidate == path:
                raise read_error
            return real_read_text(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied_read)
        with pytest.raises(storage_error):
            await storage.load()
        with pytest.raises(storage_error):
            async with storage.modify() as data:
                data.clear()
        with pytest.raises(storage_error):
            await storage.save({})
        assert path.read_bytes() == original

    asyncio.run(run())


def test_missing_file_still_initializes_as_empty(reminder_main, tmp_path: Path):
    async def run():
        path = tmp_path / "reminders.json"
        storage = reminder_main.ReminderStorage(path)
        assert await storage.load() == {}
        assert not path.exists()
        async with storage.modify() as data:
            data["qq:dm:10001"] = []
        assert await storage.load() == {"qq:dm:10001": []}

    asyncio.run(run())


def test_restore_does_not_overwrite_corrupt_reminders(reminder_main, tmp_path: Path):
    async def run():
        path = tmp_path / "reminders.json"
        path.write_bytes(b"{corrupt")
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(path)
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._scheduler = None

        with pytest.raises(storage_error_type(reminder_main)):
            await plugin._restore_jobs()
        assert path.read_bytes() == b"{corrupt"

    asyncio.run(run())


def test_corrupt_reminders_stop_initialization_before_scheduler_starts(
    reminder_main, tmp_path: Path, monkeypatch
):
    async def run():
        path = tmp_path / "reminders.json"
        path.write_bytes(b"{corrupt")
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(path)
        plugin.config = reminder_main.ReminderConfig()
        attach_delivery(plugin, reminder_main, tmp_path)
        scheduler_started = False

        def scheduler_factory():
            nonlocal scheduler_started
            scheduler_started = True
            raise AssertionError("scheduler must not start")

        monkeypatch.setattr(reminder_main, "AsyncIOScheduler", scheduler_factory)
        with pytest.raises(storage_error_type(reminder_main)):
            await plugin.initialize()
        assert not scheduler_started
        assert path.read_bytes() == b"{corrupt"

    asyncio.run(run())


def test_failed_autonomy_load_skips_autonomy_without_stopping_the_plugin(
    reminder_main, tmp_path: Path, monkeypatch
):
    class FakeScheduler:
        def __init__(self):
            self.running = False
            self.shutdown_called = False
            self.jobs = []

        def start(self):
            self.running = True

        def add_job(self, *_args, **kwargs):
            self.jobs.append(kwargs)

        def shutdown(self, wait=False):
            self.shutdown_called = True
            self.running = False

    async def run():
        scheduler = FakeScheduler()
        monkeypatch.setattr(reminder_main, "AsyncIOScheduler", lambda: scheduler)
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        bad_state = tmp_path / "autonomous_state.json"
        bad_state.write_bytes(b"{broken")
        plugin._autonomy_storage = reminder_main.ReminderStorage(bad_state)
        plugin._scheduler = None
        plugin._health_task = None
        plugin._pending = {}
        plugin.config = SimpleNamespace(
            admin_users=[],
            authorized_users=[],
            autonomy_enabled=True,
            autonomy_mode="plan_only",
            allowed_sessions=["qq:dm:10001"],
            daily_reflection_enabled=False,
            followup_due_enabled=False,
            random_check_enabled=True,
            random_check_daily_count=1,
            random_check_start_hour=10,
            random_check_end_hour=23,
        )

        try:
            await plugin.initialize()
            assert scheduler.running
            assert not scheduler.shutdown_called
            assert scheduler.jobs == []
            assert plugin._health_task is not None
            assert bad_state.read_bytes() == b"{broken"
        finally:
            health_task = plugin._health_task
            await plugin.terminate()
            if health_task is not None:
                await asyncio.gather(health_task, return_exceptions=True)
        assert scheduler.shutdown_called
        assert not scheduler.running
        assert plugin._health_task is None

    asyncio.run(run())
