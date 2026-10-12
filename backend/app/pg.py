# ============================================================================ #
# 本文件在干什么
# ---------------------------------------------------------------------------- #
# pg.py 是「PostgreSQL 结构化查询工具」。它让 Agent 能回答那些「用相似度算不出来」
# 的问题，比如：
#   「有多少个预算超过 100 万的项目？」（计数）
#   「各地区的项目金额合计是多少？」（分组求和）
#   「金额最高的前 5 个项目是哪些？」（排序 Top-N）
# 做法是：先读出数据库有哪些表、哪些字段（schema），把这套结构交给大模型，
# 让大模型把用户的自然语言问题「翻译」成一条只读 SQL，再拿回来校验、执行，
# 把结果渲染成文字给大模型看，最后大模型据此作答。
#
# ---------------------------------------------------------------------------- #
# 上游谁调用它
# ---------------------------------------------------------------------------- #
# - agent.py / deep_agent.py 在装配 Agent 时会导入本文件里的 @tool 工具函数
#   query_bidding_postgres，把它注册成 Agent 可以「自动调用」的一个工具。
#   当用户的问题属于「统计/计数/求和/排序/精确筛选」这一类时，Agent 会自己判断
#   并调用它（这一步是模型自己决定的，不需要人写 if-else）。
# - 事件层（agent_events）会读取本文件导出的常量与工具名，用于把工具调用过程
#   （生成了什么 SQL、查到了几行）推给前端展示。
#
# ---------------------------------------------------------------------------- #
# 下游给谁用
# ---------------------------------------------------------------------------- #
# - 结果文字（render_result / _failure_text 的返回值）交回给大模型，
#   大模型再据此组织成给用户的最终回答。
# - 结构化结果（state 字典里的 sql / columns / rows / row_count）交给事件层，
#   前端用可折叠面板展示 SQL 和结果表格。
#
# ---------------------------------------------------------------------------- #
# 概念预习（用大白话讲清楚，后面代码里遇到就不会懵）
# ---------------------------------------------------------------------------- #
# 【schema】= 数据库的「目录/说明书」：有哪些表、每张表有哪些列、列是什么类型、
#   列上有没有备注。大模型要写 SQL 必须先知道这些，否则会瞎编字段名。
# 【只读（read-only）】= 只能看，不能改。我们让它只能 SELECT（查），禁止
#   INSERT/UPDATE/DELETE（增删改），防止把生产数据改坏。
# 【with 语句】= Python 的「用完自动收拾」语法。`with conn.cursor() as cur:`
#   意思是拿到一个游标（执行 SQL 的小工具），离开缩进块后自动关闭，不用手写 close。
# 【游标 cursor】= 你可以理解成「执行 SQL、接住结果的把手」。
# 【正则（re）】= 按「模式」匹配文本的工具。例如 \blimit\s+(\d+) 用来找 SQL 里
#   有没有写 ``LIMIT 数字``。r"..." 表示原始字符串，反斜杠不被转义。
# 【单词边界 \b】= 正则里表示「单词的开头或结尾」，避免把 select 匹配到 selected
#   这种词里，也避免把 set 匹配到 offset 里。
# 【元组解包】= 一行里同时给多个变量赋值，例如 a, b = (1, 2) 就是一次性拆开。
# 【f-string】= 以 f 开头的字符串，里面 {变量} 会被替换成变量值，方便拼文本。
# 【async / await】= 异步语法。遇到「要等很久」的操作（如网络、数据库）时不干等，
#   先去做别的，等结果好了再回来。本文件的工具主体是 async，但真正干活的 _run_pg
#   是普通同步函数，用 asyncio.to_thread 丢到工作线程跑（避免卡住主流程）。
#
# ---------------------------------------------------------------------------- #
# 只读保护三层（务必理解各自的定位，改代码时别拆错）
# ---------------------------------------------------------------------------- #
# 第 1 层 · 服务端强约束（真正的安全边界）：
#   建连接时传 options="-c default_transaction_read_only=on ..."。
#   这是把「只读」开关交给 PostgreSQL 自己执行——哪怕我们的代码出了漏洞，
#   数据库也会直接报错拒绝写操作。这是唯一「靠得住」的一层。
# 第 2 层 · 静态关键字护栏（不是安全边界，只是快速拦截）：
#   用正则扫一遍 SQL，命中 insert/update/drop 等关键字就拒绝；并要求语句必须以
#   SELECT 或 WITH 开头。它只能挡住「明显的越界输入」，不能当成安全保证。
# 第 3 层 · 单语句 + 强制 LIMIT：
#   1) 拒绝一次执行多条语句（用分号分隔的多条语句）；
#   2) 若没有 LIMIT 就补上，若已有且超过上限就改小，避免把整张大表拉回来。
#
# ---------------------------------------------------------------------------- #
# 硬约束 & 实测结论（改参数前先看这里）
# ---------------------------------------------------------------------------- #
# - 连接信息（host/port/user/password/database）全部来自 .env，绝不硬编码在代码里。
# - PostgreSQL 未配置完整时（比如 PG_USER/PG_PASSWORD 为空、服务没在监听），
#   本工具根本不会注册——这是项目约定，见 agent 装配处。
# - 工具失败时一律返回「可读的失败提示」，绝不向上抛异常。这样大模型会诚实地
#   告诉用户「没查到」，而不是凭空编造数据；这也保证了「空库也能跑」。
#
# ---------------------------------------------------------------------------- #
# 阅读顺序指南（第一次读建议按这个顺序）
# ---------------------------------------------------------------------------- #
#   1. 顶部常量与正则（PG_TOOL_NAME / _FORBIDDEN_RE ...）—— 先知道有哪些「规则」；
#   2. 连接 _connect —— 安全边界在这里；
#   3. schema 读取 load_schema / render_schema_text —— 给模型看的「说明书」；
#   4. 自然语言→SQL：build_sql_messages / clean_sql / validate_sql —— 核心加工线；
#   5. 执行 run_query / render_result —— 真正跑 SQL、渲染结果；
#   6. 工具主体 _run_pg / query_bidding_postgres —— 把上面串起来。
# ============================================================================ #
"""PostgreSQL 结构化查询工具（自然语言 → 只读 SQL）。

与 ``retriever.py`` 的向量检索是**并行**的两条路径：

- 混合检索擅长「语义相似的原文」（公告内容、条款措辞、资质描述）；
- 本模块擅长「跨表统计、计数、求和/平均、排序 Top-N、精确条件筛选」——
  这些用相似度算不出来（相似度不是计数，文本块里也没有聚合结果）。

只读保护分三层，务必理解各自的定位：

1. **服务端强约束（真正的安全边界）**：连接时带
   ``options="-c default_transaction_read_only=on -c statement_timeout=<ms>"``，
   PostgreSQL 会直接拒绝任何写操作（``cannot execute INSERT in a read-only transaction``），
   并限制慢查询时长。**建议再为应用单独建一个只有 SELECT 权限的账号。**
2. **静态关键字护栏（不是安全边界）**：命中写操作关键字即拒绝，且要求语句以
   ``SELECT`` / ``WITH`` 起头。它只为把明显越界的输入挡在发请求之前。
3. **单语句 + 强制 LIMIT**：防止一次执行多条语句、防止大表全量拉回。

工具失败时一律返回「可读的失败提示」而不是抛异常，让 Agent 据此诚实作答、不编造。
"""

