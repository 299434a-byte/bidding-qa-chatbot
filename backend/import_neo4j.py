# ============================================================================
# 文件：import_neo4j.py —— 「业务库 → Neo4j 知识图谱」的建图脚本
#
# 【这个脚本是干什么的】
#   把业务库（默认本机 PostgreSQL；--source mysql 时为原远程局域网 MySQL）的两张表：
#     · projects（933 个招标项目）
#     · procurement_notices（933 条采购公告）
#   导入到 Neo4j 知识图谱（本地或 Aura 云服务，由 .env 的 NEO4J_* 决定），建成「两类节点 + 一类关系」：
#     · (:Project) 招标项目节点
#     · (:Notice)  采购公告节点
#     · (:Project)-[:PUBLISHED]->(:Notice)  项目「发布」了公告
#   建好之后，后端 Agent 就能回答「某项目发布了哪些公告」「某公司中标了哪些项目」
#   这类「关系型/多跳」问题（向量检索不擅长的场景）。
#
# 【和前面脚本的分工】
#   · sync_pg.py   → PostgreSQL：适合「统计/计数/求和/精确筛选」
#   · import_neo4j.py → Neo4j：适合「实体之间的关系/多跳查询」
#   两者互不冲突，各管一摊。
#
# 【先懂四个概念（大白话）】
#   1) 图数据库（Neo4j）：数据存成「点(节点) + 线(关系)」的网络，而不是表格。
#      点=事物（项目、公告），线=事物之间的联系（项目发布了公告）。
#   2) 节点(Node)：就是图里的一个「点」，可以有标签(:Project)和一堆属性
#      （如 project_name、budget_amount）。标签像「类型」，属性像「字段」。
#   3) 关系(Relationship)：连接两个点的一条「线」，有方向和类型（这里叫 PUBLISHED）。
#      本脚本中方向是「项目 → 公告」，表示项目发布了这条公告。
#   4) MERGE（幂等）：Neo4j 的一条命令，意思是「有就复用、没有才新建」。
#      反复跑这个脚本，不会产生重复节点/重复关系——这叫「幂等」。
#
# 【硬性约束】
#   · 对源库（PostgreSQL / MySQL）只做 SELECT，绝不写入；
#   · 连不上 Neo4j 时明确报错，不硬试、不乱猜密码；打印连接信息时密码必须脱敏。
# ============================================================================

"""把业务库的项目/公告导入 Neo4j 知识图谱（两类节点 + 一类关系）。

设计要点：
- **数据源可选**：``--source pg``（默认取 .env 的 KB_SOURCE，当前 postgres）走本机
  PostgreSQL，复用 ``app.pg_source`` 的只读连接；``--source mysql`` 保持原远程 MySQL 行为不变；
- **只读源库**：PG 连接带 default_transaction_read_only=on，MySQL 只做 SELECT；
- **幂等**：全部用 MERGE，重复执行不产生重复数据；
- **精确匹配**：用 ``project_name`` 把项目和公告连起来；
- **批量写入**：用 UNWIND + 参数分批提交，稳且快；读取侧同样按 --batch 分批取回；
- **连不上就明确报**：本机地址先探针检查 7687 端口；云端（neo4j+s://）跳过探针，
  交给驱动握手判定。

命令行用法（需在 backend 目录下执行）::

    ..\\.venv\\Scripts\\python.exe import_neo4j.py                    # 按 KB_SOURCE（postgres）读本机 PG
    ..\\.venv\\Scripts\\python.exe import_neo4j.py --source pg --batch 500
    ..\\.venv\\Scripts\\python.exe import_neo4j.py --source mysql     # 原远程 MySQL 行为，完全不变
"""

# 打开「类型注解延迟求值」开关：让 list[str]、str | None 这类新写法在老版本 Python 也能写。
from __future__ import annotations

# argparse：解析命令行参数（--batch 等）。标准库自带。
import argparse
# socket：做「端口探针」——先看看 7687 端口通不通，用来判断 Neo4j 是否已启动。
import socket
# sys：设置进程退出码（0=成功，1=失败）。
import sys
# time：记录开始时刻，结尾相减得到总耗时。
import time
# typing.Any：表示「任意类型」，用于类型标注。
from typing import Any
# urllib.parse.urlparse：把 "bolt://127.0.0.1:7687" 拆成 host / port，方便做端口探针。
from urllib.parse import urlparse

