"""Adapter namespaces, scoped ACL compatibility, and receipt-safe migrations."""

import asyncio
import copy
import datetime as dt
import json
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from _events import batch, message, observe

from _loader import load_plugin_module
from _loader import PLUGIN_DIR


identity = load_plugin_module("identity")
permissions = load_plugin_module("permissions")
config_module = load_plugin_module("config")
migration = load_plugin_module("migration")


def user(adapter="QQ", user_id="123", **kwargs):
    return identity.PrincipalContext(
        identity.PrincipalKind.USER, user_id, session_id=f"{adapter}:gm:456", **kwargs,
    )


def test_same_id_is_not_same_actor_across_adapters():
    resolver = identity.IdentityResolver("test-secret")
    actors = []
    for adapter in ("QQ", "Telegram"):
        event = SimpleNamespace(
            session=SimpleNamespace(sid=f"{adapter}:gm:456", adapter_name=adapter),
            messages=[SimpleNamespace(sender=SimpleNamespace(user_id="123", nickname="Alice"))],
        )
        actors.append(resolver.resolve(event))
    assert actors[0].principal_id == actors[1].principal_id == "123"
    assert actors[0].actor_key != actors[1].actor_key
    assert actors[0].scoped_user_id == "QQ:123"
    assert actors[1].scoped_user_id == "Telegram:123"


@pytest.mark.parametrize("check", [permissions.is_admin, permissions.is_authorized])
def test_acl_requires_exact_adapter_and_never_accepts_bare_ids(check):
    assert check(user(), ["QQ:123"])
    assert not check(user("Telegram"), ["QQ:123"])
    assert not check(user("qq"), ["QQ:123"])
    assert not check(user(), ["123"])
    assert not check(user(), ["QQ:999"])
    assert not check(user("unknown"), ["unknown:123"])


@pytest.mark.parametrize("policy,mentioned,expected", [
    ("admin_only", False, False), ("mentioned_user", False, False),
    ("mentioned_user", True, True), ("all", False, True),
])
def test_group_policies_survive_scoping(policy, mentioned, expected):
    assert permissions.can_create_reminder(
        user("Telegram"), is_group=True, is_mentioned=mentioned,
        group_policy=policy, admin_users=["QQ:123"], authorized_users=["QQ:123"],
        autonomy_mode="plan_only",
    ) is expected


def test_authorized_scope_and_admin_action_scope():
    for adapter, expected in (("QQ", True), ("Telegram", False)):
        assert permissions.can_create_reminder(
            user(adapter), is_group=True, is_mentioned=False, group_policy="admin_only",
            admin_users=[], authorized_users=["QQ:123"], autonomy_mode="plan_only",
        ) is expected
        assert permissions.can_set_action(
            user(adapter), action_policy="admin_only", admin_users=["QQ:123"],
            autonomy_mode="plan_only",
        ) is expected


@pytest.mark.parametrize("operation", list(permissions.ReminderOperation))
def test_owners_and_admins_cannot_manage_a_different_adapter(operation):
    target = {"owner_type": "user", "owner_id": "123", "owner_adapter_name": "QQ"}
    assert permissions.can_manage_reminder(
        user(), target, operation=operation, sid="QQ:gm:456", admin_users=[],
    )
    assert not permissions.can_manage_reminder(
        user("Telegram"), target, operation=operation, sid="QQ:gm:456",
        admin_users=["Telegram:123"],
    )
    assert not permissions.can_manage_reminder(
        user(), target, operation=operation, sid="Telegram:gm:456", admin_users=["QQ:123"],
    )
    other = {**target, "owner_id": "999"}
    assert permissions.can_manage_reminder(
        user(), other, operation=operation, sid="QQ:gm:456", admin_users=["QQ:123"],
    )


def test_unknown_scope_is_denied_but_authenticated_web_retains_management():
    missing = identity.PrincipalContext(identity.PrincipalKind.USER, "123")
    assert not permissions.can_create_reminder(
        missing, is_group=False, is_mentioned=False, group_policy="all",
        admin_users=[], authorized_users=[], autonomy_mode="plan_only",
    )
    assert not permissions.can_set_action(
        missing, action_policy="all", admin_users=[], autonomy_mode="plan_only",
    )
    web = identity.PrincipalContext(identity.PrincipalKind.WEB, "web:admin", trusted=True)
    assert permissions.can_manage_reminder(
        web, {"owner_type": "legacy"}, operation=permissions.ReminderOperation.DELETE,
        sid="Telegram:gm:456", admin_users=[],
    )
    bot = identity.PrincipalContext(
        identity.PrincipalKind.BOT, "bot:QQ:321", session_id="QQ:dm:123", trusted=True,
        capabilities=frozenset({"reminder.manage_all"}),
    )
    assert not permissions.can_manage_reminder(
        bot, {"owner_type": "user", "owner_id": "123"},
        operation=permissions.ReminderOperation.DELETE, sid="Telegram:dm:123", admin_users=[],
    )


