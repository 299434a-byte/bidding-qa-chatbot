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


def test_validate_sql_appends_limit_despite_string_literal():
    """字符串常量里的 limit 字样不能骗过护栏——否则该补的上限没补上（整表拉回）。"""
    sql = "SELECT * FROM procurement_notices WHERE note = 'no limit here'"
    assert validate_sql(sql, 50) == f"{sql} LIMIT 50"


def test_validate_sql_limit_not_swallowed_by_trailing_comment():
    """末尾行尾注释不能吃掉补上的 LIMIT（拼接出来的上限被注释掉 = 护栏失效）。"""
    out = validate_sql("SELECT * FROM procurement_notices -- 拉全部", 50)
    assert out == "SELECT * FROM procurement_notices LIMIT 50"
    # 双斜杠行尾注释（模型偶尔会写）同理。
    out2 = validate_sql("SELECT * FROM procurement_notices // 拉全部", 50)
    assert out2 == "SELECT * FROM procurement_notices LIMIT 50"


def test_validate_sql_replaces_limit_all_without_stacking():
    """LIMIT ALL 是「不限行数」：换成安全上限，且不能叠成 LIMIT ALL LIMIT 50。"""
    out = validate_sql("SELECT * FROM procurement_notices LIMIT ALL", 50)
    assert out == "SELECT * FROM procurement_notices LIMIT 50"
    assert "ALL" not in out
    assert out.count("LIMIT") == 1


def test_validate_sql_treats_fetch_first_as_row_cap():
    """FETCH FIRST n ROWS ONLY 视为已有上限：够小就不动；超限/没数字就换 LIMIT。"""
    assert validate_sql("SELECT * FROM t FETCH FIRST 5 ROWS ONLY", 50) == (
        "SELECT * FROM t FETCH FIRST 5 ROWS ONLY"
    )
    assert validate_sql("SELECT * FROM t FETCH FIRST 500 ROWS ONLY", 50) == (
        "SELECT * FROM t LIMIT 50"
    )
    assert validate_sql("SELECT * FROM t FETCH FIRST ROWS ONLY", 50) == (
        "SELECT * FROM t LIMIT 50"
    )


def test_run_pg_without_settings_returns_readable_failure():
    """state 缺 settings 不能让 KeyError 冒泡出工具——工具只返回可读提示。"""
    from app.pg import _run_pg

    state: dict = {}
    text = _run_pg("预算超过一百万的项目有几个", state)
    # 失败信息回填进 state，返回的是人话提示而不是异常。
    assert state["error"]
    assert "settings" in text or "配置" in text