# ============================================================================
# 文件：tests/test_time_utc.py —— 时间统一到 UTC 的行为测试
#
# 【这个测试文件整体在测什么】
#   任务：存储与比较统一用 UTC（datetime.now(timezone.utc) + ISO 文本），
#   同时向后兼容 —— 历史 naive 字符串按其原本语义（东八区）解析，不让老数据失效。
#   本文件用离线单测锁住这三件事：
#     ① token 过期边界的 UTC 判定（刚好过期 / 刚好未过期各一条）；
#     ② history 时间戳「写 → 读」往返转换不丢秒；
#     ③ 老的 naive 文本按东八区解释（含由此产生的过期判定正确性）。
#   全部走 SQLite 临时文件 + 纯函数，不连 MySQL/PG/Neo4j/网络。
# ============================================================================

from __future__ import annotations

# datetime as dt：构造「刚好过期 / 刚好未过期」的精确时刻。
import datetime as dt
# SimpleNamespace：造只含 history_db_path 的轻量配置替身（history 只读这个字段）。
from types import SimpleNamespace

# 被测模块：历史存储（时间戳读写）与时间工具（解析/比较）。
from app import history, timeutil


def _settings(tmp_path) -> SimpleNamespace:
    """造一个只含历史库路径的配置替身（库落在 pytest 临时目录里）。

    ``history_configured=True``：history 的入口会先检查「历史功能已开启」，
    否则抛 HistoryUnavailable（真实 Settings 里这是 history_enabled+auth_enabled 的合成属性）。
    """
    return SimpleNamespace(
        history_db_path=tmp_path / "conversations.db",
        history_configured=True,
    )


# ----------------------------------------------------------------------------
# 用例 1：token 过期边界（UTC）—— 刚好未过期有效，刚好过期无效
# ----------------------------------------------------------------------------
def test_token_expiry_boundary_in_utc(tmp_path) -> None:
    """expires_at 刚好在未来 → 有效；刚好在过去 → 无效（比较走 UTC 时刻，不看字符串）。"""
    s = _settings(tmp_path)
    # 先建一个用户，会话要挂在真实用户上（JOIN 查询会用到）。
    user = history.create_user(s, "边界用户", "hash", "salt", 1)

    now = timeutil.now_utc()
    # 两条会话：一条过期时间在未来 1 秒（未过期），一条在过去 1 秒（已过期）。
    # 写库用的都是「UTC ISO 文本」——与 auth.issue_token 的新写法完全一致。
    live = "live-token-hash"
    dead = "dead-token-hash"
    history.create_session(
        s, live, user["id"], (now + dt.timedelta(seconds=1)).isoformat(), "test-agent"
    )
    history.create_session(
        s, dead, user["id"], (now - dt.timedelta(seconds=1)).isoformat(), "test-agent"
    )

    # 刚好未过期 → 能解析出用户。
    assert history.get_session_user(s, live) is not None
    # 刚好过期 → 按无效处理（返回 None）。
    assert history.get_session_user(s, dead) is None


# ----------------------------------------------------------------------------
# 用例 2：history 时间戳往返转换不丢秒
# ----------------------------------------------------------------------------
def test_history_timestamp_round_trip_keeps_seconds(tmp_path) -> None:
    """_now() 写出的文本经 timeutil.parse_ts 读回，秒数（乃至微秒）一粒不丢。"""
    # 取一个已知秒数/微秒数的 UTC 时刻，走「写 → 读」全程。
    known = dt.datetime(2026, 10, 11, 2, 18, 45, 123456, tzinfo=dt.timezone.utc)
    text = known.isoformat()
    back = timeutil.parse_ts(text)

    assert back is not None
    # 往返后完全相等（秒与微秒都保留）——若格式化丢了秒，这里立刻红。
    assert back == known
    assert back.second == 45

    # 真实写库路径：_now() 产出的文本同样能被解析，且秒数与字面一致。
    s = _settings(tmp_path)
    user = history.create_user(s, "往返用户", "hash", "salt", 1)
    written = user["created_at"]
    parsed = timeutil.parse_ts(written)
    assert parsed is not None
    # 字面秒数（文本第 17~18 位）与解析结果的秒数一致 —— 「不丢秒」。
    assert parsed.second == int(written[17:19])


# ----------------------------------------------------------------------------
# 用例 3：老的 naive 字符串按东八区解释（向后兼容）
# ----------------------------------------------------------------------------
def test_legacy_naive_string_is_interpreted_as_utc_plus_8() -> None:
    """历史数据里的 naive 文本（无时区）按东八区解析，换算到 UTC 后正好差 -8 小时。"""
    legacy = "2026-09-29 10:00:00"  # 老格式：本机（东八区）本地时间
    parsed = timeutil.parse_ts(legacy)

    assert parsed is not None
    # 东八 10:00 == UTC 02:00。
    assert parsed == dt.datetime(2026, 9, 29, 2, 0, 0, tzinfo=dt.timezone.utc)

    # 由此产生过期判定的正确性：老格式写的「未来时刻」不能被误判成已过期。
    # 东八 23:00 == UTC 15:00；以 UTC 14:00 为参照，它还在未来。
    future_legacy = "2026-09-29 23:00:00"
    ref = dt.datetime(2026, 9, 29, 14, 0, 0, tzinfo=dt.timezone.utc)
    assert timeutil.is_past(future_legacy, now=ref) is False
    # 同一文本以 UTC 16:00 为参照就已过期。
    assert timeutil.is_past(future_legacy, now=dt.datetime(2026, 9, 29, 16, 0, 0, tzinfo=dt.timezone.utc)) is True


# ----------------------------------------------------------------------------
# 用例 4：坏文本 / 空文本 → 一律按「已过去」处理，不抛异常
# ----------------------------------------------------------------------------
def test_unparseable_timestamp_counts_as_past() -> None:
    """解析不了的时间文本按已过期处理（安全侧：宁可要求重登，不放过期令牌）。"""
    assert timeutil.is_past("") is True
    assert timeutil.is_past("not-a-time") is True
    assert timeutil.is_past(None) is True
