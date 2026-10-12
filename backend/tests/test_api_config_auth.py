# ============================================================================
# 文件：tests/test_api_config_auth.py —— /api/config 的鉴权与敏感字段回归测试
#
# 【这个测试文件整体在测什么】
#   ① 未登录 GET /api/config → 401（与 /api/metrics 同一登录门禁，口径一致）；
#   ② 登录后 → 200，且响应里搜不到内网拓扑片段：
#      "192.168"（内网主机）、"3306"（MySQL 端口）、"bolt://"（Neo4j 连接串）、
#      "/data/"（本地数据目录）；
#   ③ 结构断言：mysql / qdrant / bm25 / history / auth / postgres / neo4j 里
#      不再出现已移除的敏感键（防止将来有人「顺手加回来」）。
#
# 【为什么要注入标记值再断言】conftest 的假环境变量用的是 127.0.0.1 / 临时目录，
#   即使敏感字段没删干净也搜不到 192.168。所以先把 settings 里的主机、端口、
#   连接串、路径改成「含敏感标记的假值」，再断言响应文本不含这些标记——
#   删字段若回归，测试立刻失败。
# ============================================================================

from __future__ import annotations

# pytest：fixture / monkeypatch。
import pytest
# TestClient：把 FastAPI 应用当普通对象发请求，不起真实服务器。
from fastapi.testclient import TestClient

# 被测对象：应用入口与配置。
from app.config import get_settings
from app.main import app


@pytest.fixture()
def client():
    """TestClient；每个用例独立，顺手清掉限流桶避免连打被限。"""
    from app import ratelimit

    ratelimit._buckets.clear()
    return TestClient(app)


def _register(client: TestClient, username: str) -> str:
    """注册并拿令牌；同名已存在时（重跑测试）改走登录，保证用例可重复执行。"""
    resp = client.post("/api/auth/register", json={"username": username, "password": "test-pass-123"})
    if resp.status_code == 200:
        return resp.json()["token"]
    resp = client.post("/api/auth/login", json={"username": username, "password": "test-pass-123"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_config_requires_login(client: TestClient) -> None:
    """未登录访问 /api/config 必须 401（此前是匿名 200，内网拓扑裸奔）。"""
    resp = client.get("/api/config")
    assert resp.status_code == 401


def test_config_hides_sensitive_values(client: TestClient) -> None:
    """登录后 200，且响应文本搜不到内网主机 / 端口 / 连接串 / 数据目录。"""
    token = _register(client, "cfg_auth_user_1")
    # 把配置里的「地址类」字段改成含敏感标记的假值：字段若没删干净，下面就会搜到。
    # Settings 是 frozen dataclass，直接赋值会抛 FrozenInstanceError，
    # 这里用 object.__setattr__ 绕过冻结（仅测试内生效，finally 里恢复原值）。
    s = get_settings()
    original = {
        "mysql_host": s.mysql_host,
        "mysql_port": s.mysql_port,
        "pg_host": s.pg_host,
        "pg_port": s.pg_port,
        "neo4j_uri": s.neo4j_uri,
    }
    object.__setattr__(s, "mysql_host", "192.168.10.120")
    object.__setattr__(s, "mysql_port", 3306)
    object.__setattr__(s, "pg_host", "192.168.10.121")
    object.__setattr__(s, "pg_port", 5432)
    object.__setattr__(s, "neo4j_uri", "bolt://192.168.10.122:7687")
    try:
        resp = client.get("/api/config", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        body = resp.text
        for marker in ("192.168", "3306", "bolt://", "/data/"):
            assert marker not in body, f"响应仍泄露敏感片段：{marker}"
    finally:
        for key, value in original.items():
            object.__setattr__(s, key, value)


def test_config_drops_sensitive_keys(client: TestClient) -> None:
    """结构断言：敏感键名不能出现在响应里（防「顺手加回来」的回归）。"""
    token = _register(client, "cfg_auth_user_2")
    resp = client.get("/api/config", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # mysql：不再带 host / port / database / tables。
    mysql = data["mysql"]
    for key in ("host", "port", "database", "tables"):
        assert key not in mysql, f"mysql.{key} 不应回显"
    # qdrant：不再带 path。
    assert "path" not in data["qdrant"]
    # bm25：不再带 index_path。
    assert "index_path" not in data["bm25"]
    # history：不再带 db_path。
    assert "db_path" not in data["history"]
    # postgres：不再带 host / port / database。
    pg = data["postgres"]
    for key in ("host", "port", "database"):
        assert key not in pg, f"postgres.{key} 不应回显"
    # neo4j：不再带 uri / database / user。
    neo4j = data["neo4j"]
    for key in ("uri", "database", "user"):
        assert key not in neo4j, f"neo4j.{key} 不应回显"
    # auth：不再带 token_ttl_days / pbkdf2_iterations / max_login_attempts。
    auth_cfg = data["auth"]
    for key in ("token_ttl_days", "pbkdf2_iterations", "max_login_attempts"):
        assert key not in auth_cfg, f"auth.{key} 不应回显"

    # 非敏感信息仍保留：前端排障要用的 configured 布尔、模型名、调参项。
    assert "configured" in data["llm"]
    assert "model" in data["llm"]
    assert "dense_top_k" in data["retrieval"]
    assert "timeout" in data["rerank"]
