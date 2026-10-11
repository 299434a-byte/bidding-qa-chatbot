# ============================================================================
# 文件：ingest.py —— 建库流水线的「总装线」
#
# 【这个文件在整个项目里是干什么的】
#   用户在系统里能搜到公告、项目、公司，前提是先把 MySQL 里的数据「搬」进检索系统。
#   这条搬运生产线就是本文件：它按顺序做四件事：
#     ① 连 MySQL，逐张表读数据，把每行渲染成自然语言文字（文档）；
#     ② 用所有文档「拟合」出 BM25 关键词索引，并保存到磁盘；
#     ③ 准备好向量数据库 Qdrant 里的「集合」（相当于一张表）；
#     ④ 把每个文档算成向量，连同稀疏（关键词）向量一起写进 Qdrant。
#   一整套跑完，用户提问时检索才有东西可查。
#
# 【谁调用它（上游）】
#   · 命令行：`python -m app.ingest`（本文件末尾 if __name__ == "__main__" 触发 main()）；
#   · 后端接口：main.py 里的「重建知识库」按钮，会在线程里调用 run_ingest()。
#
# 【它给谁用（下游）】
#   产出落到两个地方：磁盘上的 BM25 索引文件 + Qdrant 向量库；
#   之后 retriever.py 提问时就从这两处读数据做混合检索。
#
# 【先懂的四个概念（大白话）】
#   1) 流水线（pipeline）：把一个大任务拆成若干「工序」，一件一件顺着做。
#      本文件就是这样的四道工序，每完成一道就给调用方「报个进度」（progress 回调）。
#   2) 回调（callback）：把「一个函数」当作参数传给别人，别人在合适的时候替你调用它。
#      这里的 progress 就是回调——流水线每到一站就调用它汇报进度，
#      但「进度往哪儿显示」由调用方决定（命令行打印到屏幕，接口推给前端）。
#   3) 幂等（idempotent）：同样的操作做一次和做十次，结果一样。
#      本项目靠「文档 ID 稳定映射成 UUID」实现：重跑建库是「覆盖」而非「叠加」，
#      不会出现同一份数据在库里存两遍。
#   4) 空库可跑：三张业务表里一行数据都没有时，这条流水线不报错，
#      照样把「空集合 + 空索引」建好，服务能正常启动，只是查不到东西。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring）】
# 【为什么放在这里】注释不算语句，所以放在这一大段注释之后，它依然是本模块的
#   第一条语句、依然是官方自我介绍，内容与原文一字不差。
"""建库管道：MySQL → 文档构建 → BM25 拟合 → 稠密向量 → 写入 Qdrant。

设计要点：
- **空数据可跑**：三张表为空时不报错，照样建好集合与空索引，服务可正常启动；
- **重跑即覆盖**：文档 ID 稳定映射为 UUIDv5，重复建库是幂等覆盖，不会产生重复数据；
- **结构自适配**：字段完全来自运行时发现的表结构，表结构调整后重跑即可。

命令行用法（需先停掉后端服务，本地模式目录独占）::

    cd backend
    ..\\.venv\\Scripts\\python.exe -m app.ingest
"""

# 【这行在干什么】打开「类型注解延迟求值」开关。
# 【为什么这么写】本文件用了 list[dict[str, Any]]、Settings | None 这类新式标注，
#   老版本 Python（3.8/3.9）不认；加上这行后标注先被当成字符串存着、需要时才解析。
# 【注意事项】必须放在文件最顶部（所有执行代码之前），这是 Python 的硬性要求。
from __future__ import annotations

# 【这行在干什么】引入 time 模块。
# 【为什么需要】流水线开头记一个开始时间、结尾再取一次当前时间，
#   两者相减就得到「整条流水线花了多少秒」——这个耗时对判断建库是否正常很关键。
import time

# 【这行在干什么】引入类型标注工具 Any 和 Callable。
# 【参数逐个解释】
#   · Any —— 报告字典里的值类型不固定（数字、字符串、列表、字典都有），用 Any 兜住；
#   · Callable —— 表示「可调用的东西」（函数、方法等）。下面 ProgressFn 要用它描述
#     「一个函数类型」：把「进度回调函数」这个概念标注清楚。
from typing import Any, Callable

# 【这行在干什么】引入本项目的 BM25 索引类和分词函数。
# 【参数逐个解释】
#   · BM25Index —— BM25 索引对象，负责统计词频、建词表、编码向量；
#   · tokenize —— 把一段文本切成一个个词（中文分词），BM25 的一切统计都基于它。
from .bm25 import BM25Index, tokenize

# 【这行在干什么】引入本项目的配置类和取配置函数。
# 【为什么这么写】MySQL 表清单、BM25 参数、Qdrant 集合名、向量模型名……全部来自 .env，
#   代码里不写死；开头点号「.」表示「同一个包里的模块」。
from .config import Settings, get_settings

# 【这行在干什么】引入数据库相关的四个工具（全部来自 db.py）。
# 【参数逐个解释】
#   · DatabaseUnavailable —— 数据库不可用的自定义异常，main() 里专门捕获它给友好提示；
#   · discover_schema —— 自动探查表结构（列定义、主键、行数）；
#   · iter_rows —— 流式逐行读取表数据（大表也不爆内存）；
#   · mysql_connection —— 上下文管理器，自动「连上→用完自动关闭」。
from .db import DatabaseUnavailable, discover_schema, iter_rows, mysql_connection

# 【这行在干什么】引入文档构建相关的两样东西（来自 documents.py）。
# 【参数逐个解释】
#   · Document —— 一条可检索的最小单元（本流水线搬运的「货物」）；
#   · build_documents —— 把「一行数据库数据」渲染成「一段或多段 Document」。
from .documents import Document, build_documents

# 【这行在干什么】引入向量模型包装器的单例获取函数。
# 【为什么需要】第 4 道工序要把文档文字算成向量（embed_documents），靠它拿到模型对象。
from .embeddings import get_embedder

# 【这行在干什么】引入向量库（Qdrant）的单例获取函数，以及「内容指纹」算法。
# 【为什么需要】第 3、4 道工序要准备集合、写入向量，靠 get_store 拿到向量库对象；
#   增量更新要靠 text_hash 给每块正文算 sha256 指纹，用于判断「改没改」。
from .vector_store import get_store, text_hash

