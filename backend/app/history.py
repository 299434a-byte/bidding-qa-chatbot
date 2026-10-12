# ============================================================================ #
# 本文件在干什么
# ---------------------------------------------------------------------------- #
# history.py 是「对话历史仓库」。它把聊天记录（谁、什么时候、问了什么、答了什么、
# 用了什么工具、来源是什么）存进一个本地的 SQLite 数据库文件里，从而支持：
#   1. 跨会话恢复：换个浏览器、隔几天再来，登录后还能看到之前聊过什么；
#   2. 历史搜索：按关键词 + 时间范围 + 模式，找回以前问过的内容；
#   3. 账号/令牌的落库（配合 auth.py 使用）。
#
# ---------------------------------------------------------------------------- #
# 为什么用 SQLite（而不是项目里已有的 MySQL / PostgreSQL）
# ---------------------------------------------------------------------------- #
# 1. MySQL / PostgreSQL 当前都不是「可写通道」——它们的账号口令是空的，而且
#    pg.py / graph.py 是「三层只读保护」的设计，不能挪来当写入通道；
# 2. sqlite3 是 Python 标准库，零额外依赖、零额外服务，跟项目「Qdrant 嵌入式
#    本地模式」是同一个思路（不装额外的数据库服务，就存本地文件）；
# 3. 单文件便于备份、迁移与本地开发。
#
# ---------------------------------------------------------------------------- #
# 概念预习（大白话）
# ---------------------------------------------------------------------------- #
# 【SQLite】= 一个「就是一个文件」的小型数据库。不用启动服务，代码直接读写 .db 文件。
# 【WAL 模式】= SQLite 的一种日志模式，好处是「读写可以同时进行」，不容易互相卡住。
# 【FTS5】= SQLite 自带的「全文检索」插件（相当于内置的搜索引擎），能做关键词打分。
#   本机如果 SQLite 没编译 FTS5，就自动降级成用 LIKE 做模糊匹配（见下）。
# 【分词】= 把一句话切成一个个词。英文按空格切，中文没空格，得用 jieba 这类工具切。
# 【连接不能跨线程共享】= SQLite 的连接对象不能几个线程一起用（本机 threadsafety==1）。
#   所以这里「每次操作都新开连接、用完就关」，并由调用方用 asyncio.to_thread 包起来跑。
# 【事务】= 一组要么全成功、要么全作废的数据库操作。用 `with conn:` 包住就自动管理。
# 【DDL】= 建表语句（CREATE TABLE ...）。【DML】= 增删改查数据的语句。
# 【CJK】= 中日韩文字，这里主要指汉字。
# 【子串查询】= 用词的一部分去搜，比如用「市政」搜「市政道路」。
#
# ---------------------------------------------------------------------------- #
# 两个必须遵守的实现约束（本机实测，别凭记忆改写）
# ---------------------------------------------------------------------------- #
# 1. sqlite3.threadsafety == 1 —— 连接不能跨线程共享。因此每次操作都新开连接，
#    由异步侧用 asyncio.to_thread 包起来执行。WAL 模式下并发读不受影响。
# 2. 中文检索必须自己分词。FTS5 默认的 unicode61 分词器不切中文词，所以专门存一列
#    jieba 分词结果 messages.seg（复用 bm25.tokenize），查询时同样分词后再 MATCH。
#
# ---------------------------------------------------------------------------- #
# 关于预览片段（snippet）的设计取舍
# ---------------------------------------------------------------------------- #
# 不用 FTS5 自带的 snippet()：因为外部内容表里它能读到的只有分词后的 seg（词之间
# 带空格，展示很难看）。这里改成「FTS 只负责找出命中的消息行，预览在 Python 里从
# messages.content 原文截取」，顺便自己插入高亮标记 <em>。
#
# ---------------------------------------------------------------------------- #
# 阅读顺序指南
# ---------------------------------------------------------------------------- #
#   1. 顶部常量 + 三个自定义异常；
#   2. 连接与建表：_connect / _DDL / _FTS_DDL / _migrate_terms / ensure_schema；
#   3. 小工具：_now / _new_id / _json_or_none / _clip / _terms 系列 / _build_preview；
#   4. 用户与令牌：create_user ... purge_expired_sessions；
#   5. 会话与消息：create_conversation ... _message_dict；
#   6. 历史搜索：_match_expression / search_messages / _search_like；
#   7. 统计：stats。
# ============================================================================ #
"""对话历史持久化（SQLite 本地文件）。

为什么用 SQLite 而不是项目里已有的三个库：

1. MySQL / PostgreSQL 当前都不是可写通道 —— ``MYSQL_USER`` / ``MYSQL_PASSWORD``、
   ``PG_USER`` / ``PG_PASSWORD`` 均为空，且 ``pg.py`` / ``graph.py`` 是「三层只读保护」的设计，
   不能挪用为写入通道；
2. ``sqlite3`` 是标准库，零额外依赖、零额外服务，与项目「Qdrant 嵌入式本地模式」同一思路；
3. 单文件便于备份、迁移与本地开发。

两个必须遵守的实现约束（本机实测，勿凭记忆改写）：

1. ``sqlite3.threadsafety == 1`` —— **连接不能跨线程共享**。因此这里每次操作都新开连接，
   并由调用方（异步侧）用 ``asyncio.to_thread`` 包起来执行。WAL 模式下并发读不受影响。
2. **中文检索必须自己分词**。FTS5 默认的 unicode61 分词器不切中文词，
   因此专门存一列 jieba 分词结果 ``messages.seg``（复用 ``bm25.tokenize``），查询时同样分词后 MATCH。

关于预览片段：不用 FTS5 的 ``snippet()``，因为外部内容表里它能读到的只有分词后的 ``seg``
（词之间带空格，展示很难看）。这里改成「FTS 只负责找出命中的消息行，预览在 Python 里
从 ``messages.content`` 原文截取」，顺便自己插入高亮标记。
"""

# 让类型标注延迟求值，兼容性更好。
from __future__ import annotations

# datetime as dt：日期时间工具。「as dt」给它起个别名，后面写起来短一些。
import datetime as dt
# html：做 HTML 转义（把 < > & 等换成安全写法），防止预览片段里混入标签。
import html
# json：把消息的 meta（原始结构）存成文本、读回来时再还原成对象。
import json
# re：正则，用于识别中文（CJK）字符段。
import re
# sqlite3：Python 标准库自带的轻量数据库，历史记录就存在一个本地 .db 文件里。
import sqlite3
# threading：线程锁，保护「建表只做一次」的标记集合，避免多线程重复建表。
import threading
# uuid：生成每条记录的唯一 id。
import uuid
# contextmanager：把「打开/关闭连接」包装成 with 可用的上下文管理器。
from contextlib import contextmanager
# Path：优雅地处理文件路径（拼接、自动建目录）。
from pathlib import Path
# 类型标注用：Any 表示任意类型；Iterator 表示「可以被逐个取出元素的对象」。
from typing import Any, Iterator

