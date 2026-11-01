"""DeepSeek Chat API 客户端（OpenAI 兼容协议，支持思考模式与工具调用）。"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import aiohttp

from config import Config

logger = logging.getLogger(__name__)


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
    ) -> dict:
        messages = [{"role": "system", "content": self._config.system_prompt}]
        messages.extend(history)
        limit = self._config.deepseek_max_tokens
        if tools:
            limit = max(limit, self._config.deepseek_tool_max_tokens)
        effective_max = limit
        if max_tokens is not None:
            effective_max = min(limit, max(1, int(max_tokens)))
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

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._config.deepseek_api_key}",
            "Content-Type": "application/json",
        }

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
        )

        async with self._session.post(
            self._url, json=payload, headers=self._headers(), timeout=self._timeout
        ) as resp:
            raw = await resp.text()
            if resp.status != 200:
                raise LLMError(f"DeepSeek 调用失败：HTTP {resp.status} {raw}")

        try:
            data = json.loads(raw)
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"DeepSeek 响应解析失败：{raw}") from exc

        content = message.get("content")
        if not isinstance(content, str):
            content = ""
        if state is not None:
            state.tool_calls = message.get("tool_calls") or []
            state.reasoning_content = message.get("reasoning_content") or ""
            state.finish_reason = data["choices"][0].get("finish_reason") or ""
        if not content.strip() and not (state and state.tool_calls):
            raise LLMError(f"DeepSeek 返回空内容：{raw}")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return content.strip(), usage

    async def stream_chat(
        self,
        history: list[dict],
        tools: list[dict] | None = None,
        state: StreamState | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[str]:
        """流式对话，逐段产出正文增量。

        思考内容、工具调用与 usage 写入 state；不传 state 时只输出正文。
        """
        payload = self._build_payload(
            history,
            stream=True,
            tools=tools,
            max_tokens=max_tokens,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
        )
        tool_slots: dict[int, dict] = {}

        async with self._session.post(
            self._url, json=payload, headers=self._headers(), timeout=self._stream_timeout
        ) as resp:
            if resp.status != 200:
                raw = await resp.text()
                raise LLMError(f"DeepSeek 调用失败：HTTP {resp.status} {raw}")

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

                if state is not None:
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        state.reasoning_content += reasoning

                    for call in delta.get("tool_calls") or []:
                        self._merge_tool_call(tool_slots, call)

                content = delta.get("content")
                if content:
                    yield content

        if state is not None and tool_slots:
            state.tool_calls = [tool_slots[i] for i in sorted(tool_slots)]

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
