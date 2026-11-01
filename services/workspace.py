"""私聊用户的独立工作空间（默认 50MB），对模型暴露为工具。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path

from services.sandbox import CommandSandbox

logger = logging.getLogger(__name__)

MB = 1024 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_LIST_ENTRIES = 200
MAX_TOOL_RESULT_CHARS = 8000
MAX_SEND_FILE_BYTES = 200 * MB  # QQ 富媒体文件硬上限

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出你在该用户私有空间中的文件（含子目录），只显示路径和大小。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的子目录，留空表示根目录",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取私有空间中的文本文件。单次最多读取 64KB，超出部分会被截断。"
                "可通过 start_line / end_line 按行读取（行号从 1 开始，包含结束行），"
                "按行读取时结果会带行号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径，例如 notes/todo.md"},
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（含），从 1 开始，可省略",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（含），可省略",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建或整体覆盖私有空间中的文本文件，目录不存在会自动创建。内容超过约 2000 字时，先写入开头，再用 append_file 分段追加，避免单次参数过长被截断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "在已有文本文件末尾追加内容，文件不存在时自动创建。长内容请分多次调用，每次约 2000 字以内。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"},
                    "content": {"type": "string", "description": "要追加的文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "在文本文件中把 old_string 替换为 new_string。默认要求 old_string 唯一匹配。按行替换请用 replace_lines。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"},
                    "old_string": {"type": "string", "description": "要替换的原文本"},
                    "new_string": {"type": "string", "description": "替换后的新文本"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配项，默认 false",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_lines",
            "description": (
                "按行号替换文本文件内容：把第 start_line 到 end_line 行（含）替换为 content。"
                "content 为空表示删除这些行。行号从 1 开始。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"},
                    "start_line": {"type": "integer", "description": "起始行号（含）"},
                    "end_line": {"type": "integer", "description": "结束行号（含）"},
                    "content": {"type": "string", "description": "替换后的文本，可为空"},
                },
                "required": ["path", "start_line", "end_line", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除私有空间中的指定文件，无法删除目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_path",
            "description": "复制文件或目录到私有空间中的新路径，目录会递归复制。目标已存在时默认报错。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "源文件或目录的相对路径"},
                    "new_path": {"type": "string", "description": "目标相对路径"},
                    "overwrite": {
                        "type": "boolean",
                        "description": "目标已存在时是否覆盖，默认 false",
                    },
                },
                "required": ["path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_path",
            "description": "重命名或移动文件/目录，目标已存在时报错，不允许覆盖。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "源文件或目录的相对路径"},
                    "new_path": {"type": "string", "description": "新路径"},
                },
                "required": ["path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_dir",
            "description": "在私有空间中创建目录，父目录不存在会自动创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录相对路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_dir",
            "description": "删除私有空间中的目录。默认只能删除空目录；非空目录需设置 recursive=true。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录相对路径"},
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归删除非空目录，默认 false",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在用户私有空间内执行 shell 命令。命令的工作目录就是该用户的私有空间，"
                "默认超时 10 秒，输出最多 8000 字符。运行环境为受限沙箱（Linux 优先使用 "
                "bubblewrap，隔离进程；仅私有空间可写），网络是否可用由部署配置决定，"
                "通常可以访问外网。适合运行脚本、处理文本、查看文件等；"
                "不要用于安装系统软件或修改系统文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，范围 1-30，默认 10",
                    },
                },
                "required": ["command"],
            },
        },
    },
]


class ToolError(RuntimeError):
    """工具参数或执行错误，会作为结果返回给模型。"""


class WorkspaceTools:
    def __init__(
        self,
        base_dir: Path,
        quota_bytes: int = 50 * MB,
        sandbox: CommandSandbox | None = None,
    ) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._quota = max(1, quota_bytes)
        self._sandbox = sandbox or CommandSandbox()

    def schemas(self) -> list[dict]:
        return TOOL_SCHEMAS

    async def execute(self, user_openid: str, name: str, arguments: str) -> str:
        """执行工具，返回给模型看的文本结果；异常不外抛。"""
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ToolError("参数必须是 JSON 对象")

            root = self._root(user_openid)
            if name == "run_command":
                result = await self._sandbox.run(
                    root, args.get("command"), args.get("timeout")
                )
            else:
                tool = getattr(self, f"_tool_{name}", None)
                if tool is None:
                    raise ToolError(f"未知工具：{name}")
                result = await asyncio.to_thread(tool, root, **args)

            if len(result) > MAX_TOOL_RESULT_CHARS:
                result = result[:MAX_TOOL_RESULT_CHARS] + "\n…（结果已截断）"
            return result
        except json.JSONDecodeError as exc:
            logger.warning("工具参数 JSON 解析失败：%s（%s）", name, exc)
            return (
                "工具参数不是有效 JSON，通常是单次输出过长被截断。"
                "请缩短本次内容，分批调用 write_file/append_file。"
            )
        except ToolError as exc:
            return f"工具执行失败：{exc}"
        except TypeError as exc:
            return f"工具参数错误：{exc}"
        except Exception as exc:
            logger.exception("工具执行异常：%s", name)
            return f"工具执行异常：{exc}"

    # ---------- 内部工具实现 ----------

    def _root(self, user_openid: str) -> Path:
        digest = hashlib.sha256(user_openid.encode("utf-8")).hexdigest()[:24]
        root = self._base / digest
        root.mkdir(parents=True, exist_ok=True)
        return root

    def export_file(self, user_openid: str, path: str) -> tuple[str, bytes]:
        """读取私有空间中的文件，返回 (文件名, 内容)，用于发送给用户。"""
        target = self._resolve(self._root(user_openid), path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{path}")
        if target.stat().st_size > MAX_SEND_FILE_BYTES:
            raise ToolError("文件超过 200MB，QQ 不支持发送")
        return target.name, target.read_bytes()

    def _resolve(self, root: Path, rel: str) -> Path:
        if not isinstance(rel, str):
            raise ToolError("path 必须是字符串")
        rel = rel.strip()
        if ":" in rel:
            raise ToolError("路径不能包含盘符")
        path = Path(rel)
        if path.is_absolute() or any(part == ".." for part in path.parts):
            raise ToolError("非法路径")
        target = (root / path).resolve()
        root_resolved = root.resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise ToolError("非法路径")
        return target

    def _used_bytes(self, root: Path) -> int:
        return sum(f.lstat().st_size for f in root.rglob("*") if f.is_file())

    def _check_quota(self, root: Path, delta: int) -> None:
        used = self._used_bytes(root)
        if used + delta > self._quota:
            raise ToolError(
                f"空间不足：已用 {self._fmt_size(used)}，本次需要 {self._fmt_size(delta)}，"
                f"上限 {self._fmt_size(self._quota)}"
            )

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size >= MB:
            return f"{size / MB:.1f}MB"
        return f"{size / 1024:.0f}KB"

    def _tool_list_files(self, root: Path, path: str = "") -> str:
        target = self._resolve(root, path)
        if not target.exists():
            raise ToolError(f"路径不存在：{path}")
        if target.is_file():
            return f"{path} ({target.stat().st_size} 字节)"

        lines = []
        for item in sorted(target.rglob("*")):
            if item.is_dir():
                continue
            rel = item.relative_to(root).as_posix()
            lines.append(f"{rel} ({item.stat().st_size} 字节)")
            if len(lines) >= MAX_LIST_ENTRIES:
                lines.append("…（文件过多，已截断）")
                break
        if not lines:
            return "空间为空。"
        return "当前文件：\n" + "\n".join(lines)

    def _tool_read_file(
        self,
        root: Path,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        target = self._resolve(root, path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{path}")

        if start_line is None and end_line is None:
            raw = target.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ToolError("不是 UTF-8 文本文件，暂不支持读取二进制内容")

            if len(raw) > MAX_READ_BYTES:
                text = text[:MAX_READ_BYTES] + "\n…（内容超过 64KB，已截断）"
            return text

        start = self._line_no(start_line, "start_line", 1)
        end = self._line_no(end_line, "end_line", None)
        if end is not None and end < start:
            raise ToolError("end_line 不能小于 start_line")

        numbered: list[str] = []
        chars = 0
        truncated = False
        try:
            with target.open("r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if number < start:
                        continue
                    if end is not None and number > end:
                        break
                    text = line.rstrip("\n")
                    if chars + len(text) > MAX_READ_BYTES:
                        truncated = True
                        break
                    numbered.append(f"{number}\t{text}")
                    chars += len(text) + 1
        except UnicodeDecodeError:
            raise ToolError("不是 UTF-8 文本文件，暂不支持读取二进制内容")

        if not numbered:
            return "指定行范围没有内容。"
        result = "\n".join(numbered)
        if truncated:
            result += "\n…（内容超过 64KB，已截断）"
        return result

    def _tool_replace_lines(
        self,
        root: Path,
        path: str,
        start_line: int,
        end_line: int,
        content: str,
    ) -> str:
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串")
        target = self._resolve(root, path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{path}")

        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            raise ToolError("文件为空，无法按行替换")

        start = self._line_no(start_line, "start_line")
        end = self._line_no(end_line, "end_line")
        if start > end:
            raise ToolError("start_line 不能大于 end_line")
        if start > total or end > total:
            raise ToolError(f"行号超出范围（文件共 {total} 行）")

        new_lines = content.splitlines() if content else []
        updated_lines = lines[: start - 1] + new_lines + lines[end:]
        updated = "\n".join(updated_lines)
        if text.endswith("\n"):
            updated += "\n"

        new_size = len(updated.encode("utf-8"))
        self._check_quota(root, new_size - len(text.encode("utf-8")))

        target.write_text(updated, encoding="utf-8")
        return f"已替换第 {start}-{end} 行（{total} 行 → {len(updated_lines)} 行）"

    @staticmethod
    def _line_no(value: int | None, name: str, default: int | None = None) -> int | None:
        if value is None:
            return default
        if isinstance(value, bool):
            raise ToolError(f"{name} 必须是正整数")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ToolError(f"{name} 必须是正整数")
        if number < 1:
            raise ToolError(f"{name} 必须大于等于 1")
        return number

    def _tool_write_file(self, root: Path, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串")
        target = self._resolve(root, path)
        if target == root.resolve() or target.is_dir():
            raise ToolError("路径必须是文件")

        old_size = target.stat().st_size if target.exists() else 0
        new_size = len(content.encode("utf-8"))
        self._check_quota(root, new_size - old_size)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入 {path}（{new_size} 字节）"

    def _tool_append_file(self, root: Path, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串")
        target = self._resolve(root, path)
        if target.exists() and not target.is_file():
            raise ToolError("路径必须是文件")

        added = len(content.encode("utf-8"))
        self._check_quota(root, added)

        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return f"已追加 {path}（新增 {added} 字节）"

    def _tool_edit_file(
        self,
        root: Path,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            raise ToolError("old_string / new_string 必须是字符串")
        target = self._resolve(root, path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{path}")
        if not old_string:
            raise ToolError("old_string 不能为空")

        text = target.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            raise ToolError("原文中找不到 old_string")
        if count > 1 and not replace_all:
            raise ToolError(f"old_string 匹配到 {count} 处，请补充上下文或设置 replace_all")

        updated = text.replace(old_string, new_string) if replace_all else text.replace(
            old_string, new_string, 1
        )
        new_size = len(updated.encode("utf-8"))
        self._check_quota(root, new_size - len(text.encode("utf-8")))

        target.write_text(updated, encoding="utf-8")
        return f"已修改 {path}（替换 {count if replace_all else 1} 处）"

    def _tool_delete_file(self, root: Path, path: str) -> str:
        target = self._resolve(root, path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{path}")
        target.unlink()
        return f"已删除 {path}"

    def _path_size(self, target: Path) -> int:
        if target.is_file():
            return target.stat().st_size
        return sum(f.lstat().st_size for f in target.rglob("*") if f.is_file())

    def _tool_copy_path(
        self, root: Path, path: str, new_path: str, overwrite: bool = False
    ) -> str:
        source = self._resolve(root, path)
        target = self._resolve(root, new_path)
        root_resolved = root.resolve()

        if not source.exists():
            raise ToolError(f"源路径不存在：{path}")
        if source == root_resolved:
            raise ToolError("不能复制整个空间根目录")
        if target == source:
            raise ToolError("源和目标路径相同")
        if source.is_dir() and source in target.parents:
            raise ToolError("不能把目录复制到它自己内部")
        if target.exists() and not overwrite:
            raise ToolError(f"目标已存在：{new_path}")

        source_size = self._path_size(source)
        old_size = self._path_size(target) if target.exists() else 0
        self._check_quota(root, source_size - old_size)

        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        return f"已复制 {path} -> {new_path}（{source_size} 字节）"

    def _tool_rename_path(self, root: Path, path: str, new_path: str) -> str:
        source = self._resolve(root, path)
        target = self._resolve(root, new_path)
        root_resolved = root.resolve()

        if not source.exists():
            raise ToolError(f"源路径不存在：{path}")
        if source == root_resolved:
            raise ToolError("不能重命名整个空间根目录")
        if source.is_dir() and source in target.parents:
            raise ToolError("不能把目录移动到它自己内部")
        if target.exists():
            raise ToolError(f"目标已存在：{new_path}")

        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        return f"已重命名 {path} -> {new_path}"

    def _tool_create_dir(self, root: Path, path: str) -> str:
        target = self._resolve(root, path)
        if target == root.resolve():
            return "目录已存在"
        if target.exists() and not target.is_dir():
            raise ToolError(f"同名文件已存在：{path}")
        target.mkdir(parents=True, exist_ok=True)
        return f"已创建目录 {path}"

    def _tool_delete_dir(self, root: Path, path: str, recursive: bool = False) -> str:
        target = self._resolve(root, path)
        if target == root.resolve():
            raise ToolError("不能删除整个空间根目录")
        if not target.is_dir():
            raise ToolError(f"目录不存在：{path}")

        if recursive:
            shutil.rmtree(target)
        else:
            if any(target.iterdir()):
                raise ToolError("目录非空；如需递归删除请设置 recursive=true")
            target.rmdir()
        return f"已删除目录 {path}"
