"""JSON-backed reminder storage with atomic file replacement."""

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Dict, List

from core.plugin import logger


class ReminderStorage:
    """Serialize reminder data with an asyncio lock and atomic writes."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _unsafe_load(self) -> Dict[str, List[Dict]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[Reminder] 加载数据失败: {e}")
            return {}

    def _unsafe_save(self, data: Dict[str, List[Dict]]):
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
                    temp_file.write(content)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(tmp_path, self.path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error(f"[Reminder] 保存数据失败: {e}")
            raise

    async def load(self) -> Dict[str, List[Dict]]:
        async with self._lock:
            return self._unsafe_load()

    async def save(self, data: Dict[str, List[Dict]]):
        async with self._lock:
            self._unsafe_save(data)

    @asynccontextmanager
    async def modify(self) -> AsyncGenerator[Dict[str, List[Dict]], None]:
        """Provide a locked read-modify-write transaction."""
        async with self._lock:
            data = self._unsafe_load()
            yield data
            self._unsafe_save(data)
