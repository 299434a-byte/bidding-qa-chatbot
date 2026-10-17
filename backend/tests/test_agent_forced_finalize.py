# ============================================================================
# 文件：tests/test_agent_forced_finalize.py —— ReAct 步数用尽的「强制收口」行为
#
# 【这个测试文件整体在测什么】
#   agent.agent_events 的工具循环有步数上限（AGENT_MAX_STEPS，撞上限时 LangGraph 抛
#   GraphRecursionError）。旧行为直接掉进兜底（多轮问题可能以「（未获得回答）」收场）；
#   现在必须先「强制收口」：bind_tools(tools, tool_choice="none") 禁止工具调用，
#   基于已有的工具观察再要一次最终文字答案，按正常 delta 流式发出。本文件锁四件事：
#     ① 模型一直要调工具 → 最终仍产出文字答案，事件流以 done(agent_rag) 收尾；
#     ② 收口那一轮真的被禁止了工具调用，且重放里带着工具返回的观察结果；
#     ③ 收口拿不到正文时才降级到原有兜底（error + 兜底文字 + done(agent_failed)）；
#     ④ 工具没来得及返回的悬空 tool_calls 会被截掉，不会带进收口（消息配对合法）。
#   全部离线：假 ReAct 图 + 假收口模型，不连大模型。
# ============================================================================

from __future__ import annotations

# asyncio：以同步方式消费 agent_events 异步生成器。
import asyncio

# 造假消息流与「步数用尽」异常。
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

# 被测模块与依赖。
from app import agent as agent_mod
from app.config import get_settings
from app.metrics import summary as metrics_summary


def _run_events(**kwargs) -> list[dict]:
    """跑一次 agent_events 并收集全部事件（离线假桩在各用例里装好）。"""
    kwargs.setdefault("settings", get_settings())

    async def _collect() -> list[dict]:
        return [event async for event in agent_mod.agent_events(**kwargs)]

    return asyncio.run(_collect())


class _NeverStopAgent:
    """假 ReAct 图：模型始终请求调用工具，直到撞步数上限（抛 GraphRecursionError）。

    事件顺序模拟真实 LangGraph：模型节点完成（updates 出 AIMessage）→
    工具节点执行（messages 出 ToolMessage）→ 工具节点完成（updates 出 ToolMessage）。
    """

    def __init__(self, *, tool_returns: bool = True) -> None:
        self.tool_returns = tool_returns

    def astream(self, stream_input, *, stream_mode=None, config=None):
        async def _gen():
            # updates 流真实结构是 {节点名: {"messages": [...]}}（节点 → 状态更新）。
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": agent_mod.TOOL_NAME,
                                        "args": {"query": "市政道路"},
                                        "id": "call_1",
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        ]
                    }
                },
            )
            if self.tool_returns:
                observation = ToolMessage(
                    content="[参考资料1] 来源表：招标公告｜预算 1590 万元",
                    name=agent_mod.TOOL_NAME,
                    tool_call_id="call_1",
                )
                # messages 流：工具返回会翻译成 tool_result / sources 事件。
                yield ("messages", (observation, {}))
                # updates 流：工具节点完成的完整消息进 transcript（收口重放来源）。
                yield ("updates", {"tools": {"messages": [observation]}})
            raise GraphRecursionError("Recursion limit of 6 reached.")

        return _gen()


class _FakeFinalizeModel:
    """假收口模型：记录 bind_tools 约束与被重放的消息，产出固定文字。"""

    def __init__(self, text: str = "综合以上工具观察：项目预算 1590 万元。") -> None:
        self.text = text
        self.tool_choice: str | None = None
        self.bound_tools: list | None = None
        self.seen_messages: list | None = None

    def bind_tools(self, tools, tool_choice=None):
        self.bound_tools = list(tools)
        self.tool_choice = tool_choice
        return self

    async def astream(self, messages):
        self.seen_messages = list(messages)
        if self.text:
            yield AIMessageChunk(content=self.text)


def _patch_agent(monkeypatch, model: _FakeFinalizeModel, *, tool_returns: bool = True) -> None:
    """把建图与收口模型都换成假桩：整个 ReAct 循环不连网。"""
    monkeypatch.setattr(
        agent_mod,
        "_build_agent",
        lambda s, web_search_enabled=False, intent="unknown": _NeverStopAgent(
            tool_returns=tool_returns
        ),
    )
    monkeypatch.setattr(agent_mod, "_make_chat_model", lambda s: model)


