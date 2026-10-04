"""Validate autonomous intent updates before opening a write transaction."""

import asyncio
import sys

import pytest

from test_autonomy import make_autonomy_plugin


SID = "qq:dm:10001"
UPDATED_AT = "2099-01-01 09:00"


async def seeded_plugin(reminder_main, tmp_path):
    plugin = make_autonomy_plugin(reminder_main, tmp_path)
    coordinator = plugin._autonomy_coordinator()
    session = reminder_main.ReminderPlugin._ensure_autonomy_session({}, SID)
    session["intents"] = [{
        "id": "intent-1",
        "title": "original",
        "notes": "original notes",
        "status": "active",
        "priority": 0.5,
        "created_at": "2000-01-01 10:00",
        "updated_at": "2000-01-01 10:00",
        "next_check_at": "2099-01-02 10:00",
        "next_check_job_id": "followup-1",
        "custom": {"keep": [1, 2]},
    }]
    await plugin._autonomy_storage.save({
        "sessions": {SID: session}, "custom_root": {"keep": True},
    })
    return plugin, coordinator


async def stored_intent(plugin):
    return (await plugin._autonomy_storage.load())["sessions"][SID]["intents"][0]


def freeze_update_time(monkeypatch, coordinator):
    module = sys.modules[coordinator.__class__.__module__]
    monkeypatch.setattr(module, "now_str", lambda: UPDATED_AT)


def forbid_storage_io(monkeypatch, storage):
    def unexpected_io(*args, **kwargs):
        pytest.fail("Invalid parameters must not read or save autonomous state")

    monkeypatch.setattr(storage, "_unsafe_load", unexpected_io)
    monkeypatch.setattr(storage, "_unsafe_save", unexpected_io)


@pytest.mark.parametrize("invalid_fields,error", [
    ({"status": "invalid"}, "❌ status 参数无效"),
    ({"status": "   "}, "❌ status 参数无效"),
    ({"status": 123}, "❌ status 参数无效"),
    ({"priority": "not-a-number"}, "❌ priority 需要是 0~1 的数字"),
    ({"priority": ""}, "❌ priority 需要是 0~1 的数字"),
    ({"priority": []}, "❌ priority 需要是 0~1 的数字"),
    ({"priority": {}}, "❌ priority 需要是 0~1 的数字"),
])
def test_invalid_fields_preserve_file_without_storage_io(
    reminder_main, tmp_path, monkeypatch, invalid_fields, error,
):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        original_bytes = plugin._autonomy_storage.path.read_bytes()
        forbid_storage_io(monkeypatch, plugin._autonomy_storage)
        fields = {
            "title": "changed", "notes": "changed notes",
            "status": "paused", "priority": 0.8,
            **invalid_fields,
        }
        result = await coordinator.update_intent(SID, "intent-1", **fields)
        assert result == error
        assert plugin._autonomy_storage.path.read_bytes() == original_bytes

    asyncio.run(run())


@pytest.mark.parametrize("fields,error", [
    ({"status": "invalid"}, "❌ status 参数无效"),
    ({"priority": "invalid"}, "❌ priority 需要是 0~1 的数字"),
])
def test_invalid_fields_do_not_create_a_state_file(
    reminder_main, tmp_path, monkeypatch, fields, error,
):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        forbid_storage_io(monkeypatch, plugin._autonomy_storage)
        result = await plugin._autonomy_coordinator().update_intent(
            SID, "missing", title="changed", **fields,
        )
        assert result == error
        assert not plugin._autonomy_storage.path.exists()

    asyncio.run(run())


