# ============================================================================
# 文件：tests/test_kb_version.py —— 知识库版本指纹（kb_version）的行为测试
#
# 【这个测试文件整体在测什么】
#   kb_version 是「缓存与知识库版本解耦」的判据，必须满足四条：
#     ① 只读廉价本地文件（kb_version.txt 首选 / bm25_index.json 的 mtime+size 兜底），
#        读取时绝不开 Qdrant（本地目录独占，请求期开客户端有风险）；
#     ② 输出 8~12 位短 hash，同样信号稳定、不同信号必变；
#     ③ 读文件失败一律降级 "unknown"，绝不抛异常；
#     ④ ingest 成功后写入的 kb_version.txt 能被读成新版本。
#   全部是临时目录里的纯文件操作，离线可测。
# ============================================================================

from __future__ import annotations

# SimpleNamespace：造只含 bm25_index_path 的轻量配置替身。
from types import SimpleNamespace

# 被测模块。
from app import kb_version


def _settings(tmp_path) -> SimpleNamespace:
    """配置替身：数据目录指向 pytest 临时目录（bm25_index_path 的父目录即数据目录）。"""
    return SimpleNamespace(bm25_index_path=tmp_path / "bm25_index.json")


# ----------------------------------------------------------------------------
# 用例 1：两个文件都没有 → "unknown"（且不抛异常）
# ----------------------------------------------------------------------------
def test_missing_files_yield_unknown(tmp_path) -> None:
    """什么信号都读不到时返回 "unknown"，而不是抛错。"""
    assert kb_version.kb_version(_settings(tmp_path)) == "unknown"


# ----------------------------------------------------------------------------
# 用例 2：write_kb_version 写入后可读出稳定指纹（8~12 位）
# ----------------------------------------------------------------------------
def test_write_then_read_gives_stable_short_hash(tmp_path) -> None:
    """ingest 成功后写的 kb_version.txt 能被读成版本号：短、稳定、非 unknown。"""
    s = _settings(tmp_path)
    ver = kb_version.write_kb_version(s, points=933, added=1, updated=2, deleted=0)

    # 8~12 位短 hash。
    assert 8 <= len(ver) <= 12
    assert ver != "unknown"
    # 连续读两次结果一致（确定性：同样的文件内容 → 同样的指纹）。
    assert kb_version.kb_version(s) == ver


# ----------------------------------------------------------------------------
# 用例 3：内容一变（重跑 ingest）→ 版本必变
# ----------------------------------------------------------------------------
def test_content_change_changes_version(tmp_path) -> None:
    """重跑建库会改写 kb_version.txt（ISO 时间必变）→ 版本指纹改变 → 旧缓存全部失效。"""
    s = _settings(tmp_path)
    v1 = kb_version.write_kb_version(s, points=933, added=0, updated=0, deleted=0)
    v2 = kb_version.write_kb_version(s, points=933, added=0, updated=0, deleted=0)
    # 两次写入的 ISO 时间不同 → 文件内容不同 → 指纹不同。
    assert v1 != v2


# ----------------------------------------------------------------------------
# 用例 4：没有 kb_version.txt 时，用 bm25_index.json 的 mtime+size 兜底
# ----------------------------------------------------------------------------
def test_falls_back_to_bm25_mtime_and_size(tmp_path) -> None:
    """老环境没有 kb_version.txt：靠 BM25 索引的「修改时间 + 大小」感知知识库变没变。"""
    s = _settings(tmp_path)
    bm25 = s.bm25_index_path
    bm25.write_text('{"terms": ["招标"]}', encoding="utf-8")
    v1 = kb_version.kb_version(s)
    assert v1 != "unknown"

    # 文件内容/大小变了（相当于重建过知识库）→ 版本跟着变。
    bm25.write_text('{"terms": ["招标", "公告"]}', encoding="utf-8")
    v2 = kb_version.kb_version(s)
    assert v2 != v1


# ----------------------------------------------------------------------------
# 用例 5：读文件失败 → 降级 "unknown"，不抛异常
# ----------------------------------------------------------------------------
def test_read_failure_degrades_to_unknown(tmp_path) -> None:
    """kb_version.txt 变成目录（读取必然失败）之类的异常场景：降级 unknown，不炸。"""
    s = _settings(tmp_path)
    # 把 kb_version.txt 建成一个「目录」：read_bytes() 会抛 IsADirectoryError 之类的错。
    kb_version.kb_version_path(s).mkdir(parents=True, exist_ok=True)
    # bm25 也不存在 → 兜底信号同样拿不到。
    assert kb_version.kb_version(s) == "unknown"


# ----------------------------------------------------------------------------
# 用例 6：kb_version.txt 为空白 → 视同没有，走 bm25 兜底
# ----------------------------------------------------------------------------
def test_blank_version_file_falls_through(tmp_path) -> None:
    """空文件没有信号价值：跳过它、改用 bm25 兜底。"""
    s = _settings(tmp_path)
    kb_version.kb_version_path(s).write_text("   \n", encoding="utf-8")
    s.bm25_index_path.write_text("{}", encoding="utf-8")
    assert kb_version.kb_version(s) != "unknown"
