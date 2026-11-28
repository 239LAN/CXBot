"""以 / 开头的聊天指令。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from bot import __author__, __version__
from bot.events import MessageEvent
from config import Config
from services.admins import AdminStore, normalize_identifier
from services.blocklist import BlockStore
from services.minecraft import MinecraftError, ping_server
from services.preferences import PreferenceStore
from services.quota import QuotaManager
from services.reset_codes import ResetCodeError, ResetCodeStore
from services.workspace import WorkspaceTools

PERIOD_LABELS = (("day", "今日"), ("week", "本周"), ("month", "本月"))
THINKING_VALUES = {"off", "low", "medium", "high", "max", "default"}
THINKING_LABELS = {
    "off": "关闭",
    "low": "低",
    "medium": "中",
    "high": "高",
    "max": "最高",
}
PROGRESS_VALUES = {
    "off": None,
    "none": None,
    "关闭": None,
    "不显示": None,
    "default": None,
    "on": "on",
    "calls": "on",
    "brief": "on",
    "仅调用": "on",
    "开启": "on",
}

GROUP_SETTINGS_DENIED = "只有群主、群管理员或 Bot 管理员可以修改本群设置。"


def _parse_tokens(raw: str) -> int | None:
    """解析 4096、32k 这类 token 数量。"""
    text = (raw or "").strip().lower().replace("_", "")
    multiplier = 1
    if text.endswith("k"):
        text = text[:-1]
        multiplier = 1000
    if not text.isdigit():
        return None
    value = int(text) * multiplier
    return value if value > 0 else None

HELP_COMMON = """基础：
/help — 查看帮助
/about — 查看版本、开源协议与当前设置
/id — 查看当前会话标识
/quota — 查看剩余额度
/quota detail — 查看各周期已用/上限
/workspace — 查看当前会话空间占用
/reset — 清空当前对话上下文
/continue — 继续发送被截断的内容
/cancel — 取消当前会话正在进行的任务
/redeem <重置码> — 恢复自己的额度
/motd <地址> — 探测 Minecraft Java 服务器（支持 SRV）

