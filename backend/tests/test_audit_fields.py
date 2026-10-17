# ============================================================================
# 文件：tests/test_audit_fields.py —— 审计面板数据（SSE 纯新增字段）契约
#
# 【这个测试文件整体在测什么】
#   前端审计面板要展示可信度 / 阶段耗时 / 工具调用 / 无依据句，数据全部来自既有
#   SSE 事件的**新增字段**（不加新接口）：
#     · done.phase_timings   —— 检索 / 重排 / 生成 各段毫秒数（没跑过的阶段不出现）；
#     · tool_result.elapsed_ms / ok —— 单工具耗时与成败；
#     · 历史 meta 随上述字段落库，刷新页面后还能回放。
#   本文件锁住这些字段的**有无与取值形态**：字段只增不改，旧字段语义不变。
#   全部离线：假检索 / 假大模型 / 假 Agent 事件流，不连网不连库。
# ============================================================================

from __future__ import annotations

# json：解析 /api/agent/chat 的 SSE data 行。
import json

from fastapi.testclient import TestClient

from app import rag
from app.config import get_settings
from app.retriever import RetrievalResult

# 结构完整的假命中（to_sources 与引用校验需要这些字段）。
_FAKE_HIT = {
    "doc_id": "bidding_notices:1:0",
    "table": "bidding_notices",
    "label": "招标公告",
    "primary_key": 1,
    "score": 0.9,
    "text": "本项目预算金额为 1590.0 万元，资金已落实。",
    "fields": {"project_name": "某某工程"},
}


def _patch_rag(monkeypatch, answer_text: str) -> None:
    """把检索与大模型打掉：固定一条命中 + 固定回答文本。"""
    monkeypatch.setattr(
        rag,
        "retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[dict(_FAKE_HIT)], dense_count=1, sparse_count=0, bm25_ready=True
        ),
    )
    monkeypatch.setattr(rag, "stream_chat", lambda *args, **kwargs: iter([answer_text]))


# ----------------------------------------------------------------------------
# 用例 1：/api/chat 通路的 done 带 phase_timings（检索/重排/生成三段）
# ----------------------------------------------------------------------------
def test_answer_events_done_carries_phase_timings(monkeypatch):
    """完整生成通路的 done 必须带 retrieval_ms / rerank_ms / generation_ms。"""
    _patch_rag(monkeypatch, "该项目预算为 1590 万元 [参考资料1]。")

    events = list(rag.answer_events("项目预算多少？", settings=get_settings()))
    done = [e for e in events if e.get("type") == "done"]

    assert len(done) == 1
    timings = done[0].get("phase_timings")
    assert isinstance(timings, dict)
    # 三段都在，且是非负整数毫秒
    for key in ("retrieval_ms", "rerank_ms", "generation_ms"):
        assert key in timings, f"缺少阶段耗时 {key}"
        assert isinstance(timings[key], int) and timings[key] >= 0


# ----------------------------------------------------------------------------
# 用例 2：无数据通路也有耗时（检索/重排跑了，生成没跑就只有前两段）
# ----------------------------------------------------------------------------
def test_no_data_done_keeps_partial_phase_timings(monkeypatch):
    """检索无命中 → done(mode=no_data)：有检索/重排耗时，没有 generation_ms。"""
    monkeypatch.setattr(
        rag,
        "retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[], dense_count=0, sparse_count=0, bm25_ready=True
        ),
    )

    events = list(rag.answer_events("不存在的问题", settings=get_settings()))
    done = [e for e in events if e.get("type") == "done"]

    assert len(done) == 1
    assert done[0]["mode"] == "no_data"
    timings = done[0]["phase_timings"]
    assert "retrieval_ms" in timings and "rerank_ms" in timings
    # 没进生成阶段：不出现 generation_ms（前端「没有的阶段不显示」）
    assert "generation_ms" not in timings


# ----------------------------------------------------------------------------
# 用例 3：直答通道（不检索）只有 generation_ms
# ----------------------------------------------------------------------------
def test_direct_answer_done_only_generation(monkeypatch):
    """needs_retrieval=False 的寒暄直答：phase_timings 只有 generation_ms。"""
    from app.config import get_settings as _gs
    from app.rag import _stream_direct_answer

    monkeypatch.setattr(
        rag, "stream_chat", lambda *args, **kwargs: iter(["你好，有什么可以帮你？"])
    )

    events = list(_stream_direct_answer("你好", _gs()))
    done = [e for e in events if e.get("type") == "done"]

    assert len(done) == 1
    assert done[0]["mode"] == "direct"
    assert done[0].get("skipped_retrieval") is True
    timings = done[0]["phase_timings"]
    assert set(timings) == {"generation_ms"}
    assert isinstance(timings["generation_ms"], int) and timings["generation_ms"] >= 0


