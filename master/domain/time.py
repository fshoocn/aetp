"""领域层时间工具。

统一使用带 UTC 时区的 datetime；落库时由持久化层转换为 naive UTC，
返回时再标记为 UTC，保证跨数据库（SQLite/MySQL/PostgreSQL）行为一致。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """返回当前 UTC 时间（带时区信息）。"""
    return datetime.now(timezone.utc)