# 复用知识库那边的分词函数（jieba 分词），保证「历史搜索」与「知识库检索」口径一致。
from .bm25 import tokenize
# 【UTC 改造】timeutil：时间戳的写入与解析统一走它（存储/比较 UTC，老 naive 文本按东八区解释）。
from . import timeutil
# Settings/get_settings：读取 .env 配置（开关、库文件路径、各种长度上限）。
from .config import Settings, get_settings

# 预览片段：命中词前后各截多少字符
# 命中词前面留 40 字、后面留 120 字，拼成搜索结果的「上下文预览」。
_PREVIEW_BEFORE = 40
_PREVIEW_AFTER = 120
# 列表里会话标题为空时的占位
DEFAULT_CONVERSATION_TITLE = "新对话"

# 索引分词方案版本。改动分词口径时 +1，``ensure_schema`` 会自动重算旧数据的 seg。
#   1（历史）：只用 jieba 词
#   2：jieba 词 + CJK 单字 + CJK 二字组合（搜索子串才命得中，见 _index_terms 说明）
# 大白话：这是「分词算法」的版本号。以后若改了分词方式，把它 +1，程序启动时会自动
# 把老数据重新分词一遍，否则老数据会因为「分词口径不一致」而搜不到。
_TERMS_VERSION = 2

# 揪出「一串连续汉字」的正则：[一-龥]+ 表示一个或多个汉字。
# 为什么需要：英文靠空格分词，中文词连在一起没有分隔，要先把汉字段切出来再组合子串。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


# 未开启对话历史功能时抛这个（需 HISTORY_ENABLED 与 AUTH_ENABLED 同时为真）。
# 上层捕获后返回 503。
class HistoryUnavailable(RuntimeError):
    """未开启对话历史（HISTORY_ENABLED / AUTH_ENABLED 未同时为真），上层应返回 503。"""


# 唯一约束冲突（例如抢注同一个用户名）时抛这个。
class HistoryConflict(RuntimeError):
    """唯一约束冲突，例如用户名已存在。"""


# 目标不存在（或不属于当前用户）时抛这个。
# 关键设计：这两种情况一律当作「不存在」，不告诉调用者「其实存在但不属于你」，
# 避免泄露「这个会话是不是别人的」这类信息。
class HistoryNotFound(RuntimeError):
    """目标不存在，**或不属于当前用户** —— 两种情况一律当作不存在，不泄露存在性。"""


# --------------------------------------------------------------------------- #
# 连接与建表
# --------------------------------------------------------------------------- #
# 建表只需做一次；按库文件的绝对路径记录，避免每次请求都跑一遍 DDL
# _schema_ready 里存的是「已经建过表的库文件路径」。运行期用它快速判断，省掉重复建表。
_schema_ready: set[str] = set()
# 多线程同时进来时，用这把锁保证「只有一个线程在建表」，避免重复执行 DDL 打架。
_schema_lock = threading.Lock()
# 每个库是否支持 FTS5（不支持时搜索降级为 LIKE）
# 键=库文件路径，值=True/False。为什么记下来：探测一次就够，不必每次都试。
_fts_supported: dict[str, bool] = {}


# _require_enabled：检查「对话历史」功能是否已开启。
# 没开启就抛 HistoryUnavailable（上层返回 503），并提示去 .env 检查开关。
def _require_enabled(settings: Settings) -> None:
    if not settings.history_configured:
        raise HistoryUnavailable(
            "对话历史未开启（需 HISTORY_ENABLED=true 且 AUTH_ENABLED=true），"
            "请在项目根目录的 .env 中检查配置"
        )


# @contextmanager 把下面这个函数变成「可以 with 用的」上下文管理器。
# 用法：`with _connect(settings) as conn:` —— 进入时打开连接，离开时自动关闭。
# 好处：不管中间成功还是报错，连接一定被关掉（见 finally），不会泄漏。
@contextmanager
def _connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    """打开一个连接（每次操作独立，用完即关）。

    ``timeout`` 与 ``busy_timeout`` 让并发写自动排队，而不是直接抛 database is locked。
    """
    # 取出数据库文件路径，并确保它所在的目录存在（parents=True 会一路建好上级目录）。
    path: Path = settings.history_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # 新建连接。timeout=5 表示「被别的写操作挡住时最多等 5 秒」。
    conn = sqlite3.connect(str(path), timeout=5)
    # row_factory=Row：让查询结果可以像字典一样用列名取值（row["id"]），比位置下标好读。
    conn.row_factory = sqlite3.Row
    try:
        # WAL 模式：让读写可以更少互相阻塞。
        conn.execute("PRAGMA journal_mode=WAL")
        # 打开外键约束（SQLite 默认是关的）。这样删除用户时会连带删掉他的会话等。
        conn.execute("PRAGMA foreign_keys=ON")
        # busy_timeout=5000：被占用时自动排队等待 5 秒，而不是立刻报 database is locked。
        conn.execute("PRAGMA busy_timeout=5000")
        # yield 把连接「交给」with 的使用者；等他那边代码跑完，再回到这里执行 finally。
        yield conn
    finally:
        # 无论如何都关闭连接。
        conn.close()


# 主建表脚本（DDL）。用三引号的多行字符串写一整套建表语句，好处是一次性执行、集中维护。
# 全部用 CREATE TABLE IF NOT EXISTS —— 「不存在才建」，所以反复执行也安全（幂等）。
# 表之间的关系（用大白话说明）：
#   users（账号）—— 一个用户可以有多个 auth_sessions（登录令牌）与多个 conversations（会话）。
#   conversations（会话）—— 一个会话有多条 messages（消息）。
#   ON DELETE CASCADE 表示「删掉父级，子级自动一起删」。
#   schema_meta 是一张「键值表」，用来记一些内部状态（比如分词方案版本号）。
# 另外几条 CREATE INDEX 是为「按用户+时间查会话」「按会话取消息」「按时间搜消息」加速的。
_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    iterations    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_user ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'react',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conv_user_time ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    seg             TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    meta            TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_seq ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# 外部内容表的三个同步触发器（FTS5 官方推荐写法）
# 大白话：FTS 索引表（messages_fts）不会自动跟着 messages 变，得靠「触发器」在
# 插入/删除/更新 messages 时同步维护索引。三个触发器分别对应：
#   _ai = after insert（新增消息后 → 把分词结果也写进索引）
#   _ad = after delete（删除消息后 → 从索引里移除）
#   _au = after update（更新消息后 → 先移除旧的、再插入新的）
# content='messages' / content_rowid='rowid' 表示这是「外部内容表」：索引自己不存正文，
# 只存分词列 seg，正文仍从 messages 表读，省空间。
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    seg,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, seg) VALUES (new.rowid, new.seg);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, seg) VALUES ('delete', old.rowid, old.seg);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, seg) VALUES ('delete', old.rowid, old.seg);
    INSERT INTO messages_fts(rowid, seg) VALUES (new.rowid, new.seg);
