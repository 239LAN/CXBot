"""语音识别：调用 Fun-ASR 录音文件转写接口。

QQ 官方语音附件带 asr_refer_text 与 voice_wav_url；handler 优先使用参考文本，
缺失时仅在 config.asr_enabled 开启后，把公网可访问的 WAV / AMR 地址提交给
Fun-ASR 异步任务接口，轮询完成后再下载转写结果。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.0


class ASRError(RuntimeError):
    """语音转写失败，消息可以直接展示给用户。"""


class ASRClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=config.asr_timeout)

    @property
    def enabled(self) -> bool:
        return self._config.asr_enabled and bool(self._config.asr_api_key)

    @property
    def _submit_url(self) -> str:
        return f"{self._config.asr_base_url}/services/audio/asr/transcription"

    def _task_url(self, task_id: str) -> str:
        return f"{self._config.asr_base_url}/tasks/{task_id}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._config.asr_api_key}",
            "Content-Type": "application/json",
        }

    async def transcribe(self, url: str) -> str:
        """转写公网音频地址；失败时抛出 ASRError。"""
        if not self.enabled:
            raise ASRError("未配置语音识别")
        if not url:
            raise ASRError("音频地址为空")

        task_id = await self._submit(url)
        output = await self._wait(task_id)

        results = output.get("results") or []
        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("subtask_status") != "SUCCEEDED":
                continue
            transcription_url = str(item.get("transcription_url") or "").strip()
            if transcription_url:
                return await self._fetch(transcription_url)

        detail = ""
        for item in results:
            if isinstance(item, dict) and item.get("message"):
                detail = str(item["message"])
                break
        if detail:
            raise ASRError(f"识别任务失败：{detail}")
        raise ASRError("识别任务失败")

    async def _submit(self, url: str) -> str:
        parameters: dict = {"channel_id": [0]}
        if self._config.asr_language:
            parameters["language_hints"] = [self._config.asr_language]
        payload = {
            "model": self._config.asr_model,
            "input": {"file_urls": [url]},
            "parameters": parameters,
        }
        headers = {**self._headers(), "X-DashScope-Async": "enable"}

        try:
            async with self._session.post(
                self._submit_url,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                body = await response.text()
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("提交语音识别任务失败", exc_info=True)
            raise ASRError("语音识别服务连接失败") from exc

        if status != 200:
            logger.warning("提交语音识别任务返回 HTTP %s：%s", status, body[:500])
            raise ASRError(f"提交识别任务失败（HTTP {status}）")

        try:
            payload_data = json.loads(body)
        except ValueError as exc:
            logger.warning("提交语音识别任务返回非 JSON：%s", body[:500])
            raise ASRError("语音识别服务返回了无效响应") from exc

        output = payload_data.get("output") if isinstance(payload_data, dict) else None
        task_id = str((output or {}).get("task_id") or "").strip()
        if not task_id:
            logger.warning("语音识别任务缺少 task_id：%s", body[:500])
            raise ASRError("提交识别任务失败")
        return task_id

    async def _wait(self, task_id: str) -> dict:
        deadline = time.monotonic() + self._config.asr_timeout
        while True:
            try:
                async with self._session.get(
                    self._task_url(task_id),
                    headers=self._headers(),
                    timeout=self._timeout,
                ) as response:
                    body = await response.text()
                    status = response.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("查询语音识别任务失败", exc_info=True)
                raise ASRError("语音识别服务连接失败") from exc

            if status != 200:
                logger.warning("查询语音识别任务返回 HTTP %s：%s", status, body[:500])
                raise ASRError(f"查询识别任务失败（HTTP {status}）")

            try:
                payload = json.loads(body)
            except ValueError as exc:
                logger.warning("查询语音识别任务返回非 JSON：%s", body[:500])
                raise ASRError("语音识别服务返回了无效响应") from exc

            output = payload.get("output") if isinstance(payload, dict) else None
            output = output if isinstance(output, dict) else {}
            state = str(output.get("task_status") or "").upper()
            if state == "SUCCEEDED":
                return output
            if state in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise ASRError(f"识别任务失败（{state}）")
            if time.monotonic() >= deadline:
                raise ASRError("语音识别超时")
            await asyncio.sleep(POLL_INTERVAL)

    async def _fetch(self, url: str) -> str:
        try:
            async with self._session.get(url, timeout=self._timeout) as response:
                body = await response.text()
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("下载语音识别结果失败", exc_info=True)
            raise ASRError("下载识别结果失败") from exc

        if status != 200:
            logger.warning("下载语音识别结果返回 HTTP %s", status)
            raise ASRError(f"下载识别结果失败（HTTP {status}）")

        try:
            payload = json.loads(body)
        except ValueError as exc:
            logger.warning("语音识别结果不是 JSON：%s", body[:500])
            raise ASRError("语音识别结果无效") from exc

        transcripts = payload.get("transcripts") if isinstance(payload, dict) else None
        parts: list[str] = []
        for item in transcripts or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("transcript") or ""
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if not parts:
            raise ASRError("未识别到语音内容")
        return "\n".join(parts)
