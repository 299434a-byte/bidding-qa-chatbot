# ============================================================================
# 文件：import_neo4j.py —— 「远程 MySQL → Neo4j 知识图谱」的建图脚本
#
# 【这个脚本是干什么的】
#   把远程局域网 MySQL（bidding_information_dai 库）的两张业务表：
#     · projects（933 个招标项目）
#     · procurement_notices（933 条采购公告）
#   导入到本机 Neo4j 知识图谱，建成「两类节点 + 一类关系」：
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
#   · 对远程 MySQL 只做 SELECT，绝不写入；
#   · 连不上 Neo4j 时明确报「服务未启动」，不硬试、不乱猜密码。
# ============================================================================

"""把远程 MySQL 的项目/公告导入本机 Neo4j 知识图谱（两类节点 + 一类关系）。

设计要点：
- **只读源库**：对远程 MySQL 只做 SELECT；
- **幂等**：全部用 MERGE，重复执行不产生重复数据；
- **精确匹配**：用 ``project_name`` 把项目和公告连起来；
- **批量写入**：用 UNWIND + 参数分批提交，稳且快；
- **连不上就明确报**：先探针检查 7687 端口，不通直接提示「服务未启动」。

命令行用法（需在 backend 目录下执行）::

    ..\\.venv\\Scripts\\python.exe import_neo4j.py
    ..\\.venv\\Scripts\\python.exe import_neo4j.py --batch 500
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


# --------------------------------------------------------------------------- #
# 要从源表读取哪些列（只取建图需要的，避免把无用字段也搬进来）
# --------------------------------------------------------------------------- #
# Project 节点需要的 6 个属性（顺序与下面的 Cypher 一一对应）。
PROJECT_COLUMNS = ["project_name", "project_id", "budget_amount", "method", "agency", "project_date"]
# Notice 节点需要的 5 个属性。
NOTICE_COLUMNS = ["project_name", "title", "purchaser", "procurement_date", "content"]


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
    parser = argparse.ArgumentParser(description="把远程 MySQL 的项目/公告导入本机 Neo4j 知识图谱")
    parser.add_argument("--batch", type=int, default=500, help="每批写入行数（默认 500）")
    args = parser.parse_args()

    # 全部连接信息来自 .env。
    s = get_settings()

    print("=" * 70)
    print("MySQL → Neo4j 建图")
    print(f"  源（MySQL）：{s.mysql_host}:{s.mysql_port}/{s.mysql_database}（只读）")
    print(f"  目标（Neo4j）：{s.neo4j_uri}（库 {s.neo4j_database}）")
    print(f"  批大小：{args.batch} 行/批")
    print("=" * 70)

    # ——— 第 0 步：探针检查 Neo4j 端口，不通就明确报「服务未启动」，直接退出 ———
    alive, host, port = neo4j_port_open(s.neo4j_uri)
    if not alive:
        print(f"[错误] Neo4j 服务未启动：{host}:{port} 端口连不上（{s.neo4j_uri}）。")
        print("       人话解释：图数据库没有在运行，脚本不会硬试。请先启动 Neo4j 再重跑本脚本。")
        return 1

    # ——— 第 1 步：连源 MySQL（只读）———
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

    # ——— 第 2 步：连 Neo4j。端口虽通，但账号密码可能不对，这里再握手一次 ———
    try:
        driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        src.close()
        print(f"[错误] Neo4j 连接失败（{s.neo4j_uri}）：{exc}")
        print("       人话解释：端口是通的，但握手/认证没通过——多半是 NEO4J_USER / NEO4J_PASSWORD 不对。")
        print("                 请核对 .env 里的这两个值是否与图库一致。")
        return 1

    # ——— 第 3 步：读源数据（读成字典列表）———
    started = time.time()
    print("\n[读取] 从 MySQL 取 projects 与 procurement_notices ...")
    try:
        project_rows = fetch_rows(src, "projects", PROJECT_COLUMNS)
        notice_rows = fetch_rows(src, "procurement_notices", NOTICE_COLUMNS)
    except Exception as exc:  # noqa: BLE001
        driver.close()
        src.close()
        print(f"[错误] 读取源表失败：{exc}")
        print("       人话解释：多半是表名/列名不对，或该账号没有这两张表的读取权限。")
        return 1
    finally:
        # 源库用完了就关掉，及时释放连接。
        src.close()

    # 把 MySQL 的值翻译成 Neo4j 能存的样子（decimal→float、date→ISO 字符串）。
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
            created_nodes += after - before
            print(f"       :Project 节点共 {after} 个（本次新建 {after - before} 个）")

            # 4.3 写 Notice 节点。
            print("[建图] 写入 :Notice 节点 ...")
            before = session.run("MATCH (n:Notice) RETURN count(n) AS c").single()["c"]
            load_notices(session, notice_rows, args.batch)
            after = session.run("MATCH (n:Notice) RETURN count(n) AS c").single()["c"]
            created_nodes += after - before
            print(f"       :Notice 节点共 {after} 个（本次新建 {after - before} 个）")

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

    # ——— 第 5 步：打印结果报告 ———
    print("\n" + "=" * 70)
    print("建图结果汇总")
    print("=" * 70)
    print(f"  :Project 节点数            {n_proj}")
    print(f"  :Notice  节点数            {n_notice}")
    print(f"  (:Project)-[:PUBLISHED]->(:Notice) 关系数  {n_rel}")
    print(f"  本次新建：节点 {created_nodes} 个，关系 {created_rels} 条")
    print(f"  总耗时：{elapsed} 秒")
    print("-" * 70)
    if n_rel >= 900:
        print("  结论：关系数已达 900+，建图成功。")
    else:
        print(f"  结论：关系数 {n_rel} 少于 900，请检查两张表的 project_name 是否能对应上。")
    return 0


# 【启动守卫】只有「直接运行本文件」时才执行 main()；被别的脚本 import 时不会自动跑。
if __name__ == "__main__":
    sys.exit(main())