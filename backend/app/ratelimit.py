"""ratelimit：应用层限流（按「IP + 用户」的内存令牌桶，超限 429 + Retry-After）。

职责：
  - 为问答类端点提供 FastAPI 依赖式限流
  - 令牌按固定速率回填，容量 / 速率从 .env 读取（RATELIMIT_*）
  - 多进程不共享桶（已知限制，见 README）

依赖：config、auth（读登录态分桶）
被谁调用：main（问答与检索端点挂 Depends）
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

from fastapi import Depends, HTTPException, Request

from . import auth
from .config import get_settings

_CAPACITY = get_settings().ratelimit_capacity
_REFILL_PER_SEC = get_settings().ratelimit_refill_per_sec


class _Bucket:
    """一个键（IP+用户）对应的令牌桶：当前令牌数 + 上次补充的时刻。"""

    __slots__ = ("tokens", "updated")

    def __init__(self, tokens: float, updated: float) -> None:
        self.tokens = tokens
        self.updated = updated


_buckets: dict[str, _Bucket] = {}
_lock = threading.Lock()


def _client_key(request: Request, user: dict[str, Any] | None) -> str:
    """拼限流维度：「IP + 用户」，登录用户按账号分桶，匿名按出口 IP 分桶。"""
    ip = request.client.host if request.client else "unknown"
    uid = str(user.get("id")) if user else "anon"
    return f"{ip}:{uid}"


def rate_limit(
    request: Request,
    user: dict[str, Any] | None = Depends(auth.optional_user),
) -> None:
    """FastAPI 依赖：消耗一枚令牌；桶空则抛 429（带 Retry-After 响应头）。"""
    key = _client_key(request, user)
    now = time.monotonic()

    with _lock:
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(_CAPACITY), updated=now)
            _buckets[key] = bucket
        else:
            bucket.tokens = min(float(_CAPACITY), bucket.tokens + (now - bucket.updated) * _REFILL_PER_SEC)
            bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return

        wait_seconds = (1.0 - bucket.tokens) / _REFILL_PER_SEC
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(max(1, math.ceil(wait_seconds)))},
        )
