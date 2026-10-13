
"""bm25：自建 BM25 关键词检索（jieba 分词 + 稀疏向量，可持久化）。

职责：
  - 中文分词与词频统计，构建 / 加载 BM25 索引
  - 提供稀疏向量打分，与稠密向量检索做 RRF 融合
  - 增量更新时对未变化文档复用旧统计

依赖：无本项目模块（jieba 第三方分词）
被谁调用：retriever（关键词路召回）、ingest（建库时构建索引）、history（历史全文检索）
"""

from __future__ import annotations

import json

import math

import re

from pathlib import Path

import jieba

_TOKEN_KEEP = re.compile(r"^[\w\u4e00-\u9fff]+$")


def tokenize(text: str) -> list[str]:
    """中文分词 + 归一化。"""
    if not text:
        return []
    tokens: list[str] = []
    for piece in jieba.cut(text, cut_all=False):
        token = piece.strip().lower()
        if token and _TOKEN_KEEP.match(token):
            tokens.append(token)
    return tokens


class BM25Index:
    """可持久化的 BM25 统计量与稀疏向量编码器。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.term_to_id: dict[str, int] = {}
        self.doc_freq: dict[str, int] = {}
        self.n_docs = 0
        self.total_length = 0
        self.avg_doc_length = 0.0

    def fit(self, token_lists: list[list[str]]) -> "BM25Index":
        """基于全量语料统计词表与文档频率。空语料也能安全通过。"""
        self.term_to_id = {}
        self.doc_freq = {}
        total_length = 0
        n_docs = 0

        for tokens in token_lists:
            n_docs += 1
            total_length += len(tokens)
            for term in set(tokens):
                if term not in self.term_to_id:
                    self.term_to_id[term] = len(self.term_to_id)
                self.doc_freq[term] = self.doc_freq.get(term, 0) + 1

        self.n_docs = n_docs
        self.total_length = total_length
        self.avg_doc_length = (total_length / n_docs) if n_docs else 0.0
        return self

    @property
    def ready(self) -> bool:
        """语料为空时 BM25 不可用，检索侧据此短路。"""
        return self.n_docs > 0 and bool(self.term_to_id)

    def idf(self, term: str) -> float:

        if self.n_docs == 0:
            return 0.0
        df = self.doc_freq.get(term, 0)
        return math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def encode_document(self, tokens: list[str]) -> tuple[list[int], list[float]]:
        """文档侧稀疏向量：只用 tf 归一化因子（不含 idf）。"""
        if not tokens or not self.ready:
            return [], []
        length = len(tokens)
        avgdl = self.avg_doc_length or 1.0

        term_freq: dict[str, int] = {}
        for token in tokens:
            term_freq[token] = term_freq.get(token, 0) + 1

        indices: list[int] = []
        values: list[float] = []
        for term, freq in term_freq.items():
            term_id = self.term_to_id.get(term)
            if term_id is None:
                continue
            denominator = freq + self.k1 * (1 - self.b + self.b * length / avgdl)
            if denominator <= 0:
                continue
            indices.append(term_id)
            values.append(freq * (self.k1 + 1) / denominator)
        return indices, values

    def encode_query(self, text: str) -> tuple[list[int], list[float]]:
        """查询侧稀疏向量：idf * 查询词频。"""
        if not self.ready:
            return [], []
        term_freq: dict[str, int] = {}
        for token in tokenize(text):
            term_freq[token] = term_freq.get(token, 0) + 1

        indices: list[int] = []
        values: list[float] = []
        for term, freq in term_freq.items():
            term_id = self.term_to_id.get(term)
            if term_id is None:
                continue
            indices.append(term_id)
            values.append(self.idf(term) * freq)
        return indices, values

    def to_dict(self) -> dict:
        return {
            "k1": self.k1,
            "b": self.b,
            "n_docs": self.n_docs,
            "total_length": self.total_length,
            "avg_doc_length": self.avg_doc_length,
            "term_to_id": self.term_to_id,
            "doc_freq": self.doc_freq,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fp:
            json.dump(self.to_dict(), fp, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path, k1: float = 1.5, b: float = 0.75) -> "BM25Index":
        """索引文件不存在（例如尚未建库）时，返回一个空索引而不是报错。"""
        index = cls(k1=k1, b=b)
        if not path.exists():
            return index
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            return index
        index.k1 = float(data.get("k1", k1))
        index.b = float(data.get("b", b))
        index.n_docs = int(data.get("n_docs", 0))
        index.total_length = int(data.get("total_length", 0))
        index.avg_doc_length = float(data.get("avg_doc_length", 0.0))
        index.term_to_id = {k: int(v) for k, v in data.get("term_to_id", {}).items()}
        index.doc_freq = {k: int(v) for k, v in data.get("doc_freq", {}).items()}
        return index