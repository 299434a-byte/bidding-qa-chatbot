# ============================================================================
# 文件：tests/test_history_date_filter.py —— 历史搜索「按日期筛选漏掉当天」回归测试
#
# 【这个测试文件整体在测什么】
#   背景 bug：库里时间戳有两种格式混存——
#     · 老数据：naive 本地文本（形如 "2026-10-12 08:00:00"，按东八区语义，见 timeutil.py）；
#     · 新数据：UTC ISO 文本（形如 "2026-10-12T08:00:00+00:00"，带 T 与偏移）。
#   日期筛选把用户给的日期补成 "YYYY-MM-DD 23:59:59"（空格分隔）后直接与 created_at
#   做字符串比较：'T'(84) > ' '(32)，于是「结束日当天」的 T 格式记录全部被上界挡掉
#   （实测 70 条只命中 64）。另外库里存 UTC、用户给本地日期，还有 8 小时偏移：
#   UTC 存的「本地凌晨」记录会被算到前一天。
#
#   本文件锁住四件事：
#     ① 同一天的老 naive + 新 aware 记录，按「当天」过滤两条都必须命中（规格用例）；
#     ② 老 naive 晚间记录不因修复而漏（护栏：naive 按东八区解释）；
#     ③ 「本地凌晨」的 aware 记录（UTC 前一天）也算当天（8 小时偏移修复点）；
#        「本地次日凌晨」的 aware 记录不算当天（不能修过头）。
#     ④ FTS 降级路径 _search_like 与主路径吃同一套归一化（两处比较都改）。
#   全部走 SQLite 临时文件 + 轻量配置替身，离线运行。
# ============================================================================

from __future__ import annotations

# SimpleNamespace：造只含 history 所需字段的配置替身（不加载真实 Settings）。
from types import SimpleNamespace

# 被测模块：历史存储。
from app import history


def _settings(tmp_path) -> SimpleNamespace:
    """轻量配置替身：库落在 pytest 临时目录，消息/标题上限给足（不测截断）。

    ``history_configured=True``：history 入口先检查「历史功能已开启」。
    """
    return SimpleNamespace(
        history_db_path=tmp_path / "conversations.db",
        history_configured=True,
        history_max_message_chars=20000,
        history_title_max_chars=30,
    )


def _seed(tmp_path, monkeypatch, records: list[tuple[str, str]]) -> tuple[SimpleNamespace, str, dict[str, str]]:
    """按指定 created_at 文本插入消息；返回 (配置, user_id, {正文: message_id})。

    ``append_message`` 自己调 ``_now()`` 取当前时间——这里 monkeypatch 掉 ``_now``，
    让每条消息的 created_at 完全受控（不许有「跑测试的那一刻」这种不稳定因素）。
    """
    s = _settings(tmp_path)
    user = history.create_user(s, "日期筛选用户", "hash", "salt", 1)
    conv = history.create_conversation(s, user["id"], "日期筛选会话", "react")
    ids: dict[str, str] = {}
    for created_at, content in records:
        monkeypatch.setattr(history, "_now", lambda t=created_at: t)
        saved = history.append_message(s, user["id"], conv["id"], "user", content)
        ids[content] = saved["id"]
    return s, user["id"], ids


# ----------------------------------------------------------------------------
# 用例 1（规格用例）：同一天的老 naive + 新 aware，按当天过滤两条都命中
# ----------------------------------------------------------------------------
def test_same_day_hits_naive_and_aware_records(tmp_path, monkeypatch) -> None:
    """date_from=date_to="2026-10-12"：老 "2026-10-12 08:00:00" 与
    新 "2026-10-12T08:00:00+00:00" 都必须命中。

    修复前失败原因：上界补成 "2026-10-12 23:59:59"（空格），
    'T'(84) > ' '(32)，T 格式记录 `created_at <= 上界` 恒假。
    """
    s, user_id, ids = _seed(
        tmp_path,
        monkeypatch,
        [
            ("2026-10-12 08:00:00", "老格式消息"),
            ("2026-10-12T08:00:00+00:00", "新格式消息"),
        ],
    )

    items, total = history.search_messages(
        s, user_id, date_from="2026-10-12", date_to="2026-10-12"
    )
    hit_ids = {i["message_id"] for i in items}
    assert ids["老格式消息"] in hit_ids, "老 naive 记录应命中当天"
    assert ids["新格式消息"] in hit_ids, "新 aware 记录应命中当天（修复前被 T>空格 挡掉）"
    assert total >= 2


