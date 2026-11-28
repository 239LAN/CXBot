"""DeepSeek Chat API 客户端（OpenAI 兼容协议，支持思考模式与工具调用）。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

CLIENT_BLOCK_HINTS = (
    "本站仅限主流",
    "当前客户端不支持",
    "仅限主流 Coding",
    "Coding 客户端",
)
_RETRY_USER_AGENTS: tuple[str | None, ...] = (
    None,
    "codex_cli_rs/0.20.0",
    "claude-code/1.0.0",
)
_MAX_RETRIES = 3  # 模型异常/连接中断时的重试次数，总尝试次数为 1 + 3


class LLMError(RuntimeError):
    """调用大模型失败。"""


@dataclass
class StreamState:
    """流式响应过程中收集的非正文内容。"""

    reasoning_content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""


class DeepSeekClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=90)
        # 流式响应可能持续较久，只限制连接与两次读之间的间隔
        self._stream_timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)

    def _build_payload(
        self,
        history: list[dict],
        *,
        stream: bool,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
        system_suffix: str = "",
    ) -> dict:
        system_content = self._config.system_prompt
        if system_suffix:
            system_content = f"{system_content}\n{system_suffix}"
        messages = [{"role": "system", "content": system_content}]
        messages.extend(history)
        if max_tokens is not None:
            # 调用方已按用户额度/管理员上限算好本次输出上限，直接采用
            effective_max = max(1, int(max_tokens))
        else:
            effective_max = self._config.deepseek_max_tokens
            if tools:
                effective_max = max(
                    effective_max, self._config.deepseek_tool_max_tokens
                )
        payload: dict = {
            "model": self._config.deepseek_model,
            "messages": messages,
            "stream": stream,
            "max_tokens": effective_max,
            "temperature": self._config.deepseek_temperature,
        }
        if stream:
            # 让 API 在最后一个 chunk 返回真实 token 用量，用于计费
            payload["stream_options"] = {"include_usage": True}
        thinking = (
            self._config.deepseek_thinking_enabled
            if thinking_enabled is None
            else thinking_enabled
        )
        if thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = (
                reasoning_effort or self._config.deepseek_reasoning_effort
            )
        else:
            payload["thinking"] = {"type": "disabled"}
        if tools:
            payload["tools"] = tools
        return payload

    @staticmethod
    def _is_client_blocked(text: str) -> bool:
        return any(hint in (text or "") for hint in CLIENT_BLOCK_HINTS)

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        """上游 5xx、限流与请求超时属于可重试的临时故障。"""
        return status >= 500 or status in (408, 429)

    def _headers(self, attempt: int = 0) -> dict:
        headers = {
            "Authorization": f"Bearer {self._config.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        ua = _RETRY_USER_AGENTS[attempt % len(_RETRY_USER_AGENTS)]
        if ua:
            headers["User-Agent"] = ua
        return headers

    @property
    def _url(self) -> str:
        return f"{self._config.deepseek_base_url}/chat/completions"

    async def chat(
        self,
        history: list[dict],
        *,
        tools: list[dict] | None = None,
        state: StreamState | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
        system_suffix: str = "",
    ) -> tuple[str, dict]:
        """非流式对话，返回 (完整回复, usage)。

        传入 state 时会记录 tool_calls 与 reasoning_content；只有工具调用、
        没有正文的响应不会视为失败。
        """
        payload = self._build_payload(
            history,
            stream=False,
            tools=tools,
            max_tokens=max_tokens,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            system_suffix=system_suffix,
        )

        async def _post(headers_attempt: int) -> tuple[int, str]:
            async with self._session.post(
                self._url,
                json=payload,
                headers=self._headers(headers_attempt),
                timeout=self._timeout,
            ) as resp:
                return resp.status, await resp.text()

        attempts = 1 + _MAX_RETRIES
        last_raw = ""
        for attempt in range(attempts):
            try:
                status, raw = await _post(attempt)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_raw = str(exc)
                if attempt + 1 < attempts:
                    logger.warning(
                        "请求连接中断（%s），第 %d 次重试", exc, attempt + 2
                    )
                    await asyncio.sleep(0.8)
                    continue
                raise LLMError(f"请求连接中断：{exc}") from exc
            last_raw = raw
            if status != 200:
                if self._is_client_blocked(raw) and attempt + 1 < attempts:
                    logger.warning("API 限制当前客户端，换用备用客户端标识重试")
                    await asyncio.sleep(0.6)
                    continue
                if self._is_retryable_status(status) and attempt + 1 < attempts:
                    logger.warning(
                        "上游返回 HTTP %d，第 %d 次重试", status, attempt + 2
                    )
                    await asyncio.sleep(min(3.0, 1.0 * (attempt + 1)))
                    continue
                raise LLMError(f"DeepSeek 调用失败：HTTP {status} {raw}")

            try:
                data = json.loads(raw)
                message = data["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"DeepSeek 响应解析失败：{raw}") from exc

            content = message.get("content")
            if not isinstance(content, str):
                content = ""
            if self._is_client_blocked(content):
                if attempt + 1 < attempts:
                    logger.warning("API 返回客户端限制提示，换用备用客户端标识重试")
                    await asyncio.sleep(0.6)
                    continue
                raise LLMError(f"DeepSeek 拒绝了当前客户端：{content[:300]}")
            if state is not None:
                state.tool_calls = message.get("tool_calls") or []
                state.reasoning_content = (
                    message.get("reasoning_content")
                    or message.get("reasoning")
                    or ""
                )
                state.finish_reason = data["choices"][0].get("finish_reason") or ""
            if not content.strip() and not (state and state.tool_calls):
                raise LLMError(f"DeepSeek 返回空内容：{raw}")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            return content.strip(), usage

        raise LLMError(f"DeepSeek 拒绝了当前客户端：{last_raw[:300]}")

    async def stream_chat(
        self,
        history: list[dict],
        tools: list[dict] | None = None,
        state: StreamState | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
        system_suffix: str = "",
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
    ) -> AsyncIterator[str]:
        """流式对话，逐段产出正文增量。

        思考内容、工具调用与 usage 写入 state；不传 state 时只输出正文。
        on_reasoning 用于实时接收思考增量（仅用于展示）。
        """
        payload = self._build_payload(
            history,
            stream=True,
            tools=tools,
            max_tokens=max_tokens,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            system_suffix=system_suffix,
        )
        attempts = 1 + _MAX_RETRIES
        last_block = ""
        for attempt in range(attempts):
            tool_slots: dict[int, dict] = {}
            yielded = False
            retry = False

            try:
                async with self._session.post(
                    self._url,
                    json=payload,
                    headers=self._headers(attempt),
                    timeout=self._stream_timeout,
                ) as resp:
                    if resp.status != 200:
                        raw = await resp.text()
                        last_block = raw
                        if self._is_client_blocked(raw) and attempt + 1 < attempts:
                            retry = True
                        elif (
                            self._is_retryable_status(resp.status)
                            and attempt + 1 < attempts
                        ):
                            logger.warning(
                                "上游返回 HTTP %d，第 %d 次重试",
                                resp.status,
                                attempt + 2,
                            )
                            retry = True
                        else:
                            raise LLMError(
                                f"DeepSeek 调用失败：HTTP {resp.status} {raw}"
                            )
                    else:
                        async for raw_line in resp.content:
                            line = raw_line.decode("utf-8", "ignore").strip()
                            if not line or line.startswith(":"):
                                continue
                            if not line.startswith("data:"):
                                continue

                            data = line[len("data:") :].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                continue

                            error = chunk.get("error")
                            if error:
                                message = (
                                    str(error.get("message") or error)
                                    if isinstance(error, dict)
                                    else str(error)
                                )
                                last_block = message
                                logger.warning("上游流式返回错误：%s", message[:300])
                                if self._is_client_blocked(message):
                                    if not yielded and attempt + 1 < attempts:
                                        retry = True
                                        break
                                    raise LLMError(
                                        f"DeepSeek 拒绝了当前客户端：{message}"
                                    )
                                raise LLMError(f"上游返回错误：{message[:300]}")

                            choices = chunk.get("choices") or []
                            usage = chunk.get("usage")
                            if state is not None and isinstance(usage, dict) and usage:
                                state.usage = usage
                            if not choices:
                                continue
                            choice = choices[0] if isinstance(choices[0], dict) else {}
                            finish = choice.get("finish_reason")
                            if state is not None and finish:
                                state.finish_reason = finish
                            delta = choice.get("delta") or {}

                            reasoning = delta.get("reasoning_content") or delta.get(
                                "reasoning"
                            )
                            if reasoning:
                                if state is not None:
                                    state.reasoning_content += reasoning
                                if on_reasoning is not None:
                                    await on_reasoning(reasoning)

                            if state is not None:
                                for call in delta.get("tool_calls") or []:
                                    self._merge_tool_call(tool_slots, call)

                            content = delta.get("content")
                            if content and self._is_client_blocked(content):
                                last_block = content
                                if not yielded and attempt + 1 < attempts:
                                    retry = True
                                    break
                                raise LLMError(
                                    f"DeepSeek 拒绝了当前客户端：{content[:300]}"
                                )
                            if content:
                                yielded = True
                                yield content
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_block = str(exc)
                if not yielded and attempt + 1 < attempts:
                    logger.warning(
                        "流式连接中断（%s），第 %d 次重试", exc, attempt + 2
                    )
                    await asyncio.sleep(0.8)
                    continue
                raise LLMError(f"流式连接中断：{exc}") from exc

            if retry:
                logger.warning("API 限制当前客户端，换用备用客户端标识重试")
                await asyncio.sleep(0.6)
                continue
            if state is not None and tool_slots:
                state.tool_calls = [tool_slots[i] for i in sorted(tool_slots)]
            if not yielded and not tool_slots:
                finish = state.finish_reason if state is not None else ""
                if (
                    state is not None
                    and state.reasoning_content
                    and finish == "length"
                ):
                    raise LLMError("思考内容占满输出长度，未生成正文")
                logger.warning(
                    "上游返回空内容（finish_reason=%s），第 %d/%d 次尝试",
                    finish or "无",
                    attempt + 1,
                    attempts,
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.6)
                    continue
                raise LLMError("上游返回空内容，请稍后重试")
            return

        raise LLMError(f"DeepSeek 拒绝了当前客户端：{last_block[:300]}")

    @staticmethod
    def _merge_tool_call(slots: dict[int, dict], call: dict) -> None:
        try:
            index = int(call.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        slot = slots.setdefault(
            index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if call.get("id"):
            slot["id"] = call["id"]
        function = call.get("function")
        if not isinstance(function, dict):
            return
        if function.get("name"):
            slot["function"]["name"] += function["name"]
        if function.get("arguments"):
            slot["function"]["arguments"] += function["arguments"]
