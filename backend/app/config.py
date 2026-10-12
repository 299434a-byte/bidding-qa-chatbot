# ============================================================================
# 文件：config.py —— 全项目配置的「总控台」
#
# 【这个文件在整个项目里是干什么的】
#   项目里几乎所有能调的东西——数据库地址、向量模型名、检索条数、大模型密钥、
#   各种超时时间——统统集中在这个文件里读取和保管。它做的事情只有两件：
#     ① 从项目根目录的 .env 文件把配置「读进来」，并做类型转换（把文字变成数字/真假值/路径）；
#     ② 打包成一个不可变的「配置快照对象」Settings，供全项目共享。
#   这样别处的代码就只管「向配置要数」，不用操心「数是从哪儿读的、怎么读的」。
#
# 【谁调用它（上游）】
#   几乎所有模块 —— 想拿配置就 `from .config import get_settings`，然后 s.某个字段。
#
# 【它给谁用（下游）】
#   db.py（MySQL 连接参数）、retriever.py（检索条数）、llm.py（大模型参数）、
#   main.py（各种开关）……全项目共用一个配置对象。
#
# 【先懂的五个概念（大白话）】
#   1) .env 文件：一个纯文本文件，一行一条「键=值」，如 MYSQL_HOST=127.0.0.1。
#      把「配置」和「代码」分开存放，改配置不用改代码、不用重新开发，这是行业惯例。
#   2) 环境变量：操作系统给程序提供的「外部参数」。.env 里的键值会被加载成环境变量，
#      程序用 os.getenv("键名") 就能读到。密钥类信息放这里，不进代码、不进 Git。
#   3) dataclass（数据类）：给类贴一个标签，Python 就自动帮你生成构造函数等样板代码；
#      这里还加了 frozen=True，让对象「创建后就改不了」——见下方类定义处解释。
#   4) lru_cache：一个「结果缓存」装饰器。第一次调用函数会真正执行，之后同样参数的调用
#      直接返回缓存结果，不再执行函数体。本文件用它保证「配置只从磁盘读一次」。
#   5) 降级（graceful degradation）：某项配置没配时，程序不崩溃，而是「跳过这个功能、
#      用更弱的模式继续服务」。本文件里一堆 xxx_configured 属性就是干这个判断的。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring）】
# 【为什么放在这里】注释不算语句，所以放在这一大段注释之后，它依然是本模块的
#   第一条语句、依然是官方自我介绍，内容与原文一字不差。
"""应用配置。

所有连接信息与可调参数一律从项目根目录的 ``.env`` 读取，
代码中不出现任何主机、端口、口令等硬编码值。
"""

# 【这行在干什么】打开「类型注解延迟求值」开关。
# 【为什么这么写】本文件用了 dict[str, str]、tuple[str, ...]、Settings | None 这类新式标注，
#   老版本 Python（3.8/3.9）不认；加上这行后标注先被当成字符串存着、需要时才解析。
# 【注意事项】必须放在文件最顶部（所有执行代码之前），这是 Python 的硬性要求。
from __future__ import annotations

# 【这行在干什么】引入 os 模块（操作系统接口）。
# 【为什么需要】读环境变量要用 os.getenv()，它是这个模块提供的。
import os

# 【这行在干什么】从 dataclasses 引入 dataclass 装饰器。
# 【为什么需要】用它定义下面的 Settings 类，省去手写构造函数等样板代码。
from dataclasses import dataclass

# 【这行在干什么】从 functools 引入 lru_cache 装饰器。
# 【为什么需要】把 get_settings() 变成「带缓存」的函数，保证配置只构建一次。
#   【简称解释】LRU = Least Recently Used（最近最少使用），是一种缓存淘汰策略：
#   缓存满了就优先丢掉最久没用过的。本文件 maxsize=1（只留一个结果），淘汰策略用不上，
#   但机制是一致的。
from functools import lru_cache

# 【这行在干什么】引入 Path 类（比裸字符串更好用的路径工具）。
# 【为什么需要】拼路径、判断绝对/相对、取项目根目录，用 Path 比手工拼字符串可靠得多
#   （它会自动处理不同操作系统的路径分隔符：Windows 用 \、Linux 用 /）。
from pathlib import Path

# 【这行在干什么】从第三方库 python-dotenv 引入 load_dotenv 函数。
# 【为什么需要】它能读取 .env 文件，并把里面的「键=值」加载成环境变量，
#   之后 os.getenv 就能读到。没有它，.env 文件里的配置就白写了。
from dotenv import load_dotenv

# 【这行在干什么】算出「项目根目录」的位置，存成常量 PROJECT_ROOT。
# 【写法逐步拆解】
#   · __file__ —— 本文件自己的路径（config.py 的完整路径）；
#   · Path(__file__) —— 把它包成 Path 对象，方便做路径运算；
#   · .resolve() —— 把它转成「绝对路径」并解析掉符号链接（得到最实在的地址）；
#   · .parents[2] —— 往上找两级父目录。parents[0] 是上一级（app/），
#     parents[1] 是再上一级（backend/），parents[2] 就是项目根目录（bidding-qa-chatbot/）。
# 【为什么必须这样动态算（重要设计）】如果写死 "d:/rag_agent/..."，换台电脑、换个目录
#   就全坏了。用「以本文件为锚点往上找」的方式，无论项目被放到哪里都能正确定位，
#   这叫「位置无关」。
# backend/app/config.py -> 上溯三层即项目根目录 bidding-qa-chatbot/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 【这行在干什么】拼出 .env 文件的完整路径（项目根目录下，文件名就叫 .env）。
# 【为什么单独存成常量】下面加载它、以及别的地方可能需要引用它，用常量避免重复计算。
ENV_FILE = PROJECT_ROOT / ".env"

# 【这行在干什么】正式加载 .env 文件，把里面的配置灌进「环境变量」。
# 【为什么要放在模块顶层立刻执行（重要）】必须在任何读取配置的代码之前执行，
#   否则下面读环境变量时会什么都读不到（配置还没加载进来）。
# 【注意事项】如果 .env 文件不存在，load_dotenv 不会报错，只是什么都不加载；
#   于是所有配置都取默认值，程序依然能启动（可能功能降级）。
load_dotenv(ENV_FILE)

# 【这行在干什么】定义三张业务表的「默认中文展示名」。
# 【为什么需要默认值】界面上要给用户看中文表名（如「招标公告」），不能显示 bidding_notices。
#   这里给一份内置默认，开箱即用；若运维想改（比如加一张新表），可用 .env 里的
#   TABLE_LABELS 覆盖（见下面的 _parse_table_labels）。
# 【结构解释】字典：键是数据库里的英文表名，值是给用户看的中文名。
# 【注意事项】这里只是「默认值」，不是硬性绑定——表结构变了、改名了，
#   可以不改代码、只改配置。这正是「不硬编码业务字段」约束的体现。
# 三张业务表的默认展示名；可用 .env 中的 TABLE_LABELS 覆盖
DEFAULT_TABLE_LABELS = {
    "bidding_notices": "招标公告",
    "bidding_project": "招投标项目",
    "companies": "公司信息",
}


# 【这行在干什么】定义工具函数 _str：从环境变量里读一个「字符串」配置。
# 【名字前下划线的含义】约定俗称：下划线开头表示「本模块内部专用」，不对外承诺使用。
# 【参数逐个解释】
#   · key —— 环境变量的键名（如 "MYSQL_HOST"）；
#   · default="" —— 读不到时用这个默认值（默认是空字符串）。
# 【返回值】一个「干净」的字符串（去掉了首尾空白）。
# 【为什么不直接 os.getenv】因为直接读会踩两个坑：①变量不存在时返回 None，
#   后续用字符串方法会报错；②值可能带着多余空格。这个函数把这两件事一次性处理掉。
def _str(key: str, default: str = "") -> str:
    # 【这行在干什么】读取环境变量 key 的值。
    # 【为什么可能是 None】该键在环境里不存在时，getenv 返回 None（而不是空字符串）。
    value = os.getenv(key)
    # 【这行在干什么】判断并返回最终结果。
    # 【条件逐个解释】`value is None or not value.strip()`：
    #   · value is None —— 键根本不存在；
    #   · not value.strip() —— 值存在但「去掉首尾空白后是空」（比如配置里手滑写了几个空格）。
    #   只要满足其一，就返回默认值。
    # 【为什么否则要 .strip()】正常值时也顺手去掉首尾空白，防止「MYSQL_HOST = 127.0.0.1 」
    #   这种配置意外带了空格，导致连接失败这种极其难查的问题。
    return default if value is None or not value.strip() else value.strip()


