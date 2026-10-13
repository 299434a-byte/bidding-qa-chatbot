"""timeutil：时间工具（存储统一 UTC，历史 naive 文本按 UTC+8 解释）。

职责：
  - 解析库内 / 历史里的各种时间文本为 UTC datetime
  - 统一格式化与合法性判断（非法输入返回 None，不抛错）

依赖：无
被谁调用：auth（令牌过期判断）、history（会话时间过滤）
"""

from __future__ import annotations

import datetime as dt

UTC = dt.timezone.utc

LEGACY_TZ = dt.timezone(dt.timedelta(hours=8))


def now_utc() -> dt.datetime:
    """当前 UTC 时间（感知型，带 +00:00）。"""
    return dt.datetime.now(UTC)


def now_iso() -> str:
    """当前 UTC 时间的 ISO 文本（带时区偏移），用于写库。"""
    return now_utc().isoformat()


def parse_ts(text: str | None) -> dt.datetime | None:
    """把库里的时间文本解析成「感知型 UTC 时间」；解析不了返回 None。"""
    if not text:
        return None
    try:
        value = dt.datetime.fromisoformat(str(text))
    except Exception:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=LEGACY_TZ)
    return value.astimezone(UTC)


def is_past(ts_text: str | None, *, now: dt.datetime | None = None) -> bool:
    """判断「这个时间点是否已过去」；解析不了的文本一律当作已过去（安全侧）。"""
    moment = parse_ts(ts_text)
    if moment is None:
        return True
    return moment <= (now if now is not None else now_utc())
