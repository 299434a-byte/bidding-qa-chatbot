# ============================================================================
# 文件：tests/test_api_sse_contract.py —— SSE 事件序列（接口契约）测试
#
# 【这个测试文件整体在测什么】
#   前端是按「事件到达顺序」渲染的：先来源卡片、再正文流、再修订卡片、最后收尾。
#   这份顺序一旦漂移，界面就会「先出正文后出来源」「done 之后还在冒正文」。
#   本文件用 TestClient 直接打 /api/chat 与 /api/agent/chat，把契约锁进测试：
#     · /api/chat（真实 answer_events）：retrieval → sources → delta → done；
#       done **之后**追加 citation_check（rag.py 的既定设计：正文由 done 收尾，
#       校验结论走补充通道；校验失败时先 revision 再 citation_check）。
#       citation_check 无论通过/失败都发：事件新增 confidence / hallucination_flags
#       等逐句可信度字段（纯新增），ok / issues / number_checks 的语义不变。
#     · /api/agent/chat：conversation → 工具事件 → sources → delta → citation_check → revision → done
#       （首个事件永远是 conversation——main.py 既有约定，先给前端会话 id 再流正文；
#       Agent 通路里校验事件在 done **之前**发出，见 agent.py 注释「校验事件要在收尾之前」）。
#   全部离线：检索与大模型用假实现顶掉，不连网、不连库。
#
# 【与「口口相传的契约」的两处差异（如实记录，不是笔误）】
#   ① citation_check 通过时也出现（承载逐句可信度 confidence）；正文 delta 绝不
#      出现在 done 之后——done 之后只允许补充类事件（revision / citation_check）。
#   ② /api/chat 的修订事件在 done 之后（正文不可撤回，修订走补充通道）；
#      /api/agent/chat 的修订事件在 done 之前（用户要求校验先于收尾）。
# 两个接口各自的顺序都是「有意为之」，测试锁住的是各自的现状。
# ============================================================================

from __future__ import annotations

# json：解析 SSE 的 data 行。
import json

# TestClient：不起真实服务器，直接在进程里发请求。
from fastapi.testclient import TestClient

# 被测对象。
from app.main import app
from app.retriever import RetrievalResult

# 一条结构完整的假命中（to_sources 需要这几个字段；正文含金额供引用校验比对）。
_FAKE_HIT = {
    "doc_id": "bidding_notices:1:0",
    "table": "bidding_notices",
    "label": "招标公告",
    "primary_key": 1,
    "score": 0.9,
    "text": "本项目预算金额为 1590.0 万元，资金已落实。",
    "fields": {"project_name": "某某工程"},
}


def _parse_sse(text: str) -> list[dict]:
    """把 SSE 响应文本解析成事件字典列表（跳过 [DONE] 哨兵）。"""
    events = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[len("data: ") :]))
    return events


def _patch_rag(monkeypatch, answer_text: str) -> None:
    """把检索与大模型打掉：固定一条命中 + 固定回答文本。"""
    monkeypatch.setattr(
        "app.rag.retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[dict(_FAKE_HIT)], dense_count=1, sparse_count=0, bm25_ready=True
        ),
    )
    monkeypatch.setattr("app.rag.stream_chat", lambda *args, **kwargs: iter([answer_text]))


def _ask_chat(client: TestClient, question: str) -> list[dict]:
    resp = client.post("/api/chat", json={"question": question})
    assert resp.status_code == 200, resp.text
    return _parse_sse(resp.text)


# ----------------------------------------------------------------------------
# 用例 1：/api/chat 的事件顺序与字段（校验通过路径）
# ----------------------------------------------------------------------------
def test_chat_event_order_and_fields(monkeypatch) -> None:
    """retrieval → sources → delta → done → citation_check；各事件的必备字段必须齐全。"""
    _patch_rag(monkeypatch, "该项目预算为 1590 万元 [参考资料1]。")
    events = _ask_chat(TestClient(app), "项目预算多少？")
    types = [e["type"] for e in events]

    # 顺序：先检索统计、再来源、再正文、再收尾；citation_check 是补充通道，只在 done 之后。
    assert types[0] == "retrieval"
    assert types[1] == "sources"
    assert "delta" in types
    assert types[-1] == "citation_check"
    assert types.index("done") < types.index("citation_check")
    # 正文 delta 绝不追到 done 之后（补充通道只允许 revision / citation_check）。
    last_delta = max(i for i, e in enumerate(events) if e["type"] == "delta")
    assert last_delta < types.index("done")
    # 校验通过：citation_check 仍然发出（承载逐句可信度），但没有 revision。
    check = events[-1]
    assert check["ok"] is True
    assert check["issues"] == []
    assert check["confidence"] == 1.0
    assert check["hallucination_flags"] == []
    assert "revision" not in types

    # 字段：前端渲染依赖它们，缺一个就会白屏/降级。
    retrieval = events[0]
    assert "meta" in retrieval and isinstance(retrieval["meta"], dict)
    sources = events[1]["sources"]
    assert sources and sources[0]["doc_id"] == "bidding_notices:1:0"
    assert sources[0]["label"] == "招标公告"
    # 正文 delta 必须带 text 字段。
    assert all("text" in e for e in events if e["type"] == "delta")
    # done 带 mode（前端据此显示徽标）。
    assert events[types.index("done")]["mode"] == "llm"


