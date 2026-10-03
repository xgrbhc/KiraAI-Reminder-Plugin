"""Recoverable, startup-only migrations of plugin-owned files."""

from pathlib import Path
import shutil
from typing import Any

from .identity import IDENTITY_SCHEMA_VERSION, migrate_reminder_identity
from .storage import ReminderStorage


def backup_once(path: Path, suffix: str) -> None:
    """Keep the first pre-migration snapshot; never overwrite an existing backup."""
    backup = path.with_name(f"{path.stem}.{suffix}.backup.json")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)


async def pin_legacy_acl_adapter(path: Path, config: dict[str, Any], adapter: str) -> None:
    """Persist the inferred scope before granting any legacy-ID privileges."""
    storage = ReminderStorage(path)
    saved = await storage.load()
    if not path.exists():
        saved = dict(config)
    saved["legacy_acl_adapter"] = adapter
    backup_once(path, "pre-adapter-acl")
    await storage.save(saved)


async def migrate_identity_stores(
    reminders: ReminderStorage, deliveries: ReminderStorage,
) -> int:
    """Upgrade records and receipts together, preserving exact snapshot matching.

    Both files are validated before writing. Each write is atomic, not a two-file
    transaction: startup must stop on a failure and a later retry completes the
    idempotent migration before the scheduler can run.
    """
    data = await reminders.load()
    ledger = await deliveries.load()
    changed_reminders = 0
    changed_snapshots = 0
    for sid, records in data.items():
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict) and migrate_reminder_identity(str(sid), record):
                changed_reminders += 1
    for sid, entries in ledger.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            snapshot = entry.get("reminder") if isinstance(entry, dict) else None
            if isinstance(snapshot, dict) and migrate_reminder_identity(str(sid), snapshot):
                changed_snapshots += 1

    suffix = f"pre-identity-v{IDENTITY_SCHEMA_VERSION}"
    if changed_reminders:
        backup_once(reminders.path, suffix)
    if changed_snapshots:
        backup_once(deliveries.path, suffix)
    if changed_reminders:
        await reminders.save(data)
    if changed_snapshots:
        await deliveries.save(ledger)
    return changed_reminders + changed_snapshots
