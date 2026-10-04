"""JSON-backed reminder storage with atomic file replacement."""

import asyncio
import copy
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Dict, List

from core.plugin import logger


class ReminderStorageError(RuntimeError):
    """Raised when persisted data cannot be read or validated safely."""


def write_json_atomic(path: Path, data: dict, *, indent: int = 2) -> None:
    """Replace JSON after a complete write; callers own validation and locking."""
    content = json.dumps(data, ensure_ascii=False, indent=indent)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        temp_file = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        fd = None
        with temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(tmp_path, path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


class ReminderStorage:
    """Serialize reminder data with an asyncio lock and atomic writes."""

    def __init__(self, path: Path, *, validator: Callable[[dict], None] | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._validator = validator

    def _unsafe_load(self) -> Dict[str, List[Dict]]:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as e:
            logger.error(f"[Reminder] 加载数据失败: {e}")
            raise ReminderStorageError(f"Cannot read {self.path.name}") from e

        try:
            content = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            logger.error(f"[Reminder] 加载数据失败: {e}")
            raise ReminderStorageError(f"Cannot read {self.path.name}") from e

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"[Reminder] 加载数据失败: {e}")
            raise ReminderStorageError(f"Invalid JSON in {self.path.name}") from e
        if not isinstance(data, dict):
            logger.error(f"[Reminder] 数据文件顶层必须是 JSON 对象: {self.path.name}")
            raise ReminderStorageError(f"Invalid data shape in {self.path.name}")
        if self._validator is not None:
            try:
                self._validator(data)
            except ReminderStorageError as e:
                logger.error(f"[Reminder] 数据结构校验失败 {self.path.name}: {e}")
                raise
        return data

    def _unsafe_save(self, data: Dict[str, List[Dict]]):
        try:
            if self._validator is not None:
                self._validator(data)
            write_json_atomic(self.path, data)
        except Exception as e:
            logger.error(f"[Reminder] 保存数据失败: {e}")
            raise

    async def load(self) -> Dict[str, List[Dict]]:
        async with self._lock:
            return self._unsafe_load()

    async def save(self, data: Dict[str, List[Dict]]):
        async with self._lock:
            self._unsafe_load()
            self._unsafe_save(data)

    @asynccontextmanager
    async def read(self) -> AsyncGenerator[Dict[str, List[Dict]], None]:
        """Hold a read lock while validating a related ledger transaction."""
        async with self._lock:
            yield self._unsafe_load()

    @asynccontextmanager
    async def modify(
        self, *, after_save: Callable[[dict, dict], None] | None = None,
    ) -> AsyncGenerator[Dict[str, List[Dict]], None]:
        """Commit data before synchronous effects, compensating a rejected commit."""
        async with self._lock:
            data = self._unsafe_load()
            original = copy.deepcopy(data) if after_save is not None else None
            yield data
            self._unsafe_save(data)
            if after_save is not None:
                try:
                    after_save(original, data)
                except Exception as error:
                    try:
                        self._unsafe_save(original)
                    except Exception as rollback_error:
                        logger.error(f"[Reminder] Data compensation failed: {rollback_error}")
                        raise ReminderStorageError(
                            "调度更新失败，数据回滚也失败，请检查数据和日志后重载插件"
                        ) from error
                    raise
