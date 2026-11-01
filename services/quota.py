"""积分额度管理：按用户/群和全站的自然日、自然周、自然月累计用量。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MB = 1024 * 1024
CST = timezone(timedelta(hours=8))
PERIODS = (("day", "今日"), ("week", "本周"), ("month", "本月"))

QUOTA_FILE_VERSION = 1


class QuotaManager:
    """按真实 token / 文件大小扣积分，并检查用户与全站三层限额。

    计费规则（各维度独立向上取整）：
    - 输入：1 积分 = N 个输入 token（默认 4）
    - 输出：1 积分 = 1 个输出 token
    - 文件：10 积分 = 1MB（默认）
    """

    def __init__(
        self,
        path: Path,
        *,
        user_daily: int = 8000,
        user_weekly: int = 32000,
        user_monthly: int = 80000,
        global_daily: int = 50000,
        global_weekly: int = 300000,
        global_monthly: int = 1000000,
        input_tokens_per_credit: int = 4,
        credits_per_mb: int = 10,
    ) -> None:
        self._path = path
        self._user_limits = {
            "day": max(0, user_daily),
            "week": max(0, user_weekly),
            "month": max(0, user_monthly),
        }
        self._global_limits = {
            "day": max(0, global_daily),
            "week": max(0, global_weekly),
            "month": max(0, global_monthly),
        }
        self._input_tokens_per_credit = max(1, input_tokens_per_credit)
        self._credits_per_mb = max(0, credits_per_mb)
        self._lock = asyncio.Lock()
        self._data = self._load()

    # ---------- 计费 ----------

    def cost(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        file_bytes: int = 0,
    ) -> int:
        """按各维度向上取整计算积分。"""
        input_credits = math.ceil(
            max(0, input_tokens) / self._input_tokens_per_credit
        )
        output_credits = math.ceil(max(0, output_tokens))
        file_credits = math.ceil(
            max(0, file_bytes) * self._credits_per_mb / MB
        )
        return input_credits + output_credits + file_credits

    async def charge(
        self,
        scope: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        file_bytes: int = 0,
    ) -> int:
        """按真实用量扣积分，同时计入用户/群与全站账户。返回本次积分。"""
        credits = self.cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            file_bytes=file_bytes,
        )
        if credits <= 0:
            return 0

        async with self._lock:
            keys = self._period_keys()
            record = self._data["scopes"].setdefault(scope, {})
            self._touch(record, keys)
            for period, _label in PERIODS:
                record[period]["used"] += credits
            self._touch(self._data["global"], keys)
            for period, _label in PERIODS:
                self._data["global"][period]["used"] += credits
            self._save()

        logger.info("额度扣减 %s：%d 积分（输入 %d / 输出 %d / 文件 %d 字节）",
                    scope, credits, input_tokens, output_tokens, file_bytes)
        return credits

    # ---------- 限额检查 ----------

    async def check(self, scope: str) -> tuple[int, str | None]:
        """返回 (当前剩余积分, 拒绝原因)。剩余为 0 或超限时原因为提示文案。"""
        async with self._lock:
            return self._check_locked(scope)

    async def remaining(self, scope: str) -> int:
        async with self._lock:
            return self._check_locked(scope)[0]

    async def usage(self, scope: str) -> dict[str, dict[str, int]]:
        """返回当前周期内该用户/群的已用与上限。"""
        async with self._lock:
            keys = self._period_keys()
            record = self._data["scopes"].get(scope) or {}
            return {
                period: {
                    "used": self._used(record, period, keys[period]),
                    "limit": self._user_limits[period],
                }
                for period, _label in PERIODS
            }

    async def reset(self, scope: str) -> None:
        """重置该用户/群的日、周、月用量（不影响全站累计）。"""
        async with self._lock:
            keys = self._period_keys()
            record = self._data["scopes"].setdefault(scope, {})
            self._touch(record, keys)
            for period, _label in PERIODS:
                record[period]["used"] = 0
            self._save()
        logger.info("额度已重置：%s", scope)

    def _check_locked(self, scope: str) -> tuple[int, str | None]:
        keys = self._period_keys()
        record = self._data["scopes"].get(scope) or {}
        remaining: int | None = None

        for period, label in PERIODS:
            limit = self._user_limits[period]
            left = max(0, limit - self._used(record, period, keys[period]))
            if left <= 0:
                subject = "本群" if scope.startswith("group:") else "你"
                return 0, f"{subject}的{label}额度已用完（上限 {limit} 积分），请稍后再来。"
            remaining = left if remaining is None else min(remaining, left)

        for period, _label in PERIODS:
            limit = self._global_limits[period]
            left = max(
                0, limit - self._used(self._data["global"], period, keys[period])
            )
            if left <= 0:
                return 0, "服务繁忙，请过段时间再试。"
            remaining = left if remaining is None else min(remaining, left)

        return max(0, remaining or 0), None

    # ---------- 周期与持久化 ----------

    @staticmethod
    def _period_keys(now: datetime | None = None) -> dict[str, str]:
        current = now or datetime.now(CST)
        iso = current.isocalendar()
        return {
            "day": current.strftime("%Y-%m-%d"),
            "week": f"{iso.year}-W{iso.week:02d}",
            "month": current.strftime("%Y-%m"),
        }

    @staticmethod
    def _used(record: dict, period: str, key: str) -> int:
        bucket = record.get(period)
        if not isinstance(bucket, dict) or bucket.get("key") != key:
            return 0
        try:
            return max(0, int(bucket.get("used", 0)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _touch(record: dict, keys: dict[str, str]) -> None:
        for period, key in keys.items():
            bucket = record.get(period)
            if not isinstance(bucket, dict) or bucket.get("key") != key:
                record[period] = {"key": key, "used": 0}

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                scopes = data.get("scopes")
                global_record = data.get("global")
                if isinstance(scopes, dict) and isinstance(global_record, dict):
                    return {"scopes": scopes, "global": global_record}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("额度文件读取失败，使用空账户：%s", self._path, exc_info=True)
        return {"scopes": {}, "global": {}}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": QUOTA_FILE_VERSION,
            "scopes": self._data["scopes"],
            "global": self._data["global"],
        }
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("额度文件写入失败：%s", self._path)
