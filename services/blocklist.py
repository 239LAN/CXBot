"""用户黑名单：被拉黑的用户不能继续使用机器人。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from services.admins import normalize_identifier

logger = logging.getLogger(__name__)

BLOCK_FILE_VERSION = 1


def load_blocks(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("黑名单文件解析失败：%s", path, exc_info=True)
        return []

    if isinstance(raw, dict):
        raw = raw.get("blocked") or []
    if not isinstance(raw, list):
        return []

    result: list[str] = []
    for item in raw:
        identifier = normalize_identifier(str(item or ""))
        if identifier and identifier not in result:
            result.append(identifier)
    return result


def save_blocks(path: Path, blocked: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": BLOCK_FILE_VERSION, "blocked": blocked}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class BlockStore:
    """按文件修改时间自动刷新的黑名单。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._ids: set[str] = set()
        self._mtime: int | None = None

    def _refresh(self) -> None:
        try:
            mtime = self._path.stat().st_mtime_ns
        except OSError:
            self._ids = set()
            self._mtime = None
            return
        if mtime == self._mtime:
            return
        self._ids = set(load_blocks(self._path))
        self._mtime = mtime

    def is_blocked(self, identifier: str) -> bool:
        self._refresh()
        return normalize_identifier(identifier) in self._ids

    def list(self) -> list[str]:
        self._refresh()
        return sorted(self._ids)

    def add(self, identifier: str) -> bool:
        target = normalize_identifier(identifier)
        if not target:
            return False
        self._refresh()
        if target in self._ids:
            return False
        self._ids.add(target)
        save_blocks(self._path, sorted(self._ids))
        self._mtime = self._path.stat().st_mtime_ns
        return True

    def remove(self, identifier: str) -> bool:
        target = normalize_identifier(identifier)
        if not target:
            return False
        self._refresh()
        if target not in self._ids:
            return False
        self._ids.remove(target)
        save_blocks(self._path, sorted(self._ids))
        self._mtime = self._path.stat().st_mtime_ns
        return True