对话设置：
/markdown — 查看 Markdown 渲染状态
/markdown on|off|default — 开/关 Markdown 渲染
/thinking — 查看思考强度与思考显示
/thinking off|low|medium|high|max|default — 调整思考强度
/thinking show on|off|default — 开/关思考内容显示
/progress — 查看过程显示状态
/progress off|on|default — 关/开过程显示（只显示调用）
/length — 查看单次输出上限
/length <tokens>|default — 设置输出上限（不超过管理员上限）
/context — 查看上下文预算
/context <tokens>|default — 设置上下文 token 上限（不超过管理员上限）
群聊中只有群主、群管理员或 Bot 管理员可以修改这些设置"""

HELP_PRIVATE = """私聊专属：
/stream — 查看流式输出状态
/stream on|off|default — 开/关流式输出"""

HELP_ADMIN = """管理员：
/gencode [数量] [天数] — 生成重置码
/resetquota [群标识] — 重置群额度（群聊默认本群）
/block <用户标识> — 拉黑用户
/unblock <用户标识> — 解除拉黑
/console — 获取网页控制台验证码（30 秒刷新）
/update — 检查并安装最新版本"""


def help_text(*, group: bool, admin: bool) -> str:
    parts = [HELP_COMMON]
    if not group:
        parts.append(HELP_PRIVATE)
    if admin:
        parts.append(HELP_ADMIN)
    return "\n\n".join(parts)


class CommandRouter:
    def __init__(
        self,
        config: Config,
        quota: QuotaManager,
        preferences: PreferenceStore,
        reset_codes: ResetCodeStore,
        admins: AdminStore,
        workspace: WorkspaceTools,
        blocks: BlockStore,
        updater=None,
    ) -> None:
        self._config = config
        self._quota = quota
        self._preferences = preferences
        self._reset_codes = reset_codes
        self._admins = admins
        self._workspace = workspace
        self._blocks = blocks
        self._updater = updater

    async def route(self, event: MessageEvent, text: str) -> str:
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        if command in ("/help", "/帮助"):
            return help_text(
                group=event.kind == "group",
                admin=self._admins.is_admin(event.user_openid),
            )
        if command in ("/about", "/关于", "/version", "/版本"):
            return await self._about_reply(event)
        if command in ("/quota", "/额度"):
            return await self._quota_reply(event, args)
        if command in ("/workspace", "/空间", "/files", "/文件"):
            return await self._workspace_reply(event)
        if command in ("/thinking", "/思考"):
            return await self._thinking_reply(event, args)
        if command in ("/progress", "/过程"):
            return await self._progress_reply(event, args)
        if command in ("/length", "/输出", "/长度"):
            return await self._length_reply(event, args)
        if command in ("/context", "/上下文"):
            return await self._context_reply(event, args)
        if command in ("/markdown", "/md", "/渲染"):
            return await self._markdown_reply(event, args)
        if command in ("/stream", "/流式"):
            return await self._stream_setting_reply(event, args)
        if command in ("/gencode", "/生成重置码"):
            return await self._gencode_reply(event, args)
        if command in ("/resetquota", "/重置额度"):
            return await self._reset_quota_reply(event, args)
        if command in ("/block", "/拉黑"):
            return self._block_reply(event, args)
        if command in ("/unblock", "/解除拉黑"):
            return self._unblock_reply(event, args)
        if command in ("/update", "/更新"):
            return await self._update_reply(event)
        if command == "/motd":
            return await self._motd_reply(args)
        if command in ("/redeem", "/兑换", "/重置码"):
            return await self._redeem_reply(event, args)
        if command == "/id":
            lines = [
                f"当前会话标识：{event.context_key}",
                f"你的用户标识：{event.user_openid}",
            ]
            if event.kind == "group":
                lines.append(f"群标识：{event.group_openid}")
            return "\n".join(lines)
        return "未知指令。发送 /help 查看可用指令。"

    async def _about_reply(self, event: MessageEvent) -> str:
        scope = event.context_key
        markdown = await self._preferences.get(scope, "markdown") != "off"
        thinking = await self._preferences.get(scope, "thinking")
        progress = await self._preferences.get(scope, "progress")
        reasoning = await self._preferences.get(scope, "reasoning")

        if event.kind == "c2c":
            stream = await self._preferences.get(scope, "stream")
            stream_label = "关闭" if stream == "off" else "开启"
            progress_label = self._progress_label(progress)
            reasoning_label = "开启" if reasoning == "on" else "关闭"
        else:
            stream_label = "不支持"
            progress_label = "不支持"
            reasoning_label = "不支持"

        lines = [
            f"CXBot v{__version__}",
            f"© {datetime.now().year} {__author__}",
            "",
            "新一代 QQ 机器人 AI 助手",
            "",
            "当前设置",
            f"Markdown　{'开启' if markdown else '关闭'}",
            f"流式输出　{stream_label}",
            f"过程显示　{progress_label}",
            f"思考强度　{self._thinking_label(thinking)}",
            f"思考显示　{reasoning_label}",
            "",
            "主要功能",
            "· 流式对话与长文续传",
            "· 文件读写与工作空间",
            "· 在线命令执行",
            "· 网页搜索与抓取",
            "· 文档解析、图片识别、语音转写",
            "· 群聊公用与额度管理",
            "",
            "通过 AGPL-3.0 协议开源",
            "请勿商业使用",
            "github.com/239LAN/CXBot",
            "",
            "发送 /help 查看指令",
        ]
        return "\n".join(lines)

    async def _quota_reply(self, event: MessageEvent, args: list[str]) -> str:
        if self._admins.is_admin(event.user_openid):
            if event.kind == "group":
                return "你是管理员，额度不限，使用不计费（本群额度不受影响）。"
            return "你是管理员，额度不限，使用不计费。"

        usage = await self._quota.usage(event.context_key)
        _remaining, reason = await self._quota.check(event.context_key)
        subject = "本群" if event.kind == "group" else "你"
        detailed = bool(args) and args[0].lower() in ("detail", "详情")

        if detailed:
            lines = [f"{subject}的额度使用详情："]
            for period, label in PERIOD_LABELS:
                item = usage[period]
                used = item["used"]
                limit = item["limit"]
                lines.append(
                    f"{label}：已用 {used} / {limit}，剩余 {max(0, limit - used)}"
                )
        else:
            lines = [f"{subject}的剩余额度："]
            for period, label in PERIOD_LABELS:
                item = usage[period]
                limit = item["limit"]
                lines.append(
                    f"{label}：剩余 {max(0, limit - item['used'])}（上限 {limit}）"
                )

        if reason:
            lines.append(reason)
        lines.append(
            "计费：1 积分 = 1 输出 token = 4 输入 token；10 积分 = 1MB 文件；"
            "沙箱 100 积分/秒；沙箱流量 1 积分/MB。"
        )
        return "\n".join(lines)

    async def _workspace_reply(self, event: MessageEvent) -> str:
        identifier = (
            event.context_key if event.kind == "group" else event.user_openid
        )
        quota = None
        if event.kind == "c2c" and self._admins.is_admin(event.user_openid):
            quota = self._config.workspace_admin_quota_bytes
        return await asyncio.to_thread(
            self._workspace.usage_text, identifier, quota_bytes=quota
        )

    async def _thinking_reply(self, event: MessageEvent, args: list[str]) -> str:
        scope = event.context_key
        current = await self._preferences.get(scope, "thinking")
        show = await self._preferences.get(scope, "reasoning") == "on"

        if not args:
            return (
                f"当前思考强度：{self._thinking_label(current)}\n"
                f"思考显示：{'开启' if show else '关闭'}"
            )

        value = args[0].lower()
        if value in ("show", "display", "显示"):
            if len(args) < 2 or args[1].lower() not in ("on", "off", "default"):
                return "用法：/thinking show on|off|default"
            if not self._can_change_settings(event):
                return GROUP_SETTINGS_DENIED
            mode = args[1].lower()
            await self._preferences.set(
                scope, "reasoning", "on" if mode == "on" else None
            )
            return f"思考显示已{'开启' if mode == 'on' else '关闭'}。"

        if value not in THINKING_VALUES:
            return "思考强度可选：off / low / medium / high / max / default"
        if not self._can_change_settings(event):
            return GROUP_SETTINGS_DENIED
        await self._preferences.set(
            scope, "thinking", None if value == "default" else value
        )
        if value == "default":
            return f"已恢复默认思考强度（{self._default_thinking()}）。"
        if value == "off":
            return "已关闭思考模式。"
        note = "（low/medium 可能被官方映射为 high）" if value in ("low", "medium") else ""
        return f"思考强度已设为 {value}{note}。"

    async def _progress_reply(
        self, event: MessageEvent, args: list[str]
    ) -> str:
        scope = event.context_key
        current = await self._preferences.get(scope, "progress")
        if not args:
            suffix = "（默认）" if current is None else ""
            return (
                f"过程显示：{self._progress_label(current)}{suffix}\n"
                "可设为 off / on"
            )
        value = args[0].lower()
        if value not in PROGRESS_VALUES:
            return "用法：/progress on|off|default"
        if not self._can_change_settings(event):
            return GROUP_SETTINGS_DENIED
        stored = PROGRESS_VALUES[value]
        await self._preferences.set(scope, "progress", stored)
        if stored is None:
            return "过程显示已关闭。"
        return "过程显示已开启：回复中显示命令与工具调用。"

    async def _length_reply(
        self, event: MessageEvent, args: list[str]
    ) -> str:
        scope = event.context_key
        cap = self._config.admin_max_tokens
        current = await self._preferences.get(scope, "length")
        if not args:
            value = current or str(cap)
            suffix = "（默认）" if current is None else ""
            return (
                f"单次输出上限：{value} tokens{suffix}\n"
                f"可设为 256-{cap}，例如 /length 4096"
            )
        if not self._can_change_settings(event):
            return GROUP_SETTINGS_DENIED
        raw = args[0].lower()
        if raw == "default":
            await self._preferences.set(scope, "length", None)
            return f"单次输出上限已恢复默认（{cap} tokens）。"
        value = _parse_tokens(raw)
        if value is None:
            return "用法：/length <tokens>|default，例如 /length 4096"
        value = max(256, min(cap, value))
        await self._preferences.set(scope, "length", str(value))
        return f"单次输出上限已设为 {value} tokens（管理员上限 {cap}）。"

    async def _context_reply(
        self, event: MessageEvent, args: list[str]
    ) -> str:
        scope = event.context_key
        cap = self._config.admin_context_tokens
        if self._admins.is_admin(event.user_openid):
            default = cap
        else:
            default = self._config.context_max_tokens
        current = await self._preferences.get(scope, "context")
        if not args:
            value = current or str(default)
            suffix = "（默认）" if current is None else ""
            return (
                f"上下文预算：{value} tokens{suffix}\n"
                f"可设为 1024-{cap}，例如 /context 32000"
            )
        if not self._can_change_settings(event):
            return GROUP_SETTINGS_DENIED
        raw = args[0].lower()
        if raw == "default":
            await self._preferences.set(scope, "context", None)
            return f"上下文预算已恢复默认（{default} tokens）。"
        value = _parse_tokens(raw)
        if value is None:
            return "用法：/context <tokens>|default，例如 /context 32000"
        value = max(1024, min(cap, value))
        await self._preferences.set(scope, "context", str(value))
        return f"上下文预算已设为 {value} tokens（管理员上限 {cap}）。"

    async def _markdown_reply(
        self, event: MessageEvent, args: list[str]
    ) -> str:
        scope = event.context_key
        current = await self._preferences.get(scope, "markdown")
        enabled = current != "off"
        if not args:
            suffix = "（默认）" if current is None else ""
            return f"Markdown 渲染：{'开启' if enabled else '关闭'}{suffix}"
        value = args[0].lower()
        if value not in ("on", "off", "default"):
            return "用法：/markdown on|off|default"
        if not self._can_change_settings(event):
            return GROUP_SETTINGS_DENIED
        await self._preferences.set(
            scope, "markdown", "off" if value == "off" else None
        )
        if value == "off":
            return "已关闭 Markdown 渲染，之后的回复将使用纯文本。"
        return "已开启 Markdown 渲染。"

    async def _stream_setting_reply(
        self, event: MessageEvent, args: list[str]
    ) -> str:
        if event.kind != "c2c":
            return "群聊不支持流式输出，该设置仅在私聊生效。"

        scope = event.context_key
        current = await self._preferences.get(scope, "stream")
        enabled = current != "off"

        if not args:
            suffix = "（默认）" if current is None else ""
            return f"流式输出：{'开启' if enabled else '关闭'}{suffix}"

        value = args[0].lower()
        if value not in ("on", "off", "default"):
            return "用法：/stream on|off|default"
        await self._preferences.set(scope, "stream", "off" if value == "off" else None)
        return f"流式输出已{'关闭' if value == 'off' else '开启'}。"

    async def _motd_reply(self, args: list[str]) -> str:
        if not args:
            return "用法：/motd <地址>，例如 /motd mc.hypixel.net"
        try:
            result = await ping_server(args[0])
        except MinecraftError as exc:
            return f"查服失败：{exc}"

        lines = [f"{result.address}  {result.ping_ms}ms"]
        meta: list[str] = []
        if result.version:
            meta.append(f"版本 {result.version}")
        if result.players_max is not None:
            online = result.players_online or 0
            meta.append(f"在线 {online}/{result.players_max}")
        if result.srv:
            meta.append("SRV")
        if meta:
            lines.append("  ".join(meta))
        lines.append(result.motd or "(无 MOTD)")
        return "\n".join(lines)

    async def _redeem_reply(self, event: MessageEvent, args: list[str]) -> str:
        if not args:
            return "用法：/redeem <重置码>"
        ok, reason = await self._reset_codes.redeem(args[0], event.context_key)
        if not ok:
            return f"兑换失败：{reason}"

        await self._quota.reset(event.context_key)
        usage = await self._quota.usage(event.context_key)
        subject = "本群" if event.kind == "group" else "你"
        lines = [f"兑换成功，{subject}的额度已重置："]
        for period, label in PERIOD_LABELS:
            item = usage[period]
            lines.append(f"{label}：剩余 {item['limit']} / {item['limit']}")
        return "\n".join(lines)

    def _scope(self, raw: str) -> str:
        value = (raw or "").strip()
        if not value:
            return ""
        if ":" in value:
            return value
        return f"group:{value}"

    async def _gencode_reply(
        self, event: MessageEvent, args: list[str]
    ) -> str:
        if not self._admins.is_admin(event.user_openid):
            return "仅管理员可以生成重置码。"
        try:
            count = int(args[0]) if args else 1
            days = int(args[1]) if len(args) > 1 else 30
        except ValueError:
            return "用法：/gencode [数量] [有效天数]"
        count = max(1, min(10, count))
        days = max(1, min(365, days))
        try:
            codes = await self._reset_codes.generate(count, days)
        except ResetCodeError as exc:
            return f"生成失败：{exc}"
        lines = ["已生成重置码（仅本次可见）：", *codes]
        lines.append(f"有效期 {days} 天；用户发送 /redeem <码> 兑换。")
        return "\n".join(lines)

    async def _reset_quota_reply(
        self, event: MessageEvent, args: list[str]
    ) -> str:
        if not self._admins.is_admin(event.user_openid):
            return "仅管理员可以重置额度。"
        if event.kind == "group" and not args:
            scope = event.context_key
        elif args:
            scope = self._scope(args[0])
        else:
            return "用法：/resetquota <群标识>"
        if not scope.startswith("group:"):
            return "只能重置群聊额度，请提供群标识。"
        await self._quota.reset(scope)
        return f"已重置群聊额度：{scope}"

    def _block_reply(self, event: MessageEvent, args: list[str]) -> str:
        if not self._admins.is_admin(event.user_openid):
            return "仅管理员可以拉黑用户。"
        if not args:
            return "用法：/block <用户标识>"
        target = normalize_identifier(args[0])
        if not target:
            return "用户标识无效。"
        if self._admins.is_admin(target):
            return "不能拉黑管理员。"
        added = self._blocks.add(target)
        return f"已拉黑：{target}" if added else f"已在黑名单：{target}"

    def _unblock_reply(self, event: MessageEvent, args: list[str]) -> str:
        if not self._admins.is_admin(event.user_openid):
            return "仅管理员可以解除拉黑。"
        if not args:
            return "用法：/unblock <用户标识>"
        target = normalize_identifier(args[0])
        removed = self._blocks.remove(target)
        return f"已解除拉黑：{target}" if removed else f"不在黑名单：{target}"

    async def _update_reply(self, event: MessageEvent) -> str:
        if not self._admins.is_admin(event.user_openid):
            return "仅管理员可以执行更新检查。"
        if self._updater is None:
            return "当前环境未启用更新功能。"
        return await self._updater.check_and_update()

    def _can_change_settings(self, event: MessageEvent) -> bool:
        """私聊可改自己的设置；群聊仅群主、群管理员或 Bot 管理员可改。"""
        if event.kind != "group":
            return True
        if self._admins is not None and self._admins.is_admin(event.user_openid):
            return True
        return (event.group_role or "").lower() in ("owner", "admin")

    def _default_thinking(self) -> str:
        if not self._config.deepseek_thinking_enabled:
            return "关闭"
        return f"开启，强度 {self._config.deepseek_reasoning_effort}"

    def _thinking_label(self, value: str | None) -> str:
        if value is None:
            if not self._config.deepseek_thinking_enabled:
                return "关闭"
            value = self._config.deepseek_reasoning_effort
        return THINKING_LABELS.get(value, value)

    @staticmethod
    def _progress_label(value: str | None) -> str:
        if value in (None, "", "off", "none"):
            return "不显示"
        return "开启（仅显示调用）"