# 【日志】用标准库 logging 取代原来的 print：带时间/级别/模块前缀，
# 且能同时输出到控制台与 backend/data/app.log 文件（配置见 logging_setup.py）。
from .logging_setup import get_logger
# 【缓存与知识库版本解耦】kb_version：建库成功后写 data/kb_version.txt，
# 让问答缓存的版本指纹跟着知识库走（详见 app/kb_version.py）。
from .kb_version import write_kb_version

# 本模块的日志器（名字形如 "app.ingest"）。
logger = get_logger(__name__)

# 【这行在干什么】定义一个常量：每次送给向量模型「批量处理」的文档数（64 个）。
# 【为什么要分批（关键性能设计）】如果一次性把几万个文档全交给向量模型，
#   ①显存/内存会瞬间暴涨甚至崩掉；②模型推理效率反而下降。
#   分批（每 64 个算一批）能稳定控制内存占用，让长流水线平稳跑完。
# 【调大调小会怎样】调大 → 每批更快但吃内存；调小 → 更省内存但批次更多、调度开销变多。
# 每次送给向量模型的文档数
_EMBED_BATCH = 64

# 【这行在干什么】定义「进度回调函数」的类型别名 ProgressFn。
# 【为什么叫「类型别名」】给一长串类型标注起个好记的名字，方便下面反复使用和阅读。
# 【写法拆解】Callable[..., None] 读作「一个可调用对象，参数任意（...），没有返回值（None）」。
# 【为什么参数写成 ... 而不是写具体】因为进度回调可能带各种额外信息（表名、计数等），
#   用 ... 表示「参数很灵活」，兼容性最好。
ProgressFn = Callable[..., None]


