# ============================================================================
# 文件：tests/test_followup_reuse.py —— 追问复用（跳过检索，复用上一问证据）行为测试
#
# 【这个测试文件整体在测什么】
#   ① is_followup 的规则版判定：短 + 指代/延续词 + 无新实体词才算追问，拿不准一律 False；
#   ② get_last_evidence：按会话回捞最近一次「带来源」的回答；
#   ③ 接口层：追问命中复用（事件标 reused=true、Agent 主链路不再被调用）；
#   ④ 失效校验：kb_version 变化 / 来源块已不在库里 → 放弃复用、走完整链路；
#   ⑤ 非追问（长问题 / 带新实体）永远走完整链路。
#   全部离线：Agent 与大模型用假实现顶掉，缓存/指标走 SQLite 沙箱。
# ============================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import answer_cache, kb_version, metrics as metrics_store
from app.config import get_settings
from app.main import app
from app.vector_store import VectorStore

_FAKE_HIT = {
    "doc_id": "bidding_notices:1:0",
    "table": "bidding_notices",
    "label": "招标公告",
    "primary_key": 1,
    "score": 0.9,
    "text": "本项目预算金额为 1590.0 万元。",
    "fields": {"project_name": "某某工程"},
}

_FIRST_QUESTION = "市政道路改造项目的招标公告有哪些？"


class _FakeAgent:
    """假 Agent 事件流：记录被调用过的问题，供断言「完整链路是否被触发」。"""

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def __call__(self, question: str, **kwargs):
        self.questions.append(question)
        yield {"type": "retrieval", "meta": {"final_count": 1}}
        yield {"type": "sources", "sources": [dict(_FAKE_HIT)]}
        yield {"type": "delta", "text": "完整链路答案"}
        yield {"type": "done", "mode": "agent_rag"}


def _fake_stream_chat(messages, settings, **kwargs):
    """假大模型：追问复用链路的生成器（不连网）。"""
    yield "复用证据答案 [参考资料1]。"


@pytest.fixture()
def client_and_agent(monkeypatch):
    """TestClient + 打掉 Agent / 复用链路大模型；每个用例前清缓存、钉版本、清限流。"""
    agent = _FakeAgent()
    monkeypatch.setattr("app.main.agent_events", agent)
    monkeypatch.setattr("app.rag.stream_chat", _fake_stream_chat)
    # 假命中不在真实向量库里：把「来源块存在性校验」打成「全部还在」，
    # 失效场景由各用例自行改成「已缺失」。
    monkeypatch.setattr(VectorStore, "missing_doc_ids", lambda self, doc_ids, batch_size=256: set())

    from app import ratelimit

    ratelimit._buckets.clear()
    s = get_settings()
    answer_cache.clear(s)
    kb_version.write_kb_version(s, points=100, added=0, updated=0, deleted=0)

    yield TestClient(app), agent


