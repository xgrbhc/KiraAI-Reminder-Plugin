"""Issue-only decisions preserve original plans and permit genuine future cycles."""

import asyncio
import copy
import datetime as dt
import json

import pytest

from _loader import load_plugin_module
from core.plugin.plugin_registry import _plugin_components
from test_delivery import SID, _event, _plugin, _record

delivery = load_plugin_module("delivery")
scheduler = load_plugin_module("scheduler")


@pytest.mark.parametrize("status", ["failed", "unconfirmed", "legacy_unconfirmed"])
@pytest.mark.parametrize("repeat", ["none", "daily"])
@pytest.mark.parametrize("action", ["", "perform an action"])
@pytest.mark.parametrize("important", [False, True])
def test_ignore_matrix_preserves_original_bytes_and_metadata(reminder_main, tmp_path, status, repeat, action, important):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = dict(_record(repeat=repeat, action=action), important=important, paused=True)
        await plugin._storage.save({SID: [record]})
        key = await plugin._delivery.begin(SID, record)
        async with plugin._delivery_storage.modify() as state:
            state[SID][0]["status"] = status
            if status == "legacy_unconfirmed":
                state[SID][0]["attempt_count"] = 0
        original = plugin._storage.path.read_bytes()
        assert "已记录" in await plugin.review_delivery_issue(_event(plugin, reminder_main), key)
        assert await plugin._delivery.list_issues(SID, plugin._get_principal(_event(plugin, reminder_main)), [], [SID]) == []
        assert plugin._storage.path.read_bytes() == original
        await plugin._delivery.reconcile(startup=True)
        assert plugin._storage.path.read_bytes() == original
        assert (await plugin._delivery_storage.load())[SID][0]["resolution"] == "dismiss"
    asyncio.run(run())


@pytest.mark.parametrize("status", ["failed", "unconfirmed"])
def test_historical_repeat_issue_does_not_block_next_cycle_or_accept_late_ack(reminder_main, tmp_path, monkeypatch, status):
    async def run():
        now = [dt.datetime(2030, 6, 2, 9, 30)]
        monkeypatch.setattr(delivery, "_now", lambda: now[0])
        plugin = _plugin(reminder_main, tmp_path)
        record = _record(repeat="daily")
        await plugin._storage.save({SID: [record]})
        first = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, first, status)
        assert await plugin._delivery.begin(SID, record) is None
        now[0] += dt.timedelta(days=1)
        second = await plugin._delivery.begin(SID, record)
        assert second and second != first
        assert await plugin._delivery.mark(SID, first, "llm_received") is None
        assert (await plugin._delivery_storage.load())[SID][-1]["status"] == "awaiting_llm"
        await plugin._delivery.mark(SID, second, "llm_received")
        assert (await plugin._storage.load())[SID] == [record]
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == status
    asyncio.run(run())


def test_live_attempt_is_guarded_and_expired_attempt_allows_new_cycle(reminder_main, tmp_path, monkeypatch):
    async def run():
        now = [dt.datetime(2030, 6, 2, 9, 30)]
        monkeypatch.setattr(delivery, "_now", lambda: now[0])
        plugin = _plugin(reminder_main, tmp_path)
        record = _record(repeat="interval")
        first = await plugin._delivery.begin(SID, record)
        now[0] += dt.timedelta(minutes=1)
        assert await plugin._delivery.begin(SID, record) is None
        assert (await plugin._delivery_storage.load())[SID][0]["missed_count"] == 1
        now[0] += dt.timedelta(minutes=4)
        second = await plugin._delivery.begin(SID, record)
        assert second and second != first
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "unconfirmed"
    asyncio.run(run())


