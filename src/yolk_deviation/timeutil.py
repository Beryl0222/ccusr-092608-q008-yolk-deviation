"""时间解析工具。"""

from __future__ import annotations

from datetime import datetime


def parse(value: str) -> datetime:
    """解析带时区的 ISO-8601 字符串。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
