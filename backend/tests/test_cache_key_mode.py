# ============================================================================
# 文件：tests/test_cache_key_mode.py —— 缓存主键「必须含模式与 top_k」回归测试
#
# 【这个测试文件整体在测什么】
#   背景 bug：cache_key 只按「规范化问题」，不含请求模式与检索参数——
#   react 与 plan 对同一问题的答案完全不同（plan 会拆解子任务再综合），
#   却互相命中对方的答案（串答案）；同问题不同 top_k 同理。
#
#   本文件锁住四件事：
#     ① 同问题不同 mode 不互相命中（规格用例）；
#     ② 同 mode 同问题命中（规格用例，先证明「该命中的还命中」）；
#     ③ 不同 top_k 不互相命中（「影响答案的检索参数进键」的落点）；
#     ④ created_at 解析失败按未命中（fail-closed）并记 warning——
#        不许跳过 TTL 检查把脏数据当命中（fail-open）。
#   另补一条：老主键（纯规范化问题）的行自然不再命中（无需迁移）。
#   全部走 SQLite 临时文件 + 轻量配置替身，离线运行。
# ============================================================================

from __future__ import annotations

# logging：断言 warning 日志。
import logging
# sqlite3：直接往缓存表里塞一行「created_at 是垃圾值」的脏数据。
import sqlite3
# SimpleNamespace：造只含缓存所需字段的配置替身。
from types import SimpleNamespace

# 被测模块。
from app import answer_cache


def _settings(tmp_path, ttl: int = 3600) -> SimpleNamespace:
    """配置替身：缓存库落在 pytest 临时目录。"""
    return SimpleNamespace(cache_db_path=tmp_path / "answer_cache.db", cache_ttl=ttl)


# ----------------------------------------------------------------------------
# 用例 1（规格用例）：同问题不同 mode 不互相命中
# ----------------------------------------------------------------------------
def test_same_question_different_mode_do_not_cross_hit(tmp_path) -> None:
    """react 写入的答案，plan 去读必须未命中（反过来也一样）。"""
    s = _settings(tmp_path)
    assert answer_cache.set_cached("市政道路招标有哪些", "react 的答案", [], "agent_rag", s, key_mode="react") is True

    # 同问题、不同 mode：不许串答案。
    assert answer_cache.get_cached("市政道路招标有哪些", s, mode="plan") is None
    # 同问题、同 mode：正常命中。
    hit = answer_cache.get_cached("市政道路招标有哪些", s, mode="react")
    assert hit is not None
    assert hit["answer"] == "react 的答案"

    # 反向再验一次：plan 写入不影响已有的 react 行，也不被 react 读到。
    assert answer_cache.set_cached("市政道路招标有哪些", "plan 的答案", [], "agent_plan", s, key_mode="plan") is True
    assert answer_cache.get_cached("市政道路招标有哪些", s, mode="react")["answer"] == "react 的答案"
    assert answer_cache.get_cached("市政道路招标有哪些", s, mode="plan")["answer"] == "plan 的答案"


# ----------------------------------------------------------------------------
# 用例 2（规格用例）：同 mode 同问题命中（含书写差异仍归一）
# ----------------------------------------------------------------------------
def test_same_mode_same_question_hits(tmp_path) -> None:
    """同一 mode 下，大小写/空白不同的同一问题仍然命中（规范化语义不变）。"""
    s = _settings(tmp_path)
    assert answer_cache.set_cached("Hello   World", "答案正文", [{"title": "来源1"}], "agent_rag", s, key_mode="react") is True

    hit = answer_cache.get_cached("hello world", s, mode="React")
    assert hit is not None
    assert hit["answer"] == "答案正文"
    assert hit["sources"] == [{"title": "来源1"}]
    # mode 列存的是结束模式（回放用），不等于进键的请求模式。
    assert hit["mode"] == "agent_rag"


