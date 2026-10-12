# ============================================================================
# 文件：graph.py —— 知识图谱查询工具（把自然语言翻译成只读 Cypher）
#
# 【这个文件在整个项目里是干什么的】
#   本项目回答招投标问题一共有三条"取资料"的路子（可同时用、由 Agent 自己选）：
#     · retriever.py —— 混合检索：擅长"语义相似的原文"（公告正文、条款措辞、资质描述）；
#     · pg.py        —— PostgreSQL：擅长"跨表统计、计数、求和、排序、精确筛选"；
#     · 本文件       —— Neo4j 图谱：擅长"**实体之间的关联**"。
#   什么叫"实体之间的关联"？举个例子：某公司中标了哪些项目、某项目有哪些参与单位、
#   两家公司共同参与过什么、某类关系一共有多少条。这类问题靠"文本相似度"是算不出来的，
#   因为普通的文本块里根本没有"关系/连线"这个视角，只有图谱里才有。
#
# 【三个必须懂的背景概念（大白话）】
#   1) Neo4j：一种"图数据库"。它存数据的方式不是表格，而是"点 + 连线"：
#      点（节点 node）代表一个实体（公司/项目/公告），连线（关系 relationship）
#      代表实体之间的事（"中标""参与""属于"）。查它要用专门的语言 Cypher。
#   2) Cypher：Neo4j 的查询语言，长得有点像画图：
#      MATCH (c:Company)-[:WINS]->(p:Project) 表示"找一家公司，它通过 WINS 关系连到一个项目"。
#   3) 自然语言 → Cypher：用户用中文提问，我们让大模型把这句话翻译成一条 Cypher，
#      再拿去执行。这就是本文件干的最核心的一件事。
#
# 【只读保护分三层（务必理解各自的定位，这是安全设计的关键）】
#   第 1 层 · 服务端强约束（**真正的安全边界**）：
#       所有生成的 Cypher 都通过 session.execute_read(...) 在「只读事务」里执行，
#       Neo4j 服务端会直接拒绝任何写操作（报 Writing in read access mode not allowed）。
#       【更保险的做法】建议在 Neo4j 里为本应用单独建一个"只读账号"。
#   第 2 层 · 静态关键字护栏（**不是安全边界**，只是提前挡刀）：
#       语句里只要出现写操作关键字（CREATE / MERGE / DELETE / SET ...）就直接拒绝，
#       并要求语句必须以只读子句开头。它的作用只是把"明显越界"的输入挡在发请求之前，
#       真正兜住安全的是第 1 层。
#   第 3 层 · 单语句 + 强制 LIMIT：
#       防止一次执行多条语句（用分号塞私货）、防止把整个图的数据一次拉回内存。
#
# 【一条重要的产品原则】本文件里的工具失败时，一律返回「人能读懂的失败提示」而不是抛异常。
#   这样 Agent 看到提示后可以诚实地告诉用户"这次没查到"，而不是瞎编一个答案。
#
# 【谁调用它（上游）】agent.py 把这个工具注册给大模型，由模型自主决定是否调用。
#
# 【它给谁用（下游）】前端展示可折叠的 Cypher 与结果表格；模型拿结果去作答。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring），内容与原文一字不差】
"""Neo4j 知识图谱查询工具（自然语言 → 只读 Cypher）。

与前两条路径**并行**的第三条路：

- ``retriever.py`` 混合检索擅长「语义相似的原文」（公告内容、条款措辞、资质描述）；
- ``pg.py`` 擅长「跨表统计、计数、求和、排序、精确筛选」；
- 本模块擅长「**实体之间的关联**」——某公司中标了哪些项目、某项目有哪些参与单位、
  两家公司的共同关联方、某类关系的数量/路径。这些用相似度算不出来，因为文本块里
  没有「关系」这个视角。

只读保护分三层，务必理解各自的定位：

1. **服务端强约束（真正的安全边界）**：所有生成的 Cypher 都通过
   ``session.execute_read(...)`` 在**只读事务**里执行，服务端会直接拒绝写操作
   （``Writing in read access mode not allowed``）。**建议再为应用单独建一个只读 Neo4j 账号。**
2. **静态关键字护栏（不是安全边界）**：命中写操作关键字（CREATE / MERGE / DELETE /
   SET / ...）即拒绝，且要求语句以只读子句起头。它只为把明显越界的输入挡在发请求之前。
3. **单语句 + 强制 LIMIT**：防止一次执行多条语句、防止把整个图拉回内存。

工具失败时一律返回「可读的失败提示」而不是抛异常，让 Agent 据此诚实作答、不编造。
"""

# 【这行在干什么】打开「类型注解延迟求值」开关（同其他模块的做法）。
# 【为什么这么写】本文件用了 dict[str, Any] 这类新式标注，老 Python 不认；
#   加上这行后标注先当字符串存着，需要时才解析，兼容性更好。
# 【注意事项】必须放在所有执行代码之前。
from __future__ import annotations

# 【这行在干什么】引入 asyncio（异步工具箱）。
# 【为什么需要】本文件最外层那个"工具函数"是 async 的，它要用 asyncio.to_thread
#   把同步阻塞的查询逻辑丢到旁支线程去跑（否则会卡住整个服务，详见文件末尾注释）。
import asyncio

# atexit：注册「进程退出时自动关闭缓存驱动」的关闭钩子（见 _close_drivers）。
import atexit

# 【这行在干什么】引入 json 模块，用来把字典序列化成 JSON 文本（渲染节点样例时用）。
import json

# 【这行在干什么】引入 re —— Python 的正则表达式模块（用来做文本模式匹配）。
# 【为什么需要】实现"只读护栏"要靠正则去检查语句里有没有写操作关键字、
#   有没有以只读子句开头、有没有 LIMIT 等（详见下面护栏部分的注释）。
import re

# 【这行在干什么】引入 time 模块，用来取当前时间戳（给 schema 缓存做过期判断）。
import time

# 【这行在干什么】引入 Any 类型（表示"任意类型"，用来兜住形态不定的值）。
from typing import Any

# 【这行在干什么】引入 RunnableConfig 与 tool 装饰器（同 agent.py 里的用法）。
# 【参数逐个解释】
#   · RunnableConfig —— LangChain 调用工具时会自动传进来的"随身包"，
#     本项目把每个请求的状态字典挂在里面（本文件从包里取 _graph_state）；
#   · tool          —— 把普通函数包装成"大模型能识别并调用的工具"的装饰器。
# 【重点】被 @tool 包装后，函数的 docstring 会被原样送给大模型当"工具说明书"——
#   模型判断"要不要用这个工具"就看那段文字，所以工具函数的 docstring 极其重要。
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

# 【这行在干什么】引入配置类与取配置函数（连接信息、模型名、超时等全来自 .env）。
from .config import Settings, get_settings

# 【这行在干什么】引入大模型相关的三样东西。
# 【参数逐个解释】
#   · LLMError          —— 大模型调用失败的通用异常；
#   · LLMNotConfigured  —— 专门的"根本没配大模型"异常；
#   · complete_chat     —— 一次性调用模型（本文件用它把自然语言翻译成 Cypher，
#                          要的是完整结果，不需要边生成边给用户看）。
from .llm import LLMError, LLMNotConfigured, complete_chat

# 工具名（函数名即注册给模型的名字，导出常量供 event 层引用）
# ----------------------------------------------------------------------------
# 【这行在干什么】定义一个常量，存本工具的名字。
# 【为什么单独定义常量】项目里别处（事件层、前端）需要"认出"哪些消息是本图谱工具产生的，
#   大家统一引用这个名字常量，避免各处手写字符串写错、写得不一致。
# 【为什么值要和函数名一样】大模型调用工具时用的就是这个名字，必须与下面的函数名一致。
GRAPH_TOOL_NAME = "query_bidding_graph"

# 渲染结果时单元格文本的截断长度
# ----------------------------------------------------------------------------
# 【这行在干什么】单个单元格（表格里一格）最多显示多少字符，超出就截断。
# 【为什么限制】图谱返回的字段可能很长（比如描述文本），不截断的话既占屏幕又浪费 token。
_CELL_TEXT_LIMIT = 300

# 回传给前端的结果行数上限（前端只展示不参与作答）
# ----------------------------------------------------------------------------
# 【这行在干什么】最多把多少行结果回传给前端展示。
# 【为什么是"只展示"】前端只拿它来画表格给用户看；真正喂给模型作答的是完整结果文本
#   （见 render_result）。所以这里的上限只影响界面展示，不影响答案质量，可以放心小一点。
_ROWS_TO_STATE = 20

