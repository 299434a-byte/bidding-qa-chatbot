# ============================================================================
# 文件：tests/test_pg_guard.py —— 给「SQL 只读护栏」做体检
#
# 被测对象是 backend/app/pg.py 的 validate_sql：它把大模型生成的 SQL 过一遍规则，
# 合规才放行、违规就抛 PgError。这里只测纯字符串规则，不连数据库。
# ============================================================================

from __future__ import annotations

import pytest

from app.pg import PgError, validate_sql


def test_validate_sql_rejects_write_statements():
    """写操作必须被直接拒绝。"""
    # 分开断言 DELETE / UPDATE 两种形态，避免出现「只挡了一种」的假安全。
    with pytest.raises(PgError):
        validate_sql("DELETE FROM procurement_notices", 100)
    with pytest.raises(PgError):
        validate_sql("UPDATE procurement_notices SET budget = 0", 100)


def test_validate_sql_rejects_stacked_statements():
    """分号堆叠的第二条语句必须被拒。"""
    # 故意用两条「本身合法」的 SELECT 叠加：这样能证明拒绝原因是「多语句」，
    # 而不是被写操作关键字误伤，否则测不出这条规则本身。
    with pytest.raises(PgError):
        validate_sql("SELECT * FROM a; SELECT * FROM b", 100)


def test_validate_sql_rejects_non_select_start():
    """不以 SELECT / WITH 开头的语句一律拒绝。"""
    # 单用 DROP 测「开头即非法」，与「含禁用词」是两条独立规则，不能混为一谈。
    with pytest.raises(PgError):
        validate_sql("DROP TABLE procurement_notices", 100)


def test_validate_sql_rejects_empty():
    """空语句（模型啥也没生成）不能放行。"""
    with pytest.raises(PgError):
        validate_sql("", 100)


def test_validate_sql_appends_limit_when_missing():
    """规范化：没有 LIMIT 的查询要自动补上传入的 max_rows。"""
    # 这是「防止大表全量拉回」的兜底；断言补的正是 max_rows，而非某个硬编码值。
    assert validate_sql("SELECT * FROM procurement_notices", 100) == (
        "SELECT * FROM procurement_notices LIMIT 100"
    )


def test_validate_sql_shrinks_oversized_limit():
    """规范化：已有 LIMIT 但超过上限时，要改小到 max_rows。"""
    assert validate_sql("SELECT * FROM procurement_notices LIMIT 9999", 50) == (
        "SELECT * FROM procurement_notices LIMIT 50"
    )