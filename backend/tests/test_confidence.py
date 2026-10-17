# ============================================================================
# 文件：tests/test_confidence.py —— 逐句可信度评分（score_answer）与事件透传
#
# 【这个测试文件整体在测什么】
#   背景：citation.verify_citations 已经能给出「引用越界 / 数值无依据」的问题清单，
#   但前端与指标侧还需要一个可聚合的答案级结论：这句话到底有没有依据？
#   本文件锁定 citation.score_answer 的判定口径与边界：
#     ① 全句都有引用且数值逐字命中 → confidence = 1.0；
#     ② 有一句没引用 → confidence < 1，且该句出现在 hallucination_flags（reason=no_citation）；
#     ③ 有引用但数值在来源里找不到 → 该句被标 number_no_source；
#     ④ 空答案 → total_sentences=0、confidence=0.0，绝不除零报错；
#     ⑤ rag 流式链路把 confidence / hallucination_flags 放进 citation_check 事件。
#   全部离线：假 sources + 纯函数 / monkeypatch，不连大模型、不连库。
# ============================================================================

from __future__ import annotations

# 被测对象：逐句可信度评分 + 它依赖的引用校验。
from app.citation import score_answer, verify_citations

# 一条结构完整的假来源（与真实 to_sources 的关键字段一致；含金额供数值比对）。
SOURCES = [
    {"text": "本项目预算金额为 1590.0 万元，资金已落实。", "fields": {"project_name": "某某工程"}},
    {"text": "招标人为某某单位，采用公开招标方式。", "fields": {}},
]


# ----------------------------------------------------------------------------
# 用例 1：全句都有引用且数值命中 → confidence = 1.0
# ----------------------------------------------------------------------------
def test_all_supported_sentences_give_full_confidence():
    """两句都带真实引用、金额逐字命中：confidence 必须是 1.0，且没有幻觉标记。"""
    # 【设计意图】可信度分数的分子分母口径：「有支撑句数 ÷ 总句数」。
    #   「1590 万元」与来源里的「1590.0 万元」是同一金额的不同写法，必须算命中（防误杀）。
    answer = "本项目预算 1590 万元 [参考资料1]。采用公开招标方式 [参考资料2]。"

    verify = verify_citations(answer, SOURCES)
    score = score_answer(answer, SOURCES, verify)

    assert score["confidence"] == 1.0
    assert score["total_sentences"] == 2
    assert score["supported_sentences"] == 2
    assert score["unsupported_sentences"] == 0
    assert score["hallucination_flags"] == []


# ----------------------------------------------------------------------------
# 用例 2：有一句没引用 → confidence < 1，该句进 hallucination_flags
# ----------------------------------------------------------------------------
def test_sentence_without_citation_lowers_confidence_and_is_flagged():
    """第二句没有任何引用编号：confidence 降到 0.5，flags 里给出该句与 no_citation 原因。"""
    answer = "本项目预算 1590 万元 [参考资料1]。该项目位于城东区。"

    verify = verify_citations(answer, SOURCES)
    score = score_answer(answer, SOURCES, verify)

    assert score["confidence"] < 1
    assert score["confidence"] == 0.5
    assert score["total_sentences"] == 2
    assert score["supported_sentences"] == 1
    assert score["unsupported_sentences"] == 1
    assert len(score["hallucination_flags"]) == 1
    flag = score["hallucination_flags"][0]
    # 句子原文要能对上（断句后不带句末标点），原因必须是缺引用。
    assert flag["sentence"] == "该项目位于城东区"
    assert flag["reason"] == "no_citation"


# ----------------------------------------------------------------------------
# 用例 3：数值在来源里找不到 → 该句被标 number_no_source
# ----------------------------------------------------------------------------
def test_number_not_found_in_source_flags_number_no_source():
    """句里有真实引用，但金额在来源中不存在：该句不计为有支撑，原因是数值无依据。"""
    sources = [{"text": "招标人为某某单位，采用公开招标方式。", "fields": {}}]
    answer = "本项目预算 500 万元 [参考资料1]。"

    verify = verify_citations(answer, sources)
    score = score_answer(answer, sources, verify)

    assert score["confidence"] == 0.0
    assert score["total_sentences"] == 1
    assert score["supported_sentences"] == 0
    assert len(score["hallucination_flags"]) == 1
    flag = score["hallucination_flags"][0]
    assert flag["reason"] == "number_no_source"
    assert "500" in flag["sentence"]


# ----------------------------------------------------------------------------
# 用例 4：空答案 → total_sentences=0、confidence=0，且不除零报错
# ----------------------------------------------------------------------------
def test_empty_answer_returns_zero_confidence_without_division_error():
    """空 / 纯空白 / None 回答：total_sentences=0、confidence=0.0，函数绝不抛 ZeroDivisionError。"""
    for answer in ("", "   ", None):
        verify = verify_citations(answer or "", SOURCES)
        score = score_answer(answer or "", SOURCES, verify)

        assert score["total_sentences"] == 0
        assert score["confidence"] == 0.0
        assert score["supported_sentences"] == 0
        assert score["unsupported_sentences"] == 0
        assert score["hallucination_flags"] == []


# ----------------------------------------------------------------------------
# 用例 5：rag 流式链路把可信度带进 citation_check 事件（通过路径也发）
# ----------------------------------------------------------------------------
def test_rag_stream_emits_confidence_in_citation_check(monkeypatch):
    """校验通过时 citation_check 仍会发出，并携带 confidence=1.0 与空幻觉清单。"""
    from app import rag
    from app.config import get_settings
    from app.retriever import RetrievalResult

    fake_hit = {
        "doc_id": "bidding_notices:1:0",
        "table": "bidding_notices",
        "label": "招标公告",
        "primary_key": 1,
        "score": 0.9,
        "text": "本项目预算金额为 1590.0 万元，资金已落实。",
        "fields": {"project_name": "某某工程"},
    }
    monkeypatch.setattr(
        rag,
        "retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[dict(fake_hit)], dense_count=1, sparse_count=0, bm25_ready=True
        ),
    )
    monkeypatch.setattr(
        rag, "stream_chat", lambda *args, **kwargs: iter(["该项目预算为 1590 万元 [参考资料1]。"])
    )

    events = list(rag.answer_events("项目预算多少？", settings=get_settings()))
    checks = [e for e in events if e.get("type") == "citation_check"]

    assert len(checks) == 1
    assert checks[0]["ok"] is True
    assert checks[0]["issues"] == []
    # 既有字段语义不变 + 新增可信度字段到位。
    assert checks[0]["confidence"] == 1.0
    assert checks[0]["total_sentences"] == 1
    assert checks[0]["supported_sentences"] == 1
    assert checks[0]["hallucination_flags"] == []
