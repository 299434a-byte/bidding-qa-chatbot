# ============================================================================
# 文件：db.py —— 连接 MySQL 数据库的「只读取数窗口」
#
# 【这个文件在整个项目里是干什么的】
#   后台的知识库是从 MySQL 里的一张张业务表（招标公告、项目、公司等）来的。
#   这个文件负责：①管好与 MySQL 的连接（怎么连、什么时候关）；
#   ②「自动发现表结构」—— 不用提前知道有哪些字段，连上后现场问数据库「这张表有哪些列」；
#   ③把数据库里的值（日期、金额小数、二进制等）统一清洗成「能安全转成 JSON 的文字」；
#   ④一行一行地把表数据「流」出来，喂给建库流水线。
#
# 【谁调用它（上游）】
#   backend/app/ingest.py —— 建库管道：
#     · discover_schema() 拿表结构（列名/类型/注释/主键/行数）；
#     · iter_rows() 一行一行取数据；
#     · describe_columns() 拿「列名 → 中文展示名」的映射。
#   backend/app/main.py —— /api/tables 之类的接口也可能调用 list_tables() 做排查。
#
# 【它给谁用（下游）】
#   documents.py 拿「行数据 + 列定义」渲染成自然语言文本，再进 BM25 和向量库。
#
# 【先懂的三个概念（大白话）】
#   1) 只读（项目中只允许读，不允许写）：本项目连数据库只做 SELECT 查询，
#      绝不 INSERT/UPDATE/DELETE，避免误伤业务数据。这是硬约束，注释里多处强调。
#   2) 连接（connection）：和数据库之间的一条「电话线」。要打电话得先拨号（connect）、
#      说完话要挂断（close）。不挂断的话，连接越积越多，数据库会拒绝新连接。
#   3) 流式读取（streaming）：一次只从数据库取一小批数据（比如 500 行），
#      处理完再取下一批。好处是几百万行的大表也不会把内存撑爆——
#      全读进来一次性处理才叫危险。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring）】
# 【为什么放在这里】注释不算语句，所以放在这一大段注释之后，它依然是本模块的
#   第一条语句、依然是官方自我介绍，内容与原文一字不差。
"""MySQL 数据源接入（只读）。

核心设计：**表结构在运行时自动发现**。
字段名、字段注释、主键全部来自 ``information_schema``，
因此代码里不写死任何业务字段，后续表结构变更也不需要改代码。
三张表当前为空时，``information_schema`` 依然能读到完整列定义，
所以「空数据运行」是天然支持的。
"""

# 【这行在干什么】打开「类型注解延迟求值」开关。
# 【为什么这么写】本文件用了 dict[str, Any]、tuple[str, ...]、Settings | None 这类新式标注，
#   老版本 Python（3.8/3.9）不认；加上这行后标注先被当成字符串存着、需要时才解析。
# 【注意事项】必须放在文件最顶部（所有执行代码之前），这是 Python 的硬性要求。
from __future__ import annotations

# 【这行在干什么】引入 Python 自带的日期时间模块，并起个别名 dt（省得每次写 datetime）。
# 【为什么需要】数据库里的日期/时间/时间差（timedelta）是特殊对象，不能直接转成 JSON；
#   下面 normalize_value 要用 dt.datetime / dt.date 等来识别它们，并格式化成字符串。
#   【别名解释】`import datetime as dt` 读作「把 datetime 模块引入，用简称 dt 指代它」，
#   于是下文就能写 dt.date 而不是 datetime.date。
import datetime as dt

# 【这行在干什么】引入 decimal 模块，专门处理「高精度小数」。
# 【为什么必须用它处理金额】普通浮点数（float）存 0.1+0.2 会得到 0.30000000000000004，
#   金额上出现这种误差是灾难。decimal.Decimal 是为「精确十进制」设计的，
#   数据库里的 DECIMAL 类型读出来就是 Decimal，本文件把它保留成字符串，分毫不差。
import decimal

# 【这行在干什么】引入正则表达式模块 re。
# 【为什么需要】下面要写一个「白名单正则」，用来校验表名、列名是否只含
#   字母、数字、下划线。这是防 SQL 注入的关键一环（详见 _SAFE_IDENT 处注释）。
import re

# 【这行在干什么】引入 contextmanager 装饰器。
# 【为什么需要】用它把「获取连接」写成一个「上下文管理器」，配合 with 语句使用：
#   with mysql_connection() as conn: ... 进入时自动拨号、离开时自动挂断（哪怕中途报错
#   也保证挂断）。这属于「资源自动释放」的经典用法，详见 mysql_connection 处注释。
from contextlib import contextmanager

