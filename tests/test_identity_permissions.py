from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "reminder_plugin_v22_tests"


def _load_module(name: str):
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[PACKAGE_NAME] = package
    qualified_name = f"{PACKAGE_NAME}.{name}"
    module = sys.modules.get(qualified_name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(qualified_name, PLUGIN_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module


identity = _load_module("identity")
permissions = _load_module("permissions")

EventOrigin = identity.EventOrigin
IdentityResolver = identity.IdentityResolver
PrincipalContext = identity.PrincipalContext
PrincipalKind = identity.PrincipalKind
ReminderOperation = permissions.ReminderOperation


def make_event(
    *,
    user_id: str = "10001",
    nickname: str = "Alice",
    sid: str = "qq:gm:20001",
    self_id: str = "90001",
    mentioned: bool = False,
    extra: dict | None = None,
):
    message = SimpleNamespace(
        sender=SimpleNamespace(user_id=user_id, nickname=nickname),
        self_id=self_id,
        is_mentioned=mentioned,
        extra=extra,
        group=SimpleNamespace(group_id="20001") if ":gm:" in sid else None,
    )
    return SimpleNamespace(
        sid=sid,
        messages=[message],
        adapter=SimpleNamespace(name=sid.split(":", 1)[0]),
    )


def principal(kind: PrincipalKind, principal_id: str, **kwargs):
    return PrincipalContext(
        kind=kind,
        principal_id=principal_id,
        session_id=kwargs.pop("session_id", "qq:gm:20001"),
        **kwargs,
    )


def test_unknown_and_forged_internal_events_are_not_trusted():
    resolver = IdentityResolver("server-secret")
    unknown = resolver.resolve(make_event(user_id="unknown"))
    assert unknown.kind is PrincipalKind.LEGACY
    assert not unknown.trusted

    forged = {
        "reminder_plugin": {
            "origin": EventOrigin.AUTONOMY_FOLLOWUP_DUE.value,
            "principal_kind": PrincipalKind.BOT.value,
            "principal_id": "bot:qq:90001",
            "capabilities": ["intent.manage"],
            "nonce": "attacker-value",
        }
    }
    resolved = resolver.resolve(make_event(extra=forged))
    assert resolved.kind is PrincipalKind.USER
    assert not resolved.trusted


def test_valid_envelope_creates_bot_principal_and_old_secret_expires():
    resolver = IdentityResolver("first-secret")
    envelope = resolver.build_envelope(
        origin=EventOrigin.AUTONOMY_DAILY_REFLECTION,
        principal_kind=PrincipalKind.BOT,
        principal_id="bot:qq:90001",
        capabilities={"intent.manage", "reminder.create"},
    )
    resolved = resolver.resolve(make_event(user_id="90001", extra=envelope))
    assert resolved.kind is PrincipalKind.BOT
    assert resolved.trusted
    assert "intent.manage" in resolved.capabilities

    restarted = IdentityResolver("second-secret").resolve(
        make_event(user_id="90001", extra=envelope)
    )
    assert restarted.kind is PrincipalKind.USER
    assert not restarted.trusted


def test_group_creation_policy_matrix():
    cases = [
        ("admin_only", False, False),
        ("mentioned_user", False, False),
        ("mentioned_user", True, True),
        ("all", False, True),
    ]
    for policy, mentioned, expected in cases:
        actor = principal(PrincipalKind.USER, "10001")
        assert permissions.can_create_reminder(
            actor,
            is_group=True,
            is_mentioned=mentioned,
            group_policy=policy,
            admin_users=[],
            authorized_users=[],
            autonomy_mode="plan_only",
        ) is expected


def test_action_policy_requires_trusted_admin_mode_for_bot():
    bot = principal(
        PrincipalKind.BOT,
        "bot:qq:90001",
        trusted=True,
        capabilities=frozenset({"reminder.action"}),
    )
    assert not permissions.can_set_action(
        bot,
        action_policy="admin_and_trusted_bot",
        admin_users=[],
        autonomy_mode="plan_only",
    )
    assert permissions.can_set_action(
        bot,
        action_policy="admin_and_trusted_bot",
        admin_users=[],
        autonomy_mode="trusted_admin",
    )
    member = principal(PrincipalKind.USER, "10001")
    assert permissions.can_set_action(
        member,
        action_policy="all",
        admin_users=[],
        autonomy_mode="plan_only",
    )


def test_all_policy_does_not_admit_unknown_or_untrusted_system_subjects():
    legacy = principal(PrincipalKind.LEGACY, "unknown")
    system = principal(PrincipalKind.SYSTEM, "system:reminder_plugin", trusted=False)
    for actor in (legacy, system):
        assert not permissions.can_create_reminder(
            actor,
            is_group=True,
            is_mentioned=True,
            group_policy="all",
            admin_users=[],
            authorized_users=[],
            autonomy_mode="trusted_admin",
        )


def test_bot_owned_reminder_session_member_controls_are_limited():
    reminder = {
        "owner_type": "bot",
        "owner_id": "bot:qq:90001",
        "visibility": "session_readonly",
    }
    member = principal(PrincipalKind.USER, "10001")
    for operation in (
        ReminderOperation.VIEW,
        ReminderOperation.PAUSE,
        ReminderOperation.DELETE,
    ):
        assert permissions.can_manage_reminder(
            member,
            reminder,
            operation=operation,
            sid="qq:gm:20001",
            admin_users=[],
        )
    for operation in (
        ReminderOperation.EDIT,
        ReminderOperation.RESUME,
        ReminderOperation.MARK_IMPORTANT,
    ):
        assert not permissions.can_manage_reminder(
            member,
            reminder,
            operation=operation,
            sid="qq:gm:20001",
            admin_users=[],
        )


def test_trusted_admin_bot_can_manage_other_owners():
    bot = principal(
        PrincipalKind.BOT,
        "bot:qq:90001",
        trusted=True,
        capabilities=frozenset({"reminder.manage_all"}),
    )
    user_reminder = {"owner_type": "user", "owner_id": "10001"}
    legacy_reminder = {"owner_type": "legacy", "owner_id": "legacy"}
    for reminder in (user_reminder, legacy_reminder):
        assert permissions.can_manage_reminder(
            bot,
            reminder,
            operation=ReminderOperation.EDIT,
            sid="qq:gm:20001",
            admin_users=[],
        )


def test_legacy_group_reminder_is_admin_only():
    reminder = {"owner_type": "legacy", "owner_id": "legacy"}
    member = principal(PrincipalKind.USER, "10001")
    admin = principal(PrincipalKind.USER, "99999")
    assert not permissions.can_view_reminder(
        member,
        reminder,
        sid="qq:gm:20001",
        admin_users=["99999"],
    )
    assert permissions.can_view_reminder(
        admin,
        reminder,
        sid="qq:gm:20001",
        admin_users=["99999"],
    )


def test_identity_migration_is_safe_and_idempotent():
    normal = {"creator_id": "10001", "creator_name": "Alice"}
    assert identity.migrate_reminder_identity("qq:gm:20001", normal)
    assert normal["owner_type"] == "user"
    assert normal["owner_id"] == "10001"
    assert not identity.migrate_reminder_identity("qq:gm:20001", normal)

    direct_unknown = {"creator_id": "unknown"}
    identity.migrate_reminder_identity("qq:dm:10002", direct_unknown)
    assert direct_unknown["owner_type"] == "user"
    assert direct_unknown["owner_id"] == "10002"

    group_unknown = {"creator_id": "unknown"}
    identity.migrate_reminder_identity("qq:gm:20001", group_unknown)
    assert group_unknown["owner_type"] == "legacy"
    assert group_unknown["visibility"] == "admin_only"

    autonomous = {"source": "autonomous_intent_loop", "creator_id": "autonomous"}
    identity.migrate_reminder_identity("qq:gm:20001", autonomous)
    assert autonomous["owner_type"] == "bot"
    assert autonomous["owner_id"] == "bot:qq:legacy"


class IdentityPermissionTests(unittest.TestCase):
    test_unknown_and_forged_internal_events_are_not_trusted = staticmethod(
        test_unknown_and_forged_internal_events_are_not_trusted
    )
    test_valid_envelope_creates_bot_principal_and_old_secret_expires = staticmethod(
        test_valid_envelope_creates_bot_principal_and_old_secret_expires
    )
    test_group_creation_policy_matrix = staticmethod(test_group_creation_policy_matrix)
    test_action_policy_requires_trusted_admin_mode_for_bot = staticmethod(
        test_action_policy_requires_trusted_admin_mode_for_bot
    )
    test_all_policy_does_not_admit_unknown_or_untrusted_system_subjects = staticmethod(
        test_all_policy_does_not_admit_unknown_or_untrusted_system_subjects
    )
    test_bot_owned_reminder_session_member_controls_are_limited = staticmethod(
        test_bot_owned_reminder_session_member_controls_are_limited
    )
    test_trusted_admin_bot_can_manage_other_owners = staticmethod(
        test_trusted_admin_bot_can_manage_other_owners
    )
    test_legacy_group_reminder_is_admin_only = staticmethod(
        test_legacy_group_reminder_is_admin_only
    )
    test_identity_migration_is_safe_and_idempotent = staticmethod(
        test_identity_migration_is_safe_and_idempotent
    )