# 让「类型标注」延迟求值（Python 3.7+ 的写法）。
# 好处：即使某个类型名在当前 Python 版本里还不存在，只要写在标注位置也不报错，
# 方便前后版本兼容。大白话：类型说明先「挂着」，用到时再算。
from __future__ import annotations

# ---------------------------------------------------------------- #
# 标准库导入区（Python 自带，不用 pip 安装）
# ---------------------------------------------------------------- #
# 异步工具箱：后面用 asyncio.to_thread 把同步的数据库/模型调用挪到后台线程。
import asyncio
# 处理「日期/时间」类型，用于把查询结果里的时间列转成可读文本。
import datetime
# 处理高精度小数（数据库里的金额常返回 Decimal，而不是会丢精度的 float）。
import decimal
# 处理 JSON：把列表/字典类型的列序列化成字符串。
import json
# 正则表达式：用来做关键字护栏、识别 LIMIT、剥代码围栏。
import re
# 时间工具：给 schema 缓存做「有效期」判断。
import time
# 生成/识别 UUID：把 uuid 类型的列转成文本。
import uuid
# 只用于类型标注的 Any（表示「任意类型」），方便新手少纠结具体类型。
from typing import Any

# ---------------------------------------------------------------- #
# 第三方库导入区（需要 pip 安装）
# ---------------------------------------------------------------- #
# RunnableConfig 是 LangChain 里「工具/链」通用的配置载体，工具靠它拿到事件层
# 塞进来的状态（例如 settings、以及用于回填结果的 state 字典）。
from langchain_core.runnables import RunnableConfig
# @tool 装饰器：把一个普通函数「注册」成大模型可调用的工具。
from langchain_core.tools import tool

# ---------------------------------------------------------------- #
# 本项目内部导入区（同目录下的其它模块）
# ---------------------------------------------------------------- #
# Settings/get_settings：读取 .env 配置（连接信息绝不硬编码，全部从这来）。
from .config import Settings, get_settings
# LLM 相关：LLMError/LLMNotConfigured 是我们自己定义的错误类型；
# complete_chat 是「把消息发给大模型、拿回一段文本」的统一入口。
from .llm import LLMError, LLMNotConfigured, complete_chat

# 工具名（函数名即注册给模型的名字，导出常量供 event 层引用）
# 大模型「认」的是函数名 query_bidding_postgres，所以这里把名字单独抽成常量导出，
# 让事件层/前端引用同一份名字，不容易写错。
# 注意事项：一旦改动这里，Agent 提示词里提到的工具名也要同步改，否则模型会「找不到工具」。
PG_TOOL_NAME = "query_bidding_postgres"

# 单次查询结果渲染时，单元格文本的截断长度
# 为什么限制：数据库里可能有很长的文本列，全塞给模型既费 token 又没必要。
_CELL_TEXT_LIMIT = 300

# 回传给前端的结果行数上限（前端只展示不参与作答）
# 注意：这只是给前端「结果表格」展示用的，不参与大模型作答——模型看到的是完整结果文字。
_ROWS_TO_STATE = 20

