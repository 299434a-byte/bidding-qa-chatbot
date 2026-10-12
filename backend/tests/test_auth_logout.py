# ============================================================================
# 文件：tests/test_auth_logout.py —— 「注销必须持有合法令牌」回归测试
#
# 【这个测试文件整体在测什么】
#   背景 bug：_bearer 在 Authorization 头缺失/格式不对时静默返回 None，
#   require_bearer 原样透传，auth.logout(s, None) 什么都不吊销也照样走完，
#   接口若因此返回 {"ok": true} —— 前端以为已登出，令牌却还有效。
#   （接口层另有 current_user 依赖挡着，但 require_bearer 注释写着
#   「格式不对会抛错、翻译成 400/401」，实现根本没抛 —— 注释与实现打架。）
#
#   本文件锁住四件事：
#     ① 缺 Authorization 头 / 格式不对 / 令牌不认识 → 一律 401，绝不 ok:true（规格）；
#     ② 合法令牌注销 → 200，且令牌真的被吊销（随后 /api/auth/me 也 401）；
#     ③ 库层双保险：require_bearer / auth.logout 拿不到令牌就抛 Unauthorized，
#        不许静默成功；revoke 本身幂等——重复注销不抛异常；
#     ④ 令牌格式与 TTL 规则不动（这里只测「拿没拿到令牌」，不测签发细节）。
# ============================================================================

from __future__ import annotations

# pytest：fixture。
import pytest
# TestClient：把 FastAPI 应用当普通对象发请求，不起真实服务器。
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.main import app


@pytest.fixture()
def client():
    """TestClient；顺手清掉限流桶，避免连打被限（与其它接口测试同口径）。"""
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


# ----------------------------------------------------------------------------
# 用例 1（规格用例）：拿不到合法令牌 → 401，绝不 ok:true
# ----------------------------------------------------------------------------
def test_logout_without_authorization_header_returns_401(client: TestClient) -> None:
    """缺 Authorization 头注销 → 401。

    修复前的隐患：_bearer 返回 None → logout 空跑一趟 → 若回 ok:true，
    前端以为已登出，令牌仍有效。
    """
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 401
    assert resp.json().get("ok") is not True


def test_logout_with_malformed_header_returns_401(client: TestClient) -> None:
    """Authorization 头格式不对（不是 Bearer / 没带令牌）→ 401。"""
    for header in ("Token abc.def.ghi", "Basic dXNlcjpwYXNz", "Bearer", "Bearer   "):
        resp = client.post("/api/auth/logout", headers={"Authorization": header})
        assert resp.status_code == 401, header
        assert resp.json().get("ok") is not True, header


def test_logout_with_unknown_token_returns_401(client: TestClient) -> None:
    """格式对但令牌不认识（没登录过）→ 也拿不到合法令牌 → 401。"""
    resp = client.post("/api/auth/logout", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json().get("ok") is not True


# ----------------------------------------------------------------------------
# 用例 2（规格用例）：合法令牌注销 → 200，且令牌真的被吊销
# ----------------------------------------------------------------------------
def test_logout_with_valid_token_returns_200_and_revokes(client: TestClient) -> None:
    """正常令牌注销 → 200 ok:true；事后拿同一令牌查 /api/auth/me 必须 401（吊销生效）。"""
    token = _register(client, "logout_user_1")
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.post("/api/auth/logout", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["username"] == "logout_user_1"

    # 吊销必须落库：同一令牌随后就不再是「合法令牌」。
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 401


# ----------------------------------------------------------------------------
# 用例 3：库层双保险 —— 拿不到令牌抛 Unauthorized，revoke 幂等
# ----------------------------------------------------------------------------
def test_require_bearer_raises_instead_of_returning_none() -> None:
    """require_bearer 不许再静默返回 None：拿不到合法令牌抛 Unauthorized。"""
    with pytest.raises(auth.Unauthorized):
        auth.require_bearer(None)
    with pytest.raises(auth.Unauthorized):
        auth.require_bearer("Token abc")
    with pytest.raises(auth.Unauthorized):
        auth.require_bearer("Bearer")
    # 合法格式原样返回（大小写不敏感、只切一刀 —— 令牌格式规则不变）。
    assert auth.require_bearer("Bearer abc.def") == "abc.def"
    assert auth.require_bearer("bearer  abc.def") == "abc.def"


def test_logout_without_token_raises_not_silent_success() -> None:
    """auth.logout(s, None)：不吊销任何东西就不许「返回成功」，必须抛 Unauthorized。"""
    s = get_settings()
    with pytest.raises(auth.Unauthorized):
        auth.logout(s, None)
    with pytest.raises(auth.Unauthorized):
        auth.logout(s, "")


def test_revoke_and_repeat_logout_are_idempotent(client: TestClient) -> None:
    """吊销是幂等的：同一令牌 revoke/logout 各来两遍都不抛异常。

    （HTTP 层第二次注销会 401——令牌已吊销就不再是「合法令牌」，
    这与「拿不到合法令牌返回 401」同一口径；状态上始终是已登出。）
    """
    s = get_settings()
    token = _register(client, "logout_user_2")

    auth.revoke_token(s, token)
    auth.revoke_token(s, token)  # 第二遍：删 0 行，不算错
    auth.logout(s, token)
    auth.logout(s, token)  # 重复注销不抛异常
