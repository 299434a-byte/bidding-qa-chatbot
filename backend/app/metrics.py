"""metrics：问答可观测性指标（SQLite 落盘：耗时、缓存命中、重排统计）。

职责：
  - 记录每次问答的总耗时、首 token、模式、重排结果
  - 汇总为 /api/metrics 的均值 / 中位数 / 命中率
  - 写失败静默降级（不影响问答）

依赖：config
被谁调用：main（问答端点打点、/api/metrics 汇总）
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings, get_settings

_DDL = """
CREATE TABLE IF NOT EXISTS qa_metrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mode           TEXT NOT NULL DEFAULT 'llm',
    elapsed_ms     REAL NOT NULL DEFAULT 0,
    first_token_ms REAL,
    sources_count  INTEGER NOT NULL DEFAULT 0,
    cache_hit      INTEGER NOT NULL DEFAULT 0,
    rerank_applied INTEGER NOT NULL DEFAULT 0,
    rerank_failed  INTEGER NOT NULL DEFAULT 0,
    rerank_skipped INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
"""

_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


@contextmanager
def _connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    """打开一个 SQLite 连接（每次操作独立、用完即关）。"""
    path: Path = settings.metrics_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection, path: Path) -> None:
    """确保表已建好（每个库文件只建一次）。"""
    key = str(path)
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        conn.execute(_DDL)
        try:
            conn.execute(
                "ALTER TABLE qa_metrics ADD COLUMN rerank_failed INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        try:
            conn.execute(
                "ALTER TABLE qa_metrics ADD COLUMN rerank_skipped INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        conn.commit()
        _schema_ready.add(key)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 文本，作为 created_at。统一用 UTC，避免时区换算歧义。"""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def record(
    *,
    mode: str,
    elapsed_ms: float,
    first_token_ms: float | None = None,
    sources_count: int = 0,
    cache_hit: bool = False,
    rerank_applied: bool = False,
    rerank_failed: bool = False,
    rerank_skipped: bool = False,
    settings: Settings | None = None,
) -> bool:
    """记一条问答指标。成功返回 ``True``；任何失败静默返回 ``False``。"""
    s = settings or get_settings()
    try:
        with _connect(s) as conn:
            _ensure_schema(conn, s.metrics_db_path)
            conn.execute(
                "INSERT INTO qa_metrics "
                "(mode, elapsed_ms, first_token_ms, sources_count, cache_hit, "
                "rerank_applied, rerank_failed, rerank_skipped, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mode or "llm",
                    max(0.0, float(elapsed_ms or 0.0)),
                    None if first_token_ms is None else max(0.0, float(first_token_ms)),
                    int(sources_count or 0),
                    1 if cache_hit else 0,
                    1 if rerank_applied else 0,
                    1 if rerank_failed else 0,
                    1 if rerank_skipped else 0,
                    _now_iso(),
                ),
            )
            conn.commit()
        return True
    except Exception:
        return False


def summary(settings: Settings | None = None) -> dict[str, Any]:
    """汇总指标，返回一个字典（全部失败时返回「空统计」，而不是抛异常）。"""
    s = settings or get_settings()
    empty: dict[str, Any] = {
        "total": 0,
        "avg_elapsed_ms": 0.0,
        "max_elapsed_ms": 0.0,
        "avg_first_token_ms": 0.0,
        "mode_distribution": {},
        "cache_hit_rate": 0.0,
        "rerank_applied_rate": 0.0,
        "rerank_applied": 0,
        "rerank_failed": 0,
        "rerank_skipped": 0,
        "rerank_success_rate": 0.0,
    }
    try:
        with _connect(s) as conn:
            _ensure_schema(conn, s.metrics_db_path)
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(AVG(elapsed_ms), 0) AS avg_elapsed, "
                "COALESCE(MAX(elapsed_ms), 0) AS max_elapsed, "
                "COALESCE(AVG(first_token_ms), 0) AS avg_first_token, "
                "COALESCE(SUM(cache_hit), 0) AS hits, "
                "COALESCE(SUM(rerank_applied), 0) AS reranks, "
                "COALESCE(SUM(rerank_failed), 0) AS rerank_fails, "
                "COALESCE(SUM(rerank_skipped), 0) AS rerank_skips "
                "FROM qa_metrics"
            ).fetchone()
            mode_rows = conn.execute(
                "SELECT mode, COUNT(*) AS cnt FROM qa_metrics GROUP BY mode"
            ).fetchall()
        total = int(row["total"] or 0)
        mode_distribution = {str(r["mode"]): int(r["cnt"]) for r in mode_rows}
        hits = int(row["hits"] or 0)
        reranks = int(row["reranks"] or 0)
        rerank_fails = int(row["rerank_fails"] or 0)
        cache_hit_rate = round(hits / total, 4) if total else 0.0
        rerank_applied_rate = round(reranks / total, 4) if total else 0.0
        attempted = reranks + rerank_fails
        rerank_success_rate = round(reranks / attempted, 4) if attempted else 0.0
        return {
            "total": total,
            "avg_elapsed_ms": round(float(row["avg_elapsed"] or 0.0), 1),
            "max_elapsed_ms": round(float(row["max_elapsed"] or 0.0), 1),
            "avg_first_token_ms": round(float(row["avg_first_token"] or 0.0), 1),
            "mode_distribution": mode_distribution,
            "cache_hit_rate": cache_hit_rate,
            "rerank_applied_rate": rerank_applied_rate,
            "rerank_applied": reranks,
            "rerank_failed": rerank_fails,
            "rerank_skipped": int(row["rerank_skips"] or 0),
            "rerank_success_rate": rerank_success_rate,
        }
    except Exception:
        return empty