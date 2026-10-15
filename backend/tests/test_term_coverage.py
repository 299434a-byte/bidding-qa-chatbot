# ============================================================================
# 文件：tests/test_term_coverage.py —— 覆盖率闸门（答非所问挡在生成之前）体检
#
# 【这个测试文件整体在测什么】
#   1) retriever.term_coverage 纯函数：问题实词在候选块上的最大命中比例；
#   2) rag.answer_events 的第二道判据：覆盖率低于 COVERAGE_MIN 时
#      不进入生成、返回固定白话、事件带 coverage/refused、metrics 计数；
#   3) 通过闸门的正常路径行为不变（照常进生成、计 coverage_pass）。
#
# 【为什么用真实反例】问「采购企业有哪些？」时检索回过字面相近、实则
#   答非所问的行业分析资料，模型顺着编了一整段——这正是本闸门要挡的场景。
# ============================================================================

from __future__ import annotations

from dataclasses import replace

from app import rag
from app.config import get_settings
from app.metrics import summary as metrics_summary
from app.retriever import RetrievalResult, term_coverage


def _gate_settings():
    """注入真实阈值的配置：假环境里 COVERAGE_MIN=0（闸门放行），这里显式打开。"""
    return replace(get_settings(), coverage_min=0.45)


def _hit(text: str, doc_id: str = "doc-1") -> dict:
    """造一条命中记录（term_coverage 只看 text，其余字段按检索产物补齐）。"""
    return {
        "doc_id": doc_id,
        "table": "procurement_notices",
        "label": "采购公告",
        "primary_key": doc_id,
        "text": text,
        "fields": {},
    }


# 知识库里真实存在的那类招标公告文本（资质问法的合理落点）。
_QUALIFICATION_CHUNK = (
    "投标单位应具备的资质要求：投标人须具备建筑工程施工总承包二级以上资质，"
    "并满足下列条件：具有独立法人资格，近三年内承担过类似工程业绩不少于三个，"
    "项目负责人具备一级注册建造师执业资格。"
)

# 当时答非所问的那批资料（盐化工下游行业分析）：字面与「采购企业」毫无交集。
_OFFTOPIC_CHUNKS = [
    _hit("盐化工下游行业分析：纯碱主要用于玻璃制造，烧碱广泛应用于肥皂与洗涤剂生产。", "salt-1"),
    _hit("磷化工需求跟踪：磷酸一铵开工率回升，下游复合肥工厂拿货情绪一般。", "salt-2"),
    _hit("氯碱市场周报：片碱价格窄幅整理，氧化铝行业需求平稳。", "salt-3"),
]


# ---------------------------------------------------------------------------
# 纯函数：覆盖率本身
# ---------------------------------------------------------------------------

def test_coverage_high_for_synonym_rewrite_of_same_topic():
    """同义改写的问题：块里词形基本都在，覆盖率应该高（过闸门）。"""
    original = "投标人需要具备什么资质要求？"
    rewritten = "参与投标需要什么资格与资质？"

    hits = [_hit(_QUALIFICATION_CHUNK)]

    # 两种问法指向同一资料，覆盖率都应过线——换个说法不该被误杀。
    assert term_coverage(original, hits) >= 0.45
    assert term_coverage(rewritten, hits) >= 0.45


def test_coverage_low_for_real_offtopic_counterexample():
    """真实反例「采购企业有哪些？」+ 字面相近但答非所问的资料：覆盖率低于阈值。"""
    question = "采购企业有哪些？"

    coverage = term_coverage(question, _OFFTOPIC_CHUNKS)

    # 「采购」「企业」两个实词一个都没出现在行业分析里 → 0.0，必然低于 0.45。
    assert coverage < 0.45


def test_coverage_is_max_over_hits_and_requires_content_words():
    """取所有候选里的最大值；单字与疑问虚词不计入实词。"""
    # 第一条一个实词都不中，第二条全中 → 取最大值而非平均。
    hits = [_hit("无关文本甲", "a"), _hit("采购企业信用信息公示系统公告", "b")]
    assert term_coverage("采购企业有哪些？", hits) == 1.0

    # 「有哪些」是虚词+单字，全被过滤后没有实词 → 0.0（而不是误判为满分）。
    assert term_coverage("有哪些？", hits) == 0.0


