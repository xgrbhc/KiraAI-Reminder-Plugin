from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "reminder_plugin_v22_main_tests"


def _load_main_module():
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PACKAGE_NAME] = package
    name = f"{PACKAGE_NAME}.main"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


main = _load_main_module()


class StorageMigrationTests(unittest.TestCase):
    def test_identity_migration_creates_one_immutable_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "reminders.json"
            original = {
                "qq:gm:20001": [
                    {
                        "creator_id": "unknown",
                        "creator_name": "未知",
                        "content": "legacy group reminder",
                    }
                ],
                "qq:dm:10001": [
                    {
                        "creator_id": "unknown",
                        "creator_name": "未知",
                        "content": "legacy direct reminder",
                    }
                ],
            }
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

            plugin = main.ReminderPlugin.__new__(main.ReminderPlugin)
            plugin._storage = main.ReminderStorage(path)
            asyncio.run(plugin._migrate_identity_schema_v2())

            backup = path.with_name("reminders.pre-v2.2.backup.json")
            assert backup.exists()
            assert json.loads(backup.read_text(encoding="utf-8")) == original
            migrated = json.loads(path.read_text(encoding="utf-8"))
            assert migrated["qq:gm:20001"][0]["owner_type"] == "legacy"
            assert migrated["qq:dm:10001"][0]["owner_id"] == "10001"

            backup_bytes = backup.read_bytes()
            asyncio.run(plugin._migrate_identity_schema_v2())
            assert backup.read_bytes() == backup_bytes

    def test_internal_event_tool_filter_is_capability_aware(self):
        class DummyToolSet:
            def __init__(self, names):
                self.tools = [SimpleNamespace(name=name) for name in names]

            def remove(self, *names):
                blocked = set(names)
                self.tools = [tool for tool in self.tools if tool.name not in blocked]

        plugin = main.ReminderPlugin.__new__(main.ReminderPlugin)
        plugin._identity = main.IdentityResolver("test-secret")

        def event_for(kind, origin, capabilities):
            envelope = plugin._identity.build_envelope(
                origin=origin,
                principal_kind=kind,
                principal_id="system:test" if kind is main.PrincipalKind.SYSTEM else "bot:qq:90001",
                capabilities=capabilities,
            )
            message = SimpleNamespace(
                sender=SimpleNamespace(user_id="90001", nickname="Kira"),
                self_id="90001",
                extra=envelope,
                is_mentioned=True,
            )
            return SimpleNamespace(sid="qq:gm:20001", messages=[message])

        plugin.config = SimpleNamespace(
            autonomy_allowed_tools=["set_reminder", "exec"],
            autonomy_mode="plan_only",
        )
        request = SimpleNamespace(tool_set=DummyToolSet(["set_reminder", "exec"]))
        plugin._filter_internal_event_tools(
            event_for(
                main.PrincipalKind.SYSTEM,
                main.EventOrigin.REMINDER_FIRE,
                set(),
            ),
            request,
        )
        assert request.tool_set.tools == []

        request = SimpleNamespace(tool_set=DummyToolSet(["set_reminder", "exec"]))
        plugin._filter_internal_event_tools(
            event_for(
                main.PrincipalKind.BOT,
                main.EventOrigin.AUTONOMY_DAILY_REFLECTION,
                {"intent.manage"},
            ),
            request,
        )
        assert [tool.name for tool in request.tool_set.tools] == ["set_reminder"]

        plugin.config.autonomy_mode = "trusted_admin"
        request = SimpleNamespace(tool_set=DummyToolSet(["set_reminder", "exec"]))
        plugin._filter_internal_event_tools(
            event_for(
                main.PrincipalKind.BOT,
                main.EventOrigin.AUTONOMY_DAILY_REFLECTION,
                {"intent.manage"},
            ),
            request,
        )
        assert [tool.name for tool in request.tool_set.tools] == ["set_reminder", "exec"]

    def test_bot_principal_keeps_target_direct_message_session(self):
        class DummyBus:
            def __init__(self):
                self.events = []

            async def publish(self, event):
                self.events.append(event)

        adapter = SimpleNamespace(
            info=SimpleNamespace(name="QQ"),
            config={"bot_pid": "90001"},
            message_types=[],
        )
        bus = DummyBus()
        plugin = main.ReminderPlugin.__new__(main.ReminderPlugin)
        plugin._identity = main.IdentityResolver("test-secret")
        plugin.ctx = SimpleNamespace(
            adapter_mgr=SimpleNamespace(get_adapter=lambda name: adapter if name == "QQ" else None),
            event_bus=bus,
        )
        asyncio.run(
            plugin._publish_immediate_notice(
                "QQ:dm:10001",
                main.MessageChain([main.Text("check")]),
                origin=main.EventOrigin.AUTONOMY_DAILY_REFLECTION,
                principal_kind=main.PrincipalKind.BOT,
                capabilities={"intent.manage"},
            )
        )
        event = bus.events[0]
        assert event.session.sid == "QQ:dm:10001"
        assert event.message.sender.user_id == "90001"
        resolved = plugin._identity.resolve(event)
        assert resolved.kind is main.PrincipalKind.BOT
        assert resolved.trusted
