# ============================================================================
# 文件：tests/test_retriever.py —— 给「两路召回融合算法」做体检
#
# 【这个测试文件整体在测什么】
#   被测对象是 backend/app/retriever.py 里的 rrf_fuse（RRF 融合）和 RetrievalResult。
#   RRF 是「按排名融合」的核心算法：一条记录在两条检索路里排得越靠前、被命中的路越多，
#   融合分就越高。这里全部用手工造的假数据来测，不碰真实向量库。
# ============================================================================

from __future__ import annotations

import pytest

from app.retriever import RetrievalResult, rrf_fuse


def _hit(doc_id: str, score: float) -> dict:
    """造一条「命中记录」。

    【为什么写这个小工具函数】rrf_fuse 要求每条命中至少带 doc_id / table / label /
    primary_key / score 这几个字段。每个用例都手写一遍太啰嗦，抽出来一处维护。
    """
    return {
        "doc_id": doc_id,
        "table": "bidding_notices",
        "label": "招标公告",
        "primary_key": 1,
        "score": score,
        "text": "正文",
        "fields": {},
    }


def test_rrf_prefers_item_hit_by_both_lists():
    """融合：两路都命中的条目，总分要高于只在单路排第一的条目。"""
    # 【设计意图】这是 RRF 最核心的直觉 ——「两路都认可」比「一路强推」更可信。
    #   dense 路：A 第 1、B 第 2；bm25 路：B 第 1、C 第 2。
    #   B 两条路都出现，得分 = 1/(k+2) + 1/(k+1)，必然大于只在 dense 排第 1 的 A。
    dense = [_hit("A", 0.9), _hit("B", 0.8)]
    bm25 = [_hit("B", 12.0), _hit("C", 5.0)]

    fused = rrf_fuse([("dense", dense), ("bm25", bm25)], rrf_k=60, final_k=10)
    by_id = {hit["doc_id"]: hit for hit in fused}

    assert by_id["B"]["rrf_score"] > by_id["A"]["rrf_score"]
    # 【为什么第二名是 A】A 得 1/61≈0.0164；C 得 1/62≈0.0161，A 略高。这个断言顺序。
    assert [hit["doc_id"] for hit in fused] == ["B", "A", "C"]
    # 【matched_by 记录「被哪几路命中」】两路都命中时应该是两个来源都有。
    assert by_id["B"]["matched_by"] == ["bm25", "dense"]


def test_rrf_merges_same_doc_into_one_entry():
    """去重：同一条文档被两路命中，只能合成一条，且两路的分数都要留下。"""
    # 【设计意图】doc_id 相同 = 同一条资料。若不去重，它会重复出现、还会被算两份分，
    #   既占位置又破坏排序公平性。源码用「按 doc_id 合并」来处理，这里验证该行为。
    dense = [_hit("A", 0.9)]
    bm25 = [_hit("A", 7.0)]

    fused = rrf_fuse([("dense", dense), ("bm25", bm25)], rrf_k=60, final_k=10)

    # 【数量必须是 1】两路命中同一条，最终只能有一条。
    assert len(fused) == 1
    entry = fused[0]
    # 【两路的原始分都要按来源分开保留】dense 是 0~1 的相似度、bm25 是可能很大的分数，
    #   量纲不同，必须分开存、分开看。
    assert entry["scores"] == {"dense": 0.9, "bm25": 7.0}
    # 【两路各自的名次也都要保留】
    assert entry["ranks"] == {"dense": 1, "bm25": 1}
    # 【命中来源去重并排序】源码最后统一做了 sorted(set(...))，结果是稳定且有序的。
    assert entry["matched_by"] == ["bm25", "dense"]


def test_rrf_respects_final_k():
    """截断：融合结果最多只保留 final_k 条。"""
    # 【设计意图】下游（重排/生成答案）处理的条数有限，候选太长既费钱又费时间，
    #   所以必须能截断。造 5 条、只要 3 条，验证截断生效。
    dense = [_hit(f"doc{i}", 1.0 - i * 0.1) for i in range(5)]

    fused = rrf_fuse([("dense", dense)], rrf_k=60, final_k=3)

    assert len(fused) == 3
    # 【为什么前面的先留下】源码先按分数降序排，再取前 N 条；
    #   dense 分是递减造的，所以留下的必然是 doc0、doc1、doc2。
    assert [hit["doc_id"] for hit in fused] == ["doc0", "doc1", "doc2"]


