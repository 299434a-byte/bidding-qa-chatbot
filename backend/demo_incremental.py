# ============================================================================
# 文件：demo_incremental.py —— 「增量建库」演示脚本（打印证据用）
#
# 【这个脚本是干什么的】
#   一次跑完四个场景，把「全量建库」和「增量更新（新增/修改/删除/无变化）」
#   到底发生了什么，用打印出来的数字讲清楚：
#     ① 全量建一次，看点数是不是 9164；
#     ② 往 PG 插 1 行新数据 → 增量 → 只新增对应的块数，总数正确增加；
#     ③ 把这条记录的正文改短（块数从 N 变少）→ 增量 → 旧的多余块被删除，总数下降；
#     ④ 再跑一次增量（没有任何变化）→ added=0 updated=0 deleted=0 unchanged=全部。
#     ⑤ 清理：删掉演示用的那行 → 增量 → 演示点被回收，总数回到起点。
#
# 【为什么用「沙箱」（本脚本最重要的安全设计）】
#   真实的 data/qdrant 是本地文件库、单进程独占，而且里面有 9164 条生产数据。
#   本脚本把所有 Qdrant / BM25 产物都写到一个「临时沙箱目录」里（见 _sandbox_settings），
#   绝不碰 data/qdrant、也不动 data/bm25_index.json —— 演示再激进也伤不到生产库。
#   沙箱里同样从 PG 全量建一次，得到的 9164 与生产库口径一致，证据完全等价。
#
# 【怎么运行】
#   cd backend
#   ..\.venv\Scripts\python.exe demo_incremental.py
#
# 【注意事项】
#   · 运行前请确保后端服务已停止（本地 Qdrant 是单进程独占，服务开着会抢锁）；
#   · 本脚本会往 PG 的 procurement_notices 插入一行演示数据，并在结束时删除（净增 0）；
#   · 演示行的 project_name 带固定前缀 DEMO_INCREMENTAL_，便于识别与清理。
# ============================================================================

# 打开「类型注解延迟求值」开关（与项目其它文件一致）。
from __future__ import annotations

# dataclasses.replace：给冻结(frozen)的 Settings 复制出一份「改了路径」的副本。
import dataclasses
# shutil：演示结束后清掉临时沙箱目录。
import shutil
# tempfile：创建临时沙箱目录。
import tempfile
# time：给演示行生成一个唯一后缀，避免与历史残留撞主键。
import time
# Path：沙箱目录路径。
from pathlib import Path

# psycopg2：直接对 PG 做「插入/更新/删除」演示行（写操作，只读连接做不到）。
import psycopg2

# 把 backend 目录加进模块搜索路径，保证直接 `python demo_incremental.py` 也能 import app.*。
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings  # noqa: E402
from app.ingest import run_ingest  # noqa: E402
from app.vector_store import _close_quietly, get_store  # noqa: E402

# 演示行的固定前缀：便于识别，也方便万一残留时手工清理。
_DEMO_PREFIX = "DEMO_INCREMENTAL_"


def _sandbox_settings():
    """复制一份 Settings，把 Qdrant / BM25 路径改到临时沙箱目录。

    【为什么必须复制】Settings 是 frozen=True 的（创建后不可改），
    所以用 dataclasses.replace 造一个「只改了路径」的新副本，其余配置原样保留。
    """
    s = get_settings()
    sandbox = Path(tempfile.mkdtemp(prefix="demo_qdrant_"))
    return dataclasses.replace(
        s,
        qdrant_path=sandbox / "qdrant",
        bm25_index_path=sandbox / "bm25_index.json",
    ), sandbox


def _pg_conn(s):
    """打开一个「可写」的 PG 连接（演示行的增删改要用）。"""
    return psycopg2.connect(
        host=s.pg_host,
        port=s.pg_port,
        user=s.pg_user,
        password=s.pg_password,
        dbname=s.pg_database,
    )


def _pg_exec(s, sql: str, params: tuple = ()) -> int:
    """执行一条写 SQL 并提交，返回受影响行数。"""
    conn = _pg_conn(s)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            affected = cur.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


def _pk(db: str) -> str:
    """生成一个本次演示专用的 project_name（主键）。"""
    return f"{_DEMO_PREFIX}{db}"


