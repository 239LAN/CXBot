"""按会话保存的用户偏好（目前用于思考强度）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class PreferenceStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._data = self._load()

    async def get(self, scope: str, key: str) -> str | None:
        async with self._lock:
            value = (self._data.get(scope) or {}).get(key)
            return value if isinstance(value, str) else None

    async def set(self, scope: str, key: str, value: str | None) -> None:
        async with self._lock:
            entry = self._data.setdefault(scope, {})
            if value is None:
                entry.pop(key, None)
                if not entry:
                    self._data.pop(scope, None)
            else:
                entry[key] = value
            self._save()

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    str(scope): entry
                    for scope, entry in data.items()
                    if isinstance(entry, dict)
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("偏好文件读取失败：%s", self._path, exc_info=True)
        return {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("偏好文件写入失败：%s", self._path)
