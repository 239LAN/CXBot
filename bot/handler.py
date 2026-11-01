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
import json
import logging
import time
from pathlib import Path

import aiohttp

from bot.api import QQBotAPI, QQBotAPIError
from bot.commands import CommandRouter
from bot.events import MessageEvent, split_text
from config import Config
from services.admins import AdminStore
from services.context import ContextStore
from services.deepseek import DeepSeekClient, LLMError, StreamState
from services.documents import DocumentError, extract_text
from services.preferences import PreferenceStore
from services.quota import QuotaManager
from services.reset_codes import ResetCodeStore
from services.web import WebTools
from services.workspace import ToolError, WorkspaceTools

logger = logging.getLogger(__name__)

WEB_TOOL_NAMES = {"web_search", "web_fetch"}
ATTACHMENT_DOWNLOAD_TIMEOUT = 20.0

RESET_COMMANDS = {"/reset", "/clear", "重置上下文", "清空上下文"}
MAX_CHUNKS = 5  # 群聊/回退消息被动回复同一个 msg_id 最多 5 条
CHUNK_INTERVAL = 0.25

STREAM_FLUSH_INTERVAL = 0.6  # 秒，避免每来一个 token 都调一次接口
STREAM_MAX_CHARS = 2000
MAX_TOOL_ROUNDS = 8
MAX_TOOL_CALLS_PER_ROUND = 8
MAX_C2C_PASSIVE_REPLIES = 4  # 单聊同一条消息最多回复 4 次（含流式消息）
DIRECT_UPLOAD_MAX_BYTES = 5 * 1024 * 1024  # 超过后走分片上传
FALLBACK_REPLY = "抱歉，我这边暂时出了点问题，请稍后再试～"
TRUNCATED_TOOL_RESULT = (
    "本次工具调用参数因输出达到长度上限被截断，未执行。"
    "请缩短内容，分批调用 write_file/append_file 等工具。"
)


