"""metrics：问答可观测性指标（SQLite 落盘：耗时、缓存命中、重排统计、可信度）。

职责：
  - 记录每次问答的总耗时、首 token、模式、重排结果、逐句可信度
  - 汇总为 /api/metrics 的均值 / 中位数 / 命中率 / avg_confidence
  - 事件计数（qa_event_counters：如 forced_finalize 强制收口触发次数）
  - 写失败静默降级（不影响问答）

依赖：config
被谁调用：main（问答端点打点、/api/metrics 汇总）
"""

from __future__ import annotations

import datetime as dt
import json
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

# 事件计数表：记录低频但需要观测频率的事件（如 forced_finalize 强制收口触发次数）。
# 与 qa_metrics 分开：一次问答只记一条 qa_metrics，但同一事件可在其他维度独立计数。
_COUNTER_DDL = """
CREATE TABLE IF NOT EXISTS qa_event_counters (
    name TEXT PRIMARY KEY,
    hits INTEGER NOT NULL DEFAULT 0
);
"""

# 深度模式「预算四本账」消耗表：一次深度问答记一条（工具/检索/LLM 次数 + 耗时 + 超限项）。
# 与 qa_metrics 分开：只有 deep/plan 模式记账，普通问答不掺水平均值。
_BUDGET_DDL = """
CREATE TABLE IF NOT EXISTS qa_budget_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL DEFAULT 'deep',
    tool_calls  INTEGER NOT NULL DEFAULT 0,
    retrievals  INTEGER NOT NULL DEFAULT 0,
    llm_calls   INTEGER NOT NULL DEFAULT 0,
    elapsed_ms  REAL NOT NULL DEFAULT 0,
    exceeded    TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL
);
"""

# summary.budget.exceeded 里输出的账本名（与 deep_agent._Budget 的 exceeded 键同名）。
BUDGET_ACCOUNTS = ("tool_calls", "retrievals", "llm_calls", "time_seconds")

# summary 里输出的事件计数名（顶层字段与 key 同名）。
COUNTER_NAMES = (
    "forced_finalize",
    "coverage_refused",
    "coverage_pass",
    "no_gain_stop",
    "followup_reused",
    "retrieval_skipped",
)

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
        conn.execute(_COUNTER_DDL)
        conn.execute(_BUDGET_DDL)
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
        try:
            # 逐句可信度（0~1 的浮点；NULL = 本轮没有 citation_check 事件，不参与均值）。
            conn.execute("ALTER TABLE qa_metrics ADD COLUMN confidence REAL")
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
    confidence: float | None = None,
    settings: Settings | None = None,
) -> bool:
    """记一条问答指标。成功返回 ``True``；任何失败静默返回 ``False``。

    ``confidence`` 是本轮 citation_check 事件里的逐句可信度（0~1）；
    传 ``None`` 表示本轮没有可信度结论（如缓存命中 / 直答通道），不参与均值。
    """
    s = settings or get_settings()
    try:
        with _connect(s) as conn:
            _ensure_schema(conn, s.metrics_db_path)
            conn.execute(
                "INSERT INTO qa_metrics "
                "(mode, elapsed_ms, first_token_ms, sources_count, cache_hit, "
                "rerank_applied, rerank_failed, rerank_skipped, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mode or "llm",
                    max(0.0, float(elapsed_ms or 0.0)),
                    None if first_token_ms is None else max(0.0, float(first_token_ms)),
                    int(sources_count or 0),
                    1 if cache_hit else 0,
                    1 if rerank_applied else 0,
                    1 if rerank_failed else 0,
                    1 if rerank_skipped else 0,
                    None if confidence is None else round(float(confidence), 6),
                    _now_iso(),
                ),
            )
            conn.commit()
        return True
    except Exception:
        return False


def record_budget(
    *,
    mode: str,
    tool_calls: int,
    retrievals: int,
    llm_calls: int,
    elapsed_ms: float,
    exceeded: list[str] | tuple[str, ...] = (),
    settings: Settings | None = None,
) -> bool:
    """记一条深度模式预算消耗。成功返回 ``True``；任何失败静默返回 ``False``。"""
    s = settings or get_settings()
    try:
        with _connect(s) as conn:
            _ensure_schema(conn, s.metrics_db_path)
            conn.execute(
                "INSERT INTO qa_budget_metrics "
                "(mode, tool_calls, retrievals, llm_calls, elapsed_ms, exceeded, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    mode or "deep",
                    max(0, int(tool_calls or 0)),
                    max(0, int(retrievals or 0)),
                    max(0, int(llm_calls or 0)),
                    max(0.0, float(elapsed_ms or 0.0)),
                    json.dumps(list(exceeded or []), ensure_ascii=False),
                    _now_iso(),
                ),
            )
            conn.commit()
        return True
    except Exception:
        return False