# 【这行在干什么】定义函数 _columns_for_docs：把庞杂的列定义「精简」成只留文档构建需要的字段。
# 【名字前的下划线含义】约定俗称：下划线开头表示「本模块内部专用」，不对外承诺使用。
# 【参数解释】columns —— db.py 的 discover_schema() 给出的完整列定义列表，
#   每项有很多键（name/type/column_type/nullable/key/comment/position）。
# 【返回值】精简后的列定义列表，每项只保留 name、type、comment 三个键。
# 【为什么要精简（重要设计）】documents.py 构建文本时只关心「列名叫什么、值怎么展示、
#   是不是二进制要跳过」，用不到 nullable、position 那些。这里先把数据「削」到刚好够用，
#   等于给下游一份「定制菜单」，接口面更小、更清晰，将来 discover_schema 的字段增减
#   也不会影响 documents.py。
def _columns_for_docs(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 information_schema 的列定义精简成文档构建所需的最小结构。"""
    # 【这行在干什么】用列表推导式，逐列构造精简后的字典。
    # 【写法拆解】遍历 columns 里的每个 col，为每个 col 生成一个新字典，收成列表：
    #   · "name" —— 列名，必填，直接用 col["name"]；
    #   · "type" —— 列类型，用 col.get("type", "")：万一这个键不存在，就用空字符串兜底，
    #     避免 KeyError 中断整个建库；
    #   · "comment" —— 列的中文注释，同样用 .get(..., "") 兜底，没注释就当空字符串。
    # 【为什么不需要提供默认值给 name】列名是列定义的核心，必然存在，所以直接取即可。
    return [
        {
            "name": col["name"],
            "type": col.get("type", ""),
            "comment": col.get("comment", ""),
        }
        for col in columns
    ]


# 【这行在干什么】定义纯函数 generate_documents：读数据源 → 生成全部文档 + 分词结果。
# 【为什么把它单独抽出来（本次重构的核心动机）】
#   「读取数据源 + 渲染文档 + 分词」这段逻辑，全量建库要用、增量更新也要用。
#   抽成一个「只读、不写库」的纯函数后，两条链路都能复用它：
#     · 全量模式：拿到全量文档，整体覆盖写入；
#     · 增量模式：拿到全量文档，与库里现有文档做差集，只写变化的部分。
# 【为什么叫「纯」】它只读数据源、只生成内存里的对象，绝不碰向量库、不删数据，
#   所以可以放心地在「还没决定怎么写入」之前先调用它，也便于单独测试。
# 【数据源选择】按 .env 的 KB_SOURCE 自动切换：
#   · 默认（mysql）：走 db.py，行为与改动前一字不差；
#   · postgres：走 pg_source.py 的「同接口孪生函数」，下游逻辑不变。
# 【参数】settings=None 不传就用 .env；progress=None 不传就不汇报进度。
# 【返回值】三元组 (documents, token_lists, stage_summary)：
#   · documents —— 全部 Document（本函数第 i 个元素与 token_lists 第 i 个一一对应）；
#   · token_lists —— 每个文档分词后的词列表；
#   · stage_summary —— 本阶段报告片段（每张表的行数/文档数、errors、warnings、总数）。
def generate_documents(
    settings: Settings | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list[Document], list[list[str]], dict[str, Any]]:
    """读取数据源并生成全量文档（只读，不写向量库）。"""

    # 【这行在干什么】和 run_ingest 里一样的「进度汇报闭包」。
    # 【为什么每个函数各自定义一份】因为它要用到本函数收到的 progress 参数；
    #   放在内部就能直接访问，不必把 progress 一路当参数往下传。
    def report(stage: str, message: str, **extra: Any) -> None:
        if progress:
            progress(stage=stage, message=message, **extra)

    # 【这行在干什么】拿到真正要用的配置对象（传了就用传的，没传就从 .env 读）。
    s = settings or get_settings()

    # 【这行在干什么】准备本阶段的报告片段，结构对齐 run_ingest 的总报告。
    stage_summary: dict[str, Any] = {
        "tables": {},
        "errors": [],
        "warnings": [],
        "documents": 0,
    }
    # 【这行在干什么】收集全部文档（全流程产物）。
    documents: list[Document] = []
    # 【这行在干什么】收集与之一一对应的分词结果（下标 i 与 documents[i] 配对）。
    token_lists: list[list[str]] = []

    # 【这行在干什么】按 KB_SOURCE 选择数据源：连接器 / 结构探查 / 逐行读取三件套。
    # 【为什么要用「包装成同名局部函数」的写法】db.py 与 pg_source.py 的三个函数
    #   名字相同、返回结构相同，但**入参数量略有差别**（PG 版多一个 settings 参数）。
    #   这里把差异就地抹平，下面的主循环就能对两种数据源写一模一样的代码。
    if s.kb_source_is_postgres:
        from .pg_source import (
            discover_schema as _discover_schema,
            iter_rows as _iter_rows,
            pg_connection as _open_connection,
        )

        # PG 版：discover_schema / iter_rows 都要带上 settings。
        def _discover(conn: Any) -> dict[str, dict]:
            return _discover_schema(conn, s.kb_tables, s)

        def _iter(conn: Any, table: str) -> Any:
            return _iter_rows(conn, table, settings=s)

        source_label = "postgres"
        report("postgres", f"连接 PostgreSQL {s.pg_host}:{s.pg_port}/{s.pg_database} …")
    else:
        from .db import (
            discover_schema as _discover_schema,
            iter_rows as _iter_rows,
            mysql_connection as _open_connection,
        )

        # MySQL 版（默认，行为不变）：沿用原来的两参数调用。
        def _discover(conn: Any) -> dict[str, dict]:
            return _discover_schema(conn, s.kb_tables)

        def _iter(conn: Any, table: str) -> Any:
            return _iter_rows(conn, table)

        source_label = "mysql"
        report("mysql", f"连接 MySQL {s.mysql_host}:{s.mysql_port}/{s.mysql_database} …")

    # 【这行在干什么】用选好的连接器连上数据库；with 结束时自动关闭，不泄漏连接。
    with _open_connection(s) as conn:
        # 【这行在干什么】一次性探查所有表的结构（列定义、主键、行数）。
        schema = _discover(conn)

        # 【这行在干什么】逐张表处理数据。
        for table in s.kb_tables:
            # 【这行在干什么】取出这张表的探查结果；取不到就用空字典兜底。
            info = schema.get(table, {})
            # 【这行在干什么】取得这张表的中文展示名（如「采购公告」）。
            label = s.label_of(table)
            # 【这行在干什么】表不存在 / 无权限时，记一条失败信息并跳过这张表。
            # 【为什么跳过而不是让整条流水线停工】某张表坏了不该拖垮其他表，这是容错设计。
            if not info.get("exists"):
                error = info.get("error") or "表不存在"
                stage_summary["tables"][table] = {
                    "label": label,
                    "exists": False,
                    "rows": 0,
                    "documents": 0,
                    "error": error,
                }
                stage_summary["errors"].append(f"{table}（{label}）：{error}")
                report(source_label, f"跳过 {table}：{error}", table=table)
                continue

            # 【这行在干什么】汇报开始读取这张表。
            report(source_label, f"读取 {table}（{label}）…", table=table)
            # 【这行在干什么】统计这张表生成了多少个文档块。
            doc_count = 0
            # 【这行在干什么】把完整列定义精简成文档构建需要的最小结构。
            columns = _columns_for_docs(info["columns"])
            # 【这行在干什么】把这张表每一行「翻译」成文档（build_documents 是生成器，逐条吐出）。
            for doc in build_documents(
                _iter(conn, table),
                columns,
                table=table,
                label=label,
                settings=s,
                primary_key_column=info.get("primary_key"),
            ):
                # 收进总清单。
                documents.append(doc)
                # 顺手分词并收进平行清单（供 BM25 使用，避免后面重复分词）。
                token_lists.append(tokenize(doc.text))
                doc_count += 1

            # 【这行在干什么】把这张表的成功结果记进报告。
            stage_summary["tables"][table] = {
                "label": label,
                "exists": True,
                "rows": info.get("row_count", 0),
                "documents": doc_count,
                "error": None,
            }
            # 【这行在干什么】汇报完毕，消息里带上「行数 → 文档块数」的对比，便于一眼判断是否正常。
            report(
                source_label,
                f"{table}（{label}）：{info.get('row_count', 0)} 行 → {doc_count} 个文档块",
                table=table,
            )

    # 【这行在干什么】统计全部表加起来的文档总数。
    stage_summary["documents"] = len(documents)
    # 【这行在干什么】把三样产物一起返回，交给调用方（全量/增量）继续处理。
    return documents, token_lists, stage_summary


# 【这行在干什么】定义核心函数 run_ingest：执行一次完整建库，并返回一份「结构化报告」。
# 【参数逐个解释】
#   · settings=None —— 配置对象；不传就从 .env 读（测试时可塞假配置）；
#   · recreate=True —— 是否「重建集合」：True 表示清空旧向量库数据再写（干净重来），
#     False 表示沿用旧集合并覆盖写入（保留其他数据）。默认 True 是「重建知识库」的常规行为；
#   · progress=None —— 进度回调函数；不传表示「不需要汇报进度」（命令行示例里会传打印函数）。
# 【返回值】一个字典（summary），记录了本次建库的详细结果：
#   每张表读了多少行/生成多少文档、总文档数、写进向量库多少条、BM25 词表大小、
#   有没有错误/警告、是否空库、总耗时。前端接口可以把它展示给用户。
def run_ingest(
    settings: Settings | None = None,
    recreate: bool = True,
    progress: ProgressFn | None = None,
    mode: str = "full",
) -> dict[str, Any]:
    """执行一次完整建库（默认）或增量更新，返回结构化报告。

    【mode 参数（本次新增）】控制走哪条链路：
      · "full"（默认）—— 全量重建，行为与改动前**完全一致**（保证老调用方不受影响）；
      · "incremental" —— 增量更新：只写「新增/内容变化」的文档块，删掉「数据源已消失」的旧块。
    【为什么默认值是 full（关键）】接口与命令行老调用方都没传 mode，默认走全量 → 行为不变。
    【为什么不传就报错】保持向后兼容：老代码不改也能照常工作。
    """
    # 【这行在干什么】增量模式直接转交给 run_incremental，全量模式继续往下走原来的流程。
    # 【为什么放在函数最前面】这样「全量」那段代码一个字都不用动，最大程度保证老行为不变。
    if mode == "incremental":
        return run_incremental(settings=settings, progress=progress)

    # 【这行在干什么】在函数内部再定义一个「小函数」report，专门负责「汇报进度」。
    # 【为什么要在函数里定义（关键设计）】它要用到外层传进来的 progress 参数；
    #   定义在内部就能直接访问它（这叫「闭包」，即内层函数能记住并使用外层的变量），
    #   不用把 progress 一路作为参数往每个调用点传，调用处写起来更干净：
    #   直接 report("mysql", "读取中…") 就行。
    # 【参数解释】
    #   · stage —— 当前处于哪个阶段（mysql/bm25/qdrant/embed/upsert/done）；
    #   · message —— 给用户看的进度文字；
    #   · **extra —— 「关键字参数收集」：允许调用方再塞任意额外信息（如 table=表名），
    #     这些会被打包成一个字典 extra 传给回调。
    def report(stage: str, message: str, **extra: Any) -> None:
        # 【这行在干什么】只有在调用方传了进度回调时，才真正去调用它。
        # 【为什么先判断 progress（重要，别删）】progress 默认是 None；如果不判断就去
        #   progress(...)，None 不是函数、会立刻报 TypeError。这个「有则调用、无则跳过」
        #   的写法让进度汇报变成「可选功能」，不影响主流程。
        if progress:
            # 【这行在干什么】调用回调函数，把阶段、消息、以及额外信息一起传过去。
            # 【写法解释】progress(stage=stage, message=message, **extra) 是「关键字传参」：
            #   接收方通过参数名就能取到值，不依赖位置顺序；
            #   **extra 把那个打包好的额外信息字典「摊开」传进去。
            progress(stage=stage, message=message, **extra)

    # 【这行在干什么】拿到真正要用的配置对象（传了就用传的，没传就从 .env 读）。
    # 【为什么这么写】settings or get_settings() 一行实现「不传就用默认」；
    #   get_settings() 自带缓存，反复调用不重复读盘。
    s = settings or get_settings()
    # 【这行在干什么】记录流水线的开始时刻（一个浮点数时间戳）。
    # 【为什么记它】结尾取一次当前时间相减，就得到「整条流水线耗时多少秒」。
    started = time.time()
    # 【这行在干什么】创建「结果报告」字典，并把各项初值填好。
    # 【为什么初值这么设计（重要）】先在开头把最终报告的结构「定型」，后面每道工序
    #   只往相应字段里填数字。好处：①任何中途出错也不影响「至少有一份完整结构的报告」；
    #   ②读代码的人一眼就知道最后会返回哪些信息。
    # 【字段逐个解释】
    #   · tables —— 按表名记录每张表的处理情况（行数、文档数、有无错误）；
    #   · documents —— 所有表加起来生成了多少个文档块；
    #   · points —— 实际写进向量库多少条向量；
    #   · bm25_terms / bm25_docs —— BM25 词表里有多少个词 / 有多少篇文档；
    #   · errors —— 严重问题清单（如某张表不存在），会导致该表被跳过；
    #   · warnings —— 提醒清单（如空库），不影响流水线继续；
    #   · empty —— 本次是否「空库」（一个文档都没有）；
    #   · elapsed —— 总耗时（秒），开头填 0.0，结尾再算出来覆盖。
    summary: dict[str, Any] = {
        "tables": {},
        "documents": 0,
        "points": 0,
        "bm25_terms": 0,
        "bm25_docs": 0,
        "errors": [],
        "warnings": [],
        "empty": True,
        "elapsed": 0.0,
    }

    # 【这行在干什么】建一个空列表 documents，准备收集全流程产出的所有文档。
    # 【为什么全部先攒起来（关键设计）】BM25 要「看完全部文档」才能算出词表和文档频率，
    #   向量模型也需要拿到全部文字；所以必须先把文档全收齐，再统一处理。
    #   （这与 db.py 的流式读取不矛盾：读的时候分批省内存，但最终产物要完整集齐。）
    documents: list[Document] = []
    # 【这行在干什么】建一个空列表 token_lists，准备收集「每个文档分词后的结果」。
    # 【为什么和 documents 平行保存】documents 存原始对象、token_lists 存分词结果，
    #   两者下标一一对应（第 i 个文档对应第 i 组词）。BM25 拟合和编码都要用分词结果，
    #   提前算好存下来，避免后面重复分词浪费时间。
    token_lists: list[list[str]] = []

    # 【这行在干什么】纯注释，用分隔线标出第 1 道工序：「读取数据源并生成文档」。
    # ---------------- 1. 读取数据源并生成文档 ---------------- #
    # 【这行在干什么】调用前面抽出的纯函数 generate_documents：读数据源 → 生成全部文档。
    # 【为什么这里只是一行调用（重要重构）】「取数 + 渲染 + 分词」这段逻辑以前长在本函数里，
    #   但增量更新同样需要它，于是抽成纯函数由两边复用。对全量模式来说，行为一字未变：
    #   该连哪个库、该怎么设进度、每张表算什么，全在 generate_documents 里原样实现。
    # 【返回的三样东西】
    #   · documents    —— 全部文档（本函数下面要整体写入）；
    #   · token_lists  —— 与之一一对应的分词结果（BM25 要用）；
    #   · stage（内部叫 _stage）—— 本阶段的报告片段（每张表的行数/文档数、错误、警告、总数）。
    documents, token_lists, _stage = generate_documents(s, progress)
    # 【这行在干什么】把纯函数产出的阶段报告并进总报告，保持与改动前完全一样的报告结构。
    summary["tables"] = _stage["tables"]
    summary["errors"] = _stage["errors"]
    summary["warnings"] = _stage["warnings"]
    summary["documents"] = _stage["documents"]

    # 【这行在干什么】纯注释，标出第 2 道工序：「拟合 BM25 并落盘」。
    # ---------------- 2. 拟合 BM25 并落盘 ---------------- #
    # 【这行在干什么】汇报「开始拟合 BM25」。
    report("bm25", "拟合 BM25 词表与文档频率 …")
    # 【这行在干什么】创建 BM25 索引对象，然后用全部文档的分词结果去「拟合」。
    # 【参数逐个解释】
    #   · k1=s.bm25_k1、b=s.bm25_b —— BM25 的两个可调参数，来自 .env
    #     （k1 控制词频饱和度，b 控制文档长度惩罚强弱，检索与建库必须用同一套值）；
    #   · .fit(token_lists) —— 「拟合：把全部文档喂进去，统计出词表（有哪些词）、
    #     每个词的文档频率（出现在多少篇文档里）、每篇文档的长度等」。
    #   【为什么叫「拟合」】它不是训练模型，而是「统计一批数据、算出该统计量」，
    #   英语里惯用 fit 这个词。
    bm25 = BM25Index(k1=s.bm25_k1, b=s.bm25_b).fit(token_lists)
    # 【这行在干什么】把拟合好的索引保存到磁盘（供检索服务加载）。
    # 【参数解释】s.bm25_index_path —— 索引文件路径，来自 .env。
    # 【为什么必须落盘（关键）】检索服务是「另一个进程」，它没法直接用这里内存里的对象；
    #   把统计结果写进文件，检索时读回来即可。同时 retriever.py 会按文件修改时间判断
    #   「索引有没有更新」，所以每次重建库后这个文件的修改时间变化就能触发服务重载。
    bm25.save(s.bm25_index_path)
    # 【这行在干什么】把词表大小记进报告。
    # 【参数解释】bm25.term_to_id —— 「词 → 编号」的映射字典，它的长度就是词表里有多少个词。
    summary["bm25_terms"] = len(bm25.term_to_id)
    # 【这行在干什么】把「有多少篇文档」记进报告。
    # 【参数解释】bm25.n_docs —— 拟合时一共处理了多少篇文档（用于 BM25 公式里的文档总数）。
    summary["bm25_docs"] = bm25.n_docs
    # 【这行在干什么】汇报 BM25 已就绪，消息里给出文档数与词元数。
    report("bm25", f"BM25 就绪：{bm25.n_docs} 篇文档 / {len(bm25.term_to_id)} 个词元")
    # 【这行在干什么】把每篇文档的「稀疏向量」算出来，先存着，等会儿和稠密向量一起写入向量库。
    # 【参数逐个解释】
    #   · bm25.encode_document(tokens) —— 把一组词编码成「稀疏向量」：
    #     稀疏向量只记录「哪些词出现了、权重多少」，没出现的词一律不存，所以叫「稀疏」；
    #     这与后面稠密向量（512 个数字全有值）正好互补。
    #   · token_lists —— 上面存好的全部文档分词结果，逐个编码。
    # 【为什么现在就编码】因为 token_lists 在手，趁热打铁；而且 BM25 对象此刻正是最新的。
    sparse_vectors = [bm25.encode_document(tokens) for tokens in token_lists]

    # 【这行在干什么】纯注释，标出第 3 道工序：「向量库准备」。
    # ---------------- 3. 向量库准备 ---------------- #
    # 【这行在干什么】拿到向量库（Qdrant）对象。
    # 【参数解释】get_store(s) —— 单例获取函数：整个进程只创建一次，复用同一个连接/目录，
    #   避免重复打开本地数据库文件。
    store = get_store(s)
    # 【这行在干什么】汇报「准备集合」，消息里说明集合名和是否重建。
    report("qdrant", f"准备集合 {s.qdrant_collection}（recreate={recreate}）…")
    # 【这行在干什么】确保向量库里存在「集合」（可以理解为一张存向量的表），并按要求决定是否重建。
    # 【参数解释】recreate=recreate —— True 表示「先清空旧集合再新建」（重建知识库的干净做法）；
    #   False 表示「集合在就沿用、不在才建」（保留已有数据的增量做法）。
    # 【为什么必须先做这一步】数据写进向量库前，库和集合必须已就位；否则写入会失败。
    store.ensure_collection(recreate=recreate)

    # 【这行在干什么】纯注释，标出第 4 道工序：「稠密向量 + 写入」。
    # ---------------- 4. 稠密向量 + 写入 ---------------- #
    # 【这行在干什么】判断是否有文档需要处理。
    # 【为什么要判断（关键，对应「空库可跑」硬约束）】如果三张表都是空的，documents 是空列表，
    #   这时候不该去调向量模型（没东西可算），直接走 else 分支，把「空库初始化完成」记下，
    #   流水线照样成功结束。这就是「支持空数据运行」的实现位置。
    if documents:
        # 【这行在干什么】汇报「开始向量化」，消息里带上文档数与所用模型名。
        # 【为什么消息里带模型名】不同模型产出的向量维度和结果不同，写清楚便于排查
        #   「模型换过没有」「维度和集合配置是否匹配」等问题。
        report("embed", f"向量化 {len(documents)} 个文档块（模型 {s.embed_model}）…")
        # 【这行在干什么】拿到向量模型对象（单例）。
        embedder = get_embedder(s)
        # 【这行在干什么】建一个空列表，准备收集所有文档的稠密向量。
        # 【类型解释】list[list[float]]：外层列表每个元素对应一篇文档，内层是该文档的向量
        #   （512 个浮点数）。顺序与 documents 严格一一对应，写入时靠下标配对。
        dense_vectors: list[list[float]] = []
        # 【这行在干什么】分批循环：从 0 开始，每批跨 _EMBED_BATCH（64）个文档。
        # 【参数逐个解释】
        #   · range(0, len(documents), _EMBED_BATCH) —— 生成 0, 64, 128, … 这些「批次起点」；
        #   · start —— 当前批次的起始下标。
        # 【为什么用 range 步长而不是切片遍历】这样能直接拿到 start，方便算进度百分比。
        for start in range(0, len(documents), _EMBED_BATCH):
            # 【这行在干什么】用切片取出本批文档（从 start 到 start+64，末批可能不足 64）。
            # 【写法解释】documents[start : start + _EMBED_BATCH] 是「切片」：取下标区间内的元素，
            #   左闭右开（含 start、不含 start+64），超出范围也不会报错、自动截断。
            batch = documents[start : start + _EMBED_BATCH]
            # 【这行在干什么】把本批文档的正文交给向量模型算向量，并把结果追加到总表里。
            # 【参数逐个解释】
            #   · [d.text for d in batch] —— 列表推导式，从本批文档里取出各自的正文文字；
            #   · embedder.embed_documents(...) —— 批量把一批文字算成向量（比一条条算快得多）；
            #   · dense_vectors.extend(...) —— 把本批算出的若干个向量「一次性接到」总表末尾。
            #     【为什么用 extend 而不是 append】extend 是把一个列表的元素逐个并进来，
            #     append 会把整个列表当成一个元素塞进去——这里要的是前者。
            dense_vectors.extend(embedder.embed_documents([d.text for d in batch]))
            # 【这行在干什么】汇报本批的向量化进度。
            # 【参数解释】min(start + _EMBED_BATCH, len(documents)) 取「本批应处理到的位置」
            #   与「总文档数」的较小值：这样最后一批不会显示出比总数还大的数字（如 1024/1000），
            #   保证进度显示成 64/1000、128/1000 … 1000/1000 这种正确形态。
            report(
                "embed",
                f"向量化进度 {min(start + _EMBED_BATCH, len(documents))}/{len(documents)}",
            )

        # 【这行在干什么】汇报「开始写入向量库」。
        report("upsert", "写入向量库 …")
        # 【这行在干什么】把文档、稠密向量、稀疏向量三者对应着写进向量库，并把写入条数记进报告。
        # 【参数逐个解释】
        #   · documents —— 文档（提供 ID、正文、payload 附加信息）；
        #   · dense_vectors —— 稠密向量（语义检索用）；
        #   · sparse_vectors —— 稀疏向量（关键词检索用）。
        #   三者按下标一一对应，缺一不可。
        # 【关键词 upsert 解释】upsert = update + insert，意为「有则覆盖、无则插入」。
        #   因为文档 ID 是稳定映射的，重跑建库时同样的 ID 会被覆盖而非重复插入——这就是幂等。
        summary["points"] = store.upsert_documents(documents, dense_vectors, sparse_vectors)
    # 【这行在干什么】else 分支：没有文档（空库）时的处理。
    else:
        # 【这行在干什么】把「空库」这条提醒加进警告清单。
        # 【为什么要写一条警告而不是静默略过】用户点「重建知识库」后如果看到「完成」却
        #   什么都搜不到，会困惑。明确告知「数据表是空的，灌数据后重跑即可」，
        #   用户就知道下一步该干什么了。
        summary["warnings"].append(
            "三张表均无数据，已建好空集合与空 BM25 索引；灌入数据后重跑建库即可。"
        )
        # 【这行在干什么】汇报「无文档可写、空库初始化完成」。
        # 【注意事项】这里用的是「正常完成」的口吻，不报错——空库是合法状态，不是故障。
        report("upsert", "无文档可写入，已完成空库初始化")

    # 【这行在干什么】设置报告里的「是否空库」标记：文档数为 0 就是空库。
    # 【写法解释】summary["documents"] == 0 是一个比较表达式，结果是 True 或 False。
    summary["empty"] = summary["documents"] == 0
    # 【这行在干什么】算出总耗时并记进报告。
    # 【参数解释】
    #   · time.time() - started —— 当前时间减去开始时间，得到流逝的秒数；
    #   · round(..., 2) —— 四舍五入保留 2 位小数，显示成 12.34 这种清爽的形式。
    summary["elapsed"] = round(time.time() - started, 2)
    # 【这行在干什么】汇报「建库完成」及总耗时。
    report("done", f"建库完成，耗时 {summary['elapsed']} 秒")
    # 【这行在干什么】把整份报告返回给调用方（命令行模式下会被打印出来；
    #   接口模式下会被包装成 JSON 返回给前端）。
    # 【缓存与知识库版本解耦】全量建库成功：写 data/kb_version.txt（ISO 时间 + 点数 + 差集数字）。
    # 文件内容一变，kb_version 指纹就变，问答缓存里针对旧知识库的答案全部自动失效
    # （不需要显式 clear；answer_cache 查询时以 kb_version 为必要条件）。
    write_kb_version(
        s,
        points=summary.get("points"),
        added=summary.get("added"),
        updated=summary.get("updated"),
        deleted=summary.get("deleted"),
    )
    return summary


# 【这行在干什么】定义 run_incremental：增量更新——只处理「有变化」的文档，最后删掉失效的旧块。
# 【整体思路（先算差集 → 再写入 → 最后删除，顺序至关重要）】
#   ① 生成全量文档（复用 generate_documents，只读）；
#   ② 拟合 BM25 并**合并旧词表**（保证旧文档已存的稀疏向量下标不失效）；
#   ③ 取回库里现有文档的「doc_id → 正文指纹」，和全量文档做差集，分出；
#        · added     —— 库里没有的新 doc_id；
#        · updated   —— doc_id 在、但正文 sha256 变了；
#        · unchanged —— doc_id 在、指纹也一样；
#        · deleted   —— 库里有、但这次全量文档里已不存在的 doc_id；
#   ④ 只对 added + updated 做向量化并写入（unchanged 完全不碰，省时省算力）；
#   ⑤ **最后**才删除 deleted（万一前面出错，删除还没发生，已有数据不会被破坏）。
# 【返回报告】在原有字段基础上增加 mode/added/updated/deleted/unchanged，便于前端回显。
def run_incremental(
    settings: Settings | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """增量更新：对比内容指纹，只写新增/变更的文档块，最后删除失效的旧块。"""

    # 【这行在干什么】和 run_ingest 同名同款的进度汇报闭包。
    def report(stage: str, message: str, **extra: Any) -> None:
        if progress:
            progress(stage=stage, message=message, **extra)

    # 【这行在干什么】拿到配置对象与开始时刻。
    s = settings or get_settings()
    started = time.time()
    # 【这行在干什么】准备报告，结构对齐全量模式（额外多出增量专属字段）。
    summary: dict[str, Any] = {
        "mode": "incremental",
        "tables": {},
        "documents": 0,
        "points": 0,
        "bm25_terms": 0,
        "bm25_docs": 0,
        "errors": [],
        "warnings": [],
        "empty": True,
        "elapsed": 0.0,
        # 增量专属：四个差集计数（added/updated/deleted 会被接口与命令行回显）。
        "added": 0,
        "updated": 0,
        "deleted": 0,
        "unchanged": 0,
    }

    # ---------------- 1. 生成全量文档（只读，复用纯函数） ---------------- #
    documents, token_lists, stage = generate_documents(s, progress)
    summary["tables"] = stage["tables"]
    summary["errors"] = stage["errors"]
    summary["warnings"] = stage["warnings"]
    summary["documents"] = stage["documents"]

    # ---------------- 2. 拟合 BM25 并「合并旧词表」 ---------------- #
    report("bm25", "拟合 BM25（增量：保留旧词表编号）…")
    # 【这行在干什么】用「全量语料」拟合 BM25，得到最新的统计量（n_docs / df / 平均长度）。
    bm25 = BM25Index(k1=s.bm25_k1, b=s.bm25_b).fit(token_lists)
    # 【这行在干什么】把上次存盘的旧词表读回来。
    # 【为什么必须读旧词表（本方案最关键的一步，别删）】BM25 的词编号是按 set() 遍历顺序
    #   分配的，跨进程不稳定。库里 unchanged 文档的稀疏向量是用「旧编号」编码并存下来的；
    #   如果这里直接用新 fit 的编号，旧向量就会指向「别的词」，检索结果张冠李戴。
    old_index = BM25Index.load(s.bm25_index_path, s.bm25_k1, s.bm25_b)
    if old_index.term_to_id:
        # 【这行在干什么】以旧词表为底，**只追加新词**、绝不改动老词编号（append-only）。
        merged = dict(old_index.term_to_id)
        # 【这行在干什么】新词从「旧词表最大编号 + 1」开始往后排，保证与旧编号不冲突。
        next_id = max(merged.values()) + 1
        for term in bm25.term_to_id:
            if term not in merged:
                merged[term] = next_id
                next_id += 1
        # 【这行在干什么】用合并后的词表替换 fit 出来的那份，再存盘。
        bm25.term_to_id = merged
    bm25.save(s.bm25_index_path)
    summary["bm25_terms"] = len(bm25.term_to_id)
    summary["bm25_docs"] = bm25.n_docs
    report("bm25", f"BM25 就绪：{bm25.n_docs} 篇文档 / {len(bm25.term_to_id)} 个词元")

    # ---------------- 3. 取现有指纹并算差集 ---------------- #
    store = get_store(s)
    # 【这行在干什么】确保集合存在，但**绝不重建**（recreate=False）——增量不能清空旧数据。
    store.ensure_collection(recreate=False)
    report("qdrant", "读取现有文档指纹（分批 scroll，避免一次拉爆内存）…")
    # 【这行在干什么】把库里现有文档一次性取成 {doc_id: 正文sha256}（内部已分批滚动）。
    existing = store.scroll_existing()

    # 【这行在干什么】遍历全量文档，按「有没有旧指纹、指纹一样不一样」分类。
    # 【为什么用下标记录而不是记对象】下标能把 documents 与 token_lists 对齐，
    #   后面编码稀疏向量时直接用 token_lists[i]，省去重复分词。
    new_ids: set[str] = set()
    added_idx: list[int] = []
    updated_idx: list[int] = []
    unchanged = 0
    for i, doc in enumerate(documents):
        new_ids.add(doc.doc_id)
        old_hash = existing.get(doc.doc_id)
        if old_hash is None:
            # 库里没有 → 新增。
            added_idx.append(i)
        elif old_hash != text_hash(doc.text):
            # 库里在、但正文变了 → 更新。
            updated_idx.append(i)
        else:
            # 指纹完全一致 → 未变（本次不动它）。
            unchanged += 1
    # 【这行在干什么】反向差集：库里有、但全量文档里已经没有的 doc_id，就是要删除的。
    deleted_ids = [doc_id for doc_id in existing if doc_id not in new_ids]

    summary["added"] = len(added_idx)
    summary["updated"] = len(updated_idx)
    summary["deleted"] = len(deleted_ids)
    summary["unchanged"] = unchanged
    report(
        "diff",
        f"差集完成：新增 {summary['added']} / 更新 {summary['updated']} / "
        f"删除 {summary['deleted']} / 未变 {summary['unchanged']}",
    )

    # ---------------- 4. 写入「新增 + 更新」的文档块 ---------------- #
    # 【这行在干什么】把要写的那批文档下标拼起来（先新增、后更新，顺序无所谓）。
    write_idx = added_idx + updated_idx
    if write_idx:
        # 【这行在干什么】取出要写的文档与它们的分词结果（靠下标配对）。
        to_write = [documents[i] for i in write_idx]
        write_tokens = [token_lists[i] for i in write_idx]
        # 【这行在干什么】向量化（分批，和全量模式用同一个批大小）。
        report("embed", f"向量化 {len(to_write)} 个文档块（模型 {s.embed_model}）…")
        embedder = get_embedder(s)
        dense_vectors: list[list[float]] = []
        for start in range(0, len(to_write), _EMBED_BATCH):
            batch = to_write[start : start + _EMBED_BATCH]
            dense_vectors.extend(embedder.embed_documents([d.text for d in batch]))
            report(
                "embed",
                f"向量化进度 {min(start + _EMBED_BATCH, len(to_write))}/{len(to_write)}",
            )
        # 【这行在干什么】用（已合并旧词表的）bm25 编码稀疏向量，保持与库里旧数据同一套下标。
        sparse_vectors = [bm25.encode_document(tokens) for tokens in write_tokens]
        # 【这行在干什么】写入向量库（upsert：同 doc_id 覆盖，天生幂等）。
        report("upsert", f"写入 {len(to_write)} 个文档块…")
        summary["points"] = store.upsert_documents(to_write, dense_vectors, sparse_vectors)
    else:
        # 【这行在干什么】没有任何新增/更新，跳过向量化与写入（增量「无变化」时的快速路径）。
        report("upsert", "无新增/变更文档，跳过写入")

    # ---------------- 5. 删除「数据源已消失」的旧块（放最后，最安全） ---------------- #
    if deleted_ids:
        report("delete", f"删除 {len(deleted_ids)} 个已失效文档块…")
        # 【这行在干什么】按 point id 批量删除（内部会先把 doc_id 换算成 UUID）。
        summary["deleted_points"] = store.delete_documents(deleted_ids)
    else:
        summary["deleted_points"] = 0

    # 【这行在干什么】收尾：置空库标记、算耗时、汇报完成。
    summary["empty"] = summary["documents"] == 0
    summary["elapsed"] = round(time.time() - started, 2)
    report("done", f"增量更新完成，耗时 {summary['elapsed']} 秒")
    # 【缓存与知识库版本解耦】增量更新成功：写 data/kb_version.txt（ISO 时间 + 点数 + 差集数字）。
    # 文件内容一变，kb_version 指纹就变，问答缓存里针对旧知识库的答案全部自动失效。
    write_kb_version(
        s,
        points=summary.get("points"),
        added=summary.get("added"),
        updated=summary.get("updated"),
        deleted=summary.get("deleted"),
    )
    return summary


# 【这行在干什么】定义函数 main：命令行入口。
# 【什么时候会执行它】直接运行 `python -m app.ingest` 时，
#   文件末尾的 `if __name__ == "__main__": main()` 会触发它。
def main() -> None:
    """命令行入口：打印带阶段前缀的进度，最后输出汇总。"""
    # 【这行在干什么】在函数内部导入 json 模块。
    # 【为什么不在文件顶部导入（值得注意的取舍）】json 只在「命令行打印结果」时才用得上；
    #   如果服务进程（接口模式）只是调用 run_ingest 而从不执行 main，
    #   顶部导入就白白执行了一次。放在函数内部属于「按需导入、就近使用」的小优化，
    #   也表明「它只服务于这个函数」。
    import json

    # 【这行在干什么】引入 argparse，用来解析命令行参数（如 --incremental）。
    # 【为什么放在函数内部】同 json：它只服务于命令行入口，接口模式用不到。
    import argparse

    # 【这行在干什么】定义命令行参数：默认全量；加 --incremental 才走增量更新。
    # 【为什么默认全量（关键）】不带任何参数运行 `python -m app.ingest` 时行为与改动前
    #   完全一致（全量重建），老习惯/老脚本不受影响。
    parser = argparse.ArgumentParser(description="建库管道：全量重建或增量更新")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="增量更新：只写入新增/内容变化的文档块，并删除数据源已消失的旧块（默认全量重建）",
    )
    args = parser.parse_args()
    # 【这行在干什么】把布尔开关翻译成 run_ingest 认的 mode 字符串。
    mode = "incremental" if args.incremental else "full"

    # 【这行在干什么】定义一个「进度打印」函数 printer，作为回调传给 run_ingest。
    # 【参数逐个解释】
    #   · stage —— 阶段名（由流水线传来）；
    #   · message —— 进度文字；
    #   · **_ —— 「收集并丢弃」其余的额外关键字参数（比如 table=表名）。
    #     用下划线 _ 当名字是惯例：表示「我知道这里有值，但我不需要用」，
    #     这样即便回调签名和流水线传来的额外信息不完全一致，也不会报错。
    def printer(stage: str, message: str, **_: Any) -> None:
        # 【这行在干什么】按「[ 阶段名] 消息」的格式记一行 INFO 日志。
        # 【写法拆解】"[%6s] %s" 与原来的 f"[{stage:>6}] {message}" 等价：
        #   · %6s —— 把阶段名右对齐、占 6 个字符宽（长短不一的阶段名对齐后更好看）；
        #   · 用 logging 的「惰性格式化」（参数分开传），比先拼好字符串再传更省开销。
        logger.info("[%6s] %s", stage, message)

    # 【这行在干什么】打开「接住岔子」的网，执行一次完整建库。
    # 【为什么这里要处心积虑地捕获异常】命令行工具崩溃时，用户希望看到「人话」错误提示，
    #   而不是一大坨看不懂的 Python 堆栈。所以分门别类地处理。
    try:
        # 【这行在干什么】执行建库，并把刚才定义的 printer 作为进度回调传进去。
        # 【为什么用 progress=printer 关键字传参】明确表明「我传的是进度回调」，
        #   避免和前面的位置参数搞混。
        # 【mode=mode】命令行开关决定跑全量还是增量（默认 full，行为不变）。
        result = run_ingest(progress=printer, mode=mode)
    # 【这行在干什么】专门捕获「数据库不可用」这种最常见、最易懂的错误。
    except DatabaseUnavailable as exc:
        # 【这行在干什么】记一条 ERROR 日志（前面加「[错误]」前缀）。
        logger.error("[错误] %s", exc)
        # 【这行在干什么】抛 SystemExit(1) 结束程序，并把「退出码」设为 1。
        # 【为什么要退出码 1（关键）】操作系统/脚本靠「退出码」判断程序成功与否：
        #   0 = 成功，非 0 = 失败。设为 1，能让外部的自动化脚本（如 CI、批处理）
        #   正确感知「建库失败了」，而不是被当成成功。这是命令行工具的基本礼仪。
        raise SystemExit(1)
    # 【这行在干什么】兜住其他任何意外错误。
    # 【注意事项】这里是用 `except Exception:`（不带 as）——我们不需要用到异常对象本身，
    #   因为下面用 logger.exception 会自动把当前异常的完整堆栈一并记下来。
    except Exception:
        # 【这行在干什么】记一条 ERROR 日志，并带上完整异常堆栈。
        # 【为什么不分别写「说明行 + traceback 行」】logger.exception 一步到位：
        #   既输出人话说明，又把「从出错点到入口的完整调用链」一并记下，方便定位。
        # 【为什么这种错误要带堆栈（与上面区别对待）】数据库不可用是「可预期、原因明确」
        #   的问题，给一句人话就够；而这种意外错误「原因不明」，必须把完整调用链亮出来。
        logger.exception("[错误] 建库失败：")
        # 【这行在干什么】同样以失败退出码结束程序。
        raise SystemExit(1)

    # 【这行在干什么】记一个分隔标题，表示下面是要展示最终结果了。
    # 【写法解释】"\n===== 建库结果 =====" 开头有个 \n，先空一行再记标题，视觉上更清楚。
    logger.info("\n===== 建库结果 =====")
    # 【这行在干什么】把整份结果报告以 JSON 格式漂亮地记录下来。
    # 【参数逐个解释】
    #   · json.dumps(...) —— 把 Python 字典转成 JSON 字符串；
    #   · ensure_ascii=False —— 关键参数：默认 True 会把中文转成 \uXXXX 转义码（看不懂）；
    #     设为 False 后中文原样输出，日志里能直接读懂。
    #   · indent=2 —— 每层缩进 2 个空格，输出成「多层结构清晰可读」的排版，而不是压成一行。
    logger.info("%s", json.dumps(result, ensure_ascii=False, indent=2))


# 【这行在干什么】判断「本文件是不是被直接运行」。
# 【原理】Python 里每个模块都有个内置变量 __name__：
#   · 直接运行本文件时，__name__ 等于 "__main__"；
#   · 被别的文件 import 时，__name__ 是本模块的名字（"app.ingest"）。
#   所以这个判断的含义是：「只有当这个文件被当场执行时，才调用 main()」。
# 【为什么必须这样写（重要设计）】因为 main.py 会「import ingest 来调用 run_ingest」，
#   如果这里不判断、直接写 main()，那么每次被导入都会自动跑一遍建库——这显然不行。
#   加上这个守卫，就实现了「既能当命令行工具、又能当被导入的库」两种身份。
if __name__ == "__main__":
    # 【这行在干什么】调用命令行入口函数，开始建库。
    main()