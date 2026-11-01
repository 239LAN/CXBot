"""构建 Linux 单文件安装包：源码 ZIP + base64 内嵌安装脚本。

用法：python scripts/build_installer.py
输出：dist/cxbot.zip、dist/cxbot-install.sh
"""

from __future__ import annotations

import base64
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
TEMPLATE = ROOT / "scripts" / "install.sh"

INCLUDE_FILES = ("main.py", "config.py", "requirements.txt", ".env.example")
INCLUDE_DIRS = ("bot", "services", "scripts")
SKIP_RELATIVE = {"scripts/install.sh"}
SKIP_PARTS = {"__pycache__"}


def collect_files() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if not path.is_file():
            raise SystemExit(f"缺少文件：{path}")
        files.append(path)

    for directory in INCLUDE_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            raise SystemExit(f"缺少目录：{base}")
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative in SKIP_RELATIVE:
                continue
            if SKIP_PARTS.intersection(path.parts):
                continue
            if path.suffix == ".pyc":
                continue
            files.append(path)
    return files


def build() -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    files = collect_files()

    zip_path = DIST / "cxbot.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())

    payload = base64.encodebytes(zip_path.read_bytes()).decode("ascii")
    template = TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not template.rstrip().endswith("__PAYLOAD_BELOW__"):
        raise SystemExit("安装脚本模板缺少 __PAYLOAD_BELOW__ 标记")

    installer = DIST / "cxbot-install.sh"
    installer.write_text(
        template.rstrip("\n") + "\n" + payload,
        encoding="utf-8",
        newline="\n",
    )
    try:
        installer.chmod(0o755)
    except OSError:
        pass

    print(f"源码文件：{len(files)} 个")
    print(f"ZIP：{zip_path}（{zip_path.stat().st_size} 字节）")
    print(f"安装脚本：{installer}（{installer.stat().st_size} 字节）")
    print("使用：sudo bash dist/cxbot-install.sh")


if __name__ == "__main__":
    build()