def bump(name: str, *, settings: Settings | None = None) -> bool:
    """事件计数 +1（如 forced_finalize）。成功返回 ``True``；任何失败静默返回 ``False``。"""
    if name not in COUNTER_NAMES:
        return False
    s = settings or get_settings()
    try:
        with _connect(s) as conn:
            _ensure_schema(conn, s.metrics_db_path)
            conn.execute(
                "INSERT INTO qa_event_counters (name, hits) VALUES (?, 1) "
                "ON CONFLICT(name) DO UPDATE SET hits = hits + 1",
                (name,),
            )
            conn.commit()
        return True
    except Exception:
        return False


def summary(settings: Settings | None = None) -> dict[str, Any]:
    """汇总指标，返回一个字典（全部失败时返回「空统计」，而不是抛异常）。"""
    s = settings or get_settings()
    empty_budget: dict[str, Any] = {
        "samples": 0,
        "avg_tool_calls": 0.0,
        "avg_retrievals": 0.0,
        "avg_llm_calls": 0.0,
        "avg_elapsed_ms": 0.0,
        "exceeded": {name: 0 for name in BUDGET_ACCOUNTS},
    }
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
        "avg_confidence": 0.0,
        "low_confidence_count": 0,
        "forced_finalize": 0,
        "coverage_refused": 0,
        "coverage_pass": 0,
        "no_gain_stop": 0,
        "followup_reused": 0,
        "retrieval_skipped": 0,
        "budget": empty_budget,
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
                "COALESCE(SUM(rerank_skipped), 0) AS rerank_skips, "
                "COALESCE(AVG(CASE WHEN confidence IS NOT NULL THEN confidence END), 0) AS avg_conf, "
                "COALESCE(SUM(CASE WHEN confidence IS NOT NULL AND confidence < 0.5 THEN 1 ELSE 0 END), 0) AS low_conf "
                "FROM qa_metrics"
            ).fetchone()
            mode_rows = conn.execute(
                "SELECT mode, COUNT(*) AS cnt FROM qa_metrics GROUP BY mode"
            ).fetchall()
            counter_rows = conn.execute(
                "SELECT name, hits FROM qa_event_counters"
            ).fetchall()
            budget_row = conn.execute(
                "SELECT COUNT(*) AS samples, "
                "COALESCE(AVG(tool_calls), 0) AS avg_tools, "
                "COALESCE(AVG(retrievals), 0) AS avg_retrievals, "
                "COALESCE(AVG(llm_calls), 0) AS avg_llms, "
                "COALESCE(AVG(elapsed_ms), 0) AS avg_elapsed "
                "FROM qa_budget_metrics"
            ).fetchone()
            budget_exceeded_rows = conn.execute(
                "SELECT exceeded, COUNT(*) AS cnt FROM qa_budget_metrics GROUP BY exceeded"
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
        counters = {str(r["name"]): int(r["hits"] or 0) for r in counter_rows}
        budget_samples = int(budget_row["samples"] or 0)
        exceeded_counts = {name: 0 for name in BUDGET_ACCOUNTS}
        for r in budget_exceeded_rows:
            try:
                names = json.loads(r["exceeded"] or "[]")
            except Exception:
                names = []
            for name in names:
                if name in exceeded_counts:
                    exceeded_counts[name] += int(r["cnt"] or 0)
        budget = {
            "samples": budget_samples,
            "avg_tool_calls": round(float(budget_row["avg_tools"] or 0.0), 2),
            "avg_retrievals": round(float(budget_row["avg_retrievals"] or 0.0), 2),
            "avg_llm_calls": round(float(budget_row["avg_llms"] or 0.0), 2),
            "avg_elapsed_ms": round(float(budget_row["avg_elapsed"] or 0.0), 1),
            "exceeded": exceeded_counts,
        }
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
            # 平均逐句可信度（只统计有 citation_check 结论的问答）+ 低可信（<0.5）次数
            "avg_confidence": round(float(row["avg_conf"] or 0.0), 4),
            "low_confidence_count": int(row["low_conf"] or 0),
            "forced_finalize": counters.get("forced_finalize", 0),
            "coverage_refused": counters.get("coverage_refused", 0),
            "coverage_pass": counters.get("coverage_pass", 0),
            "no_gain_stop": counters.get("no_gain_stop", 0),
            "followup_reused": counters.get("followup_reused", 0),
            # Self-RAG 检索判定：寒暄类问题跳过检索的次数（retrieval.meta.skipped_retrieval）
            "retrieval_skipped": counters.get("retrieval_skipped", 0),
            "budget": budget,
        }
    except Exception:
        return empty