# 【这行在干什么】引入类型标注工具 Any、Iterator。
# 【为什么需要】
#   · Any —— 数据库取出来的值类型不确定（数字/字符串/日期…），用 Any 兜住；
#   · Iterator —— 标注那些「一个接一个吐数据」的函数（如 iter_rows）。
from typing import Any, Iterator

# 【这行在干什么】引入 pymysql：本项目使用的 MySQL 驱动库（Python 与 MySQL 之间的翻译官）。
# 【为什么用它】它是纯 Python 实现的轻量驱动，安装方便、够用；且支持「流式游标」，
#   满足大表分批读取的需求。
import pymysql

# 【这行在干什么】从 pymysql 的游标模块里引入 SSDictCursor。
# 【名词解释】
#   · 游标（cursor）—— 可以理解成「读数据的指针/取号机」，执行 SQL、取回结果都靠它；
#   · SSDictCursor —— Server-Side 的字典游标：①结果以「字典」形式返回（可用列名取值，
#     如 row["COLUMN_NAME"]，比按下标 row[0] 直观得多）；②「服务端游标」意味着一批一批
#     从数据库服务器拉数据，而不是一次性全拉到客户端内存里，适合大表。
# 【注意事项】它只在 iter_rows 里显式指定；其他查询用连接默认的 DictCursor（也是字典形式）。
from pymysql.cursors import SSDictCursor

# 【这行在干什么】引入本项目的配置类和取配置函数。
# 【为什么这么写】MySQL 的主机、端口、用户名、密码、库名、字符集、超时统统来自 .env，
#   代码里不写死（项目硬约束）；开头点号「.」表示「同一个包里的模块」。
from .config import Settings, get_settings

# 【这行在干什么】编译一个正则表达式「白名单」，用来校验表名/列名是否合法。
# 【参数逐个解释】
#   · r"..." —— 前缀 r 表示「原始字符串」：里面的 \ 不会被当作转义符，写起来更省心；
#   · ^ 表示字符串开头，$ 表示字符串结尾，两者合起来表示「整个字符串必须完全满足规则」；
#   · [A-Za-z0-9_]+ —— 允许出现「大小写字母 / 数字 / 下划线」，且至少要有一个字符；
#   · 综合起来：名字必须由字母、数字、下划线组成，不能含空格、分号、引号等任何其他字符。
# 【为什么这是安全防线（关键，别删）】下面有些 SQL 需要把表名/列名拼进语句里（例如
#   SELECT * FROM 某表）。而 SQL 参数化查询（%s 占位）只能保护「值」，保护不了
#   「表名/列名」这种标识符。黑客如果能让表名变成 `x; DROP TABLE y`，后果不堪设想。
#   所以凡是拼进 SQL 的标识符，一律先用这个白名单校验，不通过就报错——这就是防注入。
# 表名/列名白名单校验，杜绝拼接 SQL 时的注入风险
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


# 【这行在干什么】定义一个自定义异常类 DatabaseUnavailable。
# 【为什么需要它（重要设计）】数据库连不上是很常见的情况（没配 .env、网络不通、
#   服务没起）。如果直接把底层异常抛给上层，上层很难统一处理。自定义一个「语义明确」的
#   异常后，上层（如 main.py）可以专门捕获它，转成友好的「数据库未配置/不可用」提示，
#   而不是让请求 500。这就叫「异常分层」。
# 【参数解释】继承 RuntimeError —— 表示「运行时出错」的通用异常类型，语义合适。
# 【注意事项】文档字符串里点明了调用方的责任：「需优雅降级」——见凡出错的地方都要
#   让系统退到「无数据库也能用」的状态。
class DatabaseUnavailable(RuntimeError):
    """数据库不可用（未配置 / 连不上 / 无权限），调用方需优雅降级。"""


# 【这行在干什么】定义函数 quote_ident：给表名/列名加上反引号，并做合法性校验。
# 【参数解释】name —— 待处理的标识符（如 "bidding_notices"）。
# 【返回值】带反引号的字符串，如 "`bidding_notices`"（供拼进 SQL）。
# 【为什么要加反引号】反引号是 MySQL 里用来「圈住标识符」的符号，作用是防止名字恰好
#   和 SQL 关键字冲突（比如某列名叫 order，不加反引号会被当成排序命令）。
# 【为什么还要校验（关键）】反引号本身挡不住注入（`a`; DROP... 依然能作恶），
#   所以必须先用白名单确认名字只含安全字符，确认后再加反引号——两步缺一不可。
def quote_ident(name: str) -> str:
    """给标识符加反引号，并校验合法性。"""
    # 【这行在干什么】用白名单正则去匹配这个名字；匹配不上（不是「字母数字下划线」）
    #   就进入下面的分支报错。
    # 【参数解释】_SAFE_IDENT.match(name) 从字符串开头尝试匹配；
    #   因为正则里带了 $（必须到结尾），所以 match 成功就等于「整个字符串合法」。
    #   匹配成功返回一个匹配对象（真），失败返回 None（假），所以可直接写在 if 里。
    if not _SAFE_IDENT.match(name):
        # 【这行在干什么】抛出一个 ValueError，拒绝这个非法名字。
        # 【为什么用 {name!r}】!r 表示「用 repr 形式展示」，会把值带上引号。
        #   比如 name 是 abc;drop 时，报错信息是 非法标识符: 'abc;drop'，
        #   一眼就能看出那个可疑的字符串到底是什么，比不加引号更清楚。
        raise ValueError(f"非法标识符: {name!r}")
    # 【这行在干什么】给合法的名字套上反引号并返回。
    # 【写法解释】f"`{name}`" 是格式化字符串，最终得到 `bidding_notices` 这样的结果。
    return f"`{name}`"


