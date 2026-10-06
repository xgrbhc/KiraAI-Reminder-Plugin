"""Durable, conservative receipts for reminder-to-LLM delivery."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any, Collection

from .identity import PrincipalContext, PrincipalKind
from .permissions import can_view_reminder
from .storage import ReminderStorage


ISSUE_STATES = frozenset({"failed", "unconfirmed", "legacy_unconfirmed"})
OPEN_STATES = ISSUE_STATES | {"awaiting_llm"}
ACK_TIMEOUT = dt.timedelta(minutes=5)


def _now() -> dt.datetime:
    return dt.datetime.now()


def _stamp(value: dt.datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _parse_stamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    value = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::[0-5]\d(?:\.\d+)?)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?", value):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_stamp(value: Any) -> dt.datetime | None:
    parsed = _parse_stamp(value)
    return parsed.astimezone().replace(tzinfo=None) if parsed and parsed.tzinfo else parsed


def delivery_time_fields(entry: dict) -> dict:
    """Describe recorded times without inventing a trigger for legacy issues."""
    def display(value):
        parsed = _parse_stamp(value)
        if parsed is None:
            return None
        offset = parsed.strftime("%z")
        suffix = f" {offset[:3]}:{offset[3:]}" if offset else ""
        return parsed.strftime("%Y-%m-%d %H:%M") + suffix

    attempted = entry.get("status") != "legacy_unconfirmed" and entry.get("attempt_count") not in (0, "0")
    return {
        "scheduled_time": str((entry.get("reminder") or {}).get("time", "?"))[:32],
        "triggered_at": display(entry.get("created_at")) if attempted else None,
        "latest_cycle_at": display(entry.get("latest_due_at")) if entry.get("missed_count") else None,
    }


class DeliveryTracker:
    def __init__(self, ledger: ReminderStorage, reminders: ReminderStorage):
        self.ledger = ledger
        self.reminders = reminders

    async def begin(self, sid: str, reminder: dict[str, Any]) -> str | None:
        """Start a new cycle despite historical issues; guard duplicate live work."""
        job_id = str(reminder.get("job_id") or "")
        if not job_id:
            raise ValueError("A scheduled reminder must have a job_id")
        now = _now()
        cycle_key = now.strftime("%Y-%m-%d %H:%M")
        repeating = reminder.get("repeat", "none") != "none"
        merged = False
        delivery_id = None
        async with self.ledger.modify() as state:
            entries = state.setdefault(sid, [])
            duplicate = False
            for entry in entries:
                if (
                    entry.get("job_id") == job_id
                    and entry.get("reminder") == reminder
                ):
                    created = _local_stamp(entry.get("created_at"))
                    if entry.get("status") == "awaiting_llm" and (created is None or now - created >= ACK_TIMEOUT):
                        entry["status"] = "unconfirmed"
                        entry["updated_at"] = _stamp(now)
                    last_due = _local_stamp(entry.get("latest_due_at"))
                    same_cycle = (entry.get("cycle_key") == cycle_key
                                  or (created is not None and created.strftime("%Y-%m-%d %H:%M") == cycle_key)
                                  or (last_due is not None and last_due.strftime("%Y-%m-%d %H:%M") == cycle_key))
                    if not repeating or same_cycle or entry.get("status") == "awaiting_llm":
                        duplicate = True
                        if repeating and entry.get("status") == "awaiting_llm" and not same_cycle:
                            entry["missed_count"] = int(entry.get("missed_count") or 0) + 1
                            entry["latest_due_at"] = _stamp(now)
            merged = self._merge_repeat_issues(entries)
            if not duplicate:
                delivery_id = uuid.uuid4().hex
                entry = {
                    "delivery_id": delivery_id, "job_id": job_id,
                    "status": "awaiting_llm", "created_at": _stamp(now),
                    "updated_at": _stamp(now), "attempt_count": 1,
                    "missed_count": 0, "reminder": dict(reminder),
                }
                if repeating:
                    entry["cycle_key"] = cycle_key
                entries.append(entry)
        if merged:
            await self._prune_closed(sid)
        return delivery_id

    @staticmethod
    def _merge_repeat_issues(entries: list[dict]) -> bool:
        """Summarize issues, not dispatch: each new cycle keeps its own receipt."""
        groups: dict[str, list[dict]] = {}
        merged = False
        for entry in entries:
            reminder = entry.get("reminder") or {}
            if entry.get("status") not in ISSUE_STATES or reminder.get("repeat", "none") == "none":
                continue
            candidates = groups.setdefault(str(entry.get("job_id") or ""), [])
            previous = next((item for item in candidates if item.get("reminder") == reminder), None)
            if previous is None:
                candidates.append(entry)
                continue
            previous["status"] = "failed" if previous["status"] == entry["status"] == "failed" else "unconfirmed"
            previous["missed_count"] = int(previous.get("missed_count") or 0) + int(entry.get("missed_count") or 0) + 1
            old_time = _local_stamp(previous.get("latest_due_at") or previous.get("created_at"))
            new_value = entry.get("latest_due_at") or entry.get("created_at")
            new_time = _local_stamp(new_value)
            if new_time is not None and (old_time is None or new_time >= old_time):
                previous["latest_due_at"] = new_value
                if entry.get("last_error"):
                    previous["last_error"] = entry["last_error"]
            previous["updated_at"] = _stamp()
            entry["status"] = "resolved"
            entry["resolution"] = "merged"
            entry["merged_into"] = previous["delivery_id"]
            merged = True
        return merged

    async def mark(self, sid: str, delivery_id: str, status: str, error: str = "") -> dict | None:
        if status not in {"failed", "unconfirmed", "llm_received"}:
            raise ValueError("Invalid delivery state")
        found = None
        merged = False
        async with self.ledger.modify() as state:
            for entry in state.get(sid, []):
                if entry.get("delivery_id") != delivery_id:
                    continue
                if entry.get("status") not in OPEN_STATES:
                    return None
                if status == "llm_received" and entry.get("status") != "awaiting_llm":
                    return None
                entry["status"] = status
                entry["updated_at"] = _stamp()
                if error:
                    entry["last_error"] = str(error)[:300]
                merged = self._merge_repeat_issues(state[sid])
                found = dict(entry)
                break
        if found and status == "llm_received":
            await self._cleanup_confirmed(sid, found)
            await self._prune_closed(sid)
        elif merged:
            await self._prune_closed(sid)
        return found

    async def _prune_closed(self, sid: str) -> None:
        current = await self.reminders.load()
        current_by_job = {
            str(item.get("job_id") or ""): item for item in current.get(sid, [])
        }
        snapshot = await self.ledger.load()
        entries = snapshot.get(sid, [])
        eligible = [
            entry for entry in entries
            if entry.get("status") in {"llm_received", "resolved"}
            and (
                entry.get("resolution") in {"retry", "reminder_deleted"}
                or (entry.get("reminder") or {}).get("repeat", "none") != "none"
                or entry.get("reminder") != current_by_job.get(entry.get("job_id"))
            )
        ]
        if len(eligible) <= 100:
            return
        remove_ids = {
            entry.get("delivery_id")
            for entry in sorted(eligible, key=lambda item: item.get("updated_at", ""))[:-100]
        }
        async with self.ledger.modify() as state:
            state[sid] = [
                entry for entry in state.get(sid, [])
                if entry.get("delivery_id") not in remove_ids
            ]

    async def _cleanup_confirmed(self, sid: str, entry: dict) -> None:
        reminder = entry.get("reminder") or {}
        if reminder.get("repeat", "none") != "none":
            return
        job_id = entry.get("job_id")
        async with self.reminders.modify() as data:
            if sid in data:
                data[sid] = [
                    r for r in data[sid]
                    if r.get("job_id") != job_id or r != reminder
                ]

    async def reconcile(self, *, startup: bool = False) -> None:
        """Keep old overdue reminders visible; never replay ambiguous events."""
        reminders = await self.reminders.load()
        ledger_snapshot = await self.ledger.load()
        now = _now()
        overdue_cutoff = now if startup else now - ACK_TIMEOUT
        needs_update = any(self._merge_repeat_issues(entries) for entries in ledger_snapshot.values())
        for sid, entries in ledger_snapshot.items():
            current_by_job = {
                str(item.get("job_id") or ""): item for item in reminders.get(sid, [])
            }
            for entry in entries:
                status = entry.get("status")
                if status == "llm_received" and (entry.get("reminder") or {}).get("repeat", "none") == "none" and entry.get("reminder") == current_by_job.get(entry.get("job_id")):
                    needs_update = True
                if status in ISSUE_STATES and entry.get("job_id") not in current_by_job:
                    needs_update = True
                if status == "awaiting_llm":
                    created = _local_stamp(entry.get("created_at"))
                    if created is None or now - created >= ACK_TIMEOUT:
                        needs_update = True
        for sid, records in reminders.items():
            for reminder in records:
                if reminder.get("repeat", "none") != "none" or reminder.get("paused"):
                    continue
                job_id = str(reminder.get("job_id") or "")
                known = any(
                    entry.get("job_id") == job_id and entry.get("reminder") == reminder
                    for entry in ledger_snapshot.get(sid, [])
                )
                if not job_id or known:
                    continue
                try:
                    due = dt.datetime.strptime(reminder["time"], "%Y-%m-%d %H:%M")
                except (KeyError, TypeError, ValueError):
                    continue
                if due <= overdue_cutoff:
                    needs_update = True
                    break
        if not needs_update:
            return
        confirmed: list[tuple[str, dict]] = []
        async with self.ledger.modify() as state:
            for sid, entries in state.items():
                current_by_job = {
                    str(item.get("job_id") or ""): item for item in reminders.get(sid, [])
                }
                for entry in entries:
                    if entry.get("status") == "llm_received" and (entry.get("reminder") or {}).get("repeat", "none") == "none" and entry.get("reminder") == current_by_job.get(entry.get("job_id")):
                        confirmed.append((sid, dict(entry)))
                    if entry.get("status") in ISSUE_STATES and entry.get("job_id") not in current_by_job:
                        entry["status"] = "resolved"
                        entry["resolution"] = "reminder_deleted"
                        entry["updated_at"] = _stamp(now)
                    if entry.get("status") == "awaiting_llm":
                        created = _local_stamp(entry.get("created_at"))
                        if created is None or now - created >= ACK_TIMEOUT:
                            entry["status"] = "unconfirmed"
                            entry["updated_at"] = _stamp(now)
                self._merge_repeat_issues(entries)
            for sid, records in reminders.items():
                entries = state.setdefault(sid, [])
                for reminder in records:
                    if reminder.get("repeat", "none") != "none" or reminder.get("paused"):
                        continue
                    job_id = str(reminder.get("job_id") or "")
                    known = any(
                        entry.get("job_id") == job_id and entry.get("reminder") == reminder
                        for entry in entries
                    )
                    if not job_id or known:
                        continue
                    try:
                        due = dt.datetime.strptime(reminder["time"], "%Y-%m-%d %H:%M")
                    except (KeyError, TypeError, ValueError):
                        continue
                    if due <= overdue_cutoff:
                        entries.append({
                            "delivery_id": uuid.uuid4().hex,
                            "job_id": job_id,
                            "status": "legacy_unconfirmed",
                            "created_at": _stamp(now),
                            "updated_at": _stamp(now),
                            "attempt_count": 0,
                            "missed_count": 0,
                            "reminder": dict(reminder),
                        })
        for sid, entry in confirmed:
            await self._cleanup_confirmed(sid, entry)
        for sid in ledger_snapshot:
            await self._prune_closed(sid)

    @staticmethod
    def may_review(
        principal: PrincipalContext,
        reminder: dict,
        sid: str,
        admin_users: Collection[str],
        autonomy_sessions: Collection[str],
    ) -> bool:
        if can_view_reminder(principal, reminder, sid=sid, admin_users=admin_users):
            return True
        return (
            principal.trusted
            and principal.kind is PrincipalKind.BOT
            and principal.is_autonomy_event
            and "intent.manage" in principal.capabilities
            and ":dm:" in sid
            and sid in autonomy_sessions
            and principal.session_id == sid
        )

    async def list_issues(
        self,
        sid: str,
        principal: PrincipalContext,
        admin_users: Collection[str],
        autonomy_sessions: Collection[str],
        include_awaiting: bool = False,
    ) -> list[dict]:
        state = await self.ledger.load()
        visible_states = OPEN_STATES if include_awaiting else ISSUE_STATES
        return [
            dict(entry)
            for entry in state.get(sid, [])
            if entry.get("status") in visible_states
            and self.may_review(
                principal, entry.get("reminder") or {}, sid, admin_users, autonomy_sessions
            )
        ]

    async def resolve(
        self,
        sid: str,
        delivery_id: str,
        principal: PrincipalContext,
        admin_users: Collection[str],
        autonomy_sessions: Collection[str],
        decision: str = "dismiss",
        expected: dict | None = None,
    ) -> tuple[str, dict | None]:
        message, entry_copy = await self._resolve_locked(
            sid, delivery_id, principal, admin_users, autonomy_sessions, decision, expected,
        )
        if entry_copy:
            await self._prune_closed(sid)
        return message, entry_copy

    async def _resolve_locked(self, sid, delivery_id, principal, admin_users,
                              autonomy_sessions, decision, expected):
        if decision != "dismiss":
            return "无效处理方式", None
        entry_copy = None
        async with self.ledger.modify() as state:
            entry = next(
                (item for item in state.get(sid, []) if item.get("delivery_id") == delivery_id),
                None,
            )
            if entry is None:
                return "找不到投递记录", None
            if expected is not None and entry != expected["entry"]:
                return "投递记录已变化，请重新查询并确认", None
            if not self.may_review(
                principal, entry.get("reminder") or {}, sid, admin_users, autonomy_sessions
            ):
                return "权限拒绝", None
            if entry.get("status") not in ISSUE_STATES:
                return "该记录已经处理或仍在等待模型", None
            entry["status"] = "resolved"
            entry["resolution"] = decision
            entry["resolved_by"] = principal.actor_key
            entry["updated_at"] = _stamp()
            entry_copy = dict(entry)
        return "已记录处理决定", entry_copy
