# ============================================================================
# 文件：copy_mysql.py —— 「远程 MySQL → 本机 MySQL」的整库拷贝脚本（备用方案）
#
# 【这个脚本是干什么的】
#   把远程局域网 MySQL（地址从 .env 的 MYSQL_HOST 读取）里 bidding_information_dai 库的两张业务表
#   （procurement_notices 采购公告、projects 招标项目）整表复制到「本机 MySQL」
#   （默认 127.0.0.1:3306）。拷贝完成后，即使远程库连不上，本机也有一份能用的副本。
#
# 【和 sync_pg.py 的区别】
#   · sync_pg.py  把数据搬到 PostgreSQL（为了「结构化统计查询」，如 count/sum）；
#   · copy_mysql.py 把数据搬到本机 MySQL（为了「数据留底 / 离线可用」，表结构和源库一模一样）。
#   两者互不冲突，可以都跑。
#
# 【为什么叫「备用」】
#   本机 MySQL 的账号密码用户还没确定，所以本脚本写好后「先不执行」，等确认账号再跑。
#   没提供账号时，脚本不会乱试乱撞，而是打印提示并安全退出。
#
# 【谁调用它】
#   人工在命令行执行（在 backend 目录下），例如：
#       ..\.venv\Scripts\python.exe copy_mysql.py --user root --password 你的密码
#       ..\.venv\Scripts\python.exe copy_mysql.py --user root --password 你的密码 --with-companies
#   （--with-companies 才会连 70 万行的 companies 一起拷，默认不拷。）
#
# 【它依赖哪些配置】
#   · 源（远程 MySQL）：.env 里的 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD /
#     MYSQL_DATABASE / MYSQL_CHARSET —— 全部从 .env 读，脚本里不写死。
#   · 目标（本机 MySQL）：不写死在脚本里，用命令行参数传，或用环境变量传：
#       --host / --port 有默认值（127.0.0.1 / 3306）
#       --user / --password 必须提供（或用环境变量 LOCAL_MYSQL_USER / LOCAL_MYSQL_PASSWORD）
#
# 【先懂五个概念（大白话）】
#   1) 整库拷贝：把源库的「建表语句」和「整张表的数据」都拿到目标库重建一遍。
#   2) SHOW CREATE TABLE：MySQL 的一条命令，能「吐」出某张表完整的建表 SQL。
#      直接拿它去目标库执行，就能建出「和源表一模一样」的表（列名/类型/索引/字符集全对）。
#      这比自己手写建表语句更保险，源表改了结构重跑就自动跟上。
#   3) 分批插入：把上万行拆成每 1000 行一组插入。一次性插太多会撑爆内存或超过 MySQL 的
#      「单个数据包上限」（max_allowed_packet）而报错，分批最稳。
#   4) 只读源库：脚本对远程 MySQL 只做 SELECT / SHOW，绝不写入，避免误伤生产数据。
#   5) 失败隔离：某张表拷贝失败（比如表不存在），不让整个脚本崩掉，记下错误继续下一张，
#      最后统一汇报——这叫「不拖垮别的任务」。
# ============================================================================

"""把远程 MySQL 的 bidding_information_dai 两张表整表拷贝到本机 MySQL。

设计要点：
- **只读源库**：对远程 MySQL 只做 SELECT / SHOW，绝不写入；
- **全量重建**：目标表先 DROP 再按源表的 ``SHOW CREATE TABLE`` 原样重建；
- **分批插入**：默认每 1000 行提交一批，控制内存与数据包大小；
- **可选用开关**：``--with-companies`` 决定是否连 70 万行的 companies 一起拷（默认不拷）；
- **失败隔离**：单表失败不影响其他表，最后汇总并给出人话原因；
- **不乱试口令**：目标库账号密码未提供时，打印提示并退出，不会盲目尝试。

命令行用法（需在 backend 目录下执行）::

    ..\\.venv\\Scripts\\python.exe copy_mysql.py --user root --password 你的密码
    ..\\.venv\\Scripts\\python.exe copy_mysql.py --user root --password 你的密码 --with-companies
    ..\\.venv\\Scripts\\python.exe copy_mysql.py --user root --password 你的密码 --tables projects
"""

# 打开「类型注解延迟求值」开关：让 list[str]、str | None 这类新写法在 Python 3.9 及以前
# 也能正常书写（注解先当字符串存着，需要时才解析）。必须放在文件最顶部。
from __future__ import annotations

