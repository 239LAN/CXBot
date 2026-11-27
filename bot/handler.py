"""消息处理：调模型、执行工具、存上下文、回复消息。

上下文策略：
- 单聊（C2C）按用户隔离：c2c:{user_openid}
- 群聊按群共享：group:{group_openid}（同一群内所有成员共用一份上下文）

回复策略：
- 单聊默认使用流式消息（/v2/users/{openid}/stream_messages，可用 /stream off 关闭），
  并开放 50MB 私有文件空间工具与网页工具
- 群聊没有流式接口，使用普通文本分条回复，不开放文件工具，但支持网页搜索与抓取
- 用户发送图片时，下载后以 base64 data URL 作为多模态输入传给模型
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

try:
    from PIL import Image
except ImportError:  # 未安装 Pillow 时不做压缩
    Image = None

from bot.api import QQBotAPI, QQBotAPIError
from bot.commands import CommandRouter
from bot.events import MessageEvent, attachment_kind, split_text
from config import Config
from services.admins import AdminStore
from services.asr import ASRClient, ASRError
from services.blocklist import BlockStore
from services.console import ManualConsole
from services.context import ContextStore
from services.continuation import ContinuationStore
from services.deepseek import DeepSeekClient, LLMError, StreamState
from services.documents import DocumentError, extract_text
from services.markdown import close_fences, split_markdown_chunks, tail_after
from services.preferences import PreferenceStore
from services.quota import QuotaManager
from services.reset_codes import ResetCodeStore
from services.web import WebTools
from services.workspace import ToolError, WorkspaceTools

logger = logging.getLogger(__name__)

WEB_TOOL_NAMES = {"web_search", "web_fetch"}
ATTACHMENT_DOWNLOAD_TIMEOUT = 20.0

RESET_COMMANDS = {"/reset", "/clear", "重置上下文", "清空上下文"}
SETTINGS_COMMANDS = {
    "/progress",
    "/过程",
    "/markdown",
    "/md",
    "/渲染",
    "/thinking",
    "/思考",
    "/stream",
    "/流式",
    "/length",
    "/输出",
    "/长度",
    "/context",
    "/上下文",
}
MAX_CHUNKS = 5  # 群聊/回退消息被动回复同一个 msg_id 最多 5 条
CHUNK_INTERVAL = 0.25

STREAM_FLUSH_INTERVAL = 0.6  # 秒，避免每来一个 token 都调一次接口
STREAM_MAX_CHARS = 2000
STREAM_SAFE_CHARS = 1900  # 给 QQ 端与 Markdown 围栏留安全余量
# 流式消息首帧固定发送零宽空格：QQ 要求已下发内容前缀不可修改（40111001），
# 后续所有分片都必须是「STREAM_PREFIX + 已累积正文」的严格追加。
STREAM_PREFIX = "\u200b"
MAX_TOOL_ROUNDS = 8
MAX_TOOL_CALLS_PER_ROUND = 8
MAX_C2C_PASSIVE_REPLIES = 4  # 单聊同一条消息最多回复 4 次（含流式消息）
DIRECT_UPLOAD_MAX_BYTES = 5 * 1024 * 1024  # 超过后走分片上传
PENDING_UPLOAD_TTL = 600  # 只发附件后，等待提示词的最长时间（秒）
MAX_PENDING_ATTACHMENTS = 10
MAX_PROGRESS_CHARS = 400
HEARTBEAT_INTERVAL = 8.0  # 思考或工具执行期间维持流式连接
MAX_FULL_ANSWER_CHARS = 20000
CONTINUE_HINT = "\n\n……\n\n发送[/继续]继续"
CONTINUE_CHARS = 1800
PLAIN_TEXT_SUFFIX = (
    "当前会话已关闭 Markdown 渲染，回复内容尽量不使用 Markdown 格式，"
    "不要使用 # 标题、** 加粗、列表符号或表格，直接用纯文本自然表达。"
)
FALLBACK_REPLY = "抱歉，我这边暂时出了点问题，请稍后再试～"
TRUNCATED_TOOL_RESULT = (
    "本次工具调用参数因输出达到长度上限被截断，未执行。"
    "请缩短内容，分批调用 write_file/append_file 等工具。"
)


@dataclass
class PendingUploads:
    """用户只发附件、还没发提示词时暂存的内容。"""

    images: list[str] = field(default_factory=list)
    document_blocks: list[str] = field(default_factory=list)
    voice_texts: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    updated_at: float = 0.0


def _short(value: object, limit: int = 80) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_arg(arguments: str, key: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get(key) or "").strip()


@dataclass
class ProgressItem:
    label: str
    result: str = ""


@dataclass
class ProgressTracker:
    """汇总工具调用，用于流式过程展示。

    show_result=False 时只显示调用了什么；True 时附带结果摘要。
    """

    show_result: bool = False
    reads: list[ProgressItem] = field(default_factory=list)
    commands: list[ProgressItem] = field(default_factory=list)
    writes: list[ProgressItem] = field(default_factory=list)
    others: list[ProgressItem] = field(default_factory=list)
    _last: ProgressItem | None = field(default=None, repr=False)

    def record(self, name: str, arguments: str) -> None:
        path = _tool_arg(arguments, "path")
        if name == "read_file":
            item = ProgressItem(path or "文件")
            self.reads.append(item)
        elif name == "run_command":
            item = ProgressItem(_tool_arg(arguments, "command") or "命令")
            self.commands.append(item)
        elif name in {
            "write_file",
            "append_file",
            "edit_file",
            "replace_lines",
            "copy_file",
            "move_file",
            "delete_file",
            "mkdir",
        }:
            item = ProgressItem(path or name)
            self.writes.append(item)
        elif name == "list_files":
            item = ProgressItem("查看了文件列表")
            self.others.append(item)
        elif name == "workspace_usage":
            item = ProgressItem("查看了空间占用")
            self.others.append(item)
        elif name == "web_search":
            item = ProgressItem(
                "搜索了网页：" + _short(_tool_arg(arguments, "query"), 40)
            )
            self.others.append(item)
        elif name == "web_fetch":
            item = ProgressItem(
                "抓取了网页：" + _short(_tool_arg(arguments, "url"), 60)
            )
            self.others.append(item)
        elif name == "save_attachment":
            item = ProgressItem("保存了附件")
            self.others.append(item)
        elif name == "send_file":
            item = ProgressItem("发送了文件：" + _short(path, 40))
            self.others.append(item)
        else:
            item = ProgressItem("调用了 " + name)
            self.others.append(item)
        self._last = item

    def add_result(self, result: str) -> None:
        """记录上一次工具调用的结果摘要（仅在完整档位展示）。"""
        if self._last is not None and result:
            self._last.result = _short(result, 120)

    @property
    def last_label(self) -> str:
        """最近一次工具调用的简短描述。"""
        return self._last.label if self._last is not None else ""

    @staticmethod
    def _render_items(
        lines: list[str], items: list[ProgressItem], limit: int, show_result: bool
    ) -> None:
        for item in items[-limit:]:
            lines.append(f"> {_short(item.label, 80)}")
            if show_result and item.result:
                lines.append(f"↳ {item.result}")

    def render(self) -> str:
        lines: list[str] = []
        if self.reads:
            lines.append(f"【阅读了{len(self.reads)}个文件】")
            self._render_items(lines, self.reads, 3, self.show_result)
        if self.commands:
            lines.append(f"【执行了{len(self.commands)}条命令】")
            self._render_items(lines, self.commands, 3, self.show_result)
        if self.writes:
            lines.append(f"【修改了{len(self.writes)}个文件】")
            if self.show_result:
                self._render_items(lines, self.writes, 3, True)
        for item in self.others[-5:]:
            lines.append(f"【{item.label}】")
            if self.show_result and item.result:
                lines.append(f"↳ {item.result}")
        text = "\n".join(lines)
        if len(text) > MAX_PROGRESS_CHARS:
            text = text[: MAX_PROGRESS_CHARS - 1].rstrip() + "…"
        return text


def _sniff_image_mime(data: bytes, *candidates: str) -> str:
    """优先按文件头判断图片类型，拿不准时返回空字符串而不是猜 jpeg。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    for value in candidates:
        mime = str(value or "").split(";")[0].strip().lower()
        if mime in SUPPORTED_IMAGE_MIMES:
            return mime
    return ""


