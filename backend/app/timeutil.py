# ============================================================================
# 文件：timeutil.py —— 全项目统一的「时间取值 / 解析 / 比较」工具
#
# 【这个文件是干什么的】
#   把「现在几点」「文本怎么存」「老数据怎么读」三件事收拢到一处，
#   保证存储与比较统一用 UTC —— 部署到 TZ=UTC 的容器/服务器时，
#   token 过期判断、TTL 计算不会再因为「本地时区不同」偏移几小时。
#
# 【为什么要「新老两套语义」】（向后兼容的关键）
#   · 新写入的时间戳一律是 UTC 的 ISO 文本（带 +00:00 偏移，机器可判别）；
#   · 老数据里已经存下的 naive 文本（形如 "2026-09-29 10:00:00"，不带时区）
#     当年是按「本机本地时间」写进去的，本项目开发环境默认东八区，
#     所以解析这类老文本时按东八区解释 —— 让老数据全部继续有效，不会突然全部过期。
#   · 判断标准很简单：文本里带不带时区偏移。带偏移 = 新数据按其自带时区；
#     不带偏移 = 老数据按东八区。绝不能「一律按 UTC 解析老文本」，那会让老 token
#     立刻过期 8 小时（或反过来凭空多活 8 小时）。
#
# 【展示层怎么办】库里只存 UTC，显示给用户时由前端（或后端格式化函数）换算成本地时间。
#   默认展示时区为东八区；服务器也可用 TZ=Asia/Shanghai 统一整个进程的本地时区。
#   绝不把「换算后的本地时间」写回数据库。
# ============================================================================
"""时间工具：存储/比较统一 UTC；历史 naive 文本按东八区（UTC+8）解释。"""

from __future__ import annotations

# datetime：时间对象与解析/格式化都在这个标准库模块里。
import datetime as dt

# UTC：感知型（aware）时间的基准时区。datetime.timezone.utc 是标准写法。
UTC = dt.timezone.utc

# LEGACY_TZ —— 历史 naive 文本的语义时区：东八区（UTC+8）。
# 【为什么固定写 +08:00 而不是用 ZoneInfo("Asia/Shanghai")】东八区不实行夏令时，
#   固定偏移与区域时区规则完全一致；固定偏移还不依赖系统 tzdata，任何环境行为一致。
LEGACY_TZ = dt.timezone(dt.timedelta(hours=8))


def now_utc() -> dt.datetime:
    """当前 UTC 时间（感知型，带 +00:00）。"""
    return dt.datetime.now(UTC)


def now_iso() -> str:
    """当前 UTC 时间的 ISO 文本（带时区偏移），用于写库。"""
    return now_utc().isoformat()


def parse_ts(text: str | None) -> dt.datetime | None:
    """把库里的时间文本解析成「感知型 UTC 时间」；解析不了返回 None。

    · 带时区偏移的新格式（如 ``2026-10-11T02:18:00.123456+00:00``）—— 按自带偏移理解；
    · 不带时区的旧格式（如 ``2026-09-29 10:00:00``）—— 视为东八区本地时间（见文件头）。
    """
    if not text:
        return None
    try:
        value = dt.datetime.fromisoformat(str(text))
    except Exception:
        # 【静默降级】文本坏掉不当成致命错误：调用方拿到 None 自行兜底。
        return None
    # naive（无时区信息）= 老数据，按东八区补上时区，使其成为可比较的感知型时间。
    if value.tzinfo is None:
        value = value.replace(tzinfo=LEGACY_TZ)
    # 统一换算成 UTC，方便直接与 now_utc() 做差值/比较。
    return value.astimezone(UTC)


def is_past(ts_text: str | None, *, now: dt.datetime | None = None) -> bool:
    """判断「这个时间点是否已过去」；解析不了的文本一律当作已过去（安全侧）。"""
    moment = parse_ts(ts_text)
    if moment is None:
        # 无法解析 = 不可信 = 按「已过期/已过去」处理（宁可要求重新登录，不放过期令牌）。
        return True
    return moment <= (now if now is not None else now_utc())
