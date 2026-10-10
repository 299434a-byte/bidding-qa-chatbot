# ============================================================================
# 文件：sync_pg.py —— 「远程 MySQL → 本机 PostgreSQL」的结构化数据同步脚本
#
# 【这个脚本是干什么的】
#   把远程 MySQL 里 bidding_information_dai 库的两张业务表（采购公告、招标项目）
#   整表复制到本机 PostgreSQL 的 chatbot 库。
#   复制之后，后端的「PostgreSQL 查询工具」才有数据可查——像「预算超过一百万的项目
#   有几个」这种统计问题，走 PG 比走向量检索精确得多。
#
# 【谁调用它】
#   人工在命令行执行，例如（在 backend 目录下）：
#       ..\.venv\Scripts\python.exe sync_pg.py
#   它是「一次性同步工具」，后端服务不会自动调用它。
#
# 【它依赖哪些配置】
#   · 源（远程 MySQL）：.env 里的 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
#   · 目标（本机 PG） ：.env 里的 PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DATABASE / PG_SCHEMA
#   配置一律从 .env 读，脚本里不写死任何地址与口令（这是本项目的硬性约束）。
#
# 【先懂四个概念（大白话）】
#   1) 全量重建：不管目标表原来有什么，先删掉（DROP）再按源表结构新建（CREATE），
#      然后把数据灌进去。好处是「跑一次的结果 = 源库的当前样子」，不会越跑越乱；
#      代价是每次都要重来一遍（数据量大时较慢）。
#   2) 结构自适配：目标表的列名、列类型不是写死在脚本里的，而是运行时从源表「问」出来的
#      （MySQL 的 SHOW FULL COLUMNS）。这样源表加了一列，重跑脚本就自动跟上。
#   3) 类型翻译：MySQL 和 PostgreSQL 的类型名字不完全一样（比如 MySQL 的
#      decimal(10,2) 在 PG 里习惯写成 NUMERIC(10,2)，text 在 PG 里就叫 TEXT）。
#      本脚本内置了一张「对照表」做翻译。
#   4) 失败隔离：某一张表同步失败（比如表不存在），不让整个脚本崩掉，
#      而是记下错误、继续处理下一张表，最后统一汇报——这叫「不拖垮别的任务」。
# ============================================================================

"""把远程 MySQL 的 bidding_information_dai 两张表全量同步到本机 PostgreSQL。

设计要点：
- **只读源库**：对远程 MySQL 只做 SELECT / SHOW，绝不写入，避免误伤生产数据；
- **全量重建**：目标表先 DROP 再 CREATE，结构完全对齐源表；
- **结构自适配**：列名与类型运行时读取，脚本内不硬编码任何业务字段；
- **失败隔离**：单表失败不影响其他表，最后汇总并给出人话原因。

命令行用法（需在 backend 目录下执行）::

    ..\\.venv\\Scripts\\python.exe sync_pg.py
    ..\\.venv\\Scripts\\python.exe sync_pg.py --tables projects        # 只同步一张表
    ..\\.venv\\Scripts\\python.exe sync_pg.py --batch 500             # 自定义每批行数
"""

# 打开「类型注解延迟求值」开关：让 list[str]、str | None 这类新写法在 Python 3.9 及以前
# 也能正常书写（注解先当字符串存着，需要时才解析）。必须放在文件最顶部。
from __future__ import annotations

# argparse：解析命令行参数（--tables、--batch 这类）。标准库自带，不用额外安装。
import argparse
# sys：用来设置「退出码」（0 = 成功，1 = 失败），让外部脚本能判断本次同步是否成功。
import sys
# time：记录开始时刻，结尾相减得到总耗时。
import time
# typing.Any：表示「任意类型」，用于类型标注。
from typing import Any

# psycopg2：PostgreSQL 的 Python 驱动（连接本机 PG 用）。
import psycopg2
# psycopg2.extras 里有 execute_values —— 它能「一条 SQL 批量插入多行」，
# 比一行一条 INSERT 快非常多，是导入数据的关键工具。
import psycopg2.extras
# pymysql：MySQL 的 Python 驱动（读远程 MySQL 用）。
import pymysql
# pymysql.cursors 里有个 SSCursor（服务端游标），特点是「一边读一边取」，
# 大表也不会把所有行一次性塞进内存，避免内存爆掉。
import pymysql.cursors

