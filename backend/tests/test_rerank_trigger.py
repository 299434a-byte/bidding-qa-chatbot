# ============================================================================
# 文件：tests/test_rerank_trigger.py —— 重排按需触发（should_rerank）的单测
#
# 【这个测试文件整体在测什么】
#   重排单次大模型调用要 8.7~13.1 秒，旧逻辑「候选够就一律重排」让约一半请求白白
#   扛着这笔开销（实测首字延迟被拖到 6.9 秒）。本轮把重排改成按需触发：
#     · should_rerank(hits, settings) 是纯函数 —— 只看 RRF 融合结果的统计特征，
#       判定「顺序可不可靠」，不碰网络/大模型；
#     · rerank_hits 再按 RERANK_MODE（auto/always/off）决定排不排。
#   本文件逐条锁定两条触发条件、一条「不值得就不排」、以及三态模式开关的行为。
#
# 【为什么不是三条（如实说明）】最初实现有第三条「多路分歧：dense 第一 ≠ bm25 第一」，
#   实测 13 个真实问题上 12/13 恒触发（两路召回集本就不同，分歧是常态不是信号），
#   已砍掉——下面 test_no_trigger_merely_on_path_disagreement 专门把这个坏信号锁死。
#   条件①最初看 RRF 分差，实测 13/13 恒触发（RRF 分天然密集），已换成看 dense 分差。
#
# 【为什么不用「候选数超过 N」当触发条件（如实说明）】
#   候选多只是池子大，与顺序可不可靠无关；rerank_hits 已有 len(hits) <= want_k
#   的免排分支（没得挑就不排）。再加一条「候选多就排」只会反向拉高重排率，
#   与省时间的目标相反——所以这条件没进实现，此处也不测它。
#
# 【全部离线】不连网、不连库、不调大模型：should_rerank 是纯函数；
#   rerank_hits 的大模型调用用 monkeypatch 打掉并计数。
# ============================================================================

from __future__ import annotations

# SimpleNamespace：造 should_rerank 需要的轻量配置替身（只读两个阈值字段）。
from types import SimpleNamespace

# dataclasses.replace：在真实配置副本上只改 RERANK_MODE 等字段（rerank_hits 的测试用）。
import dataclasses

# pytest.raises / HTTPException 不需要；这里只要 monkeypatch。
from app import rerank as rerank_mod
from app.config import get_settings
from app.rerank import rerank_hits, should_rerank


# 阈值替身：与 config.py 的默认值保持一致（gap=0.1、min_dense=0.35）。
def _settings(**overrides) -> SimpleNamespace:
    base = {"rerank_auto_gap": 0.1, "rerank_auto_min_dense": 0.35}
    base.update(overrides)
    return SimpleNamespace(**base)


def _hit(
    doc_id: str,
    *,
    rrf: float,
    dense: float | None = None,
    bm25: float | None = None,
    dense_rank: int | None = None,
    bm25_rank: int | None = None,
) -> dict:
    """造一条 RRF 融合后的候选（字段形状与 retriever.rrf_fuse 的产出一致）。"""
    scores: dict = {}
    ranks: dict = {}
    if dense is not None:
        scores["dense"] = dense
    if bm25 is not None:
        scores["bm25"] = bm25
    if dense_rank is not None:
        ranks["dense"] = dense_rank
    if bm25_rank is not None:
        ranks["bm25"] = bm25_rank
    return {"doc_id": doc_id, "rrf_score": rrf, "scores": scores, "ranks": ranks, "text": ""}


# ----------------------------------------------------------------------------
# 条件 ①：头两名 dense 分数并列 → 顺序像抛硬币 → 值得重排
# ----------------------------------------------------------------------------
def test_trigger_when_top_scores_are_close() -> None:
    """top1 与 top2 的 dense 分相对差 < rerank_auto_gap → True。"""
    hits = [
        _hit("a", rrf=0.030, dense=0.72, dense_rank=1, bm25_rank=2),
        _hit("b", rrf=0.029, dense=0.70, dense_rank=2, bm25_rank=1),
    ]
    # dense 相对分差 = (0.72-0.70)/0.72 ≈ 0.028 < 0.1 → 顺序不可靠。
    assert should_rerank(hits, _settings()) is True


