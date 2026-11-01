"""管理员名单管理：名单内用户不受额度限制、不计费。

用法：
    python scripts/manage_admins.py list
    python scripts/manage_admins.py add <用户标识> [更多标识...]
    python scripts/manage_admins.py remove <用户标识> [更多标识...]

用户标识不是 QQ 号，而是 QQ 机器人 API 返回的 openid；
让对方私聊发送 /id，其中的「你的用户标识」就是需要添加的值。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import DATA_DIR, load_dotenv  # noqa: E402
from services.admins import (  # noqa: E402
    add_admins,
    load_admins,
    normalize_identifier,
    remove_admins,
)


def _admin_file(raw: str | None) -> Path:
    if raw:
        return Path(raw)
    load_dotenv(BASE_DIR / ".env")
    configured = os.environ.get("ADMIN_FILE", "").strip()
    return Path(configured) if configured else DATA_DIR / "admins.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="管理不限额度的管理员名单")
    parser.add_argument("action", choices=("list", "add", "remove"))
    parser.add_argument("identifiers", nargs="*", help="用户标识（openid）")
    parser.add_argument("--file", help="管理员名单文件，默认 data/admins.json")
    args = parser.parse_args()

    path = _admin_file(args.file)

    if args.action == "list":
        admins = load_admins(path)
        if not admins:
            print(f"管理员名单为空：{path}")
            return
        print(f"管理员共 {len(admins)} 人（{path}）：")
        for identifier in admins:
            print(identifier)
        return

    identifiers = [normalize_identifier(item) for item in args.identifiers]
    if not any(identifiers):
        parser.error("请提供至少一个用户标识")

    if args.action == "add":
        added = add_admins(path, identifiers)
        if added:
            print(f"已添加 {len(added)} 人：")
            for identifier in added:
                print(identifier)
        existing = [item for item in identifiers if item and item not in added]
        if existing:
            print("以下标识已在名单中或无效：")
            for identifier in existing:
                print(identifier or "(空)")
    else:
        removed = remove_admins(path, identifiers)
        if removed:
            print(f"已移除 {len(removed)} 人：")
            for identifier in removed:
                print(identifier)
        missing = [item for item in identifiers if item and item not in removed]
        if missing:
            print("以下标识不在名单中或无效：")
            for identifier in missing:
                print(identifier or "(空)")

    print(f"名单文件：{path}")


if __name__ == "__main__":
    main()
