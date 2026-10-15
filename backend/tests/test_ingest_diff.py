"""增量更新（run_incremental）的离线单元测试。

完全离线：用「假 store / 假 embedder / 假文档生成」替换真实依赖，
只验证「差集分类」与「删除顺序」这两件最关键的逻辑。

覆盖三件事（对应用户要求）：
  1) added / updated / unchanged / deleted 四个分支各自归类正确；
  2) 「删除」发生在「写入」之后（关键安全约束：先写后删，前面出错也不会破坏已有数据）；
  3) BM25 词表合并后，已存在词的编号不变（只追加新词），保证旧稀疏向量下标不失效。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from app import ingest
from app.bm25 import BM25Index
from app.config import get_settings
from app.documents import Document
from app.vector_store import text_hash


def _doc(doc_id: str, text: str) -> Document:
    """造一条最简 Document（只填必填字段，payload 用默认空字典）。"""
    return Document(
        doc_id=doc_id,
        table="t",
        label="T",
        primary_key=doc_id,
        chunk_index=0,
        chunk_total=1,
        text=text,
    )


class _FakeStore:
    """假向量库：只记录「被要求做什么」，不做任何真实读写的实现。

    · scroll_existing() 返回预置的 {doc_id: 正文指纹}，用来驱动差集分类；
    · events 记录调用顺序，供「删除在写入之后」的顺序断言使用。
    """

    def __init__(self, existing: dict[str, str]) -> None:
        self._existing = dict(existing)
        self.events: list[tuple[str, Any]] = []
        self.upserted: list[str] = []
        self.deleted: list[str] = []

    def ensure_collection(self, recreate: bool = False) -> None:
        self.events.append(("ensure_collection", recreate))

    def scroll_existing(self) -> dict[str, str]:
        return dict(self._existing)

    def upsert_documents(self, docs: list[Document], dense: Any, sparse: Any) -> int:
        ids = [d.doc_id for d in docs]
        self.events.append(("upsert", ids))
        self.upserted = ids
        return len(ids)

    def delete_documents(self, ids: list[str]) -> int:
        ids = list(ids)
        self.events.append(("delete", ids))
        self.deleted = ids
        return len(ids)


class _FakeEmbedder:
    """假向量模型：返回定长假向量，不加载真实模型。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


def _patch(monkeypatch: Any, store: _FakeStore, documents: list[Document], token_lists: list[list[str]]) -> None:
    """把 run_incremental 依赖的三个真实入口换成假对象。"""
    stage = {
        "tables": {},
        "errors": [],
        "warnings": [],
        "documents": len(documents),
        "cleaning": {},
    }
    monkeypatch.setattr(ingest, "generate_documents", lambda s, p: (documents, token_lists, stage))
    monkeypatch.setattr(ingest, "get_store", lambda s: store)
    monkeypatch.setattr(ingest, "get_embedder", lambda s: _FakeEmbedder())


def _settings(tmp_path: Any) -> Any:
    """把 BM25 索引落到 pytest 临时目录，避免污染共享沙箱。"""
    return dataclasses.replace(get_settings(), bm25_index_path=tmp_path / "bm25.json")


def test_incremental_classifies_four_branches(tmp_path, monkeypatch) -> None:
    """added / updated / unchanged / deleted 四个分支各自归类正确。"""
    s = _settings(tmp_path)
    # 库里已有三条：A（本次无变化）、C（本次正文变了）、D（本次已从数据源消失）。
    a_text = "投标人需要资质"
    existing = {
        "doc:A": text_hash(a_text),
        "doc:C": text_hash("投标保证金形式"),
        "doc:D": text_hash("即将被删除的旧块"),
    }
    # 本次全量文档：A 原样、B 全新、C 正文变更；D 不在其中 → 应删除。
    documents = [
        _doc("doc:A", a_text),
        _doc("doc:B", "全新加入的文档块"),
        _doc("doc:C", "投标保证金形式已变更"),
    ]
    token_lists = [["投标", "资质"], ["全新", "文档"], ["保证金", "变更"]]
    store = _FakeStore(existing)
    _patch(monkeypatch, store, documents, token_lists)

    summary = ingest.run_incremental(settings=s)

    assert summary["added"] == 1  # B
    assert summary["updated"] == 1  # C
    assert summary["unchanged"] == 1  # A
    assert summary["deleted"] == 1  # D
    # 只写「新增 + 更新」，且顺序为先新增后更新。
    assert store.upserted == ["doc:B", "doc:C"]
    # 只删「数据源已消失」的 D。
    assert store.deleted == ["doc:D"]
    # 增量绝不重建集合。
    assert ("ensure_collection", False) in store.events


def test_incremental_deletes_after_upsert(tmp_path, monkeypatch) -> None:
    """顺序断言：删除必须发生在写入之后。"""
    s = _settings(tmp_path)
    existing = {"doc:D": text_hash("旧块")}
    documents = [_doc("doc:B", "新增块")]
    token_lists = [["新增"]]
    store = _FakeStore(existing)
    _patch(monkeypatch, store, documents, token_lists)

    ingest.run_incremental(settings=s)

    kinds = [name for name, _ in store.events]
    assert "upsert" in kinds and "delete" in kinds
    # 写入早于删除：万一写入环节出错，删除还没发生，已有数据不会被破坏。
    assert kinds.index("upsert") < kinds.index("delete")


def test_incremental_preserves_bm25_term_ids(tmp_path, monkeypatch) -> None:
    """BM25 词表合并后：旧词编号不变，新词编号从旧最大值之后顺延。"""
    s = _settings(tmp_path)
    # 预置「上次建库」存下的词表编号。
    old = BM25Index(k1=s.bm25_k1, b=s.bm25_b).fit([["投标", "资质"], ["保证金"]])
    old.save(s.bm25_index_path)
    old_map = dict(old.term_to_id)
    assert old_map  # 词表非空，后续断言才有意义

    # 本次语料：含旧词（投标/资质/保证金）+ 新词（道路/招标）。
    documents = [_doc("doc:A", "投标 资质 道路"), _doc("doc:B", "保证金 招标")]
    token_lists = [["投标", "资质", "道路"], ["保证金", "招标"]]
    store = _FakeStore({})  # 全部当作新增
    _patch(monkeypatch, store, documents, token_lists)

    ingest.run_incremental(settings=s)

    after = BM25Index.load(s.bm25_index_path, s.bm25_k1, s.bm25_b)
    # 已存在词的编号必须原封不动。
    for term, idx in old_map.items():
        assert after.term_to_id[term] == idx, f"旧词 {term} 的编号被改动了"
    # 新词的编号必须严格大于旧最大值。
    base = max(old_map.values())
    for term, idx in after.term_to_id.items():
        if term not in old_map:
            assert idx > base, f"新词 {term} 的编号未顺延"