# ----------------------------------------------------------------------------
# 条件 ②：融合第一名的 dense 相似度过低 → 召回质量存疑 → 值得重排
# ----------------------------------------------------------------------------
def test_trigger_when_top1_dense_is_weak() -> None:
    """top1 的 dense 分 < rerank_auto_min_dense → True（哪怕分差很大）。"""
    hits = [
        # 分差巨大（第二名远低于第一名）→ 条件 ① 不会触发；
        # 但 top1 的 dense 只有 0.20（低于 0.35 门槛）→ 条件 ② 触发。
        _hit("a", rrf=0.032, dense=0.20, dense_rank=1, bm25_rank=1),
        _hit("b", rrf=0.010, dense=0.10, dense_rank=5, bm25_rank=6),
    ]
    assert should_rerank(hits, _settings()) is True


# ----------------------------------------------------------------------------
# 反向锁定：仅凭「多路分歧」不触发（该条件实测恒真，已砍掉，防止被加回来）
# ----------------------------------------------------------------------------
def test_no_trigger_merely_on_path_disagreement() -> None:
    """dense 第一 ≠ bm25 第一，但 dense 分差悬殊 → 不重排。

    【为什么这样测】「两路第一名不同」曾是触发条件③，实测 12/13 恒触发
    （两路召回集本就不同），砍掉后才让 auto 真正跳过。这里把它锁死：
    分差悬殊（0.85 vs 0.55）时，即便两路各推各的第一名，也不该重排。
    """
    hits = [
        # 融合第一：dense 第 1、bm25 第 2，dense 0.85。
        _hit("a", rrf=0.030, dense=0.85, dense_rank=1, bm25_rank=2),
        # 融合第二：dense 第 2、bm25 第 1，dense 0.55 —— 两路第一名不同。
        _hit("b", rrf=0.029, dense=0.55, dense_rank=2, bm25_rank=1),
    ]
    # dense 相对差 = 0.353 > 0.1，dense1=0.85 也高于 0.35 → 两条都不该触发。
    assert should_rerank(hits, _settings()) is False


# ----------------------------------------------------------------------------
# 「不值得时不重排」：三路信号都说顺序可靠 → False
# ----------------------------------------------------------------------------
def test_no_trigger_when_order_is_reliable() -> None:
    """dense 分差悬殊 + 第一名 dense 分高 → 不重排（省下 8~13 秒）。"""
    hits = [
        # 两路都排第 1、dense 0.85，明显高于第二名的 0.55。
        _hit("a", rrf=0.0328, dense=0.85, dense_rank=1, bm25_rank=1),
        _hit("b", rrf=0.0164, dense=0.55, dense_rank=4, bm25_rank=3),
        _hit("c", rrf=0.0161, dense=0.50, dense_rank=5, bm25_rank=2),
    ]
    assert should_rerank(hits, _settings()) is False


# ----------------------------------------------------------------------------
# 边界：候选不足 2 条 → 没有「顺序可争」→ False
# ----------------------------------------------------------------------------
def test_no_trigger_with_fewer_than_two_hits() -> None:
    """0 或 1 条候选时直接 False（rerank_hits 里另有 too_few_candidates 分支兜底）。"""
    assert should_rerank([], _settings()) is False
    assert should_rerank([_hit("a", rrf=0.03, dense=0.9, dense_rank=1, bm25_rank=1)], _settings()) is False


