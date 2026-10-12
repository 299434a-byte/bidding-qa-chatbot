# ============================================================================
# 文件：tests/test_cache_versioning.py —— 缓存与知识库版本解耦的行为测试
#
# 【这个测试文件整体在测什么】
#   任务明确要求的四条（全部离线，走 SQLite 临时文件）：
#     ① 版本变化后同一问题不再命中；
#     ② 同版本内命中并返回缓存；
#     ③ 敏感问题（looks_sensitive）不缓存；
#     ④ kb_version 读文件失败时降级为 unknown 且不影响问答（写入照旧可命中）。
#   另外补一条：answer_cache.clear() 能手动清空。
#
# 【为什么要单独测「版本」】缓存键只是规范化问题文本；知识库一重建，
#   同一个键对应的正确答案可能已经变了。版本号写进行里、查询时作必要条件，
#   就是保证「旧知识库的答案绝不会被端给新知识库的用户」。
# ============================================================================

from __future__ import annotations

# SimpleNamespace：造含缓存 + 版本路径的轻量配置替身（答案缓存只读这几个字段）。
from types import SimpleNamespace

# 被测模块。
from app import answer_cache, kb_version


def _settings(tmp_path) -> SimpleNamespace:
    """配置替身：缓存库与知识库版本文件都落在 pytest 临时目录里。"""
    return SimpleNamespace(
        cache_db_path=tmp_path / "answer_cache.db",
        cache_ttl=3600,
        bm25_index_path=tmp_path / "bm25_index.json",
    )


# ----------------------------------------------------------------------------
# 用例 1：同版本内命中并返回缓存（基线：先证明「能命中」）
# ----------------------------------------------------------------------------
def test_same_version_hits(tmp_path) -> None:
    """知识库版本没变时，同一问题命中缓存并原样返回答案/来源/模式。"""
    s = _settings(tmp_path)
    kb_version.write_kb_version(s, points=100, added=1, updated=0, deleted=0)

    assert answer_cache.set_cached("市政道路招标有哪些", "答案正文", [{"title": "来源1"}], "llm", s) is True
    hit = answer_cache.get_cached("市政道路招标有哪些", s, mode="llm")
    assert hit is not None
    assert hit["answer"] == "答案正文"
    assert hit["sources"] == [{"title": "来源1"}]
    assert hit["mode"] == "llm"


# ----------------------------------------------------------------------------
# 用例 2：版本变化后同一问题不再命中
# ----------------------------------------------------------------------------
def test_version_change_invalidates_same_question(tmp_path) -> None:
    """知识库一重建（版本指纹改变），同一问题的旧缓存立即不再命中。"""
    s = _settings(tmp_path)
    kb_version.write_kb_version(s, points=100, added=1, updated=0, deleted=0)
    assert answer_cache.set_cached("同一个问题", "旧知识库的答案", [], "llm", s) is True

    # 重建知识库：kb_version.txt 被改写 → 指纹变化。
    kb_version.write_kb_version(s, points=200, added=5, updated=1, deleted=1)

    # 同一个问题：版本对不上 → 一律当未命中（宁可重算，不用旧答案）。
    assert answer_cache.get_cached("同一个问题", s, mode="llm") is None


# ----------------------------------------------------------------------------
# 用例 3：敏感问题（looks_sensitive）不缓存
# ----------------------------------------------------------------------------
def test_sensitive_question_is_not_cacheable() -> None:
    """问题里疑似含口令/身份证等敏感信息 → 不读也不写缓存（is_cacheable 一票否决）。"""
    assert answer_cache.looks_sensitive("admin 的 password 是什么") is True
    # 缓存准绳：敏感问题直接判 False（调用方据此跳过 get_cached / set_cached）。
    assert (
        answer_cache.is_cacheable(
            logged_in=True,
            mode="llm",
            has_history=False,
            web_search=False,
            has_image=False,
            question="帮我查一下身份证 110101199001011234",
        )
        is False
    )
    # 非敏感问题作为对照：同一条件下可以缓存。
    assert (
        answer_cache.is_cacheable(
            logged_in=True,
            mode="llm",
            has_history=False,
            web_search=False,
            has_image=False,
            question="市政道路招标有哪些",
        )
        is True
    )


# ----------------------------------------------------------------------------
# 用例 4：kb_version 读文件失败 → 降级 unknown，问答不受影响（缓存照常工作）
# ----------------------------------------------------------------------------
def test_kb_version_read_failure_does_not_break_qa(tmp_path) -> None:
    """版本文件读不出来时降级 "unknown"：写入/命中照常，绝不影响问答主流程。"""
    s = _settings(tmp_path)
    # 把 kb_version.txt 弄成目录（读取必失败），bm25 也不存在 → 只能拿到 "unknown"。
    kb_version.kb_version_path(s).mkdir(parents=True, exist_ok=True)
    assert kb_version.kb_version(s) == "unknown"

    # 「不影响问答」的直接证据：缓存读写全部照常、不抛异常，且同版本内能命中。
    assert answer_cache.set_cached("版本坏了也照样缓存", "答案", [], "llm", s) is True
    hit = answer_cache.get_cached("版本坏了也照样缓存", s, mode="llm")
    assert hit is not None
    assert hit["answer"] == "答案"


# ----------------------------------------------------------------------------
# 用例 5：clear() 手动清空全部缓存
# ----------------------------------------------------------------------------
def test_clear_removes_all_rows(tmp_path) -> None:
    """运维兜底：clear() 之后任何问题都不再命中。"""
    s = _settings(tmp_path)
    answer_cache.set_cached("问题一", "答案一", [], "llm", s)
    answer_cache.set_cached("问题二", "答案二", [], "llm", s)

    assert answer_cache.clear(s) is True
    assert answer_cache.get_cached("问题一", s, mode="llm") is None
    assert answer_cache.get_cached("问题二", s, mode="llm") is None
