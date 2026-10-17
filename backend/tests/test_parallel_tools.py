# ============================================================================
# 文件：tests/test_parallel_tools.py —— 一轮多工具并行执行（_ParallelToolNode）测试
#
# 【这个测试文件整体在测什么】
#   背景：ReAct 模型一轮可能同时返回多个 tool_calls（如「关系+统计」复合问题
#   同时查图谱与 PostgreSQL）。langgraph 的 ToolNode 用 asyncio.gather 并发执行，
#   我们在其上加了并发上限与单工具异常隔离——这三件事必须用测试锁死：
#     ① 结果按 tool_calls 原始顺序回填（tool_call_id 对齐，绝不按完成顺序）；
#     ② 并发数不超过 AGENT_TOOL_CONCURRENCY 指定的上限（信号量限流）；
#     ③ 单个工具执行异常只影响自己（status=error 的可读 ToolMessage），其余不受影响。
#   另外锁定：done 事件把「本轮单轮最大并行工具数」透传给 SSE 与 metrics。
#   全部离线：假工具 + 直接调用 _afunc / monkeypatch，不连大模型、不连库。
# ============================================================================

from __future__ import annotations

# 【为什么需要 asyncio / json】并行执行与 SSE 解析。
import asyncio
import json

# langchain 的 tool 装饰器与消息类型：造假工具与 tool_calls 输入。
from langchain_core.messages import AIMessage

# langgraph 运行时：直调 _afunc 需要一个空 Runtime（不进完整图执行）。
from langgraph.runtime import Runtime

# 被测对象：并行工具节点。
from app.agent import _ParallelToolNode

# 一把「完成顺序记录仪」：每个工具收尾时把自己写进列表，用于证明
# 完成顺序 ≠ 回填顺序（回填必须以 tool_calls 原始顺序为准）。
_completion_order: list[str] = []


def _tool_calls(*names_and_ids: tuple[str, str]) -> list[dict]:
    """拼一份模型返回的 tool_calls（OpenAI 兼容结构）。"""
    return [
        {"name": name, "args": {}, "id": call_id, "type": "tool_call"}
        for name, call_id in names_and_ids
    ]


def _run(node: _ParallelToolNode, calls: list[dict]) -> list:
    """直调 _afunc 跑一轮工具执行，返回按 tool_calls 顺序回填的 ToolMessage 列表。"""
    ai = AIMessage(content="", tool_calls=calls)
    config: dict = {}
    result = asyncio.run(node._afunc({"messages": [ai]}, config, Runtime()))
    # dict 输入形态 → 返回 {"messages": [...]}；与父类 _combine_tool_outputs 一致。
    return result["messages"]


# ----------------------------------------------------------------------------
# 用例 1：结果按 tool_calls 原始顺序回填（慢工具排前面也必须排在结果第一位）
# ----------------------------------------------------------------------------
def test_parallel_tool_calls_preserve_original_order():
    """slow 排在 tool_calls 首位、最后才完成：回填顺序仍必须与 tool_calls 一致。"""
    from langchain_core.tools import tool

    _completion_order.clear()

    @tool
    async def slow_tool() -> str:
        """慢工具：最后完成。"""
        await asyncio.sleep(0.05)
        _completion_order.append("slow")
        return "slow-done"

    @tool
    async def fast_tool() -> str:
        """快工具：最先完成。"""
        _completion_order.append("fast")
        return "fast-done"

    node = _ParallelToolNode([fast_tool, slow_tool], concurrency=2)
    # 故意把慢工具放第一位：如果回填按完成顺序，结果会被反转。
    messages = _run(node, _tool_calls(("slow_tool", "call_1"), ("fast_tool", "call_2")))

    # 完成顺序确实是「快先慢后」——证明两个工具真的并发跑了。
    assert _completion_order == ["fast", "slow"]
    # 但回填顺序严格按 tool_calls 原始顺序（tool_call_id 对齐）。
    assert [m.tool_call_id for m in messages] == ["call_1", "call_2"]
    assert [m.name for m in messages] == ["slow_tool", "fast_tool"]
    assert [m.content for m in messages] == ["slow-done", "fast-done"]


