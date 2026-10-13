
"""vector_store：Qdrant 封装（本地嵌入式模式，免 Docker 独立服务）。

职责：
  - 集合创建 / 写入 / 滚动读取 / 按 doc_id 删除，点 ID 稳定映射（uint64 hash）
  - 建库前校验维度、距离算法与稀疏命名向量配置
  - 混合检索的稠密路查询与知识库统计 / 样本

依赖：config、documents（文档元数据约定）
被谁调用：retriever（稠密路查询）、ingest（写入与差集删除）、main（统计 / 样本端点）
"""

from __future__ import annotations

import atexit

import hashlib

import threading

import uuid

from typing import Any

from qdrant_client import QdrantClient, models

from .config import Settings, get_settings

from .documents import Document

DENSE_VECTOR_NAME = "dense"

SPARSE_VECTOR_NAME = "bm25"


def _close_quietly(client: QdrantClient) -> None:
    """解释器退出前主动关闭客户端。"""
    try:
        client.close()
    except Exception:
        pass


class VectorStoreError(RuntimeError):
    """向量库操作的可读错误。"""


def text_hash(text: str) -> str:
    """给一段正文算「内容指纹」（sha256 十六进制串）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def point_id_of(doc_id: str) -> str:
    """Qdrant 只接受 uint64 或 UUID 作为点 ID。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"bidding-qa::{doc_id}"))


class VectorStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.qdrant_path.mkdir(parents=True, exist_ok=True)
        try:
            self.client = QdrantClient(path=str(settings.qdrant_path))
        except Exception as exc:
            raise VectorStoreError(
                f"打开本地向量库失败（{settings.qdrant_path}）：{exc}。"
                "本地模式同一目录只能被一个进程占用，请确认没有其他后端/建库脚本在运行。"
            ) from exc
        self._lock = threading.Lock()
        atexit.register(_close_quietly, self.client)

    @property
    def collection(self) -> str:
        return self.settings.qdrant_collection

    def collection_exists(self) -> bool:
        try:
            return bool(self.client.collection_exists(self.collection))
        except Exception:
            return False

    def ensure_collection(self, recreate: bool = False) -> None:
        """创建集合；``recreate=True`` 先删后建（重建知识库用）。"""
        with self._lock:
            if recreate and self.collection_exists():
                self.client.delete_collection(self.collection)
            if not self.collection_exists():
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config={
                        DENSE_VECTOR_NAME: models.VectorParams(
                            size=self.settings.embed_dim,
                            distance=models.Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={SPARSE_VECTOR_NAME: models.SparseVectorParams()},
                )
                return
            self._verify_dimension()

    def _verify_dimension(self) -> None:
        """核对集合规格与当前配置：维度、距离算法、稀疏命名向量，不一致就给明确提示。"""
        try:
            info = self.client.get_collection(self.collection)
            params = info.config.params.vectors
            dense = params.get(DENSE_VECTOR_NAME) if isinstance(params, dict) else params
            actual = getattr(dense, "size", None)
            dense_cfg = getattr(dense, "config", None)
            distance = getattr(dense_cfg, "distance", None)
            if distance is None:
                distance = getattr(dense, "distance", None)
            sparse_cfg = getattr(info.config.params, "sparse_vectors", None)
        except Exception:
            return
        if actual and int(actual) != self.settings.embed_dim:
            raise VectorStoreError(
                f"向量库现有维度为 {actual}，当前配置 EMBED_DIM={self.settings.embed_dim} 不一致。"
                "请重建知识库（POST /api/ingest）以适配新模型。"
            )
        if distance is not None and str(getattr(distance, "value", distance)).lower() != "cosine":
            raise VectorStoreError(
                f"向量库距离算法为 {getattr(distance, 'value', distance)}，当前配置要求 Cosine。"
                "距离算法变更后检索分数口径不一致，请重建知识库（POST /api/ingest）。"
            )
        if isinstance(sparse_cfg, dict) and SPARSE_VECTOR_NAME not in sparse_cfg:
            raise VectorStoreError(
                f"向量库缺少稀疏命名向量「{SPARSE_VECTOR_NAME}」（旧版集合只建了稠密向量），"
                "BM25 关键词检索将不可用。请重建知识库（POST /api/ingest）。"
            )

    def upsert_documents(
        self,
        documents: list[Document],
        dense_vectors: list[list[float]],
        sparse_vectors: list[tuple[list[int], list[float]]],
        batch_size: int = 128,
    ) -> int:
        """批量写入。稀疏向量为空（BM25 未拟合）时只写稠密向量。"""
        if not documents:
            return 0

        points: list[models.PointStruct] = []
        for doc, dense, (indices, values) in zip(documents, dense_vectors, sparse_vectors):
            vector: dict[str, Any] = {DENSE_VECTOR_NAME: dense}
            if indices:
                vector[SPARSE_VECTOR_NAME] = models.SparseVector(indices=indices, values=values)
            payload = dict(doc.payload)
            payload["doc_id"] = doc.doc_id
            payload["text"] = doc.text
            points.append(
                models.PointStruct(id=point_id_of(doc.doc_id), vector=vector, payload=payload)
            )

        with self._lock:
            for start in range(0, len(points), batch_size):
                self.client.upsert(
                    collection_name=self.collection,
                    points=points[start : start + batch_size],
                    wait=True,
                )
        return len(points)

    def scroll_existing(self, batch_size: int = 1024) -> dict[str, str]:
        """只读：分批滚动取回全库文档，返回 ``{doc_id: 正文sha256}``。"""
        if not self.collection_exists():
            return {}
        existing: dict[str, str] = {}
        offset: Any = None
        with self._lock:
            while True:
                points, offset = self.client.scroll(
                    collection_name=self.collection,
                    limit=batch_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload = point.payload or {}
                    doc_id = payload.get("doc_id")
                    if doc_id:
                        existing[doc_id] = text_hash(payload.get("text") or "")
                if offset is None:
                    break
        return existing

    def delete_documents(self, doc_ids: list[str], batch_size: int = 256) -> int:
        """按 doc_id 批量删除点，返回**请求删除**的 ID 数。"""
        if not doc_ids:
            return 0
        point_ids = [point_id_of(doc_id) for doc_id in doc_ids]
        deleted = 0
        with self._lock:
            for start in range(0, len(point_ids), batch_size):
                batch = point_ids[start : start + batch_size]
                self.client.delete(
                    collection_name=self.collection,
                    points_selector=models.PointIdsList(points=batch),
                    wait=True,
                )
                deleted += len(batch)
        return deleted

    @staticmethod
    def _to_hit(point: models.ScoredPoint, source: str) -> dict[str, Any]:
        payload = point.payload or {}
        return {
            "point_id": str(point.id),
            "doc_id": payload.get("doc_id", ""),
            "table": payload.get("table", ""),
            "label": payload.get("label", payload.get("table", "")),
            "text": payload.get("text", ""),
            "fields": payload.get("fields", {}),
            "primary_key": payload.get("primary_key"),
            "chunk_index": payload.get("chunk_index", 0),
            "chunk_total": payload.get("chunk_total", 1),
            "score": float(point.score),
            "source": source,
        }

    def search_dense(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        if not vector or limit <= 0 or not self.collection_exists():
            return []
        with self._lock:
            response = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                using=DENSE_VECTOR_NAME,
                limit=limit,
                with_payload=True,
            )
        return [self._to_hit(p, "dense") for p in response.points]

    def search_sparse(self, indices: list[int], values: list[float], limit: int) -> list[dict[str, Any]]:
        if not indices or limit <= 0 or not self.collection_exists():
            return []
        with self._lock:
            response = self.client.query_points(
                collection_name=self.collection,
                query=models.SparseVector(indices=indices, values=values),
                using=SPARSE_VECTOR_NAME,
                limit=limit,
                with_payload=True,
            )
        return [self._to_hit(p, "bm25") for p in response.points]

    def stats(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "collection": self.collection,
            "path": str(self.settings.qdrant_path),
            "exists": False,
            "points": 0,
            "by_table": {},
            "vector_dim": self.settings.embed_dim,
            "empty": True,
        }
        if not self.collection_exists():
            return base

        base["exists"] = True
        try:
            info = self.client.get_collection(self.collection)
            base["points"] = int(info.points_count or 0)
        except Exception:
            pass

        by_table: dict[str, int] = {}
        for table in self.settings.kb_tables:
            try:
                counted = self.client.count(
                    collection_name=self.collection,
                    count_filter=models.Filter(
                        must=[models.FieldCondition(key="table", match=models.MatchValue(value=table))]
                    ),
                    exact=True,
                )
                by_table[table] = int(counted.count)
            except Exception:
                by_table[table] = 0
        base["by_table"] = by_table
        base["empty"] = base["points"] == 0
        return base

    def sample(self, limit: int = 5) -> list[dict[str, Any]]:
        """按存储顺序取前 N 条样本（scroll，非随机），供前端自检「库里有东西」。"""
        if not self.collection_exists():
            return []
        try:
            points, _ = self.client.scroll(
                collection_name=self.collection,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return []
        return [
            {
                "doc_id": (p.payload or {}).get("doc_id", ""),
                "table": (p.payload or {}).get("table", ""),
                "label": (p.payload or {}).get("label", ""),
                "primary_key": (p.payload or {}).get("primary_key"),
                "text": (p.payload or {}).get("text", "")[:300],
            }
            for p in points
        ]


_store: VectorStore | None = None

_store_lock = threading.Lock()


def get_store(settings: Settings | None = None) -> VectorStore:
    """进程内单例（本地模式一个目录只能开一个客户端）。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = VectorStore(settings or get_settings())
    return _store