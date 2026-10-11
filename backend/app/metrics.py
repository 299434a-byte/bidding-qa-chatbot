# ============================================================================
# 文件：metrics.py —— 问答的「可观测性指标」
#
# 【这个文件是干什么的】
#   每次有人提问，就顺手记一条「这次问答表现如何」的小账：
#   花了多少时间、第一段文字多久冒出来、命中没命中缓存、用没用重排、是哪个模式……
#   攒起来之后，summary() 就能算出「平均耗时」「缓存命中率」这类汇总数字，
#   让运营/开发者一眼看出系统健康状况（这叫「可观测性」）。
#
# 【和 answer_cache.py 一样的设计约束（硬要求）】
#   指标的读写失败必须「静默降级」。记指标是「给系统做体检」，属于旁路工作；
#   绝不能因为指标库坏了、被锁了，就把用户的正常问答拖垮或搞失败。
#
# 【为什么用 SQLite】与 conversations.db / answer_cache.db 同一思路：
#   标准库自带的轻量数据库，零额外服务、就是磁盘上一个文件。
#
# 【为什么要在意「不能拖慢主流程」（见 record 的注释）】
#   问答本身才是主任务；记指标是附带的。如果记指标很慢（比如等磁盘），
#   用户就会觉得「答完还卡一下」。所以写入要快、出错要静默、绝不阻塞返回。
# ============================================================================
"""问答可观测性指标（SQLite 本地文件）。

对外接口：
    · ``record(...)``  —— 记一条问答指标；失败静默返回 ``False``；
    · ``summary(...)`` —— 汇总统计：总数 / 平均最大耗时 / 平均首字延迟 /
      模式分布 / 缓存命中率 / 重排应用率与成功失败计数（含重排成功率）。

所有读写失败一律静默降级：指标是「旁路统计」，绝不能因为它而影响问答。
"""

from __future__ import annotations

# datetime as dt：生成记录时间戳，并可做「近 N 天」之类的按时间筛选（当前未用，留作扩展）。
import datetime as dt
# sqlite3：标准库自带的轻量数据库，指标就存在一个本地 .db 文件里。
import sqlite3
# threading：锁，保证「建表只做一次」，避免多线程同时执行建表语句打架。
import threading
# contextmanager：把「打开连接 / 用完关闭」包装成 `with` 可用的上下文管理器。
from contextlib import contextmanager
# Path：优雅地处理文件路径（自动建上级目录）。
from pathlib import Path
# 类型标注：Any=任意类型（汇总结果里值类型不一）；Iterator=可被逐个取值的对象。
from typing import Any, Iterator

# Settings/get_settings：读取 .env 配置（指标库路径）。
from .config import Settings, get_settings

# 建表语句（DDL）。CREATE TABLE IF NOT EXISTS —— 「不存在才建」，反复执行也安全。
# 【字段说明（大白话）】
#   id             —— 自增主键（SQLite 里 INTEGER PRIMARY KEY 会自动递增）；
#   mode           —— 本次问答的模式（llm / agent_rag / react / plan / deep / cache …）；
#   elapsed_ms     —— 端到端耗时（毫秒），从「收到请求」到「问答结束」；
#   first_token_ms —— 首字延迟（毫秒），从「收到请求」到「第一段文字开始冒」；
#                     某些请求可能没有任何文字输出（为 NULL），所以允许为空；
#   sources_count  —— 本次引用了几条参考来源；
#   cache_hit      —— 是否命中缓存（0/1，SQLite 没有专门的布尔类型，用整数表示）；
#   rerank_applied —— 本次是否应用了重排（0/1）；
#   rerank_failed  —— 本次是否「尝试了重排但失败降级」（0/1），与 applied 互斥；
#   rerank_skipped —— 本次是否「按需触发判定后主动跳过重排」（0/1），与 applied/failed 互斥；
#   created_at     —— 记录时间（UTC 的 ISO 文本）。
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