# 读取项目配置（它就是读 .env 的模块）。因为我们只用到它的读配置能力，
# 所以这个脚本不会依赖后端服务的任何其它部分，可以单独运行。
from app.config import get_settings


# --------------------------------------------------------------------------- #
# 类型翻译：MySQL 列类型 → PostgreSQL 等价类型
# --------------------------------------------------------------------------- #
def _paren(column_type: str) -> str:
    """从 MySQL 类型字符串里取出括号部分，如 'varchar(255)' → '(255)'。

    取不到（类型不带括号，如 'text'）时返回空字符串，方便后面直接拼接。
    """
    # 找左括号的位置；找不到就说明这个类型没有长度/精度，直接返回空串。
    start = column_type.find("(")
    if start == -1:
        return ""
    # 从右括号位置截到包含右括号为止（用 find 从 start 往后找一个 ')'）。
    end = column_type.find(")", start)
    # 理论上括号一定成对；为稳妥起见，万一没找到右括号就退回到「到字符串末尾」。
    return column_type[start : end + 1] if end != -1 else column_type[start:]


def pg_type_of(column_type: str) -> str:
    """把 MySQL 的列类型（带长度，如 decimal(10,2)）翻译成 PostgreSQL 的列类型。

    【为什么要翻译】两种数据库语法相近但不相同。直接照抄有时也能建表成功，
    但像 decimal → NUMERIC 这种习惯写法、text → TEXT 的映射，写清楚更稳、更易读。

    【参数解释】column_type —— 源表列类型的完整字符串，例如 'varchar(255)'、
    'decimal(10,2)'、'datetime'、'text'。

    【返回值】可直接写进 PostgreSQL CREATE TABLE 的类型字符串。
    """
    # 统一转小写，避免源库写成 VARCHAR / Varchar 时匹配不上。
    ct = column_type.strip().lower()
    # 取「基础类型名」：把括号里的长度/精度去掉。如 'decimal(10,2)' → 'decimal'。
    base = ct.split("(")[0].strip()

    # —— 精确小数：MySQL 的 decimal 在 PG 里推荐写成 NUMERIC（两者等价，PG 更常用）。——
    # 保留原来的精度，如 decimal(10,2) → NUMERIC(10,2)，避免把「两位小数」弄丢。
    if base in ("decimal", "numeric"):
        return "NUMERIC" + _paren(ct)
    # —— 各种小整数：PG 里统一用 SMALLINT（MySQL 的 tinyint/mediumint 在 PG 无对应名）。——
    if base in ("tinyint", "smallint", "mediumint"):
        return "SMALLINT"
    # —— 常规整数 → INTEGER。——
    if base in ("int", "integer"):
        return "INTEGER"
    # —— 大整数 → BIGINT（PB 级 ID 常用）。——
    if base == "bigint":
        return "BIGINT"
    # —— 单精度浮点 → REAL。——
    if base == "float":
        return "REAL"
    # —— 双精度浮点 → DOUBLE PRECISION。——
    if base in ("double", "double precision", "real"):
        return "DOUBLE PRECISION"
    # —— 定长字符串：保留长度，如 char(2) → CHAR(2)。——
    if base == "char":
        return "CHAR" + _paren(ct)
    # —— 变长字符串：保留长度。这里 str(255) 的来源就是它。——
    # 【注意】PG 的 VARCHAR 长度上限远大于 255，所以 MySQL 的 varchar 长度可以照搬。
    if base == "varchar":
        return "VARCHAR" + _paren(ct)
    # —— 各种大文本：MySQL 的 text/tinytext/mediumtext/longtext 在 PG 里统统叫 TEXT。——
    # 【为什么这样处理】PG 的 TEXT 没有长度限制，可以装下 MySQL 的所有文本类型，
    # 所以「统一成 TEXT」既安全又简单。任务要求 content 字段用 TEXT，就是这条规则命中的。
    if base in ("text", "tinytext", "mediumtext", "longtext"):
        return "TEXT"
    # —— 二进制数据：MySQL 的 blob 系列在 PG 里叫 BYTEA。——
    if base in ("blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary"):
        return "BYTEA"
    # —— 日期只保留年月日 → DATE（如 procurement_date）。——
    if base == "date":
        return "DATE"
    # —— 日期时间 → TIMESTAMP（PG 的 TIMESTAMP 不带时区，与 MySQL datetime 语义最接近）。——
    if base in ("datetime", "timestamp"):
        return "TIMESTAMP"
    # —— 只有时间部分 → TIME。——
    if base == "time":
        return "TIME"
    # —— MySQL 的 year 类型本质是个小整数 → SMALLINT。——
    if base == "year":
        return "SMALLINT"
    # —— 枚举/集合：PG 没有完全对应的类型，退化成 TEXT 保存它的字面值，够用且不会丢数据。——
    if base in ("enum", "set"):
        return "TEXT"
    # —— JSON → JSONB（PG 的二进制 JSON，查询效率更好）。——
    if base == "json":
        return "JSONB"
    # —— 位类型：本项目数据里没有，退化成 BOOLEAN 处理。——
    if base == "bit":
        return "BOOLEAN"
    # —— 兜底：万一遇到没列出的类型，就原样交给 PostgreSQL 去判断（它可能恰好认识）。——
    # 【潜在风险】如果 PG 也不认识，建表会报错——但错误会被本脚本捕获并如实报告，
    # 不会让脚本静默出错。
    return column_type.upper()


