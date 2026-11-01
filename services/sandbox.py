"""工作空间命令执行沙箱。

Linux 优先使用 bubblewrap（bwrap）隔离进程，只把用户私有空间挂成可写目录；
默认允许访问外网（SANDBOX_NETWORK=on），可设为 off 用网络命名空间断网。
未安装 bwrap 时退化为受限执行：固定工作目录、最小环境变量、CPU/内存/文件大小限制、
超时强制终止。Windows 下仅用于本地开发调试（PowerShell + 超时）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
from pathlib import Path

try:
    import resource
except ImportError:  # Windows
    resource = None

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 8000
MEMORY_LIMIT = 512 * 1024 * 1024
FILE_SIZE_LIMIT = 16 * 1024 * 1024

_SECRET_KEYS = ("DEEPSEEK_API_KEY", "QQ_CLIENT_SECRET", "QQ_APP_ID")


class CommandSandbox:
    def __init__(
        self,
        mode: str = "auto",
        max_output_chars: int = MAX_OUTPUT_CHARS,
        allow_network: bool = True,
    ) -> None:
        self._mode = mode if mode in {"auto", "bwrap", "plain"} else "auto"
        self._max_output = max_output_chars
        self._allow_network = allow_network
        self._bwrap = shutil.which("bwrap") if os.name == "posix" else None
        self._bwrap_usable = False
        if os.name == "posix" and self._bwrap and self._mode != "plain":
            self._bwrap_usable = self._probe_bwrap()
            if self._mode == "auto" and not self._bwrap_usable:
                logger.warning(
                    "bubblewrap 不可用（可能未启用非特权用户命名空间），退化为受限执行"
                )

    async def run(self, cwd: Path, command: str, timeout: float | None = None) -> str:
        if not isinstance(command, str) or not command.strip():
            return "命令不能为空"

        cwd = Path(cwd)
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / ".tmp").mkdir(exist_ok=True)

        seconds = self._clamp_timeout(timeout)
        use_bwrap = os.name == "posix" and (
            self._mode == "bwrap" or (self._mode == "auto" and self._bwrap_usable)
        )
        if os.name == "posix" and self._mode == "bwrap" and not self._bwrap:
            return "沙箱不可用：未安装 bubblewrap（bwrap），可安装或设置 SANDBOX_MODE=plain"
        if os.name == "posix" and self._mode == "bwrap" and not self._bwrap_usable:
            return "沙箱不可用：bubblewrap 无法创建命名空间，请检查内核配置或改用 SANDBOX_MODE=plain"

        argv = self._build_argv(cwd, command.strip(), use_bwrap)
        env = self._build_env(cwd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
                preexec_fn=self._make_preexec(seconds) if os.name == "posix" else None,
            )
        except OSError as exc:
            logger.exception("命令启动失败")
            return f"命令启动失败：{exc}"

        buffer = bytearray()
        stats = {"total": 0}
        cap = 64 * 1024

        async def drain() -> None:
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                stats["total"] += len(chunk)
                if len(buffer) < cap:
                    buffer.extend(chunk[: cap - len(buffer)])

        read_task = asyncio.create_task(drain())
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            timed_out = True
            self._kill(proc)
            await proc.wait()

        try:
            await asyncio.wait_for(read_task, timeout=2)
        except asyncio.TimeoutError:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)

        if timed_out:
            return f"命令执行超时（{seconds:g} 秒），已强制终止。"

        output = bytes(buffer).decode("utf-8", "replace").strip()
        if stats["total"] > cap:
            output += "\n…（输出超过 64KB，已截断）"
        if len(output) > self._max_output:
            output = output[: self._max_output] + "\n…（输出已截断）"
        body = output or "（无输出）"
        return f"退出码：{proc.returncode}\n{body}"

    # ---------- 内部实现 ----------

    def _probe_bwrap(self) -> bool:
        try:
            args = [self._bwrap or "bwrap", "--die-with-parent"]
            if not self._allow_network:
                args.append("--unshare-net")
            args += ["--ro-bind", "/", "/", "--", "true"]
            result = subprocess.run(args, capture_output=True, timeout=5)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _clamp_timeout(self, timeout: float | None) -> float:
        try:
            value = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT
        return max(1.0, min(MAX_TIMEOUT, value))

    def _build_argv(self, cwd: Path, command: str, use_bwrap: bool) -> list[str]:
        if os.name != "posix":
            return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]

        shell = shutil.which("sh") or "/bin/sh"
        if not use_bwrap:
            return [shell, "-c", command]
        args = [
            self._bwrap or "bwrap",
            "--die-with-parent",
            "--new-session",
        ]
        if not self._allow_network:
            args.append("--unshare-net")
        args.append("--unshare-pid")
        # 只读挂载运行所需系统目录，避免暴露 /home、/root 等私人数据
        for src in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
            path = Path(src)
            if not path.exists():
                continue
            real = path.resolve() if path.is_symlink() else path
            args += ["--ro-bind", str(real), src]
        args += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(cwd),
            str(cwd),
            "--chdir",
            str(cwd),
            "--",
            shell,
            "-c",
            command,
        ]
        return args

    @staticmethod
    def _build_env(cwd: Path) -> dict:
        if os.name != "posix":
            env = os.environ.copy()
            for key in _SECRET_KEYS:
                env.pop(key, None)
            env["HOME"] = str(cwd)
            return env

        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(cwd),
            "TMPDIR": str(cwd / ".tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "PYTHONIOENCODING": "utf-8",
        }

    @staticmethod
    def _make_preexec(seconds: float):
        if resource is None:
            return None

        def limit() -> None:
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (int(seconds) + 1, int(seconds) + 2))
                resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
                resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE_LIMIT, FILE_SIZE_LIMIT))
                resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
            except (ValueError, OSError):
                pass

        return limit

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
