"""Recoverable, startup-only migrations of plugin-owned files."""

from pathlib import Path
import json
import os
import shutil
from typing import Any

from .identity import IDENTITY_SCHEMA_VERSION, migrate_reminder_identity
from .storage import ReminderStorage, ReminderStorageError, write_json_atomic


def _config_bytes(path: Path) -> bytes:
    """Read an existing config snapshot without treating unreadable data as empty."""
    content = path.read_bytes()
    if not isinstance(json.loads(content.decode("utf-8")), dict):
        raise ReminderStorageError(f"Invalid config shape in {path.name}")
    return content


def _backup_config_once(path: Path, content: bytes) -> None:
    """Preserve the first readable snapshot and clean up a failed owned write."""
    backup = path.with_name(f"{path.stem}.pre-advanced-config.backup.json")
    try:
        backup_file = backup.open("xb")
    except FileExistsError:
        _config_bytes(backup)
        return
    try:
        with backup_file:
            backup_file.write(content)
            backup_file.flush()
            os.fsync(backup_file.fileno())
    except Exception:
        backup.unlink(missing_ok=True)
        raise


def persist_advanced_config(path: Path, config: dict[str, Any]) -> None:
    """Back up a valid old config before the startup-only atomic replacement."""
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        _backup_config_once(path, _config_bytes(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, config, indent=4)


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