# pymysql：MySQL 的 Python 驱动（读远程源库用）。
import pymysql
# GraphDatabase：Neo4j 官方 Python 驱动的入口，用它创建「驱动器」连图库。
from neo4j import GraphDatabase

# 读取项目配置（它就是读 .env 的模块）。源库与图库的连接信息全部来自 .env。
from app.config import get_settings
# PG 分支复用 pg_source 的只读连接（default_transaction_read_only=on），不另写一套 psycopg2。
from app.pg_source import pg_connection


# --------------------------------------------------------------------------- #
# 要从源表读取哪些列（只取建图需要的，避免把无用字段也搬进来）
# 表名与列名只在下面的常量里出现，调用处一律引用常量，不散落字面量。
# --------------------------------------------------------------------------- #
# 两张源表（MySQL 与 PostgreSQL 同名）。
PROJECT_TABLE = "projects"
NOTICE_TABLE = "procurement_notices"
# Project 节点需要的 6 个属性（顺序与下面的 Cypher 一一对应）。
PROJECT_COLUMNS = ["project_name", "project_id", "budget_amount", "method", "agency", "project_date"]
# Notice 节点需要的 5 个属性。
NOTICE_COLUMNS = ["project_name", "title", "purchaser", "procurement_date", "content"]


def mask_secret(value: str) -> str:
    """密码脱敏：任何打印场合都只显示 ****，明文绝不进终端与日志。"""
    return "****" if value else "(空)"


# --------------------------------------------------------------------------- #
# 小工具：把 MySQL 的值「翻译」成 Neo4j 能安全存储的类型
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float | None:
    """把 MySQL 的 decimal（精确小数）转成 Python 的 float。

    【为什么必须转】Neo4j 只认「整数 / 浮点 / 字符串 / 布尔 / 日期」这几种类型，
    不认 Python 的 Decimal。直接塞进去会报错，所以统一转成 float。
    """
    # None（数据库里的空值）原样返回，转成 None 后 Cypher 里相当于「不设这个属性」。
    if value is None:
        return None
    return float(value)


