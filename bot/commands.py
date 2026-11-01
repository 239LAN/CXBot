"""以 / 开头的聊天指令。"""

from __future__ import annotations

from bot.events import MessageEvent
from config import Config
from services.admins import AdminStore
from services.minecraft import MinecraftError, ping_server
from services.preferences import PreferenceStore
from services.quota import QuotaManager
from services.reset_codes import ResetCodeStore

PERIOD_LABELS = (("day", "今日"), ("week", "本周"), ("month", "本月"))
THINKING_VALUES = {"off", "low", "medium", "high", "max", "default"}

HELP_TEXT = """可用指令：
/help — 查看帮助
/reset — 清空当前对话上下文
/quota — 查看剩余额度
/quota detail — 查看各周期已用/上限
/thinking — 查看当前思考强度
/thinking off|low|medium|high|max|default — 调整思考强度
/stream — 查看流式输出状态
/stream on|off|default — 开/关流式输出（仅私聊）
/motd <地址> — 探测 Minecraft Java 服务器
/redeem <重置码> — 恢复自己的额度
/id — 查看当前会话标识"""


class CommandRouter:
    def __init__(
        self,
        config: Config,
        quota: QuotaManager,
        preferences: PreferenceStore,
        reset_codes: ResetCodeStore,
        admins: AdminStore,
    ) -> None:
        self._config = config
        self._quota = quota
        self._preferences = preferences
        self._reset_codes = reset_codes
        self._admins = admins

    async def route(self, event: MessageEvent, text: str) -> str:
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        if command in ("/help", "/帮助"):
            return HELP_TEXT
        if command in ("/quota", "/额度"):
            return await self._quota_reply(event, args)
        if command in ("/thinking", "/思考"):
            return await self._thinking_reply(event, args)
        if command in ("/stream", "/流式"):
            return await self._stream_setting_reply(event, args)
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
        lines.append("计费：1 积分 = 1 输出 token = 4 输入 token；10 积分 = 1MB 文件。")
        return "\n".join(lines)

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

    def _default_thinking(self) -> str:
        if not self._config.deepseek_thinking_enabled:
            return "关闭"
        return f"开启，强度 {self._config.deepseek_reasoning_effort}"