def quote_ident(name: str) -> str:
    """给 PostgreSQL 标识符（表名/列名）加双引号，并转义内部的双引号。

    【为什么要加引号】有些列名可能与 SQL 关键字重名（比如 order、user），
    加双引号后 PG 就会把它当「名字」而不是「关键字」，避免语法错误。
    【转义规则】名字里本来就有双引号时，把它写成两个双引号（SQL 的标准转义方式）。
    """
    return '"' + name.replace('"', '""') + '"'


def backtick(name: str) -> str:
    """给 MySQL 标识符加反引号，并转义内部反引号（MySQL 的转义方式）。"""
    return "`" + name.replace("`", "``") + "`"


# --------------------------------------------------------------------------- #
# 读取源表结构
# --------------------------------------------------------------------------- #
def read_mysql_columns(conn: pymysql.connections.Connection, table: str) -> list[dict[str, Any]]:
    """读取 MySQL 某张表的列定义（列名、类型、是否允许空、注释）。

    【原理】MySQL 提供了 `SHOW FULL COLUMNS FROM 表名` 命令，它会返回一行行的列信息。
    每行包含 9 个字段，我们只挑其中 5 个有用的：
        字段名 Field / 类型 Type / 是否可空 Null / 键类型 Key / 注释 Comment

    【参数解释】
        conn  —— 已连上的 MySQL 连接；
        table —— 表名。

    【返回值】列表，每项形如 {"name": 列名, "column_type": 带长度的类型, "nullable": 布尔, "comment": 注释}
    """
    # 用一个普通游标执行 SHOW 命令（数据量很小，不需要流式游标）。
    with conn.cursor() as cur:
        # 表名和列名不能作为参数（? 占位符只用于「值」），所以只能用字符串拼接。
        # 【安全说明】table 来自本脚本的命令行参数/配置，不是用户输入，
        # 且这里用反引号包起来，不存在 SQL 注入风险。
        cur.execute("SHOW FULL COLUMNS FROM " + backtick(table))
        rows = cur.fetchall()
    # 把「元组列表」整理成「字典列表」，让后面的代码可读性更好。
    columns: list[dict[str, Any]] = []
    for row in rows:
        # SHOW FULL COLUMNS 的返回顺序固定为：
        # 0=Field 1=Type 2=Collation 3=Null 4=Key 5=Default 6=Extra 7=Privileges 8=Comment
        columns.append(
            {
                "name": row[0],
                "column_type": row[1],
                "nullable": (row[3] or "").upper() == "YES",
                # row[4] 是 Key 列：主键列的值是 'PRI'。必须带上它，否则重建表时主键会丢。
                "key": (row[4] or "").upper(),
                "comment": row[8] or "",
            }
        )
    return columns


