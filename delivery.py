"""Durable, conservative receipts for reminder-to-LLM delivery."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Collection

from .identity import PrincipalContext, PrincipalKind
from .permissions import can_view_reminder
from .storage import ReminderStorage
from .confirmation import reminder_targets


ISSUE_STATES = frozenset({"failed", "unconfirmed", "legacy_unconfirmed"})
OPEN_STATES = ISSUE_STATES | {"awaiting_llm"}
ACK_TIMEOUT = dt.timedelta(minutes=5)


def _now() -> dt.datetime:
    return dt.datetime.now()


def _stamp(value: dt.datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


class DeliveryTracker:
    def __init__(self, ledger: ReminderStorage, reminders: ReminderStorage):
        self.ledger = ledger
        self.reminders = reminders

    async def begin(self, sid: str, reminder: dict[str, Any]) -> str | None:
        """Persist an attempt before publishing; coalesce unresolved repeats."""
        job_id = str(reminder.get("job_id") or "")
        if not job_id:
            raise ValueError("A scheduled reminder must have a job_id")
        async with self.ledger.modify() as state:
            entries = state.setdefault(sid, [])
            for entry in entries:
                if (
                    entry.get("job_id") == job_id
                    and entry.get("status") in OPEN_STATES
                    and entry.get("reminder") == reminder
                ):
                    if reminder.get("repeat", "none") != "none":
                        entry["missed_count"] = int(entry.get("missed_count") or 0) + 1
                        entry["latest_due_at"] = _stamp()
                    return None
            delivery_id = uuid.uuid4().hex
            entries.append({
                "delivery_id": delivery_id,
                "job_id": job_id,
                "status": "awaiting_llm",
                "created_at": _stamp(),
                "updated_at": _stamp(),
                "attempt_count": 1,
                "missed_count": 0,
                "reminder": dict(reminder),
            })
            return delivery_id

    async def mark(self, sid: str, delivery_id: str, status: str, error: str = "") -> dict | None:
        if status not in {"failed", "unconfirmed", "llm_received"}:
            raise ValueError("Invalid delivery state")
        found = None
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
                found = dict(entry)
                break
        if found and status == "llm_received":
            await self._cleanup_confirmed(sid, found)
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
        needs_update = False
        for sid, entries in ledger_snapshot.items():
            current_by_job = {
                str(item.get("job_id") or ""): item for item in reminders.get(sid, [])
            }
            for entry in entries:
                status = entry.get("status")
                if (
                    status == "llm_received"
                    or (status == "resolved" and entry.get("resolution") == "dismiss")
                ) and (entry.get("reminder") or {}).get("repeat", "none") == "none" and entry.get("reminder") == current_by_job.get(entry.get("job_id")):
                    needs_update = True
                if status in ISSUE_STATES and entry.get("job_id") not in current_by_job:
                    needs_update = True
                if status == "awaiting_llm":
                    try:
                        created = dt.datetime.fromisoformat(entry["created_at"])
                    except (KeyError, TypeError, ValueError):
                        created = dt.datetime.min
                    if now - created >= ACK_TIMEOUT:
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
                    if (
                        entry.get("status") == "llm_received"
                        or (entry.get("status") == "resolved" and entry.get("resolution") == "dismiss")
                    ) and (entry.get("reminder") or {}).get("repeat", "none") == "none" and entry.get("reminder") == current_by_job.get(entry.get("job_id")):
                        confirmed.append((sid, dict(entry)))
                    if entry.get("status") in ISSUE_STATES and entry.get("job_id") not in current_by_job:
                        entry["status"] = "resolved"
                        entry["resolution"] = "reminder_deleted"
                        entry["updated_at"] = _stamp(now)
                    if entry.get("status") == "awaiting_llm":
                        try:
                            created = dt.datetime.fromisoformat(entry["created_at"])
                        except (KeyError, TypeError, ValueError):
                            created = dt.datetime.min
                        if now - created >= ACK_TIMEOUT:
                            entry["status"] = "unconfirmed"
                            entry["updated_at"] = _stamp(now)
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
        decision: str,
        allow_unsafe_retry: bool = False,
        expected: dict | None = None,
    ) -> tuple[str, dict | None]:
        # Always acquire reminder data before the ledger; release before cleanup.
        async with self.reminders.read() as current_data:
            if expected is not None:
                try:
                    target = expected["reminder"]
                    if reminder_targets(current_data, sid, target["params"]) != target:
                        return "原提醒已变化，请重新查询并确认", None
                except ValueError:
                    return "原提醒已不存在，请重新查询", None
            message, entry_copy = await self._resolve_locked(
                sid, delivery_id, principal, admin_users, autonomy_sessions, decision,
                allow_unsafe_retry, expected, current_data,
            )
        if decision == "dismiss" and entry_copy:
            await self._cleanup_confirmed(sid, entry_copy)
        if entry_copy:
            await self._prune_closed(sid)
        return message, entry_copy

    async def _resolve_locked(self, sid, delivery_id, principal, admin_users,
                              autonomy_sessions, decision, allow_unsafe_retry, expected, current_data):
        if decision not in {"dismiss", "retry", "defer"}:
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
            current = next(
                (item for item in current_data.get(sid, []) if item.get("job_id") == entry.get("job_id")),
                None,
            )
            if current is None:
                return "原提醒已不存在，不能继续处理", None
            if decision == "retry" and current.get("paused"):
                return "原提醒已暂停，不能重试", None
            if decision == "retry" and current != entry.get("reminder"):
                return "原提醒已被修改，请重新检查后再处理", None
            sensitive = entry.get("status") != "failed" or (entry.get("reminder") or {}).get("action")
            if decision == "retry" and sensitive and not allow_unsafe_retry:
                return "此记录结果不明或包含 action，不能自动重试", None
            if decision == "dismiss" and sensitive and principal.kind is not PrincipalKind.WEB:
                return "此记录结果不明或包含 action，需要在 WebUI 手动确认", None
            if decision == "defer":
                entry["review_after"] = _stamp(_now() + dt.timedelta(hours=24))
                entry["updated_at"] = _stamp()
                return "已延后检查", dict(entry)
            entry["status"] = "resolved"
            entry["resolution"] = decision
            entry["resolved_by"] = principal.actor_key
            entry["updated_at"] = _stamp()
            entry_copy = dict(entry)
            if decision == "retry":
                new_id = uuid.uuid4().hex
                state[sid].append({
                    "delivery_id": new_id,
                    "job_id": entry["job_id"],
                    "status": "awaiting_llm",
                    "created_at": _stamp(),
                    "updated_at": _stamp(),
                    "attempt_count": int(entry.get("attempt_count") or 0) + 1,
                    "missed_count": 0,
                    "reminder": dict(entry.get("reminder") or {}),
                })
                entry_copy["retry_delivery_id"] = new_id
        return "已记录处理决定", entry_copy