END;
"""


# _migrate_terms：按当前分词口径，把「全部消息」的 seg 列重算一遍，并重建 FTS 索引。
# 只在分词方案版本号升级时跑一次（版本号记在 schema_meta 表里），
# 避免旧数据因为分词不匹配而搜不到。
def _migrate_terms(conn: sqlite3.Connection) -> None:
    """按当前分词口径重算全部消息的 ``seg``，并重建 FTS 索引。

    只在那套口径升级时跑一次（``schema_meta.terms_version`` 记录），
    避免旧数据因为分词不匹配而搜不到。
    """
    # 读出「库里记录的分词版本号」。
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'terms_version'").fetchone()
    # 把记录值转成整数；若记录不存在或不是数字，就当作 0（表示「比任何版本都旧」）。
    current = int(row["value"]) if row is not None and str(row["value"]).isdigit() else 0
    # 版本一致 → 不用迁移，直接返回。
    if current == _TERMS_VERSION:
        return
    # 取所有消息的 id 和正文内容。
    rows = conn.execute("SELECT id, content FROM messages").fetchall()
    # 逐条重新分词并写回 seg 列（" ".join 把词用空格连起来存）。
    for r in rows:
        conn.execute(
            "UPDATE messages SET seg = ? WHERE id = ?",
            (" ".join(_index_terms(r["content"])), r["id"]),
        )
    # 有数据才需要重建索引（空库没必要）。
    if rows:
        # 外部内容表的官方重建命令，兜住触发器未覆盖的历史行
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    # 记下新的分词版本号（INSERT OR REPLACE = 有就覆盖、没有就插入）。
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('terms_version', ?)",
        (str(_TERMS_VERSION),),
    )


# ensure_schema：确保数据库和表都建好了（幂等——反复调用也安全）。
# FTS5 不可用时自动降级，搜索改走 LIKE（见 search_messages 的兜底）。
def ensure_schema(settings: Settings | None = None) -> None:
    """建表（幂等）。FTS5 不可用时自动降级，搜索改走 LIKE。"""
    # 没传配置就取全局配置。
    s = settings or get_settings()
    # 检查功能开关（没开就抛 503）。
    _require_enabled(s)
    # 用数据库文件路径当「是否已建表」的标识。
    key = str(s.history_db_path)
    # 已经建过 → 直接返回（快速通道，不加锁，避免每次都抢锁）。
    if key in _schema_ready:
        return
    # 加锁，保证多线程下只有一个线程真正去建表。
    with _schema_lock:
        # 「双重检查」：等锁的过程中别的线程可能已经建好了，所以进来再查一次。
        if key in _schema_ready:
            return
        # 这个 with 里有两个上下文管理器：
        #   1) _connect(s) —— 负责打开连接、结束关闭；
        #   2) conn —— 把连接本身当作「事务」，正常结束自动 commit，出错自动 rollback。
        with _connect(s) as conn, conn:
            # 执行主建表脚本（一次执行多条 DDL）。
            conn.executescript(_DDL)
            try:
                # 尝试建 FTS5 索引表和触发器。
                conn.executescript(_FTS_DDL)
                # 成功则记下「这个库支持 FTS5」。
                _fts_supported[key] = True
                # 顺便按当前分词口径迁移旧数据。
                _migrate_terms(conn)
            except sqlite3.OperationalError:
                # 本机 SQLite 未编译 FTS5：不影响主流程，只是搜索退化为 LIKE
                _fts_supported[key] = False
        # 标记「这个库已经建过表」。
        _schema_ready.add(key)


# fts_available：查这个库能不能用 FTS5 全文检索。
# 会顺带确保表已建好（ensure_schema 幂等，调用无副作用）。
def fts_available(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    ensure_schema(s)
    # 取值并转成布尔；没记录到就当作 False（不支持）。
    return bool(_fts_supported.get(str(s.history_db_path), False))


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
# _now：返回当前时间的字符串，格式与项目其它模块保持一致（便于排序、比较）。
# 为什么用字符串而不是时间对象：SQLite 里就按文本存，文本格式统一才能正确地
# 排序和做范围比较（"2026-09-29 10:00:00" 这种写法从小到大排就是时间顺序）。
def _now() -> str:
    """本地时间字符串，与项目其它模块（日志、建库）保持一致。"""
    # 【UTC 改造】时间戳统一写 UTC 的 ISO 文本（带 +00:00 偏移）：
    #   服务器 TZ=UTC 时，本地 now() 写下的时间与东八区相差 8 小时，
    #   过期判断、TTL 计算会随之偏移。解析老的 naive 文本时按东八区解释，
    #   保证历史数据继续有效（见 app/timeutil.py）。
    return timeutil.now_iso()


# _new_id：生成一个新记录的唯一 id（32 位十六进制字符串）。
def _new_id() -> str:
    return uuid.uuid4().hex


# _json_or_none：把任意结构（字典/列表等）转成 JSON 文本存库；None 就返回 None。
# ensure_ascii=False 保留中文不转义；default=str 兜底处理 json 不认识的对象。
def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


# _load_json：把库里的 JSON 文本还原成对象；空值或格式坏掉都返回 None（不抛异常）。
def _load_json(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# _row_to_dict：把数据库行对象转成普通字典；没有行（None）就返回 None。
def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# _clip：去掉首尾空白后按长度截断。
# limit > 0 才截；limit <= 0 表示「不限制」。
def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


# _terms：生成「用于全文检索的词元列表」。核心是解决中文子串搜索问题。
# **为什么不能只用 jieba 词**：jieba 会把「市政道路」切成一个词，用户搜「市政」
# 这种子串就永远命不中 —— 而中文搜索里子串查询是最常见的用法。
# 于是这里在 jieba 词之外，额外补上「单字」和「连续两字组合」：
#   例如「市政道路」→ jieba: [市政, 道路]；二字组合: [市政, 政道, 道路]；单字: [市,政,道,路]
#   这样搜「市政」「政道」「道路」都能命中。
# 参数 unigrams：是否包含单字。只在**建索引**时为 True；查询侧为 False，
#   因为若查询也放单字，一个「市」字就会把结果放得太宽（命中太多不相关记录）。
# 返回：去重且保持原有顺序的词元列表。
def _terms(text: str, *, unigrams: bool) -> list[str]:
    """生成 FTS 词元：jieba 词 + CJK 单字 / 二字组合（各自去重、保序）。

    **为什么不能只用 jieba 词**：jieba 会把「市政道路」切成一个词，用户搜「市政」
    这种子串就永远命不中 —— 中文搜索里子串查询是最常见的用法。
    补上单字与二字组合后，≥2 字的子串查询能精确命中，单字查询也兜得住。

    ``unigrams`` 只在**建索引**时为真：查询侧若也放单字，一个「市」字就会把结果放得过宽。
    """
    # out 收集最终词元（保序）；seen 用于去重（同一词不重复放）。
    out: list[str] = []
    seen: set[str] = set()
    # tokenize(text) 先用 jieba 把整句切成词（例如「市政道路」→ ["市政","道路"]）。
    for token in tokenize(text):
        # 候选集先放入「这个词本身」。
        candidates = [token]
        # 再对词里的每一段连续汉字（run）做「补子串」处理。
        for run in _CJK_RE.findall(token):
            # 建索引时把该段汉字的每个单字都加进去（保证单字也能搜到）。
            if unigrams:
                candidates.extend(run)
            # 无论建索引还是查询，都把「连续两字组合」加进去。
            # run[i : i + 2] 是「从第 i 位起取 2 个字符」的切片；range(len(run)-1) 保证不越界。
            candidates.extend(run[i : i + 2] for i in range(len(run) - 1))
        # 逐个候选去重后收进 out。
        for cand in candidates:
            if cand and cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


# _index_terms：写索引（messages.seg 列）时用的词元——**含单字**。
def _index_terms(text: str) -> list[str]:
    """写入 ``messages.seg`` 的词元（含单字，保证单字查询也能命中）。"""
    return _terms(text, unigrams=True)


# _query_terms：查询时用的词元——**不含单字**（避免结果太宽）。
def _query_terms(text: str) -> list[str]:
    """查询词元（不含单字，避免把结果放得过宽）。"""
    return _terms(text, unigrams=False)


# title_from_question：用「第一个提问」自动生成会话标题。
# 处理：把换行/多空格压成单个空格 → 太长就截断加省略号；空的就用默认标题。
def title_from_question(question: str, limit: int) -> str:
    """用首个提问生成会话标题：压平换行、截断并加省略号。"""
    # split() 不加参数会按「任意空白」切分并丢掉空串，join 再用单个空格连起来 —— 结果就是「压平空白」。
    text = " ".join((question or "").split())
    # 空问题 → 用默认标题。
    if not text:
        return DEFAULT_CONVERSATION_TITLE
    # 超长 → 截断 + 省略号。
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


# _to_utc_bound：把用户给的「本地时刻文本」换算成 UTC 的 "YYYY-MM-DD HH:MM:SS"。
# 用户给的是本地日期/时刻（东八区），比较轴必须与库里的 UTC 对齐（见 timeutil.py）：
# 带偏移的文本按自带偏移解析；naive 文本按 LEGACY_TZ（东八区）解释再转 UTC ——
# 偏移量取自 timeutil 常量，不硬编码 8。
def _to_utc_bound(text: str) -> str:
    moment = timeutil.parse_ts(text)
    if moment is None:
        # 解析不了就按原样比较（老行为兜底），不让一个坏参数把整次搜索打死。
        return text
    # 归一成秒级 UTC 文本，与 _created_at_utc_sql 产出的值同形可比。
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# _legacy_to_utc_modifier：naive 文本（东八区）→ UTC 的 SQLite 时间修正串。
# 正号取自 timeutil.LEGACY_TZ 再取负：本地转 UTC 是「减去」本地偏移。
def _legacy_to_utc_modifier() -> str:
    minutes = -timeutil.LEGACY_TZ.utcoffset(None).total_seconds() / 60
    return f"{minutes:g} minutes"


# _created_at_utc_sql：把 created_at 归一成 UTC 秒级文本的 SQL 表达式（比较前的值侧归一）。
# 库里两种格式混存（不迁移老数据，比较时就地归一）：
#   · 新格式带 +00:00 偏移（"2026-10-12T08:00:00+00:00"）—— datetime() 按自带偏移换算成 UTC；
#   · 老格式 naive（"2026-10-12 08:00:00"）—— 按东八区解释，减去 LEGACY_TZ 偏移得 UTC；
#   · 文本坏掉时 datetime() 返回 NULL —— COALESCE 回落到 replace(x,'T',' ') 原样比较（老行为）。
# 为什么必须 replace(x,'T',' ')：'T'(84) > ' '(32)，T 格式与空格格式字符串比较不等价，
# 不归一则 "2026-10-12T…" <= "2026-10-12 23:59:59" 恒假，结束日当天的记录全漏。
# 为什么 naive 不能当 UTC 文本比：老记录是按本地时间写入的，晚间记录会被 UTC 上界误杀。
def _created_at_utc_sql(column: str = "m.created_at") -> str:
    shift = _legacy_to_utc_modifier()
    return (
        "COALESCE("
        f"CASE WHEN {column} GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR {column} GLOB '*Z' "
        f"THEN datetime(replace({column},'T',' ')) "
        f"ELSE datetime(replace({column},'T',' '), '{shift}') END, "
        f"replace({column},'T',' '))"
    )


# _normalize_range：把用户给的日期范围「补全成能比较的完整时间」，并统一到 UTC。
# 只给日期（长度 <= 10，如 "2026-09-29"）时，补上本地当天的 00:00:00 到 23:59:59，
# 保证「包含整天」，不会因为少了时分秒而漏掉当天的记录；
# 随后把本地边界换算成 UTC —— 值侧由 _created_at_utc_sql 归一到同一根轴再比。
def _normalize_range(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """时间范围归一化：补全当天 00:00:00 / 23:59:59（本地），再把边界换算成 UTC。"""
    start = (date_from or "").strip()
    end = (date_to or "").strip()
    # 起始：只有日期就补本地最早时刻。
    if start and len(start) <= 10:
        start = f"{start} 00:00:00"
    # 结束：只有日期就补本地最晚时刻（仍是 "YYYY-MM-DD 23:59:59"，含当天整日）。
    if end and len(end) <= 10:
        end = f"{end} 23:59:59"
    # 本地时刻 → UTC；空串统一转成 None（表示「不限」）。
    return (
        _to_utc_bound(start) if start else None,
        _to_utc_bound(end) if end else None,
    )


# _escape：HTML 转义，防止原文里的 < > & 被当成标签。
# quote=False 表示不转义引号（因为我们不在 HTML 属性里用它）。
def _escape(text: str) -> str:
    return html.escape(text or "", quote=False)


# _build_preview：生成「搜索结果预览片段」——截取命中词附近一段，并把命中词用 <em> 标出。
# 步骤：先把原文整体转义（安全）→ 找到最早出现的命中词位置 → 按前后字数截取 → 插入 <em> 高亮。
# 因为先转义再插标记，返回值可以安全地当 HTML 片段渲染。
def _build_preview(content: str, tokens: list[str]) -> str:
    """从原文截出命中词附近的一段，并把命中词用 <em> 标出。

    先整体转义再插标记，因此返回值可以安全地当 HTML 片段渲染。
    """
    # 先转义，得到安全的文本。
    safe = _escape(content)
    # 空的直接返回空串。
    if not safe:
        return ""
    # 为了「不区分大小写地定位命中词」，准备一份小写副本用于查找。
    lowered = safe.lower()
    # pos 记录命中词位置（-1 表示还没找到）；hit_len 记录命中词长度。
    pos = -1
    hit_len = 0
    # 在所有词元里，找出「位置最靠前」的那个，作为预览的锚点。
    for token in tokens:
        if not token:
            continue
        # 在小写副本里找该词（也转小写），这样大小写不影响定位。
        found = lowered.find(token.lower())
        # 找到且比当前锚点更靠前 → 更新锚点。
        if found >= 0 and (pos < 0 or found < pos):
            pos = found
            hit_len = len(token)
    # 一个都没定位到（例如 LIKE 兜底路径只匹配了原词的一部分）→ 直接截开头。
    if pos < 0:
        # 没定位到（例如 LIKE 兜底路径只匹配了原词的一部分）：直接截开头
        head = safe[:_PREVIEW_BEFORE + _PREVIEW_AFTER]
        return head + ("…" if len(safe) > len(head) else "")
    # 计算截取窗口：命中词前 _PREVIEW_BEFORE 字、后 _PREVIEW_AFTER 字，且不越界。
    start = max(0, pos - _PREVIEW_BEFORE)
    end = min(len(safe), pos + hit_len + _PREVIEW_AFTER)
    # 若前面被截掉了，加省略号提示；后面同理。
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(safe) else ""
    # 拼接：前缀省略号 + 命中词前的内容 + <em>命中词</em> + 命中词后的内容 + 后缀省略号。
    marked = (
        safe[:start]
        + prefix
        + safe[start:pos]
        + "<em>"
        + safe[pos : pos + hit_len]
        + "</em>"
        + safe[pos + hit_len : end]
        + suffix
    )
    return marked


# --------------------------------------------------------------------------- #
# 用户与令牌
# --------------------------------------------------------------------------- #
# create_user：新建一个账号记录。
# 注意：这里只管「存」，口令哈希怎么算在 auth.py 里做（这里收到的是算好的值）。
# 参数：settings 配置；username 用户名；password_hash 哈希；salt 盐；iterations 迭代次数。
# 返回：刚建的用户字典（含生成的 id 和时间）。
def create_user(
    settings: Settings,
    username: str,
    password_hash: str,
    salt: str,
    iterations: int,
) -> dict[str, Any]:
    # 确保表已建好。
    ensure_schema(settings)
    # 组装用户记录（注意：密码相关的字段已经是哈希/盐，绝不是明文）。
    user = {
        "id": _new_id(),
        "username": username,
        "password_hash": password_hash,
        "salt": salt,
        "iterations": iterations,
        "created_at": _now(),
        "last_login_at": None,
    }
    try:
        # with conn 同时开启事务：插入成功自动提交，失败自动回滚。
        # 用 :id 这类「命名占位符」，参数直接传整个 user 字典，清晰不易错位。
        with _connect(settings) as conn, conn:
            conn.execute(
                """
                INSERT INTO users (id, username, password_hash, salt, iterations, created_at, last_login_at)
                VALUES (:id, :username, :password_hash, :salt, :iterations, :created_at, :last_login_at)
                """,
                user,
            )
    # 唯一约束冲突（username 重复）会被 SQLite 抛 IntegrityError，转成我们的 HistoryConflict。
    except sqlite3.IntegrityError as exc:
        raise HistoryConflict(f"用户名「{username}」已被注册") from exc
    return user


# get_user_by_username：按用户名查用户；没查到返回 None。
def get_user_by_username(settings: Settings, username: str) -> dict[str, Any] | None:
    ensure_schema(settings)
    with _connect(settings) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _row_to_dict(row)


# get_user_by_id：按用户 id 查用户；没查到返回 None。
def get_user_by_id(settings: Settings, user_id: str) -> dict[str, Any] | None:
    ensure_schema(settings)
    with _connect(settings) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_dict(row)


# touch_login：登录成功后更新「最近登录时间」。
def touch_login(settings: Settings, user_id: str) -> None:
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), user_id))


# create_session：新建一条登录会话记录（存令牌哈希，不存明文）。
# 参数：token_hash 令牌的 SHA-256；user_id 所属用户；expires_at 过期时间；user_agent 设备信息。
def create_session(
    settings: Settings,
    token_hash: str,
    user_id: str,
    expires_at: str,
    user_agent: str | None,
) -> None:
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at, user_agent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash, user_id, _now(), expires_at, user_agent),
        )


# get_session_user：按「令牌哈希」找出对应的会话与用户。
# 关键点：**已过期的会话直接当作无效**——顺手把它删掉，并返回 None。
def get_session_user(settings: Settings, token_hash: str) -> dict[str, Any] | None:
    """按令牌哈希取会话与用户；已过期的会话直接当作无效。"""
    ensure_schema(settings)
    with _connect(settings) as conn:
        # 用 JOIN 一次查出会话过期时间和用户信息。
        row = conn.execute(
            """
            SELECT s.expires_at AS expires_at,
                   u.id AS id, u.username AS username, u.created_at AS created_at
            FROM auth_sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
    # 没查到 → None。
    if row is None:
        return None
    # 字符串直接比大小即可判断过期（时间格式统一，字典序 == 时间序）。
    # 【UTC 改造】改用 timeutil.is_past 做「解析后」的时刻比较：
    #   新格式是 UTC ISO 文本、老格式是东八区 naive 文本，两种混存时字典序不再等于时间序，
    #   必须先解析成感知型时间再比（解析不了按已过期处理，宁可要求重登也不放过期令牌）。
    if timeutil.is_past(str(row["expires_at"])):
        # 过期就顺手删掉这条会话，减少垃圾数据。
        delete_session(settings, token_hash)
        return None
    # 返回用户信息（不含过期时间字段，前端用不到）。
    return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}