# argparse：解析命令行参数（--user、--password、--with-companies 这类）。标准库自带。
import argparse
# os：读环境变量（给「不想在命令行明文写密码」的同学留的后路）。
import os
# sys：设置进程退出码（0=成功，1=有表失败，2=参数缺失），让外部脚本能判断结果。
import sys
# time：记录开始时刻，结尾相减得到总耗时。
import time
# typing.Any：表示「任意类型」，用于类型标注。
from typing import Any

# pymysql：MySQL 的 Python 驱动（源库、目标库都用它连）。
import pymysql
# pymysql.cursors 里的 SSCursor（服务端游标）：「取一批、用一批」，大表也不爆内存。
import pymysql.cursors

# 读取项目配置（它就是读 .env 的模块）。只用它的读配置能力，脚本可独立运行。
from app.config import get_settings


# --------------------------------------------------------------------------- #
# 给标识符加反引号
# --------------------------------------------------------------------------- #
def backtick(name: str) -> str:
    """给 MySQL 标识符（库名/表名/列名）加反引号，并转义内部反引号。

    【为什么要加引号】有些名字可能与 SQL 关键字重名（比如 order、user），
    加反引号后 MySQL 就会把它当「名字」而不是「关键字」，避免语法错误。
    """
    return "`" + name.replace("`", "``") + "`"


# --------------------------------------------------------------------------- #
# 读取源表结构（拿建表语句）
# --------------------------------------------------------------------------- #
def read_create_table(conn: pymysql.connections.Connection, table: str) -> str:
    """读取源表的完整建表语句（CREATE TABLE ...）。

    【原理】MySQL 提供 ``SHOW CREATE TABLE 表名`` 命令，返回两列：
    第 0 列是表名，第 1 列就是这条表完整的建表 SQL 文本。我们只要第 1 列。

    【参数解释】
        conn  —— 已连上的源 MySQL 连接；
        table —— 表名。

    【返回值】可直接在目标库执行的建表 SQL 字符串。
    """
    with conn.cursor() as cur:
        # 表名不是「值」，不能用 %s 占位，只能拼字符串；这里用反引号包住，无注入风险。
        cur.execute("SHOW CREATE TABLE " + backtick(table))
        row = cur.fetchone()
    # row[1] 就是那条 CREATE TABLE 语句。
    return row[1]


def read_columns(conn: pymysql.connections.Connection, table: str) -> list[str]:
    """读取源表的列名列表（顺序与表结构一致），用于拼 INSERT 语句。

    【为什么需要它】插入数据时，INSERT 里的列顺序必须和 VALUES 的值顺序严格对应，
    否则「姓名」可能被插进「地址」列。这里按源表列定义的原顺序取出，保证不错位。
    """
    with conn.cursor() as cur:
        cur.execute("SHOW FULL COLUMNS FROM " + backtick(table))
        rows = cur.fetchall()
    # 每一行的第 0 个字段就是列名。
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# 在目标库重建表
# --------------------------------------------------------------------------- #
def recreate_table(dst: pymysql.connections.Connection, table: str, create_sql: str) -> None:
    """在目标库「先删后建」一张表，结构完全照抄源表。

    【参数解释】
        dst        —— 已连上的目标 MySQL 连接；
        table      —— 表名；
        create_sql —— 源表的建表语句（read_create_table 的返回值）。

    【为什么先 DROP】不删的话，目标表若已存在，CREATE 会报「表已存在」，后面的导入全废。
    先删再建，就能保证「跑一次的结果 = 源表当前的结构」。

    【注意事项】DROP 会删掉目标表里的旧数据——这正是「全量重建」的预期行为，
    且只作用于本机（目标库），不会影响远程源库。
    """
    with dst.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS " + backtick(table))
        cur.execute(create_sql)
    dst.commit()


# --------------------------------------------------------------------------- #
# 复制数据（分批）
# --------------------------------------------------------------------------- #
def copy_table(
    src: pymysql.connections.Connection,
    dst: pymysql.connections.Connection,
    table: str,
    batch: int,
) -> int:
    """把一张表的数据从源库分批复制到目标库，返回复制成功的行数。

    【参数解释】
        src   —— 源 MySQL 连接；
        dst   —— 目标 MySQL 连接；
        table —— 表名；
        batch —— 每批插入多少行（默认 1000）。

    【返回值】实际写入的行数。
    """
    # ① 拿到源表完整建表语句，并在目标库按它重建（先删后建）。
    create_sql = read_create_table(src, table)
    recreate_table(dst, table, create_sql)

    # ② 取源表列名（顺序很关键，保证插入时值不会错位）。
    cols = read_columns(src, table)
    # 拼出 INSERT 语句：列名用反引号包住；%s 是占位符，executemany 会用它批量填值。
    insert_sql = (
        f"INSERT INTO {backtick(table)} "
        f"({', '.join(backtick(c) for c in cols)}) VALUES "
        f"({', '.join(['%s'] * len(cols))})"
    )

    # ③ 用服务端游标读源表：一次只取一批，大表也不爆内存。
    src_cur = src.cursor(pymysql.cursors.SSCursor)
    src_cur.execute("SELECT " + ", ".join(backtick(c) for c in cols) + " FROM " + backtick(table))

    copied = 0
    # ④ 循环取批 → 批量插入 → 提交。
    with dst.cursor() as dst_cur:
        while True:
            rows = src_cur.fetchmany(batch)
            if not rows:
                break
            # executemany 一次把整批行插进去，比逐行 INSERT 快很多。
            dst_cur.executemany(insert_sql, rows)
            # 每批提交一次：即使中途失败，已提交的批次不会白干。
            dst.commit()
            copied += len(rows)
            print(f"    ... 已写入 {copied} 行", flush=True)
    src_cur.close()
    return copied


