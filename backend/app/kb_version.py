# ============================================================================
# 文件：kb_version.py —— 知识库「版本指纹」（缓存失效的判据）
#
# 【这个文件是干什么的】
#   问答缓存（answer_cache）里存的是「某个问题在某个知识库状态下的答案」。
#   知识库一旦重建 / 增量更新，旧答案就可能是错的（引用的公告变了、分数变了）。
#   所以缓存必须「跟着知识库版本走」：版本一变，旧缓存立即全部失效。
#   本文件负责回答一个问题：**现在知识库是什么版本？**
#
# 【为什么要「读取时绝不开 Qdrant」（硬约束）】
#   本地模式下 Qdrant 是「目录独占」的：服务运行时向量库文件已被进程锁住，
#   请求期间再开一个客户端去问「有多少点」轻则报错、重则把库搞坏。
#   所以版本信号只允许来自**廉价的本地文件**，绝不连向量库、绝不连网络。
#
# 【两条廉价信号（按优先级）】
#   ① 首选 data/kb_version.txt —— 由 ingest 在 full / incremental **成功结束时**写入，
#      内容含 ISO 时间、点数、added/updated/deleted。任何一次成功建库都会改写它
#      → 版本号必变 → 旧缓存全部失效（这正是我们要的效果）。
#   ② 兜底 data/bm25_index.json 的 mtime + size —— 老环境可能还没有 kb_version.txt；
#      BM25 索引每次建库都会重写，它的「修改时间 + 文件大小」足以感知知识库变了没有。
#
# 【失败怎么办】任何一步出错都降级成 "unknown"，绝不抛异常：
#   版本指纹只是缓存的判据，不能因为它坏了而让问答挂掉（与缓存本身的静默降级一致）。
# ============================================================================
"""知识库版本指纹：kb_version(settings) -> 8~12 位短 hash；读失败返回 "unknown"。"""

from __future__ import annotations

# hashlib：把版本信号文本压成固定长度的短指纹（sha256 取前若干位）。
import hashlib
# datetime：写 kb_version.txt 时记录 ISO 时间（UTC）。
import datetime as dt
# Path：定位 data/kb_version.txt、data/bm25_index.json。
from pathlib import Path

# Settings：配置类型（只需要文件路径字段，测试里可用轻量替身）。
from .config import Settings

# 指纹长度：8~12 位短 hash（取 10 位：足够区分版本、又方便日志里一眼读完）。
_HASH_LEN = 10
# 读不到任何信号时的哨兵值（也算一个合法的「版本」，只是所有环境共享它）。
UNKNOWN = "unknown"


def kb_version_path(settings: Settings) -> Path:
    """data/kb_version.txt 的路径 —— 与 BM25 索引同目录（就是项目的 data/）。"""
    # 用 bm25_index_path 的父目录推导：它一定是数据目录，且测试替身只需给这一个字段。
    base = getattr(settings, "bm25_index_path", None)
    parent = Path(base).parent if base else Path("data")
    return parent / "kb_version.txt"


def _fingerprint(signal: bytes) -> str:
    """把版本信号压成 10 位短 hash（确定性：同样的信号永远得到同样的版本号）。"""
    return hashlib.sha256(signal).hexdigest()[:_HASH_LEN]


def kb_version(settings: Settings) -> str:
    """读取当前知识库版本指纹（8~12 位短 hash）；任何失败都返回 "unknown"。

    【性能】只读一个小文本 / 一次 stat()，微秒级；请求路径上每次问答都可负担。
    【顺序】先看 kb_version.txt（首选）；没有再退到 bm25_index.json 的 mtime+size；
    两条都没有 → "unknown"。
    """
    try:
        # ---------- ① 首选：ingest 成功时写下的版本文件 ----------
        version_file = kb_version_path(settings)
        if version_file.is_file():
            raw = version_file.read_bytes().strip()
            if raw:
                return _fingerprint(b"kb_version.txt:" + raw)
        # ---------- ② 兜底：BM25 索引的 mtime + size ----------
        bm25_path = getattr(settings, "bm25_index_path", None)
        if bm25_path:
            stat = Path(bm25_path).stat()
            # st_mtime_ns 是纳秒级修改时间；size 是字节数。两者组合即可感知「重建过」。
            signal = f"bm25:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
            return _fingerprint(signal)
    except Exception:
        # 【静默降级】文件不存在/没权限/路径坏掉……一律返回 unknown，绝不抛。
        pass
    return UNKNOWN


def write_kb_version(
    settings: Settings,
    *,
    points: int | None = None,
    added: int | None = None,
    updated: int | None = None,
    deleted: int | None = None,
) -> str:
    """ingest 成功结束时调用：把本次报告数字写进 data/kb_version.txt，返回新版本号。

    内容含 ISO 时间（UTC）、点数、added/updated/deleted —— 任何一次成功建库
    都会改写文件内容（ISO 时间必变），版本指纹随之改变，旧缓存立即全部失效。
    """
    content = (
        f"time={dt.datetime.now(dt.timezone.utc).isoformat()}\n"
        f"points={points}\n"
        f"added={added}\n"
        f"updated={updated}\n"
        f"deleted={deleted}\n"
    )
    path = kb_version_path(settings)
    # 确保目录存在（新建 data/ 的环境）。
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    # 立刻回读算一次：调用方拿到的就是「写完之后」的版本号。
    return kb_version(settings)