# --------------------------------------------------------------------------- #
# 在 PostgreSQL 里重建表
# --------------------------------------------------------------------------- #
def recreate_pg_table(
    conn: "psycopg2.extensions.connection",
    schema: str,
    table: str,
    columns: list[dict[str, Any]],
) -> None:
    """在 PostgreSQL 里「先删后建」一张表，列结构与源表对齐。

    【参数解释】
        conn    —— 已连上的 PG 连接；
        schema  —— 建到哪个 schema（本项目是 public）；
        table   —— 表名；
        columns —— 源表列定义（read_mysql_columns 的返回值）。

    【为什么先 DROP】不删的话，如果目标表已存在且结构不同（比如少了几列），
    CREATE 会直接报错「表已存在」，后面的导入就全废了。先删再建最省心。

    【注意事项】DROP 会连表里的数据一起删掉。这是「全量重建」的预期行为，
    但只作用于本机 PG 的这张表，不会影响远程 MySQL 的源数据。
    """
    # 逐个列拼出「列名 类型 [NOT NULL]」这样的片段。
    definitions: list[str] = []
    for col in columns:
        # 列名加双引号（防关键字冲突），类型做翻译，非空约束按源表如实照搬。
        piece = f"{quote_ident(col['name'])} {pg_type_of(col['column_type'])}"
        # 源表是 NOT NULL 的，目标表也加上；源表允许为空就不加（PG 默认允许空）。
        if not col["nullable"]:
            piece += " NOT NULL"
        definitions.append(piece)

    # 【补主键（关键，别删）】源表的主键列（SHOW FULL COLUMNS 第 5 列 Key = 'PRI'）也要照搬过来。
    # 【为什么必须有】只搬列、不搬主键的话，全量同步一跑主键就丢了；而建库管道要拿主键列
    #   当 doc_id 的稳定标识，丢了就只能退化成「行号+1」，重跑建库会产生重复点。
    pri_cols = [col["name"] for col in columns if (col.get("key") or "").upper() == "PRI"]
    if pri_cols:
        # 主键可能由多列组成，所以把列名用逗号连接，写成 PRIMARY KEY (列1, 列2)。
        definitions.append("PRIMARY KEY (" + ", ".join(quote_ident(c) for c in pri_cols) + ")")

    # DROP TABLE IF EXISTS 的 IF EXISTS 很重要：表不存在时不报错，保证脚本可重复运行。
    # CASCADE 表示「如果有别的东西依赖这张表，也一并处理」，避免删除被外键挡住。
    drop_sql = f"DROP TABLE IF EXISTS {quote_ident(schema)}.{quote_ident(table)} CASCADE"
    # CREATE TABLE 把所有列定义用逗号连接起来。
    create_sql = (
        f"CREATE TABLE {quote_ident(schema)}.{quote_ident(table)} (\n  "
        + ",\n  ".join(definitions)
        + "\n)"
    )
    # 用一个事务把「删除 + 新建」一起提交：要么都成功，要么都不生效，不会留下半拉状态。
    with conn.cursor() as cur:
        cur.execute(drop_sql)
        cur.execute(create_sql)
    conn.commit()