# delete_session：按令牌哈希删除会话（注销用）。
def delete_session(settings: Settings, token_hash: str) -> None:
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


# purge_expired_sessions：清掉所有已过期的会话记录（定时任务/启动时调用）。
# 返回：删了几条。
def purge_expired_sessions(settings: Settings) -> int:
    ensure_schema(settings)
    # 【UTC 改造】过期判断改成「先解析、后比较」（timeutil.is_past）：
    #   表里混存 UTC ISO 新文本与东八区 naive 老文本，SQL 里按字符串比大小会误判，
    #   所以取回后在 Python 侧过滤，再删掉确实过期的那些（会话表很小，代价可忽略）。
    with _connect(settings) as conn, conn:
        rows = conn.execute("SELECT token_hash, expires_at FROM auth_sessions").fetchall()
        expired = [str(r["token_hash"]) for r in rows if timeutil.is_past(str(r["expires_at"]))]
        if expired:
            conn.executemany("DELETE FROM auth_sessions WHERE token_hash = ?", [(h,) for h in expired])
    # rowcount 是「这条 DELETE 影响的行数」。
    # 【UTC 改造】返回值改为上面 Python 侧统计的「确实过期并已删除」的条数（语义不变：删了几条）。
    return len(expired)


# --------------------------------------------------------------------------- #
# 会话与消息
# --------------------------------------------------------------------------- #
# create_conversation：新建一个会话。
# 参数：user_id 归属用户；title 标题（默认「新对话」）；mode 对话模式（react/plan/deep）。
# 返回：新建的会话字典。
def create_conversation(
    settings: Settings,
    user_id: str,
    title: str = DEFAULT_CONVERSATION_TITLE,
    mode: str = "react",
) -> dict[str, Any]:
    ensure_schema(settings)
    now = _now()
    conversation = {
        "id": _new_id(),
        "user_id": user_id,
        # 标题先按上限截断；截完为空就用默认标题兜底。
        "title": _clip(title, settings.history_title_max_chars) or DEFAULT_CONVERSATION_TITLE,
        # 模式为空就默认 react。
        "mode": mode or "react",
        "created_at": now,
        "updated_at": now,
        # 初始消息数为 0。
        "message_count": 0,
    }
    with _connect(settings) as conn, conn:
        conn.execute(
            """
            INSERT INTO conversations (id, user_id, title, mode, created_at, updated_at, message_count)
            VALUES (:id, :user_id, :title, :mode, :created_at, :updated_at, :message_count)
            """,
            conversation,
        )
    return conversation