def _points(s) -> int:
    """读沙箱向量库当前总点数。"""
    return int(get_store(s).stats().get("points", 0))


def _mk_content(repeat: int) -> str:
    """造一段「够长、能切成多块」的正文。

    【为什么用 repeat 控制长度】场景③要把正文「改短」，靠 repeat 从大改小即可。
    【为什么要够长】documents.py 的切块阈值是 MAX_DOC_CHARS=1200：
      正文（连同其它字段）超过 1200 字才会被切成多块；短于它只算 1 块。
      所以这里每段造得足够长，repeat 大 → 多块；repeat=1 → 只 1 块（用来演示「块数变少」）。
    """
    unit = (
        "本演示项目位于测试市示范区，采用公开招标方式采购智慧园区综合平台，"
        "工期三百六十五日历天，要求投标人具备相应施工资质与类似业绩。"
    )
    para = unit * 3  # 每段约 190 字
    return "\n".join(f"第{i + 1}段：{para}" for i in range(repeat))


def _insert_demo_row(s, project_name: str, content: str) -> None:
    """插入一行演示数据（8 列里的关键列填上，其余留空）。"""
    _pg_exec(
        s,
        "INSERT INTO public.procurement_notices "
        "(project_name, address, purchaser, procurement_date, title, content, "
        " attachment_file_name, attachment_file_link) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            project_name,
            "测试市示范区演示路 1 号",
            "演示采购单位",
            "2026-10-09",
            "智慧园区综合平台建设项目（增量演示）",
            content,
            None,
            None,
        ),
    )


def _update_demo_content(s, project_name: str, content: str) -> None:
    """把演示行的正文改成新的（用于「改短」场景）。"""
    _pg_exec(
        s,
        "UPDATE public.procurement_notices SET content = %s WHERE project_name = %s",
        (content, project_name),
    )


def _delete_demo_row(s, project_name: str) -> None:
    """删除演示行。"""
    _pg_exec(
        s,
        "DELETE FROM public.procurement_notices WHERE project_name = %s",
        (project_name,),
    )


def _show(tag: str, summary: dict, db: str = "") -> None:
    """把一次建库/增量报告里最关键的几个数字打印出来。"""
    print(
        f"[{tag}] mode={summary.get('mode', 'full')} "
        f"documents={summary.get('documents')} points={summary.get('points')} "
        f"added={summary.get('added', '-')} updated={summary.get('updated', '-')} "
        f"deleted={summary.get('deleted', '-')} unchanged={summary.get('unchanged', '-')} "
        f"库内总点数={db}"
    )


