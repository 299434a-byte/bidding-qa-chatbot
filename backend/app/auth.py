"""auth：账号体系（注册、登录、令牌签发与校验）。

职责：
  - PBKDF2-HMAC-SHA256 密码哈希存储与校验
  - 令牌生成、下发与请求态校验（optional_user / require_user）
  - 注销时校验并作废当前令牌

依赖：config、timeutil
被谁调用：main（鉴权相关端点）、ratelimit（登录态分桶）、history（按用户分区）
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from . import timeutil
from typing import Any

from fastapi import Header, HTTPException

from . import history
from .config import Settings, get_settings

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
_SALT_BYTES = 16


class AuthUnavailable(RuntimeError):
    """账号体系未开启，上层应返回 503。"""


class AuthError(RuntimeError):
    """注册/登录失败（用户名已存在、口令错误、被锁定等），消息可直接展示给用户。"""


class RateLimited(AuthError):
    """登录失败次数过多，暂时锁定。"""


class Unauthorized(AuthError):
    """拿不到合法令牌（头缺失 / 格式不对 / 令牌为空），上层应返回 401。"""


def _require_enabled(settings: Settings) -> None:
    if not settings.auth_enabled:
        raise AuthUnavailable("账号体系未开启（AUTH_ENABLED=false）")


def hash_password(password: str, iterations: int) -> tuple[str, str]:
    """返回（哈希十六进制, 盐十六进制）。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return digest.hex(), salt.hex()


def verify_password(password: str, salt_hex: str, iterations: int, expected_hash: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest.hex(), expected_hash)


def validate_credentials(username: str, password: str) -> None:
    """校验用户名与口令格式，不合规直接抛 ``AuthError``（中文提示）。"""
    if not _USERNAME_RE.match(username or ""):
        raise AuthError("用户名需为 3~32 位的中英文、数字或下划线")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError(f"口令至少 {MIN_PASSWORD_LEN} 位")
    if len(password) > MAX_PASSWORD_LEN:
        raise AuthError(f"口令不能超过 {MAX_PASSWORD_LEN} 位")


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(settings: Settings, user_id: str, user_agent: str | None) -> str:
    """签发令牌并落库（只存哈希），返回明文令牌。"""
    raw = secrets.token_urlsafe(32)
    expires_at = (timeutil.now_utc() + timedelta(days=settings.auth_token_ttl_days)).isoformat()
    history.create_session(settings, _hash_token(raw), user_id, expires_at, user_agent)
    return raw


def resolve_token(settings: Settings, raw: str | None) -> dict[str, Any] | None:
    """按令牌取用户；令牌为空/无效/过期都返回 None。"""
    if not raw:
        return None
    return history.get_session_user(settings, _hash_token(raw))


def revoke_token(settings: Settings, raw: str | None) -> None:
    if raw:
        history.delete_session(settings, _hash_token(raw))


_attempts: dict[str, tuple[int, float]] = {}
_attempts_lock = threading.Lock()


def _check_lockout(settings: Settings, username: str) -> None:
    with _attempts_lock:
        count, locked_until = _attempts.get(username, (0, 0.0))
    if count >= settings.auth_max_login_attempts and locked_until > time.time():
        remaining = int(locked_until - time.time()) + 1
        raise RateLimited(f"登录失败次数过多，请 {remaining} 秒后再试")


def _record_failure(settings: Settings, username: str) -> None:
    with _attempts_lock:
        count, _ = _attempts.get(username, (0, 0.0))
        count += 1
        locked_until = time.time() + settings.auth_lockout_seconds if count >= settings.auth_max_login_attempts else 0.0
        _attempts[username] = (count, locked_until)


def _clear_failures(username: str) -> None:
    with _attempts_lock:
        _attempts.pop(username, None)


def register(
    settings: Settings,
    username: str,
    password: str,
    user_agent: str | None = None,
) -> dict[str, Any]:
    _require_enabled(settings)
    username = (username or "").strip()
    validate_credentials(username, password)
    password_hash, salt = hash_password(password, settings.auth_pbkdf2_iterations)
    user = history.create_user(settings, username, password_hash, salt, settings.auth_pbkdf2_iterations)
    token = issue_token(settings, user["id"], user_agent)
    return {"token": token, "user": {"id": user["id"], "username": user["username"]}}


def login(
    settings: Settings,
    username: str,
    password: str,
    user_agent: str | None = None,
) -> dict[str, Any]:
    _require_enabled(settings)
    username = (username or "").strip()
    _check_lockout(settings, username)

    user = history.get_user_by_username(settings, username)
    if user is None:
        hash_password(password or "", settings.auth_pbkdf2_iterations)
        _record_failure(settings, username)
        raise AuthError("用户名或口令错误")

    if not verify_password(password or "", user["salt"], int(user["iterations"]), user["password_hash"]):
        _record_failure(settings, username)
        raise AuthError("用户名或口令错误")

    _clear_failures(username)
    history.touch_login(settings, user["id"])
    token = issue_token(settings, user["id"], user_agent)
    return {"token": token, "user": {"id": user["id"], "username": user["username"]}}


def logout(settings: Settings, raw: str | None) -> None:
    _require_enabled(settings)
    if not raw:
        raise Unauthorized("未登录或登录已过期，请重新登录")
    revoke_token(settings, raw)


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def optional_user(
    authorization: str | None = Header(None, description="Bearer 令牌；不传则以匿名身份问答（不落库）"),
) -> dict[str, Any] | None:
    """可选登录：令牌为空或无效都返回 None，**不抛异常** —— 保证匿名问答与改动前一致。"""
    s = get_settings()
    if not s.auth_enabled:
        return None
    try:
        return resolve_token(s, _bearer(authorization))
    except Exception:  # noqa: BLE001 - 数据库异常不应让匿名问答失败
        return None


def current_user(
    authorization: str | None = Header(None, description="Bearer 令牌"),
) -> dict[str, Any]:
    """必须登录；未登录返回 401，账号体系未开启返回 503。"""
    s = get_settings()
    if not s.auth_enabled:
        raise HTTPException(status_code=503, detail="账号体系未开启（AUTH_ENABLED=false）")
    user = resolve_token(s, _bearer(authorization))
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    return user


def require_bearer(authorization: str | None) -> str:
    """从 Header 里取出原始令牌；拿不到合法令牌抛 Unauthorized（翻译成 401）。"""
    raw = _bearer(authorization)
    if not raw:
        raise Unauthorized("未登录或登录已过期，请重新登录")
    return raw