def _to_iso(value: Any) -> str | None:
    """把 MySQL 的日期/日期时间转成 ISO 字符串（如 2024-06-27）。

    【为什么转字符串】日期以字符串存进图库最省心，跨版本最兼容，
    展示（如前端表格）时也天然可读。
    """
    if value is None:
        return None
    # date/datetime 对象都有 .isoformat() 方法；万一不是，则直接 str() 兜底。
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# --------------------------------------------------------------------------- #
# 探针：检查 Neo4j 端口通不通（用来区分「服务未启动」和其它错误）
# --------------------------------------------------------------------------- #
def neo4j_port_open(uri: str, timeout: float = 2.0) -> tuple[bool, str, int]:
    """尝试对 Neo4j 的 bolt 端口做一次 TCP 连接，判断服务是否在监听。

    【参数解释】uri —— Neo4j 连接地址，如 "bolt://127.0.0.1:7687"。
    【返回值】三元组 (是否连通, 主机, 端口)。
    """
    # 拆地址：host 是主机名、port 是端口（urlparse 会把端口解析成整数）。
    parsed = urlparse(uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7687
    # 拿一个「套接字」尝试连接，超时 2 秒，避免卡死。
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True, host, port
    except OSError:
        # 连不上（拒绝/超时）就返回 False，让调用方给出「服务未启动」的明确提示。
        return False, host, port
    finally:
        # 不管成功失败都关掉套接字，避免句柄泄漏。
        sock.close()


# --------------------------------------------------------------------------- #
# 从 MySQL 读整张表（只读指定列）
# --------------------------------------------------------------------------- #
def fetch_rows(conn: pymysql.connections.Connection, table: str, columns: list[str]) -> list[dict[str, Any]]:
    """把源表指定列的全部数据读成「字典列表」，每项形如 {"列名": 值}。

    【参数解释】
        conn    —— 已连上的源 MySQL 连接；
        table   —— 表名；
        columns —— 要读取的列名列表。

    【为什么用 DictCursor】它让查询结果直接是「字典」，按下标取值容易错位；
    按列名取值（row["project_name"]）更直观、更不易出错。
    """
    # 列名不是「值」，不能用占位符；这里列名是脚本内写死的常量，无注入风险。
    cols_sql = ", ".join("`" + c + "`" for c in columns)
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(f"SELECT {cols_sql} FROM `{table}`")
        return list(cur.fetchall())


# --------------------------------------------------------------------------- #
# 从 PostgreSQL 读整张表（只读指定列，按 batch 分批取回）
# --------------------------------------------------------------------------- #
def fetch_rows_pg(table: str, columns: list[str], batch: int) -> list[dict[str, Any]]:
    """用 pg_source 的只读连接读源表指定列，每 batch 行一批取回，返回字典列表。

    【为什么复用 pg_connection 而不是自己 psycopg2.connect】
        pg_connection 内置 default_transaction_read_only=on（源库绝不被写）与
        连接失败的统一翻译，一行连接代码都不重复造。
    【为什么列名走常量白名单】SELECT 只拼脚本内写死的列清单（PROJECT_COLUMNS /
        NOTICE_COLUMNS），列名不是占位符参数，但因为来源是常量，同样无注入风险。
    【为什么不直接用 pg_source.iter_rows】iter_rows 会把 None 归一成 ""（JSON 场景），
        而建图要求「None 保持 None」（空值不设节点属性），所以这里取原始值，
        交给与 MySQL 分支共用的 _to_float / _to_iso 统一归一化。
    """
    # 双引号是 PG 的标识符引用；columns 来自常量清单，无注入风险。
    cols_sql = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
    table_sql = '"' + table.replace('"', '""') + '"'
    rows: list[dict[str, Any]] = []
    with pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols_sql} FROM {table_sql}")
            names = [d.name for d in (cur.description or [])]
            while True:
                chunk = cur.fetchmany(batch)
                if not chunk:
                    break
                rows.extend(dict(zip(names, record)) for record in chunk)
    return rows


# --------------------------------------------------------------------------- #
# 在 Neo4j 里建索引（加速按 project_name 匹配）
# --------------------------------------------------------------------------- #
def ensure_indexes(session: Any) -> None:
    """给 Project / Notice 的 project_name 建索引。

    【为什么要建索引】连接关系时要按 project_name 找节点。没有索引就得「全表扫」，
    数据一多就慢。建了索引就像给书加了目录，查找瞬间完成。
    【IF NOT EXISTS】已经存在就不重复建，保证脚本可重复运行。
    """
    # Neo4j 5+/2026 语法：CREATE INDEX 名字 IF NOT EXISTS FOR (变量:标签) ON (变量.属性)
    session.run("CREATE INDEX project_name_index IF NOT EXISTS FOR (p:Project) ON (p.project_name)")
    session.run("CREATE INDEX notice_name_index IF NOT EXISTS FOR (n:Notice) ON (n.project_name)")


# --------------------------------------------------------------------------- #
# 批量导入 Project 节点
# --------------------------------------------------------------------------- #
def load_projects(session: Any, rows: list[dict[str, Any]], batch: int) -> int:
    """把项目数据分批写成 :Project 节点，返回处理的行数。

    【核心 Cypher 讲解】
        UNWIND $rows AS row      —— 把参数里的一批数据「展开」成一行行；
        MERGE (p:Project {...})  —— 按 project_name 找节点：有就复用，没有就新建（幂等关键）；
        SET p.xxx = row.xxx      —— 把这一行的其它列填进节点属性。
    """
    query = (
        "UNWIND $rows AS row "
        "MERGE (p:Project {project_name: row.project_name}) "
        "SET p.project_id = row.project_id, "
        "    p.budget_amount = row.budget_amount, "
        "    p.method = row.method, "
        "    p.agency = row.agency, "
        "    p.project_date = row.project_date"
    )
    total = 0
    # 分批：每 batch 行提交一次，控制单次事务大小，稳且快。
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        session.run(query, rows=chunk)
        total += len(chunk)
        print(f"    ... Project 已处理 {total} 行", flush=True)
    return total