def test_acl_legacy_resolution_is_opt_in_and_explicit_entries_stay_separate():
    entries = ["123", "Telegram:123", " :999", "QQ:", "unknown:888"]
    assert config_module.scoped_acl_entries(entries) == {"Telegram:123"}
    assert config_module.scoped_acl_entries(entries, "QQ") == {"QQ:123", "Telegram:123"}
    with pytest.raises(ValueError):
        config_module.ReminderConfig(legacy_acl_adapter="QQ:dm:123")


def test_inconsistent_event_adapter_and_session_cannot_grant_admin():
    resolver = identity.IdentityResolver("test-secret")
    actor = resolver.resolve(SimpleNamespace(
        adapter=SimpleNamespace(name="QQ"), sid="Telegram:gm:456",
        messages=[SimpleNamespace(sender=SimpleNamespace(user_id="123", nickname="Alice"))],
    ))
    assert actor.adapter_scope == ""
    assert not permissions.is_admin(actor, ["QQ:123", "Telegram:123"])


def test_plugin_config_schema_has_scoped_fields_and_english_locales():
    from core.config.config_field import build_fields

    schema = json.loads((PLUGIN_DIR / "schema.json").read_text(encoding="utf-8"))
    fields = {field.key: field for field in build_fields(schema)}
    assert fields["legacy_acl_adapter"].default == ""
    for key in ("admin_users", "authorized_users", "legacy_acl_adapter"):
        assert fields[key].name
        assert fields[key].hint
        assert fields[key].locales["en"]["name"]
        assert fields[key].locales["en"]["hint"]


def acl_plugin(reminder_main, tmp_path, monkeypatch, names, **cfg):
    monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)
    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin._config_source = dict(cfg)
    plugin.plugin_cfg = plugin._config_source
    plugin.config = reminder_main.ReminderConfig(**cfg)
    plugin.ctx = SimpleNamespace(adapter_mgr=SimpleNamespace(
        get_adapters_info=lambda: [SimpleNamespace(name=name, enabled=False) for name in names],
    ))
    return plugin


def test_sole_configured_adapter_is_backed_up_and_pinned(reminder_main, tmp_path, monkeypatch):
    plugin = acl_plugin(reminder_main, tmp_path, monkeypatch, ["QQ"], admin_users=["123"])
    path = tmp_path / "config/plugins/reminder_plugin.json"
    path.parent.mkdir(parents=True)
    original = {"admin_users": ["123"], "unknown_setting": {"keep": True}}
    path.write_text(json.dumps(original), encoding="utf-8")
    asyncio.run(plugin._initialize_adapter_acl())
    assert plugin._admin_acl() == {"QQ:123"}
    assert plugin._config_source["legacy_acl_adapter"] == "QQ"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["unknown_setting"] == original["unknown_setting"]
    assert saved["legacy_acl_adapter"] == "QQ"
    backup = path.with_name("reminder_plugin.pre-adapter-acl.backup.json")
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    before = backup.read_bytes()
    next_plugin = acl_plugin(reminder_main, tmp_path, monkeypatch, ["Telegram"], **saved)
    asyncio.run(next_plugin._initialize_adapter_acl())
    assert next_plugin._admin_acl() == {"QQ:123"}
    assert backup.read_bytes() == before


@pytest.mark.parametrize("names", [[], ["QQ", "Telegram"], ["QQ", "QQ"], ["unknown"]])
def test_ambiguous_legacy_acl_does_not_write_or_expand(names, reminder_main, tmp_path, monkeypatch):
    plugin = acl_plugin(
        reminder_main, tmp_path, monkeypatch, names,
        admin_users=["123", "QQ:789"], authorized_users=["456"],
    )
    asyncio.run(plugin._initialize_adapter_acl())
    assert plugin._admin_acl() == {"QQ:789"}
    assert plugin._authorized_acl() == set()
    assert plugin.config.admin_users == ["123", "QQ:789"]
    assert not (tmp_path / "config/plugins/reminder_plugin.json").exists()


