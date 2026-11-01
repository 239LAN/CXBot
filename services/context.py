"""按会话持久化多轮对话上下文（JSON 文件，重启不丢）。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ContextStore:
    def __init__(self, directory: Path, max_turns: int) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_messages = max(1, max_turns) * 2

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    def load(self, key: str) -> list[dict]:
        path = self._path(key)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("上下文文件损坏，已忽略：%s", path, exc_info=True)
            return []

        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return []
        return [m for m in messages if isinstance(m, dict) and "role" in m and "content" in m]

    def append_turn(self, key: str, user_content: str, assistant_content: str) -> None:
        messages = self.load(key)
        messages.append({"role": "user", "content": user_content})
        messages.append({"role": "assistant", "content": assistant_content})
        messages = messages[-self._max_messages :]

        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"key": key, "messages": messages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def reset(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
