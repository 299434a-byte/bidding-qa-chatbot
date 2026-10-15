# ============================================================================
# 文件：tests/test_wider_recall.py —— 条号问法「主动放大召回」体检
#
# 【这个测试文件整体在测什么】
#   1) retriever.needs_wider_recall 纯函数：条款号 / 标准号 / 项目编号三类
#      模式判 True，普通问法判 False；
#   2) retrieve 的放大行为：命中模式时两路召回各自放大为 max(k*4, 24)，
#      而最终进入融合的条数（final_k）保持不变——放大的是候选池，不是上下文。
# ============================================================================

from __future__ import annotations

from dataclasses import replace

from app.config import get_settings
from app.retriever import needs_wider_recall, retrieve


class _FakeStore:
    """假向量库：记录每次召回被要求返回的条数，并按条数造等量命中。"""

    def __init__(self) -> None:
        self.dense_ks: list[int] = []
        self.sparse_ks: list[int] = []

    @staticmethod
    def _hit(doc_id: str, score: float) -> dict:
        return {
            "doc_id": doc_id,
            "table": "procurement_notices",
            "label": "采购公告",
            "primary_key": doc_id,
            "score": score,
            "text": f"正文 {doc_id}",
            "fields": {},
        }

    def search_dense(self, query_vector, k: int) -> list[dict]:
        self.dense_ks.append(k)
        return [self._hit(f"dense-{i}", 0.9) for i in range(k)]

    def search_sparse(self, indices, values, k: int) -> list[dict]:
        self.sparse_ks.append(k)
        return [self._hit(f"sparse-{i}", 8.0) for i in range(k)]


class _FakeEmbedder:
    def embed_query(self, text: str) -> list[float]:
        return [0.0]


class _FakeBM25:
    ready = True

    def encode_query(self, text: str) -> tuple[list[int], list[float]]:
        return [1, 2], [1.0, 0.5]


def _settings():
    """钉住各路召回条数（放大公式相对 k 计算，先让基线明确）。"""
    return replace(
        get_settings(),
        retrieve_dense_top_k=10,
        retrieve_sparse_top_k=10,
        retrieve_final_top_k=5,
    )


def _run(question: str) -> tuple[_FakeStore, list[dict], bool]:
    store = _FakeStore()
    result = retrieve(
        question,
        settings=_settings(),
        store=store,
        embedder=_FakeEmbedder(),
        bm25=_FakeBM25(),
    )
    return store, result.hits, result.wider_recall


# ---------------------------------------------------------------------------
# 纯函数：三类模式 + 普通问法
# ---------------------------------------------------------------------------

def test_needs_wider_recall_true_for_three_id_patterns():
    """条款号 / 标准号 / 项目编号三种问法都要放大召回。"""
    assert needs_wider_recall("第十二条说了什么？") is True
    assert needs_wider_recall("GB/T 12345 怎么规定") is True
    assert needs_wider_recall("项目 AB-1203 的情况") is True
    # 含「编号/项目号/公告号」关键词的问法同样命中
    assert needs_wider_recall("这个公告编号是多少？") is True


def test_needs_wider_recall_false_for_ordinary_question():
    """普通语义问法不该放大（候选池翻四倍是成本，不是免费的）。"""
    assert needs_wider_recall("投标人需要具备什么资质要求？") is False
    assert needs_wider_recall("有哪些采购公告？") is False
    assert needs_wider_recall("") is False


# ---------------------------------------------------------------------------
# 行为：两路放大、final_k 不变
# ---------------------------------------------------------------------------

def test_wider_recall_scales_both_routes_but_keeps_final_k():
    """条号问法：两路各放大为 max(10*4, 24)=40，最终仍只取 5 条进融合输出。"""
    store, hits, wider = _run("第十二条说了什么？")

    assert wider is True
    assert store.dense_ks == [40]
    assert store.sparse_ks == [40]
    # 放大的是候选池（80 条候选进 RRF），送进模型的仍是 final_k=5。
    assert len(hits) == 5


def test_normal_question_keeps_default_recall_size():
    """普通问法：两路维持默认 10 条，行为与改动前一致。"""
    store, hits, wider = _run("投标人需要具备什么资质要求？")

    assert wider is False
    assert store.dense_ks == [10]
    assert store.sparse_ks == [10]
    assert len(hits) == 5
