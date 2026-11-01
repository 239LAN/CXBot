"""Minecraft Java 版服务器状态探测（Server List Ping）。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import struct
import time
from dataclasses import dataclass

DEFAULT_PORT = 25565
MAX_ADDRESS_LENGTH = 200

# 传统 MOTD 的颜色/格式代码，例如 §a、§l
_LEGACY_CODE = re.compile(r"§[0-9A-FK-ORX]", re.IGNORECASE)


class MinecraftError(Exception):
    """探测失败。"""


@dataclass
class PingResult:
    address: str
    ping_ms: int
    motd: str


def parse_address(raw: str) -> tuple[str, int, str]:
    """解析 host[:port]，支持 [IPv6]:port。返回 (host, port, 展示地址)。"""
    display = (raw or "").strip()
    if not display:
        raise MinecraftError("地址为空")
    if len(display) > MAX_ADDRESS_LENGTH:
        raise MinecraftError("地址过长")

    host = display
    port = DEFAULT_PORT

    if display.startswith("["):
        end = display.find("]")
        if end <= 1:
            raise MinecraftError("IPv6 地址格式不正确")
        host = display[1:end]
        rest = display[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise MinecraftError("端口格式不正确")
            port = _parse_port(rest[1:])
    elif display.count(":") == 1:
        host, port_text = display.rsplit(":", 1)
        port = _parse_port(port_text)

    if not host or any(ch.isspace() for ch in host):
        raise MinecraftError("地址格式不正确")
    return host, port, display


def _parse_port(text: str) -> int:
    if not text.isdigit():
        raise MinecraftError("端口必须是数字")
    port = int(text)
    if not 1 <= port <= 65535:
        raise MinecraftError("端口超出范围")
    return port


def _pack_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    shift = 0
    for _ in range(5):
        raw = await reader.readexactly(1)
        byte = raw[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7
    raise MinecraftError("VarInt 长度非法")


def _read_varint_from(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(5):
        if offset >= len(data):
            raise MinecraftError("数据包不完整")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise MinecraftError("VarInt 长度非法")


def _pack_string(text: str) -> bytes:
    encoded = text.encode("utf-8")
    return _pack_varint(len(encoded)) + encoded


def _pack_packet(packet_id: int, payload: bytes = b"") -> bytes:
    body = _pack_varint(packet_id) + payload
    return _pack_varint(len(body)) + body


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    length = await _read_varint(reader)
    if length <= 0 or length > 8 * 1024 * 1024:
        raise MinecraftError("数据包长度非法")
    data = await reader.readexactly(length)
    packet_id, offset = _read_varint_from(data, 0)
    return packet_id, data[offset:]


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    length, offset = _read_varint_from(data, offset)
    if length < 0 or offset + length > len(data):
        raise MinecraftError("字符串长度非法")
    try:
        return data[offset:offset + length].decode("utf-8"), offset + length
    except UnicodeDecodeError as exc:
        raise MinecraftError("响应不是有效的 UTF-8") from exc


def plain_motd(description: object) -> str:
    """把聊天组件或字符串转成纯文本 MOTD。"""
    chunks: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                chunks.append(text)
            elif text is None and isinstance(node.get("translate"), str):
                chunks.append(node["translate"])
            extra = node.get("extra")
            if isinstance(extra, list):
                for item in extra:
                    walk(item)

    walk(description)
    raw = _LEGACY_CODE.sub("", "".join(chunks))
    raw = "".join(ch for ch in raw if ch in "\n\t" or ord(ch) >= 32)
    return raw.strip()


async def _resolve_public_ip(host: str, port: int, allow_private: bool) -> str:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if allow_private or literal.is_global:
            return str(literal)
        raise MinecraftError("只允许探测公网地址")

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise MinecraftError("域名解析失败") from exc

    for info in infos:
        candidate = str(info[4][0])
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if allow_private or address.is_global:
            return candidate
    raise MinecraftError("只允许探测公网地址")


async def _ping(
    display: str,
    host: str,
    port: int,
    allow_private: bool,
) -> PingResult:
    started = time.monotonic()
    target_ip = await _resolve_public_ip(host, port, allow_private)

    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_connection(target_ip, port)
        handshake = (
            _pack_varint(0)
            + _pack_string(host)
            + struct.pack(">H", port)
            + _pack_varint(1)
        )
        writer.write(_pack_packet(0, handshake))
        writer.write(_pack_packet(0))
        await writer.drain()

        packet_id, payload = await _read_packet(reader)
        if packet_id != 0:
            raise MinecraftError("服务器返回了意外的数据包")
        text, _ = _read_string(payload, 0)
        try:
            status = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MinecraftError("服务器状态不是有效 JSON") from exc
        if not isinstance(status, dict):
            raise MinecraftError("服务器状态格式不正确")

        motd = plain_motd(status.get("description", ""))

        ping_started = time.monotonic()
        try:
            writer.write(_pack_packet(1, struct.pack(">q", int(time.time() * 1000))))
            await writer.drain()
            pong_id, _ = await _read_packet(reader)
            if pong_id != 1:
                raise MinecraftError("服务器未返回 Pong")
            ping_ms = int(round((time.monotonic() - ping_started) * 1000))
        except (OSError, asyncio.IncompleteReadError, MinecraftError):
            # 有些服务器在状态响应后立即断开，退回握手总耗时
            ping_ms = int(round((time.monotonic() - started) * 1000))

        return PingResult(address=display, ping_ms=max(0, ping_ms), motd=motd)
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise MinecraftError("连接失败") from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def ping_server(
    address: str,
    timeout: float = 5.0,
    *,
    allow_private: bool = False,
) -> PingResult:
    """探测服务器；超时或失败抛出 MinecraftError。"""
    host, port, display = parse_address(address)
    try:
        return await asyncio.wait_for(
            _ping(display, host, port, allow_private), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        raise MinecraftError("连接超时") from exc
