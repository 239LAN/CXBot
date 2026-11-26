"""跨消息续传：保存因长度上限被截断的剩余文本，供 /继续 分块取出。"""

from __future__ import annotations

import time

from services.markdown import split_markdown

CONTINUE_TTL = 3600.0  # 1 小时


def split_at_limit(text: str, limit: int) -> tuple[str, str]:
    """在 limit 附近优先按换行/句末切开，保证两段首尾相接、不重复不缺失。"""
    if limit <= 0:
        return "", text
    if len(text) <= limit:
        return text, ""

    cut = limit
    floor = max(1, limit // 2)
    for mark in ("\n", "。", "！", "？", "；", "，", ".", "!", "?", ";", ","):
        pos = text.rfind(mark, floor, limit)
        if pos >= floor:
            cut = pos + 1
            break
    return text[:cut], text[cut:]


class ContinuationStore:
    """按会话保存待续传文本，1 小时内有效。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, float]] = {}

    def put(self, key: str, text: str) -> None:
        text = text or ""
        if not text:
            self._items.pop(key, None)
            return
        self._items[key] = (text, time.monotonic())

    def take(
        self, key: str, limit: int, markdown: bool = True
    ) -> tuple[str, str] | None:
        """取出下一段。返回 (本次内容, 剩余内容)；没有可续传内容时返回 None。"""
        item = self._items.get(key)
        if item is None:
            return None
        text, updated_at = item
        if time.monotonic() - updated_at > CONTINUE_TTL:
            self._items.pop(key, None)
            return None

        chunk, rest = split_markdown(text, limit) if markdown else split_at_limit(text, limit)
        if rest:
            self._items[key] = (rest, time.monotonic())
        else:
            self._items.pop(key, None)
        return chunk, rest

    def clear(self, key: str) -> None:
        self._items.pop(key, None)
