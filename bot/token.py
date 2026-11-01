"""AccessToken 管理：自动获取并在临近过期时刷新。"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

from config import Config

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """获取 AccessToken 失败。"""


class AccessTokenManager:
    """QQ 开放平台 AccessToken，默认 7200 秒过期，提前 60 秒刷新。"""

    REFRESH_MARGIN = 60

    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - self.REFRESH_MARGIN:
                return self._token

            payload = {
                "appId": self._config.app_id,
                "clientSecret": self._config.client_secret,
            }
            timeout = aiohttp.ClientTimeout(total=self._config.request_timeout)
            async with self._session.post(
                self._config.token_url, json=payload, timeout=timeout
            ) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    raise AuthError(f"获取 AccessToken 失败：HTTP {resp.status} {raw}")

            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise AuthError(f"AccessToken 响应解析失败：{raw}") from exc

            token = data.get("access_token")
            if not token:
                raise AuthError(f"AccessToken 响应缺少字段：{raw}")

            expires_in = int(data.get("expires_in") or 7200)
            self._token = token
            self._expires_at = time.monotonic() + expires_in
            logger.info("已刷新 AccessToken，%s 秒后过期", expires_in)
            return self._token