# 建表只需做一次；按库文件的绝对路径记录，避免每次请求都跑一遍 DDL。
_schema_ready: set[str] = set()
# 多线程同时进来时，用这把锁保证「只有一个线程在建表」。
_schema_lock = threading.Lock()


# 把「打开连接 / 用完关闭」包装成上下文管理器。
@contextmanager
def _connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    """打开一个 SQLite 连接（每次操作独立、用完即关）。"""
    # 取出指标库文件路径，并确保它所在的目录存在。
    path: Path = settings.metrics_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=3：被别的写操作挡住时最多等 3 秒，超时才报 database is locked。
    conn = sqlite3.connect(str(path), timeout=3)
    # row_factory=Row：查询结果可以像字典一样按列名取值（row["mode"]）。
    conn.row_factory = sqlite3.Row
    try:
        # WAL 模式：读写更少互相阻塞。
        conn.execute("PRAGMA journal_mode=WAL")
        # busy_timeout=3000：被占用时自动排队等待 3 秒，而不是立刻抛异常。
        conn.execute("PRAGMA busy_timeout=3000")
        yield conn
    finally:
        # 无论如何都关闭连接。
        conn.close()


def _ensure_schema(conn: sqlite3.Connection, path: Path) -> None:
    """确保表已建好（每个库文件只建一次）。"""
    key = str(path)
    if key in _schema_ready:
        return
    with _schema_lock:
        # 【双重检查】拿到锁后再确认一次：可能刚才别人已经建好了。
        if key in _schema_ready:
            return
        conn.execute(_DDL)
        # 【存量库补列】旧版本建的表没有 rerank_failed 列；CREATE TABLE IF NOT EXISTS
        #   只保证「表不存在才建」，不会给已有的表加新列，所以这里显式补一次。
        #   新库刚建就自带这列时，ALTER 会报「duplicate column name」——属预期分支。
        try:
            conn.execute(
                "ALTER TABLE qa_metrics ADD COLUMN rerank_failed INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            # 【静默】列已存在，说明这轮不用补，忽略即可。
            pass
        # 【存量库补列】同上：老表没有 rerank_skipped 列时补一列（新库建表时已带）。
        try:
            conn.execute(
                "ALTER TABLE qa_metrics ADD COLUMN rerank_skipped INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            # 【静默】列已存在，忽略。
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
    """记一条问答指标。成功返回 ``True``；任何失败静默返回 ``False``。

    【参数解释（都用关键字传，读起来更清楚）】
      · mode           —— 本次模式名（如 "llm" / "agent_rag" / "cache"）；
      · elapsed_ms     —— 端到端耗时（毫秒）；
      · first_token_ms —— 首字延迟（毫秒）；没有文字输出时传 ``None``；
      · sources_count  —— 引用来源条数；
      · cache_hit      —— 是否命中缓存；
      · rerank_applied —— 是否应用了重排；
      · rerank_failed  —— 是否「尝试了重排但失败降级」（静默降级也得留痕，否则永远看不见）；
      · rerank_skipped —— 是否「按需触发判定后主动跳过重排」（与 applied/failed 互斥）。

    【为什么整个函数包 try/except（对应硬要求）】
      记指标是「旁路统计」。万一磁盘满了、库被锁了，也只是「这条统计丢了」，
      绝不能让抛出的异常往上冒、导致用户的问答请求失败。所以出错一律吞掉。
    """
    s = settings or get_settings()
    # 【静默降级】任何异常都吞掉，不干扰问答主流程。
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
                    # 耗时统一取「非负浮点」；传进来 None/负数时兜底成 0。
                    max(0.0, float(elapsed_ms or 0.0)),
                    # 首字延迟允许为空（没文字输出时就是空）。
                    None if first_token_ms is None else max(0.0, float(first_token_ms)),
                    int(sources_count or 0),
                    # 布尔转成 0/1 存进整数列。
                    1 if cache_hit else 0,
                    1 if rerank_applied else 0,
                    1 if rerank_failed else 0,
                    1 if rerank_skipped else 0,
                    _now_iso(),
                ),
            )
            # 提交事务（不提交的话数据不会真正落盘）。
            conn.commit()
        return True
    except Exception:
        # 【关键】记指标失败静默吞掉，绝不影响问答。
        return False


