"""Deterministic interleavings for autonomous metadata and intent preservation."""

import asyncio
import datetime as dt
import sys
from contextlib import asynccontextmanager

import pytest

from test_autonomy import make_autonomy_plugin


SID = "qq:dm:10001"
KINDS = ("daily", "random", "due")


def due_intent(intent_id="intent-1"):
    return {
        "id": intent_id, "title": "original", "notes": "original notes",
        "priority": 0.5, "status": "active", "updated_at": "2000-01-01 09:00",
        "next_check_at": "2000-01-01 10:00", "next_check_job_id": f"old-{intent_id}",
        "followup_content": "original follow-up", "extension": {"keep": True},
    }


async def seed(plugin, intents=None):
    state = {"sessions": {}, "extension": {"keep": "root"}}
    session = plugin._ensure_autonomy_session(state, SID)
    session.update({
        "intents": [due_intent()] if intents is None else intents,
        "extension": {"keep": "session"},
        "last_random_check_at": "", "last_random_check_scheduled_at": "",
    })
    state["sessions"]["other:dm:user"] = {"intents": [], "extension": "other session"}
    await plugin._autonomy_storage.save(state)
    return state


def invoke(plugin, kind):
    if kind == "daily":
        return plugin._autonomous_daily_cycle_job()
    if kind == "random":
        plugin.config.random_check_enabled = True
        return plugin._autonomous_random_check_job(SID, scheduled_time="2000-01-01 10:00")
    return plugin._autonomous_followup_due_job()


@asynccontextmanager
async def paused_check(plugin, kind):
    """Let edits complete while publication is paused, then always release the task."""
    reached, release = asyncio.Event(), asyncio.Event()
    trigger = {"daily": "daily_reflection", "random": "random_check", "due": "followup_due"}[kind]
    published = []

    async def publish(*args, **kwargs):
        assert not plugin._autonomy_storage._lock.locked()
        published.append((args, kwargs))
        if args[1] == trigger:
            reached.set()
            await release.wait()

    plugin._publish_autonomous_notice = publish
    task = asyncio.create_task(invoke(plugin, kind))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        yield published
    finally:
        release.set()
        await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("kind", KINDS)
def test_check_preserves_new_intents_and_unrelated_state(reminder_main, tmp_path, kind):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        original = await seed(plugin)
        async with paused_check(plugin, kind):
            result = await asyncio.wait_for(
                plugin._autonomy_coordinator().create_intent(SID, "new", notes="new notes"), 5,
            )
            assert result.startswith("已创建")
            async with plugin._autonomy_storage.modify() as state:
                state["sessions"]["other:dm:user"]["intents"].append({"id": "other-new"})
            concurrent = await plugin._autonomy_storage.load()

        final = await plugin._autonomy_storage.load()
        assert len(final["sessions"][SID]["intents"]) == 2
        assert final["sessions"][SID]["intents"][1] == concurrent["sessions"][SID]["intents"][1]
        assert final["sessions"]["other:dm:user"] == concurrent["sessions"]["other:dm:user"]
        assert final["extension"] == original["extension"]
        assert final["sessions"][SID]["extension"] == original["sessions"][SID]["extension"]

    asyncio.run(run())


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", ("active", "waiting_confirmation", "paused", "closed"))
def test_check_preserves_concurrent_intent_edits(reminder_main, tmp_path, kind, status):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await seed(plugin)
        async with paused_check(plugin, kind):
            result = await asyncio.wait_for(plugin._autonomy_coordinator().update_intent(
                SID, "intent-1", title="edited", notes="edited notes", priority=0.9, status=status,
            ), 5)
            assert result.startswith("已更新")
            edited = (await plugin._autonomy_storage.load())["sessions"][SID]["intents"][0]
        final = (await plugin._autonomy_storage.load())["sessions"][SID]["intents"][0]
        for field in ("title", "notes", "priority", "status", "extension"):
            assert final[field] == edited[field]
        if kind == "due" and status in ("active", "waiting_confirmation"):
            assert final["last_followup_source"] == "fallback_due_job"
            assert final["next_check_at"] == final["next_check_job_id"] == ""
        else:
            assert final == edited

    asyncio.run(run())


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("change", ("delete_intent", "delete_session", "disable_session"))
def test_check_does_not_revive_removed_or_disabled_targets(reminder_main, tmp_path, kind, change):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await seed(plugin)
        async with paused_check(plugin, kind):
            async with plugin._autonomy_storage.modify() as state:
                if change == "delete_intent":
                    state["sessions"][SID]["intents"] = []
                elif change == "delete_session":
                    del state["sessions"][SID]
                else:
                    state["sessions"][SID]["enabled"] = False
            concurrent = await plugin._autonomy_storage.load()
        final = await plugin._autonomy_storage.load()
        if change == "delete_intent":
            assert final["sessions"][SID]["intents"] == []
        else:
            assert final == concurrent

    asyncio.run(run())


