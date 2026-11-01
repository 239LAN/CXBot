"""一次性额度重置码：管理员用脚本生成，用户在聊天中用 /redeem 兑换。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 I/O/0/1
CODE_CHARS = 12
MAX_GENERATE = 200


class ResetCodeError(RuntimeError):
    pass


def normalize(code: str) -> str:
    return "".join(code.split()).replace("-", "").upper()


def _hash(code: str) -> str:
    return hashlib.sha256(normalize(code).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(CST)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("codes"), list):
            return data
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logger.warning("重置码文件读取失败：%s", path, exc_info=True)
    return {"version": 1, "codes": []}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def _make_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_CHARS))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}"


def generate_codes(
    path: Path, count: int = 1, expires_days: int | None = 30
) -> list[str]:
    """生成一次性重置码并写入库存文件，返回明文码（仅此一次可见）。"""
    if not 1 <= count <= MAX_GENERATE:
        raise ResetCodeError(f"数量必须在 1-{MAX_GENERATE} 之间")

    data = _load(path)
    codes = [entry for entry in data["codes"] if isinstance(entry, dict)]
    known = {entry.get("hash") for entry in codes}
    now = _now()
    expires_at = (
        (now + timedelta(days=expires_days)).isoformat(timespec="seconds")
        if expires_days and expires_days > 0
        else None
    )

    generated: list[str] = []
    while len(generated) < count:
        code = _make_code()
        digest = _hash(code)
        if digest in known:
            continue
        known.add(digest)
        codes.append(
            {
                "hash": digest,
                "created_at": now.isoformat(timespec="seconds"),
                "expires_at": expires_at,
                "used_by": None,
                "used_at": None,
            }
        )
        generated.append(code)

    data["codes"] = codes
    _save(path, data)
    return generated


class ResetCodeStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._data = _load(path)

    async def redeem(self, code: str, scope: str) -> tuple[bool, str]:
        """核销重置码并绑定到当前会话，返回 (是否成功, 失败原因)。"""
        digest = _hash(code)
        async with self._lock:
            # 每次核销都重新读取库存，管理员运行脚本新增的码无需重启即可生效
            self._data = _load(self._path)
            for entry in self._data["codes"]:
                if not isinstance(entry, dict) or entry.get("hash") != digest:
                    continue
                if entry.get("used_by"):
                    return False, "该重置码已被使用"
                expires_at = entry.get("expires_at")
                if expires_at:
                    try:
                        expiry = datetime.fromisoformat(str(expires_at))
                    except ValueError:
                        expiry = None
                    if expiry is not None:
                        if expiry.tzinfo is None:
                            expiry = expiry.replace(tzinfo=CST)
                        if _now() > expiry:
                            return False, "该重置码已过期"
                entry["used_by"] = scope
                entry["used_at"] = _now().isoformat(timespec="seconds")
                self._save()
                return True, ""
            return False, "重置码无效"

    def _save(self) -> None:
        try:
            _save(self._path, self._data)
        except OSError:
            logger.exception("重置码文件写入失败：%s", self._path)
