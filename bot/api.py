"""QQ 开放平台 HTTP OpenAPI 客户端（v2）。"""

from __future__ import annotations

import hashlib
import json
import logging

import aiohttp

from bot.token import AccessTokenManager
from config import Config

logger = logging.getLogger(__name__)


class QQBotAPIError(RuntimeError):
    def __init__(self, status: int, code: object, message: str, path: str) -> None:
        super().__init__(f"QQ OpenAPI 调用失败：{path} HTTP {status} code={code} {message}")
        self.status = status
        self.code = code
        self.message = message
        self.path = path


class QQBotAPI:
    def __init__(
        self,
        config: Config,
        tokens: AccessTokenManager,
        session: aiohttp.ClientSession,
    ) -> None:
        self._config = config
        self._tokens = tokens
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=max(5.0, config.request_timeout))

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict:
        token = await self._tokens.get()
        url = f"{self._config.api_base}{path}"
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }
        async with self._session.request(
            method,
            url,
            json=payload,
            headers=headers,
            timeout=timeout or self._timeout,
        ) as resp:
            raw = await resp.text()

        data: dict = {}
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"message": raw}

        if not 200 <= resp.status < 300:
            raise QQBotAPIError(
                resp.status, data.get("code"), str(data.get("message") or raw), path
            )
        return data

    async def get_gateway_url(self) -> str:
        data = await self._request("GET", "/gateway")
        url = data.get("url")
        if not url:
            raise QQBotAPIError(200, None, "网关响应缺少 url", "/gateway")
        return url

    async def send_c2c_message(
        self,
        openid: str,
        content: str,
        msg_id: str | None = None,
        msg_seq: int = 1,
    ) -> dict:
        payload: dict = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = msg_seq
        return await self._request("POST", f"/v2/users/{openid}/messages", payload)

    async def stream_c2c_message(
        self,
        openid: str,
        content: str,
        *,
        input_state: int,
        index: int,
        stream_msg_id: str | None = None,
        msg_id: str | None = None,
        msg_seq: int = 1,
        content_type: str = "text",
    ) -> dict:
        """流式发送单聊消息。首片返回 id 即 stream_msg_id，后续分片需携带。"""
        payload: dict = {
            "input_mode": "replace",
            "input_state": input_state,
            "index": index,
            "content_type": content_type,
            "content_raw": content,
        }
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = msg_seq
        if stream_msg_id:
            payload["stream_msg_id"] = stream_msg_id
        return await self._request(
            "POST", f"/v2/users/{openid}/stream_messages", payload
        )

    async def send_group_message(
        self,
        group_openid: str,
        content: str,
        msg_id: str | None = None,
        msg_seq: int = 1,
    ) -> dict:
        payload: dict = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = msg_seq
        return await self._request("POST", f"/v2/groups/{group_openid}/messages", payload)

    async def upload_c2c_file(
        self,
        openid: str,
        *,
        file_name: str,
        file_data: str | None = None,
        upload_id: str | None = None,
        file_type: int = 4,
    ) -> str:
        """上传私有文件到单聊，返回 file_info。

        file_data 为 base64 二进制直传（≤5MB）；upload_id 为分片上传合并。
        """
        if not file_data and not upload_id:
            raise QQBotAPIError(
                200, None, "file_data 与 upload_id 至少提供一个", f"/v2/users/{openid}/files"
            )
        path = f"/v2/users/{openid}/files"
        payload: dict = {
            "file_type": file_type,
            "file_name": file_name,
            "srv_send_msg": False,
        }
        if file_data:
            payload["file_data"] = file_data
        if upload_id:
            payload["upload_id"] = upload_id
        data = await self._request(
            "POST",
            path,
            payload,
            timeout=aiohttp.ClientTimeout(total=60),
        )
        file_info = data.get("file_info")
        if not file_info:
            raise QQBotAPIError(200, None, "上传响应缺少 file_info", path)
        return str(file_info)

    async def _put_part(self, url: str, part: bytes) -> None:
        timeout = aiohttp.ClientTimeout(total=120)
        async with self._session.put(url, data=part, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                body = (await resp.text())[:200]
                raise QQBotAPIError(resp.status, None, f"分片上传失败：{body}", "presigned-url")

    async def upload_c2c_large_file(
        self,
        openid: str,
        *,
        file_name: str,
        data: bytes,
        file_type: int = 4,
    ) -> str:
        """分片上传大文件：预上传 → 逐片 PUT → 分片完成 → 合并。"""
        size = len(data)
        path = f"/v2/users/{openid}/upload_prepare"
        prepared = await self._request(
            "POST",
            path,
            {
                "file_type": file_type,
                "file_size": str(size),
                "file_name": file_name,
                "md5": hashlib.md5(data).hexdigest(),
                "sha1": hashlib.sha1(data).hexdigest(),
                "md5_10m": hashlib.md5(data[:10002432]).hexdigest(),
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        upload_id = str(prepared.get("upload_id") or "")
        parts = sorted(
            prepared.get("parts") or [], key=lambda item: int(item.get("index", 0))
        )
        if not upload_id or not parts:
            raise QQBotAPIError(200, None, "预上传响应缺少 upload_id 或 parts", path)

        offset = 0
        for part in parts:
            index = int(part.get("index", 0))
            block_size = int(part.get("block_size") or prepared.get("block_size") or 0)
            if block_size <= 0:
                raise QQBotAPIError(200, None, "预上传响应缺少 block_size", path)
            chunk = data[offset : offset + block_size]
            if not chunk:
                raise QQBotAPIError(200, None, f"分片 {index} 内容为空", path)

            await self._put_part(str(part["presigned_url"]), chunk)
            await self._request(
                "POST",
                f"/v2/users/{openid}/upload_part_finish",
                {
                    "upload_id": upload_id,
                    "part_index": index,
                    "block_size": str(len(chunk)),
                    "md5": hashlib.md5(chunk).hexdigest(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
            offset += len(chunk)

        if offset != size:
            raise QQBotAPIError(
                200, None, f"分片未覆盖完整文件（{offset}/{size}）", path
            )

        logger.info("分片上传完成：%s，%d 字节，%d 片", file_name, size, len(parts))
        return await self.upload_c2c_file(
            openid,
            file_name=file_name,
            upload_id=upload_id,
            file_type=file_type,
        )

    async def send_c2c_media(
        self,
        openid: str,
        file_info: str,
        msg_id: str | None = None,
        msg_seq: int = 1,
    ) -> dict:
        """发送富媒体消息（msg_type=7）。"""
        payload: dict = {"msg_type": 7, "media": {"file_info": file_info}}
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = msg_seq
        return await self._request("POST", f"/v2/users/{openid}/messages", payload)