# 【这行在干什么】@contextmanager 把下面的函数包装成「上下文管理器」。
# 【通俗解释】有了它，调用方就能写 `with mysql_connection() as conn:`。
#   with 块开始时执行「拨号」；块结束时（无论正常结束还是报错）自动执行「挂断」。
# 【为什么必须这么写（核心价值）】如果数据库连接用完忘了关，连接会一直占着，
#   越积越多最终把数据库的连接数吃满、整个服务瘫痪。用上下文管理器，
#   「用完必关」由 Python 保证，程序员不可能忘。
@contextmanager
def mysql_connection(settings: Settings | None = None) -> Iterator[pymysql.connections.Connection]:
    """获取 MySQL 连接。连接参数全部来自 .env。"""
    # 【这行在干什么】拿到真正要用的配置对象（传了就用传的，没传就从 .env 读）。
    # 【为什么这么写】settings or get_settings() 一行实现「不传就用默认」；
    #   get_settings() 自带缓存，反复调用不重复读盘。
    s = settings or get_settings()
    # 【这行在干什么】检查配置是否完整（主机、用户名等关键项有没有配）。
    # 【为什么先检查再连接】配置缺了还去连，只会得到一条语焉不详的报错；
    #   提前检查能给出「请检查 .env 文件」这种可操作的提示，用户体验好很多。
    if not s.mysql_configured:
        # 【这行在干什么】抛出「数据库不可用」异常，并附上排查指引。
        # 【为什么用自定义异常】上层可专门捕获它做优雅降级（见类定义处注释）。
        raise DatabaseUnavailable("未配置 MYSQL_USER / MYSQL_HOST，请检查项目根目录的 .env 文件")
    # 【这行在干什么】打开「接住岔子」的网，包住「真正连接数据库」这一步。
    # 【为什么只包连接这一步】连接是会失败的（网络不通、密码错、库不存在），
    #   需要把它们翻译成统一的 DatabaseUnavailable；连接成功后的使用过程不在这里管。
    try:
        # 【这行在干什么】真正拨号：按配置参数连接 MySQL。
        # 【参数逐个解释】
        #   · host / port —— 数据库地址和端口（从 .env 读）；
        #   · user / password —— 账号密码（从 .env 读，绝不写死在代码里）；
        #   · database —— 要连的库名；
        #   · charset —— 字符集（通常 utf8mb4，能正确存取中文和 emoji）；
        #   · connect_timeout —— 连接超时秒数，避免网络异常时无限等待、把请求卡死；
        #   · autocommit=True —— 自动提交事务。本项目只读，其实不涉及事务，
        #     但设为 True 能避免「长事务占着资源」；也省去手动 commit/rollback 的麻烦；
        #   · cursorclass=DictCursor —— 让默认游标返回「字典」（可按列名取值，更直观）；
        #     注意这里用的是完整路径 pymysql.cursors.DictCursor。
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
    # 【这行在干什么】捕获 pymysql 自己的异常类型 MySQLError（涵盖连不上、认证失败等）。
    except pymysql.MySQLError as exc:
        # 【这行在干什么】把底层异常「翻译」成我们的 DatabaseUnavailable 再抛出。
        # 【参数逐个解释】
        #   · 报错信息里带上「主机:端口/库名」——方便运维快速定位连的是哪个库；
        #   · {exc} —— 原始异常的文字描述，保留它不丢失一线排查线索；
        #   · from exc —— 这是「异常链」语法：表示「这个新异常是由 exc 引起的」。
        #     好处：报错时 Python 会同时打印两段堆栈（原始 + 新的），
        #     既能给上层看友好提示，又不丢技术细节。
        raise DatabaseUnavailable(f"连接 MySQL({s.mysql_host}:{s.mysql_port}/{s.mysql_database}) 失败: {exc}") from exc
    # 【这行在干什么】打开「保证收尾」的网：无论 with 块里发生什么，finally 都会执行。
    try:
        # 【这行在干什么】把连接「交出去」，暂停在这里等调用方用完。
        # 【为什么用 yield（这是上下文管理器的关键）】@contextmanager 的写法要求：
        #   yield 之前 = 进入 with 时执行的准备动作（这里是连接）；
        #   yield 出去的值 = with ... as conn 里的那个 conn；
        #   yield 之后（finally 里的代码）= 离开 with 时的收尾动作。
        yield conn
    finally:
        # 【这行在干什么】关闭连接，把「电话线」挂断。
        # 【为什么放在 finally】不管 with 块里是正常跑完、还是中途抛了异常，
        #   这句都一定会执行，保证连接不泄漏。这就是「用完必关」的实现。
        conn.close()


