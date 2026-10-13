"""embeddings：稠密向量封装（bge-small-zh-v1.5，512 维，CPU 推理）。

职责：
  - 加载本地 embedding 模型并提供批量编码接口
  - 管理模型生命周期（懒加载、进程内复用）
  - 暴露向量维度供建库与检索校验

依赖：config
被谁调用：ingest（入库向量化）、retriever（查询向量化）
"""

from __future__ import annotations

import threading

from .config import Settings, get_settings


class EmbeddingError(RuntimeError):
    """向量化过程中的可读错误。"""


class Embedder:
    """懒加载的向量模型包装器（模型加载较慢，只在首次使用时初始化）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = None
        self._lock = threading.Lock()

    @property
    def model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from fastembed import TextEmbedding

                        self._model = TextEmbedding(model_name=self._settings.embed_model)
                    except Exception as exc:
                        raise EmbeddingError(
                            f"加载向量模型 {self._settings.embed_model} 失败：{exc}"
                        ) from exc
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = [
            [float(x) for x in vec]
            for vec in self.model.embed(texts, batch_size=self._settings.embed_batch_size)
        ]
        self._check_dim(vectors)
        return vectors

    def embed_query(self, question: str) -> list[float]:
        text = f"{self._settings.embed_query_prefix}{question}" if self._settings.embed_query_prefix else question
        vectors = self.embed_documents([text])
        return vectors[0] if vectors else []

    @property
    def dim(self) -> int:
        return self._settings.embed_dim

    def _check_dim(self, vectors: list[list[float]]) -> None:
        if not vectors:
            return
        actual = len(vectors[0])
        if actual != self._settings.embed_dim:
            raise EmbeddingError(
                f"向量维度不匹配：模型 {self._settings.embed_model} 实际输出 {actual} 维，"
                f"但 .env 中 EMBED_DIM={self._settings.embed_dim}。请修正配置后重建知识库。"
            )


_embedder: Embedder | None = None

_embedder_lock = threading.Lock()


def get_embedder(settings: Settings | None = None) -> Embedder:
    """进程内单例，避免重复加载模型。"""
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                _embedder = Embedder(settings or get_settings())
    return _embedder