# ----------------------------------------------------------------------------
# 用例 3：不同 top_k 不互相命中
# ----------------------------------------------------------------------------
def test_different_top_k_do_not_cross_hit(tmp_path) -> None:
    """top_k 影响送进大模型的参考资料条数 → 同问题不同 top_k 是两条缓存。"""
    s = _settings(tmp_path)
    assert answer_cache.set_cached("同一问题", "5 条资料的答案", [], "llm", s, key_mode="llm", top_k=5) is True

    assert answer_cache.get_cached("同一问题", s, mode="llm", top_k=20) is None
    hit = answer_cache.get_cached("同一问题", s, mode="llm", top_k=5)
    assert hit is not None
    assert hit["answer"] == "5 条资料的答案"
    # 未指定 top_k（记 0）与显式 5 也不是同一条——宁可重算，不许凑合命中。
    assert answer_cache.get_cached("同一问题", s, mode="llm") is None


# ----------------------------------------------------------------------------
# 用例 4：cache_key 纯函数——模式与 top_k 都进键，规范化照旧
# ----------------------------------------------------------------------------
def test_cache_key_composes_question_mode_and_top_k() -> None:
    """主键三段：规范化问题 + 请求模式 + top_k；任何一段不同键就不同。"""
    base = answer_cache.cache_key("Hello  World", mode="react", top_k=5)
    # 书写差异归一后是同一个键。
    assert base == answer_cache.cache_key("hello world", mode="React", top_k=5)
    # 模式不同 → 键不同。
    assert base != answer_cache.cache_key("Hello  World", mode="plan", top_k=5)
    # top_k 不同 → 键不同。
    assert base != answer_cache.cache_key("Hello  World", mode="react", top_k=20)
    # 未指定 top_k 统一记 0（读写同口径）。
    assert answer_cache.cache_key("q", mode="react") == answer_cache.cache_key("q", mode="react", top_k=0)
    assert answer_cache.cache_key("q", mode="react", top_k=-3) == answer_cache.cache_key("q", mode="react", top_k=0)


# ----------------------------------------------------------------------------
# 用例 5：created_at 解析失败 → 未命中 + warning（fail-closed，不再 fail-open）
# ----------------------------------------------------------------------------
def test_unparsable_created_at_is_miss_and_logs_warning(tmp_path, caplog) -> None:
    """created_at 是垃圾值：修复前跳过 TTL 检查当命中（fail-open）；现在按未命中。"""
    s = _settings(tmp_path)
    assert answer_cache.set_cached("脏数据问题", "答案", [], "llm", s, key_mode="llm") is True
    # 手工把 created_at 改成解析不了的文本。
    with sqlite3.connect(str(s.cache_db_path)) as conn:
        conn.execute("UPDATE answer_cache SET created_at = 'not-a-timestamp'")

    with caplog.at_level(logging.WARNING, logger="app.answer_cache"):
        assert answer_cache.get_cached("脏数据问题", s, mode="llm") is None
    # 记了 warning，方便排查是哪行坏了。
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ----------------------------------------------------------------------------
# 用例 6：老主键的行自然失效（无需迁移）
# ----------------------------------------------------------------------------
def test_old_style_key_rows_naturally_miss(tmp_path) -> None:
    """老版本主键 = 纯规范化问题；新算法拼了模式与 top_k，对不上 → 不再命中。"""
    s = _settings(tmp_path)
    # 先写一条把表建好，再把它的 cache_key 改成「老键」的样子（纯规范化问题）。
    assert answer_cache.set_cached("老键问题", "旧答案", [], "llm", s, key_mode="llm") is True
    with sqlite3.connect(str(s.cache_db_path)) as conn:
        conn.execute(
            "UPDATE answer_cache SET cache_key = ?",
            (answer_cache.normalize_question("老键问题"),),
        )

    # 新算法查的是复合键，与老键对不上 → 自然不命中（老 key 失效，无需迁移）。
    assert answer_cache.get_cached("老键问题", s, mode="llm") is None
