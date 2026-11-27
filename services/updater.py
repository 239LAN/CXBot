"""自动更新：检查 GitHub Release 并触发后台安装脚本。

生产环境由安装脚本写入 systemd path 单元：
机器人只负责把安装包地址写入 pending 文件，root 助手在独立单元里下载并执行安装脚本，
因此机器人自身重启不会中断更新。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path

import aiohttp

from bot import __version__
from config import Config

logger = logging.getLogger(__name__)

REPO = "239LAN/CXBot"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "CXBot-Updater",
}


class UpdateError(RuntimeError):
    pass


def parse_version(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", (text or "").lstrip("vV"))
    return tuple(int(item) for item in numbers) or (0,)


def is_cron(expr: str) -> bool:
    return len((expr or "").split()) == 5


def parse_schedule(expr: str) -> tuple[str, object]:
    """返回 ("interval", 秒) 或 ("cron", 表达式)。默认 1 小时。"""
    text = (expr or "").strip()
    if not text:
        return "interval", 3600
    if is_cron(text):
        return "cron", " ".join(text.split())
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", text.lower())
    if not match:
        logger.warning("无法解析更新间隔 %r，回退为 1 小时", text)
        return "interval", 3600
    amount = max(1, int(match.group(1)))
    unit = match.group(2) or "s"
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return "interval", max(60, amount * factor)


def _field_matches(field: str, value: int, maximum: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) <= 0:
                continue
            step = int(raw_step)
            if part in ("", "*"):
                if value % step == 0:
                    return True
                continue
        if part == "*":
            return True
        if "-" in part:
            start, _, end = part.partition("-")
            if start.isdigit() and end.isdigit():
                low, high = int(start), int(end)
                if low <= value <= high and (value - low) % step == 0:
                    return True
            continue
        if part.isdigit() and int(part) == value:
            return True
    return False


def cron_matches(expr: str, moment: dt.datetime) -> bool:
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    weekday = (moment.weekday() + 1) % 7  # 周一=1 … 周日=0
    dow = dow.replace("7", "0")
    return (
        _field_matches(minute, moment.minute, 59)
        and _field_matches(hour, moment.hour, 23)
        and _field_matches(dom, moment.day, 31)
        and _field_matches(month, moment.month, 12)
        and _field_matches(dow, weekday, 6)
    )


class Updater:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._lock = asyncio.Lock()
        self._pending = Path(config.update_pending_file)
        self._helper = Path(config.update_helper_script)

    @property
    def enabled(self) -> bool:
        return self._config.auto_update_enabled

    @property
    def available(self) -> bool:
        """生产安装脚本是否已装入后台更新助手。"""
        return self._helper.is_file()

    async def latest_release(self) -> tuple[str, str]:
        """返回 (版本号, 安装脚本下载地址)。"""
        try:
            async with self._session.get(
                RELEASE_API,
                headers=API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    raise UpdateError(f"GitHub 返回 HTTP {resp.status}")
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise UpdateError(f"网络错误：{exc}") from exc
        except (asyncio.TimeoutError, json.JSONDecodeError) as exc:
            raise UpdateError(f"请求失败：{exc}") from exc

        tag = str(data.get("tag_name") or "").strip()
        if not tag:
            raise UpdateError("响应缺少版本号")
        assets = data.get("assets") or []
        preferred = f"cxbot-{tag.lstrip('vV')}-install.sh"
        url = ""
        for asset in assets:
            name = str(asset.get("name") or "")
            if name == preferred and asset.get("browser_download_url"):
                url = str(asset["browser_download_url"])
                break
        if not url:
            for asset in assets:
                name = str(asset.get("name") or "")
                if name.endswith(".sh") and asset.get("browser_download_url"):
                    url = str(asset["browser_download_url"])
                    break
        if not url:
            raise UpdateError("该版本没有可用的安装脚本")
        return tag, url

    def _trigger(self, url: str) -> None:
        self._pending.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._pending.with_name(self._pending.name + ".tmp")
        tmp.write_text(url + "\n", encoding="utf-8")
        tmp.replace(self._pending)
        logger.info("已写入更新触发文件：%s", self._pending)

    async def check_and_update(self) -> str:
        """检查并触发更新，返回给管理员看的文字。"""
        if not self.available:
            return "当前环境未安装后台更新助手，无法执行一键更新。"
        async with self._lock:
            try:
                tag, url = await self.latest_release()
            except UpdateError as exc:
                return f"检查更新失败：{exc}"
            if parse_version(tag) <= parse_version(__version__):
                return f"已是最新版本 v{__version__}。"
            try:
                self._trigger(url)
            except OSError as exc:
                logger.exception("写入更新触发文件失败")
                return f"触发更新失败：{exc}"
            return (
                f"发现新版本 {tag}（当前 v{__version__}），"
                "已触发后台安装，服务稍后会自动重启。"
            )

    async def _auto_check(self) -> None:
        message = await self.check_and_update()
        level = logging.INFO if "已触发" in message or "最新" in message else logging.WARNING
        logger.log(level, "自动更新检查：%s", message)

    async def loop(self) -> None:
        if not self.enabled:
            logger.info("自动更新未启用（AUTO_UPDATE_ENABLED=off）")
            return
        mode, value = parse_schedule(self._config.auto_update_schedule)
        if mode == "cron":
            logger.info("自动更新已启用，按 cron 检查：%s", value)
            await self._cron_loop(str(value))
            return
        seconds = int(value)
        logger.info("自动更新已启用，每 %d 秒检查一次", seconds)
        while True:
            await asyncio.sleep(seconds)
            try:
                await self._auto_check()
            except Exception:
                logger.exception("自动更新检查失败")

    async def _cron_loop(self, expr: str) -> None:
        last_key = ""
        while True:
            await asyncio.sleep(20)
            now = dt.datetime.now()
            key = now.strftime("%Y%m%d%H%M")
            if key == last_key or not cron_matches(expr, now):
                continue
            last_key = key
            try:
                await self._auto_check()
            except Exception:
                logger.exception("自动更新检查失败")
