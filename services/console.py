"""本机网页人工回复控制台。

- 默认仍由 AI 自动回复，网页上可对单个会话开启“人工接管”
- 接管后新消息只进入控制台，等待人工回复
- 消息与接管状态持久化到 data/console.json
- 只监听 127.0.0.1，不对外网开放
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

from bot.api import QQBotAPI, QQBotAPIError
from bot.events import MessageEvent, split_text
from services.continuation import ContinuationStore

logger = logging.getLogger(__name__)

MAX_C2C_REPLIES = 4
MAX_GROUP_REPLIES = 5
PASSIVE_WINDOW = 280.0  # 被动回复有效期约 5 分钟，留一点余量
CODE_TTL = 30  # 验证码刷新周期（秒）
COOKIE_NAME = "cxbot_console"
DIRECT_UPLOAD_MAX_BYTES = 5 * 1024 * 1024  # 群聊富媒体与单聊直传上限
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
CONTINUE_HINT = "\n\n……\n\n发送[/继续]继续"


def _attachment_note(event: MessageEvent) -> str:
    parts: list[str] = []
    if event.image_attachments:
        parts.append(f"图片×{len(event.image_attachments)}")
    if event.document_attachments:
        parts.append(f"文件×{len(event.document_attachments)}")
    if event.voice_attachments:
        parts.append(f"语音×{len(event.voice_attachments)}")
    return f"（{'，'.join(parts)}）" if parts else ""


class ManualConsole:
    def __init__(
        self,
        api: QQBotAPI,
        path: Path,
        *,
        history: int = 200,
        max_chars: int = 500,
        host: str = "127.0.0.1",
        port: int = 8080,
        session_hours: float = 12.0,
        max_upload_mb: int = 20,
        continuations: ContinuationStore | None = None,
    ) -> None:
        self._api = api
        self._path = Path(path)
        self._history = max(20, history)
        self._max_chars = max(100, max_chars)
        self._max_upload_bytes = max(1, max_upload_mb) * 1024 * 1024
        self._continuations = continuations
        self._host = host
        self._port = port
        self._session_ttl = max(60, int(session_hours * 3600))
        self._secret = secrets.token_bytes(32)
        self._tokens: dict[str, float] = {}
        self._attempts: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()
        self._ws: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._sessions: dict[str, dict] = {}
        self._held: set[str] = set()
        self._live: dict[str, dict] = {}
        self._load()

    # ---------- 数据 ----------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("控制台数据读取失败：%s", self._path, exc_info=True)
            return
        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            for key, entry in sessions.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                messages = entry.get("messages")
                if not isinstance(messages, list):
                    messages = []
                self._sessions[key] = {
                    "messages": [m for m in messages if isinstance(m, dict)][
                        -self._history :
                    ],
                    "last": entry.get("last") if isinstance(entry.get("last"), dict) else {},
                    "unread": max(0, int(entry.get("unread") or 0)),
                }
        held = data.get("held")
        if isinstance(held, list):
            self._held = {item for item in held if isinstance(item, str)}

    def _save(self) -> None:
        payload = {
            "held": sorted(self._held),
            "sessions": self._sessions,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("控制台数据写入失败：%s", self._path)

    def _entry(self, key: str) -> dict:
        return self._sessions.setdefault(
            key, {"messages": [], "last": {}, "unread": 0}
        )

    def _append(self, key: str, message: dict) -> None:
        entry = self._entry(key)
        entry["messages"].append(message)
        entry["messages"] = entry["messages"][-self._history :]

    async def _broadcast(self, payload: dict) -> None:
        for ws in list(self._ws):
            try:
                await ws.send_json(payload)
            except Exception:
                self._ws.discard(ws)

    async def publish(self, key: str, kind: str, text: str) -> None:
        """实时推送 AI 的思考、工具调用与生成进度（不写入历史）。"""
        live = {"kind": kind, "text": text or "", "ts": time.time()}
        self._live[key] = live
        await self._broadcast({"type": "live", "key": key, "live": live})

    async def clear_live(self, key: str) -> None:
        if self._live.pop(key, None) is not None:
            await self._broadcast({"type": "live", "key": key, "live": None})

    # ---------- 验证码与登录 ----------

    def _code_for(self, window: int) -> str:
        digest = hmac.new(
            self._secret, str(window).encode(), hashlib.sha256
        ).digest()
        return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"

    def current_code(self) -> tuple[str, int]:
        """返回当前验证码与剩余有效秒数。"""
        now = time.time()
        window = int(now // CODE_TTL)
        remaining = CODE_TTL - int(now % CODE_TTL)
        return self._code_for(window), remaining

    def code_text(self) -> str:
        code, remaining = self.current_code()
        return (
            f"网页控制台验证码：{code}\n"
            f"{remaining} 秒后刷新，登录有效期 {self._session_ttl // 3600} 小时\n"
            f"打开 http://<服务器IP>:{self._port}/ 输入验证码登录"
        )

    def verify_code(self, code: str) -> bool:
        text = (code or "").strip()
        if not text:
            return False
        window = int(time.time() // CODE_TTL)
        # 允许上一周期，避免刚刷新时输入旧码直接失败
        return any(
            hmac.compare_digest(text, self._code_for(item))
            for item in (window, window - 1)
        )

    def _issue_token(self) -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        for key, exp in list(self._tokens.items()):
            if exp < now:
                self._tokens.pop(key, None)
        self._tokens[token] = now + self._session_ttl
        return token

    def _authorized(self, request: web.Request) -> bool:
        token = request.cookies.get(COOKIE_NAME) or ""
        exp = self._tokens.get(token)
        if not exp:
            return False
        if exp < time.time():
            self._tokens.pop(token, None)
            return False
        return True

    def _allow_attempt(self, ip: str) -> bool:
        """限制单个 IP 每分钟的验证码尝试次数。"""
        now = time.time()
        count, start = self._attempts.get(ip, (0, now))
        if now - start > 60:
            count, start = 0, now
        if count >= 8:
            return False
        self._attempts[ip] = (count + 1, start)
        return True

    # ---------- 供 bot 调用 ----------

    async def submit(self, event: MessageEvent) -> bool:
        """记录收到的消息。返回该会话是否处于人工接管状态。"""
        text = (event.content or "").strip()
        note = _attachment_note(event)
        if note:
            text = f"{text}\n{note}".strip() if text else note
        message = {
            "dir": "in",
            "sender": "user",
            "text": text or "（空消息）",
            "ts": time.time(),
            "user": event.user_openid,
        }
        key = event.context_key
        async with self._lock:
            entry = self._entry(key)
            self._append(key, message)
            entry["unread"] += 1
            entry["last"] = {
                "msg_id": event.msg_id,
                "kind": event.kind,
                "user_openid": event.user_openid,
                "group_openid": event.group_openid or "",
                "msg_seq": 1,
                "ts": time.time(),
            }
            self._save()
            held = key in self._held
        await self._broadcast({"type": "message", "key": key, "held": held})
        return held

    async def record_outbound(
        self, event: MessageEvent, text: str, replies: int = 1
    ) -> None:
        """记录 AI 发出的回复，便于网页看到完整会话。"""
        if not (text or "").strip():
            return
        key = event.context_key
        message = {
            "dir": "out",
            "sender": "ai",
            "text": text,
            "ts": time.time(),
            "user": "bot",
        }
        async with self._lock:
            entry = self._entry(key)
            self._append(key, message)
            last = entry.get("last") or {}
            if last.get("msg_id") == event.msg_id:
                last["msg_seq"] = int(last.get("msg_seq") or 1) + max(1, replies)
                entry["last"] = last
            self._save()
        await self._broadcast({"type": "message", "key": key, "held": key in self._held})

    async def send(self, key: str, text: str) -> str:
        """人工发送回复，失败时返回错误信息。"""
        text = (text or "").strip()
        if not text:
            return "内容不能为空"
        async with self._lock:
            entry = self._sessions.get(key)
            last = dict(entry.get("last") or {}) if entry else {}
        if not last.get("msg_id"):
            return "该会话没有可回复的消息"
        if time.time() - float(last.get("ts") or 0) > PASSIVE_WINDOW:
            return "这条消息已超过 5 分钟，无法被动回复，请等用户发新消息"

        kind = last.get("kind") or "c2c"
        max_replies = MAX_GROUP_REPLIES if kind == "group" else MAX_C2C_REPLIES
        seq = int(last.get("msg_seq") or 1)
        available = max_replies - seq + 1
        if available <= 0:
            return "这条消息的被动回复次数已用完，请等用户发新消息"

        capacity = self._max_chars * available
        head = text
        rest = ""
        if len(text) > capacity:
            cut = max(1, capacity - len(CONTINUE_HINT) - 32)
            head = text[:cut].rstrip() or text[:cut]
            rest = text[cut:].lstrip("\n")
        chunks = split_text(
            head + (CONTINUE_HINT if rest else ""), self._max_chars, available
        )
        if not chunks:
            return "内容不能为空"

        sent = 0
        try:
            for chunk in chunks:
                if kind == "group":
                    await self._api.send_group_message(
                        last.get("group_openid") or "",
                        chunk,
                        msg_id=last["msg_id"],
                        msg_seq=seq,
                        markdown=False,
                    )
                else:
                    await self._api.send_c2c_message(
                        last.get("user_openid") or "",
                        chunk,
                        msg_id=last["msg_id"],
                        msg_seq=seq,
                        markdown=False,
                    )
                sent += 1
                seq += 1
        except QQBotAPIError as exc:
            if not sent:
                return f"发送失败：{exc.message}"
            logger.warning("人工回复部分发送失败：%s", exc.message)

        if rest and self._continuations is not None:
            self._continuations.put(key, rest)

        async with self._lock:
            entry = self._entry(key)
            if (entry.get("last") or {}).get("msg_id") == last["msg_id"]:
                entry["last"]["msg_seq"] = seq
            self._append(
                key,
                {
                    "dir": "out",
                    "sender": "operator",
                    "text": text,
                    "ts": time.time(),
                    "user": "operator",
                },
            )
            self._save()
        await self._broadcast({"type": "message", "key": key, "held": key in self._held})
        return ""

    async def send_media(
        self,
        key: str,
        file_name: str,
        data: bytes,
        *,
        content_type: str = "",
        caption: str = "",
    ) -> str:
        """人工发送图片或文件，失败时返回错误信息。"""
        file_name = (file_name or "file").strip() or "file"
        caption = (caption or "").strip()
        if not data:
            return "文件内容为空"
        if len(data) > self._max_upload_bytes:
            limit = self._max_upload_bytes // (1024 * 1024)
            return f"文件过大，控制台上限 {limit}MB"

        async with self._lock:
            entry = self._sessions.get(key)
            last = dict(entry.get("last") or {}) if entry else {}
        if not last.get("msg_id"):
            return "该会话没有可回复的消息"
        if time.time() - float(last.get("ts") or 0) > PASSIVE_WINDOW:
            return "这条消息已超过 5 分钟，无法被动回复，请等用户发新消息"

        kind = last.get("kind") or "c2c"
        max_replies = MAX_GROUP_REPLIES if kind == "group" else MAX_C2C_REPLIES
        seq = int(last.get("msg_seq") or 1)
        available = max_replies - seq + 1
        need = 2 if caption else 1
        if available < need:
            return "这条消息的被动回复次数不足，请等用户发新消息"

        is_image = content_type.lower().startswith("image/") or (
            Path(file_name).suffix.lower() in IMAGE_SUFFIXES
        )
        file_type = 1 if is_image else 4
        if kind == "group" and len(data) > DIRECT_UPLOAD_MAX_BYTES:
            # 先校验再发附带文字，避免文字已发出但文件失败
            return "群聊暂不支持发送超过 5MB 的文件"

        try:
            if caption:
                if kind == "group":
                    await self._api.send_group_message(
                        last.get("group_openid") or "",
                        caption,
                        msg_id=last["msg_id"],
                        msg_seq=seq,
                        markdown=False,
                    )
                else:
                    await self._api.send_c2c_message(
                        last.get("user_openid") or "",
                        caption,
                        msg_id=last["msg_id"],
                        msg_seq=seq,
                        markdown=False,
                    )
                seq += 1

            if kind == "group":
                file_info = await self._api.upload_group_file(
                    last.get("group_openid") or "",
                    file_name=file_name,
                    file_data=base64.b64encode(data).decode("ascii"),
                    file_type=file_type,
                )
                await self._api.send_group_media(
                    last.get("group_openid") or "",
                    file_info,
                    msg_id=last["msg_id"],
                    msg_seq=seq,
                )
            else:
                if len(data) <= DIRECT_UPLOAD_MAX_BYTES:
                    file_info = await self._api.upload_c2c_file(
                        last.get("user_openid") or "",
                        file_name=file_name,
                        file_data=base64.b64encode(data).decode("ascii"),
                        file_type=file_type,
                    )
                else:
                    file_info = await self._api.upload_c2c_large_file(
                        last.get("user_openid") or "",
                        file_name=file_name,
                        data=data,
                        file_type=file_type,
                    )
                await self._api.send_c2c_media(
                    last.get("user_openid") or "",
                    file_info,
                    msg_id=last["msg_id"],
                    msg_seq=seq,
                )
            seq += 1
        except QQBotAPIError as exc:
            logger.warning("人工发送媒体失败：%s", exc.message)
            return f"发送失败：{exc.message}"

        label = "图片" if is_image else "文件"
        async with self._lock:
            entry = self._entry(key)
            if (entry.get("last") or {}).get("msg_id") == last["msg_id"]:
                entry["last"]["msg_seq"] = seq
            if caption:
                self._append(
                    key,
                    {
                        "dir": "out",
                        "sender": "operator",
                        "text": caption,
                        "ts": time.time(),
                        "user": "operator",
                    },
                )
            self._append(
                key,
                {
                    "dir": "out",
                    "sender": "operator",
                    "text": f"[{label}] {file_name}（{len(data)} 字节）",
                    "ts": time.time(),
                    "user": "operator",
                },
            )
            self._save()
        await self._broadcast({"type": "message", "key": key, "held": key in self._held})
        return ""

    # ---------- 网页接口 ----------

    def sessions(self) -> list[dict]:
        items: list[dict] = []
        for key, entry in self._sessions.items():
            messages = entry.get("messages") or []
            last = messages[-1] if messages else {}
            last_event = entry.get("last") or {}
            items.append(
                {
                    "key": key,
                    "kind": last_event.get("kind") or key.split(":", 1)[0],
                    "preview": (last.get("text") or "")[:80],
                    "updated_at": last.get("ts") or 0,
                    "unread": int(entry.get("unread") or 0),
                    "held": key in self._held,
                    "live": self._live.get(key),
                }
            )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return items

    async def messages(self, key: str) -> list[dict]:
        async with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                return []
            if entry.get("unread"):
                entry["unread"] = 0
                self._save()
            return list(entry.get("messages") or [])

    async def set_held(self, key: str, held: bool) -> None:
        async with self._lock:
            if held:
                self._held.add(key)
            else:
                self._held.discard(key)
            self._save()
        await self._broadcast({"type": "state", "key": key, "held": held})

    async def release_all(self) -> None:
        """管理员退出控制台时解除所有会话的人工接管。"""
        async with self._lock:
            if not self._held:
                return
            held = sorted(self._held)
            self._held.clear()
            self._save()
        logger.info("控制台退出，已解除人工接管：%d 个会话", len(held))
        await self._broadcast({"type": "state", "held": None})
        for key in held:
            await self._broadcast({"type": "state", "key": key, "held": False})

    def _index_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "webapp" / "index.html"

    async def _index(self, request: web.Request) -> web.StreamResponse:
        path = self._index_path()
        if not path.is_file():
            return web.Response(status=404, text="webapp/index.html 不存在")
        return web.FileResponse(path)

    async def _api_auth(self, request: web.Request) -> web.Response:
        return web.json_response({"authed": self._authorized(request)})

    async def _api_login(self, request: web.Request) -> web.Response:
        ip = request.remote or "unknown"
        if not self._allow_attempt(ip):
            return web.json_response(
                {"ok": False, "error": "尝试次数过多，请稍后再试"}, status=429
            )
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)
        code = str(payload.get("code") or "")
        if not self.verify_code(code):
            return web.json_response(
                {"ok": False, "error": "验证码错误或已过期"}, status=401
            )
        self._attempts.pop(ip, None)
        token = self._issue_token()
        response = web.json_response({"ok": True})
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=self._session_ttl,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        return response

    async def _api_logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(COOKIE_NAME) or ""
        was_authed = token in self._tokens
        self._tokens.pop(token, None)
        if was_authed:
            # 只有已登录会话主动退出才解除接管，避免未授权请求误清状态
            await self.release_all()
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def _api_sessions(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "未登录"}, status=401)
        return web.json_response({"sessions": self.sessions()})

    async def _api_messages(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "未登录"}, status=401)
        key = request.query.get("key") or ""
        return web.json_response(
            {"messages": await self.messages(key), "live": self._live.get(key)}
        )

    async def _api_send(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "未登录"}, status=401)
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)
        key = str(payload.get("key") or "")
        text = str(payload.get("text") or "")
        if not key:
            return web.json_response({"ok": False, "error": "缺少会话标识"}, status=400)
        error = await self.send(key, text)
        if error:
            return web.json_response({"ok": False, "error": error}, status=400)
        return web.json_response({"ok": True})

    async def _api_send_media(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "未登录"}, status=401)
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)
        key = str(payload.get("key") or "")
        if not key:
            return web.json_response({"ok": False, "error": "缺少会话标识"}, status=400)
        try:
            data = base64.b64decode(str(payload.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            return web.json_response({"ok": False, "error": "文件内容编码错误"}, status=400)
        error = await self.send_media(
            key,
            str(payload.get("name") or "file"),
            data,
            content_type=str(payload.get("type") or ""),
            caption=str(payload.get("caption") or ""),
        )
        if error:
            return web.json_response({"ok": False, "error": error}, status=400)
        return web.json_response({"ok": True})

    async def _api_takeover(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "未登录"}, status=401)
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)
        key = str(payload.get("key") or "")
        if not key:
            return web.json_response({"ok": False, "error": "缺少会话标识"}, status=400)
        await self.set_held(key, bool(payload.get("held")))
        return web.json_response({"ok": True})

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        if not self._authorized(request):
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        self._ws.add(ws)
        try:
            await ws.send_json({"type": "state", "held": None})
            async for _ in ws:
                pass
        finally:
            self._ws.discard(ws)
            if not self._ws:
                # 关闭网页（无其他在线终端）时停止接管，恢复 AI 自动回复
                await self.release_all()
        return ws

    def _build_app(self) -> web.Application:
        app = web.Application(client_max_size=self._max_upload_bytes * 2)
        app.router.add_get("/", self._index)
        app.router.add_get("/api/session", self._api_auth)
        app.router.add_post("/api/login", self._api_login)
        app.router.add_post("/api/logout", self._api_logout)
        app.router.add_get("/api/sessions", self._api_sessions)
        app.router.add_get("/api/messages", self._api_messages)
        app.router.add_post("/api/send", self._api_send)
        app.router.add_post("/api/send_media", self._api_send_media)
        app.router.add_post("/api/takeover", self._api_takeover)
        app.router.add_get("/ws", self._ws_handler)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self._build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("网页控制台：http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        for ws in list(self._ws):
            await ws.close()
        self._ws.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