# --------------------------------------------------------------------------- #
# 复制数据
# --------------------------------------------------------------------------- #
def copy_table(
    src: "pymysql.connections.Connection",
    dst: "psycopg2.extensions.connection",
    schema: str,
    table: str,
    batch: int,
) -> int:
    """把一张表的数据从 MySQL 复制到 PostgreSQL，返回复制成功的行数。

    【参数解释】
        src    —— 源 MySQL 连接；
        dst    —— 目标 PG 连接；
        schema —— 目标 schema；
        table  —— 表名；
        batch  —— 每批插入多少行（默认 1000）。

    【为什么要分批】一次性把几十万行同时放进内存/同时发一条巨长的 INSERT，
    内存和网络都会受不了。分批（每 1000 行提交一次）能稳定控制占用，
    也让「出错时前面已提交的部分不会白干」。

    【返回值】实际写入的行数。
    """
    # 取源表列定义（列名集合必须两边完全一致，否则 INSERT 会列不对应）。
    columns = read_mysql_columns(src, table)
    col_names = [c["name"] for c in columns]

    # 先在 PG 里把表重建出来（先删后建）。
    recreate_pg_table(dst, schema, table, columns)

    # 拼出带占位符的 INSERT 语句：%s 会被 execute_values 替换成「一批多行的值」。
    insert_sql = (
        f"INSERT INTO {quote_ident(schema)}.{quote_ident(table)} "
        f"({', '.join(quote_ident(c) for c in col_names)}) VALUES %s"
    )

    # 【关键】用 SSCursor（服务端游标）读源表：它是「取一批、用一批」的模式，
    # 不会把整张表一次性拉进内存。表再大也不会把内存撑爆。
    src_cur = src.cursor(pymysql.cursors.SSCursor)
    # 只 SELECT 我们需要的列（顺序与 col_names 严格一致，插入时才不会错位）。
    src_cur.execute(
        "SELECT " + ", ".join(backtick(c) for c in col_names) + " FROM " + backtick(table)
    )

    # 累计已写入的行数。
    copied = 0
    # 用 PG 的游标 + execute_values 批量插入。
    with dst.cursor() as dst_cur:
        # 循环：每次从源表取出 batch 行（取完时会得到空列表，循环自然结束）。
        while True:
            # fetchmany 取一批；取到的行数以元组形式给出。
            rows = src_cur.fetchmany(batch)
            # 空了说明源表读完了，跳出循环。
            if not rows:
                break
            # execute_values 会把 rows 展开成「(...), (...), ...」拼到 %s 位置，
            # 一次网络往返就能插入上千行，比逐行 INSERT 快一个数量级。
            psycopg2.extras.execute_values(dst_cur, insert_sql, rows, page_size=batch)
            # 每批提交一次：这样即使中途失败，已完成的批次也不会丢。
            dst.commit()
            # 累加本批行数。
            copied += len(rows)
            # 打印一行进度，让人能看到「正在动」，而不是干等。
            print(f"    ... 已写入 {copied} 行", flush=True)
    # 关闭源端游标，及时释放连接上的资源。
    src_cur.close()
    return copied


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    """脚本入口：连两个库 → 逐表同步 → 打印汇总报告。返回退出码（0 成功 / 1 有失败）。"""
    # 定义可以传的命令行参数。
    parser = argparse.ArgumentParser(
        description="把远程 MySQL 的业务表全量同步到本机 PostgreSQL（供结构化统计查询使用）"
    )
    # --tables：指定要同步哪些表（逗号分隔）。不传则用 .env 里 KB_TABLES 的清单。
    parser.add_argument(
        "--tables",
        default="",
        help="要同步的表，逗号分隔；不传则用 .env 的 KB_TABLES（如 procurement_notices,projects）",
    )
    # --batch：每批插入多少行。默认 1000（任务要求）。
    parser.add_argument("--batch", type=int, default=1000, help="每批插入行数（默认 1000）")
    args = parser.parse_args()

    # 读配置（全部来自 .env，脚本里不写死任何地址口令）。
    s = get_settings()

    # 决定要同步哪些表：命令行优先，其次用配置里的知识库表清单。
    if args.tables.strip():
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    else:
        tables = list(s.kb_tables)

    # 保存每张表的处理结果，最后统一汇报。
    report: dict[str, dict[str, Any]] = {}

    print("=" * 70)
    print("MySQL → PostgreSQL 同步")
    print(f"  源  ：{s.mysql_host}:{s.mysql_port}/{s.mysql_database}（只读）")
    print(f"  目标：{s.pg_host}:{s.pg_port}/{s.pg_database} schema={s.pg_schema}")
    print(f"  表  ：{', '.join(tables)}")
    print("=" * 70)

    # ——— 第一步：连源库。失败就立刻给人话原因并退出（后续无从谈起）———
    try:
        src = pymysql.connect(
            host=s.mysql_host,
            port=s.mysql_port,
            user=s.mysql_user,
            password=s.mysql_password,
            database=s.mysql_database,
            charset=s.mysql_charset,
            connect_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - 这里要接住任何连接失败并翻译成人话
        print(f"[错误] 连不上源 MySQL（{s.mysql_host}:{s.mysql_port}/{s.mysql_database}）：{exc}")
        print("       人话解释：请检查 .env 的 MYSQL_HOST/PORT/USER/PASSWORD/DATABASE 是否正确，")
        print("                 以及这台机器能不能访问该局域网地址（可先 ping 一下）。")
        return 1

    # ——— 第二步：连目标 PG。失败同样立刻退出 ———
    try:
        # client_encoding=utf8 保证中文能正确写入（否则中文可能变问号）。
        dst = psycopg2.connect(
            host=s.pg_host,
            port=s.pg_port,
            user=s.pg_user,
            password=s.pg_password,
            dbname=s.pg_database,
            connect_timeout=10,
            client_encoding="utf8",
        )
    except Exception as exc:  # noqa: BLE001
        # 源连接已经开了，这里要顺手关掉，避免连接泄漏。
        src.close()
        print(f"[错误] 连不上目标 PostgreSQL（{s.pg_host}:{s.pg_port}/{s.pg_database}）：{exc}")
        print("       人话解释：请确认本机 PostgreSQL 服务已启动、端口对，且 .env 的 PG_* 配置正确。")
        print("                 （PostgreSQL 默认不监听时，本机连接会直接被拒绝。）")
        return 1

    # ——— 第三步：逐表同步。每张表单独包一层 try，做到「失败隔离」———
    started = time.time()
    for table in tables:
        # 记录这张表的开始时间，算单表耗时。
        t0 = time.time()
        print(f"\n[表 {table}] 开始同步 ...")
        try:
            rows = copy_table(src, dst, s.pg_schema, table, args.batch)
            elapsed = round(time.time() - t0, 2)
            report[table] = {"ok": True, "rows": rows, "elapsed": elapsed, "error": None}
            print(f"[表 {table}] 完成：{rows} 行，耗时 {elapsed} 秒")
        except Exception as exc:  # noqa: BLE001 - 单表失败不能拖垮其它表
            # 【失败隔离的关键位置】把异常收在这里：记录错误 → 回滚未提交的事务 →
            # 继续下一张表。这样「一张表坏掉」不会让整次同步白跑。
            elapsed = round(time.time() - t0, 2)
            # 回滚：把这张表尚未提交的改动撤销，避免留下「一半数据」的残局。
            try:
                dst.rollback()
            except Exception:  # noqa: BLE001
                # 连回滚都失败说明连接可能已断；忽略，继续走汇报流程。
                pass
            report[table] = {"ok": False, "rows": 0, "elapsed": elapsed, "error": str(exc)}
            print(f"[表 {table}] 失败：{exc}")
            print("           人话解释：可能是源表不存在/没权限，或某个列类型 PG 不认识。")
            print("                     其它表会照常继续，不受影响。")

    # ——— 第四步：收尾。关连接 + 打印汇总 ———
    total_elapsed = round(time.time() - started, 2)
    # 无论成败都把两个连接关掉（关不掉也不该让脚本崩）。
    try:
        src.close()
        dst.close()
    except Exception:  # noqa: BLE001
        pass

    print("\n" + "=" * 70)
    print("同步结果汇总")
    print("=" * 70)
    # 逐表打印：成功显示行数与耗时，失败显示原因。
    for table, info in report.items():
        if info["ok"]:
            print(f"  [成功] {table:<22} {info['rows']:>8} 行   {info['elapsed']:>7} 秒")
        else:
            print(f"  [失败] {table:<22} {'-':>8}      {info['elapsed']:>7} 秒   原因：{info['error']}")
    print("-" * 70)
    print(f"  总耗时：{total_elapsed} 秒")
    # 有任何一张表失败就退出码 1，方便外部脚本判断。
    failed = [t for t, i in report.items() if not i["ok"]]
    if failed:
        print(f"  结论：有 {len(failed)} 张表失败（{', '.join(failed)}），详见上方原因。")
        return 1
    print("  结论：全部成功。")
    return 0


# 【启动守卫】只有「直接运行本文件」时才执行 main()；被别的脚本 import 时不会自动跑。
if __name__ == "__main__":
    # sys.exit 会把 main() 的返回值当成进程退出码（0=成功，1=失败）。
    sys.exit(main())