# ----------------------------------------------------------------------------
# 用例 1：模型一直要求调工具 → 强制收口产出文字答案，事件流以 done 收尾
# ----------------------------------------------------------------------------
def test_forced_finalize_emits_text_answer_and_done(monkeypatch) -> None:
    """步数用尽后仍要有正文 delta、无 error、done(agent_rag) 收尾，且 metrics 计数 +1。"""
    fake_model = _FakeFinalizeModel()
    _patch_agent(monkeypatch, fake_model)

    before = metrics_summary()["forced_finalize"]
    events = _run_events(question="市政道路的预算是多少？")
    after = metrics_summary()["forced_finalize"]

    types = [e["type"] for e in events]
    # 最终产出文字答案（正常 delta 通道，不是 revision）。
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert any("综合以上工具观察" in text for text in deltas)
    # 收口成功就不该出现 error；事件流以 done 收尾，且此前调过工具 → agent_rag。
    assert "error" not in types
    assert events[-1]["type"] == "done"
    assert events[-1]["mode"] == "agent_rag"
    # 触发了强制收口 → metrics 事件计数恰好 +1（外部可观测的计数，不是内部标志）。
    assert after == before + 1


# ----------------------------------------------------------------------------
# 用例 2：收口那一轮禁止工具调用，且基于已有的工具观察作答
# ----------------------------------------------------------------------------
def test_forced_finalize_bans_tools_and_replays_observations(monkeypatch) -> None:
    """收口请求必须 tool_choice=none，重放消息里必须带工具返回的观察结果。"""
    fake_model = _FakeFinalizeModel()
    _patch_agent(monkeypatch, fake_model)

    _run_events(question="市政道路的预算是多少？")

    # 需求语义：这一轮明确禁止模型再调工具（约束真的传给了模型）。
    assert fake_model.tool_choice == "none"
    assert fake_model.bound_tools  # 约束是带着工具清单声明的
    # 「基于已有工具观察」：重放里有原始问题（Human）与工具返回（Tool）。
    messages = fake_model.seen_messages or []
    assert any(isinstance(m, HumanMessage) and "市政道路" in str(m.content) for m in messages)
    assert any(isinstance(m, ToolMessage) and "1590" in str(m.content) for m in messages)


# ----------------------------------------------------------------------------
# 用例 3：收口拿不到正文 → 降级到原有兜底（error + 兜底文字 + done(agent_failed)）
# ----------------------------------------------------------------------------
def test_forced_finalize_empty_falls_back_to_last_resort(monkeypatch) -> None:
    """收口这一轮一个字都没有时，走原有兜底通道，绝不静默结束。"""
    fake_model = _FakeFinalizeModel(text="")
    _patch_agent(monkeypatch, fake_model)
    # 原有兜底的最后一道：拿问题+sources 直接再问一次。
    monkeypatch.setattr(agent_mod, "stream_chat", lambda *args, **kwargs: iter(["兜底答案正文"]))

    events = _run_events(question="市政道路的预算是多少？")
    types = [e["type"] for e in events]

    # 兜底路径的既有结构：error 打头、兜底正文、done(agent_failed) 收尾。
    assert "error" in types
    assert any(e["type"] == "delta" and "兜底答案正文" in e["text"] for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["mode"] == "agent_failed"


# ----------------------------------------------------------------------------
# 用例 4：悬空 tool_calls（工具没来得及返回）被截掉，收口消息仍合法
# ----------------------------------------------------------------------------
def test_forced_finalize_prunes_dangling_tool_calls(monkeypatch) -> None:
    """撞上限撞在工具执行前：重放里不能出现「无响应的 tool_calls」，收口照常产出正文。"""
    fake_model = _FakeFinalizeModel(text="基于已知信息的最终答案。")
    _patch_agent(monkeypatch, fake_model, tool_returns=False)

    events = _run_events(question="市政道路的预算是多少？")

    # 收口仍产出正文并以 done 收尾（没有半途崩掉）。
    assert any(e["type"] == "delta" and "最终答案" in e["text"] for e in events)
    assert events[-1]["type"] == "done"
    # 重放消息里没有悬空的 tool_calls / tool 消息（配对合法，不会被 400 拒绝）。
    messages = fake_model.seen_messages or []
    assert not any(isinstance(m, ToolMessage) for m in messages)
    assert not any(getattr(m, "tool_calls", None) for m in messages)
    # 原始问题仍然在（收口是基于本轮对话收口，不是凭空生成）。
    assert any(isinstance(m, HumanMessage) for m in messages)
