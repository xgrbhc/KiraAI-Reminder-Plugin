"""Bounded random creation without altering existing tasks or confirmation policy."""

import asyncio
import copy
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from _events import batch, message, observe
from _loader import load_plugin_module


time_utils = load_plugin_module("time_utils")
service_module = load_plugin_module("reminder_service")

INVALID_PARAMS = [
    {"random_count": value} for value in (0, -1, True, False, "2", 2.0, 101, 10**100)
] + [
    {"random_count_min": 1}, {"random_count_max": 2},
    {"random_count_min": 1, "random_count_max": 101},
    {"random_count_min": 101, "random_count_max": 1},
    {"random_count_min": 0, "random_count_max": 2},
    {"random_count_min": 1, "random_count_max": -1},
    {"random_count_min": True, "random_count_max": 2},
    {"random_count_min": 1, "random_count_max": "2"},
    {"random_count_min": 1.0, "random_count_max": 2},
]


@pytest.fixture
def plugin(reminder_main, tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)
    instance = reminder_main.ReminderPlugin(SimpleNamespace(), {
        "group_create_policy": "all", "admin_users": ["QQ:alice"],
    })
    scheduled = []
    instance._add_job = lambda sid, record: scheduled.append((sid, copy.deepcopy(record)))
    return instance, scheduled


def fail_random(*_args, **_kwargs):
    pytest.fail("Validation must reject before random sampling or time-list generation")


@pytest.mark.parametrize("params", INVALID_PARAMS)
def test_invalid_counts_reject_before_sampling(params, monkeypatch):
    monkeypatch.setattr(time_utils.random, "randint", fail_random)
    with pytest.raises(ValueError, match="本次未创建"):
        time_utils.determine_random_count(**params)


@pytest.mark.parametrize("params,expected", [
    ({}, 1), ({"random_count": 1}, 1), ({"random_count": 100}, 100),
    ({"random_count_min": 1, "random_count_max": 100}, 1),
    ({"random_count_min": 100, "random_count_max": 1}, 1),
    ({"random_count_min": 100, "random_count_max": 100}, 100),
    ({"random_count": 2, "random_count_min": 1, "random_count_max": 101}, 2),
])
def test_valid_counts_keep_defaults_range_swap_and_fixed_precedence(params, expected, monkeypatch):
    calls = []

    def sample(lower, upper):
        calls.append((lower, upper))
        return lower

    monkeypatch.setattr(time_utils.random, "randint", sample)
    time_utils.validate_random_count_params(**params)
    assert calls == []
    assert time_utils.determine_random_count(**params) == expected
    if "random_count_min" in params and "random_count" not in params:
        assert calls == [(min(params.values()), max(params.values()))]
    else:
        assert calls == []


@pytest.mark.parametrize("count", [None, 0, -1, True, "2", 2.0, 101, 10**100])
def test_generator_itself_rejects_invalid_counts_even_for_empty_windows(count, monkeypatch):
    monkeypatch.setattr(time_utils.random, "randint", fail_random)
    start = dt.datetime(2099, 1, 1)
    with pytest.raises(ValueError, match="本次未创建"):
        time_utils.generate_multiple_random_times(start, start, count)


def test_generator_keeps_short_window_capacity_and_valid_zero_window_contract():
    start = dt.datetime(2099, 1, 1)
    times = time_utils.generate_multiple_random_times(start, start + dt.timedelta(minutes=2), 100)
    assert len(times) == 2
    assert times == sorted(times)
    assert all(start <= value < start + dt.timedelta(minutes=2) for value in times)
    assert time_utils.generate_multiple_random_times(start, start, 2) == [start, start]


@pytest.mark.parametrize("params", INVALID_PARAMS)
@pytest.mark.parametrize("group", [None, "group-1"])
def test_tool_rejection_never_generates_times_writes_or_schedules(plugin, monkeypatch, params, group):
    instance, scheduled = plugin
    monkeypatch.setattr(service_module, "generate_multiple_random_times", fail_random)

    async def run():
        event = batch(message(group=group))
        await instance.set_reminder(event, "keep existing", "2099-01-01 10:00")
        scheduled.clear()
        before = instance._storage.path.read_bytes()
        result = await instance.set_reminder(
            event, "rejected", "2099-01-01 10:00",
            time_range_end="2099-01-01 10:01", **params,
        )
        assert result.startswith("❌") and "本次未创建" in result
        assert instance._storage.path.read_bytes() == before
        assert scheduled == []
        assert json.loads(await instance.list_pending_reminder_requests(event)) == []

    asyncio.run(run())


@pytest.mark.parametrize("count", [1, 100])
def test_valid_batch_creates_exact_records_and_jobs(plugin, count):
    instance, scheduled = plugin

    async def run():
        event = batch(message(group=None))
        result = await instance.set_reminder(
            event, "bounded", "2099-01-01 00:00", time_range_end="2099-01-02 00:00",
            random_count=count,
        )
        assert result.startswith(f"已添加随机待办: bounded (共{count}次)\n时间列表:\n")
        records = (await instance._storage.load())[event.sid]
        assert len(records) == len(scheduled) == count
        assert all(record["random_total"] == count for record in records)
        assert [record["random_index"] for record in records] == list(range(1, count + 1))

    asyncio.run(run())