# ----------------------------------------------------------------------------
# RERANK_MODE 三态：off / always / auto（对 rerank_hits 的行为开关）
# ----------------------------------------------------------------------------
def _reliable_hits() -> list[dict]:
    """一批「顺序可靠」的候选：auto 模式下应当跳过重排。"""
    return [
        _hit("a", rrf=0.0328, dense=0.85, dense_rank=1, bm25_rank=1),
        _hit("b", rrf=0.0164, dense=0.55, dense_rank=4, bm25_rank=3),
        _hit("c", rrf=0.0161, dense=0.50, dense_rank=5, bm25_rank=2),
        _hit("d", rrf=0.0140, dense=0.45, dense_rank=6, bm25_rank=4),
        _hit("e", rrf=0.0120, dense=0.40, dense_rank=7, bm25_rank=5),
        _hit("f", rrf=0.0100, dense=0.38, dense_rank=8, bm25_rank=6),
    ]


def _counting_llm(counter: dict):
    """假大模型：计一次调用并返回一个合法排序 JSON。"""

    def fake(*args, **kwargs):
        counter["calls"] = counter.get("calls", 0) + 1
        return '{"order": [0, 1, 2, 3, 4, 5]}'

    return fake


def _rerank_settings(mode: str):
    """真实配置副本 + 指定 RERANK_MODE（其余字段不动）。"""
    return dataclasses.replace(get_settings(), rerank_mode=mode)


def test_mode_off_never_reranks(monkeypatch) -> None:
    """RERANK_MODE=off：一次大模型都不调，meta 记下跳过原因 mode_off。"""
    counter: dict = {}
    monkeypatch.setattr(rerank_mod, "complete_chat", _counting_llm(counter))

    hits, meta = rerank_hits("问题", _reliable_hits(), _rerank_settings("off"), top_k=5)
    assert counter.get("calls", 0) == 0
    assert meta["rerank_applied"] is False
    assert meta["rerank_skipped"] is True
    assert meta["rerank_skip_reason"] == "mode_off"
    assert len(hits) == 5


def test_mode_always_reranks_even_when_reliable(monkeypatch) -> None:
    """RERANK_MODE=always：保持旧行为——就算顺序可靠也照样重排（向后兼容）。"""
    counter: dict = {}
    monkeypatch.setattr(rerank_mod, "complete_chat", _counting_llm(counter))

    hits, meta = rerank_hits("问题", _reliable_hits(), _rerank_settings("always"), top_k=5)
    assert counter.get("calls") == 1
    assert meta["rerank_applied"] is True
    assert meta["rerank_skipped"] is False
    # 重排后按模型给的顺序重排（这里模型顺序 = 原顺序），并打上 rerank_rank。
    assert [h["rerank_rank"] for h in hits] == [1, 2, 3, 4, 5]


def test_mode_auto_skips_when_reliable_and_reranks_when_not(monkeypatch) -> None:
    """RERANK_MODE=auto（默认）：可靠就跳过、不可靠才调大模型。"""
    counter: dict = {}
    monkeypatch.setattr(rerank_mod, "complete_chat", _counting_llm(counter))
    s = _rerank_settings("auto")

    # 顺序可靠 → 跳过，不花大模型调用。
    _, meta = rerank_hits("问题", _reliable_hits(), s, top_k=5)
    assert counter.get("calls", 0) == 0
    assert meta["rerank_skipped"] is True
    assert meta["rerank_skip_reason"] == "auto_order_reliable"

    # 换一批「头两名 dense 分并列」的候选（0.80 vs 0.79）→ 触发重排。
    close_hits = [
        _hit("a", rrf=0.0300, dense=0.80, dense_rank=1, bm25_rank=2),
        _hit("b", rrf=0.0299, dense=0.79, dense_rank=2, bm25_rank=1),
        _hit("c", rrf=0.0161, dense=0.50, dense_rank=5, bm25_rank=3),
        _hit("d", rrf=0.0140, dense=0.45, dense_rank=6, bm25_rank=4),
        _hit("e", rrf=0.0120, dense=0.40, dense_rank=7, bm25_rank=5),
        _hit("f", rrf=0.0100, dense=0.38, dense_rank=8, bm25_rank=6),
    ]
    _, meta = rerank_hits("问题", close_hits, s, top_k=5)
    assert counter.get("calls") == 1
    assert meta["rerank_applied"] is True
    assert meta["rerank_skipped"] is False
