# ============================================================================
# 文件：tests/test_account_status.py —— 账号停用 + 注册不提权 行为测试
#
# 【这个测试文件整体在测什么】
#   ① 注册接口忽略请求体里的任何权限字段（role/status/is_admin…），
#      落库一律默认角色 user / 状态 active —— 伪造字段提不了权；
#   ② 被停用账号：登录 → 403；带旧令牌调鉴权接口 → 403；
#      匿名问答通道不受影响（可选登录把停用令牌当匿名）；
#   ③ 后台管理：仅管理员可见可操作（非管理员 403）；不能停用自己；
#      停用后立即生效（登录 403），启用后恢复（登录成功）。
#   全部离线：账号库走 SQLite 沙箱，问答用假 Agent。
# ============================================================================

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, history
from app.config import get_settings
from app.main import app


@pytest.fixture()
def client(monkeypatch):
    """TestClient + 假 Agent（打掉问答主链路）+ 限流桶清零。"""

    async def fake_agent_events(*args, **kwargs):
        yield {"type": "delta", "text": "匿名回答"}
        yield {"type": "done", "mode": "agent_rag"}

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)
    from app import ratelimit

    ratelimit._buckets.clear()
    yield TestClient(app)


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _register(client: TestClient, username: str, **extra):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": "a-strong-pass-1", **extra},
    )


def _login(client: TestClient, username: str):
    return client.post("/api/auth/login", json={"username": username, "password": "a-strong-pass-1"})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------------
# 用例 1：注册伪造权限字段 → 落库仍是普通用户
# ----------------------------------------------------------------------------
def test_register_ignores_privilege_fields(client) -> None:
    """请求体塞 role=admin / status=disabled / is_admin=True —— 全部被丢弃。"""
    username = _unique("伪造提权")
    resp = _register(
        client,
        username,
        role="admin",
        status="disabled",
        is_admin=True,
        admin=True,
        permissions=["admin"],
    )
    assert resp.status_code == 200, resp.text

    stored = history.get_user_by_username(get_settings(), username)
    assert stored is not None
    assert stored["role"] == "user"
    assert stored["status"] == "active"
    # 注册返回体也不带任何特权字段暗示。
    body = resp.json()["user"]
    assert body.get("role", "user") == "user"
    assert body.get("status", "active") == "active"


# ----------------------------------------------------------------------------
# 用例 2：被停用账号登录 → 403「账号已停用」
# ----------------------------------------------------------------------------
def test_disabled_account_login_forbidden(client) -> None:
    username = _unique("停用登录")
    assert _register(client, username).status_code == 200
    s = get_settings()
    user = history.get_user_by_username(s, username)
    assert history.set_user_status(s, user["id"], "disabled") is not None

    resp = _login(client, username)
    assert resp.status_code == 403
    assert "账号已停用" in resp.json()["detail"]


# ----------------------------------------------------------------------------
# 用例 3：被停用账号带旧令牌调鉴权接口 → 403
# ----------------------------------------------------------------------------
def test_disabled_account_api_calls_forbidden(client) -> None:
    username = _unique("停用调用")
    token = _register(client, username).json()["token"]
    s = get_settings()
    user = history.get_user_by_username(s, username)
    history.set_user_status(s, user["id"], "disabled")

    for path in ("/api/auth/me", "/api/history/conversations", "/api/metrics"):
        resp = client.get(path, headers=_auth(token))
        assert resp.status_code == 403, f"{path} → {resp.status_code}"
        assert "账号已停用" in resp.json()["detail"]


# ----------------------------------------------------------------------------
# 用例 4：停用令牌走「可选登录」的问答通道 → 按匿名处理（不 403、不落库）
# ----------------------------------------------------------------------------
def test_disabled_token_becomes_anonymous_in_chat(client) -> None:
    username = _unique("停用问答")
    token = _register(client, username).json()["token"]
    s = get_settings()
    user = history.get_user_by_username(s, username)
    history.set_user_status(s, user["id"], "disabled")

    resp = client.post(
        "/api/agent/chat",
        json={"question": "市政道路招标有哪些？"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    # 匿名：conversation 事件里没有会话 id（不落库）。
    import json

    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    conversation = next(e for e in events if e["type"] == "conversation")
    assert conversation["id"] is None
    assert conversation["saved"] is False


# ----------------------------------------------------------------------------
# 用例 5：后台管理 —— 仅管理员可操作；不能停用自己；启用后立即恢复
# ----------------------------------------------------------------------------
def test_admin_can_disable_and_reenable(client) -> None:
    admin_name = _unique("管理员")
    victim_name = _unique("被停用")
    admin_token = _register(client, admin_name).json()["token"]
    victim_token = _register(client, victim_name).json()["token"]
    s = get_settings()
    admin = history.get_user_by_username(s, admin_name)
    victim = history.get_user_by_username(s, victim_name)
    history.set_user_role(s, admin["id"], "admin")

    # 非管理员碰用户管理 → 403。
    denied = client.get("/api/admin/users", headers=_auth(victim_token))
    assert denied.status_code == 403

    # 管理员列表可见。
    listed = client.get("/api/admin/users", headers=_auth(admin_token))
    assert listed.status_code == 200
    names = {u["username"] for u in listed.json()["items"]}
    assert admin_name in names and victim_name in names

    # 不能停用自己。
    self_disable = client.post(
        f"/api/admin/users/{admin['id']}/status",
        json={"status": "disabled"},
        headers=_auth(admin_token),
    )
    assert self_disable.status_code == 400

    # 停用受害者 → 立即生效：登录与鉴权接口都 403。
    disabled = client.post(
        f"/api/admin/users/{victim['id']}/status",
        json={"status": "disabled"},
        headers=_auth(admin_token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["user"]["status"] == "disabled"
    assert _login(client, victim_name).status_code == 403
    assert client.get("/api/auth/me", headers=_auth(victim_token)).status_code == 403

    # 启用后立即恢复。
    enabled = client.post(
        f"/api/admin/users/{victim['id']}/status",
        json={"status": "active"},
        headers=_auth(admin_token),
    )
    assert enabled.status_code == 200
    assert _login(client, victim_name).status_code == 200


# ----------------------------------------------------------------------------
# 用例 6：管理员白名单（AUTH_ADMIN_USERS）与 role=admin 等价
# ----------------------------------------------------------------------------
def test_admin_whitelist_equivalent_to_role() -> None:
    s = replace(get_settings(), auth_admin_users=("白名单用户",))
    assert auth.is_admin(s, {"username": "白名单用户", "role": "user"}) is True
    assert auth.is_admin(s, {"username": "路人", "role": "user"}) is False
    assert auth.is_admin(replace(get_settings(), auth_admin_users=()), {"username": "x", "role": "admin"}) is True
    assert auth.is_admin(s, None) is False