# get_conversation：按 id 取会话，**必须带上 user_id** —— 别人的会话一律返回 None。
# 这是最重要的权限检查：WHERE 里同时匹配 id 和 user_id，就天然挡住了「越权读别人会话」。
def get_conversation(settings: Settings, user_id: str, conversation_id: str) -> dict[str, Any] | None:
    """按 id 取会话；**必须带 user_id** —— 别人的会话一律返回 None（上层转 404）。"""
    ensure_schema(settings)
    with _connect(settings) as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    return _row_to_dict(row)


# ensure_conversation：拿一个可用的会话——有 id 且属于自己就用它，否则新建。
# 为什么需要：前端「首次提问」时还没会话 id，这时就自动建一个，省得前端先建。
def ensure_conversation(
    settings: Settings,
    user_id: str,
    conversation_id: str | None,
    mode: str,
) -> dict[str, Any]:
    """取会话；不存在或不属于当前用户就新建一个（前端首次提问时不带 id）。"""
    # 带了 id：试着按「id + 归属用户」查。
    if conversation_id:
        existing = get_conversation(settings, user_id, conversation_id)
        if existing is not None:
            return existing
    # 没带 id，或查不到（不存在/不属于自己）→ 新建一个。
    return create_conversation(settings, user_id, DEFAULT_CONVERSATION_TITLE, mode)