# 【这行在干什么】定义工具函数 _int：从环境变量里读一个「整数」配置。
# 【参数逐个解释】
#   · key —— 环境变量的键名；
#   · default —— 必须传入的默认整数（读不到/解析失败时用它）。
# 【为什么 default 没有写 "=..." 默认值】强制调用方明确写下「读不到时该用什么数」，
#   避免大家偷懒不写默认值、出错时拿到一个莫名其妙的 0。
def _int(key: str, default: int) -> int:
    # 【这行在干什么】先用 _str 读出原始的字符串值（顺便处理掉 None 和空格问题）。
    raw = _str(key)
    # 【这行在干什么】打开「接住岔子」的网，尝试把字符串转成整数。
    # 【为什么可能失败】如果配置里写的是 "abc" 或 "12.5"，int() 会抛 ValueError。
    try:
        # 【这行在干什么】把字符串转成整数（如 "3306" → 3306）。
        return int(raw)
    # 【这行在干什么】转换失败时，不崩溃，改用默认值。
    # 【为什么容错而不是报错（设计取舍）】一个配置项写错不该让整个服务起不来；
    #   退回默认值让服务照常运行，运维可通过 /api/config 接口发现「配置没生效」再修正。
    except ValueError:
        return default


# 【这行在干什么】定义工具函数 _float：从环境变量里读一个「小数」配置。
# 【参数逐个解释】key —— 键名；default —— 读不到/解析失败时用的默认小数值。
# 【为什么需要它】像温度 0.2、BM25 的 b=0.75 这类配置是小数，必须用 float 转换。
def _float(key: str, default: float) -> float:
    # 【这行在干什么】先读出原始字符串。
    raw = _str(key)
    # 【这行在干什么】尝试转成小数。
    try:
        # 【这行在干什么】"0.75" → 0.75。
        return float(raw)
    # 【这行在干什么】失败时回落默认值（同 _int 的容错理由）。
    except ValueError:
        return default


# 【这行在干什么】定义工具函数 _bool：从环境变量里读一个「开关」（真/假）配置。
# 【参数逐个解释】key —— 键名；default —— 值不明确时用的默认真假值。
# 【为什么需要它（重点）】环境变量本质上都是字符串，没有「布尔类型」。
#   不同人写开关的习惯五花八门："1"、"true"、"yes"、"开"…… 这个函数统一认几种常见写法，
#   避免「明明写了 true 却被当成假」的坑。
def _bool(key: str, default: bool) -> bool:
    """布尔开关：'1/true/yes/on' 为真，'0/false/no/off' 为假，其余回落默认值。"""
    # 【这行在干什么】读出原始字符串，并统一转成小写。
    # 【为什么转小写】这样 "TRUE"、"True"、"true" 都能被识别，不区分大小写更友好。
    raw = _str(key).lower()
    # 【这行在干什么】如果值是这几种「真」的写法之一，返回真。
    # 【写法解释】`raw in {"1", "true", "yes", "on"}` 是「集合成员判断」：
    #   检查 raw 是否在右边这个集合里，这在集合上比在列表上快得多。
    if raw in {"1", "true", "yes", "on"}:
        return True
    # 【这行在干什么】如果值是这几种「假」的写法之一，返回假。
    if raw in {"0", "false", "no", "off"}:
        return False
    # 【这行在干什么】其他任何值（包括没配置导致的空字符串），一律回落默认值。
    # 【为什么这么处理】写了个不认识的值（比如 "maybe"）时，与其猜测用户意图，
    #   不如用明确的默认值，行为可预期。
    return default


# 【这行在干什么】定义工具函数 _path：从环境变量里读一个「文件/目录路径」配置。
# 【参数逐个解释】key —— 键名；default —— 读不到时用的默认路径字符串（相对项目根目录）。
# 【返回值】一个 Path 对象（绝对路径）。
# 【为什么需要专门处理路径（重要）】.env 里通常写相对路径（如 ./data/qdrant），
#   但程序的工作目录不确定（可能从 backend 启动、也可能从别处启动）。
#   若直接用相对路径，会「跑到别的地方去找文件」。所以这里统一规定：
#   相对路径一律以「项目根目录」为基准解析，保证无论从哪儿启动都指向同一个位置。
def _path(key: str, default: str) -> Path:
    """相对路径统一以项目根目录为基准解析。"""
    # 【这行在干什么】读出配置字符串，并包成 Path 对象（此时可能还是相对路径）。
    raw = Path(_str(key, default))
    # 【这行在干什么】判断并换算成绝对路径。
    # 【条件逐个解释】
    #   · raw.is_absolute() —— 如果配置里写的就是绝对路径（如 D:/data），那就直接用它；
    #   · 否则 —— 把「项目根目录」和这个相对路径拼起来，再 .resolve() 得到规范的绝对路径。
    # 【举例】项目在 d:/rag_agent/bidding-qa-chatbot，配置是 ./data/qdrant 时，
    #   结果是 d:/rag_agent/bidding-qa-chatbot/data/qdrant。
    return raw if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()


# 【这行在干什么】定义工具函数 _parse_table_labels：把 .env 里写的一串「表名=中文名」
#   解析成字典。
# 【参数解释】raw —— 原始字符串，形如 "bidding_project=项目,companies=公司"。
# 【返回值】一个字典：键是表名、值是中文展示名。
# 【为什么要这么设计（灵活配置）】如果将来要加一张新表，不必改代码，
#   只要在 .env 的 TABLE_LABELS 里补一段「表名=中文名」就行。
def _parse_table_labels(raw: str) -> dict[str, str]:
    # 【这行在干什么】先复制一份默认标签字典作为起点。
    # 【为什么用 dict(DEFAULT_TABLE_LABELS) 复制一份】不能直接改原字典
    #   （那会污染全局常量，下次调用就全乱了）；复制一份来改，安全。
    labels = dict(DEFAULT_TABLE_LABELS)
    # 【这行在干什么】按逗号把原始字符串拆成一段段，逐段解析。
    for item in raw.split(","):
        # 【这行在干什么】去掉这一段的首尾空白。
        item = item.strip()
        # 【这行在干什么】跳过「空段」或「不含等号的段」。
        # 【为什么不含等号就跳过】合法格式必须是「表名=中文名」；没有等号就不是有效配置，
        #   直接忽略，防止后面解析出错误结果。
        if not item or "=" not in item:
            continue
        # 【这行在干什么】用 partition("=") 把这一段从「第一个等号」处切成三部分。
        # 【写法拆解】name, _, label = item.partition("=")：
        #   · partition 总是返回三个值：等号左边、等号本身、等号右边；
        #   · 用 _ 接住中间那个「等号」——它是我们不需要的，用下划线表示「占位、丢弃」。
        # 【为什么用 partition 而不是 split("=")】如果中文名里本身含等号，
        #   split 会切成多段、结果错乱；partition 只按第一个等号切，更稳妥。
        name, _, label = item.partition("=")
        # 【这行在干什么】只有「表名」和「中文名」两边都非空时，才把它写进字典。
        # 【为什么必须校验（重要）】防止手滑写成 "=公司" 或 "companies=" 这种半截配置，
        #   把字典里原本正确的默认值给覆盖成一个空字符串。
        if name.strip() and label.strip():
            labels[name.strip()] = label.strip()
    # 【这行在干什么】返回解析好的完整标签字典。
    return labels


