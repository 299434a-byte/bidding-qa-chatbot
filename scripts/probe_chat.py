# ============================================================================
# probe_chat.py —— 端到端冒烟：问 3 题，断言每题答案有引用
#
# 【前提】后端已在 127.0.0.1:8000 运行（qaweb.bat / uvicorn），知识库已建好。
# 【用法】python scripts/probe_chat.py   （在项目根执行；用 backend/.venv 的 Python）
# 【判据】每题的 SSE 流满足：done 收尾、sources 非空、正文（非修订 delta 拼接）
#         含 [参考资料N]。正文为空（如推理模型思维链耗尽 LLM_MAX_TOKENS）重试一次，
#         仍失败则退出码 1。
# 【注意】走 /api/chat（纯 RAG 通道），匿名请求不吃缓存也不写缓存。
# ============================================================================
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 共享 _probe_common
from _probe_common import API_BASE, ask_chat, ensure_service_up, finish, out  # noqa: E402

QUESTIONS = [
    "市政道路的招标公告有哪些？",
    "投标人需要具备什么资质要求？",
    "中标候选人公示期是多久？",
]

MAX_ATTEMPTS = 2  # 正文为空（如思维链耗尽 token 预算）时每题最多再试一次


def check_once(question: str) -> tuple[bool, str]:
    """问一题，返回（是否通过, 说明）。"""
    t0 = time.perf_counter()
    events = ask_chat(question)
    elapsed = time.perf_counter() - t0
    types = [e.get("type") for e in events]
    if not events or types[-1] != "done":
        return False, f"{elapsed:.1f}s 事件流未以 done 收尾（types={types[-5:]}）"
    sources = next((e for e in events if e.get("type") == "sources"), None)
    if not sources or not sources.get("sources"):
        return False, f"{elapsed:.1f}s 没有 sources（检索没命中，知识库可能为空）"
    body = "".join(
        e.get("text") or "" for e in events
        if e.get("type") == "delta" and not e.get("channel")
    )
    if not body.strip():
        return False, (
            f"{elapsed:.1f}s 正文为空（sources={len(sources['sources'])} 条；"
            f"若反复出现，检查 LLM_MAX_TOKENS 是否被推理模型思维链耗尽）"
        )
    if "[参考资料" not in body:
        return False, f"{elapsed:.1f}s 正文没有 [参考资料N] 引用标注（{body[:60]!r}…）"
    return True, f"{elapsed:.1f}s 引用 OK（sources={len(sources['sources'])}，正文 {len(body)} 字）"


def main() -> int:
    ensure_service_up()
    failures = 0
    for question in QUESTIONS:
        passed, detail = "", ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            passed, detail = check_once(question)
            out(f"[{'PASS' if passed else 'FAIL'}] {question}（第 {attempt} 次）{detail}")
            if passed:
                break
        if not passed:
            failures += 1
    return finish(failures, len(QUESTIONS), "probe_chat")


if __name__ == "__main__":
    sys.exit(main())
