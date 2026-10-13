"""answer_cache：问答缓存（SQLite 文件，键 = 规范化问题 + mode + top_k）。

职责：
  - 规范化问题文本并生成缓存键（含问答模式与 top_k，避免跨模式串答案）
  - 读写缓存，fail-open：任何存储异常都不影响问答主流程
  - 按知识库版本指纹判定旧条目失效；未登录请求不进缓存

依赖：config、kb_version
被谁调用：main（/api/chat、/api/agent/chat 的缓存读写）
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings, get_settings
from . import kb_version

_DDL = """
CREATE TABLE IF NOT EXISTS answer_cache (
    cache_key    TEXT PRIMARY KEY,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]',
    mode         TEXT NOT NULL DEFAULT 'llm',
    created_at   TEXT NOT NULL,
    kb_version   TEXT NOT NULL DEFAULT 'unknown'
);
"""

_ALTER_ADD_KB_VERSION = "ALTER TABLE answer_cache ADD COLUMN kb_version TEXT NOT NULL DEFAULT 'unknown'"

_schema_ready: set[str] = set()
_schema_lock = threading.Lock()

_SENSITIVE_RE = re.compile(
    r"密码|口令|passwd|password|api[\s_-]?key|apikey|secret|token|身份证|银行卡|信用卡",
    re.IGNORECASE,
)


def normalize_question(question: str) -> str:
    """把问题规范化成「缓存键」。"""
    text = question or ""
    text = unicodedata.normalize("NFKC", text)
    text = text.strip()
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


logger = logging.getLogger(__name__)


_KEY_SEP = "\x1f"


def cache_key(question: str, *, mode: str = "react", top_k: int | None = None) -> str:
    """缓存主键 = ``normalize_question(question)`` + 请求模式 + top_k。"""
    q = normalize_question(question)
    m = (mode or "react").strip().lower() or "react"
    try:
        k = int(top_k) if top_k else 0
    except (TypeError, ValueError):
        k = 0
    if k < 0:
        k = 0
    return f"{q}{_KEY_SEP}{m}{_KEY_SEP}{k}"


def looks_sensitive(question: str) -> bool:
    """粗筛：问题里是否疑似含敏感信息（见 ``_SENSITIVE_RE``）。"""
    return bool(_SENSITIVE_RE.search(question or ""))


def is_cacheable(
    *,
    logged_in: bool,
    mode: str = "react",
    has_history: bool = False,
    web_search: bool = False,
    has_image: bool = False,
    question: str = "",
) -> bool:
    """判断「这个请求该不该进缓存」（读缓存与写缓存共用同一条准绳）。"""
    if not logged_in:
        return False
    if mode == "deep":
        return False
    if has_history:
        return False
    if web_search:
        return False
    if has_image:
        return False
    if looks_sensitive(question):
        return False
    return True


def is_cache_readable(
    *,
    logged_in: bool,
    mode: str = "react",
    has_image: bool = False,
    question: str = "",
) -> bool:
    """读缓存的准绳（写入侧仍用 :func:`is_cacheable` —— 读写两把尺子刻意不对称）。"""
    if not logged_in:
        return False
    if mode == "deep":
        return False
    if has_image:
        return False
    if looks_sensitive(question):
        return False
    return True


@contextmanager
def _connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    """打开一个 SQLite 连接（每次操作独立、用完即关）。"""
    path: Path = settings.cache_db_path
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
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(answer_cache)")}
        if "kb_version" not in cols:
            conn.execute(_ALTER_ADD_KB_VERSION)
        conn.commit()
        _schema_ready.add(key)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 文本，作为 created_at。统一用 UTC，避免时区换算歧义。"""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(text: str) -> dt.datetime | None:
    """把 created_at 文本解析回时间对象；解析不了返回 None。"""
    try:
        value = dt.datetime.fromisoformat(text)
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value
    except Exception:
        return None


def get_cached(
    question: str,
    settings: Settings | None = None,
    *,
    mode: str = "react",
    top_k: int | None = None,
) -> dict[str, Any] | None:
    """查缓存。命中且未过期返回结果字典；未命中/过期/出错一律返回 ``None``。"""
    s = settings or get_settings()
    try:
        if not normalize_question(question):
            return None
        key = cache_key(question, mode=mode, top_k=top_k)
        with _connect(s) as conn:
            _ensure_schema(conn, s.cache_db_path)
            row = conn.execute(
                "SELECT question, answer, sources_json, mode, created_at "
                "FROM answer_cache WHERE cache_key = ? AND kb_version = ?",
                (key, kb_version.kb_version(s)),
            ).fetchone()
        if row is None:
            return None
        created = _parse_iso(row["created_at"])
        if created is None:
            logger.warning(
                "answer_cache: created_at 解析失败，按未命中处理（question=%r created_at=%r）",
                row["question"],
                row["created_at"],
            )
            return None
        age_seconds = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
        if age_seconds > s.cache_ttl:
            return None
        try:
            sources = json.loads(row["sources_json"] or "[]")
        except Exception:
            sources = []
        return {
            "question": row["question"],
            "answer": row["answer"],
            "sources": sources if isinstance(sources, list) else [],
            "mode": row["mode"],
            "created_at": row["created_at"],
        }
    except Exception:
        return None


def set_cached(
    question: str,
    answer: str,
    sources: list[dict[str, Any]] | None,
    mode: str,
    settings: Settings | None = None,
    *,
    key_mode: str | None = None,
    top_k: int | None = None,
) -> bool:
    """写缓存。成功返回 ``True``；任何失败静默返回 ``False``（不影响问答）。"""
    s = settings or get_settings()
    try:
        if not normalize_question(question) or not (answer or "").strip():
            return False
        key = cache_key(question, mode=key_mode or mode, top_k=top_k)
        sources_json = json.dumps(sources or [], ensure_ascii=False)
        with _connect(s) as conn:
            _ensure_schema(conn, s.cache_db_path)
            conn.execute(
                "INSERT OR REPLACE INTO answer_cache "
                "(cache_key, question, answer, sources_json, mode, created_at, kb_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, question, answer, sources_json, mode or "llm", _now_iso(), kb_version.kb_version(s)),
            )
            conn.commit()
        return True
    except Exception:
        return False


def clear(settings: Settings | None = None) -> bool:
    """手动清空全部问答缓存。成功返回 True；失败静默返回 False（不影响问答）。"""
    s = settings or get_settings()
    try:
        with _connect(s) as conn:
            _ensure_schema(conn, s.cache_db_path)
            conn.execute("DELETE FROM answer_cache")
            conn.commit()
        return True
    except Exception:
        return False