def _parse_primary_keys(raw: str) -> dict[str, str]:
    """解析「表名=列名」映射，用于给「无主键表」声明主键兜底。

    【什么时候用得上】PG 里若某张表没有 PRIMARY KEY 约束，建库管道就不知道该拿
    哪一列当 doc_id 的稳定标识，只能退化成「行号+1」——而行号依赖查询顺序，
    重跑很可能产生重复点。KB_PRIMARY_KEYS 允许手写一份兜底声明，形如：
        KB_PRIMARY_KEYS=procurement_notices=project_name,projects=project_name
    【解析规则】与 TABLE_LABELS 完全一致：逗号分隔、只按第一个等号切、两边都非空才收。
    """
    # 结果字典从空开始（一张表都没声明兜底键时就是空字典）。
    result: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        # 跳过空段与不含等号的非法段。
        if not item or "=" not in item:
            continue
        # 只按「第一个等号」切，避免列名里含等号时切错。
        table, _, column = item.partition("=")
        # 表名与列名都非空才收，防止半截配置污染结果。
        if table.strip() and column.strip():
            result[table.strip()] = column.strip()
    return result


# 【这行在干什么】@dataclass 让 Python 自动生成构造函数等样板代码；
#   frozen=True 则让这个类的实例「创建后不可修改」。
# 【为什么 frozen=True 很重要（关键设计）】配置对象会被全项目到处共享。
#   如果有人不小心在某处改了 s.mysql_host，会「牵一发而动全身」地影响所有模块，
#   这种 bug 极难排查。冻结后一旦有人尝试修改，程序会立刻报错（而不是悄悄改坏），
#   相当于给配置上了把锁。这与「配置是只读快照」的定位完全吻合。
@dataclass(frozen=True)
class Settings:
    """运行期配置快照。"""

    # 【这行在干什么】纯注释，标出下面一组字段属于「MySQL 数据源」配置。
    # MySQL 数据源
    # 【这行在干什么】MySQL 服务器地址（如 127.0.0.1）；从 .env 的 MYSQL_HOST 读。
    mysql_host: str
    # 【这行在干什么】MySQL 端口（默认 3306）；从 MYSQL_PORT 读。
    mysql_port: int
    # 【这行在干什么】MySQL 登录用户名；从 MYSQL_USER 读。
    mysql_user: str
    # 【这行在干什么】MySQL 登录密码；从 MYSQL_PASSWORD 读（密钥，绝不写死在代码里）。
    mysql_password: str
    # 【这行在干什么】要连接的数据库名（默认 bidding_data）；从 MYSQL_DATABASE 读。
    mysql_database: str
    # 【这行在干什么】字符集（默认 utf8mb4，能正确处理中文与 emoji）；从 MYSQL_CHARSET 读。
    mysql_charset: str
    # 【这行在干什么】连接超时秒数；从 MYSQL_CONNECT_TIMEOUT 读。
    # 【为什么需要】网络异常时避免程序无限等待、把请求卡死。
    mysql_connect_timeout: int
    # 【这行在干什么】要纳入知识库的表名清单，如 ("bidding_notices", ...)。
    # 【为什么是元组】表清单是「一组固定不变的名字」，元组不可变，语义最贴切。
    kb_tables: tuple[str, ...]
    # 【这行在干什么】「表名 → 中文展示名」的映射字典；从 TABLE_LABELS 读（可覆盖默认）。
    table_labels: dict[str, str]
    # 【这行在干什么】建库管道的数据源开关；从 KB_SOURCE 读。
    # 【取值】mysql（默认，行为与改动前完全一致）/ postgres（改读本机 PG 副本）。
    kb_source: str
    # 【这行在干什么】「表名 → 兜底主键列名」映射；从 KB_PRIMARY_KEYS 读。
    # 【为什么需要】PG 某表没有 PRIMARY KEY 约束时，用它声明该拿哪一列当稳定标识。
    kb_primary_keys: dict[str, str]

    # 【这行在干什么】纯注释，标出下面一组字段属于「向量库」配置。
    # 向量库
    # 【这行在干什么】向量库（Qdrant）本地数据目录路径；从 QDRANT_PATH 读。
    # 【注意事项】本项目无 Docker，用的是 Qdrant 嵌入式本地模式，该目录同一时刻
    #   只能被一个进程打开（单进程锁）。这是项目记忆里明确的环境坑。
    qdrant_path: Path
    # 【这行在干什么】向量库里的「集合名」（相当于一张表）；从 QDRANT_COLLECTION 读。
    qdrant_collection: str

    # 【这行在干什么】纯注释，标出下面一组字段属于「向量化（Embedding）」配置。
    # 向量化
    # 【这行在干什么】向量模型名（默认 BAAI/bge-small-zh-v1.5，中文小模型）；从 EMBED_MODEL 读。
    embed_model: str
    # 【这行在干什么】向量维度（默认 512）；从 EMBED_DIM 读。
    # 【为什么必须配准（关键）】向量库里存的向量维度必须与建库时完全一致，
    #   否则检索时「维数对不上」会直接报错。
    embed_dim: int
    # 【这行在干什么】一次给模型送多少条做向量化；从 EMBED_BATCH_SIZE 读。
    embed_batch_size: int
    # 【这行在干什么】给「查询」文字加的前缀（默认「为这个句子生成表示以用于检索相关文章：」）。
    # 【为什么查询和文档要区别对待（重要）】bge 这类中文模型在训练时就约定：
    #   查询文本要加特定前缀，文档不加，这样查询和文档的向量才在同一「语义坐标系」里，
    #   相似度才有意义。忘了加前缀会导致检索质量明显下降，且不报错、极难发现。
    embed_query_prefix: str

    # 【这行在干什么】纯注释，标出下面一组字段属于「BM25 关键词检索」配置。
    # BM25
    # 【这行在干什么】BM25 索引文件的保存路径；从 BM25_INDEX_PATH 读。建库写入、检索读取。
    bm25_index_path: Path
    # 【这行在干什么】BM25 参数 k1（词频饱和度，默认 1.5）；从 BM25_K1 读。
    bm25_k1: float
    # 【这行在干什么】BM25 参数 b（文档长度惩罚强度，默认 0.75）；从 BM25_B 读。
    # 【注意事项】建库与检索必须用同一套 k1/b，否则打分口径不一致、排序混乱。
    bm25_b: float

    # 【这行在干什么】纯注释，标出下面一组字段属于「检索」配置。
    # 检索
    # 【这行在干什么】向量（语义）一路召回多少条；从 RETRIEVE_DENSE_TOP_K 读。
    retrieve_dense_top_k: int
    # 【这行在干什么】BM25（关键词）一路召回多少条；从 RETRIEVE_SPARSE_TOP_K 读。
    retrieve_sparse_top_k: int
    # 【这行在干什么】两路融合、重排之后，最终送进大模型的条数；从 RETRIEVE_FINAL_TOP_K 读。
    retrieve_final_top_k: int
    # 【这行在干什么】RRF 融合公式里的常数 k（默认 60）；从 RRF_K 读。
    # 【它的作用】调节「排名靠前」的加分幅度，60 是业界常用默认值，一般不用改。
    rrf_k: int
    # 【这行在干什么】语义一路的「最低相似度门槛」（默认 0.35）；从 DENSE_MIN_SCORE 读。
    # 【为什么需要门槛（重要）】余弦相似度低的候选其实是「完全不相关」的噪声，
    #   设个门槛把它们剔掉，能显著减少「答非所问」。见 retriever.py 里的过滤逻辑。
    dense_min_score: float

    # 【这行在干什么】纯注释，标出下面一组字段属于「重排序」配置。
    # 重排序（对 RRF 融合结果做二次排序）
    # 【这行在干什么】进入重排的候选池大小；从 RERANK_CANDIDATES 读。
    # 【为什么它要大于最终条数】重排是「从候选里挑最好的」，池子太小就没得挑。
    rerank_candidates: int
    # 【这行在干什么】重排用的模型名（留空则回落到主大模型，见属性 rerank_model_or_default）。
    rerank_model: str
    # 【这行在干什么】送进重排的每条候选最多保留多少字符；从 RERANK_MAX_CHARS 读。
    # 【为什么要截断】控制提示词长度、省 token、避免超时。
    rerank_max_chars: int
    # 【这行在干什么】重排的超时秒数（默认 45）；从 RERANK_TIMEOUT 读。
    # 【为什么是 45 秒这么大（血泪教训）】项目记忆记载：设为 20 秒时推理模型来不及返回，
    #   会导致「静默降级」（悄悄退回 RRF 顺序、用户以为重排生效了）。
    #   45 秒是实测能稳定完成的经验值，属硬约束，不要随意调小。
    rerank_timeout: int
    # 【重排按需触发】RERANK_MODE = auto | always | off（默认 auto）；从 RERANK_MODE 读。
    #   auto   —— 只在「RRF 顺序不可靠」时才花一次大模型调用去重排（见 rerank.should_rerank）；
    #   always —— 保持旧行为：候选够多就一律重排（向后兼容，评测对比用）；
    #   off    —— 从不重排，直接用 RRF 顺序（纯基线，评测对比用）。
    rerank_mode: str
    # 【重排按需触发·阈值一】RRF 头两名的「相对分差」低于该值才算顺序不可靠（默认 0.1）。
    #   相对分差 = (top1 - top2) / top1；RRF 分是名次倒数之和，量纲固定，适合用相对差。
    rerank_auto_gap: float
    # 【重排按需触发·阈值二】融合第一名的 dense 相似度低于该值才算「召回质量存疑」（默认 0.35）。
    #   0.35 与 DENSE_MIN_SCORE 的推荐门槛一致（见上面 dense_min_score 注释）。
    rerank_auto_min_dense: float

    # 【这行在干什么】纯注释，标出下面一组字段属于「长文本切块」配置。
    # 切块
    # 【这行在干什么】一段文本超过多少字符才需要切块（默认 1200）；从 MAX_DOC_CHARS 读。
    max_doc_chars: int
    # 【这行在干什么】切块时每块最多多少字符（默认 500）；从 CHUNK_SIZE 读。
    chunk_size: int
    # 【这行在干什么】相邻两块之间故意重叠多少字符（默认 80）；从 CHUNK_OVERLAP 读。
    # 【为什么必须重叠（重要）】若严格按 500 一刀切，关键句子可能正好被切断在两块之间，
    #   结果两块都检索不到完整语义。留 80 字符重叠，让边界内容在两块里都出现，兜住这种情况。
    chunk_overlap: int

    # 【这行在干什么】纯注释，标出下面一组字段属于「大模型」配置。
    # 大模型
    # 【这行在干什么】大模型接口地址（如 https://api.deepseek.com/v1）；从 LLM_BASE_URL 读。
    llm_base_url: str
    # 【这行在干什么】大模型 API 密钥；从 LLM_API_KEY 读（密钥，绝不写死在代码里）。
    llm_api_key: str
    # 【这行在干什么】实际调用的模型 id（如 deepseek-flash）；从 LLM_MODEL 读。
    llm_model: str
    # 【这行在干什么】仅用于界面「展示」的模型名；从 LLM_MODEL_DISPLAY 读。
    # 仅用于界面展示的名字（与真实 API model id 解耦）；留空则显示 llm_model
    # 【为什么需要它（重要背景）】本项目想把界面上的模型显示成「DeepSeek-V4.1-Flash」，
    #   但这个名字在 API 端并不存在（API 只认 deepseek-flash / deepseek-v4-pro）。
    #   于是把「真实调用名」和「界面展示名」拆开：调用用 llm_model，界面显示用这个。
    #   这样既能正常调用，又能给用户一个好看的名字。
    llm_model_display: str
    # 【这行在干什么】生成温度（默认 0.2）；从 LLM_TEMPERATURE 读。
    # 【它的作用】控制输出随机性：越低越稳定保守、越高越有创造性。
    #   招投标问答要求「只依据资料、不发挥」，所以设得偏低。
    llm_temperature: float
    # 【这行在干什么】回答最多生成多少 token（默认 1024）；从 LLM_MAX_TOKENS 读。
    # 【注意事项】项目记忆里提到：1024 在「引用 5 条来源」时会不够用，
    #   导致 finish_reason=length、内容被截断甚至为空，实际部署调大到了 3072。
    llm_max_tokens: int
    # 【这行在干什么】大模型请求超时秒数；从 LLM_TIMEOUT 读。
    llm_timeout: int

    # 【这行在干什么】纯注释，标出下面一组字段属于「Agent」配置。
    # Agent（把 RAG 检索注册成工具，由大模型自主决定是否调用）
    # 【这行在干什么】Agent 用哪个模型（留空回落到主模型，见 agent_model_or_default）。
    agent_model: str
    # 【这行在干什么】Agent 单次生成的最大 token 数；从 AGENT_MAX_TOKENS 读。
    agent_max_tokens: int
    # 【这行在干什么】带多少轮历史对话进 Agent 上下文；从 AGENT_HISTORY_TURNS 读。
    # 【为什么不能太多】历史越长，提示词越占 token、越慢、越贵，所以要限制轮数。
    agent_history_turns: int
    # 【这行在干什么】Agent 最多能「思考-调用工具」多少步（默认 6）；从 AGENT_MAX_STEPS 读。
    # 【为什么是 6（血泪教训）】项目记忆记载：设成 3 时，需要多次工具调用的任务会撞上
    #   「Recursion limit（递归上限）」错误，所以调大到 6。这是硬约束。
    agent_max_steps: int

    # 【这行在干什么】纯注释，标出下面一组字段属于「PostgreSQL」配置。
    # PostgreSQL（与向量检索并行的结构化明细查询，做统计/精确筛选）
    # 【这行在干什么】PostgreSQL 地址（默认 127.0.0.1）；从 PG_HOST 读。
    pg_host: str
    # 【这行在干什么】PostgreSQL 端口（默认 5432）；从 PG_PORT 读。
    pg_port: int
    # 【这行在干什么】PostgreSQL 用户名；从 PG_USER 读。
    pg_user: str
    # 【这行在干什么】PostgreSQL 密码；从 PG_PASSWORD 读（密钥）。
    # 【注意事项】项目记忆提到：本机 PG_USER/PG_PASSWORD 为空、服务未监听，
    #   所以该工具当前不会被注册（见 pg_configured 判断）。
    pg_password: str
    # 【这行在干什么】数据库名（默认 chatbot）；从 PG_DATABASE 读。
    pg_database: str
    # 【这行在干什么】模式（schema）名（默认 public）；从 PG_SCHEMA 读。
    pg_schema: str
    # 【这行在干什么】允许查询的表清单（可为空表示不限）；从 PG_TABLES 读。
    pg_tables: tuple[str, ...]
    # 【这行在干什么】连接超时秒数；从 PG_CONNECT_TIMEOUT 读。
    pg_connect_timeout: int
    # 【这行在干什么】单条 SQL 在数据库侧的「执行超时」秒数；从 PG_STATEMENT_TIMEOUT 读。
    # 【为什么需要】防止一条慢查询把数据库连接长时间占住，影响其他请求。
    pg_statement_timeout: int
    # 【这行在干什么】查询结果最多返回多少行（默认 50）；从 PG_MAX_ROWS 读。
    pg_max_rows: int
    # 【这行在干什么】生成 SQL 用的模型（留空回落主模型）。
    pg_model: str
    # 【这行在干什么】生成 SQL 时最大 token 数（默认 3072）；从 PG_MAX_TOKENS 读。
    pg_max_tokens: int
    # 【这行在干什么】生成 SQL 的超时秒数（默认 45）；从 PG_TIMEOUT 读。
    pg_timeout: int
    # 【这行在干什么】表结构信息的缓存时长秒数（默认 600）；从 PG_SCHEMA_TTL 读。
    # 【为什么需要缓存】读取表结构（含字段注释、示例行）是要查库的，比较慢；
    #   表结构又很少变，缓存 10 分钟能显著降低开销。
    pg_schema_ttl: int

    # 【这行在干什么】纯注释，标出下面一组字段属于「Neo4j 知识图谱」配置。
    # Neo4j 知识图谱（实体关系查询，与向量检索、结构化查询并行）
    # 【这行在干什么】Neo4j 连接地址（如 bolt://127.0.0.1:7687）；从 NEO4J_URI 读。
    neo4j_uri: str
    # 【这行在干什么】Neo4j 用户名；从 NEO4J_USER 读。
    neo4j_user: str
    # 【这行在干什么】Neo4j 密码；从 NEO4J_PASSWORD 读（密钥）。
    neo4j_password: str
    # 【这行在干什么】Neo4j 的数据库名（默认 neo4j）；从 NEO4J_DATABASE 读。
    neo4j_database: str
    # 【这行在干什么】建连/事务重试超时秒数；从 NEO4J_TIMEOUT 读。
    # 【注意】它不是「一条查询能跑多久」的上限——查询上限看 neo4j_query_timeout。
    neo4j_timeout: int
    # 【这行在干什么】单条查询的事务超时秒数（服务端到点终止）；从 NEO4J_QUERY_TIMEOUT 读。
    # 【为什么单独一项】NEO4J_TIMEOUT 只管建连，慢查询需要独立的执行时长上限；
    #   配成 0 或负数 = 不限时（沿用 Neo4j 库默认）。
    neo4j_query_timeout: int
    # 【这行在干什么】图谱查询结果最多返回多少行；从 NEO4J_MAX_ROWS 读。
    neo4j_max_rows: int
    # 【这行在干什么】生成 Cypher 查询语句用的模型（留空回落主模型）。
    graph_model: str
    # 【这行在干什么】生成 Cypher 时最大 token 数；从 GRAPH_MAX_TOKENS 读。
    graph_max_tokens: int
    # 【这行在干什么】生成 Cypher 的超时秒数；从 GRAPH_TIMEOUT 读。
    graph_timeout: int
    # 【这行在干什么】图谱结构信息的缓存时长秒数；从 GRAPH_SCHEMA_TTL 读。
    graph_schema_ttl: int

    # 【这行在干什么】纯注释，标出下面一组字段属于「图片解析」配置。
    # 图片解析（智谱视觉模型，POST /analyze-image）
    # 【这行在干什么】智谱 AI 的 API 密钥；从 ZHIPUAI_API_KEY 读（密钥，绝不硬编码）。
    zhipuai_api_key: str
    # 【这行在干什么】视觉模型名（默认 glm-4.6v-flashx）；从 VISION_MODEL 读。
    vision_model: str
    # 【这行在干什么】图片解析超时秒数；从 VISION_TIMEOUT 读。
    vision_timeout: int
    # 【这行在干什么】允许上传的图片最大字节数（默认 10MB）；从 VISION_MAX_BYTES 读。
    # 【写法解释】10 * 1024 * 1024 就是 10MB（乘两次 1024：KB→MB）。
    vision_max_bytes: int

    # 【这行在干什么】纯注释，标出下面一组字段属于「联网搜索」配置。
    # 联网搜索（Tavily；本地知识库无答案时补充最新公告与法规更新）
    # 【这行在干什么】Tavily 搜索服务的 API 密钥；从 TAVILY_API_KEY 读（密钥）。
    tavily_api_key: str
    # 【这行在干什么】搜索深度（默认 basic）；从 TAVILY_SEARCH_DEPTH 读。
    tavily_search_depth: str
    # 【这行在干什么】最多返回多少条搜索结果（默认 5）；从 TAVILY_MAX_RESULTS 读。
    tavily_max_results: int
    # 【这行在干什么】搜索超时秒数；从 TAVILY_TIMEOUT 读。
    tavily_timeout: int
    # 「联网搜索」开关的默认值（前端每次请求可覆盖）
    # 【这行在干什么】「联网搜索」总开关的默认值（默认开）；从 WEB_SEARCH_DEFAULT 读。
    # 【为什么前端还能覆盖】前端页面有「联网搜索」勾选项，每次请求都会带自己的选择，
    #   这里的值只是「用户没明确选时」的兜底。
    web_search_default: bool

    # 【这行在干什么】纯注释，标出下面一组字段属于「对话历史」配置。
    # 对话历史（SQLite 本地持久化；无外部服务依赖）
    # 【这行在干什么】是否开启历史存储（默认开）；从 HISTORY_ENABLED 读。
    history_enabled: bool
    # 【这行在干什么】历史数据库（SQLite 文件）的路径；从 HISTORY_DB_PATH 读。
    history_db_path: Path
    # 【这行在干什么】历史列表每页显示多少条（默认 20）；从 HISTORY_PAGE_SIZE 读。
    history_page_size: int
    # 【这行在干什么】搜索历史时最多返回多少条；从 HISTORY_SEARCH_LIMIT 读。
    history_search_limit: int
    # 【这行在干什么】自动生成的会话标题最多多少字符（默认 30）；从 HISTORY_TITLE_MAX_CHARS 读。
    history_title_max_chars: int
    # 【这行在干什么】单条消息最多保存多少字符（默认 20000）；从 HISTORY_MAX_MESSAGE_CHARS 读。
    # 【为什么限制（重要）】防止超长回答（或粘贴进来的大段文本）把数据库撑爆、
    #   拖慢查询；超出部分会被截断后再存。
    history_max_message_chars: int

    # 【这行在干什么】纯注释，标出下面一组字段属于「账号体系」配置。
    # 账号体系（注册/登录/令牌；跨设备恢复对话历史的前置条件）
    # 【这行在干什么】是否开启账号体系（默认开）；从 AUTH_ENABLED 读。
    auth_enabled: bool
    # 【这行在干什么】登录令牌的有效期天数（默认 30）；从 AUTH_TOKEN_TTL_DAYS 读。
    auth_token_ttl_days: int
    # 【这行在干什么】密码加密（PBKDF2）的迭代次数（默认 20 万次）；从 AUTH_PBKDF2_ITERATIONS 读。
    # 【它是什么（大白话）】把密码「反复搅拌」20 万次再存，让暴力破解的成本变得极高。
    #   次数越多越安全，但也越慢；20 万是常用的平衡点。
    auth_pbkdf2_iterations: int
    # 【这行在干什么】连续登录失败多少次后锁定账号（默认 5）；从 AUTH_MAX_LOGIN_ATTEMPTS 读。
    auth_max_login_attempts: int
    # 【这行在干什么】锁定时长秒数（默认 300=5 分钟）；从 AUTH_LOCKOUT_SECONDS 读。
    auth_lockout_seconds: int

    # 【这行在干什么】纯注释，标出下面一组字段属于「问答缓存 / 指标 / 日志」配置。
    # 问答缓存 / 可观测性指标 / 日志（都是本地产物，统一落在 backend/data/ 目录下）
    # 【这行在干什么】问答缓存的 SQLite 库文件路径；从 CACHE_DB_PATH 读。
    # 【默认值】未配置时落在 backend/data/answer_cache.db。
    cache_db_path: Path
    # 【这行在干什么】缓存有效期秒数（默认 3600 = 1 小时）；从 CACHE_TTL 读。
    # 【作用】超过这个时长的缓存条目视为过期，查询时当作「未命中」。
    cache_ttl: int
    # 【这行在干什么】问答指标的 SQLite 库文件路径；从 METRICS_DB_PATH 读。
    # 【默认值】未配置时落在 backend/data/metrics.db。
    metrics_db_path: Path
    # 【这行在干什么】日志级别（默认 INFO）；从 LOG_LEVEL 读。取值如 DEBUG / INFO / WARNING / ERROR。
    log_level: str
    # 【这行在干什么】日志文件路径；从 LOG_FILE_PATH 读。默认落在 backend/data/app.log。
    log_file_path: Path

    # 【这行在干什么】纯注释，标出下面一组字段属于「应用层限流」配置。
    # 应用层限流（内存令牌桶，见 app/ratelimit.py）
    # 【这行在干什么】令牌桶容量：单个「IP+用户」键瞬时允许的突发请求次数（默认 10）；
    #   从 RATELIMIT_CAPACITY 读。
    ratelimit_capacity: int
    # 【这行在干什么】每秒补充的令牌数（默认 0.5 = 每 2 秒回 1 枚，稳态约 30 次/分钟）；
    #   从 RATELIMIT_REFILL_PER_SEC 读。
    ratelimit_refill_per_sec: float

    # 【这行在干什么】纯注释，标出下面一组字段属于「跨域（CORS）」配置。
    # 跨域（CORS）—— 允许哪些前端来源跨域调用本 API
    # 【这行在干什么】跨域来源白名单；从 CORS_ORIGINS 读（逗号分隔）。
    # 【为什么不用 "*"】"*" 等于放行任意网站，任何站点都能借用户浏览器调用本接口；
    #   白名单只放行自己信任的前端地址。默认仅本机前端的两个常见地址。
    cors_origins: tuple[str, ...]

    # 【这行在干什么】纯注释，标出下面一组字段属于「深度推理模式」配置。
    # 深度推理模式（Plan-and-Solve + Reflection，由前端模式选择器触发）
    # 【这行在干什么】深度推理最多几个大步骤（默认 4）；从 DEEP_MAX_STEPS 读。
    # 【名词解释】Plan-and-Solve = 「先做计划，再按计划分步解决」的推理策略；
    #   Reflection = 「做完后自我检查、必要时修正」。
    deep_max_steps: int
    # 【这行在干什么】深度推理的每个子任务内部最多几步（默认 6）；从 DEEP_STEP_MAX_STEPS 读。
    # 【与 agent_max_steps 同类（血泪教训）】也要给足步数，否则子任务内多次工具调用会撞递归上限。
    deep_step_max_steps: int
    # 【这行在干什么】最多做几轮「自我反思修正」（默认 1）；从 DEEP_REFLECT_ROUNDS 读。
    deep_reflect_rounds: int
    # 【这行在干什么】深度推理单次生成最大 token 数；从 DEEP_MAX_TOKENS 读。
    deep_max_tokens: int
    # 【这行在干什么】深度推理整体超时秒数（默认 120）；从 DEEP_TIMEOUT 读。
    # 【为什么给得比较宽】深度推理要做多步、多轮，天然比普通问答慢，超时自然要更宽松。
    deep_timeout: int
    # 【这行在干什么】子任务「观察结果」最多保留多少字符（默认 1200）；从 DEEP_OBSERVATION_CHARS 读。
    # 【为什么截断（重要）】子任务的中间结论会拼进后续提示词；不截断的话，
    #   多轮累积下来提示词会被撑得极长，既慢又容易超 token 上限。
    deep_observation_chars: int

    # 【这行在干什么】@property 把下面的方法包装成「属性」，调用时写 s.llm_configured
    #   而不是 s.llm_configured()。
    # 【为什么需要这类属性（贯穿全文件的设计）】项目要求「配置不齐时不要报错崩溃，
    #   而是自动降级」。把「某项功能配好了没有」做成统一属性，各模块就能在动手前
    #   一眼判断（如「大模型没配 → 走仅检索模式」），降级逻辑集中、不易漏。
    @property
    def llm_configured(self) -> bool:
        """未配置时不调用大模型，自动降级为「仅检索」模式。"""
        # 【这行在干什么】判断三个关键项是否都非空：接口地址、密钥、模型名。
        # 【写法拆解】bool(a and b and c)：
        #   · 用 and 串起来，只要有一项是空字符串（假），整体就是假；
        #   · 外层 bool(...) 保证返回规规矩矩的 True/False，而不是那个空字符串本身。
        # 【为什么三项缺一不可】少任何一项，调用大模型都会失败；与其失败不如提前判定「未配置」。
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    # 【这行在干什么】判断 MySQL 是否已配置（看主机和用户名两项）。
    @property
    def mysql_configured(self) -> bool:
        # 【为什么只查两项就够】主机和用户名是「必然会配」的核心项；有这两项说明用户
        #   至少已经认真填过配置了。密码可能为空（少数本地环境允许空密码）。
        return bool(self.mysql_host and self.mysql_user)

    # 【这行在干什么】判断能否做重排。
    @property
    def rerank_configured(self) -> bool:
        """重排同样走大模型接口；未配置大模型时自动跳过重排，沿用 RRF 顺序。"""
        # 【这行在干什么】直接复用 llm_configured 的结论。
        # 【为什么这样（关键）】重排本质也是调大模型；所以「大模型配好了」就等于
        #   「重排可用」。没配时不是报错，而是「跳过重排、沿用 RRF 顺序」——这就是降级。
        return self.llm_configured

    # 【这行在干什么】给出「重排实际该用的模型名」。
    @property
    def rerank_model_or_default(self) -> str:
        # 【写法解释】self.rerank_model or self.llm_model：
        #   配了专用重排模型就用它；没配（空字符串算假）就回落到主大模型。
        # 【为什么允许回落】大多数部署不想为「重排」单配模型，回落让配置更省事。
        return self.rerank_model or self.llm_model

    # 【这行在干什么】判断 Agent 功能是否可用。
    @property
    def agent_configured(self) -> bool:
        """Agent 依赖大模型；未配置时 /api/agent/chat 直接降级为纯 RAG 通道。"""
        # 【为什么 Agent 依赖大模型】Agent 的核心就是「让大模型自主决定是否调用工具」，
        #   没有大模型就没有这个决策者，只能退回普通的检索问答。
        return self.llm_configured

    # 【这行在干什么】给出 Agent 实际该用的模型名（没配就回落主模型）。
    @property
    def agent_model_or_default(self) -> str:
        return self.agent_model or self.llm_model

    # 【这行在干什么】判断 PostgreSQL 查询工具能否启用。
    @property
    def pg_configured(self) -> bool:
        """PostgreSQL 查询工具只在连接信息齐备时才注册给 Agent。"""
        # 【这行在干什么】要求主机、用户、密码、库名四项都非空。
        # 【为什么要求这么严（重要，对应项目硬约束）】内存里明确写着：
        #   「PostgreSQL 工具只在配置完整时才注册给 Agent」。
        #   本机 PG 恰好没配（记忆里的环境坑），所以这个属性为假、工具不注册，
        #   Agent 不会去调用一个必然失败的工具。
        return bool(self.pg_host and self.pg_user and self.pg_password and self.pg_database)

    # 【这行在干什么】给出 PG 查询实际该用的模型名（没配就回落主模型）。
    @property
    def pg_model_or_default(self) -> str:
        return self.pg_model or self.llm_model

    # 【这行在干什么】给出「界面上展示的模型名」。
    @property
    def llm_display_name(self) -> str:
        """界面展示用的大模型名：优先 LLM_MODEL_DISPLAY，否则回落到真实 model id。"""
        # 【为什么需要这个属性】见 llm_model_display 字段处的解释：
        #   真实调用名（如 deepseek-flash）不好看，界面想显示成 DeepSeek-V4.1-Flash。
        #   这里统一封装「优先展示名、没配就用真实名」的逻辑，前端一处取值即可。
        return self.llm_model_display or self.llm_model

    # 【这行在干什么】判断 Neo4j 图谱查询工具能否启用。
    @property
    def graph_configured(self) -> bool:
        """Neo4j 图谱查询工具只在连接信息齐备时才注册给 Agent。"""
        # 【这行在干什么】要求连接地址、用户名、密码三项都非空。
        # 【为什么同 PG 一样严格】不能让 Agent 调用一个没配好的工具（必然失败）。
        return bool(self.neo4j_uri and self.neo4j_user and self.neo4j_password)

    # 【这行在干什么】给出图谱查询实际该用的模型名（没配就回落主模型）。
    @property
    def graph_model_or_default(self) -> str:
        return self.graph_model or self.llm_model

    # 【这行在干什么】判断图片解析功能能否启用。
    @property
    def vision_configured(self) -> bool:
        """图片解析依赖智谱 API Key；未配置时 /analyze-image 返回 502。"""
        # 【这行在干什么】只看智谱密钥配没配。
        # 【注意事项】文档里点明：没配时接口返回 502（表示「上游服务不可用」），
        #   而不是 500——这与项目约定一致（API key 问题/模型失败统一返回 502）。
        return bool(self.zhipuai_api_key)

    # 【这行在干什么】判断联网搜索能否启用。
    @property
    def web_search_configured(self) -> bool:
        """联网搜索工具只在配置了 Tavily Key 时才注册给 Agent。"""
        # 【这行在干什么】只看 Tavily 密钥配没配。
        # 【为什么这样设计（对应项目约定）】记忆里写明：web_search=true 但后端没配 Key 时，
        #   搜索工具干脆不注册，从而「维持改动前行为」，不会让用户以为开了联网却搜不到。
        return bool(self.tavily_api_key)

    # 【这行在干什么】判断对话历史功能能否启用。
    @property
    def history_configured(self) -> bool:
        """对话历史可用需同时开启「历史存储」与「账号体系」——历史按用户分区，没有用户就没法隔离。"""
        # 【这行在干什么】要求「历史存储开关」和「账号体系开关」同时为真。
        # 【为什么必须两个一起（关键设计）】历史是按「用户」分开存的：
        #   A 用户看不到 B 用户的对话。没有账号体系就没有「用户」这个概念，
        #   历史无法隔离（大家共用一个池子会串数据），所以干脆不可用。
        return bool(self.history_enabled and self.auth_enabled)

    # 【这行在干什么】定义方法 label_of：把英文表名翻成中文展示名。
    # 【参数解释】table —— 数据库里的英文表名。
    # 【返回值】对应的中文名；查不到就原样返回英文表名。
    # 【为什么要有兜底（重要）】如果加了一张新表但忘了配中文名，这个方法会返回英文表名，
    #   界面虽然显示英文但功能正常——「退而求其次」而不是报错崩溃。
    def label_of(self, table: str) -> str:
        # 【参数解释】self.table_labels.get(table, table)：
        #   在映射字典里查这个表；查不到时用第二个参数（table 本身）作为默认值。
        return self.table_labels.get(table, table)

    # 【这行在干什么】判断建库管道是否改读本机 PostgreSQL 副本。
    @property
    def kb_source_is_postgres(self) -> bool:
        """True = 走 PG 数据源；False = 走 MySQL（默认，保持改动前行为）。"""
        # 【为什么做归一化】容忍大小写与几种常见写法（pg / postgres / postgresql）。
        return self.kb_source.strip().lower() in {"pg", "postgres", "postgresql"}