# ----------------------------------------------------------------------------
# 用例 2：并发数不超上限（信号量限流）
# ----------------------------------------------------------------------------
def test_parallel_tool_concurrency_is_capped():
    """并发上限 2、4 个工具同时提交：任意时刻在跑的工具数不得超过 2。"""
    from langchain_core.tools import tool

    state = {"active": 0, "peak": 0}

    @tool
    async def probe_tool() -> str:
        """探针工具：记录同时在跑的峰值。"""
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.02)
        state["active"] -= 1
        return "ok"

    node = _ParallelToolNode([probe_tool], concurrency=2)
    calls = _tool_calls(*[("probe_tool", f"call_{i}") for i in range(4)])
    messages = _run(node, calls)

    # 4 个都成功返回（并行不丢结果）。
    assert len(messages) == 4
    assert all(m.content == "ok" for m in messages)
    # 峰值并发被信号量卡在上限内（且确实跑起来过并发）。
    assert 1 <= state["peak"] <= 2


# ----------------------------------------------------------------------------
# 用例 3：单工具异常只影响自己，其余工具照常返回
# ----------------------------------------------------------------------------
def test_parallel_tool_error_is_isolated():
    """慢工具抛异常：自己的结果是 status=error 的可读 ToolMessage，快工具不受影响。"""
    from langchain_core.tools import tool

    @tool
    async def broken_slow_tool() -> str:
        """会抛执行异常的慢工具。"""
        await asyncio.sleep(0.05)
        raise RuntimeError("模拟下游超时")

    @tool
    async def healthy_fast_tool() -> str:
        """健康的快工具。"""
        return "fine"

    node = _ParallelToolNode([healthy_fast_tool, broken_slow_tool], concurrency=2)
    messages = _run(
        node, _tool_calls(("broken_slow_tool", "call_bad"), ("healthy_fast_tool", "call_ok"))
    )

    assert len(messages) == 2
    bad, ok = messages
    # 失败的工具：按 tool_call_id 对齐回填，status=error，内容是可读的失败说明。
    assert bad.tool_call_id == "call_bad"
    assert bad.status == "error"
    assert "执行失败" in bad.content
    assert "模拟下游超时" in bad.content
    # 同轮的健康工具完全不受影响。
    assert ok.tool_call_id == "call_ok"
    assert ok.status != "error"
    assert ok.content == "fine"


# ----------------------------------------------------------------------------
# 用例 4：单个 tool_calls 仍走同一条路径（不因并行改造而回归）
# ----------------------------------------------------------------------------
def test_single_tool_call_still_works():
    """只有一轮一个工具调用时：正常执行、正常回填。"""
    from langchain_core.tools import tool

    @tool
    async def solo_tool() -> str:
        """独苗工具。"""
        return "solo"

    node = _ParallelToolNode([solo_tool], concurrency=3)
    messages = _run(node, _tool_calls(("solo_tool", "call_solo")))

    assert len(messages) == 1
    assert messages[0].tool_call_id == "call_solo"
    assert messages[0].content == "solo"


# ----------------------------------------------------------------------------
# 用例 5：done 事件把并行工具数透传出 SSE（接口契约，纯新增字段）
# ----------------------------------------------------------------------------
def test_agent_chat_done_preserves_parallel_tools(monkeypatch) -> None:
    """/api/agent/chat：done 事件里的 parallel_tools 原样端给前端。"""
    from fastapi.testclient import TestClient

    from app.main import app

    canned = [{"type": "done", "mode": "agent_rag", "parallel_tools": 2}]

    async def fake_agent_events(*args, **kwargs):
        for event in canned:
            yield event

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)
    resp = TestClient(app).post("/api/agent/chat", json={"question": "关系+统计复合问题"})
    assert resp.status_code == 200, resp.text
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert events[-1]["parallel_tools"] == 2
    assert events[-1]["mode"] == "agent_rag"


# ----------------------------------------------------------------------------
# 用例 6：metrics 记录并行工具数（单轮最大批 + 出现过并行的问答数）
# ----------------------------------------------------------------------------
def test_metrics_records_parallel_tools(tmp_path) -> None:
    """记两条指标（并行 2 / 未并行 1）：max_parallel_tools=2，parallel_tool_rounds=1。"""
    import dataclasses

    from app import metrics as metrics_store
    from app.config import get_settings

    settings = dataclasses.replace(get_settings(), metrics_db_path=tmp_path / "m.db")
    metrics_store.record(
        mode="agent_rag", elapsed_ms=100, parallel_tools=2, settings=settings
    )
    metrics_store.record(mode="llm", elapsed_ms=50, settings=settings)

    summary = metrics_store.summary(settings)
    assert summary["max_parallel_tools"] == 2
    assert summary["parallel_tool_rounds"] == 1
