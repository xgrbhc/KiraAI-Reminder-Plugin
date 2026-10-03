from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional


IDENTITY_SCHEMA_VERSION = 3
ENVELOPE_NAMESPACE = "reminder_plugin"
UNTRUSTED_IDS = frozenset(
    {
        "",
        "unknown",
        "legacy_user",
        "system",
        "system:reminder_plugin",
        "autonomous",
        "web_admin_superuser",
    }
)


class PrincipalKind(str, Enum):
    USER = "user"
    BOT = "bot"
    SYSTEM = "system"
    WEB = "web"
    LEGACY = "legacy"


class EventOrigin(str, Enum):
    USER_MESSAGE = "user_message"
    GROUP_PROACTIVE_CHAT = "group_proactive_chat"
    REMINDER_FIRE = "reminder_fire"
    AUTONOMY_DAILY_REFLECTION = "autonomy_daily_reflection"
    AUTONOMY_FOLLOWUP_DUE = "autonomy_followup_due"
    AUTONOMY_RANDOM_CHECK = "autonomy_random_check"
    WEB_ADMIN = "web_admin"
    LEGACY = "legacy"


AUTONOMY_ORIGINS = frozenset(
    {
        EventOrigin.AUTONOMY_DAILY_REFLECTION,
        EventOrigin.AUTONOMY_FOLLOWUP_DUE,
        EventOrigin.AUTONOMY_RANDOM_CHECK,
    }
)


@dataclass(frozen=True)
class PrincipalContext:
    kind: PrincipalKind
    principal_id: str
    display_name: str = ""
    origin: EventOrigin = EventOrigin.USER_MESSAGE
    session_id: str = ""
    bot_id: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)
    trusted: bool = False
    delegated_owner_id: str = ""
    delivery_id: str = ""
    adapter_name: str = ""

    @property
    def is_autonomy_event(self) -> bool:
        return self.origin in AUTONOMY_ORIGINS

    @property
    def adapter_scope(self) -> str:
        adapter = normalize_adapter_name(self.adapter_name)
        session_adapter = adapter_from_session_id(self.session_id)
        if adapter and session_adapter and adapter != session_adapter:
            return ""
        return adapter or session_adapter

    @property
    def scoped_user_id(self) -> str:
        if not self.adapter_scope or not self.principal_id:
            return ""
        return f"{self.adapter_scope}:{self.principal_id}"

    @property
    def actor_key(self) -> str:
        if self.kind in {PrincipalKind.USER, PrincipalKind.LEGACY}:
            return f"{self.kind.value}:{self.adapter_scope or 'unknown'}:{self.principal_id}"
        return f"{self.kind.value}:{self.principal_id}"


def normalize_adapter_name(value: Any) -> str:
    """Accept an unambiguous Kira session namespace; preserve its case."""
    name = str(value or "").strip()
    return name if name and ":" not in name and name != "unknown" else ""


def adapter_from_session_id(sid: str) -> str:
    parts = str(sid or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"dm", "gm", "sm"} or not parts[2]:
        return ""
    return normalize_adapter_name(parts[0])


def build_bot_principal_id(adapter_name: str, self_id: Any) -> str:
    adapter = str(adapter_name or "unknown").strip() or "unknown"
    bot_id = str(self_id or "unknown").strip() or "unknown"
    return f"bot:{adapter}:{bot_id}"


def _safe_kind(value: Any) -> Optional[PrincipalKind]:
    try:
        return PrincipalKind(str(value))
    except (TypeError, ValueError):
        return None


def _safe_origin(value: Any) -> Optional[EventOrigin]:
    try:
        return EventOrigin(str(value))
    except (TypeError, ValueError):
        return None


def _event_messages(event: Any) -> list[Any]:
    messages = getattr(event, "messages", None)
    if isinstance(messages, list) and messages:
        return messages
    message = getattr(event, "message", None)
    return [message] if message is not None else []


