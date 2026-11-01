"""管理员用：生成一次性额度重置码。

重置码只会以哈希形式保存在库存文件中，明文仅在生成时打印一次，请及时分发。

用法（在项目根目录执行）：
    python scripts/gen_reset_codes.py                 # 生成 1 个，30 天有效
    python scripts/gen_reset_codes.py 5               # 生成 5 个
    python scripts/gen_reset_codes.py 5 --days 7      # 7 天内有效
    python scripts/gen_reset_codes.py 5 --days 0      # 永久有效
    python scripts/gen_reset_codes.py --file data/reset_codes.json

用户在聊天中发送：/redeem <重置码>，即可重置自己的日/周/月额度。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import load_dotenv  # noqa: E402
from services.reset_codes import ResetCodeError, generate_codes  # noqa: E402


def _default_file() -> Path:
    load_dotenv(BASE_DIR / ".env")
    configured = os.environ.get("RESET_CODE_FILE", "").strip()
    return Path(configured) if configured else BASE_DIR / "data" / "reset_codes.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 CXBot 一次性额度重置码")
    parser.add_argument("count", nargs="?", type=int, default=1, help="生成数量（1-200）")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="有效天数，默认 30；0 表示永久有效",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="库存文件路径（默认读取 RESET_CODE_FILE，否则 data/reset_codes.json）",
    )
    args = parser.parse_args(argv)

    path = args.file or _default_file()
    expires_days = args.days if args.days > 0 else None

    try:
        codes = generate_codes(path, count=args.count, expires_days=expires_days)
    except ResetCodeError as exc:
        print(f"生成失败：{exc}")
        return 1

    validity = f"{expires_days} 天有效" if expires_days else "永久有效"
    print(f"已生成 {len(codes)} 个重置码（{validity}），库存文件：{path}")
    for code in codes:
        print(code)
    print("每个码只能使用一次，发送 /redeem <重置码> 即可重置额度。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
