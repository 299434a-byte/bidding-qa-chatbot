# ============================================================================
# 文件：tests/test_deep_budget.py —— deep 模式「预算四本账」行为测试
#
# 【这个测试文件整体在测什么】
#   deep 模式的预算拆成四本独立账：工具调用 / 检索 / 大模型调用 / 整体时间。
#   任何一本账先超限都要立即收敛到已有证据作答（不抛异常、不空手结束），
#   并且在 done 事件的 meta（新增 budget 字段）里能看到超限项与实际消耗；
#   metrics 记录一次深度问答的平均消耗（summary()["budget"]）。
#
#   断言的都是外部可观察行为：事件流、是否还烧大模型、done.meta、metrics；
#   不断言内部标志位。假 _stream_react_turn 负责「造出各种超限场景」的世界。
# ============================================================================

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from app import deep_agent
from app.agent import TOOL_NAME
from app.config import get_settings
from app.metrics import summary as metrics_summary


def _source(doc_id: str, text: str = "正文片段") -> dict:
    """造一条来源（retrieval_only_answer / _merge_meta 所需字段齐全）。"""
    return {
        "doc_id": doc_id,
        "table": "procurement_notices",
        "label": "采购公告",
        "primary_key": doc_id,
        "chunk_index": 0,
        "chunk_total": 1,
        "rrf_score": 0.016,
        "scores": {"dense": 0.5},
        "ranks": {"dense": 1},
        "matched_by": ["dense"],
        "text": text,
        "fields": {"project_name": doc_id},
    }


_PLAN_3_STEPS = (
    '{"steps": [{"title": "查公告", "detail": "查公告"}, '
    '{"title": "查项目", "detail": "查项目"}, '
    '{"title": "查公司", "detail": "查公司"}]}'
)


class _World:
    """假世界：规划返回固定 3 步；每步可配置吐出的 tool_call 事件（名字任意）。"""

    def __init__(self, tool_call_names: list[list[str]] | None = None) -> None:
        self.tool_call_names = tool_call_names or [[] for _ in range(3)]
        self.relay_calls = 0
        self.generate_calls = 0

    async def stream_react_turn(self, agent, stream_input, turn, states, **kwargs):
        idx = self.relay_calls
        self.relay_calls += 1
        rag_state = states["_rag_state"]
        source = _source(f"doc{idx}:1:0")
        rag_state["sources"] = [source]
        rag_state["meta"] = {"dense_count": 1, "final_count": 1}
        rag_state["hit_count"] = 1
        rag_state["retrieval_seq"] = int(rag_state.get("retrieval_seq") or 0) + 1
        for name in self.tool_call_names[idx]:
            yield {"type": "tool_call", "name": name, "query": "q", "arguments": {}}
        yield {"type": "step_text", "text": f"第 {idx + 1} 步的中间结论"}

    async def astream_chat(self, *args, **kwargs):
        self.generate_calls += 1
        yield "综合后的答案"


def _install(monkeypatch, world: _World) -> None:
    monkeypatch.setattr(deep_agent, "complete_chat", lambda *a, **k: _PLAN_3_STEPS)
    monkeypatch.setattr(deep_agent, "_build_agent", lambda *a, **k: object())
    monkeypatch.setattr(deep_agent, "_stream_react_turn", world.stream_react_turn)
    monkeypatch.setattr(deep_agent, "astream_chat", world.astream_chat)


async def _collect(settings=None, mode: str = "plan") -> list[dict]:
    return [e async for e in deep_agent.run_deep_events("招标公告查到了吗？", settings=settings, mode=mode)]


def _done(events: list[dict]) -> dict:
    done = [e for e in events if e["type"] == "done"]
    assert done, "事件流必须以 done 收口"
    return done[-1]


# ----------------------------------------------------------------------------
# 用例 1：大模型账本先超限 —— 不再进综合生成，以已有证据收敛作答
# ----------------------------------------------------------------------------
def test_llm_budget_exceeded_converges_with_evidence(monkeypatch) -> None:
    """LLM 调用上限设为 1：规划用掉第 1 次后即超限 → 跳过后续步骤与综合，FALLBACK 收场。"""
    world = _World()
    _install(monkeypatch, world)
    s = replace(get_settings(), deep_llm_call_limit=1, deep_tool_call_limit=0, deep_retrieval_limit=0)

    events = asyncio.run(_collect(settings=s))

    # 只有第 1 步起跑过（后续步骤因超限被跳过），综合生成不再烧 LLM。
    assert world.generate_calls == 0
    assert world.relay_calls == 1
    # 收敛到已有证据作答：有正文、有来源，不是空手结束。
    deltas = [e for e in events if e["type"] == "delta"]
    assert deltas and "仅检索模式" in deltas[-1]["text"]
    assert any(e["type"] == "sources" and e["sources"] for e in events)
    # done.meta 里能看到超限项。
    done = _done(events)
    assert done["reason"] == "budget"
    assert "llm_calls" in done["budget"]["exceeded"]
    assert done["budget"]["llm_call_limit"] == 1