# 【这行在干什么】定义函数 _build_settings：真正「组装」出配置对象。
# 【为什么单独拆个函数（与下面 get_settings 配合）】把「构建」和「缓存」分开：
#   本函数只管「一项一项从环境变量读、组装成 Settings」；缓存由 get_settings 负责。
#   职责单一，代码更好读、也更好测。
# 【返回值】一个组装完成、内容不可修改的 Settings 对象。
def _build_settings() -> Settings:
    # 【这行在干什么】解析「要纳入知识库的表名清单」。
    # 【写法逐步拆解】
    #   · _str("KB_TABLES", "bidding_notices,bidding_project,companies") —— 读配置，
    #     没配时用后面的默认清单兜底；
    #   · .split(",") —— 按逗号拆成若干段（如 ["bidding_notices", "bidding_project", ...]）；
    #   · for t in ... —— 逐段处理；
    #   · t.strip() —— 去掉每段首尾空白（容忍配置里逗号后多打的空格）；
    #   · if t.strip() —— 只保留非空的段（忽略多余的逗号造成的空段）；
    #   · tuple(...) —— 把结果收集成一个元组（不可变的表清单）。
    tables = tuple(
        t.strip() for t in _str("KB_TABLES", "bidding_notices,bidding_project,companies").split(",") if t.strip()
    )
    # 【这行在干什么】解析跨域来源白名单：与上面的 tables 同一套写法
    #   （读配置 → 按逗号拆 → 去首尾空白 → 丢弃空段 → 收集成元组）。
    # 【为什么给默认值】没配 CORS_ORIGINS 时只放行本机前端的两个常见地址，不再放行任意来源。
    cors_origins = tuple(
        o.strip()
        for o in _str("CORS_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000").split(",")
        if o.strip()
    )
    # 【这行在干什么】用 Settings(...) 构造配置对象，把所有字段一个个填上。
    # 【写法说明】下面每一个参数都是「读环境变量 + 兜底默认值」的形式，
    #   统一用 _str/_int/_float/_bool/_path 这几个小工具完成（它们已处理了类型转换和容错）。
    #   第一个参数名写在括号里（如 mysql_host=...）是「关键字传参」，一目了然、不怕顺序错。
    return Settings(
        # ---------------- MySQL 数据源 ---------------- #
        mysql_host=_str("MYSQL_HOST", "127.0.0.1"),
        mysql_port=_int("MYSQL_PORT", 3306),
        # 【注意】用户名/密码没有写默认值（默认是空字符串）——它们必须由用户在 .env 里提供，
        #   不提供就会走「数据库未配置」的降级路径。密钥类信息绝不硬编码。
        mysql_user=_str("MYSQL_USER"),
        mysql_password=_str("MYSQL_PASSWORD"),
        mysql_database=_str("MYSQL_DATABASE", "bidding_data"),
        mysql_charset=_str("MYSQL_CHARSET", "utf8mb4"),
        mysql_connect_timeout=_int("MYSQL_CONNECT_TIMEOUT", 10),
        kb_tables=tables,
        # 【这行在干什么】解析表名→中文名的映射：先读 TABLE_LABELS（默认空串），
        #   交给 _parse_table_labels 解析；因为该函数从默认字典起手，空串时也能正常返回默认映射。
        table_labels=_parse_table_labels(_str("TABLE_LABELS")),
        # 【这行在干什么】建库管道数据源开关：不设 KB_SOURCE 时默认 mysql，行为与改动前一字不差。
        kb_source=_str("KB_SOURCE", "mysql"),
        # 【这行在干什么】无主键表的主键兜底声明（仅在 PG 表缺 PK 约束时才起作用）。
        kb_primary_keys=_parse_primary_keys(_str("KB_PRIMARY_KEYS")),
        # ---------------- 向量库 ---------------- #
        qdrant_path=_path("QDRANT_PATH", "./data/qdrant"),
        qdrant_collection=_str("QDRANT_COLLECTION", "bidding_kb"),
        # ---------------- 向量化 ---------------- #
        embed_model=_str("EMBED_MODEL", "BAAI/bge-small-zh-v1.5"),
        embed_dim=_int("EMBED_DIM", 512),
        embed_batch_size=_int("EMBED_BATCH_SIZE", 32),
        embed_query_prefix=_str("EMBED_QUERY_PREFIX", "为这个句子生成表示以用于检索相关文章："),
        # ---------------- BM25 ---------------- #
        bm25_index_path=_path("BM25_INDEX_PATH", "./data/bm25_index.json"),
        bm25_k1=_float("BM25_K1", 1.5),
        bm25_b=_float("BM25_B", 0.75),
        # ---------------- 检索 ---------------- #
        retrieve_dense_top_k=_int("RETRIEVE_DENSE_TOP_K", 10),
        retrieve_sparse_top_k=_int("RETRIEVE_SPARSE_TOP_K", 10),
        retrieve_final_top_k=_int("RETRIEVE_FINAL_TOP_K", 5),
        rrf_k=_int("RRF_K", 60),
        dense_min_score=_float("DENSE_MIN_SCORE", 0.35),
        # ---------------- 重排序 ---------------- #
        rerank_candidates=_int("RERANK_CANDIDATES", 10),
        rerank_model=_str("RERANK_MODEL"),
        rerank_max_chars=_int("RERANK_MAX_CHARS", 300),
        rerank_timeout=_int("RERANK_TIMEOUT", 45),
        rerank_mode=_str("RERANK_MODE", "auto").lower(),
        rerank_auto_gap=_float("RERANK_AUTO_GAP", 0.1),
        rerank_auto_min_dense=_float("RERANK_AUTO_MIN_DENSE", 0.35),
        # ---------------- 切块 ---------------- #
        max_doc_chars=_int("MAX_DOC_CHARS", 1200),
        chunk_size=_int("CHUNK_SIZE", 500),
        chunk_overlap=_int("CHUNK_OVERLAP", 80),
        # ---------------- 大模型 ---------------- #
        llm_base_url=_str("LLM_BASE_URL"),
        llm_api_key=_str("LLM_API_KEY"),
        llm_model=_str("LLM_MODEL"),
        llm_model_display=_str("LLM_MODEL_DISPLAY"),
        llm_temperature=_float("LLM_TEMPERATURE", 0.2),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 1024),
        llm_timeout=_int("LLM_TIMEOUT", 60),
        # ---------------- Agent ---------------- #
        agent_model=_str("AGENT_MODEL"),
        agent_max_tokens=_int("AGENT_MAX_TOKENS", 2048),
        agent_history_turns=_int("AGENT_HISTORY_TURNS", 6),
        agent_max_steps=_int("AGENT_MAX_STEPS", 6),
        # ---------------- PostgreSQL ---------------- #
        pg_host=_str("PG_HOST", "127.0.0.1"),
        pg_port=_int("PG_PORT", 5432),
        pg_user=_str("PG_USER"),
        pg_password=_str("PG_PASSWORD"),
        pg_database=_str("PG_DATABASE", "chatbot"),
        pg_schema=_str("PG_SCHEMA", "public"),
        # 【这行在干什么】解析 PG 允许查询的表清单：同 kb_tables 的写法（拆分→去空白→去空段→成元组）。
        # 【注意】没配时结果是「空元组」，表示「不限定表」（交给 pg.py 里的逻辑决定行为）。
        pg_tables=tuple(t.strip() for t in _str("PG_TABLES").split(",") if t.strip()),
        pg_connect_timeout=_int("PG_CONNECT_TIMEOUT", 10),
        pg_statement_timeout=_int("PG_STATEMENT_TIMEOUT", 15),
        pg_max_rows=_int("PG_MAX_ROWS", 50),
        pg_model=_str("PG_MODEL"),
        pg_max_tokens=_int("PG_MAX_TOKENS", 3072),
        pg_timeout=_int("PG_TIMEOUT", 45),
        pg_schema_ttl=_int("PG_SCHEMA_TTL", 600),
        # ---------------- Neo4j 知识图谱 ---------------- #
        # 【注意】这三个连接项都没给默认值——不配就不会启用图谱功能（graph_configured 为假）。
        neo4j_uri=_str("NEO4J_URI"),
        neo4j_user=_str("NEO4J_USER"),
        neo4j_password=_str("NEO4J_PASSWORD"),
        neo4j_database=_str("NEO4J_DATABASE", "neo4j"),
        neo4j_timeout=_int("NEO4J_TIMEOUT", 30),
        neo4j_query_timeout=_int("NEO4J_QUERY_TIMEOUT", 30),
        neo4j_max_rows=_int("NEO4J_MAX_ROWS", 50),
        graph_model=_str("GRAPH_MODEL"),
        graph_max_tokens=_int("GRAPH_MAX_TOKENS", 3072),
        graph_timeout=_int("GRAPH_TIMEOUT", 45),
        graph_schema_ttl=_int("GRAPH_SCHEMA_TTL", 600),
        # ---------------- 图片解析 ---------------- #
        zhipuai_api_key=_str("ZHIPUAI_API_KEY"),
        vision_model=_str("VISION_MODEL", "glm-4.6v-flashx"),
        vision_timeout=_int("VISION_TIMEOUT", 60),
        # 【这行在干什么】默认 10MB 上限，写成一个直观的算式 10 * 1024 * 1024。
        vision_max_bytes=_int("VISION_MAX_BYTES", 10 * 1024 * 1024),
        # ---------------- 联网搜索 ---------------- #
        tavily_api_key=_str("TAVILY_API_KEY"),
        tavily_search_depth=_str("TAVILY_SEARCH_DEPTH", "basic"),
        tavily_max_results=_int("TAVILY_MAX_RESULTS", 5),
        tavily_timeout=_int("TAVILY_TIMEOUT", 20),
        web_search_default=_bool("WEB_SEARCH_DEFAULT", True),
        # ---------------- 对话历史 ---------------- #
        history_enabled=_bool("HISTORY_ENABLED", True),
        history_db_path=_path("HISTORY_DB_PATH", "./data/conversations.db"),
        history_page_size=_int("HISTORY_PAGE_SIZE", 20),
        history_search_limit=_int("HISTORY_SEARCH_LIMIT", 20),
        history_title_max_chars=_int("HISTORY_TITLE_MAX_CHARS", 30),
        history_max_message_chars=_int("HISTORY_MAX_MESSAGE_CHARS", 20000),
        # ---------------- 账号体系 ---------------- #
        auth_enabled=_bool("AUTH_ENABLED", True),
        auth_token_ttl_days=_int("AUTH_TOKEN_TTL_DAYS", 30),
        auth_pbkdf2_iterations=_int("AUTH_PBKDF2_ITERATIONS", 200000),
        auth_max_login_attempts=_int("AUTH_MAX_LOGIN_ATTEMPTS", 5),
        auth_lockout_seconds=_int("AUTH_LOCKOUT_SECONDS", 300),
        # ---------------- 问答缓存 / 指标 / 日志 ---------------- #
        # 【这行在干什么】缓存/指标/日志三个本地产物的路径。
        # 【为什么默认写成 ./backend/data/...（关键）】_path 以「项目根目录」为基准解析，
        #   所以 ./backend/data/answer_cache.db 最终落在 <项目根>/backend/data/answer_cache.db，
        #   与「不要动 data/qdrant」的项目根 data/ 目录天然分开，互不影响。
        cache_db_path=_path("CACHE_DB_PATH", "./backend/data/answer_cache.db"),
        # 【这行在干什么】缓存有效期（秒），默认 3600。
        cache_ttl=_int("CACHE_TTL", 3600),
        metrics_db_path=_path("METRICS_DB_PATH", "./backend/data/metrics.db"),
        # 【这行在干什么】日志级别，默认 INFO。
        log_level=_str("LOG_LEVEL", "INFO"),
        log_file_path=_path("LOG_FILE_PATH", "./backend/data/app.log"),
        # ---------------- 应用层限流 ---------------- #
        # 【这行在干什么】令牌桶容量与每秒补充数，默认值与改造前的模块常量一致（10 / 0.5），
        #   外置成配置只为让 README「配置全部集中在 .env」这句话成立，行为零变化。
        ratelimit_capacity=_int("RATELIMIT_CAPACITY", 10),
        ratelimit_refill_per_sec=_float("RATELIMIT_REFILL_PER_SEC", 0.5),
        # ---------------- 跨域（CORS） ---------------- #
        cors_origins=cors_origins,
        # ---------------- 深度推理模式 ---------------- #
        deep_max_steps=_int("DEEP_MAX_STEPS", 4),
        deep_step_max_steps=_int("DEEP_STEP_MAX_STEPS", 6),
        deep_reflect_rounds=_int("DEEP_REFLECT_ROUNDS", 1),
        deep_max_tokens=_int("DEEP_MAX_TOKENS", 3072),
        deep_timeout=_int("DEEP_TIMEOUT", 120),
        deep_observation_chars=_int("DEEP_OBSERVATION_CHARS", 1200),
    )


# 【这行在干什么】@lru_cache(maxsize=1) 给 get_settings 装上「缓存」。
# 【参数解释】maxsize=1 —— 缓存里最多存 1 个结果。因为我们希望「全进程只有一个配置对象」，
#   存 1 个就够；再次调用直接返回那一个，不会重复构建。
# 【为什么需要缓存（重要）】配置的构建要读一堆环境变量、解析路径，虽然不快但也别浪费；
#   更关键的是：如果每次调用都新建一个 Settings，各处拿到的就不是「同一个对象」，
#   会平白多占内存、也可能出现「不一致」的疑虑。缓存后，全项目拿到的是同一个实例。
# 【注意事项（环境坑）】正因为有缓存，改了 .env 之后必须重启后端才会生效——
#   运行中的进程里那个缓存对象不会自动刷新。这是项目记忆里明确记录过的坑。
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # 【这行在干什么】调用构建函数，真正组装配置并返回。
    # 【为什么这里看起来「什么都没做」】本函数的职责就是「当缓存的门面」：
    #   第一次调用时执行这行去构建；之后同样参数的调用根本不会进来，直接返回缓存值。
    return _build_settings()