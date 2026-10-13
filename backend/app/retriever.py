
"""retriever：混合检索（稠密向量 + BM25 两路召回 → RRF 融合 + 门槛过滤）。

职责：
  - 稠密路（Qdrant）与关键词路（BM25）各自召回
  - RRF 排名融合，同一 doc_id 合并计分
  - DENSE_MIN_SCORE 门槛过滤（被 BM25 命中的候选除外）

依赖：vector_store、embeddings、bm25、config
被谁调用：rag、main（/api/retrieve）、agent
"""

from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from .bm25 import BM25Index

from .config import Settings, get_settings

from .embeddings import Embedder, get_embedder

from .vector_store import VectorStore, get_store

_bm25_cache: dict[str, tuple[float, BM25Index]] = {}


def load_bm25(settings: Settings | None = None) -> BM25Index:
    """加载 BM25 索引，按文件修改时间做缓存，避免每次提问都解析大 JSON。"""
    s = settings or get_settings()
    path = s.bm25_index_path
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0

    key = str(path)
    cached = _bm25_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    index = BM25Index.load(path, s.bm25_k1, s.bm25_b)
    _bm25_cache[key] = (mtime, index)
    return index


@dataclass
class RetrievalResult:
    question: str
    hits: list[dict[str, Any]] = field(default_factory=list)
    dense_count: int = 0
    sparse_count: int = 0
    bm25_ready: bool = False
    rerank_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.hits

    def to_sources(self, text_limit: int = 900) -> list[dict[str, Any]]:
        """转成前端「参考来源」卡片所需的精简结构。"""
        sources: list[dict[str, Any]] = []
        for hit in self.hits:
            rerank_rank = hit.get("rerank_rank")
            sources.append(
                {
                    "doc_id": hit["doc_id"],
                    "table": hit["table"],
                    "label": hit["label"],
                    "primary_key": hit["primary_key"],
                    "chunk_index": hit.get("chunk_index", 0),
                    "chunk_total": hit.get("chunk_total", 1),
                    "rrf_score": round(hit.get("rrf_score", 0.0), 6),
                    "scores": {k: round(v, 6) for k, v in hit.get("scores", {}).items()},
                    "ranks": hit.get("ranks", {}),
                    "matched_by": hit.get("matched_by", []),
                    "text": (hit.get("text") or "")[:text_limit],
                    "fields": hit.get("fields", {}),
                    "rerank_rank": rerank_rank,
                }
            )
        return sources


def rrf_fuse(
    result_lists: list[tuple[str, list[dict[str, Any]]]],
    rrf_k: int,
    final_k: int,
    dense_min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """RRF 融合多路召回结果，同一 doc_id 会被合并并累计得分。"""
    merged: dict[str, dict[str, Any]] = {}

    for source, hits in result_lists:
        for rank, hit in enumerate(hits, start=1):
            key = hit.get("doc_id") or hit.get("point_id")
            item = merged.get(key)
            if item is None:
                item = {
                    "doc_id": hit.get("doc_id", ""),
                    "table": hit.get("table", ""),
                    "label": hit.get("label", ""),
                    "primary_key": hit.get("primary_key"),
                    "chunk_index": hit.get("chunk_index", 0),
                    "chunk_total": hit.get("chunk_total", 1),
                    "text": hit.get("text", ""),
                    "fields": hit.get("fields", {}),
                    "scores": {},
                    "ranks": {},
                    "matched_by": [],
                    "rrf_score": 0.0,
                }
                merged[key] = item
            item["scores"][source] = float(hit.get("score", 0.0))
            item["ranks"][source] = rank
            item["matched_by"].append(source)
            item["rrf_score"] += 1.0 / (rrf_k + rank)

    ordered = sorted(merged.values(), key=lambda x: x["rrf_score"], reverse=True)
    for item in ordered:
        item["matched_by"] = sorted(set(item["matched_by"]))

    if dense_min_score > 0:
        ordered = [
            item
            for item in ordered
            if "bm25" in item["scores"]
            or item["scores"].get("dense", 0.0) >= dense_min_score
        ]

    return ordered[:final_k]


def retrieve(
    question: str,
    settings: Settings | None = None,
    store: VectorStore | None = None,
    embedder: Embedder | None = None,
    bm25: BM25Index | None = None,
    final_k: int | None = None,
) -> RetrievalResult:
    """执行一次混合检索。知识库为空时返回空结果（不抛异常）。"""
    s = settings or get_settings()
    question = (question or "").strip()
    if not question:
        return RetrievalResult(question=question)

    store = store or get_store(s)
    embedder = embedder or get_embedder(s)
    bm25 = bm25 if bm25 is not None else load_bm25(s)

    dense_hits: list[dict[str, Any]] = []
    try:
        query_vector = embedder.embed_query(question)
        dense_hits = store.search_dense(query_vector, s.retrieve_dense_top_k)
    except Exception:
        dense_hits = []

    sparse_hits: list[dict[str, Any]] = []
    if bm25.ready:
        indices, values = bm25.encode_query(question)
        sparse_hits = store.search_sparse(indices, values, s.retrieve_sparse_top_k)

    hits = rrf_fuse(
        [("dense", dense_hits), ("bm25", sparse_hits)],
        rrf_k=s.rrf_k,
        final_k=final_k or s.retrieve_final_top_k,
        dense_min_score=s.dense_min_score,
    )

    return RetrievalResult(
        question=question,
        hits=hits,
        dense_count=len(dense_hits),
        sparse_count=len(sparse_hits),
        bm25_ready=bm25.ready,
    )