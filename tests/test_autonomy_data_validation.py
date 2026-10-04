"""Preserve malformed autonomous state while keeping ordinary reminders usable."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from test_autonomy import FakeScheduler, make_autonomy_plugin


SID = "qq:dm:10001"
CORRUPT_STATES = [
    pytest.param({"sessions": None}, id="null-sessions"),
    pytest.param({"sessions": []}, id="list-sessions"),
    pytest.param({"sessions": {SID: None}}, id="null-session"),
    pytest.param({"sessions": {SID: []}}, id="list-session"),
    pytest.param({"sessions": {SID: {"intents": "invalid"}}}, id="string-intents"),
    pytest.param({"sessions": {SID: {"intents": None}}}, id="null-intents"),
    pytest.param({"sessions": {SID: {"intents": {}}}}, id="object-intents"),
    pytest.param({"sessions": {SID: {"intents": [None]}}}, id="null-intent-entry"),
    pytest.param({"sessions": {SID: {"intents": ["invalid"]}}}, id="string-intent-entry"),
    pytest.param({"sessions": {SID: {"random_check_times": None}}}, id="null-times"),
    pytest.param({"sessions": {SID: {"random_check_times": {}}}}, id="object-times"),
    pytest.param({"sessions": {SID: {"random_check_times": [1]}}}, id="numeric-time-entry"),
]


def write_corrupt_fixture(storage, state):
    storage.path.write_text(json.dumps(state, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    return storage.path.read_bytes()


@pytest.mark.parametrize("state", CORRUPT_STATES)
@pytest.mark.parametrize("operation", ["load", "save", "modify"])
def test_storage_rejects_existing_corruption_without_changing_file(
    reminder_main, tmp_path, state, operation,
):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        storage = plugin._autonomy_storage
        original = write_corrupt_fixture(storage, state)
        with pytest.raises(reminder_main.ReminderStorageError):
            if operation == "load":
                await storage.load()
            elif operation == "save":
                await storage.save({"sessions": {}})
            else:
                async with storage.modify() as data:
                    pytest.fail("Corrupt state must be rejected before yielding the transaction")
        assert storage.path.read_bytes() == original
        assert list(tmp_path.glob("*.tmp")) == []

    asyncio.run(run())


@pytest.mark.parametrize("state", CORRUPT_STATES)
def test_invalid_candidate_is_rejected_before_commit(reminder_main, tmp_path, state):
    async def run():
        storage = make_autonomy_plugin(reminder_main, tmp_path)._autonomy_storage
        await storage.save({"sessions": {}, "custom": "keep"})
        original = storage.path.read_bytes()
        callbacks = []
        with pytest.raises(reminder_main.ReminderStorageError):
            async with storage.modify(after_save=lambda *_: callbacks.append(True)) as data:
                data.clear()
                data.update(copy.deepcopy(state))
        assert storage.path.read_bytes() == original
        assert callbacks == []
        assert list(tmp_path.glob("*.tmp")) == []

    asyncio.run(run())


@pytest.mark.parametrize("state", CORRUPT_STATES)
def test_invalid_candidate_does_not_create_a_new_file(reminder_main, tmp_path, state):
    async def run():
        storage = make_autonomy_plugin(reminder_main, tmp_path)._autonomy_storage
        with pytest.raises(reminder_main.ReminderStorageError):
            await storage.save(copy.deepcopy(state))
        assert not storage.path.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["list", "create", "update", "close", "followup"])
def test_intent_tools_report_corruption_and_preserve_both_state_files(
    reminder_main, tmp_path, operation,
):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        original = write_corrupt_fixture(plugin._autonomy_storage, {
            "sessions": {SID: {"intents": "invalid"}},
        })
        await plugin._storage.save({SID: [{"job_id": "ordinary", "content": "keep"}]})
        reminders = plugin._storage.path.read_bytes()
        plugin._check_autonomy_tool_access = lambda *_args, **_kwargs: (True, "", SID)
        plugin._get_principal = lambda _event: reminder_main.PrincipalContext(
            reminder_main.PrincipalKind.WEB, "web-admin", trusted=True,
        )
        event = SimpleNamespace()
        calls = {
            "list": (plugin.list_autonomous_intents, (event,)),
            "create": (plugin.create_autonomous_intent, (event, "new")),
            "update": (plugin.update_autonomous_intent, (event, "intent-1", "changed")),
            "close": (plugin.close_autonomous_intent, (event, "intent-1")),
            "followup": (plugin.schedule_intent_followup, (event, "intent-1", "2099-01-01 10:00")),
        }
        callback, args = calls[operation]
        result = await callback(*args)
        assert result.startswith("❌ 自主状态数据不可用，原文件已保留")
        assert plugin._autonomy_storage.path.read_bytes() == original
        assert plugin._storage.path.read_bytes() == reminders
        assert plugin._scheduler.jobs == []

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["daily", "random", "due", "plan", "fired"])
def test_background_jobs_reject_corrupt_state_without_publishing_or_scheduling(
    reminder_main, tmp_path, operation,
):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        plugin.config.random_check_enabled = True
        original = write_corrupt_fixture(plugin._autonomy_storage, {
            "sessions": {SID: {"intents": "invalid"}},
        })
        published = []

        async def publish(*args, **kwargs):
            published.append(True)

        plugin._publish_autonomous_notice = publish
        coordinator = plugin._autonomy_coordinator()
        calls = {
            "daily": (coordinator.daily_cycle_job, ()),
            "random": (coordinator.random_check_job, (SID,)),
            "due": (coordinator.followup_due_job, ()),
            "plan": (coordinator.schedule_random_checks, ()),
            "fired": (coordinator.mark_followup_fired, (SID, {"intent_id": "intent-1"})),
        }
        callback, args = calls[operation]
        with pytest.raises(reminder_main.ReminderStorageError):
            await callback(*args)
        assert plugin._autonomy_storage.path.read_bytes() == original
        assert published == [] and plugin._scheduler.jobs == []

    asyncio.run(run())


def test_response_hook_boundary_preserves_corrupt_state_without_raising(reminder_main, tmp_path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        original = write_corrupt_fixture(plugin._autonomy_storage, {
            "sessions": {SID: {"intents": "invalid"}},
        })
        await plugin._mark_autonomous_followup_fired(SID, {"intent_id": "intent-1"})
        assert plugin._autonomy_storage.path.read_bytes() == original

    asyncio.run(run())


def test_other_corrupt_session_cannot_be_overwritten_by_a_healthy_session(reminder_main, tmp_path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        original = write_corrupt_fixture(plugin._autonomy_storage, {
            "sessions": {SID: {}, "other:dm:user": {"intents": "invalid"}},
        })
        with pytest.raises(reminder_main.ReminderStorageError):
            await plugin._autonomy_coordinator().create_intent(SID, "new")
        assert plugin._autonomy_storage.path.read_bytes() == original

    asyncio.run(run())


@pytest.mark.parametrize("state", [
    {}, {"custom_root": {"keep": True}},
    {"sessions": {SID: {"custom": "keep", "intents": [{"id": "old", "custom": 123}]}}},
])
def test_missing_fields_and_unknown_fields_remain_compatible(reminder_main, tmp_path, state):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await plugin._autonomy_storage.save(state)
        coordinator = plugin._autonomy_coordinator()
        assert (await coordinator.create_intent(SID, "new")).startswith("已创建")
        after = await plugin._autonomy_storage.load()
        for key, value in state.items():
            if key != "sessions":
                assert after[key] == value
        session = after["sessions"][SID]
        assert session["random_check_times"] == []
        if "sessions" in state:
            assert session["custom"] == "keep"
            assert session["intents"][0] == state["sessions"][SID]["intents"][0]
        assert session["intents"][-1]["title"] == "new"

    asyncio.run(run())


def test_optional_validator_does_not_impose_autonomy_schema_on_other_stores(reminder_main, tmp_path):
    async def run():
        storage = reminder_main.ReminderStorage(tmp_path / "ordinary.json")
        data = {"sessions": "unrelated format", SID: [{"job_id": "ordinary"}]}
        await storage.save(data)
        assert await storage.load() == data

    asyncio.run(run())


def test_initialization_keeps_ordinary_jobs_when_autonomy_state_is_corrupt(
    reminder_main, tmp_path, monkeypatch,
):
    class RunningScheduler(FakeScheduler):
        running = False

        def start(self):
            self.running = True

        def shutdown(self, wait=False):
            self.running = False

    async def skip_setup(*args):
        pass

    async def run():
        monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)
        monkeypatch.setattr(reminder_main, "AsyncIOScheduler", RunningScheduler)
        plugin = reminder_main.ReminderPlugin(SimpleNamespace(), {
            "autonomy_enabled": True, "autonomy_mode": "plan_only", "allowed_sessions": [SID],
            "random_check_enabled": True,
        })
        monkeypatch.setattr(plugin, "_initialize_adapter_acl", skip_setup)
        monkeypatch.setattr(plugin, "_migrate_identity_schema", skip_setup)
        original = write_corrupt_fixture(plugin._autonomy_storage, {
            "sessions": {SID: {"intents": "invalid"}},
        })
        await plugin._storage.save({SID: [{
            "job_id": "ordinary", "content": "keep", "time": "2099-01-01 10:00", "repeat": "none",
        }]})
        try:
            await plugin.initialize()
            assert plugin._scheduler.running
            assert [options["id"] for _, options in plugin._scheduler.jobs] == ["ordinary"]
            assert plugin._autonomy_storage.path.read_bytes() == original
            assert plugin._health_task is not None
        finally:
            health_task = plugin._health_task
            await plugin.terminate()
            if health_task is not None:
                await asyncio.gather(health_task, return_exceptions=True)

    asyncio.run(run())
