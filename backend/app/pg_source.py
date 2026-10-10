# ============================================================================
# 文件：pg_source.py —— 建库管道的「PostgreSQL 数据源」（db.py 的 PG 版孪生兄弟）
#
# 【这个文件是干什么的】
#   建库管道（ingest.py）原本只认 MySQL（走 db.py）。但远程 MySQL（内网地址，见 .env 的 MYSQL_HOST）
#   当前不可达，而同样的两张业务表已经通过 sync_pg.py 落到本机 PostgreSQL 的
#   chatbot.public 里。于是本文件提供一套「和 db.py 同名同接口」的 PG 实现，
#   让 ingest.py 只换数据源、不改下游逻辑：
#     · pg_connection()  —— 对应 db.py 的 mysql_connection()，负责连上/用完自动关；
#     · discover_schema() —— 对应 db.py 的同名函数，读列定义/主键/行数；
#     · iter_rows()       —— 对应 db.py 的同名函数，流式逐行读数据。
#
# 【为什么必须「同接口」】
#   ingest.py 里后面几道工序（文档渲染、BM25、向量化、写 Qdrant）完全不知道数据
#   来自哪个库。只要这两个函数返回的数据形状与 db.py 一模一样，下游一行都不用改。
#
# 【为什么复用 pg.py 的连接】
#   pg.py 的 _connect(..., readonly=True) 会带 ``default_transaction_read_only=on``，
#   这是「服务端强制只读」的安全边界。本文件直接复用它，白捡一层只读保护，
#   与项目「对源库只读」的硬约束一致，也避免重复造轮子。
#
# 【与 db.py 的三处必要差异（PG 不是 MySQL，不能照抄）】
#   1) 主键：MySQL 有 COLUMN_KEY='PRI'，PG 要查 table_constraints / key_column_usage 约束表；
#   2) 注释：PG 用 pg_catalog.col_description() 读列注释；
#   3) 流式读取：PG 用「命名游标」（服务端游标），且必须在事务里运行，
#      所以连接**不能**设 autocommit=True。
#
# 【设计约束】
#   · 只读：只做 SELECT，绝不写库；
#   · 不硬编码业务字段：列名/注释/主键全部运行时从 information_schema 读；
#   · 失败隔离：单张表出错只影响它自己，其余表照常返回（与 db.py 一致）。
# ============================================================================

"""PostgreSQL 数据源接入（只读），接口与 ``db.py`` 对齐。

供建库管道在 ``KB_SOURCE=postgres`` 时切换使用；不配置时管道仍走 MySQL。
列定义、注释、主键、行数全部运行时自 ``information_schema`` / ``pg_catalog`` 读取，
代码里不写死任何业务字段。
"""

# 打开「类型注解延迟求值」开关：让 list[str]、str | None 这类新写法在旧 Python 也能书写。
from __future__ import annotations

# uuid：PG 的 uuid 列读出来是 uuid.UUID 对象，需转成字符串才能 JSON 序列化。
import uuid
# 类型标注：Any（任意类型）、Iterator（逐行产出的迭代器）。
from typing import Any, Iterator
# contextmanager：把「连接」写成一个上下文管理器，配合 with 使用（对应 db.py 的写法）。
from contextlib import contextmanager

# 读取项目配置（它就是读 .env 的模块）。
from .config import Settings, get_settings
# DatabaseUnavailable：复用 db.py 里那一个 —— 上层（ingest.py / main.py）都捕获它，
# 绝不能新造异常类，否则上层捕获不到。
# normalize_value：复用 db.py 的值归一化（已处理日期/Decimal/bytes）。
from .db import DatabaseUnavailable, normalize_value
# 复用 pg.py 的连接与标识符工具：_connect 带「服务端强制只读」，_quote_ident 给标识符加双引号。
from .pg import _connect, _quote_ident

# --------------------------------------------------------------------------- #
# 类型翻译：PG 的类型名 → documents.py 认得的 MySQL 类型名
# --------------------------------------------------------------------------- #
# 【为什么需要】documents.py 的 _SKIP_TYPES 只认 MySQL 的二进制类型名（blob/binary...），
# 而 PG 的二进制类型叫 bytea。不翻译的话，二进制列不会被跳过、会被当文字渲染，
# 产出乱码噪声。这里把 bytea 映射成 blob，就不必去改 documents.py。
_PG_TYPE_ALIASES = {"bytea": "blob"}

