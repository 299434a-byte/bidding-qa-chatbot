# ============================================================================
# 文件：tests/test_vector_store_verify.py —— _verify_dimension 的三项规格核对
#
# 【这个测试文件整体在测什么】
#   VectorStore._verify_dimension 在「集合已存在」时核对规格，此前只查维度，
#   漏掉两种「换配置没重建库」的坑（本次补齐）：
#     ① 距离算法被改（Cosine → Dot）：维度照样对得上，但分数口径变了，
#        检索排序悄悄错乱，比当场报错更难排查；
#     ② 老集合没有稀疏命名向量 "bm25"：dense 正常、search_sparse 运行时才炸。
#
# 【为什么完全离线】不碰真实 Qdrant：用 object.__new__ 绕过构造函数，
#   手动挂一个「只实现 get_collection」的假客户端，把集合信息用 SimpleNamespace
#   拼成 qdrant 客户端返回的那种嵌套结构即可。
# ============================================================================

from __future__ import annotations

# SimpleNamespace：造「结构像对象」的轻量容器，模拟 qdrant 返回的嵌套信息。
from types import SimpleNamespace

import pytest

from app.vector_store import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME, VectorStore, VectorStoreError


def _make_store(
    *,
    size: int = 512,
    distance: str = "Cosine",
    sparse_names: tuple[str, ...] | None = (SPARSE_VECTOR_NAME,),
    embed_dim: int = 512,
) -> VectorStore:
    """拼一个带假 client 的 VectorStore：只实现 _verify_dimension 用到的读取路径。

    参数：
      size          —— 集合里 dense 向量的维度；
      distance      —— 集合里登记的距离算法；
      sparse_names  —— 稀疏命名向量清单；None 表示「信息里没有这个字段」（老客户端）；
      embed_dim     —— 当前配置要求的维度（EMBED_DIM）。
    """
    # 绕过 __init__（它会建目录、真开 Qdrant 本地锁），直接造空对象。
    store = object.__new__(VectorStore)
    # settings 需要 embed_dim（要比对）与 qdrant_collection（collection 是 property，
    # 值取自 settings，所以不能直接给 store.collection 赋值），用 SimpleNamespace 顶替。
    store.settings = SimpleNamespace(embed_dim=embed_dim, qdrant_collection="test_collection")
    # dense 规格：新版客户端把距离放在 vector.config.distance（老版直接在 vector.distance）。
    dense = SimpleNamespace(size=size, config=SimpleNamespace(distance=distance))
    # vectors 区块：新版是 {向量名: 规格} 的字典。
    vectors = {DENSE_VECTOR_NAME: dense}
    params = SimpleNamespace(vectors=vectors)
    # 稀疏区块：None 模拟「老客户端/老信息不返回该字段」的情况（应跳过检查）。
    if sparse_names is not None:
        params.sparse_vectors = {name: SimpleNamespace() for name in sparse_names}
    store.client = SimpleNamespace(
        get_collection=lambda name: SimpleNamespace(config=SimpleNamespace(params=params))
    )
    return store


def test_matching_spec_passes_silently() -> None:
    """三项全对（512 维 / Cosine / 有 bm25）→ 一声不吭，正常放行。"""
    _make_store()._verify_dimension()


def test_dimension_mismatch_still_raises() -> None:
    """原有行为回归：维度不一致 → 报「请重建知识库」。"""
    with pytest.raises(VectorStoreError, match="EMBED_DIM"):
        _make_store(size=768, embed_dim=512)._verify_dimension()


def test_distance_algorithm_mismatch_raises() -> None:
    """距离算法不是 Cosine → 必须报错（维度对也拦，防分数口径悄悄错乱）。"""
    with pytest.raises(VectorStoreError, match="距离算法"):
        _make_store(distance="Dot")._verify_dimension()


def test_missing_sparse_vector_raises() -> None:
    """集合里没有稀疏命名向量 bm25 → 报错，提示 BM25 检索会不可用。"""
    with pytest.raises(VectorStoreError, match="稀疏命名向量"):
        _make_store(sparse_names=("other",))._verify_dimension()


def test_absent_sparse_field_is_tolerated() -> None:
    """信息里压根没有 sparse_vectors 字段（老客户端）→ 跳过该项检查，不误伤。"""
    _make_store(sparse_names=None)._verify_dimension()


def test_enum_style_distance_is_accepted() -> None:
    """距离值是枚举（真实取值在 .value）而非裸字符串时，同样能正确比对。"""
    # 模拟 qdrant 的 Distance 枚举：比较时代码会取 .value 拿到 "Cosine" 再比对。
    _make_store(distance=SimpleNamespace(value="Cosine"))._verify_dimension()
