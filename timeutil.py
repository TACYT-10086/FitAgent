"""北京时间工具。

约定：PostgreSQL 的 TIMESTAMPTZ 一律存 UTC instant（绝对时间点）；
但「今天/某一天/自然月」这类业务语义，以及返回给模型的时间字符串，一律用北京时间。
中国不实行夏令时，固定 UTC+8 即可，不依赖服务器或数据库的时区配置。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def to_beijing(value: datetime) -> datetime:
    """把 aware datetime 转成北京时间；naive 按 UTC 解释（库里不应该出现 naive）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ)


def iso_beijing(value: datetime) -> str:
    """返回带 +08:00 偏移的 ISO 字符串，例如 2026-10-23T18:00:00+08:00。"""
    return to_beijing(value).isoformat()
