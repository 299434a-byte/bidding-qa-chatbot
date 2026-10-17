# ============================================================================
# 文件：tests/test_clarify.py —— 对话澄清（规则优先、判不准退回原链路）
#
# 【这个测试文件整体在测什么】
#   问题太短没实体 / 指代词没上文 / 统计口径缺限定时，后端**不检索、不调模型**，
#   直接发 clarify 事件（澄清提问 + 2~4 个可点选项）后 done。本文件锁住：
#     · needs_clarification 纯函数：四类歧义问题 → needed=True 且 options 非空；
#     · 正常问题 / 带上文的指代词 → needed=False（退回原链路）；
#     · /api/agent/chat 通路：澄清时绝不触发 agent_events（monkeypatch 离线验证），
#       事件顺序 conversation → clarify → done，且 metrics 记 clarify_count。
#   全部离线：假 Agent 事件流，不连网不连大模型。
# ============================================================================

from __future__ import annotations

# json：解析 /api/agent/chat 的 SSE data 行。
import json

from fastapi.testclient import TestClient

from app.clarify import needs_clarification


# ----------------------------------------------------------------------------
# 用例 1：四类歧义问题都返回 needed=True 且 options 非空
# ----------------------------------------------------------------------------
def test_four_ambiguity_classes_need_clarification():
    """短且无实体 / 指代词无上文 / 统计缺限定 / 长一点的指代——都要澄清。"""
    cases = [
        "空调的采购情况",  # 过短（≤8 字）且不含任何实体（验收用例）
        "这个项目预算多少",  # 指代词「这个」但没有上文
        "有多少个",  # 统计口径但没说数什么、数哪段时间
        "那个项目的进展怎么样",  # 指代词「那个」没有上文（稍长，指代规则独立生效）
    ]
    for question in cases:
        result = needs_clarification(question, has_history=False)
        assert result["needed"] is True, f"应澄清：{question}"
        assert result["question"], f"澄清提问不能为空：{question}"
        options = result["options"]
        assert isinstance(options, list) and 2 <= len(options) <= 4, f"选项需 2~4 个：{question}"
        assert all(isinstance(opt, str) and opt.strip() for opt in options), f"选项需为非空文本：{question}"


# ----------------------------------------------------------------------------
# 用例 2：正常问题不打断（退回原链路）
# ----------------------------------------------------------------------------
def test_normal_question_not_clarified():
    """「最近有哪些市政道路类的招标公告」有主题有限定，直接答。"""
    result = needs_clarification("最近有哪些市政道路类的招标公告", has_history=False)
    assert result["needed"] is False


# ----------------------------------------------------------------------------
# 用例 3：带上文时的指代词不触发澄清
# ----------------------------------------------------------------------------
def test_demonstrative_with_history_not_clarified():
    """有上文时「这个/那个」可结合上文理解，不该打断提问。"""
    for question in ("这个项目预算多少", "那个项目的进展怎么样"):
        result = needs_clarification(question, has_history=True)
        assert result["needed"] is False, f"带上文不应澄清：{question}"


# ----------------------------------------------------------------------------
# 用例 4：短且含模糊名词、但带了实体的问题不打断（有编号/年份/金额/企业名就够清楚）
# ----------------------------------------------------------------------------
def test_short_question_with_entity_not_clarified():
    """模糊名词命中时，只要带上实体就不澄清。"""
    for question in (
        "2024AB情况",  # 项目编号
        "2024年进展如何",  # 年份
        "1590万元的情况",  # 金额
        "中建三局情况",  # 企业名
    ):
        result = needs_clarification(question, has_history=False)
        assert result["needed"] is False, f"带实体不应澄清：{question}"


# ----------------------------------------------------------------------------
# 用例 5：/api/agent/chat 澄清时发 clarify 事件后 done，且不触发 Agent（离线）
# ----------------------------------------------------------------------------
def test_agent_chat_emits_clarify_without_agent(monkeypatch):
    """澄清轮：conversation → clarify → done，agent_events 绝不能被调用。"""
    from app.main import app

    async def fake_agent_events(*args, **kwargs):
        raise AssertionError("澄清时不得调用 Agent（不应发散检索/调模型）")
        yield  # pragma: no cover

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)

    bumped: list[str] = []
    monkeypatch.setattr("app.main.metrics_store.bump", lambda name, **kw: bumped.append(name) or True)

    resp = TestClient(app).post("/api/agent/chat", json={"question": "空调的采购情况"})
    assert resp.status_code == 200, resp.text
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]

    # 顺序：conversation → clarify → done
    assert [e["type"] for e in events] == ["conversation", "clarify", "done"]
    clarify = events[1]
    assert clarify["question"]
    assert 2 <= len(clarify["options"]) <= 4
    assert events[2]["mode"] == "clarify"
    # metrics 记一次 clarify_count
    assert bumped == ["clarify_count"]


# ----------------------------------------------------------------------------
# 用例 6：正常问题不发 clarify 事件（Agent 通路照旧）
# ----------------------------------------------------------------------------
def test_agent_chat_normal_question_no_clarify(monkeypatch):
    """正常问题走原链路：不发 clarify，agent_events 照常调用。"""
    from app.main import app

    async def fake_agent_events(*args, **kwargs):
        yield {"type": "done", "mode": "agent_rag"}

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)

    resp = TestClient(app).post(
        "/api/agent/chat", json={"question": "最近有哪些市政道路类的招标公告"}
    )
    assert resp.status_code == 200, resp.text
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert all(e["type"] != "clarify" for e in events)
    assert events[-1]["type"] == "done"