def _image_mime(*candidates: str) -> str:
    for value in candidates:
        mime = value.split(";")[0].strip().lower()
        if mime.startswith("image/"):
            return mime
    return "image/jpeg"

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
        self._commands = CommandRouter(
            config, quota, preferences, reset_codes, admins
        )
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def handle(self, event: MessageEvent) -> None:
        key = event.context_key
        text = event.content

        async with self._lock_for(key):
            if text.lower() in RESET_COMMANDS:
                self._contexts.reset(key)
                logger.info("清空上下文：%s", key)
                await self._reply(event, "上下文已清空，我们重新开始吧。")
                return

            if text.startswith("/"):
                reply = await self._commands.route(event, text)
                await self._reply(event, reply)
                return

            unlimited = self._admins.is_admin(event.user_openid)
            if unlimited:
                remaining = self._max_output_tokens()
                logger.info("管理员不限额度：%s", event.user_openid)
            else:
                remaining, reason = await self._quota.check(key)
                if reason:
                    logger.info("额度不足，拒绝请求 %s：%s", key, reason)
                    await self._reply(event, reason)
                    return

            images = await self._load_images(event)
            doc_blocks, doc_errors = await self._load_documents(event)

            if not text and not images and not doc_blocks:
                if event.document_attachments:
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
            if doc_blocks:
                prompt_text = f"{prompt_text}\n\n" + "\n\n".join(doc_blocks)

            messages = self._contexts.load(key) + [
                {"role": "user", "content": self._user_content(prompt_text, images)}
            ]
            history_text = self._history_text(text, len(images), len(doc_blocks))

            thinking = await self._preferences.get(key, "thinking")
            thinking_enabled, reasoning_effort = self._thinking_params(thinking)
            stream_off = await self._preferences.get(key, "stream") == "off"

            if event.kind == "c2c":
                c2c_tools = (
                    self._workspace.schemas() + self._web.schemas() + [SEND_FILE_SCHEMA]
                )
                if stream_off:
                    # 关闭流式时仍保留私有空间与发送文件工具，并预留一条文本回复
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
                    )
                    return
                try:
                    answer, ok = await self._stream_reply(
                        event,
                        messages,
                        remaining,
                        thinking_enabled,
                        reasoning_effort,
                        unlimited=unlimited,
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
                    )
                    return
                if ok and answer:
                    self._contexts.append_turn(key, history_text, answer)
                return

            # 群聊不支持流式，走普通分条回复
            await self._text_reply(
                event,
                messages,
                key,
                remaining,
                thinking_enabled,
                reasoning_effort,
                history_text,
                unlimited=unlimited,
            )

    async def _download_attachment(
        self, url: str, max_bytes: int
    ) -> tuple[bytes, str] | None:
        try:
            timeout = aiohttp.ClientTimeout(total=ATTACHMENT_DOWNLOAD_TIMEOUT)
            async with self._api.session.get(url, timeout=timeout) as resp:
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
            mime = _image_mime(str(item.get("content_type") or ""), content_type)
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
    def _history_text(text: str, image_count: int, document_count: int = 0) -> str:
        notes: list[str] = []
        if image_count:
            notes.append(f"附图 {image_count} 张")
        if document_count:
            notes.append(f"附文档 {document_count} 个")
        if not notes:
            return text
        note = f"（{'，'.join(notes)}）"
        return f"{text}{note}" if text else note

    # ---------- 单聊：流式 + 工具 ----------

    async def _stream_reply(
        self,
        event: MessageEvent,
        messages: list[dict],
        remaining: int,
        thinking_enabled: bool,
        reasoning_effort: str,
        unlimited: bool = False,
    ) -> tuple[str, bool]:
        """单聊流式回复，支持多轮工具调用。返回 (最终文本, 是否成功生成)。"""
        working = list(messages)
        tools = self._workspace.schemas() + self._web.schemas() + [SEND_FILE_SCHEMA]
        reply_seq = {"next": 2}  # 1 留给流式消息

        answer = ""
        started = False
        stream_broken = False
        stream_msg_id = ""
        index = 0
        last_flush = 0.0
        last_sent_len = 0
        llm_failed = False
        rounds_exhausted = False
        output_cut = False

        async def push(content: str, final: bool = False) -> None:
            nonlocal started, stream_msg_id, index, last_flush, last_sent_len
            kwargs = {
                "input_state": 10 if final else 1,
                "index": index,
                "msg_id": event.msg_id,
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
            last_sent_len = len(content)

        async def flush(force: bool = False) -> None:
            nonlocal stream_broken
            if not answer or stream_broken:
                return
            now = time.monotonic()
            if not started or force:
                should_send = True
            else:
                should_send = (
                    now - last_flush >= STREAM_FLUSH_INTERVAL
                    and len(answer) > last_sent_len
                )
            if not should_send:
                return
            try:
                await push(answer)
            except QQBotAPIError:
                stream_broken = True
                logger.exception("流式分片发送失败：%s", event.user_openid)

        for _round in range(MAX_TOOL_ROUNDS + 1):
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
                    )
                ) as stream:
                    async for delta in stream:
                        # 超出展示上限后仍继续消费，确保拿到最终 usage 用于计费
                        if len(answer) >= STREAM_MAX_CHARS:
                            continue
                        answer += delta
                        round_text += delta
                        await flush()
            except LLMError:
                llm_failed = True
                logger.exception("流式生成失败：%s", event.user_openid)
                if not answer:
                    answer = FALLBACK_REPLY
                break

            await self._charge_usage(
                event.context_key, state.usage, unlimited=unlimited
            )
            if state.finish_reason == "length" and not state.tool_calls:
                output_cut = True
            if unlimited:
                remaining = self._max_output_tokens()
            else:
                remaining = await self._quota.remaining(event.context_key)

            if len(answer) >= STREAM_MAX_CHARS:
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
                continue

            for call in calls:
                tool_name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                tool_call_id = call["id"]
                logger.info("调用工具 %s：%s", tool_name, arguments[:200])
                result = await self._run_tool(event, tool_name, arguments, reply_seq)
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result,
                    }
                )
        else:
            rounds_exhausted = True
            logger.warning("工具调用轮数超限：%s", event.user_openid)

        if output_cut and answer and not answer.endswith(
            ("…", "。", "！", "？", "!", "?")
        ):
            answer += "…"
        if len(answer) > STREAM_MAX_CHARS:
            answer = answer[:STREAM_MAX_CHARS] + "…"
        if not answer:
            answer = FALLBACK_REPLY
            llm_failed = True

        if started:
            try:
                await self._api.stream_c2c_message(
                    event.user_openid,
                    answer,
                    input_state=10,
                    index=index,
                    stream_msg_id=stream_msg_id,
                    msg_id=event.msg_id,
                )
            except QQBotAPIError:
                logger.exception("结束流式消息失败，回退普通消息：%s", event.user_openid)
                tail = answer[last_sent_len:] if last_sent_len < len(answer) else ""
                if tail:
                    await self._reply(event, tail, start_seq=reply_seq["next"])
        else:
            # 没有成功创建过流式消息，用普通消息兜底
            await self._reply(event, answer)

        ok = not (llm_failed or rounds_exhausted)
        return answer, ok

    async def _run_tool(
        self,
        event: MessageEvent,
        tool_name: str,
        arguments: str,
        reply_seq: dict | None,
    ) -> str:
        if tool_name in WEB_TOOL_NAMES:
            return await self._web.execute(tool_name, arguments)
        if event.kind != "c2c":
            return f"工具不可用：{tool_name}（群聊只支持网页搜索与抓取）"
        if tool_name == "send_file":
            if reply_seq is None:
                return "发送失败：群聊不支持发送文件"
            return await self._send_file(event, arguments, reply_seq)
        return await self._workspace.execute(event.user_openid, tool_name, arguments)

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

    def _max_output_tokens(self) -> int:
        return max(
            self._config.deepseek_max_tokens,
            self._config.deepseek_tool_max_tokens,
        )

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
    ) -> None:
        """群聊与流式兜底：非流式回复，支持网页搜索/抓取工具。"""
        working = list(messages)
        tool_schemas = self._web.schemas() if tools is None else tools
        answer = ""

        for _round in range(MAX_TOOL_ROUNDS + 1):
            state = StreamState()
            try:
                answer, usage = await self._llm.chat(
                    working,
                    tools=tool_schemas,
                    state=state,
                    max_tokens=max_tokens,
                    thinking_enabled=thinking_enabled,
                    reasoning_effort=reasoning_effort,
                )
            except LLMError:
                logger.exception("生成回复失败：%s", key)
                await self._reply(event, FALLBACK_REPLY)
                return

            await self._charge_usage(key, usage, unlimited=unlimited)
            if not state.tool_calls:
                if state.finish_reason == "length" and answer and not answer.endswith(
                    ("…", "。", "！", "？", "!", "?")
                ):
                    answer += "…"
                break

            if unlimited:
                max_tokens = self._max_output_tokens()
            else:
                remaining = await self._quota.remaining(key)
                if remaining <= 0:
                    logger.info("额度已用完，停止后续工具轮次：%s", key)
                    break
                max_tokens = remaining

            calls = self._prepare_tool_calls(state, _round)
            if not calls:
                logger.warning("模型返回了无效工具调用：%s", key)
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
                continue

            for call in calls:
                tool_name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                logger.info("调用工具 %s：%s", tool_name, arguments[:200])
                result = await self._run_tool(event, tool_name, arguments, reply_seq)
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    }
                )

        if not answer:
            answer = FALLBACK_REPLY
        self._contexts.append_turn(key, user_text, answer)
        await self._reply(
            event,
            answer,
            start_seq=reply_seq["next"] if reply_seq else 1,
        )

    async def _reply(self, event: MessageEvent, text: str, start_seq: int = 1) -> None:
        if event.kind == "c2c":
            max_chunks = min(MAX_CHUNKS, MAX_C2C_PASSIVE_REPLIES - start_seq + 1)
            if max_chunks <= 0:
                logger.warning("被动回复次数已用完，跳过文本回复：%s", event.msg_id)
                return
        else:
            max_chunks = MAX_CHUNKS

        chunks = split_text(text, self._config.max_reply_chars, max_chunks)
        if not chunks:
            return

        for offset, chunk in enumerate(chunks):
            seq = start_seq + offset if event.kind == "c2c" else offset + 1
            try:
                if event.kind == "group":
                    await self._api.send_group_message(
                        event.group_openid or "",
                        chunk,
                        msg_id=event.msg_id,
                        msg_seq=seq,
                    )
                else:
                    await self._api.send_c2c_message(
                        event.user_openid,
                        chunk,
                        msg_id=event.msg_id,
                        msg_seq=seq,
                    )
            except QQBotAPIError:
                logger.exception("发送回复失败：%s", event.msg_id)
                return

            if offset < len(chunks) - 1:
                await asyncio.sleep(CHUNK_INTERVAL)