# 【这行在干什么】画一条分隔线注释，标明下面这一节讲的是「值归一化」。
# --------------------------------------------------------------------------- #
# 值归一化
# --------------------------------------------------------------------------- #
# 【这行在干什么】定义函数 normalize_value：把 MySQL 返回的任意值转成「可 JSON 序列化的形式」。
# 【为什么要做这件事（重要）】数据库里的类型五花八门（日期、时间差、高精度小数、二进制），
#   它们不是 JSON 原生支持的类型。若原样送去生成 JSON（比如接口响应、写文件），
#   会直接抛「Object of type date is not JSON serializable」这类错误。
#   所以在这里统一「翻译」成字符串，后面就一路畅通。
# 【参数解释】value —— 从数据库取出的任意值（可能是 None/日期/小数/字节…）。
# 【返回值】一个「安全」的值：字符串，或本来就安全的原值。
def normalize_value(value: Any) -> Any:
    """把 MySQL 返回值转成可 JSON 序列化的形式。"""
    # 【这行在干什么】如果值是 None（数据库里的 NULL），返回空字符串 ""。
    # 【为什么把 NULL 变成空串】下游（documents.py）对空值的处理就是「跳过不展示」；
    #   统一转成 "" 让下游判断简单——不用同时处理 None 和 "" 两种空。
    if value is None:
        return ""
    # 【这行在干什么】如果值是「日期+时间」对象（如 2026-09-29 10:30:00），格式化成字符串。
    # 【参数解释】strftime("%Y-%m-%d %H:%M:%S") —— 按「年-月-日 时:分:秒」的格式输出，
    #   这是最通用、最易读的写法，也是业务上常见的展示格式。
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    # 【这行在干什么】如果值是「纯日期」对象（没有时分秒），格式化成 2026-09-29。
    # 【注意事项】判断顺序很重要：datetime 是 date 的「子类」，所以必须先判断 datetime，
    #   否则 datetime 会被上面这条 date 判断「截走」，丢掉时分秒。这是经典坑。
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    # 【这行在干什么】如果值是「纯时间」对象，格式化成 10:30:00。
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    # 【这行在干什么】如果值是「时间差」对象（如 2 days, 3:00:00），直接转成文字。
    # 【为什么只 str 不特殊格式化】时间差没有统一的展示惯例，原样转字符串最省事、信息不丢。
    if isinstance(value, dt.timedelta):
        return str(value)
    # 【这行在干什么】如果值是「高精度小数」（数据库的 DECIMAL，金额最常见），转成字符串。
    # 【参数解释】format(value, "f") —— 用定点格式输出：不会用科学计数法（如 1e+06），
    #   而是老老实实写成 1000000.00 这种形式。金额必须这样写才易读、才精确。
    # 【为什么用字符串而不是 float（关键，别改）】浮点数会引入精度误差，
    #   金额一旦有误差就是账目事故。保留成字符串就完全无损。
    if isinstance(value, decimal.Decimal):
        # 金额类字段用字符串保留精度，避免浮点误差
        return format(value, "f")
    # 【这行在干什么】如果值是「字节串」（二进制数据），尝试按 utf-8 解码成文字。
    # 【参数逐个解释】
    #   · bytes(value) —— 确保拿到的是标准 bytes 对象；
    #   · .decode("utf-8", "ignore") —— 按 utf-8 解码；遇到解不出来的字节就
    #     「忽略」它（而不是报错崩溃）。数据库里的二进制字段（图片、附件）本来就不是
    #     文字，这里只是「尽力而为」地转一下，转不出来也不让流程挂掉。
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "ignore")
    # 【这行在干什么】以上都不满足（整数、字符串、布尔等本来就是 JSON 安全的），
    #   原样返回。
    return value


