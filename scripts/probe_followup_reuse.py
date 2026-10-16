# ============================================================================
# probe_followup_reuse.py —— 追问复用实测对比：复用前（完整链路） vs 复用后（跳过检索）
#
# 【前提】后端已在 127.0.0.1:8000 运行，知识库已建好（要能正常召回来源）。
# 【用法】python scripts/probe_followup_reuse.py   （项目根执行，用 backend/.venv 的 Python）
# 【方法】同一追问问题 Q_FOLLOWUP 各跑一遍：
#         ① 复用前：新建会话直接问追问问题 —— 没有上一问证据，按设计退回完整链路；
#         ② 复用后：另一会话先问原始问题建立证据，再问同一追问问题 —— 命中复用，
#            跳过检索/规划，直接拿旧证据流式作答。
#         两轮之间只删「追问问题」的缓存行，保证第 ② 轮不是吃缓存而是真走复用分支。
#         统计：首个正文 delta 的首 token 耗时、流结束总耗时、reused 标记、
#         /api/metrics 的 followup_reused 计数增量。
# 【注意】必须登录（复用要求会话落库），请求体显式 web_search=false（默认开联网会绕开复用）。
# ============================================================================
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 共享 _probe_common
from _probe_common import API_BASE, ensure_service_up, out  # noqa: E402

# 让脚本能直接读 app.config 的 cache_db_path（只 import 配置，不连库）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.config import get_settings  # noqa: E402

# 原始问题：语料里的标准探针题，必有来源（复用的证据就靠它建立）
Q_ORIGINAL = "中标候选人公示期是多久？"
# 追问问题：≤15 字 + 指代词「那…呢」+ 无新实体 → is_followup 三条件全中
Q_FOLLOWUP = "那第二条呢"

TIMEOUT = 180.0