def _event_sid(event: Any) -> str:
    sid = getattr(event, "sid", "")
    if sid:
        return str(sid)
    session = getattr(event, "session", None)
    return str(getattr(session, "sid", "") or "")


def _event_adapter_name(event: Any) -> str:
    adapter = getattr(event, "adapter", None)
    name = getattr(adapter, "name", "")
    if name:
        return normalize_adapter_name(name)
    session = getattr(event, "session", None)
    name = getattr(session, "adapter_name", "")
    return normalize_adapter_name(name) or adapter_from_session_id(_event_sid(event))


def _event_bot_id(event: Any, messages: list[Any]) -> str:
    value = getattr(event, "self_id", "")
    if value:
        return str(value)
    if messages:
        return str(getattr(messages[-1], "self_id", "") or "")
    return ""


def _event_extra(event: Any, messages: list[Any]) -> Mapping[str, Any]:
    candidates = [getattr(event, "extra", None)]
    if messages:
        candidates.append(getattr(messages[-1], "extra", None))
    fallback: Mapping[str, Any] = {}
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            if ENVELOPE_NAMESPACE in candidate:
                return candidate
            if not fallback:
                fallback = candidate
    return fallback


def _event_sender(messages: list[Any]) -> tuple[str, str]:
    if not messages:
        return "", ""
    sender = getattr(messages[-1], "sender", None)
    if sender is None:
        return "", ""
    user_id = str(getattr(sender, "user_id", "") or "").strip()
    nickname = str(getattr(sender, "nickname", "") or "").strip()
    return user_id, nickname


class IdentityResolver:
    """Resolve user and process-authenticated internal principals from Kira events."""

    def __init__(self, process_secret: str):
        if not process_secret:
            raise ValueError("process_secret must not be empty")
        self._process_secret = process_secret

    def build_envelope(
        self,
        *,
        origin: EventOrigin,
        principal_kind: PrincipalKind,
        principal_id: str,
        capabilities: Iterable[str] = (),
        delegated_owner_id: str = "",
        delivery_id: str = "",
    ) -> dict[str, dict[str, Any]]:
        return {
            ENVELOPE_NAMESPACE: {
                "origin": origin.value,
                "principal_kind": principal_kind.value,
                "principal_id": str(principal_id),
                "delegated_owner_id": str(delegated_owner_id or ""),
                "delivery_id": str(delivery_id or ""),
                "capabilities": sorted({str(item) for item in capabilities if str(item)}),
                "nonce": self._process_secret,
            }
        }

    def resolve(self, event: Any) -> PrincipalContext:
        messages = _event_messages(event)
        session_id = _event_sid(event)
        bot_id = _event_bot_id(event, messages)
        extra = _event_extra(event, messages)
        payload = extra.get(ENVELOPE_NAMESPACE)
        if isinstance(payload, Mapping):
            nonce = str(payload.get("nonce") or "")
            kind = _safe_kind(payload.get("principal_kind"))
            origin = _safe_origin(payload.get("origin"))
            principal_id = str(payload.get("principal_id") or "").strip()
            if (
                nonce
                and hmac.compare_digest(nonce, self._process_secret)
                and kind is not None
                and origin is not None
                and principal_id
            ):
                capabilities = payload.get("capabilities")
                if not isinstance(capabilities, (list, tuple, set, frozenset)):
                    capabilities = []
                _, sender_name = _event_sender(messages)
                return PrincipalContext(
                    kind=kind,
                    principal_id=principal_id,
                    display_name=sender_name,
                    origin=origin,
                    session_id=session_id,
                    bot_id=bot_id,
                    capabilities=frozenset(str(item) for item in capabilities if str(item)),
                    trusted=True,
                    delegated_owner_id=str(payload.get("delegated_owner_id") or ""),
                    delivery_id=str(payload.get("delivery_id") or ""),
                    adapter_name=_event_adapter_name(event),
                )

        user_id, nickname = _event_sender(messages)
        kind = PrincipalKind.USER if user_id not in UNTRUSTED_IDS else PrincipalKind.LEGACY
        origin = (
            EventOrigin.GROUP_PROACTIVE_CHAT
            if kind is PrincipalKind.USER and _is_unmentioned_group_event(event, messages, session_id)
            else EventOrigin.USER_MESSAGE
        )
        return PrincipalContext(
            kind=kind,
            principal_id=user_id,
            display_name=nickname,
            origin=origin,
            session_id=session_id,
            bot_id=bot_id,
            trusted=False,
            adapter_name=_event_adapter_name(event),
        )


