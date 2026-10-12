# ============================================================================
# 文件：tests/test_ingest_request_validation.py —— 建库接口 mode/recreate 互斥校验
#
# 【这个测试文件整体在测什么】
#   POST /api/ingest 的两个配置陷阱（修之前的行为）：
#   ① IngestRequest.recreate 曾默认 True → 只传 mode="incremental" 立刻 400
#      （默认值与增量模式天然互斥，前端得靠 recreate: mode==="full" 手动规避）；
#   ② mode="full" + recreate=false 既不算差集也不删除 → 源表已删的记录变成
#      僵尸点永久留在向量库里被检索，接口却照常接受。
#
#   现在的规则（README「建知识库」有互斥表）：
#     · incremental + recreate=true  → 400「增量不能重建集合」
#     · full + recreate=false        → 400「全量必须重建集合」
#     · recreate 默认 false（增量开箱即用；全量必须显式 recreate=true）
#
# 【为什么 monkeypatch 掉 _ingest_worker】
#   合法组合会真的启动后台建库线程（去连向量库、加载向量化模型），
#   测试只需要验证「通过了接口校验」，所以把 worker 换成空跑。
# ============================================================================

from __future__ import annotations

# pytest：fixture / monkeypatch。
import pytest
# TestClient：把 FastAPI 应用当普通对象发请求，不起真实服务器。
from fastapi.testclient import TestClient

# 被测对象。
from app.main import IngestRequest, app


@pytest.fixture()
def client():
    """TestClient；每个用例独立，顺手清掉限流桶与建库状态。"""
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


def _post_ingest(client: TestClient, token: str, payload: dict):
    """带令牌 POST /api/ingest。"""
    return client.post(
        "/api/ingest",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


# --------------------------------------------------------------------------- #
# ① 模型默认值
# --------------------------------------------------------------------------- #
def test_recreate_defaults_to_false() -> None:
    """recreate 默认必须是 false：增量不传 recreate 应开箱即用（曾默认 true 导致必 400）。"""
    payload = IngestRequest()
    assert payload.recreate is False
    # mode 仍默认 full（向后兼容：老调用方不传 mode 时行为不变）。
    assert payload.mode == "full"


# --------------------------------------------------------------------------- #
# ② 两种矛盾组合 → 400
# --------------------------------------------------------------------------- #
def test_incremental_with_recreate_true_is_400(client: TestClient) -> None:
    """增量 + 重建 → 400，文案点明「增量不能重建集合」。"""
    token = _register(client, "ingest_valid_user_1")
    resp = _post_ingest(client, token, {"mode": "incremental", "recreate": True})
    assert resp.status_code == 400, resp.text
    assert "增量不能重建集合" in resp.json()["detail"]


def test_full_with_recreate_false_is_400(client: TestClient) -> None:
    """全量 + 不重建 → 400，文案说明「已删除数据不会清理」的后果。"""
    token = _register(client, "ingest_valid_user_2")
    resp = _post_ingest(client, token, {"mode": "full", "recreate": False})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "全量必须重建集合" in detail
    # 必须把「僵尸点」这个后果讲清楚，调用方才知道为什么要传 recreate=true。
    assert "不会清理" in detail


# --------------------------------------------------------------------------- #
# ③ 两种合法组合 → 200（worker 换成空跑，避免真启动建库线程）
# --------------------------------------------------------------------------- #
def test_incremental_default_passes_validation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """增量不传 recreate（用默认 false）→ 通过校验、正常启动（曾因默认 true 必 400）。"""
    monkeypatch.setattr("app.main._ingest_worker", lambda mode, recreate: None)
    token = _register(client, "ingest_valid_user_3")
    resp = _post_ingest(client, token, {"mode": "incremental"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True
    # 用完把状态牌复位，别让「running=True」影响后续用例（409 检查）。
    from app.main import _ingest_state, _state_lock

    with _state_lock:
        _ingest_state.update(running=False, stage="idle")


def test_full_with_recreate_true_passes_validation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量显式 recreate=true → 通过校验、正常启动。"""
    monkeypatch.setattr("app.main._ingest_worker", lambda mode, recreate: None)
    token = _register(client, "ingest_valid_user_4")
    resp = _post_ingest(client, token, {"mode": "full", "recreate": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True
    from app.main import _ingest_state, _state_lock

    with _state_lock:
        _ingest_state.update(running=False, stage="idle")