# 【这行在干什么】定义函数 normalize_row：把「整行数据」里的每个值都归一化一遍。
# 【参数解释】row —— 一行数据的字典（键是列名，值是各种类型）。
# 【返回值】新的字典，值都被 normalize_value 处理过了。
# 【写法拆解】{key: normalize_value(val) for key, val in row.items()} 是「字典推导式」：
#   它会遍历 row 的每一对键值，把值经过归一化后，重新组装成一个新字典。
#   相当于「就地翻译、逐项替换」，比写循环+赋值更简洁。
def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    # 【这行在干什么】用字典推导式逐项归一化后返回。
    # 【注意事项】它返回的是「新字典」，不会改动原来的 row（原字典保持不变）。
    return {key: normalize_value(val) for key, val in row.items()}


# 【这行在干什么】画一条分隔线注释，标明下面这一节讲的是「表结构自动发现」。
# --------------------------------------------------------------------------- #
# 表结构自动发现
# --------------------------------------------------------------------------- #
# 【这行在干什么】定义函数 discover_schema：读取每张表的真实列定义和行数。
# 【参数逐个解释】
#   · conn —— 已建立的数据库连接（调用方负责用 with mysql_connection() 管理）；
#   · tables —— 要探查的表名清单（元组，如 ("bidding_notices", "bidding_project")）。
#     【为什么用元组而不是列表】元组是「不可变」的（定义后改不了），
#     用它表示「一组固定的表名」更符合语义，也更安全（不会被误改）。
# 【返回值】一个字典，结构见下方 docstring 的详细演示。
# 【为什么这个函数是「自动发现」的典范（关键设计）】它不硬编码任何业务字段，
#   而是去问数据库的「说明书库」information_schema：这张表有哪些列、列是什么类型、
#   列上写的中文注释是什么、主键是谁。所以将来表结构变了（加字段、改注释），
#   代码一行都不用动——这是本项目「不硬编码业务字段」硬约束的落地。
def discover_schema(conn: pymysql.connections.Connection, tables: tuple[str, ...]) -> dict[str, dict]:
    """读取每张表的真实列定义 + 行数。

    返回结构::

        {
          "bidding_notices": {
            "table": "bidding_notices",
            "exists": True,
            "row_count": 0,
            "primary_key": "id",
            "columns": [
               {"name": "id", "type": "bigint", "nullable": False,
                "key": "PRI", "comment": "主键", "position": 1},
               ...
            ],
            "error": None,
          },
          ...
        }
    """
    # 【这行在干什么】建一个空字典，用来按表名收集每张表的探查结果。
    # 【类型解释】dict[str, dict]：键是表名（字符串），值是「这张表的结果字典」。
    result: dict[str, dict] = {}
    # 【这行在干什么】逐张表处理。
    for table in tables:
        # 【这行在干什么】先给这张表准备一份「默认结果」骨架，各个字段都填上「失败/空」的初值。
        # 【为什么先填默认值（重要设计）】这样即使后面查询中途出错，也能返回一个「结构完整、
        #   只是标记了错误」的结果，而不是缺字段导致调用方取值报错。这就是「容错骨架」。
        # 【字段逐个解释】
        #   · table —— 表名；
        #   · exists —— 该表是否真实存在（先假设不存在，查到列定义后再改 True）；
        #   · row_count —— 行数（先 0）；
        #   · primary_key —— 主键列名（先 None）；
        #   · columns —— 列定义列表（先空）；
        #   · error —— 错误信息（先 None，表示「暂时没出错」）。
        entry: dict[str, Any] = {
            "table": table,
            "exists": False,
            "row_count": 0,
            "primary_key": None,
            "columns": [],
            "error": None,
        }
        # 【这行在干什么】打开「接住岔子」的网，包住对这张表的所有查询。
        # 【为什么每张表单独 try（关键设计）】如果一张表查询失败（比如不存在），
        #   不应该让整批探查全崩。分开 try，就能做到「一张表出错只影响它自己，
        #   其余表照常返回」，整体体验更稳。
        try:
            # 【这行在干什么】with conn.cursor() as cur: 创建游标，并在块结束时自动关闭。
            # 【为什么要用 with】游标也是资源，用完要关；with 保证退出时自动关闭，
            #   不依赖人工记得调 cur.close()。
            with conn.cursor() as cur:
                # 【这行在干什么】执行一条查询，从「数据库说明书库」information_schema 里
                #   读出这张表的所有列定义。
                # 【参数逐个解释（SELECT 的每一列）】
                #   · COLUMN_NAME —— 列名（如 project_name）；
                #   · DATA_TYPE —— 基础类型（如 varchar、decimal）；
                #   · COLUMN_TYPE —— 完整类型（带长度/精度，如 varchar(255)）；
                #   · IS_NULLABLE —— 是否允许为空（YES/NO）；
                #   · COLUMN_KEY —— 键信息（PRI 表示主键）；
                #   · COLUMN_COMMENT —— 列上的中文注释（业务字段名的「人话解释」）；
                #   · ORDINAL_POSITION —— 列在表里的排序位置（1、2、3……）。
                # 【WHERE 条件解释】
                #   · TABLE_SCHEMA = DATABASE() —— 只查「当前连接的这个库」，
                #     不会串到别的库去；
                #   · TABLE_NAME = %s —— 限定为这一张表。
                # 【为什么 %s 要用参数传（安全要点）】%s 是「占位符」，
                #   真正的表名通过第二个参数 (table,) 传进去。这样驱动会自动做转义，
                #   杜绝 SQL 注入。绝不能自己用 f-string 把表名拼进 SQL！
                #   【注意事项】(table,) 的逗号不能省——它是「单元素元组」的写法，
                #   少了逗号就变成普通字符串，参数个数对不上会报错。
                # 【ORDER BY ORDINAL_POSITION】按列在表中的原始顺序返回，
                #   保证「生成的文字顺序和建表顺序一致」，重跑结果稳定。
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
                # 【这行在干什么】一次性取出上面查询的全部结果行（每行是一个字典）。
                # 【为什么这里可以 fetchall】一列定义只有几十行，很小，一次取完没问题；
                #   真正的大表数据读取用的是 iter_rows 里的分批取。
                rows = cur.fetchall()
                # 【这行在干什么】如果一行结果都没有，说明这张表不存在、或当前账号没权限看它。
                # 【为什么用「没有列定义」判断表不存在】information_schema 里查不到某表的列，
                #   要么是表本身不存在，要么是权限不足看不到——两种情况对使用者而言表现相同。
                if not rows:
                    # 【这行在干什么】把错误原因写进结果，标注出来。
                    entry["error"] = "表不存在或当前账号无访问权限"
                    # 【这行在干什么】把这张表的结果存进总结果字典，然后跳过后续步骤。
                    result[table] = entry
                    # 【为什么用 continue】这张表已经处理完毕（结论是「不存在」），
                    #   直接进入下一张表；continue 比多套一层 if 更扁平易读。
                    continue

                # 【这行在干什么】走到这里说明表确实存在，把存在的标记改为 True。
                entry["exists"] = True
                # 【这行在干什么】把上面查到的每行列定义，整理成我们需要的「整洁结构」。
                # 【写法拆解】这是一个「列表推导式」：遍历 rows 里的每一行 r，
                #   为每行构造一个字典，全部收集成一个列表，赋值给 entry["columns"]。
                # 【字段逐个解释（每个字典的键）】
                #   · name —— 列名，直接取 COLUMN_NAME；
                #   · type —— 基础类型，取 DATA_TYPE；
                #   · column_type —— 完整类型（带长度的那种），取 COLUMN_TYPE；
                #   · nullable —— 是否可空：把数据库里的 "YES"/"NO" 转成 Python 的真假值；
                #     写成 `r["IS_NULLABLE"] == "YES"`，相等就是 True、不等就是 False，
                #     比原样存字符串更好用；
                #   · key —— 键信息（"PRI" 等），原样保留；
                #   · comment —— 中文注释：`(r["COLUMN_COMMENT"] or "").strip()` 先用 or ""
                #     兜住「注释为 NULL」的情况，再 strip 去掉首尾空白；
                #   · position —— 列位置，用 int(...) 确保它是数字而非数据库返回的其他形式。
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
                # 【这行在干什么】从所有列里挑出「主键列」，把它们的列名收集成一个列表。
                # 【写法拆解】列表推导式：遍历 columns，只保留 key == "PRI" 的那些列，
                #   取它们的 name。理论上主键通常只有一个，但也可能是「联合主键」（多个列）。
                pks = [c["name"] for c in entry["columns"] if c["key"] == "PRI"]
                # 【这行在干什么】如果有主键列，就取第一个作为「主键」；否则记为 None。
                # 【参数解释】pks[0] if pks else None —— 三元表达式：pks 非空（真）就取第 0 个，
                #   为空（假）就取 None。本项目的 doc_id 需要主键来保证唯一，没有主键就只能记 None。
                entry["primary_key"] = pks[0] if pks else None

                # 【这行在干什么】执行另一条查询：统计这张表总共有多少行。
                # 【参数解释】COUNT(*) 是「数总共有几条记录」的聚合函数，结果只有一行一列，
                #   列名被 AS c 起名为 c（方便下面取用）。
                # 【为什么表名要用 quote_ident（安全要点）】表名属于「标识符」，不能用 %s 占位，
                #   只能拼进 SQL。所以必须先用 quote_ident 校验并加反引号，防止注入。
                # 【为什么需要知道行数】这样能立刻发现「表是空的」——前端可以友好提示
                #   「数据还没灌入」，而不是让用户以为系统坏了。也与「支持空数据运行」的约束呼应。
                cur.execute(f"SELECT COUNT(*) AS c FROM {quote_ident(table)}")
                # 【这行在干什么】取回那一行结果，从中拿出列 c（行数），转成整数存起来。
                # 【为什么 int(...)】数据库 COUNT 返回的数值在某些驱动下可能是 Decimal 或字符串，
                #   统一转成 int，后续比较/展示更省心。
                entry["row_count"] = int(cur.fetchone()["c"])
        # 【这行在干什么】捕获这张表处理过程中的任何 MySQL 错误。
        # 【为什么 catch 在这里（而不是外面）】见上面注释：这样能实现「单表失败不影响整批」。
        except pymysql.MySQLError as exc:
            # 【这行在干什么】把失败原因写进这张表的结果里（保留结构完整）。
            entry["error"] = f"读取表结构失败: {exc}"
        # 【这行在干什么】把这张表的结果收进总结果字典。
        # 【为什么写在这里（try 之外）】无论是正常查完、还是中途出错走了 except，
        #   最终都要把这张表的结果记录进去；放在 try 块结束后统一收口，确保不遗漏。
        result[table] = entry
    # 【这行在干什么】返回「所有表的探查结果」总字典。
    return result