# ------------------------------ 小工具 ------------------------------ #
def _req(path: str, payload: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    """普通 JSON 请求（POST 当有 payload 时），返回解析后的字典。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{path} → {exc.code}: {body}") from exc


def purge_question(question: str) -> int:
    """按问题删缓存行（不碰其他题的缓存与证据）。返回删除行数。"""
    s = get_settings()
    conn = sqlite3.connect(str(s.cache_db_path), timeout=10.0)
    try:
        cur = conn.execute("DELETE FROM answer_cache WHERE question = ?", (question,))
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    finally:
        conn.close()


def ask_stream(question: str, conversation_id: str, token: str) -> dict[str, Any]:
    """POST /api/agent/chat 并边读 SSE 边计时。

    三个时刻分别记（都相对请求发出，毫秒）：
      open_ms       第一个正文 delta —— 生成链路开局会先发 delta("") 空标记，这只是「流打开」；
      first_token_ms 第一个**非空**正文 delta（出字；channel 缺省或 answer，排除 revision）；
      first_think_ms 第一个 thinking 事件（推理模型思维链开始出字，UI 也展示）。
    首个用户可见输出 = min(first_think_ms, first_token_ms)。
    总耗时 = 读到 done / [DONE] 为止。
    """
    body = json.dumps(
        {
            "question": question,
            "top_k": None,
            "history": [],
            "web_search": False,  # 复用要求非联网；.env 默认开联网，必须显式关掉
            "mode": "react",
            "conversation_id": conversation_id,
            "model": None,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/api/agent/chat",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    t0 = time.perf_counter()
    open_ms: float | None = None
    first_token_ms: float | None = None
    first_think_ms: float | None = None
    end_ms: float | None = None
    events: list[dict[str, Any]] = []
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        for raw in resp:  # 逐行流式读取，保证首 token 计时有意义
            line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                end_ms = (time.perf_counter() - t0) * 1000
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            events.append(event)
            etype = event.get("type")
            if etype == "delta" and (event.get("channel") or "answer") != "revision":
                now_ms = (time.perf_counter() - t0) * 1000
                if open_ms is None:
                    open_ms = now_ms  # 流打开（可能就是那条 delta("") 空标记）
                if first_token_ms is None and (event.get("text") or ""):
                    first_token_ms = now_ms  # 第一个真正出字的 delta 才算首 token
            if etype == "thinking" and first_think_ms is None:
                first_think_ms = (time.perf_counter() - t0) * 1000
            if etype in ("done", "error") and end_ms is None:
                end_ms = (time.perf_counter() - t0) * 1000
    if end_ms is None:
        end_ms = (time.perf_counter() - t0) * 1000

    sources = next((e for e in events if e.get("type") == "sources"), None)
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    retrieval = next((e for e in events if e.get("type") == "retrieval"), None)
    answer_len = sum(
        len(e.get("text") or "")
        for e in events
        if e.get("type") == "delta" and (e.get("channel") or "answer") != "revision"
    )
    return {
        "question": question,
        "open_ms": open_ms,
        "first_token_ms": first_token_ms,
        "first_think_ms": first_think_ms,
        "visible_ms": min(
            [v for v in (first_token_ms, first_think_ms) if v is not None],
            default=None,
        ),
        "total_ms": end_ms,
        "answer_len": answer_len,
        "types": [e.get("type") for e in events],
        "sources_count": len((sources or {}).get("sources") or []),
        "reused": bool((retrieval or {}).get("meta", {}).get("reused"))
        or bool((done or {}).get("reused")),
        "cache_hit": any(e.get("type") == "cache_hit" for e in events),
        "done_mode": (done or {}).get("mode"),
        "error": next((e.get("message") for e in events if e.get("type") == "error"), None),
    }


def metrics_followup_reused(token: str) -> int:
    """读 /api/metrics 里的 followup_reused 计数（拿不到返回 -1）。"""
    try:
        data = _req("/api/metrics", token=token)
        return int(data.get("followup_reused", -1))
    except Exception:
        return -1


def fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f} ms"


# ------------------------------ 主流程 ------------------------------ #
def main() -> int:
    ensure_service_up()

    # 1) 注册一次性账号（复用要求登录 + 会话落库）
    username = f"bench_{int(time.time())}"
    auth = _req("/api/auth/register", {"username": username, "password": "BenchPass123"})
    token = auth["token"]
    out(f"[SETUP] 注册临时账号 {username}")

    # 2) 清掉这两道题的历史缓存行，保证从「未命中」起跑（不动其他题的缓存）
    purged = purge_question(Q_ORIGINAL) + purge_question(Q_FOLLOWUP)
    out(f"[SETUP] 预清理缓存 {purged} 行（只删本轮两道题）")

    # 3) 复用前：新会话直接问追问问题 —— 无上一问证据 → 按设计退回完整链路
    conv_b = _req("/api/history/conversations", {"title": "复用前基线", "mode": "react"}, token)["conversation"]
    before_counter = metrics_followup_reused(token)
    out(f"[RUN ] 复用前（完整链路）: {Q_FOLLOWUP} @ {conv_b['id']}")
    baseline = ask_stream(Q_FOLLOWUP, conv_b["id"], token)

    # 4) 删掉基线写下的追问缓存行，让复用轮真走复用分支而不是吃缓存
    purge_question(Q_FOLLOWUP)

    # 5) 复用后：另一会话先问原始问题（建立证据），再问同一追问问题
    conv_a = _req("/api/history/conversations", {"title": "复用后", "mode": "react"}, token)["conversation"]
    original: dict[str, Any] = {}
    for attempt in range(1, 4):  # 检索偶发空召回时重试；先删该题缓存行，避免吃「0 来源」的缓存答案
        out(f"[RUN ] 建立证据（第 {attempt} 次）: {Q_ORIGINAL} @ {conv_a['id']}")
        purge_question(Q_ORIGINAL)
        original = ask_stream(Q_ORIGINAL, conv_a["id"], token)
        if original["sources_count"] > 0:
            break
        out(f"[WARN] 本轮召回 {original['sources_count']} 条来源，重试")
    if original["sources_count"] == 0:
        out("[FAIL] 原始问题连续 3 次没有召回来源，无法建立复用证据（知识库为空？）")
        return 1

    out(f"[RUN ] 复用后（命中复用）: {Q_FOLLOWUP} @ {conv_a['id']}")
    reused = ask_stream(Q_FOLLOWUP, conv_a["id"], token)
    after_counter = metrics_followup_reused(token)

    # 6) 断言与对比表
    failures = 0

    def expect(cond: bool, msg: str) -> None:
        nonlocal failures
        if cond:
            out(f"[PASS] {msg}")
        else:
            out(f"[FAIL] {msg}")
            failures += 1

    expect(baseline["visible_ms"] is not None, "复用前有可见输出（完整链路走通）")
    expect(baseline["done_mode"] not in (None, "cache"), f"复用前走完整链路（done.mode={baseline['done_mode']}）")
    expect(not baseline["reused"] and not baseline["cache_hit"], "复用前未命中缓存 / 未标 reused")
    expect(reused["reused"], "复用后事件带 reused=true（retrieval.meta 或 done）")
    expect(not reused["cache_hit"], "复用后不是缓存命中（cache_hit 事件不存在）")
    expect(reused["sources_count"] > 0, f"复用后带来源（{reused['sources_count']} 条，复用上一问证据）")
    if before_counter >= 0 and after_counter >= 0:
        expect(after_counter == before_counter + 1, f"metrics followup_reused +1（{before_counter} → {after_counter}）")
    if reused["error"] or baseline["error"]:
        out(f"[WARN] 事件流含 error：baseline={baseline['error']} reused={reused['error']}")

    out("")
    out("=" * 78)
    out(f"{'口径':<20}{'复用前（完整链路）':>26}{'复用后（命中复用）':>28}")
    out("-" * 78)
    out(f"{'流打开(首个delta)':<18}{fmt_ms(baseline['open_ms']):>28}{fmt_ms(reused['open_ms']):>28}")
    out(f"{'思维链开始':<20}{fmt_ms(baseline['first_think_ms']):>28}{fmt_ms(reused['first_think_ms']):>28}")
    out(f"{'正文出字(非空delta)':<18}{fmt_ms(baseline['first_token_ms']):>28}{fmt_ms(reused['first_token_ms']):>28}")
    out(f"{'首个可见输出':<20}{fmt_ms(baseline['visible_ms']):>28}{fmt_ms(reused['visible_ms']):>28}")
    out(f"{'总耗时':<20}{fmt_ms(baseline['total_ms']):>28}{fmt_ms(reused['total_ms']):>28}")
    out(f"{'reused':<20}{str(baseline['reused']):>28}{str(reused['reused']):>28}")
    out(f"{'来源数':<20}{baseline['sources_count']:>28}{reused['sources_count']:>28}")
    out(f"{'正文长度':<20}{str(baseline['answer_len']) + ' 字':>26}{str(reused['answer_len']) + ' 字':>28}")
    out(f"{'done.mode':<20}{str(baseline['done_mode']):>28}{str(reused['done_mode']):>28}")
    out("-" * 78)
    # 产品口径：/api/metrics 的 first_token_ms 数的是第一个 delta（含开局空标记）
    if baseline["open_ms"] and reused["open_ms"]:
        gain = (1 - reused["open_ms"] / baseline["open_ms"]) * 100
        out(f"产品首token口径（首个delta）提速 {gain:.0f}%：{fmt_ms(baseline['open_ms'])} → {fmt_ms(reused['open_ms'])}")
    if baseline["visible_ms"] and reused["visible_ms"]:
        gain = (1 - reused["visible_ms"] / baseline["visible_ms"]) * 100
        out(f"首个可见输出（min(思维链,正文)）变化 {gain:+.0f}%："
            f"{fmt_ms(baseline['visible_ms'])} → {fmt_ms(reused['visible_ms'])}")
    if baseline["total_ms"] and reused["total_ms"]:
        gain = (1 - reused["total_ms"] / baseline["total_ms"]) * 100
        out(f"总耗时变化 {gain:+.0f}%：{fmt_ms(baseline['total_ms'])} → {fmt_ms(reused['total_ms'])}")
    out("注：复用前是对着无上下文的追问词走完整链路（经常召回 0 条，只能短答）；复用后拿上一问的真")
    out("    来源直接生成。链路跳过体现在「流打开」大幅提前（跳过检索/规划/Agent 调度）；其后的")
    out("    思维链与出字时长由 LLM 当次负载决定，两口径的总耗时受此波动影响。")
    out("=" * 78)
    out(f"参考：建立证据那一问（{Q_ORIGINAL}，完整链路）首 token {fmt_ms(original['first_token_ms'])}"
        f" / 总耗时 {fmt_ms(original['total_ms'])}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