# ----------------------------------------------------------------------------
# 用例 2：工具账本先超限 —— 本步立即收手，后续步骤不再起跑
# ----------------------------------------------------------------------------
def test_tool_budget_exceeded_stops_mid_run(monkeypatch) -> None:
    """工具调用上限设为 1：第 1 步连发 2 次 tool_call → 第 2 次后立即收手。"""
    world = _World(tool_call_names=[["pg", "graph"], ["pg"], ["pg"]])
    _install(monkeypatch, world)
    s = replace(get_settings(), deep_tool_call_limit=1, deep_retrieval_limit=0, deep_llm_call_limit=99)

    events = asyncio.run(_collect(settings=s))

    # 第 1 步超限后，第 2、3 步不再起跑。
    assert world.relay_calls == 1
    skipped = [e for e in events if e["type"] == "plan_step" and e["status"] == "failed"]
    assert len(skipped) == 2
    done = _done(events)
    assert done["reason"] == "budget"
    assert "tool_calls" in done["budget"]["exceeded"]
    assert done["budget"]["tool_calls"] == 2


# ----------------------------------------------------------------------------
# 用例 3：检索账本先超限 —— 检索类工具单独记账，超限同样收敛
# ----------------------------------------------------------------------------
def test_retrieval_budget_exceeded_converges(monkeypatch) -> None:
    """检索上限设为 1：两次知识库检索后超限 → 收敛，exceeded 里是 retrievals。"""
    world = _World(tool_call_names=[[TOOL_NAME], [TOOL_NAME], [TOOL_NAME]])
    _install(monkeypatch, world)
    s = replace(get_settings(), deep_retrieval_limit=1, deep_tool_call_limit=0, deep_llm_call_limit=99)

    events = asyncio.run(_collect(settings=s))

    # 第 1 步检索 1 次（未超限），第 2 步检索到第 2 次时超限收手，第 3 步不再起跑。
    assert world.relay_calls == 2
    done = _done(events)
    assert done["reason"] == "budget"
    assert "retrievals" in done["budget"]["exceeded"]
    assert done["budget"]["retrievals"] == 2


# ----------------------------------------------------------------------------
# 用例 4：时间账本 —— _Budget 直接判定超时
# ----------------------------------------------------------------------------
def test_time_budget_flagged() -> None:
    s = get_settings()
    budget = deep_agent._Budget(s, started=time.monotonic() - (s.deep_timeout + 5))
    assert "time_seconds" in budget.exceeded()
    assert budget.over_budget()
    assert "时间预算已超限" in budget.reason_text()


# ----------------------------------------------------------------------------
# 用例 5：四本账都有余量 —— 既有行为不变，done.budget 记实际消耗
# ----------------------------------------------------------------------------
def test_normal_run_records_budget(monkeypatch) -> None:
    world = _World(tool_call_names=[[TOOL_NAME], [], []])
    _install(monkeypatch, world)

    events = asyncio.run(_collect())

    # 正常走完三步并综合生成（不误判）。
    assert world.relay_calls == 3
    assert world.generate_calls == 1
    done = _done(events)
    assert "reason" not in done
    assert done["budget"]["exceeded"] == []
    # 实际消耗可见：LLM 至少规划 1 次 + 3 步 + 综合 1 次；检索 1 次；工具 1 次。
    assert done["budget"]["llm_calls"] >= 5
    assert done["budget"]["retrievals"] == 1
    assert done["budget"]["tool_calls"] == 1
    assert done["budget"]["elapsed_seconds"] >= 0


# ----------------------------------------------------------------------------
# 用例 6：metrics 记账 —— 一次深度问答一条预算消耗，超限项进汇总
# ----------------------------------------------------------------------------
def test_budget_recorded_in_metrics(monkeypatch) -> None:
    world = _World()
    _install(monkeypatch, world)
    s = replace(get_settings(), deep_llm_call_limit=1)

    before = metrics_summary()["budget"]
    asyncio.run(_collect(settings=s))
    after = metrics_summary()["budget"]

    assert after["samples"] == before["samples"] + 1
    # 本例 llm 账超限一次 → 汇总里对应计数增加。
    assert after["exceeded"]["llm_calls"] >= before["exceeded"]["llm_calls"] + 1