@pytest.mark.parametrize("kind", KINDS)
def test_failed_publish_does_not_write_completion_or_retry(reminder_main, tmp_path, kind):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        original = await seed(plugin)
        calls = []

        async def reject(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("isolated publish rejected")

        plugin._publish_autonomous_notice = reject
        await invoke(plugin, kind)
        assert len(calls) == 1
        assert await plugin._autonomy_storage.load() == original

    asyncio.run(run())


@pytest.mark.parametrize("kind", KINDS)
def test_failed_metadata_save_is_visible_without_republication(reminder_main, tmp_path, monkeypatch, kind):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        original = await seed(plugin)
        calls = []

        async def publish(*args, **kwargs):
            calls.append(args)

        def reject(_state):
            raise OSError("isolated metadata save rejected")

        plugin._publish_autonomous_notice = publish
        monkeypatch.setattr(plugin._autonomy_storage, "_unsafe_save", reject)
        with pytest.raises(OSError, match="metadata save rejected"):
            await invoke(plugin, kind)
        assert len(calls) == 1
        assert await plugin._autonomy_storage.load() == original

    asyncio.run(run())


def test_new_real_followup_survives_old_fallback_result(reminder_main, tmp_path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await seed(plugin)
        async with paused_check(plugin, "due"):
            principal = reminder_main.PrincipalContext(
                kind=reminder_main.PrincipalKind.BOT, principal_id="bot:qq:123",
                origin=reminder_main.EventOrigin.AUTONOMY_DAILY_REFLECTION,
                session_id=SID, bot_id="123",
            )
            result = await asyncio.wait_for(plugin._autonomy_coordinator().schedule_intent_followup(
                SID, principal, "intent-1", "2099-01-01 10:00", content="new follow-up",
            ), 5)
            assert result.startswith("已安排自主跟进")
            concurrent = (await plugin._autonomy_storage.load())["sessions"][SID]["intents"][0]
        final = (await plugin._autonomy_storage.load())["sessions"][SID]["intents"][0]
        assert final == concurrent
        assert final["next_check_at"] == "2099-01-01 10:00"
        assert final["next_check_job_id"]
        assert (await plugin._storage.load())[SID][0]["job_id"] == final["next_check_job_id"]
        assert plugin._scheduler.jobs[0][1]["id"] == final["next_check_job_id"]

    asyncio.run(run())


@pytest.mark.parametrize("field,value", (
    ("next_check_at", "2099-01-01 10:00"),
    ("next_check_job_id", "replacement-job-at-same-time"),
))
def test_fallback_requires_both_occurrence_fields_to_match(reminder_main, tmp_path, field, value):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await seed(plugin)
        async with paused_check(plugin, "due"):
            async with plugin._autonomy_storage.modify() as state:
                state["sessions"][SID]["intents"][0][field] = value
            concurrent = await plugin._autonomy_storage.load()
        assert await plugin._autonomy_storage.load() == concurrent

    asyncio.run(run())


def test_closing_intent_during_publication_keeps_closed_state(reminder_main, tmp_path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await seed(plugin)
        async with paused_check(plugin, "due"):
            result = await asyncio.wait_for(plugin._autonomy_coordinator().close_intent(SID, "intent-1"), 5)
            assert result.startswith("已关闭")
            concurrent = await plugin._autonomy_storage.load()
        assert await plugin._autonomy_storage.load() == concurrent
        intent = concurrent["sessions"][SID]["intents"][0]
        assert intent["status"] == "closed" and intent["closed_at"]
        assert intent["next_check_at"] == intent["next_check_job_id"] == ""

    asyncio.run(run())


@pytest.mark.parametrize("change", ("closed", "paused", "rescheduled", "deleted", "registered", "retitled"))
def test_due_candidates_are_rechecked_after_previous_publication(reminder_main, tmp_path, change):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await seed(plugin, [due_intent(), due_intent("intent-2")])
        async with paused_check(plugin, "due") as published:
            if change in ("closed", "paused", "retitled"):
                kwargs = {"title": "latest title", "notes": "latest notes"} if change == "retitled" else {"status": change}
                await plugin._autonomy_coordinator().update_intent(SID, "intent-2", **kwargs)
            elif change == "registered":
                async with plugin._storage.modify() as data:
                    data[SID] = [{"job_id": "old-intent-2", "source": "autonomous_intent_loop"}]
            else:
                async with plugin._autonomy_storage.modify() as state:
                    if change == "deleted":
                        state["sessions"][SID]["intents"].pop()
                    else:
                        target = state["sessions"][SID]["intents"][1]
                        target["next_check_at"] = "2099-01-01 10:00"
                        target["next_check_job_id"] = "replacement-job"
        ids = [kwargs["intent"]["id"] for _, kwargs in published]
        assert ids == (["intent-1", "intent-2"] if change == "retitled" else ["intent-1"])
        if change == "retitled":
            assert published[1][1]["intent"]["title"] == "latest title"
            assert published[1][1]["intent"]["notes"] == "latest notes"

    asyncio.run(run())


@pytest.mark.parametrize("change", ("disable", "delete"))
def test_daily_rechecks_later_session_before_publication(reminder_main, tmp_path, change):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        state = await seed(plugin)
        later = "other:dm:user"
        plugin._ensure_autonomy_session(state, later)
        await plugin._autonomy_storage.save(state)
        plugin.config.allowed_sessions.append(later)
        async with paused_check(plugin, "daily") as published:
            async with plugin._autonomy_storage.modify() as state:
                if change == "delete":
                    del state["sessions"][later]
                else:
                    state["sessions"][later]["enabled"] = False
        assert [args[0] for args, _ in published] == [SID]
        final = await plugin._autonomy_storage.load()
        if change == "delete":
            assert later not in final["sessions"]
        else:
            assert final["sessions"][later]["last_cycle_at"] == ""

    asyncio.run(run())


def test_daily_and_random_results_preserve_each_other(reminder_main, tmp_path):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        await seed(plugin, [])
        async with paused_check(plugin, "daily"):
            await asyncio.wait_for(invoke(plugin, "random"), 5)
            await plugin._autonomy_coordinator().create_intent(SID, "created between checks")
        session = (await plugin._autonomy_storage.load())["sessions"][SID]
        assert session["last_cycle_at"] and session["last_random_check_at"]
        assert session["last_random_check_scheduled_at"] == "2000-01-01 10:00"
        assert session["intents"][0]["title"] == "created between checks"

    asyncio.run(run())


@pytest.mark.parametrize("kind", ("daily", "random"))
def test_first_check_still_creates_missing_session(reminder_main, tmp_path, kind):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        async with paused_check(plugin, kind):
            assert not plugin._autonomy_storage.path.exists()
        session = (await plugin._autonomy_storage.load())["sessions"][SID]
        assert session["intents"] == [] and session["enabled"]
        assert session["last_cycle_at" if kind == "daily" else "last_random_check_at"]

    asyncio.run(run())


def test_random_plan_reads_latest_state_under_its_commit_lock(reminder_main, tmp_path, monkeypatch):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        original = await seed(plugin, [])
        plugin.config.random_check_enabled = True
        autonomy = sys.modules[reminder_main.AutonomyCoordinator.__module__]
        monkeypatch.setattr(autonomy, "get_local_now", lambda: dt.datetime(2099, 1, 1, 9, 0))
        monkeypatch.setattr(autonomy, "generate_random_check_times", lambda *_: ["2099-01-01 10:00"])
        load = plugin._autonomy_storage.load

        async def reject_separate_load():
            raise AssertionError("random plans must read through modify, not an earlier snapshot")

        monkeypatch.setattr(plugin._autonomy_storage, "load", reject_separate_load)
        reached = asyncio.Event()

        async def plan():
            reached.set()
            await plugin._schedule_autonomous_random_checks()

        async with plugin._autonomy_storage.modify() as state:
            task = asyncio.create_task(plan())
            await asyncio.wait_for(reached.wait(), 5)
            state["sessions"][SID]["intents"].append(due_intent("concurrent-intent"))
            state["extension"]["concurrent"] = True
        await asyncio.wait_for(task, 5)
        final = await load()
        assert final["sessions"][SID]["intents"] == [due_intent("concurrent-intent")]
        assert final["extension"] == {**original["extension"], "concurrent": True}
        assert final["sessions"][SID]["random_check_times"] == ["2099-01-01 10:00"]
        assert len(plugin._scheduler.jobs) == 1

    asyncio.run(run())


@pytest.mark.parametrize("force_new", (False, True))
def test_random_plan_keeps_reuse_and_registration_contract(reminder_main, tmp_path, monkeypatch, force_new):
    async def run():
        plugin = make_autonomy_plugin(reminder_main, tmp_path)
        original = await seed(plugin)
        plugin.config.random_check_enabled = True
        async with plugin._autonomy_storage.modify() as state:
            state["sessions"][SID].update({
                "random_check_plan_date": "2099-01-01", "random_check_window": "10-23",
                "random_check_times": ["2099-01-01 11:00"],
            })
        autonomy = sys.modules[reminder_main.AutonomyCoordinator.__module__]
        monkeypatch.setattr(autonomy, "get_local_now", lambda: dt.datetime(2099, 1, 1, 9, 0))
        generated = []

        def generate(*_):
            generated.append(True)
            return ["2099-01-01 12:00"]

        monkeypatch.setattr(autonomy, "generate_random_check_times", generate)
        await plugin._schedule_autonomous_random_checks(force_new=force_new)
        expected = "2099-01-01 12:00" if force_new else "2099-01-01 11:00"
        state = await plugin._autonomy_storage.load()
        assert state["sessions"][SID]["intents"] == original["sessions"][SID]["intents"]
        assert state["sessions"][SID]["random_check_times"] == [expected]
        assert len(generated) == int(force_new)
        _, options = plugin._scheduler.jobs[0]
        assert options["id"] == plugin._random_job_id(SID, expected)
        assert options["kwargs"] == {"sid": SID, "scheduled_time": expected}
        assert options["replace_existing"] and options["misfire_grace_time"] == 600

    asyncio.run(run())
