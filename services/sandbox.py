"""工作空间命令执行沙箱。

资源限制：
- 单任务上限：CPU 0.1 核、内存 256MB
- 全局上限：CPU 0.5 核、内存 512MB
- 并发数按“总量不超”自动推导，运行中的任务均分 CPU 与内存
- 网络流量：通过 systemd 的 IPAccounting 统计双向字节数，用于计费

Linux 优先使用 bubblewrap（bwrap）隔离进程，只把用户工作空间挂成可写目录；
未安装 bwrap 时退化为受限执行：固定工作目录、最小环境变量、CPU/内存/文件大小限制、
超时强制终止。Windows 下仅用于本地开发调试（PowerShell + 超时）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import resource
except ImportError:  # Windows
    resource = None

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 8000
MEMORY_LIMIT = 256 * 1024 * 1024
FILE_SIZE_LIMIT = 16 * 1024 * 1024
DEFAULT_CPU_PERCENT = 10  # 单任务上限：0.1 核
DEFAULT_TOTAL_CPU_PERCENT = 50  # 全局上限：0.5 核
TOTAL_MEMORY_LIMIT = 512 * 1024 * 1024
MIN_TASK_MEMORY = 64 * 1024 * 1024
MIN_TASK_CPU_PERCENT = 2

_SECRET_KEYS = ("DEEPSEEK_API_KEY", "QQ_CLIENT_SECRET", "QQ_APP_ID")


def resolve_max_parallel(
    total_cpu_percent: int, total_memory_bytes: int, max_parallel: int = 0
) -> int:
    """并发数：显式上限优先，否则按全局总量与单任务下限推导。"""
    if max_parallel and max_parallel > 0:
        return max_parallel
    auto = min(
        total_memory_bytes // MIN_TASK_MEMORY,
        total_cpu_percent // MIN_TASK_CPU_PERCENT,
    )
    return max(1, auto)


@dataclass
class SandboxResult:
    """一次命令执行的结果与用量。"""

    output: str
    seconds: float = 0.0
    network_bytes: int = 0


class CommandSandbox:
    def __init__(
        self,
        mode: str = "auto",
        max_output_chars: int = MAX_OUTPUT_CHARS,
        allow_network: bool = True,
        cpu_percent: int = DEFAULT_CPU_PERCENT,
        memory_bytes: int = MEMORY_LIMIT,
        total_cpu_percent: int = DEFAULT_TOTAL_CPU_PERCENT,
        total_memory_bytes: int = TOTAL_MEMORY_LIMIT,
        max_parallel: int = 0,
    ) -> None:
        self._mode = mode if mode in {"auto", "bwrap", "plain"} else "auto"
        self._max_output = max_output_chars
        self._allow_network = allow_network
        self._cpu_percent = max(MIN_TASK_CPU_PERCENT, min(100, cpu_percent))
        self._memory = max(MIN_TASK_MEMORY, memory_bytes)
        self._total_cpu = max(self._cpu_percent, total_cpu_percent)
        self._total_memory = max(self._memory, total_memory_bytes)
        # 并发数按“总量不超、均分资源”自动推导：内存至少 64MB、CPU 至少 2%
        self._max_parallel = resolve_max_parallel(
            self._total_cpu, self._total_memory, max_parallel
        )
        self._slots = asyncio.Semaphore(self._max_parallel)
        self._active = 0
        self._bwrap = shutil.which("bwrap") if os.name == "posix" else None
        self._bwrap_usable = False
        self._systemd_run: str | None = None
        if os.name == "posix" and self._bwrap and self._mode != "plain":
            self._bwrap_usable = self._probe_bwrap()
            if self._mode == "auto" and not self._bwrap_usable:
                logger.warning(
                    "bubblewrap 不可用（可能未启用非特权用户命名空间），退化为受限执行"
                )
        if os.name == "posix":
            self._systemd_run = self._probe_systemd_run()
        logger.info(
            "沙箱最大并发 %d，单任务上限 CPU %d%% / %dMB，全局 %d%% / %dMB",
            self._max_parallel,
            self._cpu_percent,
            self._memory // (1024 * 1024),
            self._total_cpu,
            self._total_memory // (1024 * 1024),
        )

    def _limits_for(self, active: int) -> tuple[int, int]:
        """按当前并发数均分资源，并受单任务上限约束。"""
        count = max(1, active)
        cpu = min(self._cpu_percent, self._total_cpu / count)
        memory = min(self._memory, self._total_memory // count)
        return (
            max(MIN_TASK_CPU_PERCENT, int(cpu)),
            max(MIN_TASK_MEMORY, int(memory)),
        )

    async def run(
        self,
        cwd: Path,
        command: str,
        timeout: float | None = None,
        usage: dict | None = None,
    ) -> SandboxResult:
        if not isinstance(command, str) or not command.strip():
            return SandboxResult("命令不能为空")

        cwd = Path(cwd)
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / ".tmp").mkdir(exist_ok=True)

        seconds = self._clamp_timeout(timeout)
        use_bwrap = os.name == "posix" and (
            self._mode == "bwrap" or (self._mode == "auto" and self._bwrap_usable)
        )
        if os.name == "posix" and self._mode == "bwrap" and not self._bwrap:
            return SandboxResult(
                "沙箱不可用：未安装 bubblewrap（bwrap），可安装或设置 SANDBOX_MODE=plain"
            )
        if os.name == "posix" and self._mode == "bwrap" and not self._bwrap_usable:
            return SandboxResult(
                "沙箱不可用：bubblewrap 无法创建命名空间，请检查内核配置或改用 SANDBOX_MODE=plain"
            )

        env = self._build_env(cwd)

        # 并行任务按槽位排队；进入后按当前并发数均分 CPU 与内存
        async with self._slots:
            self._active += 1
            try:
                cpu_percent, memory = self._limits_for(self._active)
                argv = self._build_argv(cwd, command.strip(), use_bwrap)
                unit = ""
                if self._systemd_run:
                    unit = f"cxbot-sbx-{secrets.token_hex(6)}"
                    argv = self._wrap_systemd(unit, argv, cpu_percent, memory)

                started = time.monotonic()
                preexec = None
                if os.name == "posix" and not self._systemd_run:
                    # 没有 systemd cgroup 时用 rlimit 兜底：内存与 CPU 时间硬上限
                    preexec = self._make_preexec(seconds, memory)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=str(cwd),
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=(os.name == "posix"),
                        preexec_fn=preexec,
                    )
                except OSError as exc:
                    logger.exception("命令启动失败")
                    return SandboxResult(f"命令启动失败：{exc}")

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

                elapsed = time.monotonic() - started
                network_bytes = self._ip_bytes(unit) if unit else 0
                if usage is not None:
                    usage["sandbox_seconds"] = elapsed
                    usage["network_bytes"] = network_bytes
            finally:
                self._active -= 1

        if timed_out:
            return SandboxResult(
                f"命令执行超时（{seconds:g} 秒），已强制终止。",
                elapsed,
                network_bytes,
            )

        output = bytes(buffer).decode("utf-8", "replace").strip()
        if stats["total"] > cap:
            output += "\n…（输出超过 64KB，已截断）"
        if len(output) > self._max_output:
            output = output[: self._max_output] + "\n…（输出已截断）"
        body = output or "（无输出）"
        if self._systemd_run is None and self._allow_network:
            body += "\n（提示：当前环境无法统计沙箱网络流量，本次仅按执行时长计费）"
        return SandboxResult(
            f"退出码：{proc.returncode}\n{body}", elapsed, network_bytes
        )

    # ---------- 内部实现 ----------

    def _probe_bwrap(self) -> bool:
        try:
            args = [self._bwrap or "bwrap", "--die-with-parent"]
            args += self._namespace_flags()
            if not self._allow_network:
                args.append("--unshare-net")
            args += ["--ro-bind", "/", "/", "--", "true"]
            result = subprocess.run(args, capture_output=True, timeout=5)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _namespace_flags() -> list[str]:
        """额外隔离主机名、IPC 与 cgroup 视图。"""
        return ["--unshare-uts", "--unshare-ipc", "--unshare-cgroup"]

    def _probe_systemd_run(self) -> str | None:
        """检测能否用 systemd-run 的 cgroup 做 CPU/内存/流量限制。"""
        binary = shutil.which("systemd-run")
        if not binary or not Path("/run/systemd/system").is_dir():
            return None
        try:
            result = subprocess.run(
                [
                    binary,
                    "--quiet",
                    "--collect",
                    "--scope",
                    f"--property=CPUQuota={self._cpu_percent}%",
                    f"--property=MemoryMax={self._memory}",
                    "--property=MemorySwapMax=0",
                    "--",
                    "true",
                ],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            logger.warning(
                "systemd-run 不可用，沙箱将退化为 rlimit 限制：%s",
                result.stderr.decode("utf-8", "replace")[:200],
            )
            return None
        return binary

    def _wrap_systemd(
        self, unit: str, argv: list[str], cpu_percent: int, memory: int
    ) -> list[str]:
        return [
            self._systemd_run or "systemd-run",
            "--quiet",
            "--scope",
            f"--unit={unit}",
            f"--property=CPUQuota={cpu_percent}%",
            f"--property=MemoryMax={memory}",
            "--property=MemorySwapMax=0",
            "--property=TasksMax=64",
            "--property=IPAccounting=yes",
            "--",
            *argv,
        ]

    def _ip_bytes(self, unit: str) -> int:
        """读取 systemd 记录的双向流量字节数。"""
        if not unit:
            return 0
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=IPIngressBytes",
                    "--property=IPEgressBytes",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        total = 0
        for item in result.stdout.split():
            if item.isdigit():
                total += int(item)
        return total

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
            *self._namespace_flags(),
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
        if self._allow_network:
            args += self._dns_mounts(cwd)
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

    def _dns_mounts(self, cwd: Path) -> list[str]:
        """给沙箱注入可用的 resolv.conf。

        /etc/resolv.conf 经常是指向 /run/systemd/resolve/... 的软链接，
        只挂 /etc 时软链接目标不存在，直接 bind 到 /etc/resolv.conf 会因
        跟随悬空软链接而报 No such file or directory。
        这里把解析好的内容直接挂到软链接的真实目标路径。
        """
        source = Path("/etc/resolv.conf")
        try:
            content = source.read_bytes()
        except OSError:
            content = b""
        # systemd-resolved 的 127.0.0.53 stub 在部分容器内不可用，
        # 存在真实上游配置时优先使用（通常是运营商/公共 DNS）。
        upstream = Path("/run/systemd/resolve/resolv.conf")
        if upstream.is_file():
            try:
                upstream_content = upstream.read_bytes()
            except OSError:
                upstream_content = b""
            if upstream_content.strip():
                content = upstream_content
        if not content.strip():
            logger.warning("无法读取 /etc/resolv.conf，沙箱内可能无法解析域名")
            return []

        try:
            # 不要求目标存在：软链接悬空时也能拿到真实路径，由 bwrap 负责创建
            target = source.resolve()
        except (OSError, RuntimeError):
            target = source
        if target == source and not source.exists():
            logger.warning("找不到 /etc/resolv.conf，沙箱内可能无法解析域名")
            return []

        args: list[str] = []
        resolve_dir = Path("/run/systemd/resolve")
        if resolve_dir.is_dir():
            # nss-resolve 可能依赖这里的 socket 与 stub 配置
            args += ["--ro-bind", str(resolve_dir), str(resolve_dir)]

        resolv_file = cwd / ".tmp" / "resolv.conf"
        try:
            resolv_file.write_bytes(content)
        except OSError:
            logger.warning("沙箱 DNS 配置写入失败", exc_info=True)
            return args
        # 挂到软链接的真实目标路径，避免 open("/etc/resolv.conf") 跟随悬空软链接
        args += ["--ro-bind", str(resolv_file), str(target)]
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

    def _make_preexec(self, seconds: float, memory: int):
        if resource is None:
            return None

        def limit() -> None:
            try:
                resource.setrlimit(
                    resource.RLIMIT_CPU, (int(seconds) + 1, int(seconds) + 2)
                )
                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
                resource.setrlimit(
                    resource.RLIMIT_FSIZE, (FILE_SIZE_LIMIT, FILE_SIZE_LIMIT)
                )
                resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
                # 防止 fork 炸弹耗尽宿主机进程/内存（systemd-run 不可用时没有 TasksMax）
                resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
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
