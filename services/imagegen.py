"""图片生成：OpenAI 兼容 /images/generations，支持配置多个模型由 AI 选择。

模型清单来自 IMAGE_GEN_MODELS（「模型ID|倍率|难度说明」用分号分隔），
扣费 = 基准积分 × 模型倍率 × 图片张数；
工具 schema 里会把模型与相对成本给模型参考；
模型名属于内部信息，回复中禁止向用户透露。
"""

from __future__ import annotations

import base64
import json
import logging
import math

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 2000
MAX_URL_DOWNLOAD = 20 * 1024 * 1024

TOOL_NAME = "generate_image"


class ImageError(RuntimeError):
    """图片生成失败，错误信息会作为工具结果返回给模型。"""


def _sniff_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return ""


def _decode_b64(value: str) -> bytes:
    raw = value.strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    return base64.b64decode(raw)


class ImageClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._models = list(config.image_gen_models)
        self._sizes = list(config.image_gen_sizes)
        self._timeout = aiohttp.ClientTimeout(total=config.image_gen_timeout)

    @property
    def enabled(self) -> bool:
        return self._config.image_gen_enabled and bool(self._models)

    @property
    def credits(self) -> int:
        return self._config.image_gen_credits

    def rate(self, model: str) -> float:
        for name, rate, _desc in self._models:
            if name == model:
                return rate
        return 1.0

    def cost(self, model: str, count: int = 1) -> int:
        """扣费 = 基准积分 × 模型倍率 × 图片张数，向上取整。"""
        return math.ceil(self.credits * self.rate(model) * max(1, count))

    def _default_model(self) -> str:
        return self._models[0][0] if self._models else ""

    def _default_size(self) -> str:
        return self._sizes[0] if self._sizes else "1024x1024"

    def schemas(self) -> list[dict]:
        if not self.enabled:
            return []
        models_desc = "；".join(
            f"{name}（{desc or f'成本倍率 {rate:g}'}）"
            for name, rate, desc in self._models
        )
        description = (
            "根据文字描述生成一张图片并直接发给用户。"
            f"计费为基准 {self.credits} 积分 × 所选模型倍率，倍率越高通常画质越好、消耗越多。"
            f"可用模型（按成本从低到高）：{models_desc}。"
            "请根据任务难度自行选择 model，不要询问用户用哪个模型；"
            "模型名称是内部信息，禁止在回复中向用户透露，也不允许用户指定模型。"
            "prompt 要具体：主体、风格、构图、光线、画面内文字等；"
            "用户要求画图时直接调用，不必先反问。"
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": f"画面描述，不超过 {MAX_PROMPT_CHARS} 字",
                            },
                            "model": {
                                "type": "string",
                                "enum": [name for name, _rate, _desc in self._models],
                                "description": "按任务难度自行选择，用户不能指定",
                            },
                            "size": {
                                "type": "string",
                                "enum": self._sizes,
                                "description": f"图片尺寸，默认 {self._default_size()}",
                            },
                        },
                        "required": ["prompt", "model"],
                    },
                },
            }
        ]

    def _sanitize(self, text: str) -> str:
        for name, _rate, _desc in self._models:
            if name:
                text = text.replace(name, "该模型")
        return text[:300]

    def parse(self, arguments: str) -> tuple[str, str, str]:
        """解析工具参数，返回 (prompt, model, size)；不合法时抛 ImageError。"""
        try:
            args = json.loads(arguments or "{}")
        except ValueError as exc:
            raise ImageError("参数不是合法 JSON") from exc
        if not isinstance(args, dict):
            raise ImageError("参数必须是 JSON 对象")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ImageError("缺少画面描述 prompt")
        if len(prompt) > MAX_PROMPT_CHARS:
            prompt = prompt[:MAX_PROMPT_CHARS]
        model = str(args.get("model") or "").strip()
        if model not in {name for name, _rate, _desc in self._models}:
            model = self._default_model()
        size = str(args.get("size") or "").strip()
        if size not in self._sizes:
            size = self._default_size()
        return prompt, model, size

    async def generate(self, prompt: str, model: str, size: str) -> tuple[bytes, str]:
        """调用图片接口，返回 (图片字节, 扩展名)。失败抛出 ImageError。"""
        if not self.enabled:
            raise ImageError("图片生成未启用")

        payload = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "response_format": "b64_json",
        }
        url = f"{self._config.image_gen_base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {self._config.image_gen_api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with self._session.post(
                url, json=payload, headers=headers, timeout=self._timeout
            ) as resp:
                text = await resp.text()
                status = resp.status
        except aiohttp.ClientError as exc:
            raise ImageError(f"图片接口连接失败：{exc}") from exc
        except TimeoutError as exc:
            raise ImageError("图片生成超时，请稍后重试或换更快的模型") from exc

        if status == 400 and "response_format" in text:
            # 部分网关不支持 response_format，退回默认返回格式再试一次
            payload.pop("response_format", None)
            try:
                async with self._session.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                ) as resp:
                    text = await resp.text()
                    status = resp.status
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise ImageError(f"图片接口调用失败：{exc}") from exc

        if status != 200:
            detail = ""
            try:
                body = json.loads(text)
                error = body.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or "")
                elif isinstance(error, str):
                    detail = error
                detail = detail or str(body.get("message") or "")
            except ValueError:
                detail = text
            detail = self._sanitize(detail).strip()
            raise ImageError(f"图片生成失败（HTTP {status}）：{detail or '接口未返回原因'}")

        try:
            body = json.loads(text)
        except ValueError as exc:
            raise ImageError("图片接口返回的不是 JSON") from exc
        items = body.get("data")
        if not isinstance(items, list) or not items:
            raise ImageError("图片接口没有返回图片数据")
        first = items[0] if isinstance(items[0], dict) else {}

        data: bytes = b""
        if first.get("b64_json"):
            try:
                data = _decode_b64(str(first["b64_json"]))
            except (ValueError, TypeError) as exc:
                raise ImageError("图片 base64 解码失败") from exc
        elif first.get("url"):
            data = await self._download(str(first["url"]))
        if not data:
            raise ImageError("图片接口返回内容为空")
        if len(data) > self._config.image_gen_max_bytes:
            raise ImageError("生成的图片超过大小上限")

        ext = _sniff_ext(data)
        if not ext:
            raise ImageError("接口返回的不是可识别的图片格式")
        return data, ext

    async def _download(self, url: str) -> bytes:
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    raise ImageError(f"图片下载失败（HTTP {resp.status}）")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > MAX_URL_DOWNLOAD:
                        raise ImageError("图片下载超过大小上限")
                    chunks.append(chunk)
                return b"".join(chunks)
        except aiohttp.ClientError as exc:
            raise ImageError(f"图片下载失败：{exc}") from exc