# --------------------------------------------------------------------------- #
# 连接目标 MySQL（含「自动建库」）
# --------------------------------------------------------------------------- #
def connect_target(
    host: str,
    port: int,
    user: str,
    password: str,
    charset: str,
    database: str,
) -> pymysql.connections.Connection:
    """连上目标 MySQL，并确保数据库存在（不存在就自动建），最后连到该库。

    【为什么要「先连再建库再连」】
    新 MySQL 第一次连的时候，目标库还不存在，直接连过去会报「Unknown database」。
    所以分两步：先连上「不指定库」的连接（只为了有权限执行 CREATE DATABASE），
    建好库后再连一个「指定库」的连接，用来建表/插数据。

    【参数解释】host/port/user/password/charset 是目标库账号信息；database 是要建的库名。
    """
    # 第一步：不带 database 连上去，只为建库。
    root_conn = pymysql.connect(
        host=host, port=port, user=user, password=password, charset=charset, connect_timeout=10
    )
    with root_conn.cursor() as cur:
        # IF NOT EXISTS：库已存在时不报错，保证脚本可重复运行（幂等）。
        # CHARACTER SET utf8mb4：支持中文与 emoji，避免中文变问号。
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS {backtick(database)} "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
        )
    root_conn.commit()
    root_conn.close()

    # 第二步：连到（刚建好或已存在的）目标库，供后续建表/插数据使用。
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset=charset,
        connect_timeout=10,
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    """脚本入口：解析参数 → 连两个库 → 逐表拷贝 → 打印汇总。返回退出码。

    退出码约定：0 = 全部成功；1 = 有表失败；2 = 目标库账号密码没给（未执行）。
    """
    parser = argparse.ArgumentParser(
        description="把远程 MySQL 的业务表整表拷贝到本机 MySQL（数据留底/离线可用）"
    )
    # —— 目标（本机 MySQL）连接参数 ——
    parser.add_argument("--host", default="127.0.0.1", help="本机 MySQL 主机（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=3306, help="本机 MySQL 端口（默认 3306）")
    parser.add_argument("--user", default="", help="本机 MySQL 账号（或用环境变量 LOCAL_MYSQL_USER）")
    parser.add_argument("--password", default="", help="本机 MySQL 密码（或用环境变量 LOCAL_MYSQL_PASSWORD）")
    # —— 拷贝范围 ——
    parser.add_argument(
        "--tables",
        default="procurement_notices,projects",
        help="要拷贝的表，逗号分隔（默认两张业务表）",
    )
    parser.add_argument(
        "--with-companies",
        action="store_true",
        help="是否连同 companies（约 70 万行）一起拷贝；默认不拷",
    )
    # —— 性能 ——
    parser.add_argument("--batch", type=int, default=1000, help="每批插入行数（默认 1000）")
    args = parser.parse_args()

    # 读配置（源库信息全部来自 .env）。
    s = get_settings()

    # 目标库账号密码：命令行优先，其次读环境变量。都没有则「不执行，给提示」。
    target_user = args.user or os.getenv("LOCAL_MYSQL_USER", "")
    target_password = args.password or os.getenv("LOCAL_MYSQL_PASSWORD", "")
    if not target_user:
        print("=" * 70)
        print("[未执行] 还差本机 MySQL 的账号密码，脚本先不跑（避免乱试口令）。")
        print("=" * 70)
        print("请用下面任一方式提供后再运行：")
        print("  ① 命令行参数：")
        print("     ..\\.venv\\Scripts\\python.exe copy_mysql.py --user root --password 你的密码")
        print("  ② 环境变量（不想在命令行明文写密码时）：")
        print("     先设置 LOCAL_MYSQL_USER / LOCAL_MYSQL_PASSWORD 再运行本脚本")
        print()
        print("提示：如果你不知道本机 MySQL 的账号密码，可以：")
        print("  · 优先试 root（安装 MySQL 时设的那个密码）；")
        print("  · 或在本机 MySQL 里建一个专用账号，例如：")
        print("      CREATE USER 'copier'@'localhost' IDENTIFIED BY '你的密码';")
        print("      GRANT ALL PRIVILEGES ON bidding_information_dai.* TO 'copier'@'localhost';")
        print("      FLUSH PRIVILEGES;")
        return 2

    # 组装要拷贝的表清单：默认两张业务表；加 --with-companies 再补上 companies。
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    if args.with_companies and "companies" not in tables:
        tables.append("companies")

    # 目标库名：与源库同名（bidding_information_dai）。
    target_db = s.mysql_database

    print("=" * 70)
    print("MySQL → 本机 MySQL 拷贝")
    print(f"  源  ：{s.mysql_host}:{s.mysql_port}/{s.mysql_database}（只读）")
    print(f"  目标：{args.host}:{args.port}/{target_db}（账号 {target_user}）")
    print(f"  表  ：{', '.join(tables)}")
    print(f"  批大小：{args.batch} 行/批")
    print("=" * 70)

    # ——— 第一步：连源库。失败立刻给人话原因并退出 ———
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
    except Exception as exc:  # noqa: BLE001 - 接住任何连接失败并翻译成人话
        print(f"[错误] 连不上源 MySQL（{s.mysql_host}:{s.mysql_port}/{s.mysql_database}）：{exc}")
        print("       人话解释：请检查 .env 的 MYSQL_* 配置，以及能否访问该局域网地址。")
        return 1

    # ——— 第二步：连目标库（会自动建库）。失败则先关源连接再退出 ———
    try:
        dst = connect_target(
            args.host, args.port, target_user, target_password, s.mysql_charset, target_db
        )
    except Exception as exc:  # noqa: BLE001
        src.close()
        print(f"[错误] 连不上本机 MySQL（{args.host}:{args.port}，账号 {target_user}）：{exc}")
        print("       人话解释：账号或密码不对，或本机 MySQL 服务没启动。")
        print("                 如果提示 Access denied，说明密码错了；")
        print("                 如果提示 Can't connect，说明服务没起或端口不对。")
        return 1

    # ——— 第三步：逐表拷贝，每张表单独 try，做到「失败隔离」———
    report: dict[str, dict[str, Any]] = {}
    started = time.time()
    for table in tables:
        t0 = time.time()
        print(f"\n[表 {table}] 开始拷贝 ...")
        try:
            rows = copy_table(src, dst, table, args.batch)
            elapsed = round(time.time() - t0, 2)
            report[table] = {"ok": True, "rows": rows, "elapsed": elapsed, "error": None}
            print(f"[表 {table}] 完成：{rows} 行，耗时 {elapsed} 秒")
        except Exception as exc:  # noqa: BLE001 - 单表失败不能拖垮其它表
            elapsed = round(time.time() - t0, 2)
            # 回滚这张表尚未提交的改动，避免留下「一半数据」的残局。
            try:
                dst.rollback()
            except Exception:  # noqa: BLE001
                pass
            report[table] = {"ok": False, "rows": 0, "elapsed": elapsed, "error": str(exc)}
            print(f"[表 {table}] 失败：{exc}")
            print("           人话解释：可能是源表不存在/没权限，或建表语句在本机 MySQL 版本上不兼容。")
            print("                     其它表会照常继续，不受影响。")

    # ——— 第四步：收尾（关连接 + 打印汇总）———
    total_elapsed = round(time.time() - started, 2)
    try:
        src.close()
        dst.close()
    except Exception:  # noqa: BLE001
        pass

    print("\n" + "=" * 70)
    print("拷贝结果汇总")
    print("=" * 70)
    for table, info in report.items():
        if info["ok"]:
            print(f"  [成功] {table:<22} {info['rows']:>8} 行   {info['elapsed']:>7} 秒")
        else:
            print(f"  [失败] {table:<22} {'-':>8}      {info['elapsed']:>7} 秒   原因：{info['error']}")
    print("-" * 70)
    print(f"  总耗时：{total_elapsed} 秒")
    failed = [t for t, i in report.items() if not i["ok"]]
    if failed:
        print(f"  结论：有 {len(failed)} 张表失败（{', '.join(failed)}），详见上方原因。")
        return 1
    print("  结论：全部成功。")
    return 0


# 【启动守卫】只有「直接运行本文件」时才执行 main()；被别的脚本 import 时不会自动跑。
if __name__ == "__main__":
    sys.exit(main())