# ----------------------------------------------------------------------------
# 用例 2：/api/chat 引用校验失败路径 —— done 之后补 revision + citation_check
# ----------------------------------------------------------------------------
def test_chat_citation_failure_appends_revision_after_done(monkeypatch) -> None:
    """编造了不存在的 [参考资料9]：done 之后追加修订稿与校验结果（rag.py 的既定顺序）。"""
    _patch_rag(monkeypatch, "该项目预算为 50 万元 [参考资料9]。")
    events = _ask_chat(TestClient(app), "项目预算多少？")
    types = [e["type"] for e in events]

    # 核心契约：正文由 done 收尾；修订稿是「补充事件」，只能出现在 done 之后。
    assert types[-1] == "citation_check"
    assert types[-2] == "delta"
    assert types[-3] == "done"

    revision = events[-2]
    assert revision["channel"] == "revision"
    # 越界引用必须从修订稿里被删掉。
    assert "[参考资料9]" not in revision["text"]
    check = events[-1]
    assert check["ok"] is False
    assert check["issues"]  # 有问题清单供前端提示


# ----------------------------------------------------------------------------
# 用例 3：/api/agent/chat 的事件顺序（Agent 通路：校验事件在 done 之前）
# ----------------------------------------------------------------------------
def test_agent_chat_event_order(monkeypatch) -> None:
    """Agent 通路：工具事件 → sources → delta → citation_check → revision → done。

    【为什么这里替换成「契约文档里的事件流」】Agent 的执行体是 LangGraph + 大模型，
    重资产、不可离线。这一层要锁的是「接口把事件原样、按序、按 SSE 格式端出去」的契约，
    生成器内部的引用校验顺序已由 tests/test_agent_citation.py 单独锁定。
    """
    canned = [
        {"type": "tool_call", "name": "kb_search", "query": "市政道路", "arguments": {}},
        {"type": "tool_result", "name": "kb_search", "count": 1},
        {"type": "retrieval", "meta": {"final_count": 1}},
        {"type": "sources", "sources": [dict(_FAKE_HIT) | {"doc_id": "bidding_notices:1:0"}]},
        {"type": "delta", "text": "答案正文"},
        {"type": "citation_check", "ok": False, "issues": [{"kind": "ref", "ref": 9}]},
        {"type": "delta", "channel": "revision", "reason": "citation", "text": "修订稿"},
        {"type": "done", "mode": "agent_rag"},
    ]

    async def fake_agent_events(*args, **kwargs):
        for event in canned:
            yield event

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)
    resp = TestClient(app).post("/api/agent/chat", json={"question": "市政道路有哪些？"})
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)

    # 首个事件永远是 conversation（main.py 的既有约定：先告诉前端会话 id，再流正文）。
    assert events[0]["type"] == "conversation"
    assert "id" in events[0] and "mode" in events[0] and "saved" in events[0]
    # 其后逐条一致（接口不重排、不丢事件）。
    assert events[1:] == canned
    # Agent 收尾的 mode 是 agent_rag / agent_direct 系列。
    assert events[-1]["mode"] == "agent_rag"


# ----------------------------------------------------------------------------
# 用例 4：/api/agent/chat 空问题（真实生成器逻辑）—— error + done(agent_failed)
# ----------------------------------------------------------------------------
def test_agent_chat_empty_question_emits_error_then_done() -> None:
    """问题去空白后为空：error → done(agent_failed)，绝不进大模型。"""
    resp = TestClient(app).post("/api/agent/chat", json={"question": "   "})
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]

    # conversation 永远打头，随后是 error → done(agent_failed)。
    assert types == ["conversation", "error", "done"]
    assert "不能为空" in events[-2]["message"]
    assert events[-1]["mode"] == "agent_failed"


# ----------------------------------------------------------------------------
# 用例 5：/api/agent/chat 未配置大模型时降级走纯 RAG 通道（真实生成器逻辑）
# ----------------------------------------------------------------------------
def test_agent_chat_degrades_to_rag_channel(monkeypatch) -> None:
    """Agent 配不齐时整体降级委托 answer_events：事件序列与 /api/chat 一致。"""
    import dataclasses

    from app import agent as agent_mod
    from app.config import get_settings

    # 把「没配大模型」的配置打到两个入口：main 把 settings 显式传给 agent_events，
    # 所以光补 agent.get_settings 不够，必须连 app.main.get_settings 一起换成 no_llm，
    # 才能让 agent_configured 为假 → 走降级分支（否则真去建 LangGraph，又慢又不离线）。
    no_llm = dataclasses.replace(get_settings(), llm_api_key="")
    monkeypatch.setattr(agent_mod, "get_settings", lambda: no_llm)
    monkeypatch.setattr("app.main.get_settings", lambda: no_llm)
    _patch_rag(monkeypatch, "仅检索模式的降级回答")

    resp = TestClient(app).post("/api/agent/chat", json={"question": "降级路径测试？"})
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]

    # conversation 打头，其后是与纯 RAG 通道相同的骨架：retrieval → sources → delta → done。
    assert types[0] == "conversation"
    assert types[1] == "retrieval"
    assert types[2] == "sources"
    assert types[-1] == "done"
    # 没配大模型 → done.mode 是 retrieval_only（前端显示「仅检索模式」）。
    assert events[-1]["mode"] == "retrieval_only"
