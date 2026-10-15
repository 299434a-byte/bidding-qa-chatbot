# ============================================================================
# 文件：tests/test_api_cache_behavior.py —— 问答缓存的「接口层」行为测试
#
# 【这个测试文件整体在测什么】
#   走真实 HTTP 入口（TestClient + /api/chat），把大模型/检索打掉后验证缓存四行为：
#     ① 命中 —— 同问题第二次不进问答主流程，直接回放缓存答案；
#     ② 过期（TTL）—— 超过 CACHE_TTL 后不再命中，重新算答案；
#     ③ 敏感问题 —— 看似含口令/身份证的问题不读也不写缓存；
#     ④ 版本变化 —— 知识库重建后同一问题不再命中（缓存跟知识库版本走）。
#   全部离线：检索与大模型用假实现顶掉，账号/缓存走 SQLite 临时库。
#
# 【为什么必须登录才有缓存】缓存全局共享：匿名请求不进缓存（is_cacheable 的一票否决），
#   所以本文件的用例都先注册拿令牌，再带着 Authorization 头提问。
# ============================================================================

from __future__ import annotations

# pytest.fixture / monkeypatch：管理测试前后状态与替换外部依赖。
import pytest
# TestClient：把 FastAPI 应用当普通对象发请求，不起真实服务器。
from fastapi.testclient import TestClient

# 被测对象：应用入口、缓存、版本指纹、配置。
from app import answer_cache, kb_version
from app.config import get_settings
from app.main import app
from app.retriever import RetrievalResult

# 一个结构完整的「假命中」：to_sources 需要 doc_id / table / label / primary_key / text。
_FAKE_HIT = {
    "doc_id": "bidding_notices:1:0",
    "table": "bidding_notices",
    "label": "招标公告",
    "primary_key": 1,
    "score": 0.9,
    "text": "本项目预算金额为 1590.0 万元，资金已落实。",
    "fields": {"project_name": "某某工程"},
}