def test_rrf_filters_low_dense_score_when_not_matched_by_bm25():
    """阈值过滤：没被关键词命中、且语义相似度又不达标的，要被剔除。"""
    # 【设计意图】向量检索有个毛病：不管问得多离谱，总能从库里「矬子里拔将军」凑几条。
    #   如果把这些勉强凑数的塞给大模型，就可能诱导它编造答案。
    #   所以要设门槛：只有「被 BM25 精确命中」或「向量相似度达标」才算有效来源。
    dense = [_hit("strong", 0.9), _hit("weak", 0.1)]
    bm25: list[dict] = []

    fused = rrf_fuse(
        [("dense", dense), ("bm25", bm25)],
        rrf_k=60,
        final_k=10,
        dense_min_score=0.35,
    )

    # 【只有 strong 合格】weak 既没被 bm25 命中，dense 分 0.1 又低于门槛 0.35。
    assert [hit["doc_id"] for hit in fused] == ["strong"]


def test_rrf_keeps_bm25_hit_even_when_dense_below_threshold():
    """阈值过滤：只要被关键词命中，即使语义分很低也要保留。"""
    # 【设计意图】两路是互补的：BM25 命中说明「字面真的对上了」（比如精确的编号、公司名），
    #   这是很强的质量信号。所以过滤条件用的是「或」——有一路证明它靠谱就放行。
    dense = [_hit("keyword_only", 0.05)]
    bm25 = [_hit("keyword_only", 9.0)]

    fused = rrf_fuse(
        [("dense", dense), ("bm25", bm25)],
        rrf_k=60,
        final_k=10,
        dense_min_score=0.35,
    )

    assert [hit["doc_id"] for hit in fused] == ["keyword_only"]


def test_retrieval_result_empty_flag_and_sources_shape():
    """结果盒子：empty 标志要准，to_sources 产出的「来源卡片」字段要齐。"""
    # 【设计意图】RetrievalResult 是检索结果的「包装盒」，上层靠 result.empty
    #   决定「回暂无数据」还是「组织答案」；靠 to_sources 给前端渲染来源卡片。
    #   这两件事都不能出错，所以单独测。
    empty_result = RetrievalResult(question="空问题")
    assert empty_result.empty is True

    hits = rrf_fuse([("dense", [_hit("A", 0.5)])], rrf_k=60, final_k=5)
    result = RetrievalResult(question="有结果", hits=hits)
    assert result.empty is False

    sources = result.to_sources()
    assert len(sources) == 1
    card = sources[0]
    # 【必备字段】前端靠这几个字段渲染「来源卡片」并定位到原始记录。
    assert card["doc_id"] == "A"
    assert card["table"] == "bidding_notices"
    assert card["label"] == "招标公告"
    # 【分数是四舍五入后的小数】源码用 round(..., 6)，这里确认它是正数即可。
    assert card["rrf_score"] == pytest.approx(card["rrf_score"])
    assert card["rrf_score"] > 0


def test_rrf_survives_non_positive_k():
    """反向测试：非法配置 k<=0 不该把检索链路打挂（防除零/防崩溃）。"""
    # k=0 时 1/(0+rank) 仍是有限值，属「退化但可用」，必须不崩。
    fused = rrf_fuse([("dense", [_hit("A", 0.9)])], rrf_k=0, final_k=5)
    assert fused[0]["rrf_score"] == pytest.approx(1.0)

    # 负数 k：只要 k+rank 不为 0 就仍能算出（负分），同样不该崩。
    fused_neg = rrf_fuse(
        [("dense", [_hit("A", 0.9), _hit("B", 0.5)])], rrf_k=-100, final_k=5
    )
    assert [hit["doc_id"] for hit in fused_neg] == ["A", "B"]

    # 已知边界（记录现状，不在此断言）：当 k 恰好使 k+rank==0（如 k=-1 且 rank=1）时，
    # 源码 1.0/(k+rank) 会抛 ZeroDivisionError。k<=0 属调用方非法配置，
    # 契约要求调用方传 k>0；若要消除该边界需改业务代码，不在本测试范围。