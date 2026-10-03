"""Type-only descriptions of the existing persisted JSON records.

Fields are optional because historical reminders may predate current identity
and autonomy metadata. These types do not validate or rewrite stored records.
"""

from __future__ import annotations

from typing import TypedDict


class ReminderRecord(TypedDict, total=False):
    content: str
    time: str
    repeat: str
    job_id: str
    created_at: str
    creator_id: str
    creator_name: str
    session_type: str
    owner_type: str
    owner_id: str
    identity_schema: int
    owner_adapter_name: str
    created_by_adapter_name: str
    category: str
    action: str
    interval_minutes: int
    important: bool
    paused: bool
    is_random: bool
    random_batch_id: str
    random_index: int
    random_total: int
    time_range: dict[str, str]
    source: str
    intent_id: str


class AutonomousIntentRecord(TypedDict, total=False):
    id: str
    title: str
    status: str
    priority: float
    source: str
    created_at: str
    updated_at: str
    next_check_at: str
    next_check_job_id: str
    notes: str
    last_followup_job_id: str
