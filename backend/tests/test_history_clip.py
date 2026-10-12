# ============================================================================
# 文件：tests/test_history_clip.py —— 「单条消息长度上限」的截断语义
#
# 【这个测试文件整体在测什么】
#   _clip_len 的约定是「配置为 0（或负数）表示不限制」，但 append_message 的调用处
#   直接 `(content or "")[:_clip_len(settings)]` —— 切片 [:0] 恰好把正文静默清空成
#   空串。于是「不想限制长度」的配置反而把历史正文全吞了。
#   本文件锁住三件事：
#     ① limit=0（及负数）正文完整落库——「不限制」就是不限制（规格用例，修复前失败）；
#     ② limit=20000 时超长正文截断到 20000 字符，不整条丢弃；
#     ③ 截断后的正文能从 list_messages 读回（落库的是截断结果，不是只在返回值里）。
#   走 SQLite 临时文件 + 轻量配置替身，离线运行。
# ============================================================================

from __future__ import annotations

# SimpleNamespace：造只含 history 所需字段的配置替身（不加载真实 Settings）。
from types import SimpleNamespace

# 被测模块：历史存储。
from app import history


def _settings(tmp_path, max_chars: int) -> SimpleNamespace:
    """轻量配置替身；``history_max_message_chars`` 就是本文件要验的截断上限。"""
    return SimpleNamespace(
        history_db_path=tmp_path / "conversations.db",
        history_configured=True,
        history_max_message_chars=max_chars,
        history_title_max_chars=30,
    )


def _append(tmp_path, max_chars: int, content: str) -> str:
    """建好用户/会话后落一条消息，返回落库（截断后）的正文。"""
    s = _settings(tmp_path, max_chars)
    user = history.create_user(s, "截断用户", "hash", "salt", 1)
    conv = history.create_conversation(s, user["id"], "截断会话", "react")
    saved = history.append_message(s, user["id"], conv["id"], "user", content)
    return saved["content"]


# ----------------------------------------------------------------------------
# 用例 1（规格用例）：limit=0 表示不限制——正文必须完整，不许被 [:0] 清空
# ----------------------------------------------------------------------------
def test_limit_zero_keeps_full_content(tmp_path) -> None:
    """history_max_message_chars=0：落库正文 = 原文。

    修复前失败原因：`content[:_clip_len(settings)]` 在 _clip_len 返回 0 时
    等于 content[:0]，把正文静默清成空串。
    """
    content = "这是一条不短的问答正文，用来证明 limit=0 时不会被切片清空。" * 3

    stored = _append(tmp_path, 0, content)

    assert stored == content


def test_limit_negative_also_means_unlimited(tmp_path) -> None:
    """负数上限与 0 同义（_clip_len 会归一成 0）——与 _clip「limit<=0 不限制」一致。"""
    content = "负数上限也不能吞正文" * 5

    stored = _append(tmp_path, -1, content)

    assert stored == content


# ----------------------------------------------------------------------------
# 用例 2（规格用例）：limit=20000 时超长正文截断到 20000
# ----------------------------------------------------------------------------
def test_limit_20000_clips_long_content(tmp_path) -> None:
    """history_max_message_chars=20000（默认值）：20001 字正文截成 20000 字。"""
    content = "x" * 20001

    stored = _append(tmp_path, 20000, content)

    assert len(stored) == 20000
    assert stored == content[:20000]


def test_content_shorter_than_limit_untouched(tmp_path) -> None:
    """没超上限就不动——截断不能误伤短正文。"""
    content = "短正文"

    stored = _append(tmp_path, 20000, content)

    assert stored == content


# ----------------------------------------------------------------------------
# 用例 3：截断结果真的落库（list_messages 读回一致）
# ----------------------------------------------------------------------------
def test_clipped_content_persisted(tmp_path) -> None:
    """落库的是截断后的正文：list_messages 读回与 append_message 返回一致。"""
    content = "y" * 30
    s = _settings(tmp_path, 10)
    user = history.create_user(s, "落库用户", "hash", "salt", 1)
    conv = history.create_conversation(s, user["id"], "落库会话", "react")

    saved = history.append_message(s, user["id"], conv["id"], "user", content)
    rows = history.list_messages(s, user["id"], conv["id"])

    assert saved["content"] == content[:10]
    assert [r["content"] for r in rows] == [content[:10]]


def test_unlimited_content_persisted_in_full(tmp_path) -> None:
    """limit=0 时落库同样完整——不能返回值看是全的、库里却是空的。"""
    content = "z" * 100
    s = _settings(tmp_path, 0)
    user = history.create_user(s, "全量用户", "hash", "salt", 1)
    conv = history.create_conversation(s, user["id"], "全量会话", "react")

    history.append_message(s, user["id"], conv["id"], "user", content)
    rows = history.list_messages(s, user["id"], conv["id"])

    assert [r["content"] for r in rows] == [content]