def test_repeat_failures_merge_bounded_history_but_each_cycle_has_own_receipt(reminder_main, tmp_path, monkeypatch):
    async def run():
        now = [dt.datetime(2030, 6, 2, 9, 30)]
        monkeypatch.setattr(delivery, "_now", lambda: now[0])
        plugin = _plugin(reminder_main, tmp_path)
        record = _record(repeat="daily")
        await plugin._storage.save({SID: [record]})
        original = plugin._storage.path.read_bytes()
        ids = []
        for index in range(110):
            key = await plugin._delivery.begin(SID, record)
            assert key and key not in ids
            ids.append(key)
            await plugin._delivery.mark(SID, key, "unconfirmed" if index == 1 else "failed")
            now[0] += dt.timedelta(days=1)
        rows = (await plugin._delivery_storage.load())[SID]
        issues = [row for row in rows if row["status"] in delivery.ISSUE_STATES]
        assert len(rows) == 101 and len(issues) == 1
        issue = issues[0]
        assert issue["delivery_id"] == ids[0] and issue["status"] == "unconfirmed"
        assert issue["missed_count"] == 109
        times = delivery.delivery_time_fields(issue)
        assert times["triggered_at"] == "2030-06-02 09:30"
        assert times["latest_cycle_at"] == (now[0] - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        assert plugin._storage.path.read_bytes() == original
        await plugin._delivery.reconcile(startup=True)
        assert plugin._storage.path.read_bytes() == original
    asyncio.run(run())


def test_ignored_overdue_once_tombstone_survives_pruning_and_restart(reminder_main, tmp_path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = dict(_record(), time="2000-01-01 10:00")
        await plugin._storage.save({SID: [record]})
        await plugin._delivery.reconcile(startup=True)
        first = (await plugin._delivery_storage.load())[SID][0]
        assert first["status"] == "legacy_unconfirmed"
        await plugin.review_delivery_issue(_event(plugin, reminder_main), first["delivery_id"])
        async with plugin._delivery_storage.modify() as state:
            state[SID].extend(dict(first, delivery_id=f"closed-{i}", status="resolved", resolution="reminder_deleted") for i in range(110))
        await plugin._delivery._prune_closed(SID)
        await plugin._delivery.reconcile(startup=True)
        rows = (await plugin._delivery_storage.load())[SID]
        assert len(rows) == 101
        assert any(row["delivery_id"] == first["delivery_id"] and row["resolution"] == "dismiss" for row in rows)
        assert not any(row["status"] in delivery.ISSUE_STATES for row in rows)
        assert await plugin._delivery.begin(SID, record) is None
        assert (await plugin._storage.load())[SID] == [record]
    asyncio.run(run())


def test_awaiting_and_foreign_owner_cannot_ignore_and_do_not_write_original(reminder_main, tmp_path):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        record = _record()
        await plugin._storage.save({SID: [record]})
        original = plugin._storage.path.read_bytes()
        key = await plugin._delivery.begin(SID, record)
        assert "仍在等待" in await plugin.review_delivery_issue(_event(plugin, reminder_main), key)
        await plugin._delivery.mark(SID, key, "unconfirmed")
        assert "权限拒绝" in await plugin.review_delivery_issue(_event(plugin, reminder_main, user_id="other"), key)
        assert plugin._storage.path.read_bytes() == original
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "unconfirmed"
    asyncio.run(run())


@pytest.mark.parametrize("mode,enabled", [("observe", True), ("plan_only", False)])
def test_public_autonomous_ignore_respects_readonly_or_disabled_mode(reminder_main, tmp_path, mode, enabled):
    async def run():
        plugin = _plugin(reminder_main, tmp_path)
        plugin.config.autonomy_mode = mode
        plugin._autonomy_enabled = lambda: enabled
        plugin._is_autonomy_allowed_for_sid = lambda _sid: True
        record = _record()
        key = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, key, "failed")
        bot = reminder_main.PrincipalContext(kind=reminder_main.PrincipalKind.BOT,
            principal_id="bot:qq:bot-1", origin=reminder_main.EventOrigin.AUTONOMY_DAILY_REFLECTION,
            session_id=SID, trusted=True, capabilities=frozenset({"intent.manage"}))
        plugin._get_principal = lambda _event: bot
        result = await plugin.review_delivery_issue(_event(plugin, reminder_main), key)
        assert ("只允许读取" if enabled else "未启用") in result
        assert (await plugin._delivery_storage.load())[SID][0]["status"] == "failed"
    asyncio.run(run())


def test_query_and_summary_use_actual_trigger_but_legacy_is_unknown(reminder_main, tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(delivery, "_now", lambda: dt.datetime(2030, 6, 2, 9, 30))
        plugin = _plugin(reminder_main, tmp_path)
        record = _record(repeat="daily")
        await plugin._storage.save({SID: [record]})
        key = await plugin._delivery.begin(SID, record)
        await plugin._delivery.mark(SID, key, "failed")
        text = await plugin.list_delivery_issues(_event(plugin, reminder_main))
        assert "触发时间=2030-06-02 09:30 开始/原定时间=2099-01-01 10:00" in text
        rows = (await plugin._delivery_storage.load())[SID]
        summary = json.loads(plugin._delivery_recovery_text(rows).splitlines()[3])
        assert summary[0]["triggered_at"] == "2030-06-02 09:30" and "time" not in summary[0]
        legacy = dict(rows[0], status="legacy_unconfirmed", attempt_count=0)
        assert delivery.delivery_time_fields(legacy)["triggered_at"] is None
        for invalid in ("bad", "2030-02-30T09:30:00", "2030-06-02T09:30:60", "2030-06-02T09:30:00+00:60"):
            assert delivery.delivery_time_fields(dict(rows[0], created_at=invalid))["triggered_at"] is None
    asyncio.run(run())


def test_registered_tool_has_only_optional_dismiss_decision(reminder_main):
    params = _plugin_components["reminder_plugin"].tools["review_delivery_issue"]["parameters"]
    assert params["required"] == ["delivery_id"]
    assert params["properties"]["decision"]["enum"] == ["dismiss"]
    assert params["properties"]["decision"]["default"] == "dismiss"


def test_overdue_once_is_readonly_display_metadata(monkeypatch):
    monkeypatch.setattr(scheduler, "get_local_now", lambda: dt.datetime(2030, 6, 2, 9, 30))
    old = dict(_record(), time="2030-06-01 09:30")
    original = copy.deepcopy(old)
    assert scheduler.reminder_schedule_info(None, old) == {"is_overdue_once": True}
    assert old == original
    assert scheduler.reminder_schedule_info(None, _record()) == {}
    assert scheduler.reminder_schedule_info(None, dict(old, time="bad")) == {}
