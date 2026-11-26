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

HELP_TEXT = """可用指令：
/help — 查看帮助
/about — 查看版本信息
/reset — 清空当前对话上下文
/quota — 查看剩余额度
/quota detail — 查看各周期已用/上限
/workspace — 查看空间占用
/continue — 继续发送被截断的内容
/progress — 查看过程显示状态
/progress on|off|default — 开/关工具过程记录（仅私聊流式）
/markdown — 查看 Markdown 渲染状态
/markdown on|off|default — 开/关 Markdown 渲染
/thinking — 查看当前思考强度
/thinking off|low|medium|high|max|default — 调整思考强度
/stream — 查看流式输出状态
/stream on|off|default — 开/关流式输出（仅私聊）
/motd <地址> — 探测 Minecraft Java 服务器
/redeem <重置码> — 恢复自己的额度
/id — 查看当前会话标识
管理员：
/gencode [数量] [天数] — 生成重置码
/resetquota [群标识] — 直接重置群聊额度
/block <用户标识> — 拉黑用户
/unblock <用户标识> — 解除拉黑"""


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
    ) -> None:
        self._config = config
        self._quota = quota
        self._preferences = preferences
        self._reset_codes = reset_codes
        self._admins = admins
        self._workspace = workspace
        self._blocks = blocks

    async def route(self, event: MessageEvent, text: str) -> str:
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        if command in ("/help", "/帮助"):
            return HELP_TEXT
        if command in ("/about", "/关于", "/version", "/版本"):
            return f"CXBot v{__version__}\n© {datetime.now().year} {__author__}"
        if command in ("/quota", "/额度"):
            return await self._quota_reply(event, args)
        if command in ("/workspace", "/空间", "/files", "/文件"):
            return await self._workspace_reply(event)
        if command in ("/thinking", "/思考"):
            return await self._thinking_reply(event, args)
        if command in ("/progress", "/过程"):
            return await self._progress_reply(event, args)
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

        if not args:
            if current is None:
                return f"当前思考强度：默认（{self._default_thinking()}）"
            if current == "off":
                return "当前思考强度：关闭"
            return f"当前思考强度：{current}"

        value = args[0].lower()
        if value not in THINKING_VALUES:
            return "思考强度可选：off / low / medium / high / max / default"
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
        if event.kind != "c2c":
            return "群聊不支持流式过程显示，该设置仅在私聊生效。"
        scope = event.context_key
        current = await self._preferences.get(scope, "progress")
        enabled = current == "on"
        if not args:
            suffix = "（默认）" if current is None else ""
            return f"过程显示：{'开启' if enabled else '关闭'}{suffix}"
        value = args[0].lower()
        if value not in ("on", "off", "default"):
            return "用法：/progress on|off|default"
        await self._preferences.set(
            scope, "progress", "on" if value == "on" else None
        )
        return f"过程显示已{'开启' if value == 'on' else '关闭'}。"

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
        except MinecraftError:
            return "无法连接至服务器"
        motd = result.motd or "(无 MOTD)"
        return f"{result.address}  {result.ping_ms}ms\n{motd}"

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

    def _default_thinking(self) -> str:
        if not self._config.deepseek_thinking_enabled:
            return "关闭"
        return f"开启，强度 {self._config.deepseek_reasoning_effort}"
