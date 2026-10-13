
"""pg_source：PostgreSQL 建库数据源（只读，接口与 db.py 对齐）。

职责：
  - 从本机 PG 副本读取业务表（KB_SOURCE=postgres 时替代 MySQL）
  - 自省 schema 与迭代读行（返回结构与 db.py 完全一致）
  - 值归一化为可 JSON 序列化形式

依赖：config、db（结构对照）、pg（复用连接与护栏）
被谁调用：ingest（数据源切换）
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator
from contextlib import contextmanager

from .config import Settings, get_settings
from .db import DatabaseUnavailable, normalize_value
from .pg import _connect, _quote_ident

_PG_TYPE_ALIASES = {"bytea": "blob"}

_COLUMNS_SQL = """
    SELECT column_name, data_type, is_nullable, ordinal_position,
           character_maximum_length, numeric_precision, numeric_scale
    FROM information_schema.columns
    WHERE table_schema = %s AND table_name = %s
    ORDER BY ordinal_position
"""

_COMMENTS_SQL = """
    SELECT a.attname, pg_catalog.col_description(a.attrelid, a.attnum)
    FROM pg_catalog.pg_attribute a
    JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relname = %s
      AND a.attnum > 0 AND NOT a.attisdropped
"""

_PRIMARY_KEY_SQL = """
    SELECT kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
    WHERE tc.table_schema = %s AND tc.table_name = %s
      AND tc.constraint_type = 'PRIMARY KEY'
    ORDER BY kcu.ordinal_position
"""


def _current_schema(conn: Any) -> str:
    """取当前连接的 schema（默认 public），让 discover/iter 不必额外传参。"""
    with conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        row = cur.fetchone()
    return (row[0] if row else None) or "public"


def _column_type_text(data_type: str, max_len: Any, num_prec: Any, num_scale: Any) -> str:
    """拼一个「带长度/精度」的展示类型串，尽量贴近 db.py 里 MySQL 的 COLUMN_TYPE。"""
    if max_len:
        return f"{data_type}({int(max_len)})"
    if num_prec:
        return f"{data_type}({int(num_prec)},{int(num_scale or 0)})"
    return data_type


def _normalize_value(value: Any) -> Any:
    """把 PG 返回值转成可 JSON 序列化的形式。"""
    if isinstance(value, memoryview):
        value = bytes(value)
    elif isinstance(value, uuid.UUID):
        value = str(value)
    return normalize_value(value)


def _primary_key_columns(conn: Any, schema: str, table: str) -> list[str]:
    """读取某张表的真实主键列（按列顺序），没有主键返回空列表。"""
    with conn.cursor() as cur:
        cur.execute(_PRIMARY_KEY_SQL, (schema, table))
        return [r[0] for r in cur.fetchall()]


@contextmanager
def pg_connection(settings: Settings | None = None) -> Iterator[Any]:
    """获取 PostgreSQL 连接（只读）。连接参数全部来自 .env。"""
    s = settings or get_settings()
    if not s.pg_configured:
        raise DatabaseUnavailable(
            "未配置 PG_HOST / PG_USER / PG_PASSWORD / PG_DATABASE，请检查项目根目录的 .env 文件"
        )
    try:
        conn = _connect(s, readonly=True)
    except Exception as exc:  # noqa: BLE001 - 统一翻译成 DatabaseUnavailable 供上层捕获
        raise DatabaseUnavailable(
            f"连接 PostgreSQL({s.pg_host}:{s.pg_port}/{s.pg_database}) 失败: {exc}"
        ) from exc
    try:
        yield conn
    finally:
        conn.close()


def discover_schema(
    conn: Any,
    tables: tuple[str, ...],
    settings: Settings | None = None,
) -> dict[str, dict]:
    """读取每张表的真实列定义 + 主键 + 行数，返回结构与 db.py 完全一致。"""
    s = settings or get_settings()
    schema = _current_schema(conn)
    result: dict[str, dict] = {}
    for table in tables:
        entry: dict[str, Any] = {
            "table": table,
            "exists": False,
            "row_count": 0,
            "primary_key": None,
            "columns": [],
            "error": None,
        }
        try:
            with conn.cursor() as cur:
                cur.execute(_COLUMNS_SQL, (schema, table))
                rows = cur.fetchall()
                if not rows:
                    entry["error"] = "表不存在或当前账号无访问权限"
                    result[table] = entry
                    continue
                entry["exists"] = True

                cur.execute(_COMMENTS_SQL, (schema, table))
                comments = {r[0]: (r[1] or "").strip() for r in cur.fetchall()}

                pks = _primary_key_columns(conn, schema, table)

                if not pks:
                    fallback = s.kb_primary_keys.get(table)
                    if fallback:
                        pks = [fallback]

                entry["columns"] = [
                    {
                        "name": r[0],
                        "type": _PG_TYPE_ALIASES.get(r[1], r[1]),
                        "column_type": _column_type_text(r[1], r[4], r[5], r[6]),
                        "nullable": r[2] == "YES",
                        "key": "PRI" if r[0] in pks else "",
                        "comment": comments.get(r[0], ""),
                        "position": int(r[3]),
                    }
                    for r in rows
                ]
                entry["primary_key"] = pks[0] if pks else None

                cur.execute(f"SELECT COUNT(*) FROM {_quote_ident(schema)}.{_quote_ident(table)}")
                entry["row_count"] = int(cur.fetchone()[0])
        except Exception as exc:  # noqa: BLE001 - 单表失败不影响整批
            entry["error"] = f"读取表结构失败: {exc}"
        result[table] = entry
    return result


def iter_rows(
    conn: Any,
    table: str,
    batch_size: int = 500,
    settings: Settings | None = None,
) -> Iterator[dict[str, Any]]:
    """流式逐行读取整表，返回结构与 db.py 一致（每行是「列名→值」字典）。"""
    s = settings or get_settings()
    schema = _current_schema(conn)
    order_cols = _primary_key_columns(conn, schema, table)
    if not order_cols:
        fallback = s.kb_primary_keys.get(table)
        if fallback:
            order_cols = [fallback]

    sql = f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(table)}"
    if order_cols:
        sql += " ORDER BY " + ", ".join(_quote_ident(c) for c in order_cols)

    with conn.cursor(name=f"ingest_{table}") as cur:
        cur.itersize = batch_size
        cur.execute(sql)
        batch = cur.fetchmany(batch_size)
        columns = [d.name for d in (cur.description or [])]
        while batch:
            for row in batch:
                yield {col: _normalize_value(value) for col, value in zip(columns, row)}
            batch = cur.fetchmany(batch_size)