# 【这行在干什么】定义函数 iter_rows：一行一行地流式读取整表数据。
# 【参数逐个解释】
#   · conn —— 已建立的数据库连接；
#   · table —— 要读的表名；
#   · batch_size=500 —— 每批从数据库取多少行（默认 500，可按需调整）。
# 【返回值】Iterator[dict[str, Any]] —— 一个「逐行产出数据」的迭代器（生成器）。
# 【为什么必须「流式」（关键性能设计）】一张表可能有几十万甚至上百万行，
#   如果 `SELECT * FROM 表` 后一次性 fetchall，会把全部数据读进内存，
#   轻则卡顿、重则内存崩盘。改成「一批一批取」，内存里始终只留几百行，稳如泰山。
def iter_rows(
    conn: pymysql.connections.Connection,
    table: str,
    batch_size: int = 500,
) -> Iterator[dict[str, Any]]:
    """流式读取整表，避免大表一次性载入内存。"""
    # 【这行在干什么】拼出查询整表的 SQL 语句。
    # 【为什么表名要用 quote_ident】同前：表名不能用参数占位，必须校验后加反引号，
    #   防止 SQL 注入。
    # 【为什么用 SELECT *】本项目要「全字段生成文本」，所以读全部列；
    #   且列会由 documents.py 结合表结构来决定怎么展示，这里不必挑字段。
    sql = f"SELECT * FROM {quote_ident(table)}"
    # 【这行在干什么】打开一个「服务端、返回字典」的游标，并在块结束时自动关闭。
    # 【参数解释】conn.cursor(SSDictCursor) —— 显式指定游标类型为 SSDictCursor：
    #   ①返回字典（可按列名取值）；②服务端游标（分批从服务器拉取，天然适合流式）。
    # 【注意事项】服务端游标在数据没取完前会占用数据库侧的资源，
    #   所以必须保证「取完 或 关闭」。with 语句保证块结束时一定关闭，这个兜底很关键。
    with conn.cursor(SSDictCursor) as cur:
        # 【这行在干什么】执行这条查询，游标就位，等待逐批取数据。
        cur.execute(sql)
        # 【这行在干什么】进入一个「无限循环」，靠内部条件跳出。
        # 【为什么用 while True】因为不知道总共有多少行、要取多少批，
        #   只能「一直取，直到取不到为止」，再主动 break。
        while True:
            # 【这行在干什么】从游标里一次拉取最多 batch_size 行。
            # 【参数解释】fetchmany(500) —— 「最多取 500 行」。注意是「最多」：
            #   最后一批可能不足 500 行，数据取完后再取会返回空列表。
            rows = cur.fetchmany(batch_size)
            # 【这行在干什么】如果这批是空的，说明数据已经全部取完。
            if not rows:
                # 【这行在干什么】跳出循环，结束读取。
                # 【为什么以「空」为结束条件】这是取数循环的标准写法：
                #   服务端游标取到末尾后，fetchmany 会返回空列表，这就是「没了」的信号。
                break
            # 【这行在干什么】把这一批里的每一行依次处理。
            # 【为什么要内层再循环】batch 是「一批」，但我们要对外「一行一行地给」，
            #   所以把批次拆成单行逐个 yield。
            for row in rows:
                # 【这行在干什么】把这一行归一化后「吐出去」（yield）。
                # 【为什么要 normalize_row】数据库的日期/金额/二进制等类型不能直接进 JSON，
                #   统一清洗成字符串，下游（documents.py）拿到就能直接用。
                # 【为什么用 yield 而不是收集成列表返回（关键）】yield 让这个函数变成
                #   「生成器」：调用方每要一行，这里才取一行、清洗一行、交出去一行。
                #   全程内存里只有「当前这一行」，大表也不会爆内存。
                #   而若改成「全部收集后 return」，就等于把整个表读进内存，白费了流式游标的优势。
                yield normalize_row(row)


