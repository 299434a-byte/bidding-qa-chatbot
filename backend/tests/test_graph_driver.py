# ============================================================================
# 文件：tests/test_graph_driver.py —— Neo4j 驱动的「只读模式 / 查询超时 / 关闭钩子」
#
# 【这个测试文件整体在测什么】
#   ① 只读访问模式必须**真的拦住写语句**：假服务端按 session 级传入的
#      default_access_mode（驱动 v6 语义）模拟服务端语义，真的执行 CREATE → 抛错。
#      以前只断言「创建时传了 READ 参数」，
#      防线失效（忘传参数）时该断言照样绿，只制造安全感——现在改成行为断言；
#      建连超时（connection_timeout）的生效离线观察不到，验证放在人工验证用例（live）；
#   ② 查询时长的真正上限是「事务超时」（Query 的 timeout），来自 NEO4J_QUERY_TIMEOUT，
#      与只管建连的 NEO4J_TIMEOUT 分开；
#   ③ 进程退出钩子 _close_drivers 会关掉缓存的驱动并清空缓存；
#      连不上（uri 错误）时对外是可读的 GraphError，不是裸异常。
#   全部用假驱动/假会话离线断言，不连 Neo4j；真连库的检查放在最后的人工验证用例里。
# ============================================================================

from __future__ import annotations

# os：人工验证用例要读 NEO4J_LIVE_TEST 环境开关。
import os
# re：假服务端识别「写语句」用。
import re
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
# 用例 1：只读访问模式必须真的拦住写语句（行为：真的执行写 → 抛错）
# ----------------------------------------------------------------------------
_WRITE_SQL_RE = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|FOREACH)\b", re.IGNORECASE
)


class _ServerResult:
    def data(self) -> list[dict]:
        return [{"n": 1}]


class _ServerTx:
    def __init__(self, mode: str | None) -> None:
        self._mode = mode

    def run(self, query):
        text = str(getattr(query, "text", query))
        # 模拟服务端语义：READ_ACCESS 下执行写语句直接拒绝。
        if self._mode == "READ" and _WRITE_SQL_RE.search(text):
            raise RuntimeError("Cannot run a write query in read access mode")
        return _ServerResult()


class _ServerSession:
    def __init__(self, mode: str | None) -> None:
        self._mode = mode

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute_read(self, fn):
        return fn(_ServerTx(self._mode))

    def run(self, query):
        # 自省路径（session.run 自动提交）同样按驱动级访问模式对待。
        return _ServerTx(self._mode).run(query)


class _ServerDriver:
    """假 neo4j 服务端：按参数模拟连接成败与只读语义。

    访问模式按驱动 v6 语义从 **session 级**取（driver 级传入也兼容）：
    两处都没传 READ 时模式为 None，写语句会放行 → 行为断言照样能变红。
    """

    def __init__(self, uri: str, default_access_mode: str | None = None, **kwargs) -> None:
        if uri != "bolt://127.0.0.1:7687":
            # 只有目标地址上有「服务器」，其他地址连不上（行为化的 uri 检查）。
            raise ConnectionError(f"无法连接 {uri}：该地址上没有 Neo4j 服务器")
        self._mode = default_access_mode

    def session(self, database=None, default_access_mode: str | None = None, **kwargs) -> _ServerSession:
        return _ServerSession(default_access_mode or self._mode)

    def close(self) -> None:
        pass


def _install_server_driver(monkeypatch) -> None:
    import neo4j

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda uri, **kwargs: _ServerDriver(uri, **kwargs))


def test_read_only_mode_rejects_write_statements(monkeypatch) -> None:
    """只读模式下真的执行写语句必须被拒（GraphError），读语句照常返回行。

    【为什么不是断言创建参数】以前锁的是「创建时传了 default_access_mode=READ」——
    防线若失效（忘传参数），那条断言照样绿，只制造安全感。现在假服务端按拿到的
    访问模式模拟服务端行为：没传 READ 就会放行写语句、pytest.raises 失败，测试会红。
    """
    _install_server_driver(monkeypatch)

    # 读语句：只读模式下照常执行并返回行。
    assert graph._read_rows(_settings(), "MATCH (n) RETURN n LIMIT 1") == [{"n": 1}]

    # 写语句：真的执行要被只读防线拒绝（对外是可读的 GraphError）。
    with pytest.raises(graph.GraphError):
        graph._read_rows(_settings(), "CREATE (n:__probe__) RETURN n")

    # 自省路径（session.run，自动提交）同理：写语句同样被拒。
    with pytest.raises(graph.GraphError):
        graph._introspect_rows(_settings(), "CREATE (n:__probe__) RETURN n")


def test_unreachable_uri_raises_readable_graph_error(monkeypatch) -> None:
    """连不上的地址对外是可读的 GraphError（含「创建连接失败」提示），不是裸异常。"""
    _install_server_driver(monkeypatch)

    with pytest.raises(graph.GraphError) as excinfo:
        graph._read_rows(_settings(neo4j_uri="bolt://127.0.0.1:1"), "MATCH (n) RETURN n LIMIT 1")

    assert "创建 Neo4j 连接失败" in str(excinfo.value)


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
