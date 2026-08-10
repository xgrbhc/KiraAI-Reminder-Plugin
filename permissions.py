from __future__ import annotations

from enum import Enum
from typing import Any, Collection, Mapping

from .identity import PrincipalContext, PrincipalKind


class ReminderOperation(str, Enum):
    VIEW = "view"
    EDIT = "edit"
    PAUSE = "pause"
    RESUME = "resume"
    DELETE = "delete"
    MARK_IMPORTANT = "mark_important"


BOT_SESSION_MEMBER_OPERATIONS = frozenset(
    {ReminderOperation.VIEW, ReminderOperation.PAUSE, ReminderOperation.DELETE}
)


def is_admin(principal: PrincipalContext, admin_users: Collection[str]) -> bool:
    if principal.trusted and principal.kind is PrincipalKind.WEB:
        return True
    return principal.kind is PrincipalKind.USER and principal.principal_id in admin_users


def is_authorized(principal: PrincipalContext, authorized_users: Collection[str]) -> bool:
    return principal.kind is PrincipalKind.USER and principal.principal_id in authorized_users


def can_create_reminder(
    principal: PrincipalContext,
    *,
    is_group: bool,
    is_mentioned: bool,
    group_policy: str,
    admin_users: Collection[str],
    authorized_users: Collection[str],
    autonomy_mode: str,
) -> bool:
    if principal.kind is PrincipalKind.LEGACY:
        return False
    if principal.trusted and principal.kind is PrincipalKind.WEB:
        return True
    if principal.trusted and principal.kind is PrincipalKind.BOT:
        return (
            "reminder.create" in principal.capabilities
            and autonomy_mode in {"plan_only", "act_with_confirm", "trusted_admin"}
        )
    if principal.kind is not PrincipalKind.USER:
        return False
    if not is_group:
        return True
    if is_admin(principal, admin_users) or is_authorized(principal, authorized_users):
        return True
    if group_policy == "all":
        return True
    return group_policy == "mentioned_user" and is_mentioned


def can_set_action(
    principal: PrincipalContext,
    *,
    action_policy: str,
    admin_users: Collection[str],
    autonomy_mode: str,
) -> bool:
    if is_admin(principal, admin_users):
        return True
    if action_policy == "all":
        if principal.kind is PrincipalKind.USER:
            return True
        if (
            principal.trusted
            and principal.kind is PrincipalKind.BOT
            and "reminder.action" in principal.capabilities
        ):
            return autonomy_mode == "trusted_admin"
    if (
        action_policy == "admin_and_trusted_bot"
        and principal.trusted
        and principal.kind is PrincipalKind.BOT
        and "reminder.action" in principal.capabilities
    ):
        return autonomy_mode == "trusted_admin"
    return False


def can_view_reminder(
    principal: PrincipalContext,
    reminder: Mapping[str, Any],
    *,
    sid: str,
    admin_users: Collection[str],
) -> bool:
    return can_manage_reminder(
        principal,
        reminder,
        operation=ReminderOperation.VIEW,
        sid=sid,
        admin_users=admin_users,
    )


def can_manage_reminder(
    principal: PrincipalContext,
    reminder: Mapping[str, Any],
    *,
    operation: ReminderOperation,
    sid: str,
    admin_users: Collection[str],
) -> bool:
    if is_admin(principal, admin_users):
        return True
    if (
        principal.trusted
        and principal.kind is PrincipalKind.BOT
        and "reminder.manage_all" in principal.capabilities
    ):
        return True

    owner_type = str(reminder.get("owner_type") or "legacy")
    owner_id = str(reminder.get("owner_id") or "")
    if owner_type == PrincipalKind.LEGACY.value:
        return False

    if owner_type == PrincipalKind.USER.value:
        return principal.kind is PrincipalKind.USER and principal.principal_id == owner_id

    if owner_type != PrincipalKind.BOT.value:
        return False

    if principal.trusted and principal.kind is PrincipalKind.BOT:
        if owner_id == principal.principal_id:
            return True
        if owner_id.endswith(":legacy"):
            owner_adapter = owner_id.split(":", 2)[1] if owner_id.count(":") >= 2 else ""
            principal_adapter = (
                principal.principal_id.split(":", 2)[1]
                if principal.principal_id.count(":") >= 2
                else ""
            )
            if owner_adapter and owner_adapter == principal_adapter:
                return True

    same_session_user = (
        principal.kind is PrincipalKind.USER
        and bool(sid)
        and principal.session_id == sid
    )
    return same_session_user and operation in BOT_SESSION_MEMBER_OPERATIONS