def _register(client: TestClient) -> str:
    """注册并拿令牌（用户名带随机后缀，沙箱库跨轮次复用也不冲突）。"""
    import uuid

    name = f"追问用户_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={"username": name, "password": "a-strong-pass-1"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _ask(client: TestClient, token: str, payload: dict) -> tuple[list[dict], str | None]:
    """带令牌提问，返回（解析好的 SSE 事件列表, conversation_id）。"""
    import json

    resp = client.post(
        "/api/agent/chat",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    conversation_id = next((e.get("id") for e in events if e.get("type") == "conversation"), None)
    return events, conversation_id


# ----------------------------------------------------------------------------
# 用例 1：is_followup 规则判定 —— 三条必须同时成立，拿不准一律 False
# ----------------------------------------------------------------------------
def test_is_followup_rules() -> None:
    # 追问：短 + 指代/延续词 + 无新实体
    assert answer_cache.is_followup("那第二条呢")
    assert answer_cache.is_followup("它的金额是多少")
    assert answer_cache.is_followup("刚才那个项目怎么样")
    # 不是追问：没有指代/延续词
    assert not answer_cache.is_followup("市政道路改造项目的招标公告有哪些？")
    # 不是追问：带新实体（项目编号 / 日期 / 公司名 / 法规名）
    assert not answer_cache.is_followup("ZB2024-001 中标了吗")
    assert not answer_cache.is_followup("那2024年的招标公告有哪些呢")
    assert not answer_cache.is_followup("它属于哪家公司")
    assert not answer_cache.is_followup("这个办法还有效吗")
    # 不是追问：超过 15 字（哪怕满嘴指代词）
    assert not answer_cache.is_followup("那那个上面刚才说的那个项目的事情怎么样了呢")
    # 空问题
    assert not answer_cache.is_followup("")


# ----------------------------------------------------------------------------
# 用例 2：get_last_evidence —— 按会话回捞最近一次带来源的回答
# ----------------------------------------------------------------------------
def test_get_last_evidence_roundtrip() -> None:
    s = get_settings()
    answer_cache.clear(s)
    assert answer_cache.get_last_evidence("不存在的会话", s) is None
    assert answer_cache.get_last_evidence("", s) is None

    ok = answer_cache.set_cached(
        "预算多少？", "1590 万元", [dict(_FAKE_HIT)], "react", s, conversation_id="conv-1"
    )
    assert ok
    evidence = answer_cache.get_last_evidence("conv-1", s)
    assert evidence is not None
    assert evidence["sources"] == [dict(_FAKE_HIT)]
    assert evidence["kb_version"] == kb_version.kb_version(s)
    # 其他会话看不到这条证据
    assert answer_cache.get_last_evidence("conv-2", s) is None


# ----------------------------------------------------------------------------
# 用例 3：接口层 —— 追问命中复用：标 reused=true，完整链路（Agent）不再被调用
# ----------------------------------------------------------------------------
def test_followup_reuses_last_evidence(client_and_agent) -> None:
    client, agent = client_and_agent
    token = _register(client)

    # 首问（长问题、非追问）：走完整链路，写入带 conversation_id 的证据行。
    first, conversation_id = _ask(client, token, {"question": _FIRST_QUESTION})
    assert conversation_id
    assert any(e["type"] == "retrieval" for e in first)
    assert agent.questions == [_FIRST_QUESTION]

    # 追问：同一会话 + 短指代问 → 复用上一问证据。
    second, _ = _ask(
        client,
        token,
        {
            "question": "那第二条呢",
            "conversation_id": conversation_id,
            "history": [
                {"role": "user", "content": _FIRST_QUESTION},
                {"role": "assistant", "content": "完整链路答案"},
            ],
        },
    )
    # 完整链路没有被再次触发：Agent 仍只见过第一问。
    assert agent.questions == [_FIRST_QUESTION]
    # 复用链路的事件证据：retrieval.meta.reused=true + done.reused=true + 复用生成的答案。
    retrieval = next(e for e in second if e["type"] == "retrieval")
    assert retrieval["meta"].get("reused") is True
    done = next(e for e in second if e["type"] == "done")
    assert done.get("reused") is True
    assert any("复用证据答案" in e.get("text", "") for e in second if e["type"] == "delta")
    # 来源照常下发（证据面板可展示）。
    sources = next(e for e in second if e["type"] == "sources")
    assert sources["sources"] == [dict(_FAKE_HIT)]
    # metrics 里 followup_reused 计数增加。
    assert metrics_store.summary(get_settings())["followup_reused"] >= 1


# ----------------------------------------------------------------------------
# 用例 4：kb_version 变化后拒绝复用 —— 旧证据随知识库重建失效
# ----------------------------------------------------------------------------
def test_kb_version_change_rejects_reuse(client_and_agent) -> None:
    client, agent = client_and_agent
    token = _register(client)

    _, conversation_id = _ask(client, token, {"question": _FIRST_QUESTION})
    assert conversation_id

    # 模拟重建知识库：版本指纹变化 → 上一问证据判定失效。
    kb_version.write_kb_version(get_settings(), points=200, added=9, updated=3, deleted=1)

    second, _ = _ask(
        client,
        token,
        {"question": "那第二条呢", "conversation_id": conversation_id},
    )
    # 放弃复用：完整链路重新检索（Agent 被再次调用，事件里没有 reused 标注）。
    assert agent.questions == [_FIRST_QUESTION, "那第二条呢"]
    retrieval = next(e for e in second if e["type"] == "retrieval")
    assert not retrieval["meta"].get("reused")
    assert not any(e.get("reused") for e in second if e["type"] == "done")


# ----------------------------------------------------------------------------
# 用例 5：来源块已不在库里 → 拒绝复用
# ----------------------------------------------------------------------------
def test_missing_source_chunk_rejects_reuse(client_and_agent, monkeypatch) -> None:
    client, agent = client_and_agent
    token = _register(client)

    _, conversation_id = _ask(client, token, {"question": _FIRST_QUESTION})

    # 来源块被删（如增量建库清掉了这条）：校验发现缺失 → 放弃复用。
    monkeypatch.setattr(
        VectorStore, "missing_doc_ids", lambda self, doc_ids, batch_size=256: {"bidding_notices:1:0"}
    )

    second, _ = _ask(client, token, {"question": "那第二条呢", "conversation_id": conversation_id})
    assert agent.questions == [_FIRST_QUESTION, "那第二条呢"]
    retrieval = next(e for e in second if e["type"] == "retrieval")
    assert not retrieval["meta"].get("reused")


# ----------------------------------------------------------------------------
# 用例 6：非追问永远走完整链路
# ----------------------------------------------------------------------------
def test_non_followup_goes_full_pipeline(client_and_agent) -> None:
    client, agent = client_and_agent
    token = _register(client)

    _, conversation_id = _ask(client, token, {"question": _FIRST_QUESTION})

    second_question = "桥梁工程的监理单位招标有什么资质要求？"
    second, _ = _ask(client, token, {"question": second_question, "conversation_id": conversation_id})
    # 哪怕会话里有证据、问题也短不了多少 —— 没有指代/延续词就不是追问，完整链路照跑。
    assert agent.questions == [_FIRST_QUESTION, second_question]
    retrieval = next(e for e in second if e["type"] == "retrieval")
    assert not retrieval["meta"].get("reused")
    assert any(e.get("text") == "完整链路答案" for e in second if e["type"] == "delta")
