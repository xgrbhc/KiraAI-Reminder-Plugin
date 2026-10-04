"""Keep generated random times unique at the persisted minute precision."""

import asyncio
import datetime as dt
import sys
from types import SimpleNamespace

import pytest

from test_reminder_service import make_plugin


@pytest.mark.parametrize("minutes", [1, 2, 3, 101, 1440])
@pytest.mark.parametrize("count", [1, 2, 7, 100])
@pytest.mark.parametrize("edge", ["first", "last"])
def test_minute_slots_are_unique_sorted_and_inside_the_window(
    reminder_main, monkeypatch, minutes, count, edge,
):
    module = sys.modules[reminder_main.generate_multiple_random_times.__module__]
    bounds = []

    def sample(lower, upper):
        bounds.append((lower, upper))
        return lower if edge == "first" else upper

    monkeypatch.setattr(module.random, "randint", sample)
    start = dt.datetime(2099, 1, 1, 23, 59)
    end = start + dt.timedelta(minutes=minutes)
    times = module.generate_multiple_random_times(start, end, count)
    actual = min(count, minutes)
    assert len(times) == len(set(times)) == actual
    assert times == sorted(times)
    assert all(start <= value < end and value.second == value.microsecond == 0 for value in times)
    assert bounds == [
        (index * minutes // actual, (index + 1) * minutes // actual - 1)
        for index in range(actual)
    ]


@pytest.mark.parametrize("start,end,expected", [
    ("10:00:01", "10:03:00", ["10:01", "10:02"]),
    ("10:00:00", "10:02:01", ["10:00", "10:01", "10:02"]),
    ("10:00:01", "10:00:59", []),
    ("10:00:00", "10:00:00", []),
    ("10:02:00", "10:00:00", []),
])
def test_partial_and_invalid_windows_never_emit_truncated_or_duplicate_times(
    reminder_main, start, end, expected,
):
    parse = lambda value: dt.datetime.strptime("2099-01-01 " + value, "%Y-%m-%d %H:%M:%S")
    times = reminder_main.generate_multiple_random_times(parse(start), parse(end), 100)
    assert [value.strftime("%H:%M") for value in times] == expected


def test_service_saves_and_registers_only_distinct_minute_times(reminder_main, tmp_path, monkeypatch):
    async def run():
        plugin, _, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        module = sys.modules[reminder_main.generate_multiple_random_times.__module__]
        monkeypatch.setattr(module.random, "randint", lambda lower, upper: upper)
        result = await plugin.set_reminder(
            SimpleNamespace(), "random", "2099-01-01 10:00",
            time_range_end="2099-01-01 10:03", random_count=2,
        )
        records = (await plugin._storage.load())["qq:dm:10001"]
        assert "共2次" in result
        assert len(records) == len(scheduled) == len({record["time"] for record in records}) == 2
        assert [record["time"] for record in records] == ["2099-01-01 10:00", "2099-01-01 10:02"]
        assert {job_id for _, job_id in scheduled} == {record["job_id"] for record in records}

    asyncio.run(run())


def test_service_filters_past_minutes_and_reports_actual_count(reminder_main, tmp_path, monkeypatch):
    async def run():
        plugin, _, scheduled = make_plugin(reminder_main, tmp_path / "reminders.json")
        service = sys.modules[plugin._reminder_service().__class__.__module__]
        monkeypatch.setattr(service, "get_local_now", lambda: dt.datetime(2099, 1, 1, 10, 0, 30))
        result = await plugin.set_reminder(
            SimpleNamespace(), "random", "2099-01-01 10:00",
            time_range_end="2099-01-01 10:03", random_count=100,
        )
        records = (await plugin._storage.load())["qq:dm:10001"]
        assert "共2次" in result
        assert len(records) == len(scheduled) == 2
        assert [record["time"] for record in records] == ["2099-01-01 10:01", "2099-01-01 10:02"]
        assert all(record["random_total"] == 2 for record in records)

    asyncio.run(run())


def test_generator_does_not_materialize_a_long_window(reminder_main, monkeypatch):
    module = sys.modules[reminder_main.generate_multiple_random_times.__module__]
    monkeypatch.setattr(module.random, "randint", lambda lower, upper: upper)
    times = module.generate_multiple_random_times(dt.datetime(2000, 1, 1), dt.datetime(9999, 1, 1), 100)
    assert len(times) == len(set(times)) == 100