def test_failed_config_pin_grants_no_inferred_privileges(reminder_main, tmp_path, monkeypatch):
    plugin = acl_plugin(reminder_main, tmp_path, monkeypatch, ["QQ"], admin_users=["123"])
    path = tmp_path / "config/plugins/reminder_plugin.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{broken")
    with pytest.raises(RuntimeError):
        asyncio.run(plugin._initialize_adapter_acl())
    assert path.read_bytes() == b"{broken"
    assert plugin._admin_acl() == set()
    assert plugin.config.legacy_acl_adapter == ""


def test_v2_migration_preserves_all_ownership_metadata():
    original = {
        "identity_schema": 2, "owner_type": "bot", "owner_id": "bot:QQ:321",
        "owner_name": "Kira", "creator_id": "bot:QQ:321", "created_by_type": "user",
        "created_by_id": "123", "origin": "autonomy_followup_due", "important": True,
    }
    record = dict(original)
    assert identity.migrate_reminder_identity("QQ:dm:123", record)
    assert record == {
        **original, "identity_schema": 3, "owner_adapter_name": "QQ",
        "created_by_adapter_name": "QQ",
    }
    assert not identity.migrate_reminder_identity("QQ:dm:123", record)
    future = {**original, "identity_schema": 99}
    unchanged = dict(future)
    assert not identity.migrate_reminder_identity("QQ:dm:123", future)
    assert future == unchanged


def stores(reminder_main, tmp_path):
    return (
        reminder_main.ReminderStorage(tmp_path / "reminders.json"),
        reminder_main.ReminderStorage(tmp_path / "delivery_state.json"),
    )


@pytest.mark.parametrize("status", ["awaiting_llm", "failed"])
def test_migrated_receipt_still_matches_for_confirmation_or_ignore(status, reminder_main, tmp_path):
    async def run():
        reminders, ledger = stores(reminder_main, tmp_path)
        sid = "QQ:dm:123"
        record = {
            "identity_schema": 2, "owner_type": "user", "owner_id": "123",
            "job_id": "job1", "content": "check", "repeat": "none", "time": "2030-01-01 10:00",
        }
        await reminders.save({sid: [record]})
        await ledger.save({sid: [{"delivery_id": "d1", "job_id": "job1", "status": status,
                                 "reminder": dict(record), "attempt_count": 1}]})
        assert await migration.migrate_identity_stores(reminders, ledger) == 2
        current = (await reminders.load())[sid][0]
        receipt = (await ledger.load())[sid][0]
        assert current == receipt["reminder"]
        assert current["owner_adapter_name"] == "QQ"
        tracker = reminder_main.DeliveryTracker(ledger, reminders)
        if status == "awaiting_llm":
            async with ledger.modify() as state:
                state[sid][0]["created_at"] = dt.datetime.now().isoformat()
            assert await tracker.begin(sid, current) is None
            await tracker.mark(sid, "d1", "llm_received")
            assert (await reminders.load())[sid] == []
        else:
            result, entry = await tracker.resolve(sid, "d1", user(), [], [], "dismiss")
            assert result == "已记录处理决定"
            assert entry["resolution"] == "dismiss"
            assert (await reminders.load())[sid] == [current]
        assert await migration.migrate_identity_stores(reminders, ledger) == 0

    asyncio.run(run())


def test_corrupt_ledger_stops_before_any_reminder_write(reminder_main, tmp_path):
    async def run():
        reminders, ledger = stores(reminder_main, tmp_path)
        await reminders.save({"QQ:dm:123": [{"creator_id": "123"}]})
        original = reminders.path.read_bytes()
        ledger.path.write_bytes(b"{broken")
        with pytest.raises(RuntimeError):
            await migration.migrate_identity_stores(reminders, ledger)
        assert reminders.path.read_bytes() == original
        assert ledger.path.read_bytes() == b"{broken"
        assert not list(tmp_path.glob("*.backup.json"))

    asyncio.run(run())