class _FakeLLM:
    """假大模型：流式吐出一句固定答案（不连网）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages, settings):
        self.calls += 1
        yield "该项目预算为 1590 万元 [参考资料1]。"


@pytest.fixture()
def client_and_llm(monkeypatch):
    """TestClient + 打掉检索/大模型；每个用例前清空缓存与限流桶，互不干扰。"""
    llm = _FakeLLM()
    # 检索：固定返回一条假命中（不碰向量库 / BM25）。
    monkeypatch.setattr(
        "app.rag.retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[dict(_FAKE_HIT)], dense_count=1, sparse_count=0, bm25_ready=True
        ),
    )
    # 大模型：假流式输出（不连网）。
    monkeypatch.setattr("app.rag.stream_chat", llm)

    # 限流桶清零：上一个用例的连打会把额度用光。
    from app import ratelimit

    ratelimit._buckets.clear()

    # 缓存清零 + 钉住一个知识库版本，避免别的用例写下的文件影响本用例。
    s = get_settings()
    answer_cache.clear(s)
    kb_version.write_kb_version(s, points=100, added=0, updated=0, deleted=0)

    yield TestClient(app), llm


def _register(client: TestClient, username: str) -> str:
    """注册并拿令牌（注册接口直接返回令牌，省一次登录往返）。

    【为什么加随机后缀】账号库落在 pytest 沙箱目录里、跨轮次复用；
    固定用户名第二次跑就 409「用户已存在」。加个短随机后缀让每轮互不干扰。
    """
    import uuid

    # 用户名只允许中英文/数字/下划线（接口校验），随机后缀用下划线连接。
    name = f"{username}_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={"username": name, "password": "a-strong-pass-1"})
    assert resp.status_code == 200, resp.text
    token = resp.json().get("token")
    assert token, resp.json()
    return token


def _ask(client: TestClient, token: str, question: str):
    """带令牌提一个问，返回解析好的 SSE 事件列表。"""
    resp = client.post(
        "/api/chat",
        json={"question": question},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            import json

            events.append(json.loads(line[len("data: ") :]))
    return events


# ----------------------------------------------------------------------------
# 用例 1：同版本内命中 —— 第二次不进问答主流程，直接回放缓存
# ----------------------------------------------------------------------------
def test_cache_hit_skips_pipeline(client_and_llm) -> None:
    """第一问进主流程（有检索事件）；第二问命中缓存，主流程不再跑。

    【为什么不断言「假模型被调用几次」】调用计数是实现细节，防线失效时未必抓得住；
    这里锁的是外部可观察的事件流：主流程必有 retrieval 事件，缓存回放必没有。
    """
    client, _ = client_and_llm
    token = _register(client, "缓存用户甲")

    first = _ask(client, token, "市政道路招标有哪些？")
    # 主流程跑过：有检索事件、有正文。
    assert any(e["type"] == "retrieval" for e in first)
    assert any(e["type"] == "delta" for e in first)

    second = _ask(client, token, "市政道路招标有哪些？")
    # 命中缓存：给出 cache_hit 提示 + 与第一次相同的答案。
    assert any(e["type"] == "cache_hit" for e in second)
    assert any(e.get("text") == "该项目预算为 1590 万元 [参考资料1]。" for e in second if e["type"] == "delta")
    # 关键证据：没有检索事件 —— 问答主流程没有被再次触发。
    assert not any(e["type"] == "retrieval" for e in second)


# ----------------------------------------------------------------------------
# 用例 2：TTL 过期后不再命中
# ----------------------------------------------------------------------------
def test_expired_entry_is_not_used(client_and_llm) -> None:
    """把缓存行的 created_at 改到过去（超过 CACHE_TTL）→ 视同未命中，重新生成。"""
    import sqlite3

    client, _ = client_and_llm
    token = _register(client, "缓存用户乙")
    question = "城市道路工程的工期要求是什么？"

    first = _ask(client, token, question)
    assert any(e["type"] == "retrieval" for e in first)

    # 直接把这行的写入时间改早（模拟「放了太久」），TTL 判定自然过期。
    s = get_settings()
    with sqlite3.connect(str(s.cache_db_path)) as conn:
        conn.execute("UPDATE answer_cache SET created_at = '2000-01-01T00:00:00+00:00'")

    second = _ask(client, token, question)
    # 过期条目不再回放：没有 cache_hit，主流程重跑（有检索事件）。
    assert not any(e["type"] == "cache_hit" for e in second)
    assert any(e["type"] == "retrieval" for e in second)


# ----------------------------------------------------------------------------
# 用例 3：敏感问题不缓存
# ----------------------------------------------------------------------------
def test_sensitive_question_is_never_cached(client_and_llm) -> None:
    """问题里带「password」这类敏感词：两次都走主流程，缓存表里不留行。"""
    client, _ = client_and_llm
    token = _register(client, "缓存用户丙")
    question = "请帮我查一下管理员 password 的配置"

    first = _ask(client, token, question)
    second = _ask(client, token, question)

    # 敏感问题不读不写：两次都在主流程里生成（都有检索事件、都没有 cache_hit）。
    assert any(e["type"] == "retrieval" for e in first)
    assert any(e["type"] == "retrieval" for e in second)
    assert not any(e["type"] == "cache_hit" for e in first)
    assert not any(e["type"] == "cache_hit" for e in second)
    # 缓存表里没有任何行（哪怕手工查也查不到）。
    s = get_settings()
    rows = answer_cache.get_cached(question, s, mode="llm")
    assert rows is None


# ----------------------------------------------------------------------------
# 用例 4：知识库版本变化后，同一问题不再命中
# ----------------------------------------------------------------------------
def test_kb_version_change_invalidates_cache(client_and_llm) -> None:
    """重建知识库（版本指纹改变）后，同一问题的旧答案立即失效、重新生成。"""
    client, _ = client_and_llm
    token = _register(client, "缓存用户丁")
    question = "市政道路项目的招标方式有哪些？"

    first = _ask(client, token, question)
    assert any(e["type"] == "retrieval" for e in first)

    # 模拟重建知识库：写一次 kb_version.txt → 版本指纹改变。
    kb_version.write_kb_version(get_settings(), points=200, added=9, updated=3, deleted=1)

    second = _ask(client, token, question)
    # 版本变化 → 旧缓存不再被使用 → 主流程重跑（无 cache_hit、有检索事件）。
    assert not any(e["type"] == "cache_hit" for e in second)
    assert any(e["type"] == "retrieval" for e in second)


# ----------------------------------------------------------------------------
# 用例 5（缓存命中率修复）：带历史重复提问也要能命中 —— UI 多轮的常态路径
# ----------------------------------------------------------------------------
def test_cache_hit_even_when_question_repeated_with_history(client_and_llm, monkeypatch) -> None:
    """首问（无历史）写入缓存后，带历史把同一句话再问一遍应直接命中、不进 Agent。

    【修的是什么】旧行为「带历史一律不读不写」：前端第二问起必然带 history，
    于是同一会话里永远不命中（实测 cache_hit_rate 仅 2.4% 的主因之一）。
    修复后读写不对称：写入仍要求无历史（不缓存上下文相关答案），读放行。
    """
    client, _ = client_and_llm
    token = _register(client, "缓存用户戊")
    question = "城市桥梁工程的招标方式有哪些？"

    # Agent 通道的执行体换成「假事件流」：命中缓存时它整个不该被调用，
    # 行为上的证据就是响应里没有它产出的 retrieval 事件（回放流只有 cache_hit/sources/delta/done）。
    async def fake_agent_events(*args, **kwargs):
        yield {"type": "retrieval", "meta": {"final_count": 1}}
        yield {"type": "sources", "sources": [dict(_FAKE_HIT)]}
        yield {"type": "delta", "text": "答案正文"}
        yield {"type": "done", "mode": "agent_rag"}

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)

    # 首问：无历史 → 可写缓存，走一遍假 Agent（有它的 retrieval 事件与正文）。
    resp = client.post(
        "/api/agent/chat",
        json={"question": question},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    first = _parse_sse(resp.text)
    assert not any(e["type"] == "cache_hit" for e in first)
    assert any(e["type"] == "retrieval" for e in first)
    assert any(e.get("text") == "答案正文" for e in first if e["type"] == "delta")

    # 第二问：带历史（模拟前端多轮）同一问题 → 必须命中，假 Agent 不再被调用
    # （回放流里没有 retrieval 事件）。
    resp = client.post(
        "/api/agent/chat",
        json={
            "question": question,
            "history": [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"}],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    second = _parse_sse(resp.text)
    assert any(e["type"] == "cache_hit" for e in second)
    assert not any(e["type"] == "retrieval" for e in second)


# ----------------------------------------------------------------------------
# 用例 6（缓存命中率修复）：「允许联网但实际没联网」不再被误杀
# ----------------------------------------------------------------------------
def test_cache_hit_when_web_search_allowed_but_unused(client_and_llm, monkeypatch) -> None:
    """WEB_SEARCH_DEFAULT=true 的环境里，请求「允许联网」但答案没用联网结果 → 照常缓存并命中。

    【修的是什么】旧行为按「是否允许联网」判时效性，把所有允许联网的请求一律拒之缓存门外，
    即便 Agent 压根没调联网工具（实测第二问仍要 45.9s）。修复后只看「是否真的用了联网结果」。
    """
    client, _ = client_and_llm
    token = _register(client, "缓存用户己")
    question = "安置房项目的中标候选人公示在哪里看？"

    async def fake_agent_events(*args, **kwargs):
        # 注意：这条假事件流不含任何 web_search 事件 —— 模拟「允许联网但没用」。
        yield {"type": "retrieval", "meta": {"final_count": 1}}
        yield {"type": "sources", "sources": [dict(_FAKE_HIT)]}
        yield {"type": "delta", "text": "纯知识库答案"}
        yield {"type": "done", "mode": "agent_rag"}

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)

    payload = {"question": question, "web_search": True}
    headers = {"Authorization": f"Bearer {token}"}

    first = _parse_sse(client.post("/api/agent/chat", json=payload, headers=headers).text)
    assert not any(e["type"] == "cache_hit" for e in first)
    assert any(e["type"] == "retrieval" for e in first)

    second = _parse_sse(client.post("/api/agent/chat", json=payload, headers=headers).text)
    assert any(e["type"] == "cache_hit" for e in second)
    # 命中缓存：回放的是同一份答案，且假 Agent 没再被调用（流里没有 retrieval 事件）。
    assert any(e.get("text") == "纯知识库答案" for e in second if e["type"] == "delta")
    assert not any(e["type"] == "retrieval" for e in second)


def _parse_sse(text: str) -> list[dict]:
    """把 SSE 响应文本解析成事件字典列表（跳过 [DONE] 哨兵）。"""
    import json

    events = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[len("data: ") :]))
    return events
