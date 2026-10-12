# ============================================================================
# 文件：tests/test_graph_guard.py —— 给「Cypher 只读护栏」做体检
#
# 被测对象是 backend/app/graph.py 的 validate_cypher / clean_cypher：
# 合规才放行、违规就抛 GraphError。这里只测纯字符串规则，不连 Neo4j。
# 与 test_pg_guard.py 对称 —— 两套护栏的 LIMIT 语义必须一致。
# ============================================================================

from __future__ import annotations

import pytest

from app.graph import GraphError, clean_cypher, validate_cypher


def test_validate_cypher_rejects_write_statements():
    """写操作必须被直接拒绝。"""
    with pytest.raises(GraphError):
        validate_cypher("CREATE (n {name: 'x'}) RETURN n", 100)
    with pytest.raises(GraphError):
        validate_cypher("MATCH (n) DETACH DELETE n", 100)


def test_validate_cypher_appends_limit_when_missing():
    """规范化：没有 LIMIT 的查询要自动补上传入的 max_rows。"""
    assert validate_cypher("MATCH (n) RETURN n.name", 100) == (
        "MATCH (n) RETURN n.name LIMIT 100"
    )


def test_validate_cypher_appends_limit_despite_string_literal():
    """字符串常量里的 limit 字样不能骗过护栏——否则该补的上限没补上（整图拉回）。"""
    cypher = "MATCH (n) WHERE n.name = 'no limit here' RETURN n"
    assert validate_cypher(cypher, 50) == f"{cypher} LIMIT 50"


def test_validate_cypher_limit_not_swallowed_by_trailing_comment():
    """末尾行尾注释不能吃掉补上的 LIMIT。"""
    out = validate_cypher("MATCH (n) RETURN n // 拉全部", 50)
    assert out == "MATCH (n) RETURN n LIMIT 50"
    # 短横线注释同理（任务口径 -- 与 // 都要挡）。
    out2 = validate_cypher("MATCH (n) RETURN n -- 拉全部", 50)
    assert out2 == "MATCH (n) RETURN n LIMIT 50"


def test_validate_cypher_replaces_limit_all_without_stacking():
    """LIMIT ALL 是「不限行数」：换成安全上限，且不能叠成 LIMIT ALL LIMIT 50。"""
    out = validate_cypher("MATCH (n) RETURN n LIMIT ALL", 50)
    assert out == "MATCH (n) RETURN n LIMIT 50"
    assert "ALL" not in out
    assert out.count("LIMIT") == 1


def test_validate_cypher_treats_fetch_first_as_row_cap():
    """FETCH FIRST n ROWS ONLY 视为已有上限：够小就不动；超限/没数字就换 LIMIT。"""
    assert validate_cypher("MATCH (n) RETURN n FETCH FIRST 5 ROWS ONLY", 50) == (
        "MATCH (n) RETURN n FETCH FIRST 5 ROWS ONLY"
    )
    assert validate_cypher("MATCH (n) RETURN n FETCH FIRST 500 ROWS ONLY", 50) == (
        "MATCH (n) RETURN n LIMIT 50"
    )


def test_clean_cypher_strips_trailing_comment_too():
    """clean_cypher 不止删整行注释，行尾注释也要剥——否则 LIMIT 被它吃掉。"""
    assert clean_cypher("MATCH (n) RETURN n // 拉全部") == "MATCH (n) RETURN n"
    # 整行注释仍然删掉（老行为不回归）。
    assert clean_cypher("// 取全部节点\nMATCH (n) RETURN n") == "MATCH (n) RETURN n"
    # 引号里的 //（如网址）不是注释，不能误伤。
    assert clean_cypher("MATCH (n) WHERE n.url = 'http://example.com' RETURN n") == (
        "MATCH (n) WHERE n.url = 'http://example.com' RETURN n"
    )


def test_run_graph_without_settings_returns_readable_failure():
    """state 缺 settings 不能让 KeyError 冒泡出工具——工具只返回可读提示。"""
    from app.graph import _run_graph

    state: dict = {}
    text = _run_graph("这家公司中标了哪些项目", state)
    assert state["error"]
    assert "settings" in text or "配置" in text
