"""CXBot 入口：QQ 机器人 + DeepSeek 对话。"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from bot import __version__
from bot.api import QQBotAPI
from bot.gateway import GatewayClient
from bot.handler import ChatHandler
from bot.token import AccessTokenManager
from config import Config
from services.admins import AdminStore
from services.asr import ASRClient
from services.blocklist import BlockStore
from services.console import ManualConsole
from services.context import ContextStore
from services.deepseek import DeepSeekClient
from services.imagegen import ImageClient
from services.preferences import PreferenceStore
from services.quota import QuotaManager
from services.reset_codes import ResetCodeStore
from services.sandbox import CommandSandbox
from services.web import WebTools
from services.workspace import WorkspaceTools

logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    async with aiohttp.ClientSession() as session:
        tokens = AccessTokenManager(config, session)
        api = QQBotAPI(config, tokens, session)
        llm = DeepSeekClient(config, session)
        contexts = ContextStore(config.context_dir, config.max_turns)
        workspace = WorkspaceTools(
            config.workspace_dir,
            config.workspace_quota_bytes,
            CommandSandbox(
                config.sandbox_mode,
                allow_network=config.sandbox_network,
                cpu_percent=config.sandbox_cpu_percent,
                memory_bytes=config.sandbox_memory_bytes,
                total_cpu_percent=config.sandbox_total_cpu_percent,
                total_memory_bytes=config.sandbox_total_memory_bytes,
                max_parallel=config.sandbox_max_parallel,
            ),
            cleanup_days=config.workspace_cleanup_days,
        )
        quota = QuotaManager(
            config.quota_file,
            user_daily=config.quota_user_daily,
            user_weekly=config.quota_user_weekly,
            user_monthly=config.quota_user_monthly,
            global_daily=config.quota_global_daily,
            global_weekly=config.quota_global_weekly,
            global_monthly=config.quota_global_monthly,
            input_tokens_per_credit=config.quota_input_tokens_per_credit,
            credits_per_mb=config.quota_credits_per_mb,
            credits_per_second=config.quota_credits_per_second,
            credits_per_mb_network=config.quota_credits_per_mb_network,
        )

        preferences = PreferenceStore(config.settings_file)
        reset_codes = ResetCodeStore(config.reset_code_file)
        admins = AdminStore(config.admin_file)
        blocks = BlockStore(config.block_file)
        web = WebTools(
            session,
            search_count=config.web_search_count,
            fetch_timeout=config.web_fetch_timeout,
            fetch_max_chars=config.web_fetch_max_chars,
            fetch_max_bytes=config.web_fetch_max_bytes,
            render_mode=config.web_render_mode,
        )
        asr = ASRClient(config, session)
        image = ImageClient(config, session)

        console = None
        if config.console_enabled:
            console = ManualConsole(
                api,
                config.console_file,
                history=config.console_history,
                max_chars=config.max_reply_chars,
                host=config.console_host,
                port=config.console_port,
                session_hours=config.console_session_hours,
            )

        handler = ChatHandler(
            config,
            api,
            llm,
            contexts,
            workspace,
            quota,
            preferences,
            reset_codes,
            admins,
            web,
            image,
            asr,
            blocks,
            console=console,
        )
        gateway = GatewayClient(config, api, tokens, handler.handle)

        cleanup_task = asyncio.create_task(_cleanup_loop(workspace, admins))

        if console is not None:
            try:
                await console.start()
            except Exception:
                logger.exception("网页控制台启动失败，机器人继续运行")

        logger.info("CXBot v%s 启动，AppID=%s", __version__, config.app_id)
        try:
            await gateway.run()
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            if console is not None:
                await console.stop()
            gateway.stop()


async def _cleanup_loop(workspace: WorkspaceTools, admins: AdminStore) -> None:
    """定期清理长期未访问的工作空间文件（管理员空间跳过）。"""
    while True:
        try:
            await asyncio.to_thread(workspace.cleanup, admins.ids())
        except Exception:
            logger.exception("工作空间自动清理失败")
        await asyncio.sleep(6 * 3600)


def main() -> None:
    try:
        config = Config.from_env()
    except RuntimeError as exc:
        print(f"配置错误：{exc}")
        raise SystemExit(1)

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("已停止")


if __name__ == "__main__":
    main()
