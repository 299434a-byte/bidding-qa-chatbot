# ============================================================================
# 文件：tests/test_deep_no_gain.py —— deep 模式「无增益终止」行为体检
#
# 【这个测试文件整体在测什么】
#   反思/重试会改写检索词再查一轮；若改写后命中的还是同一批切片，
#   继续跑就是白烧 LLM。判定规则：最近两轮检索命中的 doc_id 集合完全相同
#   → 立即终止重试，转入 FALLBACK（已有证据按「仅检索」方式列出）。
#
#   断言的都是外部可观察行为：事件流、还烧不烧大模型、metrics 计数；
#   不断言内部标志位。假 _stream_react_turn 负责「造出两轮命中一致」的世界。
# ============================================================================

from __future__ import annotations

import asyncio

import pytest

from app import deep_agent
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
    """一次测试的假世界：规划返回固定 3 步；每步的检索命中由 rounds 依次给出。

    hits_per_round[i] = 第 i+1 轮检索后 _rag_state["sources"] 的内容；
    relay_retrieval=False 模拟「这一步没调检索工具」（retrieval_seq 不前进）。
    """

    def __init__(self, hits_per_round: list[list[dict]], relay_retrieval: list[bool] | None = None) -> None:
        self.hits_per_round = hits_per_round
        self.relay_retrieval = relay_retrieval or [True] * len(hits_per_round)
        self.relay_calls = 0
        self.generate_calls = 0

    async def stream_react_turn(self, agent, stream_input, turn, states, **kwargs):
        idx = self.relay_calls
        self.relay_calls += 1
        if self.relay_retrieval[idx]:
            rag_state = states["_rag_state"]
            rag_state["sources"] = list(self.hits_per_round[idx])
            rag_state["meta"] = {"dense_count": len(self.hits_per_round[idx]), "final_count": len(self.hits_per_round[idx])}
            rag_state["hit_count"] = len(self.hits_per_round[idx])
            # 假世界里模拟「真的执行了一次检索」：序号前进（真实世界由 agent._run_retrieval 负责）。
            rag_state["retrieval_seq"] = int(rag_state.get("retrieval_seq") or 0) + 1
        yield {"type": "step_text", "text": f"第 {idx + 1} 步的中间结论"}

    async def astream_chat(self, *args, **kwargs):
        self.generate_calls += 1
        yield "综合后的答案"


def _install(monkeypatch, world: _World) -> None:
    """把假世界装进 deep_agent（规划 / 建图 / 步进执行 / 综合生成四件套）。"""
    monkeypatch.setattr(deep_agent, "complete_chat", lambda *a, **k: _PLAN_3_STEPS)
    monkeypatch.setattr(deep_agent, "_build_agent", lambda *a, **k: object())
    monkeypatch.setattr(deep_agent, "_stream_react_turn", world.stream_react_turn)
    monkeypatch.setattr(deep_agent, "astream_chat", world.astream_chat)


async def _collect(question: str = "招标公告查到了吗？") -> list[dict]:
    return [event async for event in deep_agent.run_deep_events(question, mode="plan")]


def test_identical_hits_stop_retry_and_fall_back(monkeypatch) -> None:
    """最近两轮命中完全一致 → 不再触发第三轮、不进综合生成、以 FALLBACK 收场。"""
    same = [_source("notice:1:0"), _source("project:2:0")]
    world = _World([same, list(same)])  # 两轮命中完全一致（重写了个寂寞）
    _install(monkeypatch, world)

    before = metrics_summary()["no_gain_stop"]
    events = asyncio.run(_collect())
    after = metrics_summary()["no_gain_stop"]

    # 不再触发第三轮重试。
    assert world.relay_calls == 2
    # 不再烧综合生成的 LLM。
    assert world.generate_calls == 0

    # 终止原因在事件里写明。
    no_gain_events = [e for e in events if e["type"] == "no_gain"]
    assert no_gain_events and no_gain_events[0]["reason"] == "no_gain"
    assert no_gain_events[0]["round"] == 2

    # 第三步不再执行，补一张「提前终止」的卡片（前端有交代）。
    third = [e for e in events if e["type"] == "plan_step" and e["index"] == 3]
    assert third and third[0]["status"] == "failed"
    assert "无增益" in third[0]["error"]

    # FALLBACK：已有证据按「仅检索」方式列出，而不是继续生成。
    deltas = [e for e in events if e["type"] == "delta"]
    assert deltas and "仅检索模式" in deltas[-1]["text"]

    done = events[-1]
    assert done["type"] == "done"
    assert done["mode"] == "agent_plan"
    assert done["reason"] == "no_gain"

    # metrics 计一次。
    assert after == before + 1


def test_different_hits_run_all_steps_and_generate(monkeypatch) -> None:
    """命中不同 → 正常走完三步并综合生成（既有行为不变，不误判）。"""
    world = _World(
        [[_source("a:1:0")], [_source("b:2:0")], [_source("c:3:0")]]
    )
    _install(monkeypatch, world)

    before = metrics_summary()["no_gain_stop"]
    events = asyncio.run(_collect())
    after = metrics_summary()["no_gain_stop"]

    assert world.relay_calls == 3
    assert world.generate_calls == 1

    assert not any(e["type"] == "no_gain" for e in events)
    done = events[-1]
    assert done["mode"] == "agent_plan"
    assert "reason" not in done
    assert any(e["type"] == "delta" and e.get("text") == "综合后的答案" for e in events)

    assert after == before


def test_step_without_retrieval_does_not_trigger_no_gain(monkeypatch) -> None:
    """第二步没调检索（命中集合没更新）→ 不算「又查了一遍还一样」，第三步照常执行。"""
    world = _World(
        [[_source("a:1:0")], [_source("a:1:0")], [_source("d:4:0")]],
        relay_retrieval=[True, False, True],
    )
    _install(monkeypatch, world)

    events = asyncio.run(_collect())

    # 第二步没检索 → 不参与判定；第三步仍执行，综合生成照跑。
    assert world.relay_calls == 3
    assert world.generate_calls == 1
    assert not any(e["type"] == "no_gain" for e in events)