# --------------------------------------------------------------------------- #
# 结构查询 SQL（全部只读）
# --------------------------------------------------------------------------- #
# 列定义：列名 / 基础类型 / 是否可空 / 顺序 / 长度精度（拼展示用类型串）。
_COLUMNS_SQL = """
    SELECT column_name, data_type, is_nullable, ordinal_position,
           character_maximum_length, numeric_precision, numeric_scale
    FROM information_schema.columns
    WHERE table_schema = %s AND table_name = %s
    ORDER BY ordinal_position
"""

# 列注释：PG 没有 MySQL 那种「列上直接带注释」，要用 col_description(表oid, 列序号) 取。
_COMMENTS_SQL = """
    SELECT a.attname, pg_catalog.col_description(a.attrelid, a.attnum)
    FROM pg_catalog.pg_attribute a
    JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relname = %s
      AND a.attnum > 0 AND NOT a.attisdropped
"""

# 主键列：PG 要走「约束表」（MySQL 是 COLUMN_KEY='PRI'，没有对应字段）。
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


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _current_schema(conn: Any) -> str:
    """取当前连接的 schema（默认 public），让 discover/iter 不必额外传参。"""
    with conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        row = cur.fetchone()
    # current_schema() 理论上不会为空；万一为空就用 public 兜底。
    return (row[0] if row else None) or "public"


def _column_type_text(data_type: str, max_len: Any, num_prec: Any, num_scale: Any) -> str:
    """拼一个「带长度/精度」的展示类型串，尽量贴近 db.py 里 MySQL 的 COLUMN_TYPE。"""
    # 字符串类：带最大长度，如 character varying(255)。
    if max_len:
        return f"{data_type}({int(max_len)})"
    # 数值类：带精度与小数位，如 numeric(12,2)。
    if num_prec:
        return f"{data_type}({int(num_prec)},{int(num_scale or 0)})"
    # 其余（text/date/boolean 等）直接返回基础类型名。
    return data_type


def _normalize_value(value: Any) -> Any:
    """把 PG 返回值转成可 JSON 序列化的形式。

    先处理 PG 特有的两种类型，再交给 db.py 的 normalize_value 统一收尾：
      · memoryview —— PG 的 binary(bytea) 读出来是 memoryview，先转成 bytes；
      · uuid.UUID  —— PG 的 uuid 列，转成字符串。
    """
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


# --------------------------------------------------------------------------- #
# 连接（对应 db.py 的 mysql_connection）
# --------------------------------------------------------------------------- #
@contextmanager
def pg_connection(settings: Settings | None = None) -> Iterator[Any]:
    """获取 PostgreSQL 连接（只读）。连接参数全部来自 .env。

    【为什么只读】复用 pg.py 的 _connect(readonly=True)，连接带
    ``default_transaction_read_only=on``，数据库自身就会拒绝任何写操作。
    【注意事项】这里**不设** autocommit=True —— iter_rows 的命名游标必须在事务里运行。
    """
    # 拿配置（传了就用传的，没传就从 .env 读）。
    s = settings or get_settings()
    # 配置不全就先给「人话」提示，别去硬连。
    if not s.pg_configured:
        raise DatabaseUnavailable(
            "未配置 PG_HOST / PG_USER / PG_PASSWORD / PG_DATABASE，请检查项目根目录的 .env 文件"
        )
    try:
        # 建立只读连接（_connect 内部会把底层异常转成 PgError）。
        conn = _connect(s, readonly=True)
    except Exception as exc:  # noqa: BLE001 - 统一翻译成 DatabaseUnavailable 供上层捕获
        raise DatabaseUnavailable(
            f"连接 PostgreSQL({s.pg_host}:{s.pg_port}/{s.pg_database}) 失败: {exc}"
        ) from exc
    try:
        yield conn
    finally:
        # 无论 with 块里正常结束还是报错，都关闭连接，避免连接泄漏。
        conn.close()


