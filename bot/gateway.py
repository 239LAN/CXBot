"""QQ 机器人 WebSocket 网关：鉴权、心跳、事件分发、自动重连。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Awaitable, Callable

import aiohttp

from bot.api import QQBotAPI
from bot.events import MessageEvent, parse_message_event
from bot.token import AccessTokenManager
from config import Config

logger = logging.getLogger(__name__)

EventHandler = Callable[[MessageEvent], Awaitable[None]]

# Gateway OpCode
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class GatewayClient:
    def __init__(
        self,
        config: Config,
        api: QQBotAPI,
        tokens: AccessTokenManager,
        on_message: EventHandler,
    ) -> None:
        self._config = config
        self._api = api
        self._tokens = tokens
        self._on_message = on_message
        self._seq: int | None = None
        self._session_id: str | None = None
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("网关连接异常，3 秒后重连")
            if not self._running:
                break
            await asyncio.sleep(3)

    def stop(self) -> None:
        self._running = False

    async def _connect_once(self) -> None:
        gateway_url = await self._api.get_gateway_url()
        token = await self._tokens.get()
        logger.info("连接网关：%s", gateway_url)

        async with self._api.session.ws_connect(gateway_url, heartbeat=None) as ws:
            logger.info("网关已连接")
            heartbeat_task: asyncio.Task | None = None
            try:
                async for raw in ws:
                    if raw.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(raw.data)
                        if payload.get("s") is not None:
                            self._seq = payload["s"]

                        if payload.get("op") == OP_HELLO:
                            interval = (
                                int((payload.get("d") or {}).get("heartbeat_interval") or 30000)
                                / 1000
                            )
                            await self._authenticate(ws, token)
                            heartbeat_task = asyncio.create_task(
                                self._heartbeat_loop(ws, interval)
                            )
                            continue

                        if await self._handle_payload(ws, payload):
                            break
                    elif raw.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task

    async def _handle_payload(
        self, ws: aiohttp.ClientWebSocketResponse, payload: dict
    ) -> bool:
        """返回 True 表示需要断开重连。"""
        op = payload.get("op")

        if op == OP_HEARTBEAT:
            await self._send_heartbeat(ws)
            return False

        if op == OP_HEARTBEAT_ACK:
            logger.debug("心跳 ACK")
            return False

        if op == OP_DISPATCH:
            await self._dispatch(payload)
            return False

        if op == OP_RECONNECT:
            logger.warning("服务端要求重连")
            await ws.close()
            return True

        if op == OP_INVALID_SESSION:
            logger.warning("会话无效，重新鉴权")
            self._session_id = None
            self._seq = None
            await ws.close()
            return True

        logger.debug("未处理的 OpCode：%s", op)
        return False

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse, token: str) -> None:
        if self._session_id and self._seq is not None:
            logger.info("尝试恢复会话 session_id=%s seq=%s", self._session_id, self._seq)
            await ws.send_json(
                {
                    "op": OP_RESUME,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": self._session_id,
                        "seq": self._seq,
                    },
                }
            )
            return

        logger.info("发送 Identify，intents=%s", self._config.intents)
        await ws.send_json(
            {
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": self._config.intents,
                    "shard": [0, 1],
                    "properties": {
                        "$os": "windows",
                        "$browser": "cxbot",
                        "$device": "cxbot",
                    },
                },
            }
        )

    async def _heartbeat_loop(
        self, ws: aiohttp.ClientWebSocketResponse, interval: float
    ) -> None:
        try:
            while not ws.closed:
                await asyncio.sleep(interval)
                await self._send_heartbeat(ws)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("心跳循环结束", exc_info=True)

    async def _send_heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_json({"op": OP_HEARTBEAT, "d": self._seq})

    async def _dispatch(self, payload: dict) -> None:
        event_type = payload.get("t")
        data = payload.get("d") or {}

        if event_type == "READY":
            self._session_id = data.get("session_id")
            user = data.get("user") or {}
            logger.info(
                "鉴权成功：机器人=%s session_id=%s",
                user.get("username"),
                self._session_id,
            )
            return

        if event_type == "RESUMED":
            logger.info("会话恢复完成")
            return

        event = parse_message_event(event_type, data)
        if not event:
            return

        try:
            await self._on_message(event)
        except Exception:
            logger.exception("处理消息失败：%s", event.msg_id)