def test_ordinary_creation_ignores_unrelated_random_parameters(plugin):
    instance, scheduled = plugin

    async def run():
        event = batch(message(group=None))
        result = await instance.set_reminder(event, "ordinary", "2099-01-01 10:00", random_count=10**100)
        assert result.startswith("已添加:")
        assert len((await instance._storage.load())[event.sid]) == len(scheduled) == 1

    asyncio.run(run())


@pytest.mark.parametrize("adapter", ["QQ", "Telegram"])
def test_mixed_all_policy_keeps_selected_owner_and_cannot_bypass_limit(plugin, monkeypatch, adapter):
    instance, scheduled = plugin

    async def run():
        event = batch(message(), message("bob"), adapter=adapter)
        ref = json.loads(await instance.list_message_sources(event))["sources"][1]["source_ref"]
        params = dict(time_range_end="2099-01-02 00:00", source_ref=ref)
        result = await instance.set_reminder(event, "selected owner", "2099-01-01 00:00",
                                             random_count=100, **params)
        assert "共100次" in result and "待确认请求" not in result
        records = (await instance._storage.load())[event.sid]
        assert len(records) == len(scheduled) == 100
        assert all(record["owner_id"] == "bob" and record["owner_adapter_name"] == adapter
                   for record in records)
        before = instance._storage.path.read_bytes()
        scheduled.clear()
        monkeypatch.setattr(service_module, "generate_multiple_random_times", fail_random)
        result = await instance.set_reminder(event, "rejected", "2099-01-01 00:00",
                                             random_count=101, **params)
        assert "本次未创建" in result
        assert instance._storage.path.read_bytes() == before and scheduled == []

    asyncio.run(run())


@pytest.mark.parametrize("params", [{"random_count": 101}, {"random_count_min": 1},
                                    {"random_count_min": 1, "random_count_max": 101}])
def test_confirmation_route_rejects_invalid_batches_without_a_pending_request(plugin, monkeypatch, params):
    instance, scheduled = plugin
    instance.config.group_create_policy = "admin_only"
    monkeypatch.setattr(time_utils.random, "randint", fail_random)

    async def run():
        event = batch(message(), message("bob"))
        ref = json.loads(await instance.list_message_sources(event))["sources"][0]["source_ref"]
        result = await instance.set_reminder(
            event, "rejected", "2099-01-01 10:00", time_range_end="2099-01-02 10:00",
            source_ref=ref, **params,
        )
        assert "本次未创建" in result and "待确认请求" not in result
        assert json.loads(await instance.list_pending_reminder_requests(event)) == []
        assert not instance._storage.path.exists()
        assert scheduled == []

    asyncio.run(run())


def test_valid_confirmation_preserves_range_and_draws_only_when_executed(plugin, monkeypatch):
    instance, scheduled = plugin
    instance.config.group_create_policy = "admin_only"
    original = time_utils.determine_random_count
    draws = []

    def determine(*args):
        draws.append(args)
        return original(*args)

    monkeypatch.setattr(service_module, "determine_random_count", determine)

    async def run():
        event = batch(message(), message("bob"))
        ref = json.loads(await instance.list_message_sources(event))["sources"][0]["source_ref"]
        result = await instance.set_reminder(
            event, "approved", "2099-01-01 00:00", time_range_end="2099-01-02 00:00",
            random_count_min=100, random_count_max=100, source_ref=ref,
        )
        assert "待确认请求" in result
        assert draws == [] and scheduled == [] and not instance._storage.path.exists()
        key = json.loads(await instance.list_pending_reminder_requests(event))[0]["request_id"]
        approved = await observe(instance, message(text="确认"))
        assert "共100次" in await instance.confirm_reminder_request(approved, key)
        assert draws == [(None, 100, 100)] and len(scheduled) == 100
        assert "确认未完成" in await instance.confirm_reminder_request(approved, key)
        assert len(scheduled) == 100

    asyncio.run(run())


def test_confirmed_execution_rechecks_a_previously_saved_invalid_request(plugin, monkeypatch):
    instance, scheduled = plugin
    monkeypatch.setattr(service_module, "generate_multiple_random_times", fail_random)

    async def run():
        event = batch(message(), message("bob"))
        ref = json.loads(await instance.list_message_sources(event))["sources"][0]["source_ref"]
        context, _ = instance._source_resolver().select(event, ref)
        item = await instance._confirmation_routes().pending.create(context, "set_reminder", {
            "content": "invalid saved request", "time": "2099-01-01 00:00",
            "time_range_end": "2099-01-02 00:00", "random_count": 101,
        }, None)
        approved = await observe(instance, message(text="确认"))
        assert "本次未创建" in await instance.confirm_reminder_request(approved, item.request_id)
        assert scheduled == [] and not instance._storage.path.exists()

    asyncio.run(run())


def test_existing_large_batch_restores_without_truncation(plugin):
    instance, _ = plugin
    restored = []
    instance._scheduler = SimpleNamespace(add_job=lambda _func, **options: restored.append(options["id"]))

    async def run():
        sid = "QQ:dm:alice"
        records = [{"job_id": f"legacy-{index}", "content": "legacy", "time": "2099-01-01 10:00",
                    "repeat": "none", "is_random": True, "random_batch_id": "legacy-batch",
                    "random_total": 101, "random_index": index + 1} for index in range(101)]
        await instance._storage.save({sid: records})
        await instance._restore_jobs()
        assert (await instance._storage.load())[sid] == records
        assert restored == [record["job_id"] for record in records]

    asyncio.run(run())
