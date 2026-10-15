# ============================================================================
# _probe_common.py —— probe_*.py 共享的小工具（不单独执行）
# ============================================================================
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

API_BASE = "http://127.0.0.1:8000"


def out(message: str) -> None:
    """统一走 UTF-8 输出，避免 Windows 控制台 GBK 乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(message, flush=True)


def ensure_service_up(timeout: float = 10.0) -> None:
    """确认后端在 API_BASE 上活着，否则直接退出（探针不负责起服务）。"""
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"health 返回 {resp.status}")
    except Exception as exc:
        out(f"[SKIP] 后端 {API_BASE} 不可用（{exc}）。请先启动服务再跑探针。")
        raise SystemExit(2) from exc


def http_status(url: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, str]:
    """发一次 HTTP 请求，返回（状态码, 响应文本）。网络层错误按 0 处理。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def ask_chat(question: str, timeout: float = 180.0) -> list[dict[str, Any]]:
    """POST /api/chat 并解析 SSE，返回事件字典列表（跳过 [DONE] 哨兵）。"""
    req = urllib.request.Request(
        f"{API_BASE}/api/chat",
        data=json.dumps({"question": question}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp.read().decode("utf-8", "replace").splitlines():
            if raw.startswith("data: ") and raw != "data: [DONE]":
                try:
                    events.append(json.loads(raw[len("data: ") :]))
                except json.JSONDecodeError:
                    continue
    return events


def finish(failures: int, total: int, name: str) -> int:
    if failures:
        out(f"[FAIL] {name}: {failures}/{total} 题未通过")
        return 1
    out(f"[PASS] {name}: 全部 {total} 题通过")
    return 0


def wait_http_ready(url: str, timeout: float) -> bool:
    """轮询直到 URL 有 HTTP 响应（任意状态码都算就绪）。

    单次请求超时给 8s：锁冲突实例的 /api/health 实测约 2.3s（Qdrant 锁检查耗时），
    给 2s 会永远超时——探针必须比最慢的就绪路径更耐心。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=8)
            return True
        except urllib.error.HTTPError:
            return True  # 有响应即就绪（如 503）
        except Exception:
            time.sleep(0.5)
    return False