def test_partial_migration_retries_without_overwriting_backups(reminder_main, tmp_path, monkeypatch):
    async def run():
        reminders, ledger = stores(reminder_main, tmp_path)
        sid = "QQ:gm:456"
        original = {"creator_id": "123", "job_id": "job1"}
        await reminders.save({sid: [original]})
        await ledger.save({sid: [{"reminder": copy.deepcopy(original)}]})
        original_reminders = reminders.path.read_bytes()
        original_ledger = ledger.path.read_bytes()
        save = ledger.save

        async def fail_save(_data):
            raise OSError("simulated write failure")

        monkeypatch.setattr(ledger, "save", fail_save)
        with pytest.raises(OSError):
            await migration.migrate_identity_stores(reminders, ledger)
        assert (await reminders.load())[sid][0]["identity_schema"] == 3
        assert ledger.path.read_bytes() == original_ledger
        monkeypatch.setattr(ledger, "save", save)
        assert await migration.migrate_identity_stores(reminders, ledger) == 1
        assert (await reminders.load())[sid][0] == (await ledger.load())[sid][0]["reminder"]
        assert reminders.path.with_name("reminders.pre-identity-v3.backup.json").read_bytes() == original_reminders
        assert ledger.path.with_name("delivery_state.pre-identity-v3.backup.json").read_bytes() == original_ledger
        assert await migration.migrate_identity_stores(reminders, ledger) == 0

    asyncio.run(run())


def tool_event(adapter, user_id="123"):
    return SimpleNamespace(
        sid=f"{adapter}:gm:456", adapter=SimpleNamespace(name=adapter),
        messages=[SimpleNamespace(
            sender=SimpleNamespace(user_id=user_id, nickname="Alice"),
            is_mentioned=True, extra={},
        )],
    )


def test_tools_store_scoped_ownership_and_check_same_adapter_admin(reminder_main, tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)

    async def run():
        plugin = reminder_main.ReminderPlugin(SimpleNamespace(), {
            "admin_users": ["QQ:123"], "group_create_policy": "all",
        })
        plugin._scheduler = AsyncIOScheduler()
        events = [tool_event(adapter) for adapter in ("QQ", "Telegram")]
        for event in events:
            result = await plugin.set_reminder(event, content="private", time="2030-01-01 10:00")
            assert result.startswith("已添加:")
        data = await plugin._storage.load()
        for event in events:
            record = data[event.sid][0]
            assert record["owner_id"] == "123"
            assert record["identity_schema"] == 3
            assert record["owner_adapter_name"] == event.adapter.name
            assert record["created_by_adapter_name"] == event.adapter.name
            record.update({"owner_id": "999", "creator_id": "999"})
        await plugin._storage.save(data)
        assert "private" in await plugin.list_reminders(events[0])
        assert "private" not in await plugin.list_reminders(events[1])
        telegram_job = data[events[1].sid][0]["job_id"]
        assert "权限拒绝" in await plugin.delete_reminder(events[1], job_id=telegram_job)
        qq_job = data[events[0].sid][0]["job_id"]
        assert "已删除" in await plugin.delete_reminder(events[0], job_id=qq_job)
        web = await plugin.api_action_reminders(
            "delete", {"session_id": events[1].sid, "job_id": telegram_job},
        )
        assert web["status"] == "ok"

    asyncio.run(run())


def test_important_delete_token_cannot_cross_adapter_even_for_scoped_admin(reminder_main, tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)

    async def run():
        plugin = reminder_main.ReminderPlugin(SimpleNamespace(), {
            "admin_users": ["QQ:123", "Telegram:123"], "group_create_policy": "all",
        })
        plugin._scheduler = AsyncIOScheduler()
        qq = batch(message("123", group="456"))
        telegram = batch(message("123", group="456"), adapter="Telegram")
        await plugin.set_reminder(qq, content="important", time="2030-01-01 10:00")
        record = (await plugin._storage.load())[qq.sid][0]
        await plugin.mark_reminder_important(qq, record["job_id"])
        result = await plugin.delete_reminder(qq, job_id=record["job_id"])
        token = json.loads(await plugin.list_pending_reminder_requests(qq))[0]["request_id"]
        wrong = await observe(plugin, message("123", "确认删除", group="456"), "Telegram")
        assert "确认未完成" in await plugin.confirm_delete_reminder(wrong, token)
        assert (await plugin._storage.load())[qq.sid]
        assert json.loads(await plugin.list_pending_reminder_requests(qq))
        confirmed = await observe(plugin, message("123", "确认删除", group="456"))
        assert "已删除" in await plugin.confirm_delete_reminder(confirmed, token)
        assert not json.loads(await plugin.list_pending_reminder_requests(qq))

    asyncio.run(run())
