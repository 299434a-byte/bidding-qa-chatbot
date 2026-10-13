"""kb_version：知识库版本指纹（8~12 位 hash，问答缓存失效的依据）。

职责：
  - 按当前配置与数据源状态计算稳定指纹
  - ingest 成功后写入 data/kb_version.txt
  - 读取失败一律返回 "unknown"（不抛错）

依赖：config
被谁调用：answer_cache（旧缓存判定失效）、ingest（建库后写入）、main（状态展示）
"""

from __future__ import annotations

import hashlib
import datetime as dt
from pathlib import Path

from .config import Settings

_HASH_LEN = 10
UNKNOWN = "unknown"


def kb_version_path(settings: Settings) -> Path:
    """data/kb_version.txt 的路径 —— 与 BM25 索引同目录（就是项目的 data/）。"""
    base = getattr(settings, "bm25_index_path", None)
    parent = Path(base).parent if base else Path("data")
    return parent / "kb_version.txt"


def _fingerprint(signal: bytes) -> str:
    """把版本信号压成 10 位短 hash（确定性：同样的信号永远得到同样的版本号）。"""
    return hashlib.sha256(signal).hexdigest()[:_HASH_LEN]


def kb_version(settings: Settings) -> str:
    """读取当前知识库版本指纹（8~12 位短 hash）；任何失败都返回 "unknown"。"""
    try:
        version_file = kb_version_path(settings)
        if version_file.is_file():
            raw = version_file.read_bytes().strip()
            if raw:
                return _fingerprint(b"kb_version.txt:" + raw)
        bm25_path = getattr(settings, "bm25_index_path", None)
        if bm25_path:
            stat = Path(bm25_path).stat()
            signal = f"bm25:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
            return _fingerprint(signal)
    except Exception:
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
    """ingest 成功结束时调用：把本次报告数字写进 data/kb_version.txt，返回新版本号。"""
    content = (
        f"time={dt.datetime.now(dt.timezone.utc).isoformat()}\n"
        f"points={points}\n"
        f"added={added}\n"
        f"updated={updated}\n"
        f"deleted={deleted}\n"
    )
    path = kb_version_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return kb_version(settings)