SUPPORTED_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/bmp",
}
IMAGE_SHRINK_BYTES = 1_200_000
IMAGE_MAX_SIDE = 1600


def _prepare_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """统一成模型支持的图片格式；体积过大时压缩成 JPEG。"""
    if mime == "image/jpg":
        mime = "image/jpeg"
    small_enough = len(data) <= IMAGE_SHRINK_BYTES
    if mime in SUPPORTED_IMAGE_MIMES and small_enough:
        return data, mime
    if Image is None:
        return (data, mime) if mime in SUPPORTED_IMAGE_MIMES else (b"", "")
    try:
        with Image.open(io.BytesIO(data)) as img:
            converted = img.convert("RGB")
            converted.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            buffer = io.BytesIO()
            converted.save(buffer, format="JPEG", quality=85, optimize=True)
            result = buffer.getvalue()
        if not result:
            return b"", ""
        if mime in SUPPORTED_IMAGE_MIMES and len(result) >= len(data):
            return data, mime
        return result, "image/jpeg"
    except Exception:
        logger.warning("图片转码失败，已跳过", exc_info=True)
        return b"", ""

SEND_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_file",
        "description": (
            "把私有空间中的文件直接发送给当前用户（单个文件最大 200MB，超过 5MB 会自动分片上传）。"
            "发送后用户会收到文件卡片；每次发送会占用一次被动回复次数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要发送的文件相对路径"}
            },
            "required": ["path"],
        },
    },
}

SAVE_ATTACHMENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "save_attachment",
        "description": (
            "把当前可处理附件（文档、图片、语音等，含刚上传的附件）下载到私有空间。"
            "index 从 1 开始，对应提示中的【可处理附件】列表；"
            "path 省略时使用原文件名，overwrite=true 可覆盖同名文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "附件序号，从 1 开始"},
                "path": {
                    "type": "string",
                    "description": "保存到私有空间的相对路径，可省略",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "同名文件是否覆盖，默认 false",
                },
            },
            "required": ["index"],
        },
    },
}


