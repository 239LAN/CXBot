"""管理员名单：名单内的 QQ 用户不受额度限制。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ADMIN_FILE_VERSION = 1


def normalize_identifier(value: str) -> str:
    """去掉 c2c:/group: 前缀，只保留用户标识本身。"""
    text = (value or "").strip()
    for prefix in ("c2c:", "group:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text


def load_admins(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("管理员文件解析失败：%s", path, exc_info=True)
        return []

    if isinstance(raw, dict):
        raw = raw.get("admins") or []
    if not isinstance(raw, list):
        return []

    result: list[str] = []
    for item in raw:
        identifier = normalize_identifier(str(item or ""))
        if identifier and identifier not in result:
            result.append(identifier)
    return result


def save_admins(path: Path, admins: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": ADMIN_FILE_VERSION, "admins": admins}
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        logger.exception("管理员文件写入失败：%s", path)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_admins(path: Path, identifiers: list[str]) -> list[str]:
    """添加管理员，返回本次新增的标识。"""
    admins = load_admins(path)
    added: list[str] = []
    for raw in identifiers:
        identifier = normalize_identifier(raw)
        if not identifier or identifier in admins:
            continue
        admins.append(identifier)
        added.append(identifier)
    if added:
        save_admins(path, admins)
    return added


def remove_admins(path: Path, identifiers: list[str]) -> list[str]:
    """移除管理员，返回本次实际移除的标识。"""
    admins = load_admins(path)
    removed: list[str] = []
    for raw in identifiers:
        identifier = normalize_identifier(raw)
        if identifier and identifier in admins:
            admins.remove(identifier)
            removed.append(identifier)
    if removed:
        save_admins(path, admins)
    return removed


class AdminStore:
    """只读管理员名单，按文件修改时间自动刷新，供机器人进程使用。"""

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
        self._ids = set(load_admins(self._path))
        self._mtime = mtime

    def is_admin(self, identifier: str) -> bool:
        self._refresh()
        return normalize_identifier(identifier) in self._ids
