# ============================================================================
# 文件：ratelimit.py —— 应用层限流（内存令牌桶）
#
# 【这个文件是干什么的】
#   给问答类接口（/api/chat、/api/agent/chat、/api/retrieve）套一个「令牌桶」：
#   每个「IP + 用户」维度分一个桶，桶里最多攒 _CAPACITY 枚令牌，每来一个请求消耗 1 枚，
#   令牌按 _REFILL_PER_SEC 的速度自动补充。桶空了还继续打 → 返回 429，
#   并在响应头 Retry-After 里告诉调用方「几秒后再来」。
#
# 【为什么要有这层】应用跑在公网上时，脚本刷问答接口会把大模型额度和磁盘
#   （缓存 / 指标 / 日志）一起打爆；限流是最便宜的第一道防线。
#
# 【已知限制，如实记录，不要美化】
#   这是**纯内存**实现：令牌桶只存在于当前这个 Python 进程里。
#   · 单进程部署（uvicorn 不加 --workers）：完全有效；
#   · 多进程 / 多实例部署（--workers N 或多台机器）：每个进程各有一套桶，
#     实际放行量会按进程数翻倍，限不住总量。真要多进程限流，必须把桶的状态
#     换成共享存储（一般是 Redis + INCR/EXPIRE 或 Lua 脚本），本项目尚未实现。
# ============================================================================
"""应用层限流：按「IP + 用户」维度的内存令牌桶，超限返回 429 + Retry-After。"""

from __future__ import annotations

# math：算 Retry-After 要向上取整秒数。
import math
# threading.Lock：令牌桶会被多个请求并发读写，取令牌必须加锁。
import threading
# time.monotonic：单调时钟（不受系统时间调整影响），用于「上次更新到现在过了多久」。
import time
# Any：给「用户字典」这类松散类型做注解。
from typing import Any

# HTTPException：超限时直接抛 429，FastAPI 会自动把它转成响应（带响应头）。
from fastapi import Depends, HTTPException, Request

# auth.optional_user：复用既有的「读登录态」依赖——登录了算「用户」维度，没登录算匿名。
from . import auth

# 桶容量：单个键瞬时允许的突发请求次数（攒满后的第一波连打上限）。
_CAPACITY = 10
# 每秒补充的令牌数：0.5 = 每 2 秒回 1 枚，折合每分钟约 30 次的稳态速率。
# 正常用户一轮问答才 1~2 个请求，离这个线很远；只有脚本式连打才会被拦下。
_REFILL_PER_SEC = 0.5


class _Bucket:
    """一个键（IP+用户）对应的令牌桶：当前令牌数 + 上次补充的时刻。"""

    # __slots__：桶的数量会随访问者增长，用 slots 把每个实例的内存压到最小。
    __slots__ = ("tokens", "updated")

    def __init__(self, tokens: float, updated: float) -> None:
        self.tokens = tokens
        self.updated = updated


# 所有键的桶。结构 {key: _Bucket}；由 _lock 保护（读改写都要拿锁）。
_buckets: dict[str, _Bucket] = {}
_lock = threading.Lock()


def _client_key(request: Request, user: dict[str, Any] | None) -> str:
    """拼限流维度：「IP + 用户」。登录用户按账号分桶（换 IP 也共额度），
    匿名按 IP 分桶（同一出口 IP 共享额度）。"""
    ip = request.client.host if request.client else "unknown"
    uid = str(user.get("id")) if user else "anon"
    return f"{ip}:{uid}"


def rate_limit(
    request: Request,
    user: dict[str, Any] | None = Depends(auth.optional_user),
) -> None:
    """FastAPI 依赖：消耗一枚令牌；桶空则抛 429（带 Retry-After 响应头）。

    【用法】在端点签名里挂 ``Depends(ratelimit.rate_limit)`` 即可，不影响原有参数。
    【为什么依赖里再 Depends(optional_user)】FastAPI 对同一请求内的同一依赖
    只解析一次（带缓存），所以这里拿到的 user 和端点参数里的 user 是同一个对象，不重复查令牌。
    """
    key = _client_key(request, user)
    # monotonic：只看时间差，不怕系统时钟被 NTP 校正导致桶「凭空变满/变空」。
    now = time.monotonic()

    with _lock:
        bucket = _buckets.get(key)
        if bucket is None:
            # 第一次访问：直接给一整桶（允许突发），之后靠补充慢慢回血。
            bucket = _Bucket(tokens=float(_CAPACITY), updated=now)
            _buckets[key] = bucket
        else:
            # 按流逝的时间补令牌（最多补到桶上限 _CAPACITY）。
            bucket.tokens = min(float(_CAPACITY), bucket.tokens + (now - bucket.updated) * _REFILL_PER_SEC)
            bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return

        # 桶空：还要等多久才能攒够 1 枚令牌，换算成整秒写进 Retry-After（至少 1 秒）。
        wait_seconds = (1.0 - bucket.tokens) / _REFILL_PER_SEC
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(max(1, math.ceil(wait_seconds)))},
        )
