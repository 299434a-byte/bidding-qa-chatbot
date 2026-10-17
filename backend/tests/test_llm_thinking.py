# ============================================================================
# 文件：tests/test_llm_thinking.py —— 思维链单独一路 SSE（thinking 事件）行为
#
# 【这个测试文件整体在测什么】
#   DeepSeek 等推理模型会先输出 reasoning_content（思维链）再出正文。旧行为里
#   思维链要么被 langchain_openai 基类整段丢弃（用户干等十几秒黑屏），要么混进
#   正文（污染答案）。现在的契约是「两路分开」：
#     · llm 层：stream_chat / astream_chat 把 delta.reasoning_content 经 on_thinking
#       回调单独产出，yield 的永远只有正文；
#     · rag 层：answer_events 把思维链转成 {"type": "thinking", "delta": ...} 事件，
#       排在正文 delta 之前下发，正文 delta 拼接结果里绝不含思维链文本；
#     · agent 层：ReasoningChatOpenAI 在 chunk 转换点把 reasoning_content 挂进
#       additional_kwargs，_stream_react_turn 逐片透传成 thinking 事件；
#   且事件流以 done 收尾，done 之后不再有任何 thinking / delta 事件。
#   全部离线：假 client / 假图 / 假 stream_chat，不连大模型。
# ============================================================================

from __future__ import annotations

# asyncio：以同步方式消费 agent_events 异步生成器。
import asyncio

# SimpleNamespace：把假的流式增量包装成对象（stream_chat 用属性访问取 delta 字段）。
from types import SimpleNamespace

# 假消息流（agent 链路的思维链 chunk）。
from langchain_core.messages import AIMessageChunk

# 被测模块。
from app import llm as llm_mod
from app import rag as rag_mod
from app import agent as agent_mod
from app.config import get_settings
from app.retriever import RetrievalResult

# 与 test_api_sse_contract 同款的结构完整假命中（to_sources 与引用校验需要这些字段）。
_FAKE_HIT = {
    "doc_id": "bidding_notices:1:0",
    "table": "bidding_notices",
    "label": "招标公告",
    "primary_key": 1,
    "score": 0.9,
    "text": "本项目预算金额为 1590.0 万元，资金已落实。",
    "fields": {"project_name": "某某工程"},
}


