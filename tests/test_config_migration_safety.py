"""Protect the plugin's own startup migration without touching real config."""

import copy
import json
import sys
from pathlib import Path

import pytest


def setup_migration(reminder_main, tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_main, "get_data_path", lambda: tmp_path)
    warnings = []
    monkeypatch.setattr(reminder_main.logger, "warning", warnings.append)
    path = tmp_path / "config/plugins/reminder_plugin.json"
    backup = path.with_name("reminder_plugin.pre-advanced-config.backup.json")
    migration = sys.modules[reminder_main.persist_advanced_config.__module__]
    storage = sys.modules[reminder_main.ReminderStorage.__module__]
    return path, backup, migration, storage, warnings


def legacy_config(reminder_main):
    return {
        "autonomy_mode": "off",
        "advanced_config": {
            "autonomy_mode": reminder_main.ADVANCED_CONFIG_DEFAULTS["autonomy_mode"],
            "usage_prompt": "自定义提示词：请保留\n完整原文。",
        },
        "admin_users": ["OtherAdapter:123"], "unknown": {"keep": [1, "二"]},
    }


def seed(path, config):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(config, ensure_ascii=False, indent=1) + "\r\n").encode("utf-8")
    path.write_bytes(content)
    return content


def migrate(reminder_main, cfg):
    return reminder_main.ReminderPlugin._migrate_advanced_config(cfg)


def different_value(default):
    if isinstance(default, bool):
        return not default
    if isinstance(default, int):
        return default + 1
    if isinstance(default, list):
        return ["old-tool"]
    return "old custom value"


@pytest.mark.parametrize("existing_file", [True, False])
def test_success_is_backed_up_and_idempotent(reminder_main, tmp_path, monkeypatch, existing_file):
    path, backup, migration, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original_cfg = copy.deepcopy(cfg)
    original_bytes = seed(path, cfg) if existing_file else None
    result = migrate(reminder_main, cfg)
    assert cfg == original_cfg
    assert "autonomy_mode" not in result
    assert result["advanced_config"]["autonomy_mode"] == "off"
    assert result["advanced_config"]["usage_prompt"] == cfg["advanced_config"]["usage_prompt"]
    assert result["unknown"] == cfg["unknown"]
    assert result["admin_users"] == cfg["admin_users"]
    assert json.loads(path.read_text(encoding="utf-8")) == result
    assert b'\n    "advanced_config"' in path.read_bytes()
    assert b"\r\n" not in path.read_bytes()
    if existing_file:
        assert backup.read_bytes() == original_bytes
    else:
        assert not backup.exists()
    assert warnings == []
    before = path.read_bytes()
    monkeypatch.setattr(migration, "persist_advanced_config", lambda *args: pytest.fail("unexpected save"))
    monkeypatch.setattr(reminder_main, "persist_advanced_config", migration.persist_advanced_config)
    assert migrate(reminder_main, result) == result
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("cfg", [None, [], {"autonomy_mode": "off"},
                                {"autonomy_mode": "off", "advanced_config": None},
                                {"autonomy_mode": "off", "advanced_config": "invalid"},
                                {"advanced_config": {"autonomy_mode": "observe"}}])
def test_no_migration_does_not_read_or_write(reminder_main, tmp_path, monkeypatch, cfg):
    path, backup, _, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    monkeypatch.setattr(reminder_main, "persist_advanced_config", lambda *args: pytest.fail("unexpected save"))
    result = migrate(reminder_main, cfg)
    assert result == (cfg if isinstance(cfg, dict) else {})
    assert not path.parent.exists()
    assert not backup.exists()
    assert warnings == []


@pytest.mark.parametrize("key", ["autonomy_mode", "daily_reflection_enabled", "followup_due_enabled",
    "daily_reflection_hour", "random_check_start_hour", "random_check_end_hour",
    "autonomy_allowed_tools", "usage_prompt", "autonomous_usage_prompt"])
