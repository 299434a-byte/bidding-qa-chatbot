# ============================================================================
# 文件：tests/test_ratelimit.py —— 应用层限流（内存令牌桶）的边界测试
#
# 【这个测试文件整体在测什么】
#   /api/chat、/api/agent/chat、/api/retrieve 上的限流依赖（app/ratelimit.py）：
#     ① 桶满（10 枚突发额度）之后继续连打 → 429，且响应头带 Retry-After；
#     ② 按 Retry-After 等过回填时间（0.5 枚/秒）→ 恢复放行；
#     ③ 「IP + 用户」维度分桶：不同 IP 各有各的额度，互不影响。
#   全部离线：用假 Request + 假时钟直接驱动依赖函数，不经过网络。
#
# 【如实说明（与源码注释一致）】这是纯内存实现，只对单进程有效；
# 多进程部署需换 Redis（本项目已知限制，此处不美化）。
# ============================================================================

from __future__ import annotations

# SimpleNamespace：造「假请求」（只用到 request.client.host）与假时钟。
from types import SimpleNamespace

# pytest.raises / HTTPException：断言 429 及其响应头。
import pytest
from fastapi import HTTPException

# 被测模块。
from app import ratelimit


def _fake_request(ip: str = "10.0.0.1") -> SimpleNamespace:
    """造一个只带 client.host 的假请求（rate_limit 只读这一个字段）。"""
    return SimpleNamespace(client=SimpleNamespace(host=ip))


class _FakeClock:
    """假时钟：时间只由测试手动推进，让「回填恢复」可精确断言。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch) -> _FakeClock:
    """把 ratelimit 模块里的 time 换成假时钟，并清空上一个用例留下的桶。"""
    clock = _FakeClock()
    monkeypatch.setattr(ratelimit, "time", clock)
    ratelimit._buckets.clear()
    yield clock
    ratelimit._buckets.clear()


# ----------------------------------------------------------------------------
# 用例 1：桶满后连打 → 429 + Retry-After
# ----------------------------------------------------------------------------
def test_burst_beyond_capacity_returns_429_with_retry_after(clock) -> None:
    """突发额度 10 枚用完后，第 11 个请求必须 429，且 Retry-After 至少 1 秒。"""
    request = _fake_request()
    # 桶容量 10：前 10 个请求（含突发额度）全部放行。
    for _ in range(ratelimit._CAPACITY):
        assert ratelimit.rate_limit(request, None) is None

    # 第 11 个：抛 429。
    with pytest.raises(HTTPException) as excinfo:
        ratelimit.rate_limit(request, None)
    assert excinfo.value.status_code == 429
    # 响应头必须告诉调用方「等几秒再来」（值为 ≥1 的整数秒）。
    retry_after = excinfo.value.headers["Retry-After"]
    assert int(retry_after) >= 1


# ----------------------------------------------------------------------------
# 用例 2：按 Retry-After 等待后恢复放行（回填 0.5 枚/秒）
# ----------------------------------------------------------------------------
def test_refill_after_wait_allows_requests_again(clock) -> None:
    """429 之后等够回填时间（2 秒回 1 枚）→ 又能通过一个请求。"""
    request = _fake_request()
    for _ in range(ratelimit._CAPACITY):
        ratelimit.rate_limit(request, None)
    with pytest.raises(HTTPException):
        ratelimit.rate_limit(request, None)

    # 回填速率 0.5 枚/秒 → 2 秒后恰好攒够 1 枚。
    clock.tick(2.0)
    assert ratelimit.rate_limit(request, None) is None
    # 刚放行一个就又空了：立刻再来还是 429（证明没有凭空多发令牌）。
    with pytest.raises(HTTPException):
        ratelimit.rate_limit(request, None)


# ----------------------------------------------------------------------------
# 用例 3：按「IP + 用户」分桶 —— 不同 IP 互不影响
# ----------------------------------------------------------------------------
def test_buckets_are_per_ip(clock) -> None:
    """A 的额度用光不影响 B：限流维度是「IP + 用户」。"""
    a = _fake_request("10.0.0.1")
    b = _fake_request("10.0.0.2")
    for _ in range(ratelimit._CAPACITY):
        ratelimit.rate_limit(a, None)
    with pytest.raises(HTTPException):
        ratelimit.rate_limit(a, None)

    # 换个 IP：额度独立，照常放行。
    assert ratelimit.rate_limit(b, None) is None


# ----------------------------------------------------------------------------
# 用例 4：分桶键是「IP + 用户」组合 —— 同一用户换 IP 走独立桶
# ----------------------------------------------------------------------------
def test_key_is_ip_plus_user_combination(clock) -> None:
    """同一个用户换 IP → 换桶、额度独立（键 f\"{ip}:{uid}\" 是组合键，不是纯账号键）。

    【如实记录】app/ratelimit.py 的源码注释写着「登录用户按账号分桶（换 IP 也共额度）」，
    但实现是 f"{ip}:{uid}"（IP 与用户 id 一起决定桶）；本轮按任务要求「不改业务行为、
    不改注释」，这里锁的是实现的现状：换 IP 不共桶。若将来要改成「按账号共桶」，
    改 _client_key 后把本用例反过来即可（届时源码注释与实现才真正一致）。
    """
    user = {"id": "u-1", "username": "张三"}
    for _ in range(ratelimit._CAPACITY):
        assert ratelimit.rate_limit(_fake_request("10.0.0.1"), user) is None

    # 桶已空：同一个用户从另一个 IP 访问 → 另一个桶，照常放行（组合键的现实行为）。
    assert ratelimit.rate_limit(_fake_request("10.0.0.2"), user) is None
    # 原 IP 的桶仍然是空的：回到 10.0.0.1 立刻 429。
    with pytest.raises(HTTPException):
        ratelimit.rate_limit(_fake_request("10.0.0.1"), user)

    # 同 IP 不同用户也是独立桶（组合键里含 uid）。
    other = {"id": "u-2", "username": "李四"}
    assert ratelimit.rate_limit(_fake_request("10.0.0.1"), other) is None


# ----------------------------------------------------------------------------
# 用例 5：接口层集成 —— 连打 /api/chat 触发 429，响应头带 Retry-After
# ----------------------------------------------------------------------------
def test_api_chat_returns_429_when_bucket_exhausted(clock, monkeypatch) -> None:
    """走 FastAPI 接口（TestClient）：令牌桶空了 /api/chat 直接 429，不进问答主流程。"""
    # 这一层只验证「限流依赖在接口上生效」，问答链路全部打掉（不检索、不调模型）。
    from fastapi.testclient import TestClient

    from app.main import app
    from app.retriever import RetrievalResult

    monkeypatch.setattr("app.rag.retrieve", lambda q, settings=None, final_k=None: RetrievalResult(question=q))
    monkeypatch.setattr("app.rag.stream_chat", lambda *args, **kwargs: iter(()))

    client = TestClient(app)
    payload = {"question": "限流接口测试"}
    # 先把桶打空（TestClient 的 client.host 是固定的 testclient）。
    for _ in range(ratelimit._CAPACITY):
        assert ratelimit.rate_limit(_fake_request("testclient"), None) is None

    response = client.post("/api/chat", json=payload)
    assert response.status_code == 429
    # FastAPI 会把 HTTPException 的 headers 带出来。
    assert int(response.headers["Retry-After"]) >= 1