# list_conversations：查「我的会话列表」。可按标题关键词、模式过滤，按最近更新倒序。
# 参数：limit/offset 分页；keyword 标题关键词；mode 模式。
# 返回：(会话列表, 总条数)。总条数用于前端算总页数。
# 说明：这里用「拼 SQL 条件」的方式（where 列表 + 参数列表），既灵活又保持参数化防注入。
def list_conversations(
    settings: Settings,
    user_id: str,
    limit: int,
    offset: int,
    keyword: str | None = None,
    mode: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """会话列表：按主题（标题关键词）与模式过滤，按最近更新倒序。"""
    ensure_schema(settings)
    # where 收集所有过滤条件；params 收集对应的参数值（顺序要一致）。
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    keyword = (keyword or "").strip()
    # 有关键词：标题做模糊匹配（% 是 SQL 通配符，%xx% 表示「包含 xx」）。
    if keyword:
        where.append("title LIKE ?")
        params.append(f"%{keyword}%")
    # 有模式：精确匹配。
    if mode:
        where.append("mode = ?")
        params.append(mode)
    # 用 AND 把条件连成一段 SQL 片段。
    clause = " AND ".join(where)

    with _connect(settings) as conn:
        # 先查总数（分页要用）。
        total = int(conn.execute(f"SELECT COUNT(*) AS c FROM conversations WHERE {clause}", params).fetchone()["c"])
        # 再查当页数据；[*params, limit, offset] 把参数展开后追加分页参数。
        rows = conn.execute(
            f"""
            SELECT id, title, mode, created_at, updated_at, message_count
            FROM conversations WHERE {clause}
            ORDER BY updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    # 转成普通字典列表返回。
    return [dict(r) for r in rows], total


# rename_conversation：重命名会话（同时更新「最近更新」时间）。
# 返回：更新后的会话；若会话不存在/不属于自己（没改到任何行）则返回 None。
def rename_conversation(
    settings: Settings,
    user_id: str,
    conversation_id: str,
    title: str,
) -> dict[str, Any] | None:
    # 压平空白；空标题直接拒绝。
    clean = " ".join((title or "").split())
    if not clean:
        raise ValueError("标题不能为空")
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            # 用户重命名时允许比自动标题长一些，所以上限放大 2 倍。
            (_clip(clean, settings.history_title_max_chars * 2), _now(), conversation_id, user_id),
        )
        # rowcount 为 0 说明没匹配到（不存在或不属于自己）→ 返回 None。
        if not cur.rowcount:
            return None
    # 返回最新数据。
    return get_conversation(settings, user_id, conversation_id)


# delete_conversation：删除一个会话（连同它的所有消息）。返回是否删除成功。
# 注意：**先显式删消息、再删会话**。原因：SQLite 的 ON DELETE CASCADE 不保证触发
# 用户定义的 DELETE 触发器（取决于 recursive_triggers 设置），而 FTS 索引的清理依赖
# 触发器。显式删除消息才能保证索引同步清理。两步在同一事务里完成（要么都成功要么都作废）。
def delete_conversation(settings: Settings, user_id: str, conversation_id: str) -> bool:
    """删除会话。

    先显式删消息再删会话：SQLite 的 ``ON DELETE CASCADE`` 不保证触发用户定义的
    DELETE 触发器（取决于 ``recursive_triggers``），显式删除才能让 FTS 索引同步清理。
    两步在同一事务内完成。
    """
    ensure_schema(settings)
    with _connect(settings) as conn, conn:
        # 先确认这个会话确实属于当前用户（权限检查）。
        owned = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        # 不属于自己/不存在 → 直接返回 False（不泄露存在性）。
        if owned is None:
            return False
        # 先删该会话下所有消息（触发器会同步清理 FTS 索引）。
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        # 再删会话本身。
        conn.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id))
    return True


# append_message：往会话里追加一条消息。这是本文件最核心的写操作。
# **关键设计：消息插入与会话信息更新在「同一事务」内完成**，保证一致性。
# 顺带做三件事：消息数 +1、更新「最近更新时间」、必要时更新模式；
# 并且「第一条用户提问」会自动变成会话标题，避免列表里一堆「新对话」。
def append_message(
    settings: Settings,
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
    meta: Any = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """追加一条消息，并在**同一事务**内更新会话的标题 / 计数 / 更新时间 / 模式。

    首个用户提问会自动成为会话标题，避免出现一堆「新对话」。
    """
    ensure_schema(settings)
    # 按上限截断正文。_clip_len 返回 0 表示「不限制」——注意不能直接 content[:limit]，
    # 否则 limit=0 时切片 [:0] 会把正文静默清空成空串（与 _clip「limit<=0 不限制」同义）。
    limit = _clip_len(settings)
    text = (content or "")[:limit] if limit > 0 else (content or "")
    now = _now()
    message_id = _new_id()

    with _connect(settings) as conn, conn:
        # 先确认会话属于当前用户，同时取出「当前消息数、标题」备用。
        owned = conn.execute(
            "SELECT title, message_count FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if owned is None:
            raise HistoryNotFound("会话不存在")

        # 计算本条消息的序号 seq = 现有最大序号 + 1。
        # COALESCE(MAX(seq), 0)：没有消息时 MAX 返回 NULL，用 COALESCE 换成 0。
        seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["m"]
        ) + 1

        # 插入消息。seg 列存「分词结果」（供全文检索用），由 _index_terms 生成。
        conn.execute(
            """
            INSERT INTO messages (id, conversation_id, seq, role, content, seg, created_at, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                seq,
                role,
                text,
                " ".join(_index_terms(text)),
                now,
                _json_or_none(meta),
            ),
        )

        # 准备要更新会话的字段：消息数 +1、更新时间。
        fields = ["message_count = message_count + 1", "updated_at = ?"]
        values: list[Any] = [now]
        # 若传了模式，就一并更新。
        if mode:
            fields.append("mode = ?")
            values.append(mode)
        # 若是「首条用户提问」（角色是 user、原消息数为 0、序号为 1）→ 用它生成标题。
        if role == "user" and int(owned["message_count"] or 0) == 0 and seq == 1:
            fields.append("title = ?")
            values.append(title_from_question(text, settings.history_title_max_chars))
        # 最后追加 WHERE 用到的参数（会话 id、用户 id）。
        values.extend([conversation_id, user_id])
        # 动态拼出 UPDATE 语句（字段个数可变），执行。
        conn.execute(f"UPDATE conversations SET {', '.join(fields)} WHERE id = ? AND user_id = ?", values)

    # 返回插入结果（meta 返回原始对象，不是 JSON 文本）。
    return {
        "id": message_id,
        "conversation_id": conversation_id,
        "seq": seq,
        "role": role,
        "content": text,
        "created_at": now,
        "meta": meta,
    }


# _clip_len：取出「单条消息正文长度上限」；配置为 0 或负数时返回 0（表示不限制）。
# 语义与 _clip 一致：返回值 0（或负数）= 不限制，调用处必须按「limit>0 才截」处理。
def _clip_len(settings: Settings) -> int:
    limit = settings.history_max_message_chars
    return limit if limit and limit > 0 else 0


# list_messages：取会话内的消息。
# 参数 limit 表示「只取最近 N 条」，但返回时仍按正常顺序（从旧到新）排列。
def list_messages(
    settings: Settings,
    user_id: str,
    conversation_id: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """按顺序取会话内的消息；``limit`` 表示只取最近 N 条（仍按正序返回）。"""
    ensure_schema(settings)
    with _connect(settings) as conn:
        # 权限检查：必须是自己的会话。
        owned = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if owned is None:
            raise HistoryNotFound("会话不存在")
        if limit and limit > 0:
            # 只取最近 N 条：先在子查询里按 seq 倒序取 N 条，再外层按 seq 正序排回来。
            # 为什么用这种「子查询 + 两次排序」：直接 ORDER BY seq ASC LIMIT N 会取到最旧的 N 条，
            # 而我们要的是「最新的 N 条、但按从旧到新展示」。
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?
                ) ORDER BY seq ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        else:
            # 不限量：全部按顺序取。
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq ASC",
                (conversation_id,),
            ).fetchall()
    # 逐行转成给前端用的字典结构（meta 会从 JSON 文本还原成对象）。
    return [_message_dict(r) for r in rows]