def _is_unmentioned_group_event(event: Any, messages: list[Any], session_id: str) -> bool:
    is_group = ":gm:" in session_id
    if not is_group:
        checker = getattr(event, "is_group_message", None)
        try:
            is_group = bool(checker()) if callable(checker) else False
        except Exception:
            is_group = False
    if not is_group:
        return False
    mentioned = getattr(event, "is_mentioned", None)
    if mentioned is None and messages:
        mentioned = getattr(messages[-1], "is_mentioned", False)
    return not bool(mentioned)


def migrate_reminder_identity(sid: str, reminder: dict[str, Any]) -> bool:
    """Add adapter scope, preserving v2 ownership and compatibility fields."""
    version = reminder.get("identity_schema")
    if isinstance(version, int) and version >= IDENTITY_SCHEMA_VERSION:
        return False

    adapter_name = adapter_from_session_id(sid)
    if version == 2:
        reminder.update({
            "identity_schema": IDENTITY_SCHEMA_VERSION,
            "owner_adapter_name": adapter_name,
            "created_by_adapter_name": adapter_name,
        })
        return True

    creator_id = str(reminder.get("creator_id") or "legacy_user")
    creator_name = str(reminder.get("creator_name") or "未知")
    session_type = "gm" if ":gm:" in sid else "dm"
    is_autonomous = (
        reminder.get("source") == "autonomous_intent_loop"
        or reminder.get("managed_by") == "reminder_plugin.autonomous"
    )

    if is_autonomous:
        owner_type = PrincipalKind.BOT.value
        owner_id = build_bot_principal_id(adapter_name, "legacy")
        owner_name = "自主意图循环"
        origin = EventOrigin.AUTONOMY_FOLLOWUP_DUE.value
        visibility = "session_readonly"
        created_by_type = PrincipalKind.BOT.value
    elif creator_id not in UNTRUSTED_IDS:
        owner_type = PrincipalKind.USER.value
        owner_id = creator_id
        owner_name = creator_name
        origin = EventOrigin.USER_MESSAGE.value
        visibility = "owner"
        created_by_type = PrincipalKind.USER.value
    elif session_type == "dm" and len(sid.split(":", 2)) == 3:
        owner_type = PrincipalKind.USER.value
        owner_id = sid.split(":", 2)[2]
        owner_name = creator_name
        origin = EventOrigin.LEGACY.value
        visibility = "owner"
        created_by_type = PrincipalKind.LEGACY.value
    else:
        owner_type = PrincipalKind.LEGACY.value
        owner_id = "legacy"
        owner_name = creator_name
        origin = EventOrigin.LEGACY.value
        visibility = "admin_only"
        created_by_type = PrincipalKind.LEGACY.value

    reminder.update(
        {
            "identity_schema": IDENTITY_SCHEMA_VERSION,
            "owner_type": owner_type,
            "owner_id": owner_id,
            "owner_adapter_name": adapter_name,
            "owner_name": owner_name,
            "created_by_type": created_by_type,
            "created_by_id": owner_id,
            "created_by_adapter_name": adapter_name,
            "origin": origin,
            "visibility": visibility,
            "managed_by": reminder.get("managed_by")
            or ("reminder_plugin.autonomous" if is_autonomous else "reminder_plugin"),
            "session_type": reminder.get("session_type") or session_type,
        }
    )
    if owner_type in {PrincipalKind.USER.value, PrincipalKind.BOT.value}:
        reminder["creator_id"] = owner_id
        reminder["creator_name"] = owner_name
    return True