def _delta_chunk(reasoning: str | None, content: str | None) -> SimpleNamespace:
    """造一个 OpenAI 风格的流式增量分片（delta 上可同时带 reasoning_content 与 content）。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content=reasoning, content=content))]
    )


# ----------------------------------------------------------------------------
# 用例 1：llm.stream_chat 分流——思维链走回调，yield 只有正文
# ----------------------------------------------------------------------------
def test_stream_chat_routes_reasoning_to_callback(monkeypatch) -> None:
    """reasoning 增量触发 on_thinking、不出现在 yield 里；正文增量只走 yield。"""
    chunks = [
        _delta_chunk("先想：用户在问预算。", None),
        _delta_chunk(None, "该项目预算为 1590 万元"),
        _delta_chunk("再想：核对金额无误。", " [参考资料1]。"),
    ]

    class _FakeCompletions:
        def create(self, **kwargs):
            return iter(chunks)

    class _FakeClient:
        chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr(llm_mod, "_client", lambda s, timeout=None: _FakeClient())

    seen: list[str] = []
    pieces = list(llm_mod.stream_chat([{"role": "user", "content": "预算？"}], on_thinking=seen.append))

    # 思维链按到达顺序进入回调。
    assert seen == ["先想：用户在问预算。", "再想：核对金额无误。"]
    # yield 的拼接结果只有正文，不含任何思维链文本。
    joined = "".join(pieces)
    assert joined == "该项目预算为 1590 万元 [参考资料1]。"
    assert "先想" not in joined and "再想" not in joined


# ----------------------------------------------------------------------------
# 用例 2：llm.astream_chat 同款分流（异步路径共用 _delta_piece）
# ----------------------------------------------------------------------------
def test_astream_chat_routes_reasoning_to_callback(monkeypatch) -> None:
    """异步版行为与同步版一致：回调收思维链，yield 只有正文。"""
    chunks = [
        _delta_chunk("推理片段A", None),
        _delta_chunk(None, "正文片段"),
    ]

    class _FakeStream:
        def __aiter__(self):
            async def _gen():
                for chunk in chunks:
                    yield chunk

            return _gen()

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeStream()

    class _FakeClient:
        chat = SimpleNamespace(completions=_FakeCompletions())

    async def _apatch():
        monkeypatch.setattr(llm_mod, "aclient", lambda s, timeout=None: _FakeClient())
        seen: list[str] = []

        async def _consume():
            return [
                piece
                async for piece in llm_mod.astream_chat(
                    [{"role": "user", "content": "预算？"}], on_thinking=seen.append
                )
            ]

        pieces = await _consume()
        assert seen == ["推理片段A"]
        assert "".join(pieces) == "正文片段"

    asyncio.run(_apatch())


# ----------------------------------------------------------------------------
# 用例 3：answer_events 事件流——thinking 在正文 delta 之前，正文不被污染，done 收尾
# ----------------------------------------------------------------------------
def test_answer_events_emits_thinking_before_delta_without_pollution(monkeypatch) -> None:
    """思维链以 thinking 事件先发；delta 拼接结果不含思维链；done 之后只追加 citation_check。"""
    monkeypatch.setattr(
        rag_mod,
        "retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[dict(_FAKE_HIT)], dense_count=1, sparse_count=0, bm25_ready=True
        ),
    )

    def fake_stream_chat(messages, settings=None, *, on_thinking=None, **kwargs):
        # 模拟推理模型：先回调思维链，再吐正文——事件层必须把两者分两路下发。
        if on_thinking:
            on_thinking("思维链甲：查预算。")
        yield "该项目预算为 1590 万元 [参考资料1]。"
        if on_thinking:
            on_thinking("思维链乙：核对完毕。")
        yield "（回答结束）"

    monkeypatch.setattr(rag_mod, "stream_chat", fake_stream_chat)

    events = list(rag_mod.answer_events("项目预算多少？"))
    types = [e["type"] for e in events]

    # thinking 事件按序到达，内容与回调收到的一致。
    thinking_texts = [e["delta"] for e in events if e["type"] == "thinking"]
    assert thinking_texts == ["思维链甲：查预算。", "思维链乙：核对完毕。"]

    # 顺序契约：第一段正文（非空 delta）之前必须已经下发了第一个 thinking。
    first_thinking = next(i for i, e in enumerate(events) if e["type"] == "thinking")
    first_text = next(i for i, e in enumerate(events) if e["type"] == "delta" and e.get("text"))
    assert first_thinking < first_text

    # 正文 delta 拼接结果不含任何思维链文本（思维链绝不能污染答案）。
    body = "".join(e["text"] for e in events if e["type"] == "delta" and not e.get("channel"))
    assert body == "该项目预算为 1590 万元 [参考资料1]。（回答结束）"
    assert "思维链" not in body

    # done 收尾；done 之后只允许追加 citation_check（校验补充通道，携带逐句可信度），
    # 不再有正文 / revision 事件。
    assert types[-2:] == ["done", "citation_check"]
    assert events[types.index("done")]["mode"] == "llm"
    assert events[-1]["ok"] is True


# ----------------------------------------------------------------------------
# 用例 4：agent 链路——chunk 带 reasoning_content 时透传 thinking 事件，delta 干净
# ----------------------------------------------------------------------------
def test_agent_stream_forwards_thinking_without_polluting_delta(monkeypatch) -> None:
    """_stream_react_turn 把 additional_kwargs 里的思维链转成 thinking 事件，正文走 delta。"""
    reasoning_text = "用户在问预算，先检索知识库……"
    answer_text = "该项目预算为 1590 万元。"

    class _ThinkingAgent:
        """假 ReAct 图：messages 流直接给出带思维链的模型增量 chunk（一次收工）。"""

        def astream(self, stream_input, *, stream_mode=None, config=None):
            async def _gen():
                chunk = AIMessageChunk(
                    content=answer_text,
                    additional_kwargs={"reasoning_content": reasoning_text},
                )
                yield ("messages", (chunk, {}))

            return _gen()

    monkeypatch.setattr(
        agent_mod,
        "_build_agent",
        lambda s, web_search_enabled=False, intent="unknown": _ThinkingAgent(),
    )

    async def _collect():
        return [e async for e in agent_mod.agent_events("市政道路的预算是多少？", get_settings())]

    events = asyncio.run(_collect())

    # thinking 事件透传了思维链。
    assert any(e["type"] == "thinking" and e["delta"] == reasoning_text for e in events)
    # 正文 delta 干净：任何 delta 文本都不含思维链内容。
    assert any(e["type"] == "delta" and e.get("text") == answer_text for e in events)
    assert all(reasoning_text not in (e.get("text") or "") for e in events if e["type"] == "delta")
    # 事件流以 done 收尾。
    assert events[-1]["type"] == "done"


# ----------------------------------------------------------------------------
# 用例 5：ReasoningChatOpenAI 在 chunk 转换点保留 reasoning_content（基类会丢）
# ----------------------------------------------------------------------------
def test_reasoning_chat_model_keeps_reasoning_in_additional_kwargs() -> None:
    """基类转换丢弃 reasoning_content；子类把它挂进 additional_kwargs（透传的根基）。"""
    from langchain_openai.chat_models.base import ChatOpenAI

    raw_chunk = {
        "choices": [{"delta": {"role": "assistant", "content": None, "reasoning_content": "推理片段"}}]
    }

    base = ChatOpenAI(model="m", api_key="test-key", base_url="http://127.0.0.1:9/v1")
    base_gen = base._convert_chunk_to_generation_chunk(raw_chunk, AIMessageChunk, {})
    # 基类行为（这正是思维链丢失的根源）：additional_kwargs 里没有 reasoning_content。
    assert "reasoning_content" not in (base_gen.message.additional_kwargs if base_gen else {})

    model = agent_mod.ReasoningChatOpenAI(model="m", api_key="test-key", base_url="http://127.0.0.1:9/v1")
    gen = model._convert_chunk_to_generation_chunk(raw_chunk, AIMessageChunk, {})
    assert gen is not None
    assert gen.message.additional_kwargs.get("reasoning_content") == "推理片段"
