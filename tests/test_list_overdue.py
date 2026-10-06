"""Full list queries distinguish overdue one-time flags from recurring metadata."""

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from conftest import attach_delivery
from test_recurring_time import REPEATS, SID, record
from test_reminder_service import make_plugin
from test_scheduler import FakeScheduler


@pytest.mark.parametrize("repeat", REPEATS)
@pytest.mark.parametrize("kind", ["ordinary", "random", "autonomous"])
def test_full_mixed_list_keeps_one_time_format_and_recurring_schedule(reminder_main, tmp_path, repeat, kind):
    async def run():
        path = tmp_path / "reminders.json"
        plugin, removed, scheduled = make_plugin(reminder_main, path)
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._allowed_autonomy_sessions = lambda: []
        once = record("none", job_id="overdue", content="past one-time", time="2000-01-01 10:15")
        if kind == "random":
            once.update(is_random=True, random_index=1, random_total=2,
                        time_range={"start": "2000-01-01 10:00", "end": "2000-01-01 11:00"})
        elif kind == "autonomous":
            once.update(owner_type="bot", managed_by="reminder_plugin", intent_id="test-intent")
        records = [once, record("none", job_id="future", content="future one-time"),
                   record(repeat, content="recurring", interval_minutes=30),
                   record(repeat, job_id="paused", content="paused cycle", paused=True)]
        await plugin._storage.save({SID: records})
        original = path.read_bytes()
        queries = []
        job = SimpleNamespace(next_run_time=dt.datetime(2099, 1, 2, 10, 15).astimezone(), pending=False)

        def get_job(job_id):
            queries.append(job_id)
            return job

        plugin._scheduler = SimpleNamespace(running=True, get_job=get_job)
        result = await plugin.list_reminders(SimpleNamespace())
        assert "列出提醒失败" not in result
        assert "past one-time\n   时间: 2000-01-01 10:15" in result
        assert "future one-time\n   时间: 2099-01-01 10:15" in result
        assert "开始: 2099-01-01 10:15" in result
        assert "下次: 2099-01-02 10:15" in result and "下次: 已暂停" in result
        assert all(f"job_id: {item['job_id']}" in result for item in records)
        if kind == "random":
            assert "范围: 2000-01-01 10:00 ~ 2000-01-01 11:00 [随机 1/2]" in result
        assert queries == ["recurring"]
        assert path.read_bytes() == original
        assert removed == scheduled == []
    asyncio.run(run())


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("explicit_repeat", [False, True])
def test_overdue_once_with_or_without_repeat_key_keeps_original_output(reminder_main, tmp_path, paused, explicit_repeat):
    async def run():
        plugin, removed, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        once = record("none", time="2000-01-01 10:15", paused=paused)
        if not explicit_repeat:
            once.pop("repeat")
        await plugin._storage.save({SID: [once]})
        before = plugin._storage.path.read_bytes()
        result = await plugin._reminder_service().list_reminders(SimpleNamespace())
        assert "时间: 2000-01-01 10:15" in result
        assert "下次:" not in result and "列出提醒失败" not in result
        assert ("已暂停" in result) is paused
        assert plugin._storage.path.read_bytes() == before
        assert removed == scheduled == []
    asyncio.run(run())


def test_list_after_ignore_and_startup_restore_keeps_original_tasks(reminder_main, tmp_path):
    async def run():
        path = tmp_path / "reminders.json"
        plugin, _, _ = make_plugin(reminder_main, path)
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._allowed_autonomy_sessions = lambda: []
        records = [record("none", job_id="overdue", content="past one-time", time="2000-01-01 10:15"),
                   record("none", job_id="future", content="future one-time"), record(content="recurring")]
        await plugin._storage.save({SID: records})
        original = path.read_bytes()
        await plugin._delivery.reconcile(startup=True)
        issue = (await plugin._delivery_storage.load())[SID][0]
        assert "已记录" in await plugin.review_delivery_issue(SimpleNamespace(), issue["delivery_id"])
        assert "past one-time" in await plugin.list_reminders(SimpleNamespace())
        assert path.read_bytes() == original

        restarted, _, _ = make_plugin(reminder_main, path)
        attach_delivery(restarted, reminder_main, tmp_path)
        restarted._allowed_autonomy_sessions = lambda: []
        restarted._scheduler = FakeScheduler()
        await restarted._restore_jobs()
        jobs = [(callback, dict(options)) for callback, options in restarted._scheduler.jobs]
        before_query = path.read_bytes()
        result = await restarted.list_reminders(SimpleNamespace())
        assert "列出提醒失败" not in result
        assert all(item["content"] in result for item in records)
        assert "时间: 2000-01-01 10:15" in result and "下次: 调度不可用" in result
        assert path.read_bytes() == before_query == original
        assert restarted._scheduler.jobs == jobs
        assert [options["id"] for _, options in jobs] == ["future", "recurring"]
        receipts = (await restarted._delivery_storage.load())[SID]
        assert len(receipts) == 1 and receipts[0]["resolution"] == "dismiss"
        assert not any(row["status"] in {"failed", "unconfirmed", "legacy_unconfirmed"} for row in receipts)
    asyncio.run(run())


def test_overdue_metadata_does_not_bypass_list_permission_filter(reminder_main, tmp_path):
    async def run():
        plugin, _, _ = make_plugin(reminder_main, tmp_path / "reminders.json")
        records = [record("none", job_id="hidden", content="private overdue", time="2000-01-01 10:15"),
                   record("none", job_id="visible", content="visible once")]
        await plugin._storage.save({SID: records})
        plugin._is_admin_user = lambda _event: False
        plugin._check_permission = lambda _event, item, _operation: item["job_id"] == "visible"
        result = await plugin._reminder_service().list_reminders(SimpleNamespace())
        assert "visible once" in result and "private overdue" not in result
        assert "列出提醒失败" not in result
        assert (await plugin._storage.load())[SID] == records
    asyncio.run(run())