# 写操作关键字护栏（大小写不敏感、按单词边界匹配）
# 注意：这里只挡「明显越界」的输入；真正的边界是只读事务（第 1 层）。
# ----------------------------------------------------------------------------
# 【这块在干什么】列出"只要出现就拒绝"的危险关键字清单。
# 【为什么是这些词】它们都是 Cypher 里能改数据或搞破坏的词：
#   create/merge（新增）、delete/detach/remove（删除）、set（修改）、drop（删结构）、
#   foreach（循环批改）、load csv（从文件导入）、grant/deny/revoke（改权限）、
#   apoc/dbms（高级管理过程）、start/stop（启停服务）。
# 【注意事项（务必理解定位）】这只是"提前挡刀"，不是真正的安全边界：
#   真正保证只读的是第 1 层的只读事务（服务端会拒绝写操作）。这一层只是为了
#   把明显越界的输入挡在发请求之前，少一次无谓的网络往返。
_FORBIDDEN_KEYWORDS = (
    "create", "merge", "delete", "detach", "set", "remove", "drop", "foreach",
    "load", "csv", "grant", "deny", "revoke", "apoc", "dbms", "start", "stop",
)
# 【这行在干什么】把上面的关键字列表拼成一条正则表达式，用于全文扫描。
# 【正则逐段解释】
#   · r"\b(" + "|".join(...) + r")\b" —— \b 表示"单词边界"，| 表示"或"，
#     所以整条意思是"匹配任意一个完整单词是这些关键字之一"。
#     【为什么加 \b】防止把 "created" 这种包含关系误判成 "create"（其实也拦得住，但更精确）。
#   · re.IGNORECASE —— 忽略大小写，这样 CREATE、Create、create 都能拦。
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)
# 子查询 CALL { ... } 里仍可能写入，直接拒绝
# ----------------------------------------------------------------------------
# 【这行在干什么】再建一条正则，专门匹配 CALL { 这种"子查询"写法。
# 【为什么单独拦它】CALL { ... } 是 Cypher 的"子查询"语法，花括号里面照样能写写操作；
#   光靠关键字清单可能被绕过去（比如写在子查询里），所以这里一律拒绝、直接不给执行。
# 【正则解释】\bcall\s*\{ 表示"单词 call 后面跟任意多个空白、然后一个左花括号"。
_CALL_SUBQUERY_RE = re.compile(r"\bcall\s*\{", re.IGNORECASE)
# 【这行在干什么】建一条正则，检查语句是不是"以只读子句开头"。
# 【为什么需要】只读查询必须以 MATCH / OPTIONAL MATCH / WITH / UNWIND / RETURN 这几个词开头；
#   如果开头是别的（比如 CREATE），说明它压根不是只读查询，直接拒绝。
# 【正则解释】^\s*(match|optional\s+match|with|unwind|return)\b 表示
#   "从头开始，先允许一些空白，然后是这几个词之一，且后面是单词边界"。
_START_RE = re.compile(r"^\s*(match|optional\s+match|with|unwind|return)\b", re.IGNORECASE)
# 【这行在干什么】建一条正则，用来"抓出 LIMIT 后面跟的那个数字"。
# 【为什么要抓数字】如果模型写的 LIMIT 太大（比如 100000），我们要把它改小到安全值；
#   这时需要知道原数字在文本里的位置，才能精确替换（见 validate_cypher）。
# 【正则解释】\blimit\s+(\d+) —— 括号里的 (\d+) 是"捕获组"，代表一串数字，会被单独取出来。
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)
# 【这行在干什么】建一条正则，只判断"语句里有没有出现过 LIMIT 这个词"，不关心具体数字。
# 【为什么需要它】用来判断"要不要给语句补一个 LIMIT"（完全没有 LIMIT 时才补）。
_LIMIT_ANY_RE = re.compile(r"\blimit\b", re.IGNORECASE)
# 【这两行在干什么】识别「假上限」的两种写法：
#   · LIMIT ALL —— 名义上写了 LIMIT、实际不限行数（把整个图拉回来），必须换成安全上限；
#   · FETCH FIRST n ROWS ONLY —— SQL 标准的行数上限写法，视为「已有上限」，
#     没写数字或数字超限时把整段换成 LIMIT max_rows，绝不能在它后面再叠一个 LIMIT。
_LIMIT_ALL_RE = re.compile(r"\blimit\s+all\b", re.IGNORECASE)
_FETCH_FIRST_RE = re.compile(r"\bfetch\s+(?:first|next)\s+(?:(\d+)\s+)?rows?\s+only\b", re.IGNORECASE)
# 【这行在干什么】建一条正则，用来剥掉大模型爱加的"markdown 代码围栏"。
# 【什么是代码围栏】就是模型常把代码包成这样：
#     ```cypher
#     MATCH ...
#     ```
#   这两个 ``` 不是 Cypher 的一部分，必须去掉，否则执行会报语法错。
# 【正则解释】^```[a-zA-Z]*\s* 匹配"开头的三个反引号 + 可选的语言名（如 cypher）+ 空白"；
#   |\s*```$ 匹配"结尾的三个反引号"。中间那个 | 表示"或者"。
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

# schema 缓存：键为 (uri, database)，值为 (取到的时间戳, 结构字典)
# ----------------------------------------------------------------------------
# 【这行在干什么】建一个缓存字典，用来"暂时记住"图谱的结构（有哪些标签、关系、属性）。
# 【键的设计】键用 (连接地址, 数据库名) 的组合 —— 因为连不同库、不同地址结构可能完全不同，
#   必须分开缓存，不能混用。
# 【值的设计】值用 (取到的时间戳, 结构字典) —— 时间戳用于判断缓存是否过期（TTL 机制）。
# 【为什么需要缓存（重要）】读图谱结构要执行好几条 Cypher（列标签、列关系类型、逐标签取属性…），
#   相当费时；而图谱结构平时几乎不变。所以读一次、缓存一段时间（TTL），能省下大量时间。
# 【注意事项】结构真变了也不怕：等 TTL 过期会自动重取，或者调用时传 refresh=True 强制刷新。
_schema_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

# 驱动缓存：Neo4j 驱动本就是长生命周期、线程安全的对象，复用即可
# ----------------------------------------------------------------------------
# 【这行在干什么】建一个缓存字典，存"数据库连接驱动"对象。
# 【键的设计】用 (地址, 用户名, 密码, 库名) 四个一组当键 —— 只要其中任何一个不同，
#   就是不同的连接，必须分开缓存。
# 【为什么可以缓存驱动（重要）】Neo4j 的驱动对象天生就是"长生命周期 + 线程安全"的
#   （官方就是这么设计的），所以建一次、反复用是正确的用法，不必每次查询都重建。
#   反复创建驱动会造成连接泄漏、性能变差。
# 【潜在风险】密码变更后旧缓存仍在用 —— 本项目连接信息来自 .env，
#   改完 .env 需要重启后端（.env 本身也有缓存），因此这个风险在项目里可接受。
_drivers: dict[tuple[str, str, str, str], Any] = {}


