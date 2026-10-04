"""Expected behavior for storage failures, job rejection, and compensation."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from conftest import attach_delivery
from test_autonomy import make_autonomy_plugin
from test_reminder_service import make_plugin


SID = "qq:dm:10001"


class JobRegistry:
    """Model successful replacement and failures without running any job."""

    def __init__(self):
        self.jobs = {}
        self.events = []
        self.reject_add = lambda record: False
        self.reject_remove = lambda job_id: False

    def add_job(self, func, **options):
        record = options["args"][1]
        self.events.append(("add", options["id"]))
        if self.reject_add(record):
            raise RuntimeError("test registration rejected")
        self.jobs[options["id"]] = copy.deepcopy(record)

    def remove_job(self, job_id):
        self.events.append(("remove", job_id))
        if self.reject_remove(job_id):
            raise RuntimeError("test removal rejected")
        if job_id not in self.jobs:
            raise JobLookupError(job_id)
        del self.jobs[job_id]


def plugin_with_jobs(reminder_main, tmp_path):
    plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
    attach_delivery(plugin, reminder_main, tmp_path)
    plugin._scheduler = JobRegistry()
    plugin._add_job = lambda sid, record: reminder_main.ReminderPlugin._add_job(plugin, sid, record)
    return plugin


def record(job_id="original", **fields):
    return {
        "job_id": job_id, "content": "original", "time": "2099-01-01 10:00",
        "repeat": "daily", **fields,
    }


async def seed(plugin, records):
    await plugin._storage.save({SID: records})
    for item in records:
        if not item.get("paused"):
            plugin._add_job(SID, item)
    plugin._scheduler.events.clear()


async def invoke(plugin, operation, token=""):
    event = SimpleNamespace()
    if operation == "create":
        return await plugin.set_reminder(event, "new", "2099-01-02 10:00")
    if operation == "random":
        return await plugin.set_reminder(
            event, "new", "2099-01-02 10:00", time_range_end="2099-01-02 12:00", random_count=3,
        )
    if operation == "edit":
        return await plugin.edit_reminder(event, "original", content="new", time="2099-01-02 10:00")
    if operation == "confirm":
        return await plugin.confirm_delete_reminder(event, token)
    return await getattr(plugin, operation + "_reminder")(event, "original")


@pytest.mark.parametrize("operation", ["create", "random", "edit", "pause", "resume", "delete", "confirm"])
def test_save_failure_never_changes_jobs_or_original_data(reminder_main, tmp_path, monkeypatch, operation):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        await seed(plugin, [record(paused=operation == "resume", important=operation == "confirm")])
        token = ""
        if operation == "confirm":
            result = await plugin.delete_reminder(SimpleNamespace(), "original")
            token = result.rsplit(" ", 1)[-1]
            assert token in plugin._pending
        original_bytes = plugin._storage.path.read_bytes()
        original_jobs = copy.deepcopy(plugin._scheduler.jobs)

        def denied(data):
            assert plugin._scheduler.events == []
            raise OSError("test save rejected")

        monkeypatch.setattr(plugin._storage, "_unsafe_save", denied)
        result = await invoke(plugin, operation, token)
        assert "出错" in result
        assert plugin._storage.path.read_bytes() == original_bytes
        assert plugin._scheduler.jobs == original_jobs
        assert plugin._scheduler.events == []
        if token:
            assert token in plugin._pending

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["create", "random", "edit", "resume"])
def test_registration_failure_rolls_back_records_and_keeps_old_jobs(reminder_main, tmp_path, operation):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        records = [record(paused=operation == "resume")]
        await seed(plugin, records)
        original_jobs = copy.deepcopy(plugin._scheduler.jobs)
        if operation == "random":
            plugin._scheduler.reject_add = lambda item: item.get("random_index") == 2
        elif operation == "resume":
            plugin._scheduler.reject_add = lambda item: True
        else:
            plugin._scheduler.reject_add = lambda item: item.get("content") == "new"
        result = await invoke(plugin, operation)
        assert "出错" in result and "登记失败" in result
        assert await plugin._storage.load() == {SID: records}
        assert plugin._scheduler.jobs == original_jobs
        if operation == "edit":
            assert not any(kind == "remove" for kind, _ in plugin._scheduler.events)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["pause", "delete", "confirm"])
def test_removal_failure_rolls_back_without_consuming_delete_token(reminder_main, tmp_path, operation):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        records = [record(important=operation == "confirm")]
        await seed(plugin, records)
        token = ""
        if operation == "confirm":
            token = (await plugin.delete_reminder(SimpleNamespace(), "original")).rsplit(" ", 1)[-1]
        plugin._scheduler.reject_remove = lambda job_id: True
        result = await invoke(plugin, operation, token)
        assert "出错" in result
        assert await plugin._storage.load() == {SID: records}
        assert plugin._scheduler.jobs == {"original": records[0]}
        if token:
            assert token in plugin._pending
            plugin._scheduler.reject_remove = lambda job_id: False
            assert (await invoke(plugin, operation, token)).startswith("已删除")
            assert token not in plugin._pending

    asyncio.run(run())


def test_partial_batch_removal_restores_already_removed_jobs(reminder_main, tmp_path):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        records = [record(random_batch_id="batch"), record("second", random_batch_id="batch")]
        await seed(plugin, records)
        plugin._scheduler.reject_remove = lambda job_id: job_id == "second"
        result = await plugin.delete_reminder(SimpleNamespace(), "original", delete_batch=True)
        assert "出错" in result
        assert await plugin._storage.load() == {SID: records}
        assert plugin._scheduler.jobs == {item["job_id"]: item for item in records}

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["pause", "delete"])
def test_missing_job_removal_is_idempotent(reminder_main, tmp_path, operation):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        await seed(plugin, [record()])
        plugin._scheduler.jobs.clear()
        assert (await invoke(plugin, operation)).startswith("已")
        assert plugin._scheduler.jobs == {}

    asyncio.run(run())


def test_scheduler_missing_is_an_error_not_creation_success(reminder_main, tmp_path):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        await plugin._storage.save({SID: [record()]})
        plugin._scheduler = None
        result = await invoke(plugin, "create")
        assert "调度器不可用" in result and not result.startswith("已添加")
        assert await plugin._storage.load() == {SID: [record()]}

    asyncio.run(run())


def test_real_scheduler_failed_edit_keeps_original_job(reminder_main, tmp_path, monkeypatch):
    scheduler = BackgroundScheduler()
    scheduler.start(paused=True)
    try:
        async def run():
            plugin = plugin_with_jobs(reminder_main, tmp_path)
            plugin._scheduler = scheduler
            original = record()
            await plugin._storage.save({SID: [original]})
            plugin._add_job(SID, original)
            job = scheduler.get_job("original")
            next_run = job.next_run_time
            real_add = scheduler.add_job

            def rejected(func, **options):
                if options["args"][1]["content"] == "new":
                    raise RuntimeError("test registration rejected")
                return real_add(func, **options)

            monkeypatch.setattr(scheduler, "add_job", rejected)
            assert "修改出错" in await invoke(plugin, "edit")
            assert await plugin._storage.load() == {SID: [original]}
            assert scheduler.get_job("original") is job
            assert job.next_run_time == next_run
            assert job.args == (SID, original)

        asyncio.run(run())
    finally:
        scheduler.shutdown(wait=False)


def test_commit_callback_runs_after_save_under_same_lock(reminder_main, tmp_path):
    async def run():
        storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        original = {SID: [record()]}
        await storage.save(original)

        def fail(before, after):
            assert storage._lock.locked()
            assert before == original
            assert json.loads(storage.path.read_text(encoding="utf-8")) == after
            raise RuntimeError("test callback failed")

        with pytest.raises(RuntimeError, match="callback failed"):
            async with storage.modify(after_save=fail) as data:
                data[SID] = []
        assert await storage.load() == original
        assert not storage._lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["create", "edit", "resume"])
def test_real_scheduler_error_after_registration_restores_previous_state(reminder_main, tmp_path, monkeypatch, operation):
    scheduler = BackgroundScheduler()
    scheduler.start(paused=True)
    try:
        async def run():
            plugin = plugin_with_jobs(reminder_main, tmp_path)
            plugin._scheduler = scheduler
            original = record(paused=operation == "resume")
            await plugin._storage.save({SID: [original]})
            if not original["paused"]:
                plugin._add_job(SID, original)
            old_job = scheduler.get_job("original")
            next_run = old_job.next_run_time if old_job else None
            real_add = scheduler.add_job
            failed = False

            def fail_after_store(func, **options):
                nonlocal failed
                result = real_add(func, **options)
                if not failed:
                    failed = True
                    raise RuntimeError("test wakeup failed after registration")
                return result

            monkeypatch.setattr(scheduler, "add_job", fail_after_store)
            assert "出错" in await invoke(plugin, operation)
            assert await plugin._storage.load() == {SID: [original]}
            jobs = scheduler.get_jobs()
            if operation == "resume":
                assert jobs == []
            else:
                assert len(jobs) == 1 and jobs[0].id == "original"
                assert jobs[0].next_run_time == next_run
                assert jobs[0].args == (SID, original)

        asyncio.run(run())
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.parametrize("operation", ["pause", "delete"])
def test_real_scheduler_error_after_removal_restores_job(reminder_main, tmp_path, monkeypatch, operation):
    scheduler = BackgroundScheduler()
    scheduler.start(paused=True)
    try:
        async def run():
            plugin = plugin_with_jobs(reminder_main, tmp_path)
            plugin._scheduler = scheduler
            original = record(repeat="interval", time="2000-01-01 10:00", interval_minutes=30)
            await plugin._storage.save({SID: [original]})
            plugin._add_job(SID, original)
            next_run = scheduler.get_job("original").next_run_time
            real_remove = scheduler.remove_job

            def fail_after_remove(job_id):
                real_remove(job_id)
                raise RuntimeError("test remove observer failed")

            monkeypatch.setattr(scheduler, "remove_job", fail_after_remove)
            assert "出错" in await invoke(plugin, operation)
            assert await plugin._storage.load() == {SID: [original]}
            job = scheduler.get_job("original")
            assert job is not None
            assert job.next_run_time == next_run
            assert job.args == (SID, original)

        asyncio.run(run())
    finally:
        scheduler.shutdown(wait=False)


def test_real_batch_failure_preserves_interval_phase(reminder_main, tmp_path, monkeypatch):
    scheduler = BackgroundScheduler()
    scheduler.start(paused=True)
    try:
        async def run():
            plugin = plugin_with_jobs(reminder_main, tmp_path)
            plugin._scheduler = scheduler
            records = [record(job_id, repeat="interval", time="2000-01-01 10:00",
                              interval_minutes=30, random_batch_id="batch")
                       for job_id in ("original", "second")]
            await plugin._storage.save({SID: records})
            for item in records:
                plugin._add_job(SID, item)
            next_runs = {job.id: job.next_run_time for job in scheduler.get_jobs()}
            real_remove = scheduler.remove_job

            def reject_second(job_id):
                if job_id == "second":
                    raise RuntimeError("test second removal rejected")
                real_remove(job_id)

            monkeypatch.setattr(scheduler, "remove_job", reject_second)
            result = await plugin.delete_reminder(SimpleNamespace(), "original", delete_batch=True)
            assert "出错" in result
            assert await plugin._storage.load() == {SID: records}
            assert {job.id: job.next_run_time for job in scheduler.get_jobs()} == next_runs

        asyncio.run(run())
    finally:
        scheduler.shutdown(wait=False)


def test_failed_runtime_compensation_is_explicit(reminder_main, tmp_path):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        records = [record(random_batch_id="batch"), record("second", random_batch_id="batch")]
        await seed(plugin, records)
        plugin._scheduler.reject_remove = lambda job_id: job_id == "second"
        plugin._scheduler.reject_add = lambda item: True
        result = await plugin.delete_reminder(SimpleNamespace(), "original", delete_batch=True)
        assert "原任务恢复未完成" in result and not result.startswith("已删除")
        assert await plugin._storage.load() == {SID: records}

    asyncio.run(run())


def test_failed_data_compensation_is_explicit(reminder_main, tmp_path, monkeypatch):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        await seed(plugin, [record()])
        plugin._scheduler.reject_add = lambda item: item["content"] == "new"
        real_save = plugin._storage._unsafe_save
        calls = 0

        def fail_rollback(data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("test rollback write denied")
            real_save(data)

        monkeypatch.setattr(plugin._storage, "_unsafe_save", fail_rollback)
        result = await invoke(plugin, "create")
        assert "数据回滚也失败" in result and not result.startswith("已添加")
        assert plugin._scheduler.jobs == {"original": record()}
        assert not plugin._storage._lock.locked()
        assert len((await plugin._storage.load())[SID]) == 2

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["save", "register"])
def test_autonomous_replacement_failure_keeps_existing_followup(reminder_main, tmp_path, monkeypatch, failure):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        plugin._scheduler = JobRegistry()
        coordinator = plugin._autonomy_coordinator()
        await coordinator.create_intent(SID, "follow up")
        state = await plugin._autonomy_storage.load()
        intent_id = state["sessions"][SID]["intents"][0]["id"]
        principal = reminder_main.PrincipalContext(reminder_main.PrincipalKind.BOT, "bot:qq:test", trusted=True)
        assert (await coordinator.schedule_intent_followup(SID, principal, intent_id, "2099-01-01 10:00")).startswith("已安排")
        original_data = await plugin._storage.load()
        original_state = await plugin._autonomy_storage.load()
        original_jobs = copy.deepcopy(plugin._scheduler.jobs)
        plugin._scheduler.events.clear()
        if failure == "save":
            def denied(data):
                raise OSError("test save rejected")
            monkeypatch.setattr(plugin._storage, "_unsafe_save", denied)
        else:
            plugin._scheduler.reject_add = lambda item: item["time"] == "2099-01-02 10:00"
        result = await coordinator.schedule_intent_followup(SID, principal, intent_id, "2099-01-02 10:00")
        assert result.startswith("❌")
        assert await plugin._storage.load() == original_data
        assert await plugin._autonomy_storage.load() == original_state
        assert plugin._scheduler.jobs == original_jobs
        assert not any(kind == "remove" for kind, _ in plugin._scheduler.events)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["save", "remove"])
def test_autonomous_cleanup_failure_preserves_records_and_jobs(reminder_main, tmp_path, monkeypatch, failure):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        plugin._scheduler = JobRegistry()
        coordinator = plugin._autonomy_coordinator()
        original = record(source="autonomous_intent_loop", intent_id="intent-test")
        await plugin._storage.save({SID: [original]})
        plugin._add_job(SID, original)
        plugin._scheduler.events.clear()
        if failure == "save":
            def denied(data):
                raise OSError("test save rejected")
            monkeypatch.setattr(plugin._storage, "_unsafe_save", denied)
        else:
            plugin._scheduler.reject_remove = lambda job_id: True
        with pytest.raises((OSError, RuntimeError)):
            await coordinator.remove_autonomous_reminders(SID, intent_id="intent-test")
        assert await plugin._storage.load() == {SID: [original]}
        assert plugin._scheduler.jobs == {"original": original}
        if failure == "save":
            assert plugin._scheduler.events == []

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["pause", "resume"])
def test_web_api_reports_consistency_failure_as_error(reminder_main, tmp_path, monkeypatch, operation):
    async def run():
        plugin = plugin_with_jobs(reminder_main, tmp_path)
        plugin._build_web_event = lambda sid: SimpleNamespace(sid=sid)
        records = [record(paused=operation == "resume")]
        await seed(plugin, records)
        if operation == "pause":
            def denied(data):
                raise OSError("test save rejected")
            monkeypatch.setattr(plugin._storage, "_unsafe_save", denied)
        else:
            plugin._scheduler.reject_add = lambda item: True
        result = await plugin.api_action_reminders(operation, {"session_id": SID, "job_id": "original"})
        assert result["status"] == "error" and "出错" in result["msg"]
        assert await plugin._storage.load() == {SID: records}

    asyncio.run(run())