# --------------------------------------------------------------------------- #
# 批量导入 Notice 节点
# --------------------------------------------------------------------------- #
def load_notices(session: Any, rows: list[dict[str, Any]], batch: int) -> int:
    """把公告数据分批写成 :Notice 节点，返回处理的行数。

    【为什么 MERGE 的键用 project_name + title】一份公告由「项目名 + 标题」共同确定，
    用这两个属性做键，既能唯一识别，又能保证重复运行不产生重复节点（幂等）。
    """
    query = (
        "UNWIND $rows AS row "
        "MERGE (n:Notice {project_name: row.project_name, title: row.title}) "
        "SET n.purchaser = row.purchaser, "
        "    n.procurement_date = row.procurement_date, "
        "    n.content = row.content"
    )
    total = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        session.run(query, rows=chunk)
        total += len(chunk)
        print(f"    ... Notice 已处理 {total} 行", flush=True)
    return total


# --------------------------------------------------------------------------- #
# 建立 Project -[:PUBLISHED]-> Notice 关系
# --------------------------------------------------------------------------- #
def link_published(session: Any) -> int:
    """按 project_name 精确匹配，建立「项目发布公告」的关系，返回新建关系数。

    【Cypher 讲解】
        MATCH (p:Project)                              —— 遍历每个项目节点；
        MATCH (n:Notice {project_name: p.project_name})—— 用索引快速找到「同名」的公告节点；
        MERGE (p)-[:PUBLISHED]->(n)                    —— 已有这条关系就复用，没有才新建（幂等）。
    【为什么用这种写法而不是 WHERE 全比对】它能让 Neo4j 借用 project_name 索引，
    本质是「按名字直接命中」，比两两比对快得多。
    """
    # ① 用 counters 统计「新建了多少条关系」。
    query = (
        "MATCH (p:Project) "
        "MATCH (n:Notice {project_name: p.project_name}) "
        "MERGE (p)-[:PUBLISHED]->(n)"
    )
    result = session.run(query)
    # consume() 会等工作跑完并返回统计信息，relationships_created 就是新建关系数。
    summary = result.consume()
    return summary.counters.relationships_created


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    """脚本入口：探针 → 连两库 → 建节点 → 建关系 → 打印报告。返回退出码（0 成功 / 1 失败）。"""
    parser = argparse.ArgumentParser(description="把业务库（PG/MySQL）的项目/公告导入 Neo4j 知识图谱")
    parser.add_argument("--batch", type=int, default=500, help="每批读/写行数（默认 500）")
    parser.add_argument(
        "--source",
        choices=["pg", "mysql"],
        default=None,
        help="数据源：pg=本机 PostgreSQL；mysql=原远程 MySQL（缺省按 .env 的 KB_SOURCE）",
    )
    args = parser.parse_args()

    # 全部连接信息来自 .env。
    s = get_settings()
    # 缺省跟随 KB_SOURCE：postgres → pg，否则 mysql（--source 显式指定时以参数为准）。
    source = args.source or ("pg" if s.kb_source_is_postgres else "mysql")

    print("=" * 70)
    print(f"{'PostgreSQL' if source == 'pg' else 'MySQL'} → Neo4j 建图")
    if source == "pg":
        print(f"  源（PostgreSQL）：{s.pg_host}:{s.pg_port}/{s.pg_database}（只读）")
    else:
        print(f"  源（MySQL）：{s.mysql_host}:{s.mysql_port}/{s.mysql_database}（只读）")
    # 密码一律脱敏，明文绝不进终端与日志。
    print(f"  目标（Neo4j）：{s.neo4j_uri}（库 {s.neo4j_database}，用户 {s.neo4j_user}，密码 {mask_secret(s.neo4j_password)}）")
    print(f"  批大小：{args.batch} 行/批")
    print("=" * 70)

    # ——— 第 0 步：本机地址先探针检查端口；云端（neo4j+s:// 等）跳过，交给驱动握手 ———
    uri_host = urlparse(s.neo4j_uri).hostname or ""
    if uri_host in ("127.0.0.1", "localhost", "::1"):
        alive, host, port = neo4j_port_open(s.neo4j_uri)
        if not alive:
            print(f"[错误] Neo4j 服务未启动：{host}:{port} 端口连不上（{s.neo4j_uri}）。")
            print("       人话解释：图数据库没有在运行，脚本不会硬试。请先启动 Neo4j 再重跑本脚本。")
            return 1
    else:
        print(f"[提示] 目标是云端地址（{uri_host}），跳过本地端口探针，交给驱动握手判定。")

    # ——— 第 1 步：连源 MySQL（仅 --source mysql；只读）———
    # PG 分支不在此预连：fetch_rows_pg 内部走 pg_source.pg_connection（同样只读）。
    src = None
    if source == "mysql":
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
        except Exception as exc:  # noqa: BLE001 - 接住连接失败并翻译成人话
            print(f"[错误] 连不上源 MySQL（{s.mysql_host}:{s.mysql_port}/{s.mysql_database}）：{exc}")
            print("       人话解释：请检查 .env 的 MYSQL_* 配置，以及能否访问该局域网地址。")
            return 1

    # ——— 第 2 步：连 Neo4j。本机已探端口；云端直连。账号密码不对时在这里握手报错 ———
    try:
        driver = GraphDatabase.driver(
            s.neo4j_uri,
            auth=(s.neo4j_user, s.neo4j_password),
            connection_timeout=s.neo4j_timeout,
        )
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        if src is not None:
            src.close()
        print(f"[错误] Neo4j 连接失败（{s.neo4j_uri}，用户 {s.neo4j_user}，密码 {mask_secret(s.neo4j_password)}）：{exc}")
        print("       人话解释：握手/认证没通过——多半是 NEO4J_USER / NEO4J_PASSWORD 不对（Aura 的用户名是实例 ID），")
        print("                 或实例被闲置暂停（去 console.neo4j.io 点 Resume），或本机代理拦截了域名解析。")
        return 1

    # 打印服务端版本（本地/Aura 通用），读不到不影响导入。
    try:
        with driver.session(database=s.neo4j_database) as version_session:
            comps = version_session.run(
                "CALL dbms.components() YIELD versions, edition RETURN versions, edition LIMIT 1"
            ).data()
        if comps:
            print(f"  服务端版本：{comps[0].get('versions')}（{comps[0].get('edition')}）")
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 读取服务端版本失败（不影响导入）：{exc}")

    # ——— 第 3 步：读源数据（读成字典列表）———
    started = time.time()
    print(f"\n[读取] 从 {'PostgreSQL' if source == 'pg' else 'MySQL'} 取 {PROJECT_TABLE} 与 {NOTICE_TABLE} ...")
    try:
        if source == "pg":
            project_rows = fetch_rows_pg(PROJECT_TABLE, PROJECT_COLUMNS, args.batch)
            notice_rows = fetch_rows_pg(NOTICE_TABLE, NOTICE_COLUMNS, args.batch)
        else:
            assert src is not None  # mysql 分支上面已连上
            project_rows = fetch_rows(src, PROJECT_TABLE, PROJECT_COLUMNS)
            notice_rows = fetch_rows(src, NOTICE_TABLE, NOTICE_COLUMNS)
    except Exception as exc:  # noqa: BLE001
        driver.close()
        if src is not None:
            src.close()
        print(f"[错误] 读取源表失败：{exc}")
        print("       人话解释：多半是表名/列名不对、账号没有这两张表的读取权限，或源库连不上。")
        return 1
    finally:
        # 源库用完了就关掉，及时释放连接（PG 分支 src 为 None，无需处理）。
        if src is not None:
            src.close()

    # 把源库的值翻译成 Neo4j 能存的样子（Decimal→float、日期→ISO 字符串、None 保持 None）。
    # PG 返回原始 Decimal/date 对象，MySQL 返回 pymysql 的 Decimal/date —— 同一套归一化通吃。
    for r in project_rows:
        r["budget_amount"] = _to_float(r.get("budget_amount"))
        r["project_date"] = _to_iso(r.get("project_date"))
    for r in notice_rows:
        r["procurement_date"] = _to_iso(r.get("procurement_date"))
        # title 用 "" 兜底：MERGE 的键不能是 null，否则每次都会被当成「新节点」。
        r["title"] = r.get("title") or ""

    print(f"       projects：{len(project_rows)} 行；procurement_notices：{len(notice_rows)} 行")

    # ——— 第 4 步：写图（建索引 → 建两类节点 → 建关系）———
    created_nodes = 0
    created_rels = 0
    try:
        with driver.session(database=s.neo4j_database) as session:
            # 4.1 建索引（加速后面的按名匹配）。
            print("\n[建图] 创建索引 ...")
            ensure_indexes(session)

            # 4.2 写 Project 节点。
            print("[建图] 写入 :Project 节点 ...")
            before = session.run("MATCH (p:Project) RETURN count(p) AS c").single()["c"]
            load_projects(session, project_rows, args.batch)
            after = session.run("MATCH (p:Project) RETURN count(p) AS c").single()["c"]
            proj_created = after - before
            created_nodes += proj_created
            print(f"       :Project 节点共 {after} 个（本次写入 {proj_created} 个，复用 {after - proj_created} 个）")

            # 4.3 写 Notice 节点。
            print("[建图] 写入 :Notice 节点 ...")
            before = session.run("MATCH (n:Notice) RETURN count(n) AS c").single()["c"]
            load_notices(session, notice_rows, args.batch)
            after = session.run("MATCH (n:Notice) RETURN count(n) AS c").single()["c"]
            notice_created = after - before
            created_nodes += notice_created
            print(f"       :Notice 节点共 {after} 个（本次写入 {notice_created} 个，复用 {after - notice_created} 个）")

            # 4.4 建「项目发布公告」关系。
            print("[建图] 创建 (:Project)-[:PUBLISHED]->(:Notice) 关系 ...")
            created_rels = link_published(session)

            # 4.5 统计最终规模。
            n_proj = session.run("MATCH (p:Project) RETURN count(p) AS c").single()["c"]
            n_notice = session.run("MATCH (n:Notice) RETURN count(n) AS c").single()["c"]
            n_rel = session.run("MATCH ()-[r:PUBLISHED]->() RETURN count(r) AS c").single()["c"]
    except Exception as exc:  # noqa: BLE001
        driver.close()
        print(f"[错误] 写图失败：{exc}")
        print("       人话解释：可能是图库权限/空间问题，或某个属性类型不被支持。")
        return 1
    finally:
        driver.close()

    elapsed = round(time.time() - started, 2)

    # ——— 第 5 步：打印结果报告（写入/复用口径：本次新建 vs 已存在复用）———
    print("\n" + "=" * 70)
    print("建图结果汇总")
    print("=" * 70)
    print(f"  :Project 节点数            {n_proj}（写入 {proj_created}，复用 {n_proj - proj_created}）")
    print(f"  :Notice  节点数            {n_notice}（写入 {notice_created}，复用 {n_notice - notice_created}）")
    print(f"  (:Project)-[:PUBLISHED]->(:Notice) 关系数  {n_rel}（写入 {created_rels}，复用 {n_rel - created_rels}）")
    print(f"  本次写入：节点 {created_nodes} 个，关系 {created_rels} 条")
    print(f"  源表读取：{PROJECT_TABLE} {len(project_rows)} 行，{NOTICE_TABLE} {len(notice_rows)} 行")
    print(f"  总耗时：{elapsed} 秒")
    print("-" * 70)
    # 自检断言：关系数必须 ≥ 900（两表以 project_name 关联，933 行对 933 行应全部连上）。
    if n_rel >= 900:
        print("  结论：关系数已达 900+，建图成功。")
        return 0
    print(f"  结论：关系数 {n_rel} 少于 900——两张表的 project_name 没有对应上，或源表行数不对。")
    print(f"        请检查：{PROJECT_TABLE} 与 {NOTICE_TABLE} 读到的行数是否为预期的 933/933。")
    return 1


# 【启动守卫】只有「直接运行本文件」时才执行 main()；被别的脚本 import 时不会自动跑。
if __name__ == "__main__":
    sys.exit(main())