# 【这个类在干什么】定义本模块专用的异常类型，表示"图谱查询出了问题"。
# 【参数逐个解释】括号里的 RuntimeError 表示"继承自 RuntimeError"——
#   也就是它本质是一种运行时错误，但有了自己的名字，方便别处精确捕获它。
# 【为什么自定义异常而不用现成的】有了专属类型，调用方就能写成"只捕获 GraphError"，
#   这样不会把别的无关错误也一起吞掉，错误处理更精准。
class GraphError(RuntimeError):
    """Neo4j 查询的可读错误（连不上 / 生成失败 / 被护栏拒绝）。"""


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
# 【这段注释在干什么】把文件分成若干"章节"，这是"连接"这一章的分隔标记。
# 【下面这个函数在干什么】取（或者新建）数据库连接驱动。
# 【参数逐个解释】
#   · s —— 配置对象（要从中读连接地址、账号密码、库名、超时）。
# 【返回值】一个 Neo4j 驱动对象（可以理解为"通往数据库的连接工厂"）。
def _get_driver(s: Settings):
    """取（并缓存）连接驱动。``connection_timeout`` 限制建连时间；全程只读访问。"""
    # 【下面这段在干什么】尝试导入 neo4j 库。
    try:
        from neo4j import GraphDatabase, READ_ACCESS
    # 【下面这段在干什么】如果没装 neo4j 包（ImportError），就抛一个"人话版"错误。
    # 【为什么用 raise ... from exc】from exc 会把原始错误"挂"在新错误上（这叫异常链），
    #   这样既给出了友好提示，又保留了排错线索。
    # 【pragma: no cover 是啥】告诉测试覆盖率工具"这段不用统计覆盖率"（因为正常环境里走不到）。
    except ImportError as exc:  # pragma: no cover
        raise GraphError("缺少依赖 neo4j，请执行 pip install neo4j") from exc

    # 【这行在干什么】把四个连接参数拼成"缓存键"（只要有一个不同就是不同连接）。
    key = (s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
    # 【这行在干什么】从缓存里找找看，这个键对应的驱动是不是已经建过了。
    driver = _drivers.get(key)
    # 【这行在干什么】如果缓存里没有（None），就新建一个。
    if driver is None:
        # 【下面这段在干什么】真正创建驱动，失败则抛出可读错误。
        try:
            # 【这行在干什么】创建 Neo4j 驱动对象。
            # 【参数逐个解释】
            #   · s.neo4j_uri      —— 连接地址（形如 bolt://主机:端口），来自 .env；
            #   · auth=(用户, 密码) —— 认证信息，同样来自 .env，绝不硬编码；
            #   · default_access_mode=READ_ACCESS —— 会话默认「只读访问模式」：
            #       整个模块只做查询，这样连 _introspect_rows 那种 session.run（自动提交、
            #       不走 execute_read）也被服务端按只读对待，写操作一律被拒；
            #   · connection_timeout=max(1, s.neo4j_timeout)      —— 建立连接的最长等待秒数
            #       （用 max(1, ...) 兜底，防止配置成 0 或负数导致立刻超时）。
            #       注意：它只管「建连」，管不住「一条查询跑多久」——查询上限见 _query；
            #   · max_transaction_retry_time=max(1, s.neo4j_timeout) —— 事务重试的总时长上限，
            #       同样兜底到至少 1 秒。
            driver = GraphDatabase.driver(
                s.neo4j_uri,
                auth=(s.neo4j_user, s.neo4j_password),
                default_access_mode=READ_ACCESS,
                connection_timeout=max(1, s.neo4j_timeout),
                max_transaction_retry_time=max(1, s.neo4j_timeout),
            )
        # 【下面这段在干什么】建连失败时，统一转成一句人能看懂的 GraphError。
        # 【noqa: BLE001 是什么意思】给代码检查工具的"忽略提示"：我知道这里抓得太宽，
        #   这是故意的（任何建连问题都要转成友好提示，再往上抛）。
        except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
            raise GraphError(f"创建 Neo4j 连接失败（{s.neo4j_uri}）：{exc}") from exc
        # 【这行在干什么】把新建的驱动存进缓存，下次直接复用，不必重建。
        _drivers[key] = driver
    # 【这行在干什么】返回驱动（不管是缓存里拿的还是刚建的）。
    return driver


# 【这个函数在干什么】关闭缓存里的全部驱动并清空缓存。
# 【为什么需要】驱动是长连接对象，进程退出前应显式关闭（否则 Windows 上可能拖慢退出、
#   或留下未释放的连接）。既作为 atexit 退出钩子，测试里也能直接调用断言。
def _close_drivers() -> None:
    for driver in list(_drivers.values()):
        try:
            driver.close()
        except Exception:  # noqa: BLE001 - 退出钩子里绝不抛异常
            pass
    _drivers.clear()


# 【这行在干什么】注册进程退出钩子：Python 正常退出时自动关闭缓存的驱动。
atexit.register(_close_drivers)


# 【这个函数在干什么】把 Cypher 包成带「事务超时」的 Query 对象（真正的查询上限）。
# 【为什么不用 NEO4J_TIMEOUT】那只管建连与事务重试，管不住「一条查询跑多久」；
#   慢查询会一直挂着占连接。查询上限用事务超时：服务端到点终止执行（Query 的 timeout）。
# 【timeout=None 的含义】配置为 0/负数 = 不限时（Neo4j 的 Query 约定：None 用库默认值）。
def _query(cypher: str, s: Settings):
    from neo4j import Query

    seconds = s.neo4j_query_timeout
    return Query(cypher, timeout=float(seconds) if seconds and seconds > 0 else None)


# 【这个函数在干什么】拿一个"会话"（session）——可以把会话理解成"一次可用的工作窗口"。
# 【参数逐个解释】
#   · s —— 配置对象。
# 【注意事项】`s.neo4j_database or None`：如果配置里库名是空字符串，就传 None
#   （Neo4j 用 None 表示"用默认库"）；传空字符串反而可能出问题。
def _session(s: Settings):
    return _get_driver(s).session(database=s.neo4j_database or None)


# 【这个函数在干什么】在「只读事务」里执行生成的 Cypher（这是第 1 层、也是最关键的安全边界）。
# 【什么是只读事务（大白话）】事务=一组当作整体来执行的操作；
#   只读事务=声明"我只看不写"，Neo4j 服务端会强制拒绝其中的任何写操作。
#   所以哪怕护栏没拦住、模型真生成了一条写语句，服务端也会报错拒绝执行。
# 【参数逐个解释】
#   · s      —— 配置对象；
#   · cypher —— 要执行的只读查询语句。
# 【返回值】查询结果，是"每行一个字典"的列表。
def _read_rows(s: Settings, cypher: str) -> list[dict[str, Any]]:
    """在**只读事务**里执行生成的 Cypher（第 1 层安全边界）。"""
    # 【下面这段在干什么】打开会话、执行只读查询，失败时转成可读错误。
    try:
        # 【这行在干什么】用 with 打开会话 —— with 的好处是"用完自动关闭/清理"，
        #   即使中途出错也不会忘记释放资源（这叫上下文管理器）。
        with _session(s) as session:
            # 【这行在干什么】execute_read 表示"在只读事务里执行"；
            #   它接收一个小函数（lambda），该函数拿到事务对象 tx 后，跑我们的语并取回数据。
            # 【lambda 是什么】一种"临时的一次性小函数"，写法紧凑；
            #   这里 `lambda tx: tx.run(query).data()` 等价于"给我一个事务，我就执行语句并取数据"。
            # 【为什么要用 _query 包一层】给语句带上「事务超时」——查询跑多久的真正上限。
            # 【.data() 干什么】把查询结果转成"列表套字典"的普通 Python 结构，方便后续处理。
            query = _query(cypher, s)
            return session.execute_read(lambda tx: tx.run(query).data())
    # 【下面这段在干什么】如果是我们自己抛的 GraphError，原样继续往上抛（不重复包装）。
    except GraphError:
        raise
    # 【下面这段在干什么】其它任何失败（连不上、语句有语法错等）都统一转成可读的 GraphError。
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        raise GraphError(f"连接或执行 Neo4j 失败（{s.neo4j_uri}）：{exc}") from exc


# 【这个函数在干什么】执行"结构自省"语句（列标签、列关系类型等），用来读取图谱长什么样。
# 【参数逐个解释】
#   · s      —— 配置；
#   · cypher —— 要执行的语句。
# 【和 _read_rows 的区别（关键）】
#   · _read_rows     执行的是"由模型生成"的语句 → 必须走只读事务（防模型乱来）；
#   · 本函数         执行的是"由代码写死"的语句（固定不动的几句）→ 不存在越界风险，
#     所以直接用普通的 session.run 即可。
#   换句话说：**语句来源不同，安全等级要求也就不同**——固定语句不必走只读事务。
# 【但仍然要两道保险】驱动开了 default_access_mode=READ（服务端按只读对待一切会话语句），
#   语句再包 _query 带上事务超时——自省查询也不能无限跑。
def _introspect_rows(s: Settings, cypher: str) -> list[dict[str, Any]]:
    """执行**固定**的结构自省语句（语句由代码写死，不由模型生成）。"""
    try:
        with _session(s) as session:
            # 【这行在干什么】直接执行语句并取回数据（"列表套字典"），带事务超时。
            return session.run(_query(cypher, s)).data()
    except GraphError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        raise GraphError(f"连接或执行 Neo4j 失败（{s.neo4j_uri}）：{exc}") from exc


# --------------------------------------------------------------------------- #
# schema 自动读取
# --------------------------------------------------------------------------- #
# 【这段注释】这是"自动读取图谱结构"这一章。"schema" 指数据结构（有哪些标签、关系、属性）。
# 【下面这个函数在干什么】给标签名或属性名加上反引号，防止名字里有特殊字符导致语法错误。
# 【为什么要包反引号】Cypher 里如果名字是保留字或含特殊符号（如横杠），必须用反引号包起来
#   （形如 `my-label`）才能当普通名字用。
# 【参数逐个解释】
#   · name —— 要处理的标签/属性名。
def _quote_ident(name: str) -> str:
    """用反引号包裹标签/属性名，避免特殊字符导致的语法错误。"""
    # 【这行在干什么】先转字符串，再把名字里原本就有的反引号"双写"（转义），最后整体用反引号包住。
    # 【为什么要把内部的 ` 双写】Cypher 规定：反引号里要表示反引号本身，就写两个反引号（``）；
    #   不这样做，名字里若含反引号就会把语法撑坏。
    return "`" + str(name).replace("`", "``") + "`"


# 【这个函数在干什么】把节点的标签列表渲染成 :A:B 这种形式（Cypher 里标签的书写风格）。
# 【参数逐个解释】
#   · labels —— 标签列表（可能为空、也可能根本不是列表）。
def _labels_text(labels: Any) -> str:
    """把节点标签列表渲染成 ``:A:B`` 形式。"""
    # 【这行在干什么】如果 labels 不是列表或元组（比如直接是字符串），就直接转字符串返回。
    # 【为什么先做类型判断】Neo4j 返回的标签形态可能不统一，先兜住意外情况防止报错。
    if not isinstance(labels, (list, tuple)):
        return str(labels)
    # 【这行在干什么】给每个标签前面加一个冒号，然后拼成一串，形如 :公司:企业。
    # 【为什么最后 or ":（无标签）"】如果列表是空的，拼出来是空串，那样不好看也没信息量，
    #   所以空的时候显示"（无标签）"这个占位说明。
    return "".join(f":{label}" for label in labels) or ":（无标签）"


# 【这个函数在干什么】真正去读图谱结构：有哪些标签、哪些关系类型、哪些关系三元组、
#   每个标签有哪些属性/样例/节点数，最后打包成一本字典。
# 【只用 Neo4j 自带过程（关键取舍）】本函数只使用 Neo4j 自带的 db.labels() 与
#   db.relationshipTypes()，**不依赖 APOC**（APOC 是一套额外的插件库）。
#   这样即使目标环境没装 APOC 也能正常工作，兼容性更好。
# 【参数逐个解释】
#   · s —— 配置对象。
# 【返回值】结构字典（供渲染成给模型看的文本）。
def _read_schema_live(s: Settings) -> dict[str, Any]:
    """读取图谱结构：标签、关系类型、关系三元组、各标签的属性/样例/计数。

    只用 Neo4j 自带的 ``db.labels()`` / ``db.relationshipTypes()``，不依赖 APOC。
    """
    # 【这块在干什么】拿到图谱里所有"节点标签"的名字（这是"列表推导式"写法）。
    # 【列表推导式是什么】`[表达式 for x in 某列表 if 某条件]` 是"一边遍历一边筛选并生成新列表"
    #   的紧凑写法，等价于一个 for 循环 + 逐项判断 + append。
    # 【逐句解释】
    #   · _introspect_rows(s, "CALL db.labels() ...") —— 执行一句固定的 Cypher，列出所有标签；
    #   · for row in ... —— 逐行遍历结果；
    #   · if row.get("label") —— 只保留"label 字段有值"的行（过滤掉空的）；
    #   · row["label"]      —— 只要标签名字本身，放进结果列表。
    labels = [
        row["label"]
        for row in _introspect_rows(s, "CALL db.labels() YIELD label RETURN label ORDER BY label")
        if row.get("label")
    ]
    # 【这块在干什么】同理，拿到图谱里所有"关系类型"的名字（比如 WINS、PARTICIPATES）。
    rel_types = [
        row["relationshipType"]
        for row in _introspect_rows(
            s, "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType"
        )
        if row.get("relationshipType")
    ]

    # 【这块在干什么】查询"关系三元组"：什么样式的起点 -[关系]-> 终点，各有多少条。
    # 【三元组是什么意思】用（起点标签，关系类型，终点标签）三段来描述一类关系，
    #   例如 "公司 -[中标]-> 项目，共 500 条"。这是最能让模型理解图谱结构的信息。
    triples = _introspect_rows(
        s,
        # 【这句 Cypher 逐段解释】
        #   MATCH (a)-[r]->(b)                       —— 匹配任意"点-关系-点"，
        #                                                a 是起点、r 是关系、b 是终点；
        #   RETURN labels(a) AS from_labels,         —— 起点有哪些标签；
        #          type(r) AS rel,                   —— 关系是什么类型；
        #          labels(b) AS to_labels,           —— 终点有哪些标签；
        #          count(*) AS c                     —— 这种组合有多少条；
        #   ORDER BY c DESC LIMIT 500                —— 按数量从多到少排，最多取 500 行。
        # 【为什么按数量排序】数量多的关系通常更重要，优先展示给模型。
        "MATCH (a)-[r]->(b) "
        "RETURN labels(a) AS from_labels, type(r) AS rel, labels(b) AS to_labels, count(*) AS c "
        "ORDER BY c DESC LIMIT 500",
    )

    # 【这行在干什么】准备一个列表，装"每个标签的详细信息"。
    nodes: list[dict[str, Any]] = []
    # 【这行在干什么】逐个标签去收集它的属性、样例、节点数。
    for label in labels:
        # 【这行在干什么】把标签名用反引号包起来（防止特殊字符），再拼进后面的语句里。
        quoted = _quote_ident(label)
        # 【下面这段在干什么】取这个标签下的节点都用了哪些属性名（字段名）。
        try:
            # 【这句 Cypher 逐段解释】
            #   MATCH (n:`标签`)              —— 匹配该标签下的节点；
            #   WITH n LIMIT 20              —— 只看前 20 个（足够采出属性名了，还能省时间）；
            #   UNWIND keys(n) AS k          —— 把节点的"属性名集合"摊开成一行行（UNWIND 是"展开"）；
            #   RETURN DISTINCT k AS key     —— 去重后返回属性名；
            #   ORDER BY key                 —— 按名字排个序，输出稳定好看。
            key_rows = _introspect_rows(
                s, f"MATCH (n:{quoted}) WITH n LIMIT 20 UNWIND keys(n) AS k RETURN DISTINCT k AS key ORDER BY key"
            )
            # 【这行在干什么】把每行的 key 字段取出来，组成属性名列表。
            props = [row["key"] for row in key_rows if row.get("key")]
        # 【下面这段在干什么】万一取属性失败（比如权限问题），不影响主流程，只是"没有属性信息"。
        # 【为什么可以容忍失败】属性只是给模型的"辅助信息"，不影响能不能查图谱，所以失败就空着。
        except Exception:  # noqa: BLE001 - 属性只是辅助信息，取不到不影响主流程
            props = []
        # 【下面这段在干什么】取这个标签下 2 条样例数据，让模型"看到真数据长什么样"。
        try:
            # 【这句 Cypher 解释】取两条节点的完整属性（properties(n) 返回该节点的所有属性字典）。
            sample_rows = _introspect_rows(
                s, f"MATCH (n:{quoted}) RETURN properties(n) AS props LIMIT 2"
            )
            # 【这行在干什么】把每行的 props 取出来；若为空就用空字典兜底。
            samples = [row.get("props") or {} for row in sample_rows]
        except Exception:  # noqa: BLE001
            samples = []
        # 【下面这段在干什么】数一数这个标签下一共有多少个节点。
        try:
            count_rows = _introspect_rows(s, f"MATCH (n:{quoted}) RETURN count(n) AS c")
            # 【这行在干什么】取第一行的计数并转成整数；若查不到行则当作 0。
            count = int(count_rows[0]["c"]) if count_rows else 0
        except Exception:  # noqa: BLE001
            # 【这行在干什么】数不出来时用 None 表示"未知"（而不是 0，0 是明确"没有"，含义不同）。
            count = None
        # 【这行在干什么】把这个标签的信息打包成一个字典，加进结果列表。
        nodes.append({"label": label, "count": count, "properties": props, "samples": samples})

    # 【这行在干什么】把整份结构打包返回。
    return {
        # 当前连的是哪个数据库
        "database": s.neo4j_database,
        # 全部节点标签
        "labels": labels,
        # 全部关系类型
        "relationship_types": rel_types,
        # 关系三元组（含数量）
        "triples": triples,
        # 每个标签的详细信息
        "nodes": nodes,
    }


# 【这个函数在干什么】对外的"读结构"入口，带 TTL 缓存（读一次，短时间内直接用缓存）。
# 【参数逐个解释】
#   · s       —— 配置对象；
#   · refresh —— 是否强制刷新（True 表示"别用缓存，重新读一遍"）。
# 【返回】结构字典。
def load_schema(s: Settings, refresh: bool = False) -> dict[str, Any]:
    """读取图谱结构（带 TTL 缓存）。结构变动后可等过期或用 ``refresh=True`` 强制重取。"""
    # 【这行在干什么】拼缓存键（连接地址 + 库名）。
    key = (s.neo4j_uri, s.neo4j_database)
    # 【这行在干什么】从缓存里取出这个键对应的记录（可能是 None，表示没缓存过）。
    cached = _schema_cache.get(key)
    # 【这行在干什么】取当前时间戳，用来算"缓存放了多久"。
    now = time.time()
    # 【这行在干什么】判断能不能直接用缓存，三个条件同时满足才行：
    #   ① 没有要求强制刷新；
    #   ② 缓存确实存在（cached 不是 None）；
    #   ③ 缓存还没过期 —— now - cached[0] 是"存放至今过了多少秒"，
    #      小于配置的 graph_schema_ttl 就算没过期（max(0, ...) 防止配置成负数）。
    if not refresh and cached is not None and now - cached[0] < max(0, s.graph_schema_ttl):
        # 【这行在干什么】直接返回缓存里的结构字典（cached[1] 就是那份字典）。
        return cached[1]
    # 【这行在干什么】缓存不能用，真去读一次最新结构。
    data = _read_schema_live(s)
    # 【这行在干什么】把刚读到的结构 + 当前时间戳存进缓存，供后续复用。
    _schema_cache[key] = (now, data)
    # 【这行在干什么】返回刚读到的结构。
    return data


# 【这个函数在干什么】把结构字典"翻译"成一段给大模型看的紧凑文本。
# 【为什么需要这一步】模型看不懂 Python 字典；要把它渲染成文字说明，模型才知道
#   "有哪些标签、哪些关系、字段叫什么"，才能据此生成正确的 Cypher。
# 【参数逐个解释】
#   · data —— 前面读出来的结构字典。
# 【返回值】一段多行文本。
def render_schema_text(data: dict[str, Any]) -> str:
    """把结构字典渲染成给大模型看的紧凑文本。"""
    # 【这行在干什么】先起一行，写明"这是 Neo4j 数据库：某某"。
    lines = [f"Neo4j 数据库：{data.get('database')}"]
    # 【这行在干什么】取出节点列表；若为空则用空列表兜底。
    nodes = data.get("nodes") or []
    # 【这行在干什么】如果一个节点都没有，说明图谱是空的，直接提示后返回。
    if not nodes:
        lines.append("（图谱中还没有任何节点）")
        # 【这行在干什么】把所有行用换行符"缝"成一段文本返回。
        return "\n".join(lines)

    # 【这行在干什么】加一个小标题"节点标签："。
    lines.append("\n节点标签：")
    # 【这行在干什么】逐个标签输出它的信息。
    for node in nodes:
        # 【这行在干什么】取出这个标签的节点数（可能是整数，也可能是 None）。
        count = node.get("count")
        # 【这行在干什么】把数量渲染成文字：如果是整数就写"约 N 个节点"，否则写"节点数未知"。
        # 【为什么要区分】None 表示"数不出来"，和 0（明确没有）含义不同，不能一律显示 0。
        count_text = f"约 {count} 个节点" if isinstance(count, int) else "节点数未知"
        # 【这行在干什么】输出一行："标签名"（节点数）。
        lines.append(f'  "{node["label"]}"（{count_text}）')
        # 【这行在干什么】如果这个标签有属性名，就输出一行"属性：..."。
        if node.get("properties"):
            # 【", ".join(...) 干什么】把属性名列表用逗号+空格连成一串，形如 "name", "amount", "date"。
            lines.append("    属性：" + ", ".join(f'"{p}"' for p in node["properties"]))
        # 【这行在干什么】逐条输出样例数据（让模型看到真数据的样子）。
        for sample in node.get("samples") or []:
            # 【这行在干什么】把样例字典转成 JSON 文本输出。
            # 【参数逐个解释】
            #   · ensure_ascii=False —— 让中文正常显示（否则中文会被转成 \uXXXX 这种编码，人看不懂）；
            #   · default=str        —— 遇到 JSON 不认识的类型（如日期对象）就退化成字符串，避免报错。
            lines.append("    样例：" + json.dumps(sample, ensure_ascii=False, default=str))

    # 【这行在干什么】输出"关系类型"一行（把类型名用逗号连起来；没有就显示"（无）"）。
    lines.append("\n关系类型：" + (", ".join(data.get("relationship_types") or []) or "（无）"))

    # 【这行在干什么】取出关系三元组列表。
    triples = data.get("triples") or []
    # 【这行在干什么】输出三元组小标题。
    lines.append("\n关系三元组（起点标签 -[关系]-> 终点标签，含数量）：")
    # 【这行在干什么】如果没有任何关系，就提示一句。
    if not triples:
        lines.append("  （图谱中还没有任何关系）")
    # 【这行在干什么】逐条渲染三元组，形如：:公司 -[WINS]-> :项目（约 500 条）。
    for row in triples:
        lines.append(
            # 【这行在干什么】用 f-string 把起止标签、关系名、数量拼成一句话。
            # 【注意】这里用 _labels_text 把标签列表渲染成 :A:B 的形式。
            f'  {_labels_text(row.get("from_labels"))} -[{row.get("rel")}]-> '
            f'{_labels_text(row.get("to_labels"))}（约 {row.get("c")} 条）'
        )
    # 【这行在干什么】把所有行缝成一段文本返回。
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 自然语言 → Cypher
# --------------------------------------------------------------------------- #
# 【这段注释】这是"把中文问题翻译成 Cypher"这一章。
# 【下面这个函数在干什么】生成"翻译官"用的系统提示词（告诉模型它是 Cypher 专家、有哪些硬要求）。
# 【参数逐个解释】
#   · max_rows —— 最多允许查多少行（会写进提示词，告诉模型 LIMIT 的上限）。
def _cypher_system_prompt(max_rows: int) -> str:
    return (
        # 定角色 + 交代任务
        "你是 Neo4j Cypher 专家，负责把用户的问题翻译成一条**只读** Cypher 查询。\n"
        # 小标题
        "硬性要求：\n"
        # 要求 1：只读（与第 2 层护栏呼应）
        "1. 只生成只读查询，严禁任何写操作（CREATE / MERGE / DELETE / SET / REMOVE / DROP 等）；\n"
        # 要求 2：只能用真实存在的标签/关系/属性，不许凭空编造（否则查不到还会报错）
        "2. 只能使用下面 schema 中真实出现的节点标签、关系类型与属性名，不要臆造；\n"
        # 要求 3：方向不确定时用无向匹配（避免因为猜错方向而查不到数据）
        "3. 关系方向的写法：若不确定方向，用无向匹配 `-[r:TYPE]-`；\n"
        # 要求 4：必须带 LIMIT 且不超过上限（f-string 把 max_rows 填进去）
        f"4. 必须带 LIMIT，且不超过 {max_rows}；\n"
        # 要求 5：只输出语句本身，别加解释/代码块/分号/多条语句（减少后续清洗工作量）
        "5. 只输出 Cypher 本身：不要解释、不要 markdown 代码块、不要结尾分号、不要多条语句。\n"
        # 给一个示例，让模型照着模仿（示例教学通常比纯文字要求更有效）
        "示例：MATCH (c:Company)-[:WINS]->(p:Project) WHERE c.name CONTAINS '空调' "
        f"RETURN c.name AS 公司, p.name AS 项目 LIMIT {max_rows}"
    )


# 【这个函数在干什么】把"翻译 Cypher"要发给模型的消息拼好（系统提示词 + 图谱结构 + 用户问题）。
# 【参数逐个解释】
#   · question    —— 用户的问题；
#   · schema_text —— 渲染好的图谱结构文本（让模型知道有哪些标签/关系/属性可用）；
#   · max_rows    —— LIMIT 上限。
# 【返回值】消息列表。
def build_cypher_messages(question: str, schema_text: str, max_rows: int) -> list[dict[str, str]]:
    return [
        # 系统消息（人设 + 硬要求）
        {"role": "system", "content": _cypher_system_prompt(max_rows)},
        # 用户消息：先给结构说明，再给问题（\n\n 空一行分隔，读起来清楚）
        {"role": "user", "content": f"图谱结构：\n{schema_text}\n\n用户问题：{question}"},
    ]


# 【这个函数在干什么】扫描 Cypher 文本，一次产出两样护栏要用的东西：
#   1) 探测视图 —— 引号/反引号里的内容、注释里的内容换成等长空格。「等长」是关键：
#      探测出的下标要能原样映射回原句去改写，长度一变下标就全错位。
#      这样 WHERE n.name = 'no limit here' 这类常量、// limit 这类注释，
#      不会再骗过「有没有 LIMIT」的判定（它们不是真正的 LIMIT 子句）。
#   2) 末尾注释起点 —— 引号外最后一个行尾注释（// 或 --）的下标；没有则 None。
# 【引号规则】与 _has_multiple_statements 保持一致：单/双引号支持转义，反引号包名字。
def _scan_cypher_text(cypher: str) -> tuple[str, int | None]:
    out = list(cypher)
    cut: int | None = None
    quote: str | None = None
    i, n = 0, len(cypher)
    while i < n:
        ch = cypher[i]
        if quote is not None:
            # 转义：反斜杠吃掉下一个字符；成对引号是引号自身的转义。
            if ch == "\\" and quote != "`":
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and cypher[i + 1] == quote:
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
        # 行尾注释：// 或 -- 到行尾。记下起点（后出现的会覆盖，留最后一个）。
        if cypher[i : i + 2] in ("//", "--"):
            cut = i
            while i < n and cypher[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out), cut


# 【这个函数在干什么】剥掉行注释（// 或 --）：整行的删掉整行，行尾的删到行尾。
# 【关键】引号里的 // 不是注释（例如 n.url = 'http://example.com' 里的双斜杠），
#   所以必须像 _has_multiple_statements 那样逐字符跟踪引号状态，不能用正则一刀切。
# 【返回值】去掉全部行注释后的文本（换行保留）。
def _strip_line_comments(text: str) -> str:
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and quote != "`" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    out.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if text[i : i + 2] in ("//", "--"):
            # 注释内容丢弃，直到行尾；换行符本身留下（下一轮正常追加）。
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# 【这个函数在干什么】剥掉语句「末尾」的行尾注释（// 或 -- 到行尾）。
# 【为什么必须剥】补 LIMIT 是字符串拼接「… LIMIT 50」，若原句以 // 注释结尾，
#   拼上去的 LIMIT 会被注释整个吃掉（"MATCH … // 说明 LIMIT 50"），上限形同虚设。
# 【为什么只剥「末尾」的】注释后面还有代码的不能切，否则会把后面的子句一起删掉；
#   那种情况 LIMIT 拼在最后一行，本来就不会被注释吃。连续多行末尾注释循环剥干净。
def _strip_trailing_comments(cypher: str) -> str:
    text = (cypher or "").strip()
    while text:
        _, cut = _scan_cypher_text(text)
        if cut is None:
            return text
        tail = text[cut:]
        nl = tail.find("\n")
        # 注释跑完到行尾后，若后面还有非空白内容 → 不是「末尾注释」，别动。
        if nl != -1 and tail[nl + 1 :].strip():
            return text
        text = text[:cut].rstrip()
    return text


# 【这个函数在干什么】清洗模型生成的 Cypher：剥掉代码围栏、行注释、结尾分号。
# 【为什么需要清洗】大模型输出"不总是听话"：即使要求了"不要 markdown 代码块"，
#   它仍可能加上 ```cypher ... ```，或在结尾多写个分号。这些都会让执行失败，必须提前剥掉。
# 【参数逐个解释】
#   · raw —— 模型返回的原始文本。
# 【返回值】清洗后的 Cypher 文本。
def clean_cypher(raw: str) -> str:
    """剥掉代码围栏、行注释、结尾分号（大模型输出不总是听话）。"""
    # 【这行在干什么】先转成字符串（防止收到 None）并去掉首尾空白。
    text = (raw or "").strip()
    # 【这行在干什么】用正则把 markdown 代码围栏（``` 那两行）删掉，再去除首尾空白。
    # 【sub 是什么】re.sub(模式, 替换成什么, 原文) —— 把匹配到的内容替换掉；这里替换成空串=删除。
    text = _CODE_FENCE_RE.sub("", text).strip()
    # 【这行在干什么】把行注释删掉——整行的、行尾的都要（以前只删整行，行尾注释会把
    #   后面补的 " LIMIT 50" 整个吃掉）。引号里的 // 不会被误伤，见 _strip_line_comments。
    text = _strip_line_comments(text)
    # 【这行在干什么】把结尾的"分号或空白"统统删掉（防止结尾多个分号或空格）。
    # 【正则解释】[;\s]+$ 表示"一个或多个分号/空白字符，出现在结尾（$）"。
    text = re.sub(r"[;\s]+$", "", text)
    # 【这行在干什么】最后再去一次首尾空白，返回干净结果。
    return text.strip()


# 【这个函数在干什么】判断语句里是否含有"多条语句"（也就是引号/反引号**之外**出现了分号）。
# 【为什么需要（安全考虑）】用户或模型可能想用分号塞第二条语句（比如 "MATCH ...; CREATE ..."）。
#   所以只要在"字符串外面的位置"发现分号，就判为多条语句，予以拒绝。
# 【为什么要区分引号内外（关键）】因为分号可能合法地出现在字符串内容里，
#   （例如 WHERE c.name = 'A;B' 里的分号是数据的一部分，不是语句分隔符），
#   所以必须"跳过引号内部"，只检查引号外面。
# 【参数逐个解释】
#   · cypher —— 要检查的语句。
# 【返回值】True 表示含多条语句（不安全）；False 表示单条（安全）。
def _has_multiple_statements(cypher: str) -> bool:
    """判断引号/反引号外的分号（一条语句里不该出现）。"""
    # 【这三行在干什么】三个开关，分别记录"当前是不是在单引号/双引号/反引号内部"。
    in_single = False
    in_double = False
    in_backtick = False
    # 【这行在干什么】用 i 当作"当前读到第几个字符"的下标（指针）。
    i = 0
    # 【这行在干什么】循环扫描整条语句的每个字符。
    # 【len(cypher) 是什么】字符串长度；能读写到下标 0 到 长度-1。
    while i < len(cypher):
        # 【这行在干什么】取当前字符。
        ch = cypher[i]
        # 【这行在干什么】分支一：当前在单引号里。
        if in_single:
            # 【这行在干什么】如果遇到反斜杠（转义符），说明下一个字符只是普通内容，
            #   连它一起跳过（i 加 2），不做判断。
            if ch == "\\":
                i += 2
                continue
            # 【这行在干什么】如果遇到单引号，说明单引号结束，把开关关掉。
            if ch == "'":
                in_single = False
        # 【这行在干什么】分支二：当前在双引号里（处理逻辑同上，只是引号换成了双引号）。
        elif in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
        # 【这行在干什么】分支三：当前在反引号里（反引号里一般是名字，无转义处理）。
        elif in_backtick:
            if ch == "`":
                in_backtick = False
        # 【下面这几个分支】当前不在任何引号里，遇到引号就"进入"对应的引号状态。
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`":
            in_backtick = True
        # 【这行在干什么】关键分支：不在任何引号里、却遇到了分号 —— 说明这是"语句分隔符"，
        #   即出现了多条语句，直接返回 True（拒绝）。
        elif ch == ";":
            return True
        # 【这行在干什么】处理完当前字符，指针后移一位，继续看下一个。
        i += 1
    # 【这行在干什么】扫完整个句子都没在引号外发现分号 → 是单条语句，返回 False（安全）。
    return False


# 【这个函数在干什么】Cypher 的"只读护栏 + 单语句检查 + 强制 LIMIT"。
# 【核心职责】这是"安全 + 规范"三合一检查；通过后返回一条(必要时补上或修正过 LIMIT 的)语句；
#   一旦违规就抛 GraphError 拒绝执行。
# 【参数逐个解释】
#   · cypher   —— 清洗后的 Cypher；
#   · max_rows —— LIMIT 上限。
# 【返回值】规范化后的 Cypher。
def validate_cypher(cypher: str, max_rows: int) -> str:
    """只读护栏 + 单语句 + 强制 LIMIT。返回规范化后的 Cypher，违规时抛 GraphError。"""
    # 【这行在干什么】如果语句是空的（模型没生成有效内容），直接报错。
    if not cypher:
        raise GraphError("大模型没有生成可用的 Cypher")
    # 【这行在干什么】检查语句是否以只读子句开头；不是就直接拒绝。
    # 【not _START_RE.match(cypher)】match 表示"从开头匹配"；匹配失败返回 None，
    #   not None 就是 True，于是进入报错分支。
    if not _START_RE.match(cypher):
        raise GraphError("生成的语句不是只读查询（必须以 MATCH / OPTIONAL MATCH / WITH / UNWIND / RETURN 开头）")
    # 【这行在干什么】全文扫描有没有写操作关键字。
    hit = _FORBIDDEN_RE.search(cypher)
    # 【这行在干什么】如果命中了禁用词，报错并指出是哪个词。
    # 【hit.group(0) 是什么】正则捕获到的"实际匹配到的那段文字"（比如 "create"）；.upper() 转大写更好认。
    if hit:
        raise GraphError(f"生成的语句包含被禁止的关键字：{hit.group(0).upper()}")
    # 【这行在干什么】检查有没有 CALL { ... } 子查询（里面可能藏写操作），有就拒绝。
    if _CALL_SUBQUERY_RE.search(cypher):
        raise GraphError("生成的语句包含 CALL { ... } 子查询，已拒绝执行")
    # 【这行在干什么】检查是不是多条语句，是就拒绝。
    if _has_multiple_statements(cypher):
        raise GraphError("生成的语句包含多条语句，已拒绝执行")

    # 【这行在干什么】补/改 LIMIT 之前先剥行尾注释：不然 "MATCH … // 说明" 拼上
    #   " LIMIT 50"，补的上限会被注释吃掉（见 _strip_trailing_comments）。
    cypher = _strip_trailing_comments(cypher)
    # 【这行在干什么】判定用「探测视图」：常量/注释里的 limit 字样不算数
    #   （见 _scan_cypher_text），否则 WHERE n.name = 'no limit here' 会被当成
    #   「已有 LIMIT」而漏补上限，整个图被拉回来。
    probe, _ = _scan_cypher_text(cypher)
    limit_all = _LIMIT_ALL_RE.search(probe)
    fetch = _FETCH_FIRST_RE.search(probe)
    # 【LIMIT ALL】名义上写了 LIMIT、实际不限行数 → 换成安全上限（不叠加新 LIMIT）。
    if limit_all:
        cypher = f"{cypher[: limit_all.start()]}LIMIT {max_rows}{cypher[limit_all.end():]}"
    # 【FETCH FIRST n ROWS ONLY】视为「已有上限」；没写数字或数字超限就把整段换成
    #   LIMIT max_rows —— 同样不叠加，两条行数子句叠起来是语法错误。
    elif fetch:
        if fetch.group(1) is None or int(fetch.group(1)) > max_rows:
            cypher = f"{cypher[: fetch.start()]}LIMIT {max_rows}{cypher[fetch.end():]}"
    # 【这行在干什么】如果语句里压根没有 LIMIT，就给它补一个安全的 LIMIT。
    elif not _LIMIT_ANY_RE.search(probe):
        cypher = f"{cypher} LIMIT {max_rows}"
    # 【这行在干什么】否则（本来就有 LIMIT），检查它是不是太大了，大就改小。
    else:
        # 【这行在干什么】用 finditer 找出所有"LIMIT 数字"的位置和内容（可能有多个，如子查询里也有）。
        #   下标来自探测视图——它与原句等长，故可直接映射回原句改写。
        matches = list(_LIMIT_RE.finditer(probe))
        # 【这行在干什么】如果确实找到了 LIMIT 数字：
        if matches:
            # 【这行在干什么】取最后一个匹配（最外层、也就是最终生效的那个）。
            last = matches[-1]
            # 【这行在干什么】如果这个 LIMIT 的数字比允许的最大值还大，就把它替换成最大值。
            if int(last.group(1)) > max_rows:
                # 【这行在干什么】把原数字"挖掉"、换成 max_rows，其余部分原样保留。
                # 【切片逐段解释】
                #   · cypher[: last.start(1)]  —— 从头到"数字开始"之前的那一段（原样保留）；
                #   · max_rows                 —— 替换成的安全上限；
                #   · cypher[last.end(1):]     —— 从"数字结束"之后到结尾的那一段（原样保留）。
                #   三拼起来就是"只把那个数字换掉"。
                cypher = f"{cypher[: last.start(1)]}{max_rows}{cypher[last.end(1):]}"
    # 【这行在干什么】返回通过检查（并可能修正过 LIMIT）的语句。
    return cypher


# --------------------------------------------------------------------------- #
# 执行与渲染
# --------------------------------------------------------------------------- #
# 【这段注释】这是"执行查询并把结果渲染成文本"这一章。
# 【下面这个函数在干什么】把 Neo4j 返回的各种类型统一转换成"人能读的字符串"。
# 【为什么需要】Neo4j 返回的值类型五花八门（数字、布尔、列表、字典，还有它自己的日期/时长类型），
#   前端表格和给模型的文本都需要"字符串"，所以统一在这里转。
# 【参数逐个解释】
#   · value —— 待转换的值。
def _cell_text(value: Any) -> str:
    """把 Neo4j 返回的各种类型统一转成可读字符串（只依赖标准库 + 驱动自带转换）。"""
    # 【这行在干什么】空值 → 显示成 NULL。
    if value is None:
        return "NULL"
    # 【这行在干什么】布尔值 → 显示成 true/false（而不是 Python 的 True/False，更贴近数据库习惯）。
    # 【注意顺序（重要）】必须在"是不是数字"之前判断布尔，因为 Python 里布尔其实是整数的子类
    #   （True == 1），如果先判数字，True 会被显示成 1，容易误导。
    if isinstance(value, bool):
        return "true" if value else "false"
    # 【这行在干什么】本来就是字符串 → 原样返回。
    if isinstance(value, str):
        return value
    # 【这行在干什么】数字（整数或小数）→ 转成字符串。
    if isinstance(value, (int, float)):
        return str(value)
    # 【这行在干什么】列表或元组 → 递归地把每一项转成字符串，再用逗号拼进方括号里，形如 [a, b]。
    # 【递归是什么】函数自己调用自己；这里对列表里每一项再调一次 _cell_text，无论嵌套多深都能处理。
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_cell_text(v) for v in value) + "]"
    # 【这行在干什么】字典 → 转成 JSON 文本，失败则退化成普通字符串。
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    # 日期 / 时间 / Duration / Point 等驱动类型：直接 str()
    # 【这行在干什么】以上都不是（比如 Neo4j 的日期/时间/航点等类型）→ 用 str() 兜底转换。
    # 【为什么兜底成 str 就够了】这些类型都实现了自己的字符串表示，str() 出来的结果可读。
    return str(value)


# 【这个函数在干什么】把文本截断到指定长度（超长就切掉后面，加个省略提示）。
# 【参数逐个解释】
#   · text  —— 要处理的文本；
#   · limit —— 最大长度，默认用文件顶部定义的 _CELL_TEXT_LIMIT（=300）。
# 【默认参数是什么】函数定义时给参数预设的值；调用时不传就自动用它（这里 limit 不传就是 300）。
def _truncate(text: str, limit: int = _CELL_TEXT_LIMIT) -> str:
    # 【这行在干什么】三目表达式：没超长就原样返回；超长就取前 limit 个字符 + "…（已截断）"提示。
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


# 【这个函数在干什么】在只读事务里执行查询，返回行列表（并做行数限制）。
# 【参数逐个解释】
#   · s      —— 配置；
#   · cypher —— 已通过护栏检查的只读语句。
def run_query(s: Settings, cypher: str) -> list[dict[str, Any]]:
    """在只读事务中执行查询，返回行列表（每行为 列名→值 的字典）。"""
    # 【这行在干什么】调用 _read_rows 真正执行（走只读事务，第 1 层安全边界）。
    rows = _read_rows(s, cypher)
    # 【这行在干什么】按配置再"兜底截一刀"：最多返回 neo4j_max_rows 行。
    # 【为什么已经强制 LIMIT 了还要再截】双保险：万一护栏有漏网之鱼（比如 LIMIT 写在别处没被识别），
    #   这里再截一刀，确保不会把海量数据拉回内存。max(1, ...) 保证至少 1 行。
    return rows[: max(1, s.neo4j_max_rows)]


# 【这个函数在干什么】把查询结果渲染成给模型看的文本（模型据此作答）。
# 【参数逐个解释】
#   · cypher —— 实际执行的语句（一并展示，便于排查/前端折叠展示）；
#   · rows   —— 查询结果行。
# 【返回值】一段多行文本。
def render_result(cypher: str, rows: list[dict[str, Any]]) -> str:
    """把查询结果渲染成给模型看的文本。"""
    # 【这行在干什么】先写好"表头"：标出这是图谱查询结果，并把执行的语句也写上。
    header = f"【图谱查询结果】\nCypher：{cypher}"
    # 【这行在干什么】如果没有查到任何行，返回"查到了但没结果"的说明。
    if not rows:
        return (
            header
            # 【这行在干什么】说明查询本身是成功的，只是没匹配到记录（区分"失败"和"没结果"很重要）。
            + "\n查询成功，但返回 0 条记录。\n"
            # 【这行在干什么】明确要求模型："没有就直说没有，不许编造实体或关系。"
            # 【为什么要写这句】大模型有"必须给个答案"的倾向，不明确禁止就容易瞎编；
            #   这句就是用来压制这种编造冲动的。
            "请直接告诉用户图谱中没有查到相关记录，不要编造任何实体或关系。"
        )

    # 【这行在干什么】有结果时：写表头 + "共 N 条"。
    lines = [header, f"共 {len(rows)} 条："]
    # 【这行在干什么】逐行渲染结果，并在前面加上序号（从 1 开始）。
    for index, row in enumerate(rows, start=1):
        # 【这行在干什么】把这一行的"列名=值"逐对拼成字符串，再接到一行里。
        # 【逐段解释】
        #   · row.items()            —— 把字典变成"键值对"逐个拿出来；
        #   · _cell_text(val)        —— 把值转成可读字符串；
        #   · _truncate(...)         —— 太长就截断；
        #   · f"{key}={...}"         —— 拼成 列名=值；
        #   · ", ".join(...)         —— 用逗号把同一行的所有列拼起来。
        pairs = ", ".join(f"{key}={_truncate(_cell_text(val))}" for key, val in row.items())
        # 【这行在干什么】输出形如 "1) 公司=..., 项目=..." 的一行。
        lines.append(f"{index}) {pairs}")
    # 【这行在干什么】把所有行缝成一段文本返回。
    return "\n".join(lines)


# 【这个函数在干什么】生成"查询失败"时给模型看的文本。
# 【参数逐个解释】
#   · reason —— 失败原因（一段人话）。
# 【为什么要统一成"文本"而不是抛异常】这样模型能看到失败提示，从而诚实告知用户"没查到"，
#   而不是因为收到异常而中断整轮对话。
def _failure_text(reason: str) -> str:
    return (
        "【图谱查询结果】\n"
        # 写清失败原因
        f"查询失败：{reason}\n\n"
        # 要求模型如实说明"这次没取到结果（可以附上原因）"，并强调不要编造数据
        "请如实向用户说明知识图谱这次查询没有取到结果（可附上失败原因），不要编造数据。"
    )


# --------------------------------------------------------------------------- #
# 工具主体
# --------------------------------------------------------------------------- #
# 【这段注释】这是"真正的工具主逻辑"这一章。
# 【下面这个函数在干什么】把整条流水线串起来（同步执行）：
#   读结构 → 让模型生成 Cypher → 护栏校验 → 执行 → 渲染结果文本。
# 【为什么标注"同步阻塞段"】它内部都是同步调用（等模型、等数据库），跑起来会"独占"一会儿；
#   所以由外层工具用 asyncio.to_thread 把它丢到旁支线程去跑，避免卡住服务（见文件末尾）。
# 【参数逐个解释】
#   · question —— 用户问题；
#   · state    —— 一个字典，用来"回传执行细节"给上层（Cypher、行数、行内容、错误）。
def _run_graph(question: str, state: dict[str, Any]) -> str:
    """同步阻塞段：读 schema → 生成 Cypher → 校验 → 执行 → 渲染。

    由工具侧用 ``asyncio.to_thread`` 丢到工作线程执行，避免阻塞事件循环。
    """
    # 【这行在干什么】从状态字典里取出配置对象，并标注类型是 Settings。
    # 【为什么用 .get() 而不是 state["settings"]】后者在 state 没带 settings 时抛 KeyError，
    #   而这个 KeyError 发生在 try 之外，会穿透「工具只返回可读提示、不向上抛异常」的承诺。
    s: Settings | None = state.get("settings")
    # 【这行在干什么】先声明 cypher 变量并置空（后面成功时会赋值；失败时它可能仍是 None，
    #   好让上层知道"压根没生成出语句"）。
    cypher: str | None = None
    # 【下面这段在干什么】整条流水线，任何一步出问题都转成"可读失败提示"。
    try:
        # 【这行在干什么】配置缺失当「预期内」错误处理，与其它失败走同一条可读提示通道。
        if s is None:
            raise GraphError("工具未注入配置（state 缺少 settings），已跳过执行")
        # 【这行在干什么】读图谱结构（带 TTL 缓存），并渲染成给模型看的文本。
        schema_text = render_schema_text(load_schema(s))
        # 【这行在干什么】让模型把问题翻译成 Cypher。
        # 【参数逐个解释】
        #   · build_cypher_messages(...) —— 拼好的提示词（含结构与问题）；
        #   · settings=s                 —— 配置；
        #   · model=s.graph_model_or_default —— 用哪个模型（图谱这一路可以单独配模型）；
        #   · temperature=0.0            —— 温度 0，翻译要稳定准确、不要发挥；
        #   · max_tokens=s.graph_max_tokens —— 最多生成多少 token；
        #   · timeout=s.graph_timeout    —— 超时（秒）。
        raw = complete_chat(
            build_cypher_messages(question, schema_text, s.neo4j_max_rows),
            settings=s,
            model=s.graph_model_or_default,
            temperature=0.0,
            max_tokens=s.graph_max_tokens,
            timeout=s.graph_timeout,
        )
        # 【这行在干什么】先清洗模型的输出，再送进护栏校验（可能补/改 LIMIT），得到最终语句。
        # 【为什么"清洗 → 校验"这个顺序不能反】因为校验的正则（比如"必须以 MATCH 开头"）
        #   要求文本已经干净；若模型带了 ``` 围栏，不清洗就会被误判成"不是只读查询"。
        cypher = validate_cypher(clean_cypher(raw), s.neo4j_max_rows)
        # 【这行在干什么】执行语句、拿到结果行。
        rows = run_query(s, cypher)

        # 【这行在干什么】把最终语句回传到状态里（前端要展示可折叠的 Cypher）。
        state["cypher"] = cypher
        # 【这行在干什么】把结果行数回传。
        state["row_count"] = len(rows)
        # 【这行在干什么】把前若干行结果也回传（仅用于前端画表格，所以只取 _ROWS_TO_STATE 行）。
        # 【逐段解释】
        #   · rows[:_ROWS_TO_STATE]                          —— 只取前 20 行；
        #   · {key: _truncate(_cell_text(val)) ...}          —— 每个值都转字符串并截断；
        #   · for row in ...                                 —— 逐行处理（字典推导式 + 循环）。
        state["rows"] = [
            {key: _truncate(_cell_text(val)) for key, val in row.items()} for row in rows[:_ROWS_TO_STATE]
        ]
        # 【这行在干什么】没有错误，把 error 置为 None（表示成功）。
        state["error"] = None
        # 【这行在干什么】把结果渲染成给模型看的文本，并作为本次调用的返回值。
        return render_result(cypher, rows)
    # 【下面这段在干什么】捕获我们"预期内"的三类错误：GraphError（图谱问题）、
    #   LLMError（模型问题）、LLMNotConfigured（模型没配），把它们的原因记下来。
    except (GraphError, LLMError, LLMNotConfigured) as exc:
        reason = str(exc)
    # 【下面这段在干什么】其它任何意外错误，也转成"错误类型名 + 描述"的可读原因。
    # 【为什么能允许这么宽】工具对外的承诺就是"绝不抛异常，只返回可读提示"，所以全接住。
    except Exception as exc:  # noqa: BLE001 - 工具只返回可读提示，不向上抛
        reason = f"{type(exc).__name__}: {exc}"

    # 【这行在干什么】失败时把（可能为 None 的）语句写回状态，供前端展示。
    state["cypher"] = cypher
    # 【这行在干什么】行数为 0（失败嘛）。
    state["row_count"] = 0
    # 【这行在干什么】结果行留空。
    state["rows"] = []
    # 【这行在干什么】把失败原因写回状态（非空即表示这次查询失败）。
    state["error"] = reason
    # 【这行在干什么】返回"失败提示文本"，供模型如实作答。
    return _failure_text(reason)


# 【这行在干什么】用 @tool 装饰器把下面这个函数包装成"大模型可调用的工具"。
# 【为什么需要】装饰器会给函数挂上描述、参数说明等元信息，模型才能识别并调用它。
# 【注意最要紧的一点】下面函数的 docstring 会被原样送给模型当"工具说明书"，
#   所以那段文字写得好不好，直接决定模型"知不知道什么时候该用这个工具"。
@tool
# 【这行在干什么】定义工具函数。它是 async 的（异步），也是模型真正"调用"的那个入口。
# 【参数逐个解释】
#   · question —— 模型从用户问题里提炼出来的"查询意图"（由模型自己填）；
#   · config   —— LangChain 自动传进来的"随身包"，本项目把状态字典挂在里面。
# 【返回值】一段文本（查询结果或失败提示），会作为"工具消息"回给模型。
async def query_bidding_graph(question: str, config: RunnableConfig) -> str:
    """查询 Neo4j 知识图谱中的实体关系，用于关联、多跳、路径与关系计数类问题。

    适用于「某公司中标了哪些项目」「某项目有哪些参与单位」「两家公司共同参与过
    哪些项目」「某类关系的数量有多少」这类需要沿关系遍历的问题。
    若问题是询问公告原文、条款措辞、资质描述或统计求和，请改用
    search_bidding_knowledge 检索知识库或 query_bidding_postgres 精确查询。
    """
    # 【这行在干什么】从"随身包"里取出本请求专用的状态字典 _graph_state。
    # 【逐段解释】
    #   · config.get("configurable")  —— 取出随身包里"可配置项"那一层；
    #   · or {}                       —— 万一那层是空的（None），用空字典兜底，防止取属性报错；
    #   · .get("_graph_state")        —— 从里面取我们自己挂的状态字典。
    state = (config.get("configurable") or {}).get("_graph_state")
    # 【这行在干什么】如果没取到状态（理论上不会发生，因为 agent 一定会传），就兜底造一个。
    if state is None:
        # 理论上不会发生（agent_events 一定会传）；兜底成一次无状态查询
        # 【这行在干什么】造一个只含"配置"的最小状态字典，保证后面代码能跑通。
        state = {"settings": get_settings()}
    # 【这行在干什么】把同步阻塞的 _run_graph 丢到旁支线程执行，并等待它完成。
    # 【为什么必须这么做（关键）】_run_graph 是同步的（要等模型、等数据库），
    #   如果直接在异步函数里调用，会把整个服务的事件循环"卡住"——所有其他请求都得排队等。
    #   用 asyncio.to_thread 把它挪到另一个线程去跑，事件循环就能腾出手来处理别的请求。
    # 【await 是什么】"等它完成"——但等的过程中，事件循环可以去做别的事，不像同步等待那样干耗。
    return await asyncio.to_thread(_run_graph, question, state)