def main() -> None:
    # 基础配置：用「原配置」连 PG，用「沙箱副本」做向量库。
    base = get_settings()
    sandbox_settings, sandbox_dir = _sandbox_settings()
    demo_pk = _pk(base.pg_database or "kb")

    print("=" * 78)
    print("增量建库演示（沙箱隔离，不触碰 data/qdrant 与 data/bm25_index.json）")
    print(f"沙箱目录: {sandbox_dir}")
    print(f"数据源  : {base.kb_source}  ({base.pg_host}:{base.pg_port}/{base.pg_database})")
    print(f"演示主键: {demo_pk}")
    print("=" * 78)

    try:
        # 先清理可能残留的同名演示行，保证从干净状态开始。
        _delete_demo_row(base, demo_pk)

        # ---------------- 场景 ①：全量建一次 ---------------- #
        print("\n--- 场景 ① 全量建库（基线） ---")
        full = run_ingest(settings=sandbox_settings, mode="full")
        base_count = _points(sandbox_settings)
        _show("全量", full, base_count)
        assert base_count == 9164, f"全量点数应为 9164，实际 {base_count}"
        print(f"✔ 全量建库完成：总点数 = {base_count}")

        # ---------------- 场景 ②：插入 1 行新数据 → 增量 → 只新增 ---------------- #
        print("\n--- 场景 ② 新增 1 行原始数据 → 增量更新 ---")
        # 造一段较长的正文，让它切出多块（repeat=8 约 1500 字 → 会切成数块）。
        long_content = _mk_content(8)
        _insert_demo_row(base, demo_pk, long_content)
        inc2 = run_incremental(sandbox_settings)
        cnt2 = _points(sandbox_settings)
        new_chunks = cnt2 - base_count
        _show("增量②", inc2, cnt2)
        print(f"   预期：added = 该行切出的块数；updated=0 deleted=0 unchanged={base_count}")
        assert inc2["added"] == new_chunks and new_chunks > 0, "增量新增块数应与点数增量一致"
        assert inc2["updated"] == 0 and inc2["deleted"] == 0
        assert inc2["unchanged"] == base_count
        print(f"✔ 只新增 {new_chunks} 块，总点数 {base_count} → {cnt2}")

        # ---------------- 场景 ③：把正文改短 → 增量 → 多余旧块被删除 ---------------- #
        print("\n--- 场景 ③ 把演示行正文改短 → 增量更新 ---")
        short_content = _mk_content(1)  # 明显变短
        _update_demo_content(base, demo_pk, short_content)
        inc3 = run_incremental(sandbox_settings)
        cnt3 = _points(sandbox_settings)
        _show("增量③", inc3, cnt3)
        print("   预期：updated = 改短后剩余块数；deleted = 多出来的旧块；(总数下降)")
        assert inc3["added"] == 0, "已存在的行不该算成新增"
        assert inc3["deleted"] > 0, "改短后应有旧块被删除"
        assert cnt3 < cnt2, "总数应下降"
        assert inc3["unchanged"] == base_count, "其它文档应全部未变"
        print(f"✔ 删除 {inc3['deleted']} 个多余旧块，总点数 {cnt2} → {cnt3}（下降 {cnt2 - cnt3}）")

        # ---------------- 场景 ④：无变化 → 增量 → 全部 unchanged ---------------- #
        print("\n--- 场景 ④ 无任何变化 → 再跑一次增量 ---")
        inc4 = run_incremental(sandbox_settings)
        cnt4 = _points(sandbox_settings)
        _show("增量④", inc4, cnt4)
        assert inc4["added"] == 0 and inc4["updated"] == 0 and inc4["deleted"] == 0
        assert inc4["unchanged"] == cnt4, "无变化时 unchanged 应等于当前总点数"
        print(f"✔ added=0 updated=0 deleted=0 unchanged={inc4['unchanged']}（点数不变 {cnt4}）")

        # ---------------- 场景 ⑤：清理演示行 → 增量 → 回收演示点 ---------------- #
        print("\n--- 场景 ⑤ 删除演示行（清理）→ 增量更新 ---")
        _delete_demo_row(base, demo_pk)
        inc5 = run_incremental(sandbox_settings)
        cnt5 = _points(sandbox_settings)
        _show("增量⑤", inc5, cnt5)
        assert inc5["deleted"] == cnt3 - base_count, "清理后应删掉演示行的全部块"
        assert cnt5 == base_count, f"清理后应回到基线 {base_count}"
        print(f"✔ 回收 {inc5['deleted']} 个演示点，总点数回到基线 {cnt5}")

        print("\n" + "=" * 78)
        print("全部场景通过：新增 / 修改(改短) / 删除 / 无变化 四种增量情形均已验证。")
        print("=" * 78)

    finally:
        # 无论成功失败，都尽力清理：删演示行 + 删临时沙箱目录，绝不留脏数据。
        try:
            _delete_demo_row(base, demo_pk)
        except Exception as exc:  # noqa: BLE001 - 清理失败也不该盖住主流程结果
            print(f"[警告] 清理演示行失败：{exc}")
        # 注意：沙箱目录里的 Qdrant 仍占着文件句柄，先关闭客户端再删，否则 Windows 删不掉。
        try:
            _close_quietly(get_store(sandbox_settings).client)
        except Exception:  # noqa: BLE001 - 关闭失败可忽略
            pass
        shutil.rmtree(sandbox_dir, ignore_errors=True)
        print(f"[清理] 已删除临时沙箱目录 {sandbox_dir}")


def run_incremental(settings):
    """薄封装：调用 run_ingest 的增量模式（演示脚本里反复用，起个短名字更好读）。"""
    return run_ingest(settings=settings, mode="incremental")


if __name__ == "__main__":
    main()