# 写操作关键字护栏（大小写不敏感、按单词边界匹配）
# 命中任意一个就拒绝执行。为什么宁可多禁：这层只是「快速拦截」，误伤（拒绝掉
# 某些合法查询）总比放行一条写操作安全；真正的安全靠第 1 层「服务端只读」。
# 分组说明（便于维护时理解为什么禁它）：
#   - 增删改结构：insert / update / delete / drop / alter / create / truncate
#   - 权限与复制：grant / revoke / copy
#   - 执行与维护：call / do / vacuum / analyze / reindex / cluster / refresh /
#     merge / import / execute / prepare / discard
#   - 会话与事务：listen / notify / savepoint / comment / security / reassign /
#     detach / attach / lock / set / commit / rollback / begin
_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "call", "do", "vacuum", "analyze", "reindex",
    "cluster", "refresh", "merge", "import", "execute", "prepare", "discard",
    "listen", "notify", "savepoint", "comment", "security", "reassign",
    "detach", "attach", "lock", "set", "commit", "rollback", "begin",
)
# 把上面的黑名单「拼」成一条正则并编译（编译一次，之后重复用更快）。
# 生成的正则长这样：\b(insert|update|delete|...)\b
#   \b  = 单词边界；(...) = 捕获分组；(a|b) = a 或 b；
#   re.IGNORECASE = 忽略大小写（INSERT/insert 都能匹配）。
# 大白话：只要 SQL 里出现任意一个黑名单单词，就会被 search 找到。
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)
# 判断语句「开头」是不是只读查询：^ 表示行首，\s* 允许前面有空格，
# (select|with) 表示以 select 或 with 起头。
# 为什么允许 WITH：WITH ... AS (...) SELECT ... 是「公共表表达式」，仍是只读查询。
_START_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# 用来提取「LIMIT 后面跟的数字」：\blimit\s+(\d+) 里的 (\d+) 会捕获数字，
# 方便把过大的 LIMIT 改小。
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)
# 只判断「有没有出现 LIMIT」（不管具体数字），用于决定要不要补 LIMIT。
_LIMIT_ANY_RE = re.compile(r"\blimit\b", re.IGNORECASE)
# LIMIT ALL：PostgreSQL 的「不限行数」写法——名义上写了 LIMIT，实际把整张表拉回来，
# 必须识别出来替换成安全上限，不能当成「已有上限」放行。
_LIMIT_ALL_RE = re.compile(r"\blimit\s+all\b", re.IGNORECASE)
# FETCH FIRST n ROWS ONLY：SQL 标准的行数上限写法（等价于 LIMIT n）。
# 里的 (\d+) 捕获可选数字：写了 n 就核对数字，没写数字也要认出这是「行数子句」，
# 不能在它屁股后面再拼一个 LIMIT（两条行数子句叠加是语法错误）。
_FETCH_FIRST_RE = re.compile(r"\bfetch\s+(?:first|next)\s+(?:(\d+)\s+)?rows?\s+only\b", re.IGNORECASE)
# 用来剥掉大模型爱写的 markdown 代码围栏 ```sql ... ```。
# 它匹配「开头的 ``` 加可选语言名」或「结尾的 ```」。
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

# schema 缓存：键为连接标识，值为 (取到的时间戳, 结构字典)
# 为什么要缓存：读表结构本身也要连数据库，每次提问都重读太浪费。
# 键(key)：一串能唯一标识「连的是哪个库、哪个 schema、哪些表」的信息；
# 值(value)：(取到缓存的时间戳, 结构字典)。
# 大白话：像给「说明书」拍张照存起来，60 秒内（由 pg_schema_ttl 决定）直接用，
# 过期了再重拍，省时间。
# 潜在风险：如果缓存期间数据库改了表结构，短时间内可能看到旧结构；可等过期，
# 或调用 load_schema(refresh=True) 强制重取。
_schema_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


# 自定义异常类型，用来表示「PostgreSQL 这一路出的、已经整理成人话的错误」。
# 继承 RuntimeError（运行时错误）。
# 为什么自定义：工具主体捕获它后，会把 str(exc) 直接当作「失败原因」展示，
# 所以抛出时的文字必须是「人话」。
# 潜在风险：别把敏感信息（如密码）写进报错文本。
class PgError(RuntimeError):
    """PostgreSQL 查询的可读错误（连不上 / 生成失败 / 被护栏拒绝）。"""


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
# _connect：新建一个到 PostgreSQL 的连接。
# 参数逐个解释：
#   s —— Settings 配置对象，里面装着 host/port/user/password/database 等
#        （全部来自 .env，不硬编码）。
#   readonly —— 是否以「只读」方式连接，默认 True；只读时数据库会拒绝任何写操作。
# 注意事项：函数名前的下划线 _ 表示「模块内部使用，外部别依赖」。
# 返回值：一个 psycopg2 连接对象；调用方用完必须自己 close()。
def _connect(s: Settings, readonly: bool = True):
    """新建一个连接（调用方负责关闭）。

    psycopg2 的连接不能跨线程安全共享，而工具跑在 ``asyncio.to_thread``
    的工作线程里、线程并不固定，因此每次调用都新建连接、用完即关。
    """
    # 延迟导入：把 import 放在函数内部，只有真正要连库时才导入。
    # 好处：即使没装 psycopg2，只要不调用本函数，模块也能正常导入（环境更宽松）。
    try:
        import psycopg2
    # 若没装这个库，抛出一个「人话」错误，并告诉用户怎么装。
    # from exc 是「异常链」：保留原始错误信息，方便排查（不是吞掉）。
    except ImportError as exc:  # pragma: no cover
        raise PgError("缺少依赖 psycopg2，请执行 pip install psycopg2-binary") from exc

    # 组装连接参数 options：
    # statement_timeout 让数据库自动掐断「跑太久」的查询（单位毫秒，所以乘 1000）。
    # max(1, ...) 是保险：防止配置成 0 或负数导致非法参数。
    options = f"-c statement_timeout={max(1, s.pg_statement_timeout) * 1000}"
    # 第 1 层保护：服务端强制只读（真正的安全边界）。
    # default_transaction_read_only=on 会让数据库自身拒绝所有写操作——
    # 不依赖我们的代码是否写对，所以这一层最靠得住。
    if readonly:
        # 第 1 层保护：服务端强制只读，任何写操作都会被数据库直接拒绝
        options = f"-c default_transaction_read_only=on {options}"

    try:
        # 真正建立连接。所有连接信息都取自配置 s（即 .env），不硬编码。
        #   connect_timeout：连不上时最多等多久（秒）；
        #   application_name：给连接起个名，方便在数据库侧看到是谁连的。
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
    # 把各种底层异常统一转成 PgError（附上「连哪台机器哪个库」和原始原因）。
    # 注意：f-string 里带了 host/port/dbname 但不含密码，避免泄露凭据。
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        raise PgError(
            f"连接 PostgreSQL 失败（{s.pg_host}:{s.pg_port}/{s.pg_database}）：{exc}"
        ) from exc