# _message_dict：把数据库行转成给前端/上层用的消息字典。
# 注意 meta 用 _load_json 还原（库里存的是 JSON 文本）。
def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "seq": row["seq"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
        "meta": _load_json(row["meta"]),
    }


# --------------------------------------------------------------------------- #
# 历史搜索（关键词 + 时间范围 + 主题/模式）
# --------------------------------------------------------------------------- #
# _match_expression：把分词结果拼成 FTS5 的 MATCH 查询表达式。
# 生成形如： "市政" OR "政道" OR "道路" —— 即「命中任意一个词就算命中」。
# 用双引号把每个词包起来（FTS5 里双引号表示「精确的短语」）。
# 为什么安全：bm25.tokenize 只保留 [\w\u4e00-\u9fff]（字母数字下划线+汉字），
# 不会包含会破坏 FTS 语法的特殊字符，所以双引号包裹是安全的。
def _match_expression(tokens: list[str]) -> str:
    """把分词结果拼成 FTS5 MATCH 表达式。

    ``bm25.tokenize`` 只保留 ``[\\w\\u4e00-\\u9fff]``，因此双引号包裹是安全的。
    """
    return " OR ".join(f'"{t}"' for t in tokens)


# search_messages：多维度检索「我自己的」历史消息。这是历史搜索的核心入口。
# 三个维度可任意组合：
#   1) 关键词 —— FTS5 分词匹配；不支持 FTS 时降级为 LIKE 模糊匹配；
#   2) 时间范围 —— 含当天整日（由 _normalize_range 补全）；
#   3) 主题与模式 —— 作用在会话上（c.mode / c.title）。
# 返回：(结果列表, 总条数)。每条结果里的 snippet 是「带 <em> 高亮的预览片段」。
def search_messages(
    settings: Settings,
    user_id: str,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    mode: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """多维度检索自己的历史消息。

    三个维度可任意组合：关键词（FTS5 分词匹配，不支持时降级 LIKE）、
    时间范围（含当天整日）、主题与模式（作用在会话上）。
    """
    ensure_schema(settings)
    keyword = (keyword or "").strip()
    # 查询词元（jieba 词 + 二字组合）；预览高亮另用 jieba 词，尽量贴近用户输入
    # match_terms：给 FTS 用（不含单字）；preview_tokens：给预览高亮用（纯 jieba 词）。
    match_terms = _query_terms(keyword) if keyword else []
    preview_tokens = tokenize(keyword) if keyword else []
    # 归一化时间范围（补全成完整时刻）。
    start, end = _normalize_range(date_from, date_to)

    # 条件与参数从「只能看自己的」开始。
    where = ["c.user_id = ?"]
    params: list[Any] = [user_id]
    # 时间比较前，值侧先归一到 UTC（新 T 格式 / 老 naive 格式混存，见 _created_at_utc_sql），
    # 与 _normalize_range 算出的 UTC 边界同轴。
    created_utc = _created_at_utc_sql("m.created_at")
    if start:
        where.append(f"{created_utc} >= ?")
        params.append(start)
    if end:
        where.append(f"{created_utc} <= ?")
        params.append(end)
    if mode:
        where.append("c.mode = ?")
        params.append(mode)

    # 基础 FROM：消息表 m 关联会话表 c（消息本身不带用户/模式，要靠会话表）。
    join = "FROM messages m JOIN conversations c ON c.id = m.conversation_id"
    # 决定走哪条路：有关键词 + 有词元 + 本机支持 FTS → 用 FTS；否则若有词 → LIKE。
    use_fts = bool(keyword) and bool(match_terms) and fts_available(settings)
    if use_fts:
        # 注意：FTS5 的 MATCH 左侧必须写真实表名（别名会报 no such column）
        # 再关联 FTS 索引表（用 rowid 对齐）。
        join += " JOIN messages_fts ON messages_fts.rowid = m.rowid"
        where.append("messages_fts MATCH ?")
        params.append(_match_expression(match_terms))
    elif keyword:
        # 降级：正文或标题里「包含该关键词」就算命中。
        where.append("(m.content LIKE ? OR c.title LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])

    clause = " AND ".join(where)
    with _connect(settings) as conn:
        try:
            # 先查总数。
            total = int(conn.execute(f"SELECT COUNT(*) AS c {join} WHERE {clause}", params).fetchone()["c"])
            # 再查当页；按时间倒序（最新的在前），同一时刻按 rowid 倒序稳定排序。
            rows = conn.execute(
                f"""
                SELECT m.id AS message_id, m.conversation_id AS conversation_id, m.role AS role,
                       m.content AS content, m.created_at AS created_at,
                       c.title AS conversation_title, c.mode AS mode
                {join}
                WHERE {clause}
                ORDER BY m.created_at DESC, m.rowid DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS 索引损坏或语法异常：这一轮退化为 LIKE，保证搜索始终可用
            return _search_like(
                settings, user_id, keyword, start, end, mode, limit, offset
            )

    # 组装结果条目。snippet 的「词元」优先用 jieba 词，没有就用关键词本身兜底。
    items = [
        {
            "message_id": r["message_id"],
            "conversation_id": r["conversation_id"],
            "conversation_title": r["conversation_title"],
            "role": r["role"],
            "created_at": r["created_at"],
            "mode": r["mode"],
            "snippet": _build_preview(r["content"], preview_tokens or ([keyword] if keyword else [])),
        }
        for r in rows
    ]
    return items, total


# _search_like：搜索的「降级实现」——不用 FTS，改用 LIKE 做模糊匹配。
# 什么时候用：本机 SQLite 不支持 FTS5，或 FTS 查询出了语法/索引错误时。
# 参数里 start/end 已经是「归一化后的时间」（调用方算好了传进来）。
# 代价：LIKE 是全表扫描，速度不如 FTS，但保证「搜索永远可用」。
def _search_like(
    settings: Settings,
    user_id: str,
    keyword: str | None,
    start: str | None,
    end: str | None,
    mode: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    # 条件与参数（与 search_messages 类似，但关键词走 LIKE）。
    where = ["c.user_id = ?"]
    params: list[Any] = [user_id]
    # 时间比较与主路径同一套归一化（start/end 已是 UTC 边界，值侧同样归一到 UTC）。
    created_utc = _created_at_utc_sql("m.created_at")
    if start:
        where.append(f"{created_utc} >= ?")
        params.append(start)
    if end:
        where.append(f"{created_utc} <= ?")
        params.append(end)
    if mode:
        where.append("c.mode = ?")
        params.append(mode)
    # 关键词：正文或标题包含即可。
    if keyword:
        where.append("(m.content LIKE ? OR c.title LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    clause = " AND ".join(where)
    join = "FROM messages m JOIN conversations c ON c.id = m.conversation_id"

    with _connect(settings) as conn:
        # 先总数，再当页数据（按时间倒序）。
        total = int(conn.execute(f"SELECT COUNT(*) AS c {join} WHERE {clause}", params).fetchone()["c"])
        rows = conn.execute(
            f"""
            SELECT m.id AS message_id, m.conversation_id AS conversation_id, m.role AS role,
                   m.content AS content, m.created_at AS created_at,
                   c.title AS conversation_title, c.mode AS mode
            {join}
            WHERE {clause}
            ORDER BY m.created_at DESC, m.rowid DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()

    # 预览高亮词元用 jieba 词。
    preview_tokens = tokenize(keyword) if keyword else []
    items = [
        {
            "message_id": r["message_id"],
            "conversation_id": r["conversation_id"],
            "conversation_title": r["conversation_title"],
            "role": r["role"],
            "created_at": r["created_at"],
            "mode": r["mode"],
            "snippet": _build_preview(r["content"], preview_tokens or ([keyword] if keyword else [])),
        }
        for r in rows
    ]
    return items, total


# --------------------------------------------------------------------------- #
# 统计（/api/health 用）
# --------------------------------------------------------------------------- #
# stats：返回库内规模概览（有多少用户、会话、消息，以及是否支持 FTS）。
# 关键设计：**库还没建出来时也要能安全返回**——用 try/except 兜住，出错就返回全 0。
# 为什么：健康检查接口不该因为「数据库没准备好」而报错。
def stats(settings: Settings | None = None) -> dict[str, Any]:
    """库内规模概览；库还没建出来时也要能安全返回。"""
    s = settings or get_settings()
    # 兜底返回值（全 0、不支持 FTS）。
    empty = {"users": 0, "conversations": 0, "messages": 0, "fts": False}
    # 功能没开 → 直接返回兜底值。
    if not s.history_configured:
        return empty
    try:
        # 确保表已建好，再分别统计三张表的行数。
        ensure_schema(s)
        with _connect(s) as conn:
            users = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
            conversations = int(conn.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()["c"])
            messages = int(conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"])
        return {
            "users": users,
            "conversations": conversations,
            "messages": messages,
            "fts": fts_available(s),
        }
    # 任何数据库错误都吞掉，返回兜底值（保证健康检查不失败）。
    except sqlite3.Error:
        return empty