def summary(settings: Settings | None = None) -> dict[str, Any]:
    """汇总指标，返回一个字典（全部失败时返回「空统计」，而不是抛异常）。

    返回字段（大白话）：
      · ``total``               —— 一共记录了多少次问答；
      · ``avg_elapsed_ms``      —— 平均耗时（毫秒，保留一位小数）；
      · ``max_elapsed_ms``      —— 最大耗时（毫秒，保留一位小数）；
      · ``avg_first_token_ms``  —— 平均首字延迟（毫秒；没有首字数据的请求不计入）；
      · ``mode_distribution``   —— 各模式各占多少次（如 {"llm": 7, "cache": 3}）；
      · ``cache_hit_rate``      —— 缓存命中率（0~1 的小数，保留四位）；
      · ``rerank_applied_rate`` —— 重排应用率（0~1 的小数，保留四位）；
      · ``rerank_applied``      —— 重排成功的次数（计数）；
      · ``rerank_failed``       —— 重排失败降级的次数（计数）；
      · ``rerank_skipped``      —— 按需触发判定后主动跳过重排的次数（计数）；
      · ``rerank_success_rate`` —— 重排成功率 = 成功 /（成功 + 失败），
        分母只算「真正尝试过重排」的问答；一次都没尝试的请求不进分母。
    """
    s = settings or get_settings()
    # 先准备一份「空统计」作为兜底；一旦下面出错就返回它。
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
    # 【静默降级】读指标失败时返回空统计（接口照常可用，只是数字是 0）。
    try:
        with _connect(s) as conn:
            _ensure_schema(conn, s.metrics_db_path)
            # 一次聚合查询把所有常用数字算出来。
            # 【SQL 逐段解释】
            #   COUNT(*)                  —— 总条数；
            #   AVG(elapsed_ms)           —— 平均耗时；
            #   MAX(elapsed_ms)           —— 最大耗时；
            #   AVG(first_token_ms)       —— 平均首字延迟（SQLite 的 AVG 会自动跳过 NULL 行）；
            #   SUM(cache_hit)            —— 命中的条数（0/1 相加），除以总数就是命中率；
            #   SUM(rerank_applied)       —— 应用重排的条数；
            #   SUM(rerank_failed)        —— 重排失败降级的条数（成功率的分母的一部分）。
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
            # 按模式分组计数（例如 llm 有多少次、cache 有多少次）。
            mode_rows = conn.execute(
                "SELECT mode, COUNT(*) AS cnt FROM qa_metrics GROUP BY mode"
            ).fetchall()
        # 取出总数（转成 int）。
        total = int(row["total"] or 0)
        # 把各模式计数整理成字典 {模式名: 次数}。
        mode_distribution = {str(r["mode"]): int(r["cnt"]) for r in mode_rows}
        # 命中率 / 重排率：没有数据时记 0，避免除以 0。
        hits = int(row["hits"] or 0)
        reranks = int(row["reranks"] or 0)
        rerank_fails = int(row["rerank_fails"] or 0)
        cache_hit_rate = round(hits / total, 4) if total else 0.0
        rerank_applied_rate = round(reranks / total, 4) if total else 0.0
        # 重排成功率：分母只算「真正尝试过重排」的问答（成功 + 失败）；
        # 缓存命中、候选不足没尝试的请求不进分母，否则会把成功率虚高地摊薄。
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
        # 【关键】读指标失败时返回空统计，接口不报错。
        return empty