"""history：对话历史持久化（SQLite + FTS5 全文检索，按用户分区）。

职责：
  - 会话 / 消息 CRUD 与 schema 迁移（建表、分词词表升级）
  - FTS5 全文检索 + 时间 / 模式 / 主题多维过滤
  - 消息预览高亮（<em>）与会话标题自动生成

依赖：config、bm25（FTS 分词）、timeutil（时间口径）
被谁调用：main（/api/history/* 端点）、auth（按用户校验分区）
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .bm25 import tokenize
from . import timeutil
from .config import Settings, get_settings

_PREVIEW_BEFORE = 40
_PREVIEW_AFTER = 120
DEFAULT_CONVERSATION_TITLE = "新对话"

_TERMS_VERSION = 2

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


class HistoryUnavailable(RuntimeError):
    """未开启对话历史（HISTORY_ENABLED / AUTH_ENABLED 未同时为真），上层应返回 503。"""


class HistoryConflict(RuntimeError):
    """唯一约束冲突，例如用户名已存在。"""


class HistoryNotFound(RuntimeError):
    """目标不存在，**或不属于当前用户** —— 两种情况一律当作不存在，不泄露存在性。"""


_schema_ready: set[str] = set()
_schema_lock = threading.Lock()
_fts_supported: dict[str, bool] = {}


def _require_enabled(settings: Settings) -> None:
    if not settings.history_configured:
        raise HistoryUnavailable(
            "对话历史未开启（需 HISTORY_ENABLED=true 且 AUTH_ENABLED=true），"
            "请在项目根目录的 .env 中检查配置"
        )


@contextmanager
def _connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    """打开一个连接（每次操作独立，用完即关）。"""
    path: Path = settings.history_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    iterations    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_user ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'react',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conv_user_time ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    seg             TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    meta            TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_seq ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    seg,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, seg) VALUES (new.rowid, new.seg);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, seg) VALUES ('delete', old.rowid, old.seg);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, seg) VALUES ('delete', old.rowid, old.seg);
    INSERT INTO messages_fts(rowid, seg) VALUES (new.rowid, new.seg);
END;
"""


def _migrate_terms(conn: sqlite3.Connection) -> None:
    """按当前分词口径重算全部消息的 ``seg``，并重建 FTS 索引。"""
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'terms_version'").fetchone()
    current = int(row["value"]) if row is not None and str(row["value"]).isdigit() else 0
    if current == _TERMS_VERSION:
        return
    rows = conn.execute("SELECT id, content FROM messages").fetchall()
    for r in rows:
        conn.execute(
            "UPDATE messages SET seg = ? WHERE id = ?",
            (" ".join(_index_terms(r["content"])), r["id"]),
        )
    if rows:
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('terms_version', ?)",
        (str(_TERMS_VERSION),),
    )


def ensure_schema(settings: Settings | None = None) -> None:
    """建表（幂等）。FTS5 不可用时自动降级，搜索改走 LIKE。"""
    s = settings or get_settings()
    _require_enabled(s)
    key = str(s.history_db_path)
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        with _connect(s) as conn, conn:
            conn.executescript(_DDL)
            try:
                conn.executescript(_FTS_DDL)
                _fts_supported[key] = True
                _migrate_terms(conn)
            except sqlite3.OperationalError:
                _fts_supported[key] = False
        _schema_ready.add(key)


