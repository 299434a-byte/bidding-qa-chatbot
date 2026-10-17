# ============================================================================
# 文件：tests/test_agent_citation.py —— 验证「Agent 通路」也接上了引用校验
#
# 【这个测试文件整体在测什么】
#   背景：引用校验器（citation.py）最初只接进了 rag.py，但前端实际走的是
#   /api/agent/chat → agent.agent_events，导致演示路径上校验器完全不生效。
#   本文件验证 agent.py 里抽出的辅助函数 _citation_check：
#     ① react 通路的「累计正文」会跑校验；
#     ② 校验失败时产出 citation_check + revision 两条事件；
#     ③ 校验通过时产出一条携带逐句可信度（confidence 等新增字段）的 citation_check，
#        正文一字不差、没有修订稿；
#     ④ 无来源 / 校验器异常时，不产出任何额外事件（行为一字不差）。
#   测试全部用「假 sources」直接调这个纯函数，**不连大模型、不连数据库**。
# ============================================================================

from __future__ import annotations

# 【这几行在导入什么】从 agent.py 抽出、专门为可测试性拆出来的辅助函数与状态构造器。
from app.agent import _citation_check, _new_turn_state


# 【这行在干什么】准备一份假的来源列表，供各用例复用（与真实 to_sources 结构一致的关键字段）。
FAKE_SOURCES = [
    {"text": "本项目预算金额为 1590.0 万元，资金已落实。", "fields": {"project_name": "某某工程"}},
    {"text": "招标人为某某单位，采用公开招标方式。", "fields": {}},
]


# ----------------------------------------------------------------------------
# 用例 1：累计正文会跑校验；校验失败时产出 revision + citation_check
# ----------------------------------------------------------------------------
def test_agent_citation_failure_emits_revision_and_check():
    """失败时必须先发 citation_check、再发 channel=revision 的修订稿。"""
    # 【设计意图】回答引用了不存在的第 9 条来源——这是「编造出处」，必须被校验出来，
    #   并产出一条修订稿让前端覆盖展示。
    answer = "该项目预算为 50 万元 [参考资料9]。"

    events, revised = _citation_check(answer, FAKE_SOURCES)

    # 恰好两条事件：先校验结果、后修订正文。
    assert len(events) == 2
    assert events[0]["type"] == "citation_check"
    assert events[0]["ok"] is False
    assert events[0]["issues"]  # 必须有具体问题清单，供前端提示
    # 修订正文走独立通道，并带 reason="citation" 以便与深度模式的修订稿区分。
    assert events[1]["type"] == "delta"
    assert events[1]["channel"] == "revision"
    assert events[1]["reason"] == "citation"
    # 越界的 [参考资料9] 必须从修订稿里被删掉。
    assert "[参考资料9]" not in events[1]["text"]
    # 返回的「已校验文本」应与事件里发出的修订正文完全一致。
    assert revised == events[1]["text"]


# ----------------------------------------------------------------------------
# 用例 2：校验通过时，产出一条携带逐句可信度的 citation_check，正文一字不差
# ----------------------------------------------------------------------------
def test_agent_citation_pass_emits_check_with_confidence_and_is_byte_identical():
    """校验通过时发一条 citation_check（ok=True + confidence 等新增字段），不发修订稿。"""
    # 【设计意图】契约更新：citation_check 校验通过时也要发——它现在同时承载
    #   逐句可信度（confidence / hallucination_flags），前端与指标侧需要看到
    #   「这句有依据」的结论，而不只是出问题时的清单。
    #   既有字段语义不变：ok=True、issues 为空；「1590 万元」与「1590.0 万元」
    #   是同一金额的不同写法，必须判为通过（防误杀）；正文一字不差、没有修订稿。
    answer = "该项目的预算金额为 1590 万元 [参考资料1]。"

    events, revised = _citation_check(answer, FAKE_SOURCES)

    assert len(events) == 1
    check = events[0]
    assert check["type"] == "citation_check"
    assert check["ok"] is True
    assert check["issues"] == []
    assert check["confidence"] == 1.0
    assert check["total_sentences"] == 1
    assert check["supported_sentences"] == 1
    assert check["hallucination_flags"] == []
    assert revised == answer  # 逐字节一致


# ----------------------------------------------------------------------------
# 用例 3：本轮没有来源时，跳过校验、不产出事件
# ----------------------------------------------------------------------------
def test_agent_citation_no_sources_skips():
    """没有来源就没有比对依据，应直接放行、不发任何事件。"""
    # 【设计意图】闲聊类回答（Agent 未调检索工具）根本没有 sources，
    #   此时不该做校验，否则会把正常回答误判成有问题。
    answer = "你好，我是招投标智能问答助手。"

    events, revised = _citation_check(answer, [])

    assert events == []
    assert revised == answer


# ----------------------------------------------------------------------------
# 用例 4：react 通路的轮次状态里有「累计正文」缓冲
# ----------------------------------------------------------------------------
def test_turn_state_has_answer_parts_buffer():
    """_new_turn_state 必须带 answer_parts（供 _stream_react_turn 累积正文）。"""
    # 【设计意图】校验要跑在「累计正文」上，前提是轮次状态里有这么个缓冲区。
    #   这里直接断言它存在且初始为空。
    turn = _new_turn_state()

    assert turn["answer_parts"] == []