class ChatHandler:
    def __init__(
        self,
        config: Config,
        api: QQBotAPI,
        llm: DeepSeekClient,
        contexts: ContextStore,
        workspace: WorkspaceTools,
        quota: QuotaManager,
        preferences: PreferenceStore,
        reset_codes: ResetCodeStore,
        admins: AdminStore,
        web: WebTools,
        asr: ASRClient,
        blocks: BlockStore | None = None,
        console: ManualConsole | None = None,
    ) -> None:
        self._config = config
        self._api = api
        self._llm = llm
        self._contexts = contexts
        self._workspace = workspace
        self._quota = quota
        self._preferences = preferences
        self._reset_codes = reset_codes
        self._admins = admins
        self._web = web
        self._asr = asr
        self._blocks = blocks or BlockStore(config.block_file)
        self._console = console
        self._continuations = ContinuationStore()
        self._needs_continue: set[str] = set()
        self._commands = CommandRouter(
            config,
            quota,
            preferences,
            reset_codes,
            admins,
            workspace,
            self._blocks,
        )
        self._pending_uploads: dict[str, PendingUploads] = {}

    async def handle(self, event: MessageEvent) -> None:
        key = event.context_key
        text = event.content

        # 不串行化整个处理流程：沙箱任务由自身并发槽位排队，
        # 一个耗时任务不会阻塞其他消息的回复。
        if self._blocks.is_blocked(event.user_openid) and not self._admins.is_admin(
            event.user_openid
        ):
            await self._reply(event, "你已被限制使用本服务。")
            return

        if text.lower() in ("/continue", "/继续"):
            # 预留一条消息给“继续”提示，避免分条发送时提示被截断
            chunk_limit = max(
                200,
                min(CONTINUE_CHARS, max(200, self._config.max_reply_chars * 3))
                - len(CONTINUE_HINT)
                - 16,
            )
            markdown_on = await self._preferences.get(key, "markdown") != "off"
            item = self._continuations.take(key, chunk_limit, markdown=markdown_on)
            if item is None:
                if key in getattr(self, "_needs_continue", set()):
                    await self._continue_generation(event, key)
                    return
                await self._reply(
                    event, "没有需要继续的内容，或已超过 1 小时。"
                )
                return
            chunk, rest = item
            content = chunk + (CONTINUE_HINT if rest else "")
            await self._reply(event, content, markdown=markdown_on)
            return

        if text.lower() in RESET_COMMANDS:
            self._contexts.reset(key)
            self._pending_uploads.pop(key, None)
            self._continuations.clear(key)
            self._mark_needs_continue(key, False)
            logger.info("清空上下文：%s", key)
            await self._reply(event, "上下文已清空，我们重新开始吧。")
            return

        if text.startswith("/"):
            command = text.split()[0].lower()
            console = getattr(self, "_console", None)
            if command in ("/console", "/控制台", "/验证码"):
                if console is None:
                    reply = "网页控制台未启用（CONSOLE_ENABLED=off）。"
                elif not self._admins.is_admin(event.user_openid):
                    reply = "仅 Bot 管理员可以获取网页控制台验证码。"
                else:
                    reply = console.code_text()
                await self._reply(event, reply)
                return
            # 群@消息不保证带 member_role，改设置前补查一次群成员角色
            if (
                event.kind == "group"
                and not event.group_role
                and command in SETTINGS_COMMANDS
            ):
                event.group_role = await self._api.group_member_role(
                    event.group_openid or "", event.user_openid
                )
            reply = await self._commands.route(event, text)
            await self._reply(event, reply)
            return

        console = getattr(self, "_console", None)
        self._mark_needs_continue(key, False)
        if console is not None and await console.submit(event):
            # 人工接管：消息已进入网页控制台，等待人工回复
            logger.info("会话处于人工接管，跳过 AI 回复：%s", key)
            return

        if not text and (event.image_attachments or event.document_attachments):
            await self._handle_attachment_only(event, key)
            return

        unlimited = self._admins.is_admin(event.user_openid)
        output_limit = await self._output_limit(key)
        context_limit = await self._context_limit(key, unlimited)
        if unlimited:
            remaining = output_limit
            logger.info("管理员不限额度：%s", event.user_openid)
        else:
            remaining, reason = await self._quota.check(key)
            if reason:
                logger.info("额度不足，拒绝请求 %s：%s", key, reason)
                await self._reply(event, reason)
                return
            remaining = min(remaining, output_limit)

        images = await self._load_images(event)
        doc_blocks, doc_errors = await self._load_documents(event)
        voice_texts, voice_errors = await self._load_voices(event)

        images, doc_blocks, voice_texts = self._merge_pending_uploads(
            key, event, images, doc_blocks, voice_texts
        )

        if not text and not images and not doc_blocks and not voice_texts:
            if event.voice_attachments:
                reason = (
                    voice_errors[0]
                    if voice_errors
                    else "语音识别失败，请重试或直接发文字。"
                )
                await self._reply(event, reason)
            elif event.document_attachments:
                reason = doc_errors[0] if doc_errors else "未提取到文本"
                await self._reply(event, f"文档解析失败：{reason}")
            elif event.image_attachments:
                await self._reply(event, "图片接收失败，请重新发送。")
            else:
                await self._reply(event, "消息为空。")
            return

        if text:
            prompt_text = text
        elif doc_blocks and images:
            prompt_text = "请阅读我发送的文档和图片。"
        elif doc_blocks:
            prompt_text = "请阅读并总结我发送的文档。"
        elif images:
            prompt_text = "请描述这张图片。"
        else:
            prompt_text = ""
        if voice_texts:
            transcripts = "\n".join(f"[语音]{item}" for item in voice_texts)
            prompt_text = (
                f"{prompt_text}\n\n{transcripts}" if prompt_text else transcripts
            )
        if doc_blocks:
            prompt_text = f"{prompt_text}\n\n" + "\n\n".join(doc_blocks)
        attachment_note = self._attachment_note(event)
        if attachment_note:
            prompt_text = (
                f"{prompt_text}\n\n{attachment_note}"
                if prompt_text
                else attachment_note
            )

        messages = self._contexts.load(key, max_tokens=context_limit) + [
            {"role": "user", "content": self._user_content(prompt_text, images)}
        ]
        history_text = self._history_text(
            text, len(images), len(doc_blocks), voice_texts
        )

        thinking = await self._preferences.get(key, "thinking")
        thinking_enabled, reasoning_effort = self._thinking_params(thinking)
        stream_off = await self._preferences.get(key, "stream") == "off"
        progress_mode = self._progress_mode(
            await self._preferences.get(key, "progress")
        )
        thinking_display = (
            await self._preferences.get(key, "reasoning") == "on"
        )
        markdown_on = await self._preferences.get(key, "markdown") != "off"

        if event.kind == "c2c":
            c2c_tools = (
                self._workspace.schemas()
                + self._web.schemas()
                + [SEND_FILE_SCHEMA, SAVE_ATTACHMENT_SCHEMA]
            )
            if stream_off:
                # 关闭流式时仍保留私有空间与发送文件工具，并预留一条文本回复
                try:
                    await self._text_reply(
                        event,
                        messages,
                        key,
                        remaining,
                        thinking_enabled,
                        reasoning_effort,
                        history_text,
                        tools=c2c_tools,
                        reply_seq={"next": 1, "reserve": 1},
                        unlimited=unlimited,
                        output_limit=output_limit,
                        context_tokens=context_limit,
                        markdown=markdown_on,
                    )
                finally:
                    if console is not None:
                        await console.clear_live(key)
                return
            try:
                answer, ok = await self._stream_reply(
                    event,
                    messages,
                    remaining,
                    thinking_enabled,
                    reasoning_effort,
                    unlimited=unlimited,
                    output_limit=output_limit,
                    context_tokens=context_limit,
                    progress_mode=progress_mode,
                    thinking_display=thinking_display,
                    markdown_enabled=markdown_on,
                )
            except QQBotAPIError:
                logger.exception("流式回复不可用，回退普通消息")
                await self._text_reply(
                    event,
                    messages,
                    key,
                    remaining,
                    thinking_enabled,
                    reasoning_effort,
                    history_text,
                    tools=c2c_tools,
                    reply_seq={"next": 1, "reserve": 1},
                    unlimited=unlimited,
                    output_limit=output_limit,
                    context_tokens=context_limit,
                    markdown=markdown_on,
                )
                return
            finally:
                if console is not None:
                    await console.clear_live(key)
            if ok and answer:
                self._contexts.append_turn(
                    key, history_text, answer, max_tokens=context_limit
                )
            return

        # 群聊不支持流式，走普通分条回复；工作空间与沙箱按群共享
        group_tools = (
            self._workspace.schemas()
            + self._web.schemas()
            + [SAVE_ATTACHMENT_SCHEMA]
        )
        try:
            await self._text_reply(
                event,
                messages,
                key,
                remaining,
                thinking_enabled,
                reasoning_effort,
                history_text,
                tools=group_tools,
                unlimited=unlimited,
                output_limit=output_limit,
                context_tokens=context_limit,
                markdown=markdown_on,
            )
        finally:
            if console is not None:
                await console.clear_live(key)

    def _mark_needs_continue(self, key: str, needed: bool) -> None:
        store = getattr(self, "_needs_continue", None)
        if store is None:
            store = set()
            self._needs_continue = store
        if needed:
            store.add(key)
        else:
            store.discard(key)

    async def _continue_generation(self, event: MessageEvent, key: str) -> None:
        """没有存量续传文本时，让模型从上次被截断的位置继续生成。"""
        unlimited = self._admins.is_admin(event.user_openid)
        if unlimited:
            remaining = await self._output_limit(key)
        else:
            remaining, reason = await self._quota.check(key)
            if reason:
                await self._reply(event, reason)
                return
            remaining = min(remaining, await self._output_limit(key))
        context_limit = await self._context_limit(key, unlimited)
        history = self._contexts.load(key, max_tokens=context_limit)
        if not history:
            self._mark_needs_continue(key, False)
            await self._reply(event, "没有可以继续的内容。")
            return

        prompt = (
            "请从你上一条回复被截断的位置继续写，不要重复已发送的内容，"
            "也不要重新开头。"
        )
        messages = history + [{"role": "user", "content": prompt}]
        thinking = await self._preferences.get(key, "thinking")
        thinking_enabled, reasoning_effort = self._thinking_params(thinking)
        progress_mode = self._progress_mode(
            await self._preferences.get(key, "progress")
        )
        thinking_display = await self._preferences.get(key, "reasoning") == "on"
        markdown_on = await self._preferences.get(key, "markdown") != "off"
        self._mark_needs_continue(key, False)
        console = getattr(self, "_console", None)

        if event.kind == "c2c":
            tools = (
                self._workspace.schemas()
                + self._web.schemas()
                + [SEND_FILE_SCHEMA, SAVE_ATTACHMENT_SCHEMA]
            )
            try:
                answer, ok = await self._stream_reply(
                    event,
                    messages,
                    remaining,
                    thinking_enabled,
                    reasoning_effort,
                    unlimited=unlimited,
                    output_limit=remaining,
                    context_tokens=context_limit,
                    progress_mode=progress_mode,
                    thinking_display=thinking_display,
                    markdown_enabled=markdown_on,
                )
            except QQBotAPIError:
                await self._text_reply(
                    event,
                    messages,
                    key,
                    remaining,
                    thinking_enabled,
                    reasoning_effort,
                    "（继续）",
                    tools=tools,
                    reply_seq={"next": 1, "reserve": 1},
                    unlimited=unlimited,
                    output_limit=remaining,
                    context_tokens=context_limit,
                    markdown=markdown_on,
                )
                return
            finally:
                if console is not None:
                    await console.clear_live(key)
            if ok and answer:
                self._contexts.append_turn(
                    key, "（继续）", answer, max_tokens=context_limit
                )
            return

        group_tools = (
            self._workspace.schemas()
            + self._web.schemas()
            + [SAVE_ATTACHMENT_SCHEMA]
        )
        try:
            await self._text_reply(
                event,
                messages,
                key,
                remaining,
                thinking_enabled,
                reasoning_effort,
                "（继续）",
                tools=group_tools,
                unlimited=unlimited,
                output_limit=remaining,
                context_tokens=context_limit,
                markdown=markdown_on,
            )
        finally:
            if console is not None:
                await console.clear_live(key)

    async def _download_attachment(
        self, url: str, max_bytes: int
    ) -> tuple[bytes, str] | None:
        try:
            timeout = aiohttp.ClientTimeout(total=ATTACHMENT_DOWNLOAD_TIMEOUT)
            # QQ 富媒体 CDN 对默认 UA 偶发 403，带上浏览器 UA 更稳
            headers = {"User-Agent": "Mozilla/5.0 (compatible; CXBot/1.0)"}
            async with self._api.session.get(
                url, timeout=timeout, headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.warning("附件下载失败 HTTP %s：%s", resp.status, url)
                    return None
                data = await resp.content.read(max_bytes + 1)
                content_type = resp.headers.get("Content-Type", "")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.warning("附件下载异常：%s", url, exc_info=True)
            return None

        if len(data) > max_bytes:
            logger.warning("附件超过大小上限，已跳过：%s", url)
            return None
        return data, content_type

    async def _load_images(self, event: MessageEvent) -> list[str]:
        """下载消息中的图片并转成 data URL，供多模态模型直接读取。"""
        attachments = event.image_attachments[: self._config.image_max_count]
        images: list[str] = []
        for item in attachments:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            downloaded = await self._download_attachment(
                url, self._config.image_max_bytes
            )
            if downloaded is None:
                continue
            data, content_type = downloaded
            mime = _sniff_image_mime(
                data, content_type, str(item.get("content_type") or "")
            )
            data, mime = await asyncio.to_thread(_prepare_image, data, mime)
            if not mime or not data:
                logger.warning("附件不是可识别的图片，已跳过：%s", url)
                continue
            encoded = base64.b64encode(data).decode("ascii")
            images.append(f"data:{mime};base64,{encoded}")
        return images

    async def _load_documents(
        self, event: MessageEvent
    ) -> tuple[list[str], list[str]]:
        """下载并提取文档文本，返回 (文档块, 错误信息)。"""
        attachments = event.document_attachments[: self._config.document_max_count]
        blocks: list[str] = []
        errors: list[str] = []
        remaining = self._config.document_max_chars

        for item in attachments:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            filename = Path(str(item.get("filename") or "document")).name or "document"
            downloaded = await self._download_attachment(
                url, self._config.document_max_bytes
            )
            if downloaded is None:
                errors.append(f"{filename} 下载失败")
                continue

            data, header_type = downloaded
            try:
                text = await asyncio.to_thread(
                    extract_text,
                    filename,
                    data,
                    str(item.get("content_type") or header_type or ""),
                )
            except DocumentError as exc:
                errors.append(f"{filename}：{exc}")
                continue
            except Exception:
                logger.exception("文档解析异常：%s", filename)
                errors.append(f"{filename} 解析失败")
                continue

            text = text.strip()
            if not text:
                errors.append(f"{filename} 未提取到文本")
                continue
            if len(text) > remaining:
                text = text[:remaining].rstrip() + "\n…（文档内容已截断）"
            blocks.append(f"【文档：{filename}】\n{text}")
            remaining -= len(text)
            if remaining <= 0:
                break

        return blocks, errors

    async def _load_voices(
        self, event: MessageEvent
    ) -> tuple[list[str], list[str]]:
        """优先使用 QQ 自带 ASR 文本，否则下载音频并调用语音识别。"""
        attachments = event.voice_attachments[: self._config.voice_max_count]
        texts: list[str] = []
        errors: list[str] = []
        remaining = self._config.asr_max_chars

        for item in attachments:
            refer = str(item.get("asr_refer_text") or "").strip()
            if refer and self._config.use_qq_asr_refer:
                text = refer
            elif not self._asr.enabled:
                if self._config.asr_enabled:
                    errors.append("已收到语音，但未配置语音识别密钥，请直接发文字。")
                else:
                    errors.append("已收到语音，但第三方语音识别已关闭，请直接发文字。")
                continue
            else:
                try:
                    size = int(item.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                if size > self._config.voice_max_bytes:
                    errors.append("语音文件过大，请缩短后重试")
                    continue

                urls: list[str] = []
                for key in ("voice_wav_url", "url"):
                    value = str(item.get(key) or "").strip()
                    if value and value not in urls:
                        urls.append(value)
                if not urls:
                    errors.append("语音下载地址为空")
                    continue

                text = ""
                for url in urls:
                    try:
                        text = await self._asr.transcribe(url)
                        break
                    except ASRError as exc:
                        logger.info("语音识别失败：%s", exc)
                if not text:
                    errors.append("语音识别失败，请重试或直接发文字。")
                    continue

            text = text.strip()
            if not text:
                errors.append("未识别到语音内容")
                continue
            if len(text) > remaining:
                text = text[:remaining].rstrip() + "…"
            texts.append(text)
            remaining -= len(text)
            if remaining <= 0:
                break

        return texts, errors

    async def _handle_attachment_only(self, event: MessageEvent, key: str) -> None:
        """只发文件/图片时先提示上传成功，内容暂存到下一次提示词。"""
        count = len(event.image_attachments) + len(event.document_attachments)
        await self._reply(
            event, f"【{count}个文件上传成功，请继续上传或发送提示词】"
        )

        images = await self._load_images(event)
        doc_blocks, _ = await self._load_documents(event)
        voice_texts, _ = await self._load_voices(event)
        self._add_pending_uploads(
            key,
            images=images,
            document_blocks=doc_blocks,
            voice_texts=voice_texts,
            attachments=event.attachments,
        )
        logger.info("暂存附件：%s，%d 个", key, count)

    def _add_pending_uploads(
        self,
        key: str,
        *,
        images: list[str],
        document_blocks: list[str],
        voice_texts: list[str],
        attachments: list[dict],
    ) -> None:
        pending = self._pending_uploads.get(key)
        if pending is None or time.monotonic() - pending.updated_at > PENDING_UPLOAD_TTL:
            pending = PendingUploads()

        pending.images = (pending.images + images)[-self._config.image_max_count :]
        pending.document_blocks = (
            pending.document_blocks + document_blocks
        )[-self._config.document_max_count :]
        pending.voice_texts = (
            pending.voice_texts + voice_texts
        )[-self._config.voice_max_count :]
        pending.attachments = (
            pending.attachments + attachments
        )[-MAX_PENDING_ATTACHMENTS:]
        pending.updated_at = time.monotonic()
        self._pending_uploads[key] = pending

    def _take_pending_uploads(self, key: str) -> PendingUploads | None:
        pending = self._pending_uploads.pop(key, None)
        if pending is None:
            return None
        if time.monotonic() - pending.updated_at > PENDING_UPLOAD_TTL:
            return None
        return pending

    def _merge_pending_uploads(
        self,
        key: str,
        event: MessageEvent,
        images: list[str],
        doc_blocks: list[str],
        voice_texts: list[str],
    ) -> tuple[list[str], list[str], list[str]]:
        pending = self._take_pending_uploads(key)
        if pending is None:
            return images, doc_blocks, voice_texts

        images = (pending.images + images)[-self._config.image_max_count :]
        doc_blocks = self._limit_document_blocks(
            (pending.document_blocks + doc_blocks)[
                -self._config.document_max_count :
            ],
            self._config.document_max_chars,
        )
        voice_texts = (pending.voice_texts + voice_texts)[
            -self._config.voice_max_count :
        ]
        event.attachments = pending.attachments + event.attachments
        return images, doc_blocks, voice_texts

    @staticmethod
    def _limit_document_blocks(blocks: list[str], limit: int) -> list[str]:
        result: list[str] = []
        remaining = limit
        for block in blocks:
            if remaining <= 0:
                break
            if len(block) > remaining:
                block = block[:remaining].rstrip() + "\n…（文档内容已截断）"
            result.append(block)
            remaining -= len(block)
        return result

    @staticmethod
    def _attachment_note(event: MessageEvent) -> str:
        if not event.attachments:
            return ""
        lines = ["【可处理附件】"]
        for index, item in enumerate(event.attachments[:10], 1):
            name = Path(str(item.get("filename") or "")).name
            if not name:
                name = f"attachment_{index}"
            size = item.get("size")
            try:
                size_text = f"，{int(size)} 字节" if size is not None else ""
            except (TypeError, ValueError):
                size_text = ""
            lines.append(f"{index}. {attachment_kind(item)}：{name}{size_text}")
        if len(event.attachments) > 10:
            lines.append("…（更多附件已省略）")
        return "\n".join(lines)

    @staticmethod
    def _user_content(text: str, images: list[str]) -> str | list[dict]:
        if not images:
            return text
        parts: list[dict] = []
        if text:
            parts.append({"type": "text", "text": text})
        parts.extend(
            {"type": "image_url", "image_url": {"url": image}} for image in images
        )
        return parts

    @staticmethod
    def _history_text(
        text: str,
        image_count: int,
        document_count: int = 0,
        voice_texts: list[str] | None = None,
    ) -> str:
        notes: list[str] = []
        if image_count:
            notes.append(f"附图 {image_count} 张")
        if document_count:
            notes.append(f"附文档 {document_count} 个")
        content = text
        if voice_texts:
            transcript = "\n".join(f"[语音]{item}" for item in voice_texts)
            content = f"{content}\n{transcript}" if content else transcript
        if not notes:
            return content
        note = f"（{'，'.join(notes)}）"
        return f"{content}{note}" if content else note

    # ---------- 单聊：流式 + 工具 ----------

    async def _stream_reply(
        self,
        event: MessageEvent,
        messages: list[dict],
        remaining: int,
        thinking_enabled: bool,
        reasoning_effort: str,
        unlimited: bool = False,
        output_limit: int | None = None,
        context_tokens: int | None = None,
        progress_mode: str = "off",
        thinking_display: bool = False,
        markdown_enabled: bool = True,
    ) -> tuple[str, bool]:
        """单聊流式回复，支持多轮工具调用、过程展示与断点续传。"""
        working = list(messages)
        limit = output_limit or self._admin_output_tokens()
        tools = (
            self._workspace.schemas()
            + self._web.schemas()
            + [SEND_FILE_SCHEMA, SAVE_ATTACHMENT_SCHEMA]
        )
        reply_seq = {"next": 2}  # 1 留给流式消息
        content_type = "markdown" if markdown_enabled else "text"
        system_suffix = "" if markdown_enabled else PLAIN_TEXT_SUFFIX

        # 流式正文严格追加：首帧固定 STREAM_PREFIX，之后只增不减。
        # 任何“替换已下发前缀”的写法都会触发 QQ 40111001。
        body_limit = max(300, STREAM_SAFE_CHARS - len(CONTINUE_HINT) - 16)
        progress = (
            None
            if progress_mode == "off"
            else ProgressTracker(show_result=progress_mode == "full")
        )
        full_answer = ""
        answer = ""
        body = ""
        started = False
        stream_broken = False
        stream_msg_id = ""
        index = 0
        last_flush = 0.0
        last_sent_body = ""
        llm_failed = False
        rounds_exhausted = False
        output_cut = False
        push_lock = asyncio.Lock()

        reasoning_text = ""
        console = getattr(self, "_console", None)
        live_at = 0.0
        live_lock = asyncio.Lock()

        async def publish_live(kind: str, text: str, force: bool = False) -> None:
            nonlocal live_at
            if console is None:
                return
            now = time.monotonic()
            async with live_lock:
                if not force and now - live_at < 0.8:
                    return
                live_at = now
            await console.publish(event.context_key, kind, text)

        def append_body(text: str, limit: int | None = None) -> None:
            """向流式正文追加内容，只追加不修改，超过安全上限后丢弃。"""
            nonlocal body
            if not text:
                return
            cap = body_limit if limit is None else limit
            room = cap - len(body)
            if room > 0:
                body += text[:room]

        async def push(content: str) -> None:
            nonlocal started, stream_msg_id, index, last_flush
            async with push_lock:
                kwargs = {
                    "input_state": 1,
                    "index": index,
                    "msg_id": event.msg_id,
                    "content_type": content_type,
                }
                if stream_msg_id:
                    kwargs["stream_msg_id"] = stream_msg_id
                resp = await self._api.stream_c2c_message(
                    event.user_openid, content, **kwargs
                )
                if not started:
                    stream_msg_id = str(resp.get("id") or "")
                    if not stream_msg_id:
                        raise QQBotAPIError(
                            200, None, "流式消息响应缺少 id", "stream_messages"
                        )
                    started = True
                index += 1
                last_flush = time.monotonic()

        async def flush(force: bool = False) -> None:
            nonlocal stream_broken, last_sent_body
            if stream_broken:
                return
            if started and not force:
                if body == last_sent_body:
                    return
                if time.monotonic() - last_flush < STREAM_FLUSH_INTERVAL:
                    return
            try:
                await push(STREAM_PREFIX + body)
                last_sent_body = body
                if answer:
                    await publish_live("answer", answer[-200:])
            except QQBotAPIError:
                stream_broken = True
                logger.exception("流式分片发送失败：%s", event.user_openid)

        async def on_reasoning(chunk: str) -> None:
            nonlocal reasoning_text
            if len(reasoning_text) < 4000:
                reasoning_text += chunk
            await publish_live("thinking", reasoning_text[-200:])

        async def run_tool_with_heartbeat(coro) -> str:
            task = asyncio.create_task(coro)
            while True:
                done, _ = await asyncio.wait(
                    {task}, timeout=HEARTBEAT_INTERVAL
                )
                if done:
                    return task.result()
                # 工具执行期间重复发送当前内容，避免 QQ 流式连接超时
                await keep_alive()

        async def keep_alive() -> None:
            nonlocal stream_broken
            if stream_broken:
                return
            await flush(force=True)

        # 先建立流式消息：首帧只发零宽前缀，保证后续追加的前缀永远一致
        await publish_live("thinking", "AI 正在思考…", True)
        await keep_alive()

        round_limit = self._round_limit(unlimited)
        _round = 0
        while True:
            state = StreamState()
            round_text = ""
            try:
                async with contextlib.aclosing(
                    self._llm.stream_chat(
                        working,
                        tools=tools,
                        state=state,
                        max_tokens=remaining,
                        thinking_enabled=thinking_enabled,
                        reasoning_effort=reasoning_effort,
                        system_suffix=system_suffix,
                        on_reasoning=on_reasoning if thinking_display else None,
                    )
                ) as stream:
                    iterator = stream.__aiter__()
                    while True:
                        try:
                            delta = await asyncio.wait_for(
                                anext(iterator), timeout=HEARTBEAT_INTERVAL
                            )
                        except asyncio.TimeoutError:
                            # 模型长时间没有增量时也保持连接并更新“思考中”
                            await keep_alive()
                            continue
                        except StopAsyncIteration:
                            break
                        if len(full_answer) < MAX_FULL_ANSWER_CHARS:
                            full_answer += delta
                        if len(round_text) < MAX_FULL_ANSWER_CHARS:
                            round_text += delta
                        chunk = delta[: max(0, body_limit - len(body))]
                        if chunk:
                            answer += chunk
                            append_body(chunk)
                        await flush()
            except LLMError:
                llm_failed = True
                logger.exception("流式生成失败：%s", event.user_openid)
                break

            await self._charge_usage(
                event.context_key, state.usage, unlimited=unlimited
            )
            if state.finish_reason == "length" and not state.tool_calls:
                output_cut = True
            if unlimited:
                remaining = limit
            else:
                remaining = min(
                    await self._quota.remaining(event.context_key), limit
                )

            if len(full_answer) >= MAX_FULL_ANSWER_CHARS:
                break
            if not state.tool_calls:
                break
            if remaining <= 0:
                logger.info("额度已用完，停止后续工具轮次：%s", event.context_key)
                break

            calls = self._prepare_tool_calls(state, _round)
            if not calls:
                logger.warning("模型返回了无效工具调用：%s", event.user_openid)
                break
            if round_limit is not None and _round >= round_limit:
                rounds_exhausted = True
                logger.warning("工具调用轮数超限：%s", event.user_openid)
                break
            working.append(self._assistant_tool_message(round_text, state))
            if state.finish_reason == "length":
                logger.warning("工具调用参数被截断：%s", event.user_openid)
                for call in calls:
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": TRUNCATED_TOOL_RESULT,
                        }
                    )
                _round += 1
                continue

            for call in calls:
                tool_name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                tool_call_id = call["id"]
                logger.info("调用工具 %s：%s", tool_name, arguments[:200])
                if progress is not None:
                    progress.record(tool_name, arguments)
                    await publish_live("tool", progress.render(), True)
                    label = progress.last_label
                    if label:
                        # 过程信息只能追加在已下发内容之后，不能改写前缀
                        sep = "" if not body or body.endswith("\n") else "\n"
                        append_body(f"{sep}【{_short(label, 60)}】\n")
                    await flush(force=True)
                result = await run_tool_with_heartbeat(
                    self._run_tool(
                        event, tool_name, arguments, reply_seq, unlimited=unlimited
                    )
                )
                if progress is not None and progress.show_result:
                    progress.add_result(result)
                    await publish_live("tool", progress.render(), True)
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result,
                    }
                )
            _round += 1

        if not full_answer:
            full_answer = answer or FALLBACK_REPLY
            if not answer:
                llm_failed = True

        # 已下发内容不可修改：剩余内容按实际已发送长度（len(answer)）裁剪，
        # 并自动重开代码围栏、补表格表头，保证 /继续 单独发出时结构完整。
        rest_text = full_answer[len(answer):] if full_answer.startswith(answer) else ""
        if rest_text:
            if markdown_enabled:
                rest_text = tail_after(answer, rest_text)
            self._continuations.put(event.context_key, rest_text)
            self._mark_needs_continue(event.context_key, False)
            append_body(CONTINUE_HINT, STREAM_SAFE_CHARS)
        elif output_cut:
            self._mark_needs_continue(event.context_key, True)
            if answer and not answer.endswith(("……", "。", "！", "？", "!", "?")):
                append_body("……", STREAM_SAFE_CHARS)
        if markdown_enabled and body:
            # 补齐末尾未闭合的围栏代码块（只追加，不改动已发内容）
            closing = close_fences(body)[len(body):]
            append_body(closing, STREAM_SAFE_CHARS)

        if not body:
            append_body(FALLBACK_REPLY, STREAM_SAFE_CHARS)
            llm_failed = True
        final_display = STREAM_PREFIX + body

        if started:
            try:
                await self._api.stream_c2c_message(
                    event.user_openid,
                    STREAM_PREFIX + body,
                    input_state=10,
                    index=index,
                    stream_msg_id=stream_msg_id,
                    msg_id=event.msg_id,
                    content_type=content_type,
                )
                console = getattr(self, "_console", None)
                if console is not None:
                    await console.record_outbound(event, full_answer, replies=1)
            except QQBotAPIError:
                logger.exception("结束流式消息失败，回退普通消息：%s", event.user_openid)
                tail = body[len(last_sent_body):] if len(last_sent_body) < len(body) else ""
                if tail:
                    await self._reply(
                        event,
                        tail,
                        start_seq=reply_seq["next"],
                        markdown=markdown_enabled,
                    )
        else:
            # 没有成功创建过流式消息，用普通消息兜底
            await self._reply(event, final_display, markdown=markdown_enabled)

        ok = not (llm_failed or rounds_exhausted)
        return full_answer, ok

    async def _run_tool(
        self,
        event: MessageEvent,
        tool_name: str,
        arguments: str,
        reply_seq: dict | None,
        unlimited: bool = False,
    ) -> str:
        if tool_name in WEB_TOOL_NAMES:
            return await self._web.execute(tool_name, arguments)
        if tool_name == "send_file":
            if event.kind != "c2c":
                return "发送失败：群聊暂不支持发送空间文件"
            if reply_seq is None:
                return "发送失败：缺少回复序号"
            return await self._send_file(event, arguments, reply_seq)
        if tool_name == "save_attachment":
            return await self._save_attachment(event, arguments)

        usage: dict = {}
        result = await self._workspace.execute(
            self._workspace_key(event),
            tool_name,
            arguments,
            quota_bytes=self._workspace_quota(event),
            usage=usage,
        )
        if usage and not unlimited:
            seconds = float(usage.get("sandbox_seconds") or 0.0)
            network = int(usage.get("network_bytes") or 0)
            if seconds or network:
                credits = await self._quota.charge(
                    event.context_key,
                    sandbox_seconds=seconds,
                    network_bytes=network,
                )
                detail = f"{seconds:.2f} 秒"
                if network:
                    detail += f"、网络 {network / (1024 * 1024):.2f}MB"
                result += f"\n（本次执行 {detail}，消耗 {credits} 积分）"
        return result

    @staticmethod
    def _workspace_key(event: MessageEvent) -> str:
        return event.context_key if event.kind == "group" else event.user_openid

    def _workspace_quota(self, event: MessageEvent) -> int | None:
        if event.kind == "c2c" and self._admins.is_admin(event.user_openid):
            return self._config.workspace_admin_quota_bytes
        return None

    async def _save_attachment(self, event: MessageEvent, arguments: str) -> str:
        """把当前消息中的附件下载并保存到私有空间。"""
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                return "工具执行失败：参数必须是 JSON 对象"
            index = int(args.get("index") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            return "工具执行失败：index 必须是正整数"

        if index < 1 or index > len(event.attachments):
            return f"工具执行失败：index 必须在 1-{len(event.attachments)} 之间"

        item = event.attachments[index - 1]
        url = str(item.get("url") or item.get("voice_wav_url") or "").strip()
        if not url:
            return "工具执行失败：该附件没有下载地址"

        max_bytes = max(
            self._config.document_max_bytes,
            self._config.image_max_bytes,
            self._config.voice_max_bytes,
        )
        downloaded = await self._download_attachment(url, max_bytes)
        if downloaded is None:
            return "工具执行失败：附件下载失败或超过大小上限"
        data, _ = downloaded

        filename = Path(str(item.get("filename") or "")).name
        if not filename:
            suffix = Path(urlsplit(url).path).suffix or ".bin"
            filename = f"attachment_{index}{suffix}"
        path = str(args.get("path") or filename).strip() or filename
        try:
            saved_path, size = self._workspace.save_bytes(
                self._workspace_key(event),
                path,
                data,
                overwrite=bool(args.get("overwrite")),
                quota_bytes=self._workspace_quota(event),
            )
        except ToolError as exc:
            return f"工具执行失败：{exc}"
        except Exception as exc:
            logger.exception("保存附件失败")
            return f"工具执行失败：{exc}"
        return f"已保存到工作空间：{saved_path}（{size} 字节）"

    async def _send_file(
        self, event: MessageEvent, arguments: str, reply_seq: dict
    ) -> str:
        """把私有空间文件上传并发送给用户（msg_type=7）。"""
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ToolError("参数必须是 JSON 对象")
            file_name, data = await asyncio.to_thread(
                self._workspace.export_file, event.user_openid, args.get("path")
            )
        except ToolError as exc:
            return f"工具执行失败：{exc}"
        except Exception as exc:
            logger.exception("读取待发送文件失败")
            return f"工具执行失败：{exc}"

        if not self._admins.is_admin(event.user_openid):
            # 先按文件大小预估积分，避免上传成功后才因额度不足而透支
            credits = self._quota.cost(file_bytes=len(data))
            remaining = await self._quota.remaining(event.context_key)
            if credits > remaining:
                return (
                    f"发送失败：额度不足（需要 {credits} 积分，剩余 {remaining} 积分）。"
                    "可以先用 /quota 查看用量。"
                )

        limit = MAX_C2C_PASSIVE_REPLIES - int(reply_seq.get("reserve", 0))
        if reply_seq["next"] > limit:
            return "发送失败：本次消息的被动回复次数已用完"

        seq = reply_seq["next"]
        try:
            if len(data) <= DIRECT_UPLOAD_MAX_BYTES:
                encoded = base64.b64encode(data).decode("ascii")
                file_info = await self._api.upload_c2c_file(
                    event.user_openid,
                    file_name=file_name,
                    file_data=encoded,
                )
            else:
                logger.info("文件超过 5MB，走分片上传：%s（%d 字节）", file_name, len(data))
                file_info = await self._api.upload_c2c_large_file(
                    event.user_openid,
                    file_name=file_name,
                    data=data,
                )
            await self._api.send_c2c_media(
                event.user_openid,
                file_info,
                msg_id=event.msg_id,
                msg_seq=seq,
            )
        except QQBotAPIError as exc:
            logger.exception("发送文件失败：%s", file_name)
            return f"发送文件失败：{exc.message}"

        reply_seq["next"] += 1
        if self._admins.is_admin(event.user_openid):
            return f"已发送文件 {file_name}（{len(data)} 字节，管理员不计费）"
        credits = await self._quota.charge(event.context_key, file_bytes=len(data))
        return f"已发送文件 {file_name}（{len(data)} 字节，{credits} 积分）"

    async def _charge_usage(
        self, scope: str, usage: dict, *, unlimited: bool = False
    ) -> int:
        """按 API 返回的真实 usage 扣积分；管理员不计费。"""
        if not usage:
            logger.warning("模型响应未返回 usage，本次不扣积分：%s", scope)
            return 0

        def count(name: str) -> int:
            try:
                return max(0, int(usage.get(name) or 0))
            except (TypeError, ValueError):
                return 0

        input_tokens = count("prompt_tokens")
        output_tokens = count("completion_tokens")
        if unlimited:
            logger.info(
                "管理员不计费 %s：输入 %d tokens / 输出 %d tokens",
                scope,
                input_tokens,
                output_tokens,
            )
            return 0
        credits = await self._quota.charge(
            scope, input_tokens=input_tokens, output_tokens=output_tokens
        )
        logger.info(
            "计费 %s：输入 %d tokens / 输出 %d tokens → %d 积分",
            scope,
            input_tokens,
            output_tokens,
            credits,
        )
        return credits

    def _prepare_tool_calls(
        self, state: StreamState, round_index: int
    ) -> list[dict]:
        """规范化本轮工具调用，保证 assistant 消息与 tool 结果一一对应。

        - 丢弃缺少函数名的无效调用
        - 补齐缺失或重复的调用 ID
        - 非字符串 arguments 统一转成 JSON 字符串
        - 超出单轮上限的调用直接剔除，并同步写回 state，避免 API 消息错位
        """
        calls: list[dict] = []
        seen_ids: set[str] = set()
        for raw in state.tool_calls or []:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                try:
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                except (TypeError, ValueError):
                    arguments = "{}"
            call_id = raw.get("id")
            if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
                call_id = f"call_{round_index}_{len(calls)}"
            seen_ids.add(call_id)
            calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name.strip(), "arguments": arguments},
                }
            )
            if len(calls) >= MAX_TOOL_CALLS_PER_ROUND:
                break
        state.tool_calls = calls
        return calls

    @staticmethod
    def _assistant_tool_message(round_text: str, state: StreamState) -> dict:
        message: dict = {
            "role": "assistant",
            "content": round_text or None,
            "tool_calls": state.tool_calls,
        }
        # 思考模式下发生过工具调用的轮次必须回传 reasoning_content
        if state.reasoning_content:
            message["reasoning_content"] = state.reasoning_content
        return message

    def _admin_output_tokens(self) -> int:
        """管理员单次输出上限。"""
        return self._config.admin_max_tokens

    async def _output_limit(self, key: str) -> int:
        """会话输出上限：用户设置值，受管理员上限约束。"""
        cap = self._admin_output_tokens()
        raw = await self._preferences.get(key, "length")
        if not raw:
            return cap
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return cap
        return max(256, min(cap, value))

    async def _context_limit(self, key: str, unlimited: bool) -> int:
        """会话上下文预算：普通用户默认较小，管理员默认最大。"""
        cap = self._config.admin_context_tokens
        if unlimited:
            default = cap
        else:
            default = self._config.context_max_tokens
        raw = await self._preferences.get(key, "context")
        if not raw:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(1024, min(cap, value))

    @staticmethod
    def _round_limit(unlimited: bool) -> int | None:
        """管理员工具轮次不限；普通用户受 MAX_TOOL_ROUNDS 限制。"""
        return None if unlimited else MAX_TOOL_ROUNDS

    @staticmethod
    def _progress_mode(value: str | None) -> str:
        """过程显示档位：off / calls / full（兼容旧值 on）。"""
        if value == "full":
            return "full"
        if value in ("calls", "on"):
            return "calls"
        return "off"

    def _thinking_params(self, value: str | None) -> tuple[bool, str]:
        if not value:
            return (
                self._config.deepseek_thinking_enabled,
                self._config.deepseek_reasoning_effort,
            )
        if value == "off":
            return False, self._config.deepseek_reasoning_effort
        return True, value

    # ---------- 群聊 / 兜底：普通文本 ----------

    async def _text_reply(
        self,
        event: MessageEvent,
        messages: list[dict],
        key: str,
        max_tokens: int,
        thinking_enabled: bool,
        reasoning_effort: str,
        user_text: str,
        tools: list[dict] | None = None,
        reply_seq: dict | None = None,
        unlimited: bool = False,
        output_limit: int | None = None,
        context_tokens: int | None = None,
        markdown: bool = True,
    ) -> None:
        """群聊与流式兜底：非流式回复，支持网页搜索/抓取工具。"""
        working = list(messages)
        console = getattr(self, "_console", None)
        if console is not None:
            await console.publish(event.context_key, "thinking", "AI 正在思考…")
        limit = output_limit or self._admin_output_tokens()
        tool_schemas = self._web.schemas() if tools is None else tools
        system_suffix = "" if markdown else PLAIN_TEXT_SUFFIX
        answer = ""

        round_limit = self._round_limit(unlimited)
        _round = 0
        while True:
            state = StreamState()
            try:
                answer, usage = await self._llm.chat(
                    working,
                    tools=tool_schemas,
                    state=state,
                    max_tokens=max_tokens,
                    thinking_enabled=thinking_enabled,
                    reasoning_effort=reasoning_effort,
                    system_suffix=system_suffix,
                )
            except LLMError:
                logger.exception("生成回复失败：%s", key)
                await self._reply(event, FALLBACK_REPLY, markdown=markdown)
                return

            await self._charge_usage(key, usage, unlimited=unlimited)
            if not state.tool_calls:
                if state.finish_reason == "length":
                    self._mark_needs_continue(key, True)
                    if answer and not answer.endswith(
                        ("…", "。", "！", "？", "!", "?")
                    ):
                        answer += "…"
                break

            if unlimited:
                max_tokens = limit
            else:
                remaining = min(await self._quota.remaining(key), limit)
                if remaining <= 0:
                    logger.info("额度已用完，停止后续工具轮次：%s", key)
                    break
                max_tokens = remaining

            calls = self._prepare_tool_calls(state, _round)
            if not calls:
                logger.warning("模型返回了无效工具调用：%s", key)
                break
            if round_limit is not None and _round >= round_limit:
                logger.warning("工具调用轮数超限：%s", key)
                break
            working.append(self._assistant_tool_message(answer, state))
            if state.finish_reason == "length":
                logger.warning("工具调用参数被截断：%s", key)
                for call in calls:
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": TRUNCATED_TOOL_RESULT,
                        }
                    )
                _round += 1
                continue

            for call in calls:
                tool_name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                logger.info("调用工具 %s：%s", tool_name, arguments[:200])
                result = await self._run_tool(
                    event, tool_name, arguments, reply_seq, unlimited=unlimited
                )
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    }
                )
            _round += 1

        if not answer:
            answer = FALLBACK_REPLY
        self._contexts.append_turn(
            key,
            user_text,
            answer,
            max_tokens=context_tokens or self._config.admin_context_tokens,
        )
        await self._reply(
            event,
            answer,
            start_seq=reply_seq["next"] if reply_seq else 1,
            markdown=markdown,
        )

    async def _reply(
        self,
        event: MessageEvent,
        text: str,
        start_seq: int = 1,
        markdown: bool = False,
    ) -> None:
        if event.kind == "c2c":
            max_chunks = min(MAX_CHUNKS, MAX_C2C_PASSIVE_REPLIES - start_seq + 1)
            if max_chunks <= 0:
                logger.warning("被动回复次数已用完，跳过文本回复：%s", event.msg_id)
                return
        else:
            max_chunks = MAX_CHUNKS

        if markdown:
            chunks = split_markdown_chunks(
                text, self._config.max_reply_chars, max_chunks
            )
        else:
            chunks = split_text(text, self._config.max_reply_chars, max_chunks)
        if not chunks:
            return

        for offset, chunk in enumerate(chunks):
            seq = start_seq + offset if event.kind == "c2c" else offset + 1

            async def send_chunk(use_markdown: bool) -> None:
                if event.kind == "group":
                    await self._api.send_group_message(
                        event.group_openid or "",
                        chunk,
                        msg_id=event.msg_id,
                        msg_seq=seq,
                        markdown=use_markdown,
                    )
                else:
                    await self._api.send_c2c_message(
                        event.user_openid,
                        chunk,
                        msg_id=event.msg_id,
                        msg_seq=seq,
                        markdown=use_markdown,
                    )

            try:
                await send_chunk(markdown)
            except QQBotAPIError:
                if not markdown:
                    logger.exception("发送回复失败：%s", event.msg_id)
                    return
                logger.warning("Markdown 发送失败，回退纯文本：%s", event.msg_id)
                try:
                    await send_chunk(False)
                except QQBotAPIError:
                    logger.exception("发送回复失败：%s", event.msg_id)
                    return

            if offset < len(chunks) - 1:
                await asyncio.sleep(CHUNK_INTERVAL)

        console = getattr(self, "_console", None)
        if console is not None:
            await console.record_outbound(event, text, replies=len(chunks))
