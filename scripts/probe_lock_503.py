# ============================================================================
# probe_lock_503.py —— 端到端冒烟：向量库被另一进程占用时，接口返回 503
#
# 【原理】Qdrant 本地模式一个目录只允许一个进程打开。主服务（:8000）已经持有
#         data/qdrant 的锁；本探针在另一个端口（:8123）起一个临时后端实例，
#         它打开向量库必然锁冲突 → /api/kb/stats 必须以 503 + 可读中文 detail 收场。
# 【前提】主服务已在 127.0.0.1:8000 运行并持有向量库（否则临时实例会成功打开，
#         探针报告 SKIP——这不是失败，只是没测到锁冲突路径）。
# 【用法】python scripts/probe_lock_503.py   （在项目根执行；用 backend/.venv 的 Python）
# ============================================================================
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _probe_common import http_status, out, wait_http_ready  # noqa: E402

TEMP_PORT = 8123
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"


def main() -> int:
    # 1) 主服务在跑吗？不在跑就没东西「占着锁」，直接 SKIP。
    code, _ = http_status("http://127.0.0.1:8000/api/health", timeout=5)
    if code != 200:
        out("[SKIP] 主服务 :8000 不可用。请先启动主服务（它持有向量库锁）再跑本探针。")
        return 2

    # 2) 起临时实例（同一 QDRANT_PATH → 必然锁冲突）。日志落临时文件，失败时可排查。
    log = tempfile.TemporaryFile(mode="w+b")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(TEMP_PORT)],
        cwd=str(BACKEND_DIR),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        # 3) 等它就绪：health 不碰向量库，能返回任意 HTTP 响应即可。
        ready = wait_http_ready(f"http://127.0.0.1:{TEMP_PORT}/api/health", timeout=40)
        if not ready:
            log.seek(0)
            tail = log.read().decode("utf-8", "replace")[-600:]
            out(f"[FAIL] 临时实例 40s 内未就绪。启动日志尾部：\n{tail}")
            return 1

        # 4) 打开向量库的接口必须 503，且 detail 是可读中文（不许裸栈）。
        code, text = http_status(f"http://127.0.0.1:{TEMP_PORT}/api/kb/stats", timeout=30)
        if code != 503:
            out(f"[FAIL] 期望 503，实际 {code}。响应：{text[:200]}")
            return 1
        try:
            detail = json.loads(text).get("detail") or ""
        except json.JSONDecodeError:
            detail = text
        if "向量库" not in detail and "占用" not in detail:
            out(f"[FAIL] 503 的 detail 不是可读的锁冲突说明：{detail[:200]}")
            return 1
        out(f"[PASS] 向量库被锁时 /api/kb/stats 返回 503，detail={detail[:80]}…")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
