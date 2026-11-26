"""把 QQ 网关推送的原始事件整理成统一的 MessageEvent。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 群聊 @ 消息会带 <@!xxx> 或 <@xxx> 前缀
_MENTION_RE = re.compile(r"<@[!]?[^>]+>")


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_AUDIO_SUFFIXES = (
    ".amr",
    ".silk",
    ".slk",
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
)
_MEDIA_PREFIXES = ("video/",)


def _is_image_attachment(item: dict) -> bool:
    content_type = str(item.get("content_type") or "").lower()
    if content_type == "image" or content_type.startswith("image/"):
        return True
    filename = str(item.get("filename") or "").lower()
    return filename.endswith(_IMAGE_SUFFIXES)


def _is_voice_attachment(item: dict) -> bool:
    content_type = str(item.get("content_type") or "").lower()
    if content_type == "voice" or content_type.startswith("audio/"):
        return True
    filename = str(item.get("filename") or "").lower()
    return filename.endswith(_AUDIO_SUFFIXES)


def _is_document_attachment(item: dict) -> bool:
    if not item.get("url"):
        return False
    if _is_image_attachment(item) or _is_voice_attachment(item):
        return False
    content_type = str(item.get("content_type") or "").lower()
    return not content_type.startswith(_MEDIA_PREFIXES)


def attachment_kind(item: dict) -> str:
    """返回附件类型，用于提示模型和展示。"""
    if _is_image_attachment(item):
        return "图片"
    if _is_voice_attachment(item):
        return "语音"
    return "文件"


@dataclass
class MessageEvent:
    kind: str  # "group" | "c2c"
    msg_id: str
    content: str
    user_openid: str
    group_openid: str | None = None
    group_role: str = ""  # 群内角色：owner / admin / member，非群聊为空
    attachments: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def context_key(self) -> str:
        target = self.group_openid if self.kind == "group" else self.user_openid
        return f"{self.kind}:{target}"

    @property
    def image_attachments(self) -> list[dict]:
        return [
            item
            for item in self.attachments
            if item.get("url") and _is_image_attachment(item)
        ]

    @property
    def document_attachments(self) -> list[dict]:
        return [item for item in self.attachments if _is_document_attachment(item)]

    @property
    def voice_attachments(self) -> list[dict]:
        return [
            item
            for item in self.attachments
            if item.get("url") or item.get("voice_wav_url")
            if _is_voice_attachment(item)
        ]


def _clean_content(content: str) -> str:
    return _MENTION_RE.sub("", content or "").strip()


def _parse_attachments(data: dict) -> list[dict]:
    items = data.get("attachments")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def split_text(text: str, limit: int, max_chunks: int = 5) -> list[str]:
    """按长度分条，优先在换行/句末断开，超出上限时截断。"""
    text = (text or "").strip()
    if not text or limit <= 0:
        return []

    chunks: list[str] = []
    remaining = text
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= limit:
            chunks.append(remaining)
            remaining = ""
            break

        newline = remaining.rfind("\n", 0, limit)
        punctuation = max(remaining.rfind(mark, 0, limit) for mark in "。！？；")
        if newline >= limit // 2:
            cut = newline
        elif punctuation >= limit // 2:
            cut = punctuation + 1
        else:
            cut = limit

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining and chunks:
        chunks[-1] = chunks[-1][: max(1, limit - 1)].rstrip() + "…"
    return [chunk for chunk in chunks if chunk]


def parse_message_event(event_type: str, data: dict) -> MessageEvent | None:
    author = data.get("author") or {}

    if event_type == "GROUP_AT_MESSAGE_CREATE":
        msg_id = str(data.get("id") or "")
        group_openid = data.get("group_openid")
        user_openid = str(author.get("member_openid") or author.get("id") or "")
        if not (msg_id and group_openid and user_openid):
            logger.warning("群消息字段不完整，已忽略：%s", data)
            return None
        content = _clean_content(data.get("content", ""))
        attachments = _parse_attachments(data)
        if not content and not attachments:
            return None
        return MessageEvent(
            kind="group",
            msg_id=msg_id,
            content=content,
            user_openid=user_openid,
            group_openid=group_openid,
            group_role=str(author.get("member_role") or "").lower(),
            attachments=attachments,
            raw=data,
        )

    if event_type == "C2C_MESSAGE_CREATE":
        msg_id = str(data.get("id") or "")
        user_openid = str(author.get("user_openid") or author.get("id") or "")
        if not (msg_id and user_openid):
            logger.warning("单聊消息字段不完整，已忽略：%s", data)
            return None
        content = _clean_content(data.get("content", ""))
        attachments = _parse_attachments(data)
        if not content and not attachments:
            return None
        return MessageEvent(
            kind="c2c",
            msg_id=msg_id,
            content=content,
            user_openid=user_openid,
            attachments=attachments,
            raw=data,
        )

    return None