def fts_available(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    ensure_schema(s)
    return bool(_fts_supported.get(str(s.history_db_path), False))


def _now() -> str:
    """本地时间字符串，与项目其它模块（日志、建库）保持一致。"""
    return timeutil.now_iso()


def _new_id() -> str:
    return uuid.uuid4().hex


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _load_json(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def _terms(text: str, *, unigrams: bool) -> list[str]:
    """生成 FTS 词元：jieba 词 + CJK 单字 / 二字组合（各自去重、保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for token in tokenize(text):
        candidates = [token]
        for run in _CJK_RE.findall(token):
            if unigrams:
                candidates.extend(run)
            candidates.extend(run[i : i + 2] for i in range(len(run) - 1))
        for cand in candidates:
            if cand and cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def _index_terms(text: str) -> list[str]:
    """写入 ``messages.seg`` 的词元（含单字，保证单字查询也能命中）。"""
    return _terms(text, unigrams=True)


def _query_terms(text: str) -> list[str]:
    """查询词元（不含单字，避免把结果放得过宽）。"""
    return _terms(text, unigrams=False)


def title_from_question(question: str, limit: int) -> str:
    """用首个提问生成会话标题：压平换行、截断并加省略号。"""
    text = " ".join((question or "").split())
    if not text:
        return DEFAULT_CONVERSATION_TITLE
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def _to_utc_bound(text: str) -> str:
    moment = timeutil.parse_ts(text)
    if moment is None:
        return text
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _legacy_to_utc_modifier() -> str:
    minutes = -timeutil.LEGACY_TZ.utcoffset(None).total_seconds() / 60
    return f"{minutes:g} minutes"


def _created_at_utc_sql(column: str = "m.created_at") -> str:
    shift = _legacy_to_utc_modifier()
    return (
        "COALESCE("
        f"CASE WHEN {column} GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR {column} GLOB '*Z' "
        f"THEN datetime(replace({column},'T',' ')) "
        f"ELSE datetime(replace({column},'T',' '), '{shift}') END, "
        f"replace({column},'T',' '))"
    )


def _normalize_range(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """时间范围归一化：补全当天 00:00:00 / 23:59:59（本地），再把边界换算成 UTC。"""
    start = (date_from or "").strip()
    end = (date_to or "").strip()
    if start and len(start) <= 10:
        start = f"{start} 00:00:00"
    if end and len(end) <= 10:
        end = f"{end} 23:59:59"
    return (
        _to_utc_bound(start) if start else None,
        _to_utc_bound(end) if end else None,
    )


def _escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def _build_preview(content: str, tokens: list[str]) -> str:
    """从原文截出命中词附近的一段，并把命中词用 <em> 标出。"""
    safe = _escape(content)
    if not safe:
        return ""
    lowered = safe.lower()
    pos = -1
    hit_len = 0
    for token in tokens:
        if not token:
            continue
        found = lowered.find(token.lower())
        if found >= 0 and (pos < 0 or found < pos):
            pos = found
            hit_len = len(token)
    if pos < 0:
        head = safe[:_PREVIEW_BEFORE + _PREVIEW_AFTER]
        return head + ("…" if len(safe) > len(head) else "")
    start = max(0, pos - _PREVIEW_BEFORE)
    end = min(len(safe), pos + hit_len + _PREVIEW_AFTER)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(safe) else ""
    marked = (
        safe[:start]
        + prefix
        + safe[start:pos]
        + "<em>"
        + safe[pos : pos + hit_len]
        + "</em>"
        + safe[pos + hit_len : end]
        + suffix
    )
    return marked


def create_user(
    settings: Settings,
    username: str,
    password_hash: str,
    salt: str,
    iterations: int,
) -> dict[str, Any]:
    ensure_schema(settings)
    user = {
        "id": _new_id(),
        "username": username,
        "password_hash": password_hash,
        "salt": salt,
        "iterations": iterations,
        "created_at": _now(),
        "last_login_at": None,
    }
    try:
        with _connect(settings) as conn, conn:
            conn.execute(
                """
                INSERT INTO users (id, username, password_hash, salt, iterations, created_at, last_login_at)
                VALUES (:id, :username, :password_hash, :salt, :iterations, :created_at, :last_login_at)
                """,
                user,
            )
    except sqlite3.IntegrityError as exc:
        raise HistoryConflict(f"用户名「{username}」已被注册") from exc
    return user


def get_user_by_username(settings: Settings, username: str) -> dict[str, Any] | None:
    ensure_schema(settings)
    with _connect(settings) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _row_to_dict(row)


def get_user_by_id(settings: Settings, user_id: str) -> dict[str, Any] | None:
    ensure_schema(settings)
    with _connect(settings) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_dict(row)


def touch_login(settings: Settings, user_id: str) -> None:
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), user_id))


def create_session(
    settings: Settings,
    token_hash: str,
    user_id: str,
    expires_at: str,
    user_agent: str | None,
) -> None:
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at, user_agent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash, user_id, _now(), expires_at, user_agent),
        )


def get_session_user(settings: Settings, token_hash: str) -> dict[str, Any] | None:
    """按令牌哈希取会话与用户；已过期的会话直接当作无效。"""
    ensure_schema(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            """
            SELECT s.expires_at AS expires_at,
                   u.id AS id, u.username AS username, u.created_at AS created_at
            FROM auth_sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    if timeutil.is_past(str(row["expires_at"])):
        delete_session(settings, token_hash)
        return None
    return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}


def delete_session(settings: Settings, token_hash: str) -> None:
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


def purge_expired_sessions(settings: Settings) -> int:
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        rows = conn.execute("SELECT token_hash, expires_at FROM auth_sessions").fetchall()
        expired = [str(r["token_hash"]) for r in rows if timeutil.is_past(str(r["expires_at"]))]
        if expired:
            conn.executemany("DELETE FROM auth_sessions WHERE token_hash = ?", [(h,) for h in expired])
    return len(expired)


def create_conversation(
    settings: Settings,
    user_id: str,
    title: str = DEFAULT_CONVERSATION_TITLE,
    mode: str = "react",
) -> dict[str, Any]:
    ensure_schema(settings)
    now = _now()
    conversation = {
        "id": _new_id(),
        "user_id": user_id,
        "title": _clip(title, settings.history_title_max_chars) or DEFAULT_CONVERSATION_TITLE,
        "mode": mode or "react",
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
    }
    with _connect(settings) as conn, conn:
        conn.execute(
            """
            INSERT INTO conversations (id, user_id, title, mode, created_at, updated_at, message_count)
            VALUES (:id, :user_id, :title, :mode, :created_at, :updated_at, :message_count)
            """,
            conversation,
        )
    return conversation


def get_conversation(settings: Settings, user_id: str, conversation_id: str) -> dict[str, Any] | None:
    """按 id 取会话；**必须带 user_id** —— 别人的会话一律返回 None（上层转 404）。"""
    ensure_schema(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    return _row_to_dict(row)


def ensure_conversation(
    settings: Settings,
    user_id: str,
    conversation_id: str | None,
    mode: str,
) -> dict[str, Any]:
    """取会话；不存在或不属于当前用户就新建一个（前端首次提问时不带 id）。"""
    if conversation_id:
        existing = get_conversation(settings, user_id, conversation_id)
        if existing is not None:
            return existing
    return create_conversation(settings, user_id, DEFAULT_CONVERSATION_TITLE, mode)


def list_conversations(
    settings: Settings,
    user_id: str,
    limit: int,
    offset: int,
    keyword: str | None = None,
    mode: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """会话列表：按主题（标题关键词）与模式过滤，按最近更新倒序。"""
    ensure_schema(settings)
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    keyword = (keyword or "").strip()
    if keyword:
        where.append("title LIKE ?")
        params.append(f"%{keyword}%")
    if mode:
        where.append("mode = ?")
        params.append(mode)
    clause = " AND ".join(where)

    with _connect(settings) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) AS c FROM conversations WHERE {clause}", params).fetchone()["c"])
        rows = conn.execute(
            f"""
            SELECT id, title, mode, created_at, updated_at, message_count
            FROM conversations WHERE {clause}
            ORDER BY updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def rename_conversation(
    settings: Settings,
    user_id: str,
    conversation_id: str,
    title: str,
) -> dict[str, Any] | None:
    clean = " ".join((title or "").split())
    if not clean:
        raise ValueError("标题不能为空")
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (_clip(clean, settings.history_title_max_chars * 2), _now(), conversation_id, user_id),
        )
        if not cur.rowcount:
            return None
    return get_conversation(settings, user_id, conversation_id)


def delete_conversation(settings: Settings, user_id: str, conversation_id: str) -> bool:
    """删除会话。"""
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        owned = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if owned is None:
            return False
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id))
    return True


def append_message(
    settings: Settings,
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
    meta: Any = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """追加一条消息，并在**同一事务**内更新会话的标题 / 计数 / 更新时间 / 模式。"""
    ensure_schema(settings)
    limit = _clip_len(settings)
    text = (content or "")[:limit] if limit > 0 else (content or "")
    now = _now()
    message_id = _new_id()

    with _connect(settings) as conn, conn:
        owned = conn.execute(
            "SELECT title, message_count FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if owned is None:
            raise HistoryNotFound("会话不存在")

        seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["m"]
        ) + 1

        conn.execute(
            """
            INSERT INTO messages (id, conversation_id, seq, role, content, seg, created_at, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                seq,
                role,
                text,
                " ".join(_index_terms(text)),
                now,
                _json_or_none(meta),
            ),
        )

        fields = ["message_count = message_count + 1", "updated_at = ?"]
        values: list[Any] = [now]
        if mode:
            fields.append("mode = ?")
            values.append(mode)
        if role == "user" and int(owned["message_count"] or 0) == 0 and seq == 1:
            fields.append("title = ?")
            values.append(title_from_question(text, settings.history_title_max_chars))
        values.extend([conversation_id, user_id])
        conn.execute(f"UPDATE conversations SET {', '.join(fields)} WHERE id = ? AND user_id = ?", values)

    return {
        "id": message_id,
        "conversation_id": conversation_id,
        "seq": seq,
        "role": role,
        "content": text,
        "created_at": now,
        "meta": meta,
    }


def _clip_len(settings: Settings) -> int:
    limit = settings.history_max_message_chars
    return limit if limit and limit > 0 else 0


def list_messages(
    settings: Settings,
    user_id: str,
    conversation_id: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """按顺序取会话内的消息；``limit`` 表示只取最近 N 条（仍按正序返回）。"""
    ensure_schema(settings)
    with _connect(settings) as conn:
        owned = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if owned is None:
            raise HistoryNotFound("会话不存在")
        if limit and limit > 0:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?
                ) ORDER BY seq ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq ASC",
                (conversation_id,),
            ).fetchall()
    return [_message_dict(r) for r in rows]


def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "seq": row["seq"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
        "meta": _load_json(row["meta"]),
    }


def _match_expression(tokens: list[str]) -> str:
    """把分词结果拼成 FTS5 MATCH 表达式。"""
    return " OR ".join(f'"{t}"' for t in tokens)


def search_messages(
    settings: Settings,
    user_id: str,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    mode: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """多维度检索自己的历史消息。"""
    ensure_schema(settings)
    keyword = (keyword or "").strip()
    match_terms = _query_terms(keyword) if keyword else []
    preview_tokens = tokenize(keyword) if keyword else []
    start, end = _normalize_range(date_from, date_to)

    where = ["c.user_id = ?"]
    params: list[Any] = [user_id]
    created_utc = _created_at_utc_sql("m.created_at")
    if start:
        where.append(f"{created_utc} >= ?")
        params.append(start)
    if end:
        where.append(f"{created_utc} <= ?")
        params.append(end)
    if mode:
        where.append("c.mode = ?")
        params.append(mode)

    join = "FROM messages m JOIN conversations c ON c.id = m.conversation_id"
    use_fts = bool(keyword) and bool(match_terms) and fts_available(settings)
    if use_fts:
        join += " JOIN messages_fts ON messages_fts.rowid = m.rowid"
        where.append("messages_fts MATCH ?")
        params.append(_match_expression(match_terms))
    elif keyword:
        where.append("(m.content LIKE ? OR c.title LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])

    clause = " AND ".join(where)
    with _connect(settings) as conn:
        try:
            total = int(conn.execute(f"SELECT COUNT(*) AS c {join} WHERE {clause}", params).fetchone()["c"])
            rows = conn.execute(
                f"""
                SELECT m.id AS message_id, m.conversation_id AS conversation_id, m.role AS role,
                       m.content AS content, m.created_at AS created_at,
                       c.title AS conversation_title, c.mode AS mode
                {join}
                WHERE {clause}
                ORDER BY m.created_at DESC, m.rowid DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        except sqlite3.OperationalError:
            return _search_like(
                settings, user_id, keyword, start, end, mode, limit, offset
            )

    items = [
        {
            "message_id": r["message_id"],
            "conversation_id": r["conversation_id"],
            "conversation_title": r["conversation_title"],
            "role": r["role"],
            "created_at": r["created_at"],
            "mode": r["mode"],
            "snippet": _build_preview(r["content"], preview_tokens or ([keyword] if keyword else [])),
        }
        for r in rows
    ]
    return items, total


def _search_like(
    settings: Settings,
    user_id: str,
    keyword: str | None,
    start: str | None,
    end: str | None,
    mode: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    where = ["c.user_id = ?"]
    params: list[Any] = [user_id]
    created_utc = _created_at_utc_sql("m.created_at")
    if start:
        where.append(f"{created_utc} >= ?")
        params.append(start)
    if end:
        where.append(f"{created_utc} <= ?")
        params.append(end)
    if mode:
        where.append("c.mode = ?")
        params.append(mode)
    if keyword:
        where.append("(m.content LIKE ? OR c.title LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    clause = " AND ".join(where)
    join = "FROM messages m JOIN conversations c ON c.id = m.conversation_id"

    with _connect(settings) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) AS c {join} WHERE {clause}", params).fetchone()["c"])
        rows = conn.execute(
            f"""
            SELECT m.id AS message_id, m.conversation_id AS conversation_id, m.role AS role,
                   m.content AS content, m.created_at AS created_at,
                   c.title AS conversation_title, c.mode AS mode
            {join}
            WHERE {clause}
            ORDER BY m.created_at DESC, m.rowid DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()

    preview_tokens = tokenize(keyword) if keyword else []
    items = [
        {
            "message_id": r["message_id"],
            "conversation_id": r["conversation_id"],
            "conversation_title": r["conversation_title"],
            "role": r["role"],
            "created_at": r["created_at"],
            "mode": r["mode"],
            "snippet": _build_preview(r["content"], preview_tokens or ([keyword] if keyword else [])),
        }
        for r in rows
    ]
    return items, total


def stats(settings: Settings | None = None) -> dict[str, Any]:
    """库内规模概览；库还没建出来时也要能安全返回。"""
    s = settings or get_settings()
    empty = {"users": 0, "conversations": 0, "messages": 0, "fts": False}
    if not s.history_configured:
        return empty
    try:
        ensure_schema(s)
        with _connect(s) as conn:
            users = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
            conversations = int(conn.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()["c"])
            messages = int(conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"])
        return {
            "users": users,
            "conversations": conversations,
            "messages": messages,
            "fts": fts_available(s),
        }
    except sqlite3.Error:
        return empty