# 【这行在干什么】定义函数 describe_columns：返回「列名 → 展示用中文名」的映射。
# 【参数逐个解释】
#   · conn —— 已建立的数据库连接；
#   · table —— 要查的表名。
# 【返回值】一个字典，键是数据库列名，值是「适合展示的中文名」。
# 【为什么需要它（关键设计）】前端展示、生成检索文本时，直接显示 project_name 这种
#   英文列名对业务人员不友好。数据库里通常给每个字段写了中文注释（COLUMN_COMMENT），
#   优先用注释当展示名；若某字段没写注释，就退而用字段名本身兜底。
#   这样「中文展示名」全部来自数据库配置，代码里不写死任何业务字段名（呼应项目硬约束）。
def describe_columns(conn: pymysql.connections.Connection, table: str) -> dict[str, str]:
    """返回 {列名: 展示用中文名} 映射，优先用列注释。"""
    # 【这行在干什么】创建默认游标（字典形式，可按列名取值），块结束自动关闭。
    with conn.cursor() as cur:
        # 【这行在干什么】查询这张表所有列的「列名 + 注释」。
        # 【WHERE/ORDER BY 解释】同 discover_schema：只看当前库、限定这张表、按列顺序返回。
        # 【参数解释】(table,) —— 单元素元组，对应 SQL 里的 %s 占位符，防注入。
        cur.execute(
            """
            SELECT COLUMN_NAME, COLUMN_COMMENT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (table,),
        )
        # 【这行在干什么】把结果整理成「列名 → 中文名」的字典。
        # 【写法拆解】字典推导式，遍历每一行 r：
        #   · 键是 r["COLUMN_NAME"]（数据库列名）；
        #   · 值是一段「取中文名」的表达式，从右往左读：
        #     ① (r["COLUMN_COMMENT"] or "") —— 注释可能是 NULL，用 or "" 兜底成空串；
        #     ② .strip() —— 去掉注释首尾空白；
        #     ③ ... or r["COLUMN_NAME"] —— 若注释清理后是空（没写注释），
        #        就用列名本身当展示名兜底，保证「任何列都有一个名字可用」。
        return {r["COLUMN_NAME"]: (r["COLUMN_COMMENT"] or "").strip() or r["COLUMN_NAME"] for r in cur.fetchall()}


# 【这行在干什么】定义函数 list_tables：列出当前库下所有的表（含近似行数）。
# 【参数解释】conn —— 已建立的数据库连接。
# 【返回值】一个列表，每项是 {"table": 表名, "approx_rows": 近似行数}。
# 【为什么需要它（用途）】排查问题时常用：比如「表名是不是拼错了」「这个账号有没有
#   权限看某张表」——把库里所有表列出来，一眼就能对。属于「诊断辅助工具」。
def list_tables(conn: pymysql.connections.Connection) -> list[dict[str, Any]]:
    """列出当前库下所有表名，便于排查「表名拼错 / 无权限」类问题。"""
    # 【这行在干什么】创建默认游标，块结束自动关闭。
    with conn.cursor() as cur:
        # 【这行在干什么】从「说明书库」里查当前库下所有「真实存在的表」。
        # 【WHERE 条件解释】
        #   · TABLE_SCHEMA = DATABASE() —— 只查当前库；
        #   · TABLE_TYPE = 'BASE TABLE' —— 只要「基础表」，排除视图（VIEW）等，
        #     因为视图不是真正存数据的表，列出来会造成干扰。
        # 【ORDER BY TABLE_NAME】按表名字母序排列，结果稳定、便于对照阅读。
        cur.execute(
            """
            SELECT TABLE_NAME, TABLE_ROWS
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """
        )
        # 【这行在干什么】把结果整理成统一的列表结构返回。
        # 【写法拆解】列表推导式，遍历每一行 r：
        #   · "table" —— 表名，取 r["TABLE_NAME"]；
        #   · "approx_rows" —— 近似行数：
        #     ① r["TABLE_ROWS"] —— 来自 information_schema 的「估算行数」，
        #        对 InnoDB 这类引擎只是估计值（可能不准），但用来「判断表空不空」够用；
        #     ② or 0 —— 某些情况下它可能是 NULL，用 or 0 兜底成 0；
        #     ③ int(...) —— 统一转成整数类型。
        # 【注意事项】它是「近似值」不是精确值——真需要精确行数，得对表执行 COUNT(*)。
        return [{"table": r["TABLE_NAME"], "approx_rows": int(r["TABLE_ROWS"] or 0)} for r in cur.fetchall()]