def test_valid_fields_are_saved_once_and_preserve_unrelated_data(
    reminder_main, tmp_path, monkeypatch,
):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        before = await plugin._autonomy_storage.load()
        freeze_update_time(monkeypatch, coordinator)
        saves = []
        original_save = plugin._autonomy_storage._unsafe_save

        def count_save(data):
            saves.append(True)
            original_save(data)

        monkeypatch.setattr(plugin._autonomy_storage, "_unsafe_save", count_save)
        result = await coordinator.update_intent(
            SID, "intent-1", title="  changed  ", notes="  changed notes  ",
            status=" paused ", priority="0.8",
        )
        after = await plugin._autonomy_storage.load()
        expected = before["sessions"][SID]["intents"][0]
        expected.update({
            "title": "changed", "notes": "changed notes", "status": "paused",
            "priority": 0.8, "updated_at": UPDATED_AT,
        })
        assert result == "已更新自主意图: changed"
        assert after == before
        assert saves == [True]
        assert plugin._scheduler.jobs == []

    asyncio.run(run())


@pytest.mark.parametrize("title", [None, "", "   "])
def test_empty_title_keeps_the_existing_title(reminder_main, tmp_path, title):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        result = await coordinator.update_intent(SID, "intent-1", title=title)
        assert result == "已更新自主意图: original"
        assert (await stored_intent(plugin))["title"] == "original"

    asyncio.run(run())


@pytest.mark.parametrize("status", [None, "active", "paused", "waiting_confirmation", "closed"])
def test_supported_status_behavior_is_unchanged(reminder_main, tmp_path, status):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        result = await coordinator.update_intent(SID, "intent-1", status=status)
        assert result.startswith("已更新自主意图")
        assert (await stored_intent(plugin))["status"] == (status or "active")

    asyncio.run(run())


@pytest.mark.parametrize("priority,expected", [
    (None, 0.5), (0, 0.0), (0.5, 0.5), (1, 1.0), (-2, 0.0), (2, 1.0), ("0.3", 0.3),
])
def test_priority_conversion_and_clamping_are_unchanged(
    reminder_main, tmp_path, priority, expected,
):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        result = await coordinator.update_intent(SID, "intent-1", priority=priority)
        assert result.startswith("已更新自主意图")
        assert (await stored_intent(plugin))["priority"] == expected

    asyncio.run(run())


@pytest.mark.parametrize("notes,expected", [
    (None, "original notes"), ("", ""), ("   ", ""), ("  changed  ", "changed"),
])
def test_notes_omission_clearing_and_trimming_are_unchanged(
    reminder_main, tmp_path, notes, expected,
):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        result = await coordinator.update_intent(SID, "intent-1", notes=notes)
        assert result.startswith("已更新自主意图")
        assert (await stored_intent(plugin))["notes"] == expected

    asyncio.run(run())


def test_valid_fields_for_missing_target_keep_the_existing_error(reminder_main, tmp_path):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        original = await stored_intent(plugin)
        result = await coordinator.update_intent(SID, "missing", title="changed")
        assert result == "找不到自主意图: missing"
        assert await stored_intent(plugin) == original

    asyncio.run(run())


def test_update_without_fields_only_refreshes_timestamp(reminder_main, tmp_path, monkeypatch):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        expected = await stored_intent(plugin)
        freeze_update_time(monkeypatch, coordinator)
        result = await coordinator.update_intent(SID, "intent-1")
        expected["updated_at"] = UPDATED_AT
        assert result == "已更新自主意图: original"
        assert await stored_intent(plugin) == expected

    asyncio.run(run())


def test_save_failure_preserves_original_file(reminder_main, tmp_path, monkeypatch):
    async def run():
        plugin, coordinator = await seeded_plugin(reminder_main, tmp_path)
        original_bytes = plugin._autonomy_storage.path.read_bytes()

        def denied(data):
            raise OSError("test intent update write denied")

        monkeypatch.setattr(plugin._autonomy_storage, "_unsafe_save", denied)
        with pytest.raises(OSError, match="test intent update write denied"):
            await coordinator.update_intent(
                SID, "intent-1", title="changed", notes="changed notes",
                status="paused", priority=0.8,
            )
        assert plugin._autonomy_storage.path.read_bytes() == original_bytes

    asyncio.run(run())