# _cell_text：把数据库返回的「任意类型」的值，统一转成能显示的字符串。
# 为什么需要它：不同列可能返回 None / bool / 小数 / 时间 / UUID / 二进制 / 列表等，
# 直接拼字符串可能报错或显示成很难读的形式。
# 返回值：一定是 str。注意 None 显示为 "NULL"（而不是空），便于模型理解「这格是空的」。
def _cell_text(value: Any) -> str:
    """把 PostgreSQL 返回的各种类型统一转成可读字符串（只依赖标准库）。"""
    # 空值 → 明确写成 NULL，让模型知道这是「数据库里没有值」。
    if value is None:
        return "NULL"
    # 布尔值 → true/false。
    # 注意顺序：bool 要先于数字判断，否则 True 会被当成 1（Python 里 True == 1）。
    if isinstance(value, bool):
        return "true" if value else "false"
    # 高精度小数（金额常见）→ 直接转字符串，保留精度、不丢位数。
    if isinstance(value, decimal.Decimal):
        return str(value)
    # 日期/时间：datetime 用 sep=" " 让日期和时间之间是空格；date/time 直接 iso 格式。
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime.datetime) else value.isoformat()
    # UUID → 文本。
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    # 二进制（图片/附件等）→ 只显示「多少字节」，不把原始字节塞进文本。
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    # 列表/字典（如 JSON 列）→ 转成 JSON 文本；ensure_ascii=False 保留中文不转义。
    # default=str 兜底：遇到 json 不认识的对象就按 str 处理，避免直接抛错。
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    # 其它一切类型 → 用 str() 兜底。
    return str(value)