@pytest.mark.parametrize("custom_advanced", [False, True])
def test_existing_field_precedence_is_unchanged(reminder_main, tmp_path, monkeypatch, key, custom_advanced):
    path, _, _, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    default = reminder_main.ADVANCED_CONFIG_DEFAULTS[key]
    old = different_value(default)
    advanced = old if custom_advanced else copy.deepcopy(default)
    root_value = copy.deepcopy(default) if custom_advanced else old
    cfg = {key: root_value, "advanced_config": {key: advanced}, "other": "keep"}
    seed(path, cfg)
    result = migrate(reminder_main, cfg)
    assert key not in result
    assert result["advanced_config"][key] == old
    assert cfg[key] == root_value and cfg["advanced_config"][key] == advanced
    assert json.loads(path.read_text(encoding="utf-8")) == result
    assert result["other"] == "keep"
    assert warnings == []


@pytest.mark.parametrize("payload", [b"", b"{broken", b"\xff", b"[]", b"null", b"42", b'"text"'])
def test_bad_existing_config_is_never_replaced(reminder_main, tmp_path, monkeypatch, payload):
    path, backup, _, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    result = migrate(reminder_main, legacy_config(reminder_main))
    assert result["advanced_config"]["autonomy_mode"] == "off"
    assert path.read_bytes() == payload
    assert not backup.exists()
    assert list(path.parent.glob("*.tmp")) == []
    assert len(warnings) == 1 and "Failed to migrate advanced config" in warnings[0]


@pytest.mark.parametrize("error", [PermissionError("read denied"), FileNotFoundError("read interrupted")])
def test_existing_but_unreadable_config_is_not_treated_as_missing(reminder_main, tmp_path, monkeypatch, error):
    path, backup, _, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original = seed(path, cfg)
    read_bytes = Path.read_bytes
    def denied(candidate):
        if candidate == path:
            raise error
        return read_bytes(candidate)
    monkeypatch.setattr(Path, "read_bytes", denied)
    migrate(reminder_main, cfg)
    assert read_bytes(path) == original
    assert not backup.exists()
    assert len(warnings) == 1


class FailingFile:
    """Inject partial writes or flush failures while still closing the real file."""

    def __init__(self, file, stage):
        self.file = file
        self.stage = stage

    def __enter__(self):
        self.file.__enter__()
        return self

    def __exit__(self, *args):
        return self.file.__exit__(*args)

    def write(self, content):
        if self.stage == "write":
            self.file.write(content[:3])
            raise OSError("test partial write")
        return self.file.write(content)

    def flush(self):
        self.file.flush()
        if self.stage == "flush":
            raise OSError("test flush denied")

    def fileno(self):
        return self.file.fileno()