# ----------------------------------------------------------------------------
# 用例 2（护栏）：老 naive 晚间记录不能因修复而漏
# ----------------------------------------------------------------------------
def test_naive_evening_record_still_hits(tmp_path, monkeypatch) -> None:
    """naive "2026-10-12 20:00:00"（东八区晚间）必须命中 2026-10-12。

    如果修复时只把边界换成 UTC、却不把 naive 值按东八区换算（naive 当 UTC 文本比），
    这条会被上界 "UTC 15:59:59" 误杀 —— 老记录一漏一大片，属于回归。
    """
    s, user_id, ids = _seed(
        tmp_path,
        monkeypatch,
        [("2026-10-12 20:00:00", "老格式晚间消息")],
    )

    items, _ = history.search_messages(s, user_id, date_from="2026-10-12", date_to="2026-10-12")
    hit_ids = {i["message_id"] for i in items}
    assert ids["老格式晚间消息"] in hit_ids, "naive 晚间记录按东八区解释，应命中当天"


# ----------------------------------------------------------------------------
# 用例 3：8 小时偏移 —— 本地凌晨算当天，本地次日凌晨不算当天
# ----------------------------------------------------------------------------
def test_utc_offset_keeps_local_morning_only(tmp_path, monkeypatch) -> None:
    """库里存 UTC、用户给本地日期：

    · aware "2026-10-11T17:00:00+00:00" = 东八区 10-12 01:00 → 算 10-12 的（修复前漏）；
    · aware "2026-10-12T16:30:00+00:00" = 东八区 10-13 00:30 → 不算 10-12 的（不能修过头）。
    """
    s, user_id, ids = _seed(
        tmp_path,
        monkeypatch,
        [
            ("2026-10-11T17:00:00+00:00", "本地凌晨消息"),
            ("2026-10-12T16:30:00+00:00", "本地次日凌晨消息"),
        ],
    )

    items, _ = history.search_messages(s, user_id, date_from="2026-10-12", date_to="2026-10-12")
    hit_ids = {i["message_id"] for i in items}
    assert ids["本地凌晨消息"] in hit_ids, "UTC 存的「本地凌晨」应命中本地当天"
    assert ids["本地次日凌晨消息"] not in hit_ids, "本地次日凌晨不应被算进当天"


# ----------------------------------------------------------------------------
# 用例 4：FTS 降级路径 _search_like 吃同一套归一化（两处比较都改）
# ----------------------------------------------------------------------------
def test_date_filter_also_fixed_in_like_fallback(tmp_path, monkeypatch) -> None:
    """_search_like（FTS 索引损坏时的降级路径）与 search_messages 共用时间语义。

    直接以 _normalize_range 的产物调用它，锁住「第二处比较」不会悄悄退回老写法。
    """
    s, user_id, ids = _seed(
        tmp_path,
        monkeypatch,
        [
            ("2026-10-12 08:00:00", "降级路径老格式"),
            ("2026-10-12T08:00:00+00:00", "降级路径新格式"),
        ],
    )
    start, end = history._normalize_range("2026-10-12", "2026-10-12")

    items, total = history._search_like(s, user_id, None, start, end, None, 20, 0)
    hit_ids = {i["message_id"] for i in items}
    assert ids["降级路径老格式"] in hit_ids
    assert ids["降级路径新格式"] in hit_ids
    assert total >= 2