# _truncate：把过长的文本截断，并加「（已截断）」提示。
# 参数：text 原文；limit 上限（默认用全局 _CELL_TEXT_LIMIT=300）。
# 大白话：像把长微博「展开全文」前先折断，后面用省略号提示还有内容。
def _truncate(text: str, limit: int = _CELL_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


# --------------------------------------------------------------------------- #
# schema 自动读取
# --------------------------------------------------------------------------- #
# _read_schema_live：真正去数据库「现场」读一遍结构信息。
# 读的内容包括：表名清单、每张表的列（名字/类型/是否可空/备注）、近似行数，
# 以及每张表前 2 行样例数据（给模型「看一看真实数据长什么样」）。
# 为什么读样例：模型看到真实值（比如地区列里是"北京"而不是"Beijing"）更容易写对
# SQL 里的筛选条件。
# 注意事项：本函数每次都新建连接并关闭；缓存由调用方 load_schema 负责。
def _read_schema_live(s: Settings) -> dict[str, Any]:
    """从 information_schema / pg_catalog 读取表、字段、注释、近似行数与样例值。"""
    # 新建一个连接；读结构只需要只读权限即可。
    conn = _connect(s)
    # try/finally 保证：无论中途成功还是报错，最后都会执行 finally 里的 conn.close()。
    try:
        # autocommit=True 表示「每条语句自动提交」，不用手动 commit。
        # 对只读查询来说这样最省事，避免连接一直挂着未提交事务。
        conn.autocommit = True
        # with 拿到游标，离开缩进块自动关闭。
        with conn.cursor() as cur:
            # 查询「这个 schema 下有哪些基础表（BASE TABLE）」。
            # %s 是参数占位符（防注入的正确写法），实际值由后面的元组传入。
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """,
                (s.pg_schema,),
            )
            # 把结果整理成「表名列表」。fetchall() 取出所有行，每行是元组，取第 0 列。
            names = [row[0] for row in cur.fetchall()]
            # 如果配置里指定了只关心某些表（pg_tables 非空），就按它过滤。
            if s.pg_tables:
                names = [n for n in names if n in s.pg_tables]

            # 一张表都没有：直接返回「空结构」，让上层知道无表可查。
            # 这也是「支持空数据/空库也能跑」的一种表现：不报错，返回空即可。
            if not names:
                return {"database": s.pg_database, "schema": s.pg_schema, "tables": []}

            # 查询所有字段的详细信息，并「左连接」出字段备注（注释）。
            # 说明：字段备注存在 pg_description 里，需要通过 pg_statio_all_tables
            # 找到表的内部编号(relid)，再用「列序号」定位到具体列的备注。
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
            # 准备一个字典：键=表名，值=该表的列信息列表。
            columns: dict[str, list[dict[str, Any]]] = {}
            # 逐行遍历查询结果（这里用了「元组解包」：一行 6 个值一次性分给 6 个变量）。
            for table_name, col_name, data_type, nullable, position, comment in cur.fetchall():
                # 只保留我们关心的表（前面的 names 已过滤过）。
                if table_name not in names:
                    continue
                # setdefault(表名, []) 的作用：若该表还没出现过就新建空列表，
                # 然后把当前列的信息 append 进去。
                columns.setdefault(table_name, []).append(
                    {
                        "name": col_name,
                        "type": data_type,
                        # is_nullable 是字符串 "YES"/"NO"，转成布尔值更直观。
                        "nullable": nullable == "YES",
                        # comment 可能是 None，统一成空字符串。
                        "comment": comment or "",
                        "position": int(position),
                    }
                )

            # 读「近似行数」：reltuples 是数据库维护的统计估算值，读起来很快，
            # 不像 count(*) 那样要真正扫表（大表上 count(*) 可能很慢）。
            cur.execute(
                """
                SELECT c.relname, c.reltuples::bigint
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind = 'r'
                """,
                (s.pg_schema,),
            )
            # 整理成 {表名: 行数} 的字典；::bigint 让数据库直接返回整数。
            approx_rows = {row[0]: int(row[1]) for row in cur.fetchall()}

        # 开始逐表收集「样例数据」，组装最终的 tables 列表。
        tables: list[dict[str, Any]] = []
        for name in names:
            sample_columns: list[str] = []
            samples: list[dict[str, str]] = []
            try:
                with conn.cursor() as cur:
                    # 取前 2 行样例。表名/schema 用 _quote_ident 加双引号，防止
                    # 大小写或特殊字符导致语法错误。
                    # 注意：这里是拼字符串，但表名来自数据库自身元数据、不是用户输入，
                    # 所以风险可控。
                    cur.execute(f'SELECT * FROM {_quote_ident(s.pg_schema)}.{_quote_ident(name)} LIMIT 2')
                    # cur.description 是「结果集的列信息」，取每列的列名。
                    sample_columns = [d[0] for d in (cur.description or [])]
                    # 把每一行组装成 {列名: 值文本} 的字典；zip 把列名和值一一配对。
                    for row in cur.fetchall():
                        samples.append({col: _truncate(_cell_text(v)) for col, v in zip(sample_columns, row)})
            # 样例只是「锦上添花」：取不到（比如没权限）也不影响主流程，吞掉异常即可。
            except Exception:  # noqa: BLE001 - 样例只是辅助信息，取不到不影响主流程
                samples = []

            # 行数取不到时 approx_rows 里没有这个键，get 返回 -1。
            rows = approx_rows.get(name, -1)
            tables.append(
                {
                    "name": name,
                    # 只有 >=0 才当作有效行数，否则写 None（表示未知）。
                    "approx_rows": rows if rows >= 0 else None,
                    "columns": columns.get(name, []),
                    "sample_columns": sample_columns,
                    "samples": samples,
                }
            )

        # 返回完整的结构字典：库名、schema 名、每张表的详情。
        return {"database": s.pg_database, "schema": s.pg_schema, "tables": tables}
    finally:
        # 无论成功失败都关闭连接，避免连接泄漏。
        conn.close()


# load_schema：对外提供的「读结构」入口，带 TTL 缓存（TTL = 有效期时长）。
# 参数：
#   s —— 配置对象；
#   refresh —— 是否强制跳过缓存重新读（默认 False）。
# 返回：结构字典（内容同 _read_schema_live）。
def load_schema(s: Settings, refresh: bool = False) -> dict[str, Any]:
    """读取库结构（带 TTL 缓存）。结构变动后可等过期或用 ``refresh=True`` 强制重取。"""
    # 用「连接标识」当缓存键：换了库/换 schema/换表清单就会命中不同缓存。
    key = (s.pg_host, s.pg_port, s.pg_database, s.pg_schema, s.pg_tables)
    # 取出缓存里的 (时间戳, 结构)；若没有则是 None。
    cached = _schema_cache.get(key)
    # 当前时间。
    now = time.time()
    # 命中条件：不是强制刷新 且 有缓存 且 (现在 - 缓存时间) 小于 TTL。
    # max(0, ...) 防止 TTL 配成负数时出现怪异行为。
    if not refresh and cached is not None and now - cached[0] < max(0, s.pg_schema_ttl):
        return cached[1]
    # 缓存没命中：现场读一次。
    data = _read_schema_live(s)
    # 写入缓存（同时记录「什么时候读的」）。
    _schema_cache[key] = (now, data)
    return data


# render_schema_text：把结构字典「渲染」成一段给大模型读的纯文本。
# 为什么这么做：模型不擅长读嵌套的字典结构，用紧凑文本喂给它，写 SQL 更准。
def render_schema_text(data: dict[str, Any]) -> str:
    """把结构字典渲染成给大模型看的紧凑文本。"""
    # 第一行：数据库名和 schema 名。
    lines = [f"数据库：{data.get('database')}（schema：{data.get('schema')}）"]
    # 取出表列表（可能为空，用 or [] 兜底）。
    tables = data.get("tables") or []
    # 没有表：返回一句说明，让模型知道「这里查不到东西」。
    if not tables:
        lines.append("（该 schema 下没有可查询的表）")
        return "\n".join(lines)

    for table in tables:
        # 近似行数；是整数就写成「约 N 行」，否则写「行数未知」。
        rows = table.get("approx_rows")
        row_text = f"约 {rows} 行" if isinstance(rows, int) else "行数未知"
        # 表名 + 行数。
        lines.append(f'\n表 "{table["name"]}"（{row_text}）')
        for col in table.get("columns") or []:
            # 有备注就加上「 -- 备注」；not null 表示此列不允许为空。
            comment = f" -- {col['comment']}" if col.get("comment") else ""
            nullable = "" if col.get("nullable") else " not null"
            lines.append(f'  "{col["name"]}" {col["type"]}{nullable}{comment}')
        # 每张表附上样例行，帮助模型理解真实取值风格。
        for sample in table.get("samples") or []:
            lines.append("  样例：" + json.dumps(sample, ensure_ascii=False))
    # 用换行拼成最终文本。
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 自然语言 → SQL
# --------------------------------------------------------------------------- #
# _sql_system_prompt：生成「给模型的系统提示词」——写 SQL 前先给模型立规矩。
# 参数 max_rows：LIMIT 上限，会写进提示词里，引导模型别拉太多行。
# 为什么这么写：把约束（只读、真实表名、双引号、必须 LIMIT、只输出 SQL）一次讲清，
# 模型越守规矩，后面的护栏就越少误伤。
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


# build_sql_messages：把「系统提示词」和「用户问题+结构」拼成模型能吃的消息列表。
# 消息格式沿用通用约定 [{"role": ..., "content": ...}]（role 有 system/user/assistant）。
# 参数：question 用户问题；schema_text 上一步渲染的结构文本；max_rows LIMIT 上限。
def build_sql_messages(question: str, schema_text: str, max_rows: int) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _sql_system_prompt(max_rows)},
        {"role": "user", "content": f"数据库结构：\n{schema_text}\n\n用户问题：{question}"},
    ]


# clean_sql：清理大模型吐出来的 SQL——它经常不听话，爱加代码围栏和分号。
# 处理步骤：去首尾空白 → 去掉 ``` 围栏 → 去掉结尾的分号和空白。
# 参数 raw：模型原始输出；返回：清理后的 SQL 文本。
def clean_sql(raw: str) -> str:
    """剥掉代码围栏、结尾分号，合并空白行（大模型输出不总是听话）。"""
    # (raw or "") 防止 raw 是 None；strip() 去首尾空白。
    text = (raw or "").strip()
    # 去掉 markdown 代码围栏（正则 sub 把匹配到的部分替换成空串）。
    text = _CODE_FENCE_RE.sub("", text).strip()
    # 去掉结尾的分号/空白（[;\s]+$ 表示「结尾处的一串分号或空白」）。
    text = re.sub(r"[;\s]+$", "", text)
    return text.strip()


# _has_multiple_statements：判断 SQL 里是否出现了「引号之外的分号」。
# 为什么要逐字符扫描，而不是简单 count(';')：因为引号里的分号（比如字符串常量
# 'a;b'）是合法的，不能算作「多条语句」。
# 这里用「状态机」思路：记录当前是否在单/双引号内，只有引号外的分号才算越界。
def _has_multiple_statements(sql: str) -> bool:
    """判断引号外的分号（一条语句里不该出现）。"""
    # in_single / in_double 分别记录「现在是否在单引号/双引号内部」。
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        # 在单引号内：只有遇到收尾的单引号才结束；'' 是转义的单引号，要跳过两个字符。
        if in_single:
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":  # 转义的单引号
                    i += 2
                    continue
                in_single = False
        # 在双引号内：遇到下一个双引号就结束。
        elif in_double:
            if ch == '"':
                in_double = False
        # 引号外：遇到单引号进入单引号态；遇到双引号进入双引号态。
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        # 引号外遇到分号 → 说明有多条语句，直接返回 True。
        elif ch == ";":
            return True
        i += 1
    # 扫完都没发现引号外分号 → 只有一条语句。
    return False


# _scan_sql_text：扫描 SQL 文本，一次产出两样护栏要用的东西：
#   1) 探测视图 —— 引号里的内容、注释里的内容换成等长空格。「等长」是关键：
#      探测出的下标要能原样映射回原句去改写，长度一变下标就全错位。
#      这样 WHERE note = 'no limit here' 这类常量、-- limit 这类注释，
#      不会再骗过「有没有 LIMIT」的判定（它们不是真正的 LIMIT 子句）。
#   2) 末尾注释起点 —— 引号外最后一个行尾注释（-- 或 //）的下标；没有则 None。
# 引号规则与 _has_multiple_statements 保持一致：单/双引号支持成对转义（''、""）
# 与反斜杠转义；反引号包标识符，内部不转义。
def _scan_sql_text(sql: str) -> tuple[str, int | None]:
    out = list(sql)
    cut: int | None = None
    quote: str | None = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote is not None:
            # 转义：反斜杠吃掉下一个字符；成对引号是引号自身的转义。
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
        # 行尾注释：-- 或 // 到行尾。记下起点（后出现的会覆盖，留最后一个），
        # 注释内容在探测视图里全部抹成空格。
        if sql[i : i + 2] in ("--", "//"):
            cut = i
            while i < n and sql[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out), cut


# _strip_trailing_comments：剥掉语句末尾的行尾注释（-- 或 // 到行尾）。
# 为什么必须剥：补 LIMIT 是字符串拼接「… LIMIT 50」，若原句以 -- 注释结尾，
# 拼上去的 LIMIT 会被注释整个吃掉（"SELECT 1 -- 说明 LIMIT 50"），上限形同虚设。
# 为什么只剥「末尾」的：注释后面还有代码的（如 "… -- 说明\nFROM t"）不能切，
# 否则会把 FROM t 一起删掉；那种情况 LIMIT 拼在最后一行，本来就不会被注释吃。
# 连续多行末尾注释（每行一条）用循环剥干净。
def _strip_trailing_comments(sql: str) -> str:
    text = (sql or "").strip()
    while text:
        _, cut = _scan_sql_text(text)
        if cut is None:
            return text
        tail = text[cut:]
        nl = tail.find("\n")
        # 注释跑完到行尾后，若后面还有非空白内容 → 不是「末尾注释」，别动。
        if nl != -1 and tail[nl + 1 :].strip():
            return text
        text = text[:cut].rstrip()
    return text


# validate_sql：本模块的「规则检查员」——把模型生成的 SQL 过一遍护栏。
# 合规就返回规范化后的 SQL，不合规就抛 PgError。
# 检查顺序：非空 → 是否以 SELECT/WITH 开头 → 是否含禁用词 → 是否多条语句 →
#           LIMIT 处理（没有就补，过大就改小）。
# 参数：sql 待检查语句；max_rows LIMIT 允许的最大值。
def validate_sql(sql: str, max_rows: int) -> str:
    """只读护栏 + 单语句 + 强制 LIMIT。返回规范化后的 SQL，违规时抛 PgError。"""
    # 空语句直接拒绝。
    if not sql:
        raise PgError("大模型没有生成可用的 SQL")
    # 必须以 select 或 with 开头，否则拒绝。
    if not _START_RE.match(sql):
        raise PgError("生成的语句不是只读查询（必须以 SELECT 或 WITH 开头）")
    # 搜禁用词；命中就报错，并把命中的词（大写）告诉调用方，便于用户理解。
    hit = _FORBIDDEN_RE.search(sql)
    if hit:
        raise PgError(f"生成的语句包含被禁止的关键字：{hit.group(0).upper()}")
    # 含多条语句 → 拒绝。
    if _has_multiple_statements(sql):
        raise PgError("生成的语句包含多条语句，已拒绝执行")

    # 补/改 LIMIT 之前先剥行尾注释：不然 "SELECT 1 -- 说明" 拼上 " LIMIT 50"，
    # 补的上限会被注释吃掉（见 _strip_trailing_comments）。
    sql = _strip_trailing_comments(sql)
    # 判定用「探测视图」：常量/注释里的 limit 字样不算数（见 _scan_sql_text），
    # 否则 WHERE note = 'no limit here' 会被当成「已有 LIMIT」而漏补上限。
    probe, _ = _scan_sql_text(sql)
    limit_all = _LIMIT_ALL_RE.search(probe)
    fetch = _FETCH_FIRST_RE.search(probe)
    if limit_all:
        # LIMIT ALL = 名义上写了 LIMIT、实际不限行数 → 换成安全上限（不叠加新 LIMIT）。
        sql = f"{sql[: limit_all.start()]}LIMIT {max_rows}{sql[limit_all.end():]}"
    elif fetch:
        # FETCH FIRST n ROWS ONLY：SQL 标准的行数上限 → 视为「已有上限」；
        # 没写数字或数字超限，就把整段换成 LIMIT max_rows（也不叠加）。
        if fetch.group(1) is None or int(fetch.group(1)) > max_rows:
            sql = f"{sql[: fetch.start()]}LIMIT {max_rows}{sql[fetch.end():]}"
    elif not _LIMIT_ANY_RE.search(probe):
        # 没有 LIMIT：直接补一个。
        sql = f"{sql} LIMIT {max_rows}"
    else:
        # 有 LIMIT：找到所有「LIMIT 数字」，只处理最后一个（最靠近结尾的那个）。
        matches = list(_LIMIT_RE.finditer(probe))
        if matches:
            last = matches[-1]
            # 若数字大于上限，就把它替换成 max_rows。
            # 用切片拼接（sql[:位置] + 新值 + sql[位置:]）避免动到别处。
            # 下标来自探测视图——它与原句等长，故可直接映射。
            if int(last.group(1)) > max_rows:
                sql = f"{sql[: last.start(1)]}{max_rows}{sql[last.end(1):]}"
    return sql


# _quote_ident：给标识符（表名/列名）加上双引号。
# 双引号能保证数据库按「原样」理解名字，不受大小写折叠或特殊字符影响。
# 注意：名字里的双引号要「成对」写（把 " 变成 ""），这是 SQL 的转义方式。
def _quote_ident(name: str) -> str:
    """用双引号包裹标识符，避免大小写与特殊字符导致的语法错误。"""
    return '"' + str(name).replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
# run_query：真正把 SQL 发给数据库执行，拿回「列名」和「数据行」。
# 参数：s 配置；sql 已通过校验的语句。
# 返回：(列名列表, 行元组列表)。行是按上限取的，不会一次性拉爆内存。
def run_query(s: Settings, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    """在只读会话中执行查询，返回（列名, 行）。"""
    conn = _connect(s)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
            # description 为 None 通常意味着「这条语句没有结果集」（正常查询不会这样）。
            # 遇到就返回空结果，交给上层友好处理。
            if cur.description is None:
                return [], []
            # 列名：从结果集描述里逐个取第 0 项。
            columns = [d[0] for d in cur.description]
            # 只取前 pg_max_rows 行，防止大表把内存撑爆。
            rows = cur.fetchmany(max(1, s.pg_max_rows))
        return columns, rows
    # PgError 是我们自己的错误，原样往上抛（别包一层，避免信息重复）。
    except PgError:
        raise
    # 其它异常统一转成 PgError，附上原因，便于给模型「人话」提示。
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        raise PgError(f"执行 SQL 失败：{exc}") from exc
    finally:
        conn.close()


# render_result：把执行结果渲染成给模型看的文本。
# 关键约定：0 行结果要「明确告诉模型没有查到、不要编造」，避免模型凭空造数。
# 参数：sql 执行的语句（展示用）；columns 列名；rows 数据行。
def render_result(sql: str, columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """把查询结果渲染成给模型看的文本。"""
    # 头部：固定标签 + 执行的 SQL（方便模型/人核对查了什么）。
    header = f"【PostgreSQL 查询结果】\nSQL：{sql}"
    # 没有数据行：给出「请不要编造」的明确指令。
    if not rows:
        return (
            header
            + "\n查询成功，但返回 0 条记录。\n"
            "请直接告诉用户 PostgreSQL 中没有查到相关记录，不要编造任何数据。"
        )

    # 有数据：列出列名、总条数，然后逐行渲染。
    lines = [header, "返回列：" + ", ".join(columns), f"共 {len(rows)} 条："]
    # enumerate(..., start=1) 让序号从 1 开始（人更习惯）。
    for index, row in enumerate(rows, start=1):
        # 把一行的每个「列=值」用逗号连起来；值都经过 _truncate/_cell_text 处理。
        pairs = ", ".join(f"{col}={_truncate(_cell_text(val))}" for col, val in zip(columns, row))
        lines.append(f"{index}) {pairs}")
    # 拼成多行文本返回。
    return "\n".join(lines)


# _failure_text：把「失败原因」包装成一段给模型看的提示。
# 关键设计：失败也要让模型「如实说明没查到」，绝不编造（应对空库/未配置场景）。
def _failure_text(reason: str) -> str:
    return (
        "【PostgreSQL 查询结果】\n"
        f"查询失败：{reason}\n\n"
        "请如实向用户说明 PostgreSQL 这次查询没有取到结果（可附上失败原因），不要编造数据。"
    )


# --------------------------------------------------------------------------- #
# 工具主体
# --------------------------------------------------------------------------- #
# _run_pg：把所有步骤「串成一条线」的同步函数（读结构→写 SQL→校验→执行→渲染）。
# 为什么是同步：它里面有耗时的数据库和大模型调用，故意做成普通函数，再由外层用
# asyncio.to_thread 丢到工作线程跑，避免阻塞整个异步服务。
# 参数：
#   question —— 用户问题（自然语言）；
#   state —— 一个可变字典，用于把「本次查询的产物」回填给事件层/前端
#            （sql、columns、rows、row_count、error）。
# 返回：给大模型看的结果文本（成功是结果，失败是友好提示）。
def _run_pg(question: str, state: dict[str, Any]) -> str:
    """同步阻塞段：读 schema → 生成 SQL → 校验 → 执行 → 渲染。

    由工具侧用 ``asyncio.to_thread`` 丢到工作线程执行，避免阻塞事件循环。
    """
    # 从 state 里取配置（连接信息等都在里面，全部来自 .env）。
    # 用 .get() 而不是 state["settings"]：后者在 state 没带 settings 时抛 KeyError，
    # 而这个 KeyError 发生在 try 之外，会穿透「工具只返回可读提示、不向上抛异常」的承诺。
    s: Settings | None = state.get("settings")
    # sql 先置空，方便异常分支里回填「到底有没有生成出 SQL」。
    sql: str | None = None
    try:
        # 配置缺失当「预期内」错误处理，与其它失败走同一条可读提示通道。
        if s is None:
            raise PgError("工具未注入配置（state 缺少 settings），已跳过执行")
        # 1) 读（带缓存的）库结构，并渲染成文本。
        schema_text = render_schema_text(load_schema(s))
        # 2) 让大模型根据「问题 + 结构」生成 SQL。
        #    temperature=0.0 表示「要确定性」——写 SQL 不需要创造力，越稳越好。
        raw = complete_chat(
            build_sql_messages(question, schema_text, s.pg_max_rows),
            settings=s,
            model=s.pg_model_or_default,
            temperature=0.0,
            max_tokens=s.pg_max_tokens,
            timeout=s.pg_timeout,
        )
        # 3) 清理（去围栏/分号）→ 过护栏校验 → 得到最终 SQL。
        sql = validate_sql(clean_sql(raw), s.pg_max_rows)
        # 4) 执行查询。
        columns, rows = run_query(s, sql)

        # 5) 把产物回填进 state（供事件层/前端展示）。
        state["sql"] = sql
        state["columns"] = columns
        state["row_count"] = len(rows)
        # 只回填前 _ROWS_TO_STATE 行给前端表格；每格再截断，避免太长。
        state["rows"] = [
            {col: _truncate(_cell_text(val)) for col, val in zip(columns, row)}
            for row in rows[:_ROWS_TO_STATE]
        ]
        # 成功：清空错误标记。
        state["error"] = None
        # 把完整结果渲染成文本返回给模型。
        return render_result(sql, columns, rows)
    # 「预期内」的错误：我们自己抛的 PgError、LLM 错误、LLM 未配置。
    # 取出人话原因，稍后回填并提供友好提示。
    except (PgError, LLMError, LLMNotConfigured) as exc:
        reason = str(exc)
    # 「预期外」的错误：兜底把「类型名 + 信息」拼成人话，避免服务崩掉。
    # 核心原则：工具只返回可读提示，不向上抛异常。
    except Exception as exc:  # noqa: BLE001 - 工具只返回可读提示，不向上抛
        reason = f"{type(exc).__name__}: {exc}"

    # 失败分支：回填已生成的信息（sql 可能是 None），并把结果行清空。
    state["sql"] = sql
    state["columns"] = state.get("columns") or []
    state["row_count"] = 0
    state["rows"] = []
    state["error"] = reason
    # 返回友好失败提示。
    return _failure_text(reason)


# @tool 装饰器：把下面这个函数「登记」为大模型可调用的工具。
# 模型靠「函数名」和这段 docstring 来判断「什么时候该用它」，所以 docstring
# 写得越清楚，模型选得越准（这段 docstring 是给模型看的，不只是给人看的）。
# 注意：@tool 与 async def 之间允许插入注释行，不影响语法。
@tool
# query_bidding_postgres：工具本体，异步函数。
# 参数：
#   question —— 模型从用户问句里提炼出的问题；
#   config —— LangChain 传进来的运行时配置，里面藏着事件层塞的 _pg_state。
# 返回：最终给模型看的文本（成功是结果/失败是提示）。
async def query_bidding_postgres(question: str, config: RunnableConfig) -> str:
    """查询 PostgreSQL 中的招投标明细数据，用于统计、计数、求和/平均、排序、精确筛选。

    适用于「有多少个」「合计/平均多少钱」「按地区统计」「预算超过某值的项目」
    这类可以用 SQL 精确计算的问题。若问题是询问公告原文、条款措辞或资质描述，
    请改用 search_bidding_knowledge 检索知识库。
    """
    # 从 config 里取事件层放好的状态字典。
    # configurable 是 LangChain 约定的「可配置项」容器，可能为 None，用 or {} 兜底。
    state = (config.get("configurable") or {}).get("_pg_state")
    # 理论上事件层一定会传；万一没有就兜底造一个「无状态」的，保证工具不崩。
    if state is None:
        # 理论上不会发生（agent_events 一定会传）；兜底成一次无状态查询
        state = {"settings": get_settings()}
    # 用 asyncio.to_thread 把同步的 _run_pg 挪到后台线程执行，然后 await 结果。
    # 这样耗时的数据库/大模型调用就不会卡住异步主线程。
    return await asyncio.to_thread(_run_pg, question, state)