def test_coverage_edge_cases():
    """边界：空问题 / 全部停用词 / 空候选，都返回 0.0 而不是崩溃。"""
    hits = [_hit("采购与企业相关的正文")]

    assert term_coverage("", hits) == 0.0
    assert term_coverage("   ", hits) == 0.0
    assert term_coverage("什么哪些怎么为什么", hits) == 0.0
    assert term_coverage("采购企业有哪些？", []) == 0.0


# ---------------------------------------------------------------------------
# answer_events：闸门行为
# ---------------------------------------------------------------------------

def _fake_retrieval(hits: list[dict]) -> callable:
    """假 retrieve：返回给定 hits（跳过向量库与 BM25）。"""

    def _retrieve(question: str, **kwargs) -> RetrievalResult:
        return RetrievalResult(question=question, hits=list(hits), dense_count=len(hits))

    return _retrieve


def test_answer_events_refuses_when_coverage_below_threshold(monkeypatch) -> None:
    """低覆盖 → 不进生成：固定白话 + coverage/refused 事件 + done(coverage_refused)。"""
    question = "采购企业有哪些？"

    monkeypatch.setattr(rag, "retrieve", _fake_retrieval(_OFFTOPIC_CHUNKS))
    monkeypatch.setattr(rag, "rerank_hits", lambda q, hits, s, top_k: (hits, {}))

    def _fail_if_generates(*args, **kwargs):  # pragma: no cover - 触发即测试失败
        raise AssertionError("覆盖率不足时不允许进入生成")

    monkeypatch.setattr(rag, "stream_chat", _fail_if_generates)

    before = metrics_summary()["coverage_refused"]
    events = list(rag.answer_events(question, settings=_gate_settings()))
    after = metrics_summary()["coverage_refused"]

    types = [ev["type"] for ev in events]
    # 生成类事件一个都不该有（没有 llm 通道的正文增量）。
    assert "coverage_check" in types
    assert types[-2:] == ["delta", "done"]

    cov_event = next(ev for ev in events if ev["type"] == "coverage_check")
    assert cov_event["refused"] is True
    assert cov_event["coverage"] < cov_event["threshold"]

    done = events[-1]
    assert done["mode"] == "coverage_refused"

    # 拒答文案是固定白话，且带三条排查建议。
    refusal = next(ev for ev in events if ev["type"] == "delta")["text"]
    assert "暂无明确依据" in refusal
    assert "换更具体的关键词" in refusal
    assert "联网搜索" in refusal
    assert "图谱" in refusal

    # 检索事件照常下发（前端仍能看到命中了什么，便于排查）。
    retrieval = next(ev for ev in events if ev["type"] == "retrieval")
    assert retrieval["meta"]["coverage"] < 0.45

    # metrics 计一次拒答。
    assert after == before + 1


def test_answer_events_pass_gate_and_generate_normally(monkeypatch) -> None:
    """高覆盖 → 正常进生成：行为与改动前一致，且计 coverage_pass。"""
    question = "投标人需要具备什么资质要求？"
    hits = [_hit(_QUALIFICATION_CHUNK)]

    monkeypatch.setattr(rag, "retrieve", _fake_retrieval(hits))
    monkeypatch.setattr(rag, "rerank_hits", lambda q, hits, s, top_k: (hits, {}))
    monkeypatch.setattr(rag, "stream_chat", lambda *a, **k: iter(["这是回答"]))

    before = metrics_summary()["coverage_pass"]
    events = list(rag.answer_events(question, settings=_gate_settings()))
    after = metrics_summary()["coverage_pass"]

    types = [ev["type"] for ev in events]
    assert "coverage_check" not in types
    assert "delta" in types and types[-1] == "done"
    assert events[-1]["mode"] == "llm"

    retrieval = next(ev for ev in events if ev["type"] == "retrieval")
    assert retrieval["meta"]["coverage"] >= 0.45

    assert after == before + 1