# ----------------------------------------------------------------------------
# 用例 4：Agent 通路的 done 带 phase_timings（有工具轮时含检索/重排）
# ----------------------------------------------------------------------------
def test_agent_done_carries_phase_timings(monkeypatch):
    """/api/agent/chat：done 事件的 phase_timings 原样端给前端。"""
    from app.main import app

    canned = [
        {
            "type": "done",
            "mode": "agent_rag",
            "parallel_tools": 1,
            "phase_timings": {"retrieval_ms": 12, "rerank_ms": 34, "generation_ms": 56},
        }
    ]

    async def fake_agent_events(*args, **kwargs):
        for event in canned:
            yield event

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)
    resp = TestClient(app).post("/api/agent/chat", json={"question": "项目预算多少？"})
    assert resp.status_code == 200, resp.text
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    done = events[-1]
    assert done["phase_timings"] == {
        "retrieval_ms": 12,
        "rerank_ms": 34,
        "generation_ms": 56,
    }


# ----------------------------------------------------------------------------
# 用例 5：tool_result 带 elapsed_ms / ok（纯新增字段，旧字段不变）
# ----------------------------------------------------------------------------
def test_tool_result_carries_elapsed_and_ok(monkeypatch):
    """/api/agent/chat：tool_result 的 count 语义不变，新增 elapsed_ms / ok 原样透传。"""
    from app.main import app

    canned = [
        {"type": "tool_result", "name": "search_bidding_knowledge", "count": 3,
         "elapsed_ms": 88, "ok": True},
        {"type": "tool_result", "name": "query_bidding_postgres", "count": 0,
         "elapsed_ms": 12, "ok": False},
        {"type": "done", "mode": "agent_rag", "phase_timings": {"generation_ms": 5}},
    ]

    async def fake_agent_events(*args, **kwargs):
        for event in canned:
            yield event

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)
    resp = TestClient(app).post("/api/agent/chat", json={"question": "统计题"})
    assert resp.status_code == 200, resp.text
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    results = [e for e in events if e.get("type") == "tool_result"]
    assert results[0]["count"] == 3 and results[0]["elapsed_ms"] == 88 and results[0]["ok"] is True
    assert results[1]["count"] == 0 and results[1]["elapsed_ms"] == 12 and results[1]["ok"] is False


# ----------------------------------------------------------------------------
# 用例 6：历史 meta 落库带审计字段（刷新后可回放）
# ----------------------------------------------------------------------------
def test_history_meta_carries_audit_fields():
    """_EventCollector.meta 把 phase_timings / 逐句可信度 / 单工具耗时随 meta 落库。"""
    from app.main import _EventCollector

    collector = _EventCollector()
    collector.feed({"type": "tool_call", "name": "search_bidding_knowledge", "query": "预算"})
    collector.feed({
        "type": "tool_result", "name": "search_bidding_knowledge", "count": 2,
        "elapsed_ms": 70, "ok": True,
    })
    collector.feed({
        "type": "citation_check", "ok": False, "issues": [], "number_checks": [],
        "confidence": 0.5, "supported_sentences": 1, "total_sentences": 2,
        "hallucination_flags": [{"sentence": "编造句。", "reason": "no_citation"}],
    })
    collector.feed({
        "type": "done", "mode": "agent_rag",
        "phase_timings": {"retrieval_ms": 10, "rerank_ms": 20, "generation_ms": 30},
    })

    meta = collector.meta("agent_rag")
    assert meta["phase_timings"] == {"retrieval_ms": 10, "rerank_ms": 20, "generation_ms": 30}
    assert meta["confidence"] == 0.5
    assert meta["supported_sentences"] == 1
    assert meta["total_sentences"] == 2
    assert meta["hallucination_flags"] == [{"sentence": "编造句。", "reason": "no_citation"}]
    assert meta["tool_results"][0]["elapsed_ms"] == 70
    assert meta["tool_results"][0]["ok"] is True
    # 旧字段语义不变
    assert meta["tool_calls"][0]["name"] == "search_bidding_knowledge"
    assert meta["done_mode"] == "agent_rag"


# ----------------------------------------------------------------------------
# 用例 7：旧后端（没有新增字段）不能把面板打崩——字段可缺省
# ----------------------------------------------------------------------------
def test_missing_audit_fields_are_tolerated():
    """collector 没收到新增字段时 meta 里是 None / 空表，不会 KeyError。"""
    from app.main import _EventCollector

    collector = _EventCollector()
    collector.feed({"type": "done", "mode": "llm"})

    meta = collector.meta("llm")
    assert meta["phase_timings"] is None
    assert meta["confidence"] is None
    assert meta["hallucination_flags"] == []
    assert meta["tool_results"] == []