@pytest.mark.parametrize("stage", ["open", "write", "flush", "fsync"])
def test_failed_backup_blocks_replacement_and_cleans_only_owned_backup(reminder_main, tmp_path, monkeypatch, stage):
    path, backup, migration, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original = seed(path, cfg)
    original_open = Path.open
    def failing_open(candidate, mode="r", *args, **kwargs):
        if candidate == backup and mode == "xb":
            if stage == "open":
                raise PermissionError("test backup denied")
            return FailingFile(original_open(candidate, mode, *args, **kwargs), stage)
        return original_open(candidate, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", failing_open)
    if stage == "fsync":
        def denied_sync(fd):
            raise OSError("test backup sync denied")
        monkeypatch.setattr(migration.os, "fsync", denied_sync)
    migrate(reminder_main, cfg)
    assert path.read_bytes() == original
    assert not backup.exists()
    assert list(path.parent.glob("*.tmp")) == []
    assert len(warnings) == 1


@pytest.mark.parametrize("stage", ["create", "write", "flush", "fsync", "replace", "serialize"])
def test_failed_atomic_write_keeps_original_and_completed_backup(reminder_main, tmp_path, monkeypatch, stage):
    path, backup, _, storage, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original = seed(path, cfg)
    def reject(*args, **kwargs):
        raise PermissionError("test atomic write denied")
    if stage == "create":
        monkeypatch.setattr(storage.tempfile, "mkstemp", reject)
    elif stage in {"write", "flush"}:
        fdopen = storage.os.fdopen
        monkeypatch.setattr(storage.os, "fdopen", lambda *args, **kwargs: FailingFile(fdopen(*args, **kwargs), stage))
    elif stage == "fsync":
        fsync = storage.os.fsync
        calls = []
        def fail_second_sync(fd):
            calls.append(fd)
            if len(calls) == 2:
                reject()
            fsync(fd)
        monkeypatch.setattr(storage.os, "fsync", fail_second_sync)
    elif stage == "replace":
        monkeypatch.setattr(storage.os, "replace", reject)
    else:
        cfg["not_json"] = object()
    result = migrate(reminder_main, cfg)
    assert result["advanced_config"]["autonomy_mode"] == "off"
    assert path.read_bytes() == original
    assert backup.read_bytes() == original
    assert list(path.parent.glob("*.tmp")) == []
    assert len(warnings) == 1


def test_existing_valid_backup_is_not_overwritten(reminder_main, tmp_path, monkeypatch):
    path, backup, _, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    seed(path, cfg)
    first = b'{"first": "snapshot"}\r\n'
    backup.write_bytes(first)
    result = migrate(reminder_main, cfg)
    assert backup.read_bytes() == first
    assert json.loads(path.read_text(encoding="utf-8")) == result
    assert warnings == []


@pytest.mark.parametrize("payload", [b"", b"{partial", b"[]", b"\xff"])
def test_invalid_previous_backup_is_not_overwritten_or_trusted(reminder_main, tmp_path, monkeypatch, payload):
    path, backup, _, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original = seed(path, cfg)
    backup.write_bytes(payload)
    migrate(reminder_main, cfg)
    assert path.read_bytes() == original
    assert backup.read_bytes() == payload
    assert len(warnings) == 1


def test_replace_failure_can_be_retried_with_same_first_backup(reminder_main, tmp_path, monkeypatch):
    path, backup, _, storage, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original = seed(path, cfg)
    replace = storage.os.replace
    def reject(*args):
        raise OSError("test replacement denied")
    monkeypatch.setattr(storage.os, "replace", reject)
    migrate(reminder_main, cfg)
    assert path.read_bytes() == backup.read_bytes() == original
    monkeypatch.setattr(storage.os, "replace", replace)
    result = migrate(reminder_main, cfg)
    assert json.loads(path.read_text(encoding="utf-8")) == result
    assert backup.read_bytes() == original
    assert len(warnings) == 1
    assert list(path.parent.glob("*.tmp")) == []


def test_unreadable_backup_is_preserved_and_blocks_replacement(reminder_main, tmp_path, monkeypatch):
    path, backup, _, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original = seed(path, cfg)
    snapshot = b'{"first": "snapshot"}'
    backup.write_bytes(snapshot)
    read_bytes = Path.read_bytes
    def denied_read(candidate):
        if candidate == backup:
            raise PermissionError("test backup read denied")
        return read_bytes(candidate)
    monkeypatch.setattr(Path, "read_bytes", denied_read)
    migrate(reminder_main, cfg)
    assert path.read_bytes() == original
    assert read_bytes(backup) == snapshot
    assert len(warnings) == 1


def test_stat_error_never_creates_or_replaces_config(reminder_main, tmp_path, monkeypatch):
    path, backup, _, _, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original = seed(path, cfg)
    lstat = Path.lstat
    def denied_stat(candidate):
        if candidate == path:
            raise PermissionError("test stat denied")
        return lstat(candidate)
    monkeypatch.setattr(Path, "lstat", denied_stat)
    migrate(reminder_main, cfg)
    assert path.read_bytes() == original
    assert not backup.exists()
    assert len(warnings) == 1


def test_fdopen_failure_closes_owned_descriptor_and_cleans_temp(reminder_main, tmp_path, monkeypatch):
    path, backup, _, storage, warnings = setup_migration(reminder_main, tmp_path, monkeypatch)
    cfg = legacy_config(reminder_main)
    original = seed(path, cfg)
    descriptors = []
    def reject(fd, *args, **kwargs):
        descriptors.append(fd)
        raise OSError("test fdopen denied")
    monkeypatch.setattr(storage.os, "fdopen", reject)
    migrate(reminder_main, cfg)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        storage.os.fstat(descriptors[0])
    assert path.read_bytes() == backup.read_bytes() == original
    assert list(path.parent.glob("*.tmp")) == []
    assert len(warnings) == 1
