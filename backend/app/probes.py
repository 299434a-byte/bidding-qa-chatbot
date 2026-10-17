
"""probes：/api/health 的真探活（逐组件极小请求 + 计时 + 5 秒 TTL 缓存）。

【为什么要有它】旧 /api/health 只回「配置填没填」：Neo4j Aura 密码改错了它照样
  graph_configured=true、status=ok——配置在 ≠ 服务活着。这里对每个组件发一个
  只读的极小请求，把「活没活 + 多快」如实报回去。

【铁律】
  - 每个探活单独 try/except + 独立 2 秒硬超时：一项挂了不影响其它项，探活永不抛异常；
  - 探活只读绝不写库：SELECT 1 / RETURN 1 / collection_exists+count / max_tokens=1；
  - 任一项失败总状态 degraded，但 HTTP 仍 200（探活不能因依赖挂掉而自己先 500）；
  - 未配置的组件返回 {"ok": null, "skipped": true}，不算 degraded；
  - 结果缓存 5 秒：前端轮询不会把下游打成压测。

被谁调用：main（/api/health）、tests（test_health_probe.py）
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable

from .config import Settings

# 单个探活的硬超时（秒）：到点立刻判失败，不等慢下游。
PROBE_TIMEOUT_SECONDS = 2.0

# 探活结果的缓存时长（秒）：窗口内的重复请求直接回缓存。
PROBE_TTL_SECONDS = 5.0

# 探活专用线程池：四项并行发起、分别计时；挂住的线程由各自客户端超时兜底收走。
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="health-probe")

_cache_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_at = 0.0


def reset_cache() -> None:
    """清空探活结果缓存（测试用；生产无需调用，到期自然失效）。"""
    global _cache, _cache_at
    with _cache_lock:
        _cache = None
        _cache_at = 0.0


def _skipped(configured: bool | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"ok": None, "latency_ms": 0, "error": None, "skipped": True}
    if configured is not None:
        entry["configured"] = configured
    return entry


# ----------------------------------------------------------------------------
# 各组件探活（全部只读、极小请求；函数体内延迟导入，避免拖慢应用启动）
# ----------------------------------------------------------------------------


def _probe_pg(s: Settings) -> None:
    """PostgreSQL：只读连接执行 SELECT 1（连接串/密码错都会抛）。"""
    from .pg import probe as pg_probe

    pg_probe(s)


def _probe_qdrant(s: Settings) -> None:
    """向量库：collection_exists + count（复用 stats；服务挂了会抛，交由收集器收编）。"""
    from .vector_store import get_store

    get_store(s).stats()


def _probe_neo4j(s: Settings) -> None:
    """Neo4j：走图谱工具同款 driver 与只读会话执行 RETURN 1（密码错 / 实例停了都会抛）。"""
    from .graph import run_query

    rows = run_query(s, "RETURN 1 AS ok")
    if not rows or rows[0].get("ok") != 1:
        raise RuntimeError("Neo4j 没有返回 1")


def _probe_llm(s: Settings) -> None:
    """大模型：max_tokens=1 的极小请求，证明网关 / 鉴权 / 模型真实可用。"""
    from .llm import probe as llm_probe

    llm_probe(s, timeout=max(1, int(PROBE_TIMEOUT_SECONDS)))


# ----------------------------------------------------------------------------
# 收集与总装
# ----------------------------------------------------------------------------


def _submit(fn: Callable[[], None]) -> tuple[Future, dict[str, Any], float]:
    """提交探活任务。worker 在**自己完成的时刻**把延迟写进 holder——
    不在收集时统一计量，否则排在慢项后面的探活会把等待时间错算进自己的延迟。"""
    submitted_at = time.perf_counter()
    holder: dict[str, Any] = {"latency_ms": None}

    def run() -> None:
        started = time.perf_counter()
        try:
            fn()
        finally:
            holder["latency_ms"] = int((time.perf_counter() - started) * 1000)

    return _POOL.submit(run), holder, submitted_at


def _collect(future: Future, holder: dict[str, Any], submitted_at: float) -> dict[str, Any]:
    """等一个探活 future：到硬超时判失败；任何异常收成 ok=False，绝不上抛。"""
    try:
        future.result(timeout=PROBE_TIMEOUT_SECONDS)
        ok, error = True, None
    except FuturesTimeoutError:
        future.cancel()
        ok, error = False, f"探活超时（>{int(PROBE_TIMEOUT_SECONDS * 1000)}ms）"
    except Exception as exc:  # noqa: BLE001 - 探活必须吞掉一切异常
        ok, error = False, f"{type(exc).__name__}: {exc}"
    latency_ms = holder.get("latency_ms")
    if latency_ms is None:  # 超时/线程还没跑完：按已等待时间计
        latency_ms = int((time.perf_counter() - submitted_at) * 1000)
    return {"ok": ok, "latency_ms": latency_ms, "error": error, "skipped": False}


def run_probes(s: Settings) -> dict[str, Any]:
    """逐组件探活（并行发起、各自硬超时）。永不抛异常。"""
    checks: dict[str, Any] = {}
    jobs: list[tuple[str, Callable[[], None]]] = []

    for name, configured, probe in (
        ("pg", s.pg_configured, lambda: _probe_pg(s)),
        ("qdrant", True, lambda: _probe_qdrant(s)),
        ("neo4j", s.graph_configured, lambda: _probe_neo4j(s)),
        ("llm", s.llm_configured, lambda: _probe_llm(s)),
    ):
        if configured:
            jobs.append((name, probe))
        else:
            checks[name] = _skipped()

    pending: list[tuple[str, Future, dict[str, Any], float]] = []
    for name, probe in jobs:
        future, holder, submitted_at = _submit(probe)
        pending.append((name, future, holder, submitted_at))
    for name, future, holder, submitted_at in pending:
        checks[name] = _collect(future, holder, submitted_at)

    # 可选外部服务（付费 API）：只报配置态、恒不探活——未配置 / 不探活都不算降级
    checks["vision"] = _skipped(configured=s.vision_configured)
    checks["web_search"] = _skipped(configured=s.web_search_configured)

    status = "degraded" if any(c.get("ok") is False for c in checks.values()) else "ok"
    return {"status": status, "checked_at": time.time(), "checks": checks}


def probe_health(s: Settings, ttl: float = PROBE_TTL_SECONDS) -> dict[str, Any]:
    """探活入口（带 TTL 缓存）。窗口内的轮询直接回缓存，不打下游。"""
    global _cache, _cache_at
    with _cache_lock:
        if _cache is not None and time.time() - _cache_at < ttl:
            return _cache
    result = run_probes(s)
    with _cache_lock:
        _cache = result
        _cache_at = time.time()
    return result
