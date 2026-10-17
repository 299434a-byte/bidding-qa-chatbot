# ============================================================================
# 文件：tests/test_health_probe.py —— /api/health 真探活（失败降级 / 跳过 / 缓存）
#
# 【这个测试文件整体在测什么】
#   /api/health 从「读配置」升级为「真探活」后的全部承诺：
#     ① 各组件都活着 → status=ok，每项 {ok: true, latency_ms: int, error: null}；
#     ② 某项抛异常 → status=degraded、该项 ok=false 且 error 带得出原因，
#       **接口仍然 200**（探活不能因依赖挂掉而自己先 500），其它项不受影响；
#     ③ 慢下游到硬超时 → 同样判失败，不无限等；
#     ④ 未配置的组件（测试环境 PG / Neo4j 清空；vision/web_search 无密钥）
#       → {"ok": null, "skipped": true}，且**不算 degraded**；
#     ⑤ 5 秒 TTL 缓存：窗口内轮询不重探，清缓存后重探。
#   全部离线：会打网络的探活（llm 指向保留端口）全部换成桩；
#   qdrant 走沙箱嵌入式（纯本地进程，conftest 已把路径指到临时目录）。
# ============================================================================

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app import probes
from app.main import app

client = TestClient(app)


def _patch_llm_ok(monkeypatch) -> None:
    """测试环境里 llm 配置为真（指向 127.0.0.1:9 保留端口，探活必失败）——换成桩让它过。"""
    monkeypatch.setattr(probes, "_probe_llm", lambda s: None)


# ----------------------------------------------------------------------------
# 用例 1：全正常 → ok，每项 {ok:true, latency_ms:int, error:null}
# ----------------------------------------------------------------------------
def test_all_alive_returns_ok(monkeypatch) -> None:
    probes.reset_cache()
    _patch_llm_ok(monkeypatch)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    llm = body["checks"]["llm"]
    assert llm["ok"] is True
    assert llm["error"] is None
    assert llm["skipped"] is False
    assert isinstance(llm["latency_ms"], int) and llm["latency_ms"] >= 0
    # qdrant 走沙箱嵌入式真探活（本地进程，离线），同样全绿
    qdrant = body["checks"]["qdrant"]
    assert qdrant["ok"] is True and qdrant["error"] is None
    assert isinstance(body.get("checked_at"), (int, float))


# ----------------------------------------------------------------------------
# 用例 2：某项抛异常 → degraded 且该项 ok=false；接口仍 200；其它项不受影响
# ----------------------------------------------------------------------------
def test_failing_probe_degrades_but_stays_200(monkeypatch) -> None:
    probes.reset_cache()

    def boom(s) -> None:
        raise RuntimeError("密码错了")

    monkeypatch.setattr(probes, "_probe_llm", boom)
    resp = client.get("/api/health")
    assert resp.status_code == 200  # 探活失败不 500
    body = resp.json()
    assert body["status"] == "degraded"
    llm = body["checks"]["llm"]
    assert llm["ok"] is False
    assert "密码错了" in (llm["error"] or "")
    # 一项挂了不拖累其它项：qdrant 仍 ok
    assert body["checks"]["qdrant"]["ok"] is True


# ----------------------------------------------------------------------------
# 用例 3：慢下游到硬超时 → 判失败，错误信息写明超时
# ----------------------------------------------------------------------------
def test_slow_probe_times_out_as_failure(monkeypatch) -> None:
    probes.reset_cache()
    monkeypatch.setattr(probes, "PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(probes, "_probe_llm", lambda s: time.sleep(0.3))
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    llm = body["checks"]["llm"]
    assert llm["ok"] is False
    assert "超时" in (llm["error"] or "")


# ----------------------------------------------------------------------------
# 用例 4：未配置的组件 → skipped（ok=null），不算 degraded
# ----------------------------------------------------------------------------
def test_unconfigured_components_skip_without_degrading(monkeypatch) -> None:
    probes.reset_cache()
    _patch_llm_ok(monkeypatch)
    body = client.get("/api/health").json()
    checks = body["checks"]
    # 测试环境 PG / Neo4j 清空 → 未配置 → 跳过
    for key in ("pg", "neo4j"):
        assert checks[key]["ok"] is None
        assert checks[key]["skipped"] is True
    # 可选外部服务恒跳过（不发付费外部请求），configured 如实反映密钥有无
    for key in ("vision", "web_search"):
        assert checks[key]["ok"] is None
        assert checks[key]["skipped"] is True
        assert checks[key]["configured"] is False
    # 跳过项不拖累总状态
    assert body["status"] == "ok"


# ----------------------------------------------------------------------------
# 用例 5：5 秒 TTL 缓存 → 窗口内轮询不重探，清缓存后重探
# ----------------------------------------------------------------------------
def test_ttl_cache_avoids_repolling(monkeypatch) -> None:
    probes.reset_cache()
    calls: list[int] = []
    monkeypatch.setattr(probes, "_probe_llm", lambda s: calls.append(1))
    client.get("/api/health")
    client.get("/api/health")
    assert len(calls) == 1  # 5 秒窗口内第二次直接回缓存
    probes.reset_cache()
    client.get("/api/health")
    assert len(calls) == 2  # 清缓存后重探
