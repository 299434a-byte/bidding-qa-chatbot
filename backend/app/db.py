
"""db：MySQL 数据源接入（只读连接与 schema 自省）。

职责：
  - 按 .env 配置建立只读 MySQL 连接
  - 自省业务表的真实列结构与行数（供 /api/mysql/schema 与文档化）
  - 提供迭代读行接口供建库管道消费

依赖：config
被谁调用：ingest（读源表）、pg_source（结构对照）、main（schema 端点）
"""

from __future__ import annotations

import datetime as dt

import decimal

import re

from contextlib import contextmanager

from typing import Any, Iterator

import pymysql

from pymysql.cursors import SSDictCursor

from .config import Settings, get_settings

_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


class DatabaseUnavailable(RuntimeError):
    """数据库不可用（未配置 / 连不上 / 无权限），调用方需优雅降级。"""


def quote_ident(name: str) -> str:
    """给标识符加反引号，并校验合法性。"""
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"非法标识符: {name!r}")
    return f"`{name}`"


@contextmanager
def mysql_connection(settings: Settings | None = None) -> Iterator[pymysql.connections.Connection]:
    """获取 MySQL 连接。连接参数全部来自 .env。"""
    s = settings or get_settings()
    if not s.mysql_configured:
        raise DatabaseUnavailable("未配置 MYSQL_USER / MYSQL_HOST，请检查项目根目录的 .env 文件")
    try:
        conn = pymysql.connect(
            host=s.mysql_host,
            port=s.mysql_port,
            user=s.mysql_user,
            password=s.mysql_password,
            database=s.mysql_database,
            charset=s.mysql_charset,
            connect_timeout=s.mysql_connect_timeout,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as exc:
        raise DatabaseUnavailable(f"连接 MySQL({s.mysql_host}:{s.mysql_port}/{s.mysql_database}) 失败: {exc}") from exc
    try:
        yield conn
    finally:
        conn.close()


def normalize_value(value: Any) -> Any:
    """把 MySQL 返回值转成可 JSON 序列化的形式。"""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "ignore")
    return value


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: normalize_value(val) for key, val in row.items()}


def discover_schema(conn: pymysql.connections.Connection, tables: tuple[str, ...]) -> dict[str, dict]:
    """读取每张表的真实列定义 + 行数。"""
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
                cur.execute(
                    """
                    SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE,
                           COLUMN_KEY, COLUMN_COMMENT, ORDINAL_POSITION
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                    """,
                    (table,),
                )
                rows = cur.fetchall()
                if not rows:
                    entry["error"] = "表不存在或当前账号无访问权限"
                    result[table] = entry
                    continue

                entry["exists"] = True
                entry["columns"] = [
                    {
                        "name": r["COLUMN_NAME"],
                        "type": r["DATA_TYPE"],
                        "column_type": r["COLUMN_TYPE"],
                        "nullable": r["IS_NULLABLE"] == "YES",
                        "key": r["COLUMN_KEY"],
                        "comment": (r["COLUMN_COMMENT"] or "").strip(),
                        "position": int(r["ORDINAL_POSITION"]),
                    }
                    for r in rows
                ]
                pks = [c["name"] for c in entry["columns"] if c["key"] == "PRI"]
                entry["primary_key"] = pks[0] if pks else None

                cur.execute(f"SELECT COUNT(*) AS c FROM {quote_ident(table)}")
                entry["row_count"] = int(cur.fetchone()["c"])
        except pymysql.MySQLError as exc:
            entry["error"] = f"读取表结构失败: {exc}"
        result[table] = entry
    return result


def iter_rows(
    conn: pymysql.connections.Connection,
    table: str,
    batch_size: int = 500,
) -> Iterator[dict[str, Any]]:
    """流式读取整表，避免大表一次性载入内存。"""
    sql = f"SELECT * FROM {quote_ident(table)}"
    with conn.cursor(SSDictCursor) as cur:
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield normalize_row(row)


def list_tables(conn: pymysql.connections.Connection) -> list[dict[str, Any]]:
    """列出当前库下所有表名，便于排查「表名拼错 / 无权限」类问题。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT TABLE_NAME, TABLE_ROWS
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """
        )
        return [{"table": r["TABLE_NAME"], "approx_rows": int(r["TABLE_ROWS"] or 0)} for r in cur.fetchall()]