# --------------------------------------------------------------------------- #
# 表结构自动发现（对应 db.py 的 discover_schema，返回结构完全一致）
# --------------------------------------------------------------------------- #
def discover_schema(
    conn: Any,
    tables: tuple[str, ...],
    settings: Settings | None = None,
) -> dict[str, dict]:
    """读取每张表的真实列定义 + 主键 + 行数，返回结构与 db.py 完全一致。

    【主键的三级取法（关键）】
      ① 优先读 PG 的真实 PRIMARY KEY 约束（推荐、最可靠）；
      ② 没有约束时，退回配置里的 KB_PRIMARY_KEYS 兜底键（防御用）；
      ③ 两者都没有 → primary_key 为 None，此时**必须**由上层发警告（见 ingest.py），
         因为 doc_id 会退化成「行号+1」，行序不稳会导致重跑产生重复点。
    """
    s = settings or get_settings()
    # 当前 schema（默认 public）。
    schema = _current_schema(conn)
    result: dict[str, dict] = {}
    for table in tables:
        # 先准备一份「容错骨架」：即使中途出错，返回结构也完整。
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
                # ① 列定义。
                cur.execute(_COLUMNS_SQL, (schema, table))
                rows = cur.fetchall()
                if not rows:
                    # 查不到列 = 表不存在或无权限。
                    entry["error"] = "表不存在或当前账号无访问权限"
                    result[table] = entry
                    continue
                entry["exists"] = True

                # ② 列注释（col_description），整理成 {列名: 注释}。
                cur.execute(_COMMENTS_SQL, (schema, table))
                comments = {r[0]: (r[1] or "").strip() for r in cur.fetchall()}

                # ③ 真实主键约束。
                pks = _primary_key_columns(conn, schema, table)

                # ④ 主键兜底：没约束就用配置声明的键（仅取第一个列名）。
                if not pks:
                    fallback = s.kb_primary_keys.get(table)
                    if fallback:
                        pks = [fallback]

                # ⑤ 组装列定义（字段名与 db.py 逐一对齐）。
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

                # ⑥ 精确行数（与 db.py 的 COUNT(*) 语义一致；本表仅 933 行，开销可忽略）。
                cur.execute(f"SELECT COUNT(*) FROM {_quote_ident(schema)}.{_quote_ident(table)}")
                entry["row_count"] = int(cur.fetchone()[0])
        except Exception as exc:  # noqa: BLE001 - 单表失败不影响整批
            entry["error"] = f"读取表结构失败: {exc}"
        result[table] = entry
    return result


# --------------------------------------------------------------------------- #
# 逐行流式读取（对应 db.py 的 iter_rows）
# --------------------------------------------------------------------------- #
def iter_rows(
    conn: Any,
    table: str,
    batch_size: int = 500,
    settings: Settings | None = None,
) -> Iterator[dict[str, Any]]:
    """流式逐行读取整表，返回结构与 db.py 一致（每行是「列名→值」字典）。

    【为什么用命名游标】psycopg2 的普通游标会把整个结果一次性拉进客户端内存；
    命名游标（``conn.cursor(name=...)``）是服务端游标，一次只取 ``itersize`` 行，
    大表也不会爆内存 —— 对应 db.py 里的 SSDictCursor。
    【为什么不能 autocommit】命名游标必须在事务里创建（DECLARE/FETCH），
    所以连接保持默认的「非自动提交」，由 pg_connection 保证只读。
    【排序为什么重要】PG 的 SELECT 不保证顺序。这里按主键（或兜底键）ORDER BY，
    保证两次重跑的行序一致 → doc_id 可复现 → 写入是幂等覆盖而不是重复插入。
    """
    s = settings or get_settings()
    # 当前 schema。
    schema = _current_schema(conn)
    # 排序键：优先真实主键，其次配置兜底键；再没有就不排序（上层会发警告）。
    order_cols = _primary_key_columns(conn, schema, table)
    if not order_cols:
        fallback = s.kb_primary_keys.get(table)
        if fallback:
            order_cols = [fallback]

    # 拼查询语句：表名/schema 用双引号包住（标识符不能走占位符）。
    sql = f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(table)}"
    if order_cols:
        sql += " ORDER BY " + ", ".join(_quote_ident(c) for c in order_cols)

    # 命名游标：服务端游标 + 分批取，with 结束时自动关闭。
    with conn.cursor(name=f"ingest_{table}") as cur:
        # itersize：每次从服务器拉多少行，等价于 db.py 里 fetchmany 的批大小。
        cur.itersize = batch_size
        cur.execute(sql)
        # 【关键坑，务必先取一批再读列名】命名（服务端）游标的 cur.description
        #   要等「第一次 fetch」之后才会有值；在 fetch 之前读它拿到的是空列表，
        #   那样 zip 出来的字典会是空的，所有字段都变空白。
        batch = cur.fetchmany(batch_size)
        # 结果集列名（普通游标返回元组，要靠它把每行拼成字典）。
        columns = [d.name for d in (cur.description or [])]
        # 逐批产出：先处理已经取到的第一批，再继续拉后面的批次。
        while batch:
            for row in batch:
                # 逐行归一化后「吐」给调用方，内存里始终只留当前一批。
                yield {col: _normalize_value(value) for col, value in zip(columns, row)}
            batch = cur.fetchmany(batch_size)