# ============================================================================
# 文件：tests/test_graph_driver.py —— Neo4j 驱动的「只读模式 / 查询超时 / 关闭钩子」
#
# 【这个测试文件整体在测什么】
#   ① 驱动创建时必须带 default_access_mode=READ —— 连 session.run 这种自动提交语句
#      也被服务端按只读对待（此前只有 execute_read 是只读，自省路径是默认写模式）；
#   ② 查询时长的真正上限是「事务超时」（Query 的 timeout），来自 NEO4J_QUERY_TIMEOUT，
#      与只管建连的 NEO4J_TIMEOUT 分开；
#   ③ 进程退出钩子 _close_drivers 会关掉缓存的驱动并清空缓存。
#   全部用假驱动/假会话离线断言，不连 Neo4j；真连库的检查放在最后的人工验证用例里。
# ============================================================================

from __future__ import annotations

# os：人工验证用例要读 NEO4J_LIVE_TEST 环境开关。
import os
# SimpleNamespace：造只含 neo4j 连接字段的配置替身。
from types import SimpleNamespace

import pytest

from app import graph


def _settings(**overrides) -> SimpleNamespace:
    """轻量配置替身：只带 graph 连接/超时用到的字段。"""
    values = dict(
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_user="test",
        neo4j_password="test",
        neo4j_database="neo4j",
        neo4j_timeout=30,
        neo4j_query_timeout=15,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _clear_driver_cache():
    """驱动缓存是模块级字典——每个用例前后清掉，避免互相污染。"""
    graph._drivers.clear()
    yield
    graph._close_drivers()


# ----------------------------------------------------------------------------
# 用例 1：驱动必须按「只读访问模式」创建
# ----------------------------------------------------------------------------
def test_driver_created_with_read_only_default(monkeypatch) -> None:
    """default_access_mode=READ：会话语句一律按只读对待（含 _introspect_rows 的 session.run）。"""
    import neo4j

    captured: dict = {}

    def fake_driver(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", fake_driver)
    graph._get_driver(_settings())

    assert captured["uri"] == "bolt://127.0.0.1:7687"
    # READ_ACCESS 就是 "READ"：写操作会被服务端在访问模式这一层拒绝。
    assert captured["default_access_mode"] == neo4j.READ_ACCESS
    # 建连超时仍在（NEO4J_TIMEOUT 的职责不变）。
    assert captured["connection_timeout"] == 30


# ----------------------------------------------------------------------------
# 用例 2：关闭钩子关掉缓存驱动并清空缓存
# ----------------------------------------------------------------------------
def test_close_drivers_closes_cached_and_clears() -> None:
    """_close_drivers：每个缓存驱动都 close()，缓存清空（幂等、不抛异常）。"""
    closed = []

    class FakeDriver:
        def close(self):
            closed.append(True)

    graph._drivers[("bolt://x", "u", "p", "neo4j")] = FakeDriver()
    graph._drivers[("bolt://y", "u", "p", "neo4j")] = FakeDriver()

    graph._close_drivers()
    assert closed == [True, True]
    assert graph._drivers == {}
    # 再调一次不报错（退出钩子可能被重复触发）。
    graph._close_drivers()


# ----------------------------------------------------------------------------
# 用例 3/4：两条执行路径都带「事务超时」
# ----------------------------------------------------------------------------
class _FakeResult:
    def data(self):
        return [{"n": 1}]


class _FakeTx:
    def __init__(self, sink):
        self._sink = sink

    def run(self, query):
        self._sink.append(query)
        return _FakeResult()


class _FakeSession:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_read(self, fn):
        return fn(_FakeTx(self._sink))

    def run(self, query):
        self._sink.append(query)
        return _FakeResult()


def test_read_rows_attaches_query_timeout(monkeypatch) -> None:
    """_read_rows（模型生成的 Cypher）：语句带事务超时，文本原样。"""
    sink: list = []
    monkeypatch.setattr(graph, "_session", lambda s: _FakeSession(sink))

    rows = graph._read_rows(_settings(), "MATCH (n) RETURN n LIMIT 5")

    assert rows == [{"n": 1}]
    assert sink[0].text == "MATCH (n) RETURN n LIMIT 5"
    assert sink[0].timeout == 15.0


def test_introspect_rows_attaches_query_timeout(monkeypatch) -> None:
    """_introspect_rows（写死的自省语句）：同样带事务超时，不许无限跑。"""
    sink: list = []
    monkeypatch.setattr(graph, "_session", lambda s: _FakeSession(sink))

    rows = graph._introspect_rows(_settings(), "CALL db.labels() YIELD label RETURN label")

    assert rows == [{"n": 1}]
    assert sink[0].timeout == 15.0


def test_query_timeout_zero_means_unlimited() -> None:
    """NEO4J_QUERY_TIMEOUT 配 0/负数 = 不限时（Query.timeout 置 None，用库默认）。"""
    assert graph._query("RETURN 1", _settings(neo4j_query_timeout=0)).timeout is None
    assert graph._query("RETURN 1", _settings(neo4j_query_timeout=-3)).timeout is None


# ----------------------------------------------------------------------------
# 人工验证（默认跳过）：真连库的部分
# ----------------------------------------------------------------------------
@pytest.mark.skipif(
    os.environ.get("NEO4J_LIVE_TEST") != "1",
    reason="连库部分标人工验证：起好本机 Neo4j、配好 .env 后设 NEO4J_LIVE_TEST=1 手动跑",
)
def test_live_readonly_mode_and_query_timeout() -> None:
    """人工验证三件事：正常查询可用；写操作被只读模式拒绝；慢查询被事务超时终止。"""
    from app.config import get_settings

    s = get_settings()
    # 1) 固定自省查询可用（走 session.run，默认只读模式）。
    assert isinstance(graph._introspect_rows(s, "CALL db.labels() YIELD label RETURN label LIMIT 5"), list)

    # 2) 只读访问模式下写操作应被服务端拒绝。
    with pytest.raises(graph.GraphError):
        graph._read_rows(s, "CREATE (n:__probe__) RETURN n")

    # 3) 事务超时：把超时压到 1 秒跑一条重查询，应被服务端终止而不是一直挂着。
    tiny = _settings(
        neo4j_uri=s.neo4j_uri,
        neo4j_user=s.neo4j_user,
        neo4j_password=s.neo4j_password,
        neo4j_database=s.neo4j_database,
        neo4j_query_timeout=1,
    )
    with pytest.raises(graph.GraphError):
        graph._read_rows(tiny, "UNWIND range(1, 300000000) AS x RETURN count(x) AS c")
