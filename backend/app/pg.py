"""pg：PostgreSQL 统计查询工具（自然语言 → 只读 SQL）。

职责：
  - 让 LLM 生成 SELECT，经只读护栏（禁写、强制 LIMIT、超时）校验执行
  - 渲染查询结果为文本供模型引用
  - 未配置或连接失败时工具不注册

依赖：config、llm（SQL 生成）
被谁调用：main（/api/pg/schema）、agent（统计查询工具）、pg_source（复用只读护栏）
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import json
import re
import time
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from .config import Settings, get_settings
from .llm import LLMError, LLMNotConfigured, complete_chat

PG_TOOL_NAME = "query_bidding_postgres"

_CELL_TEXT_LIMIT = 300

_ROWS_TO_STATE = 20

_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "call", "do", "vacuum", "analyze", "reindex",
    "cluster", "refresh", "merge", "import", "execute", "prepare", "discard",
    "listen", "notify", "savepoint", "comment", "security", "reassign",
    "detach", "attach", "lock", "set", "commit", "rollback", "begin",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)
_START_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)
_LIMIT_ANY_RE = re.compile(r"\blimit\b", re.IGNORECASE)
_LIMIT_ALL_RE = re.compile(r"\blimit\s+all\b", re.IGNORECASE)
_FETCH_FIRST_RE = re.compile(r"\bfetch\s+(?:first|next)\s+(?:(\d+)\s+)?rows?\s+only\b", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

_schema_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


class PgError(RuntimeError):
    """PostgreSQL 查询的可读错误（连不上 / 生成失败 / 被护栏拒绝）。"""


def _connect(s: Settings, readonly: bool = True):
    """新建一个连接（调用方负责关闭）。"""
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover
        raise PgError("缺少依赖 psycopg2，请执行 pip install psycopg2-binary") from exc

    options = f"-c statement_timeout={max(1, s.pg_statement_timeout) * 1000}"
    if readonly:
        options = f"-c default_transaction_read_only=on {options}"

    try:
        return psycopg2.connect(
            host=s.pg_host,
            port=s.pg_port,
            user=s.pg_user,
            password=s.pg_password,
            dbname=s.pg_database,
            connect_timeout=max(1, s.pg_connect_timeout),
            options=options,
            application_name="bidding-qa-chatbot",
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        raise PgError(
            f"连接 PostgreSQL 失败（{s.pg_host}:{s.pg_port}/{s.pg_database}）：{exc}"
        ) from exc


def probe(s: Settings) -> None:
    """探活：新建只读连接执行 SELECT 1，证明服务活着（不写库）。失败抛 PgError。"""
    conn = _connect(s)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        conn.close()


def _cell_text(value: Any) -> str:
    """把 PostgreSQL 返回的各种类型统一转成可读字符串（只依赖标准库）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _truncate(text: str, limit: int = _CELL_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _read_schema_live(s: Settings) -> dict[str, Any]:
    """从 information_schema / pg_catalog 读取表、字段、注释、近似行数与样例值。"""
    conn = _connect(s)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """,
                (s.pg_schema,),
            )
            names = [row[0] for row in cur.fetchall()]
            if s.pg_tables:
                names = [n for n in names if n in s.pg_tables]

            if not names:
                return {"database": s.pg_database, "schema": s.pg_schema, "tables": []}

            cur.execute(
                """
                SELECT c.table_name, c.column_name, c.data_type, c.is_nullable,
                       c.ordinal_position, d.description
                FROM information_schema.columns c
                LEFT JOIN pg_catalog.pg_statio_all_tables st
                       ON st.schemaname = c.table_schema AND st.relname = c.table_name
                LEFT JOIN pg_catalog.pg_description d
                       ON d.objoid = st.relid AND d.objsubid = c.ordinal_position
                WHERE c.table_schema = %s
                ORDER BY c.table_name, c.ordinal_position
                """,
                (s.pg_schema,),
            )
            columns: dict[str, list[dict[str, Any]]] = {}
            for table_name, col_name, data_type, nullable, position, comment in cur.fetchall():
                if table_name not in names:
                    continue
                columns.setdefault(table_name, []).append(
                    {
                        "name": col_name,
                        "type": data_type,
                        "nullable": nullable == "YES",
                        "comment": comment or "",
                        "position": int(position),
                    }
                )

            cur.execute(
                """
                SELECT c.relname, c.reltuples::bigint
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind = 'r'
                """,
                (s.pg_schema,),
            )
            approx_rows = {row[0]: int(row[1]) for row in cur.fetchall()}

        tables: list[dict[str, Any]] = []
        for name in names:
            sample_columns: list[str] = []
            samples: list[dict[str, str]] = []
            try:
                with conn.cursor() as cur:
                    cur.execute(f'SELECT * FROM {_quote_ident(s.pg_schema)}.{_quote_ident(name)} LIMIT 2')
                    sample_columns = [d[0] for d in (cur.description or [])]
                    for row in cur.fetchall():
                        samples.append({col: _truncate(_cell_text(v)) for col, v in zip(sample_columns, row)})
            except Exception:  # noqa: BLE001 - 样例只是辅助信息，取不到不影响主流程
                samples = []

            rows = approx_rows.get(name, -1)
            tables.append(
                {
                    "name": name,
                    "approx_rows": rows if rows >= 0 else None,
                    "columns": columns.get(name, []),
                    "sample_columns": sample_columns,
                    "samples": samples,
                }
            )

        return {"database": s.pg_database, "schema": s.pg_schema, "tables": tables}
    finally:
        conn.close()


def load_schema(s: Settings, refresh: bool = False) -> dict[str, Any]:
    """读取库结构（带 TTL 缓存）。结构变动后可等过期或用 ``refresh=True`` 强制重取。"""
    key = (s.pg_host, s.pg_port, s.pg_database, s.pg_schema, s.pg_tables)
    cached = _schema_cache.get(key)
    now = time.time()
    if not refresh and cached is not None and now - cached[0] < max(0, s.pg_schema_ttl):
        return cached[1]
    data = _read_schema_live(s)
    _schema_cache[key] = (now, data)
    return data


def render_schema_text(data: dict[str, Any]) -> str:
    """把结构字典渲染成给大模型看的紧凑文本。"""
    lines = [f"数据库：{data.get('database')}（schema：{data.get('schema')}）"]
    tables = data.get("tables") or []
    if not tables:
        lines.append("（该 schema 下没有可查询的表）")
        return "\n".join(lines)

    for table in tables:
        rows = table.get("approx_rows")
        row_text = f"约 {rows} 行" if isinstance(rows, int) else "行数未知"
        lines.append(f'\n表 "{table["name"]}"（{row_text}）')
        for col in table.get("columns") or []:
            comment = f" -- {col['comment']}" if col.get("comment") else ""
            nullable = "" if col.get("nullable") else " not null"
            lines.append(f'  "{col["name"]}" {col["type"]}{nullable}{comment}')
        for sample in table.get("samples") or []:
            lines.append("  样例：" + json.dumps(sample, ensure_ascii=False))
    return "\n".join(lines)


def _sql_system_prompt(max_rows: int) -> str:
    return (
        "你是 PostgreSQL 专家，负责把用户的问题翻译成一条**只读** SQL 查询。\n"
        "硬性要求：\n"
        f"1. 只生成 SELECT 或 WITH 查询，严禁任何写操作；\n"
        "2. 只能使用下面 schema 中真实存在的表名与字段名，不要臆造；\n"
        "3. 表名、字段名一律用双引号包裹；\n"
        f"4. 必须带 LIMIT，且不超过 {max_rows}；\n"
        "5. 只输出 SQL 本身：不要解释、不要 markdown 代码块、不要结尾分号、不要多条语句。"
    )


def build_sql_messages(question: str, schema_text: str, max_rows: int) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _sql_system_prompt(max_rows)},
        {"role": "user", "content": f"数据库结构：\n{schema_text}\n\n用户问题：{question}"},
    ]


def clean_sql(raw: str) -> str:
    """剥掉代码围栏、结尾分号，合并空白行（大模型输出不总是听话）。"""
    text = (raw or "").strip()
    text = _CODE_FENCE_RE.sub("", text).strip()
    text = re.sub(r"[;\s]+$", "", text)
    return text.strip()


def _has_multiple_statements(sql: str) -> bool:
    """判断引号外的分号（一条语句里不该出现）。"""
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == ";":
            return True
        i += 1
    return False


def _scan_sql_text(sql: str) -> tuple[str, int | None]:
    out = list(sql)
    cut: int | None = None
    quote: str | None = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote is not None:
            if ch == "\\" and quote != "`":
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                quote = None
                i += 1
                continue
            out[i] = " "
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            i += 1
            continue
        if sql[i : i + 2] in ("--", "//"):
            cut = i
            while i < n and sql[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out), cut


def _strip_trailing_comments(sql: str) -> str:
    text = (sql or "").strip()
    while text:
        _, cut = _scan_sql_text(text)
        if cut is None:
            return text
        tail = text[cut:]
        nl = tail.find("\n")
        if nl != -1 and tail[nl + 1 :].strip():
            return text
        text = text[:cut].rstrip()
    return text


def validate_sql(sql: str, max_rows: int) -> str:
    """只读护栏 + 单语句 + 强制 LIMIT。返回规范化后的 SQL，违规时抛 PgError。"""
    if not sql:
        raise PgError("大模型没有生成可用的 SQL")
    if not _START_RE.match(sql):
        raise PgError("生成的语句不是只读查询（必须以 SELECT 或 WITH 开头）")
    hit = _FORBIDDEN_RE.search(sql)
    if hit:
        raise PgError(f"生成的语句包含被禁止的关键字：{hit.group(0).upper()}")
    if _has_multiple_statements(sql):
        raise PgError("生成的语句包含多条语句，已拒绝执行")

    sql = _strip_trailing_comments(sql)
    probe, _ = _scan_sql_text(sql)
    limit_all = _LIMIT_ALL_RE.search(probe)
    fetch = _FETCH_FIRST_RE.search(probe)
    if limit_all:
        sql = f"{sql[: limit_all.start()]}LIMIT {max_rows}{sql[limit_all.end():]}"
    elif fetch:
        if fetch.group(1) is None or int(fetch.group(1)) > max_rows:
            sql = f"{sql[: fetch.start()]}LIMIT {max_rows}{sql[fetch.end():]}"
    elif not _LIMIT_ANY_RE.search(probe):
        sql = f"{sql} LIMIT {max_rows}"
    else:
        matches = list(_LIMIT_RE.finditer(probe))
        if matches:
            last = matches[-1]
            if int(last.group(1)) > max_rows:
                sql = f"{sql[: last.start(1)]}{max_rows}{sql[last.end(1):]}"
    return sql


def _quote_ident(name: str) -> str:
    """用双引号包裹标识符，避免大小写与特殊字符导致的语法错误。"""
    return '"' + str(name).replace('"', '""') + '"'


def run_query(s: Settings, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    """在只读会话中执行查询，返回（列名, 行）。"""
    conn = _connect(s)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return [], []
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(max(1, s.pg_max_rows))
        return columns, rows
    except PgError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        raise PgError(f"执行 SQL 失败：{exc}") from exc
    finally:
        conn.close()


def render_result(sql: str, columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """把查询结果渲染成给模型看的文本。"""
    header = f"【PostgreSQL 查询结果】\nSQL：{sql}"
    if not rows:
        return (
            header
            + "\n查询成功，但返回 0 条记录。\n"
            "请直接告诉用户 PostgreSQL 中没有查到相关记录，不要编造任何数据。"
        )

    lines = [header, "返回列：" + ", ".join(columns), f"共 {len(rows)} 条："]
    for index, row in enumerate(rows, start=1):
        pairs = ", ".join(f"{col}={_truncate(_cell_text(val))}" for col, val in zip(columns, row))
        lines.append(f"{index}) {pairs}")
    return "\n".join(lines)


def _failure_text(reason: str) -> str:
    return (
        "【PostgreSQL 查询结果】\n"
        f"查询失败：{reason}\n\n"
        "请如实向用户说明 PostgreSQL 这次查询没有取到结果（可附上失败原因），不要编造数据。"
    )


def _run_pg(question: str, state: dict[str, Any]) -> str:
    """同步阻塞段：读 schema → 生成 SQL → 校验 → 执行 → 渲染。"""
    s: Settings | None = state.get("settings")
    sql: str | None = None
    try:
        if s is None:
            raise PgError("工具未注入配置（state 缺少 settings），已跳过执行")
        schema_text = render_schema_text(load_schema(s))
        raw = complete_chat(
            build_sql_messages(question, schema_text, s.pg_max_rows),
            settings=s,
            model=s.pg_model_or_default,
            temperature=0.0,
            max_tokens=s.pg_max_tokens,
            timeout=s.pg_timeout,
        )
        sql = validate_sql(clean_sql(raw), s.pg_max_rows)
        columns, rows = run_query(s, sql)

        state["sql"] = sql
        state["columns"] = columns
        state["row_count"] = len(rows)
        state["rows"] = [
            {col: _truncate(_cell_text(val)) for col, val in zip(columns, row)}
            for row in rows[:_ROWS_TO_STATE]
        ]
        state["error"] = None
        return render_result(sql, columns, rows)
    except (PgError, LLMError, LLMNotConfigured) as exc:
        reason = str(exc)
    except Exception as exc:  # noqa: BLE001 - 工具只返回可读提示，不向上抛
        reason = f"{type(exc).__name__}: {exc}"

    state["sql"] = sql
    state["columns"] = state.get("columns") or []
    state["row_count"] = 0
    state["rows"] = []
    state["error"] = reason
    return _failure_text(reason)


@tool
async def query_bidding_postgres(question: str, config: RunnableConfig) -> str:
    """查询 PostgreSQL 中的招投标明细数据，用于统计、计数、求和/平均、排序、精确筛选。"""
    state = (config.get("configurable") or {}).get("_pg_state")
    if state is None:
        state = {"settings": get_settings()}
    return await asyncio.to_thread(_run_pg, question, state)