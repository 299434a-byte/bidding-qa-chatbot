# ============================================================================
# 文件：answer_cache.py —— 问答结果的「本地缓存」
#
# 【这个文件是干什么的】
#   同一个问题，如果刚被人问过、答案还没过期，第二次就能直接把上次的答案端出来，
#   不用再跑「检索 → 重排 → 大模型生成」这一整套昂贵流程。既省钱（少调大模型）
#   又更快（用户几乎瞬间拿到答案）。
#
# 【关键澄清（非常重要，请勿误解）】
#   本缓存是「**规范化精确匹配**」，**不是向量语义匹配**。
#   大白话：
#     · 只有两次「问法几乎一模一样」的问题才会命中缓存（大小写/全角半角/多余空格
#       这些无关紧要的差异会被抹平，视为同一个问题）；
#     · 换了说法、意思相近但用词不同的问题（例如「市政道路招标」和「城市道路工程招标」）
#       会被当成两个不同的问题，**不会**命中。
#   之所以这么设计：模糊的语义匹配容易「张冠李戴」（把 A 问题的答案给了 B 问题），
#   精确匹配虽然保守，但绝不会答错。将来若要语义匹配，应另建索引，而不是改这里。
#
# 【设计约束（硬要求）】
#   缓存读写的任何失败都必须「静默降级」——即出错就当作「没缓存」，
#   绝不能因为缓存数据库坏了、被锁了，而让用户的问答请求失败。
#   所以本文件里每个对外函数都包了 try/except，出错时返回 None / False。
#
# 【为什么用 SQLite】与项目里 conversations.db 同一思路：标准库自带的轻量数据库，
#   零额外服务、就是磁盘上一个文件，随建随用。
# ============================================================================
"""问答缓存（SQLite 本地文件，规范化精确匹配）。

**注意：这是「规范化精确匹配」，不是向量语义匹配。** 只有把问题做「大小写、全角半角、
多余空白」归一化之后字符串完全相同，才算命中；近义改写不算命中。

对外接口：
    · ``normalize_question(q)`` —— 生成规范化键（也就是缓存主键）；
    · ``is_cacheable(...)``     —— 判断「这个请求该不该进缓存」；
    · ``get_cached(q)``         —— 查缓存，未命中/过期/出错都返回 ``None``；
    · ``set_cached(q, ...)``    —— 写缓存，失败静默返回 ``False``。

所有读写失败一律静默降级：缓存是「锦上添花」，绝不能因为它坏了而影响问答。
"""

from __future__ import annotations

# datetime as dt：生成/解析缓存条目的写入时间，用来算「有没有过期」。
import datetime as dt
# json：把「参考来源列表」序列化成 JSON 文本存进数据库的一列，读回来再还原成列表。
import json
# re：正则，用来把连续空白（空格/制表符/换行）压成一个空格。
import re
# sqlite3：标准库自带的轻量数据库，缓存就存在一个本地 .db 文件里。
import sqlite3
# threading：锁，保证「建表只做一次」，避免多线程同时执行建表语句打架。
import threading
# unicodedata：做 NFKC 归一化，把「全角字符」转成「半角字符」。
import unicodedata
# contextmanager：把「打开连接 / 用完关闭」包装成 `with` 可用的上下文管理器。
from contextlib import contextmanager
# Path：优雅地处理文件路径（自动建上级目录）。
from pathlib import Path
# 类型标注：Any=任意类型；Iterator=可被逐个取值的对象。
from typing import Any, Iterator

# Settings/get_settings：读取 .env 配置（缓存库路径、TTL）。
from .config import Settings, get_settings
# kb_version：知识库版本指纹（缓存与知识库版本解耦的关键判据）。
# 只读本地小文件、绝不开 Qdrant；读失败降级 "unknown"，详见 app/kb_version.py。
from . import kb_version

# 建表语句（DDL）。用 CREATE TABLE IF NOT EXISTS —— 「不存在才建」，所以反复执行也安全。
# 【字段说明（大白话）】
#   cache_key    —— 规范化后的提问，作为主键（同一个键只存一条，重复写会覆盖）；
#   question     —— 用户原始问题（保留原样，方便人工查看缓存里都存了啥）；
#   answer       —— 大模型给出的完整回答正文；
#   sources_json —— 参考来源列表（JSON 文本），命中时能一并把「来源卡片」还原出来；
#   mode         —— 结束模式（如 llm / agent_rag），命中时原样回放给前端；
#   created_at   —— 写入时间（UTC 的 ISO 文本），用来判断是否超过 TTL。
# 【kb_version 列（缓存与知识库版本解耦）】缓存的正确性依赖「答案是针对哪一版知识库」，
#   知识库一重建旧答案就可能失效。所以每行都记下写入时的知识库版本指纹
#   （见 app/kb_version.py），查询时用「cache_key 相同 且 kb_version 相同」双条件命中。
#   主键仍是 cache_key（一问一行）：版本变后旧行查不到（版本对不上），
#   下次同问再写入会 INSERT OR REPLACE 覆盖掉旧行 —— 不会残留旧版本垃圾行。
_DDL = """
CREATE TABLE IF NOT EXISTS answer_cache (
    cache_key    TEXT PRIMARY KEY,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]',
    mode         TEXT NOT NULL DEFAULT 'llm',
    created_at   TEXT NOT NULL,
    kb_version   TEXT NOT NULL DEFAULT 'unknown'
);
"""

# 【已有老库的兜底】老版本建的 answer_cache 表没有 kb_version 列，
# ALTER TABLE 补一列（默认 'unknown'）；老行版本为 unknown，天然与任何真实版本对不上
# → 知识库升级后老缓存自动全部不命中（安全侧：宁可重算，不用旧答案）。
_ALTER_ADD_KB_VERSION = "ALTER TABLE answer_cache ADD COLUMN kb_version TEXT NOT NULL DEFAULT 'unknown'"

# 建表只需做一次；按库文件的绝对路径记录，避免每次请求都跑一遍 DDL。
# 【为什么用「集合」】它天然去重（同一个路径写两遍也只算一个），判断「建过没」也快。
_schema_ready: set[str] = set()
# 多线程同时进来时，用这把锁保证「只有一个线程在建表」，避免重复执行 DDL。
_schema_lock = threading.Lock()

# 【敏感信息护栏（粗筛）】命中这些模式的问题不进缓存。
# 【为什么要它（对应硬要求「不要缓存含敏感信息的请求」）】
#   缓存会把「问题 + 答案」明文落盘，长期留存；如果问题里夹带了口令、身份证号、
#   银行卡号这类敏感内容，等于把它们也一起存下来了。这里做一次「尽力而为」的粗筛，
#   命中就跳过缓存。
# 【注意事项（诚实说明局限）】这是靠关键词/正则的粗筛，不可能 100% 覆盖；
#   它只用于「刻意避免明显敏感内容入缓存」，不是安全审计手段。
_SENSITIVE_RE = re.compile(
    r"密码|口令|passwd|password|api[\s_-]?key|apikey|secret|token|身份证|银行卡|信用卡",
    re.IGNORECASE,
)


def normalize_question(question: str) -> str:
    """把问题规范化成「缓存键」。

    规范化只抹平「与语义无关的书写差异」，顺序如下：
      1) 全角转半角（NFKC）：例如全角「ＡＢＣ」「　（全角空格）」都归一到半角；
      2) 去掉首尾空白；
      3) 整体转小写：例如「Hello」与「HELLO」视为同一个问题；
      4) 把连续空白压成一个空格：例如「招标   公告」与「招标 公告」视为同一个问题。
    最终结果既是「规范化问题」，也直接当作数据库主键 ``cache_key``。
    """
    # 【为什么要防 None】调用方可能传进来 None（类型标注是 str，但运行时不一定）。
    # 用 `question or ""` 兜底成空字符串，避免下面 .normalize 直接崩。
    text = question or ""
    # NFKC 是一种「兼容归一化」：把长得不一样、但含义相同的字符统一成一种写法。
    # 最典型的就是全角字符 → 半角字符（全角「１」→ 半角「1」）。
    text = unicodedata.normalize("NFKC", text)
    # 去掉首尾空白（含换成半角后的空格）。
    text = text.strip()
    # 统一转小写：英文大小写不该让同一个问题变成两条缓存。
    text = text.lower()
    # \s+ 表示「一个或多个空白字符」；替换成单个空格，把多余空白压平。
    text = re.sub(r"\s+", " ", text)
    # 压平后可能又出现首尾空格，再去一次。
    return text.strip()


# 语义别名：缓存键就是「规范化问题」。单独给个名字，让调用处读起来更直白。
def cache_key(question: str) -> str:
    """缓存主键 = 规范化问题（见 :func:`normalize_question`）。"""
    return normalize_question(question)


def looks_sensitive(question: str) -> bool:
    """粗筛：问题里是否疑似含敏感信息（见 ``_SENSITIVE_RE``）。"""
    return bool(_SENSITIVE_RE.search(question or ""))


def is_cacheable(
    *,
    logged_in: bool,
    mode: str = "react",
    has_history: bool = False,
    web_search: bool = False,
    has_image: bool = False,
    question: str = "",
) -> bool:
    """判断「这个请求该不该进缓存」（读缓存与写缓存共用同一条准绳）。

    【为什么要有这个判断（硬要求）】有些请求天然「不适合缓存」，缓存了会出错或泄密：

      · ``logged_in=False``（未登录）—— 缓存是全局共享的；匿名请求不进缓存，
        避免把某个人的结果给到另一个人，也避免无上限的匿名刷缓存；
      · ``mode == "deep"`` —— 深度推理（Plan-and-Solve + Reflection）代价高、过程发散，
        更适合每次现算，不进缓存；
      · ``has_history=True``（带多轮历史）—— 同一个问题在不同上下文里答案可能不同，
        带上下文的问题不该用「无上下文的缓存」去答；
      · ``web_search=True``（开启联网搜索）—— 联网结果具有时效性，缓存一小时可能就过期失真；
      · ``has_image=True``（带图片解析）—— 图文内容可能含隐私，不进缓存；
      · ``looks_sensitive`` —— 问题疑似含敏感信息，不进缓存。

    只要命中以上任一条，就返回 ``False``（不读、也不写缓存）。
    """
    if not logged_in:
        return False
    if mode == "deep":
        return False
    if has_history:
        return False
    if web_search:
        return False
    if has_image:
        return False
    if looks_sensitive(question):
        return False
    return True


def is_cache_readable(
    *,
    logged_in: bool,
    mode: str = "react",
    has_image: bool = False,
    question: str = "",
) -> bool:
    """读缓存的准绳（写入侧仍用 :func:`is_cacheable` —— 读写两把尺子刻意不对称）。

    【为什么读写不对称（缓存命中率修复，2026-10）】实测 cache_hit_rate 只有 2.4%，
    排查发现两道闸门把「明明该命中」的请求全拦了：

      · ``has_history`` 不拦读 —— 写入侧照旧拒绝「带历史生成」的答案（那份答案依赖
        上下文，不能当标准答案存起来），所以缓存里只可能是「无历史、独立成问」的答案；
        问题文本精确命中拿到的就是该问题的标准答案，带历史把同一句话再问一遍时
        直接复用即可。旧行为是「带历史一律不读不写」，结果 UI 多轮对话永远不命中。
      · ``web_search`` 不拦读 —— 写入侧只拒绝「答案里真的用了联网结果」的写入
        （时效性顾虑只对含联网内容的答案成立）。缓存里的答案永远不含联网内容，
        读出来不存在「一小时前的搜索结果」问题。旧行为是「允许联网就不读不写」，
        而 WEB_SEARCH_DEFAULT=true 的环境里所有请求都被误杀（实测第二问仍 45.9s）。

    仍然拦下的（与 is_cacheable 保持一致）：未登录 / deep 模式 / 带图片 / 疑似敏感。
    """
    if not logged_in:
        return False
    if mode == "deep":
        return False
    if has_image:
        return False
    if looks_sensitive(question):
        return False
    return True


# 把「打开连接 / 用完关闭」包装成上下文管理器。
# 用法：`with _connect(settings) as conn:` —— 进入时开连接，离开时自动关（异常也不会漏关）。
@contextmanager
def _connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    """打开一个 SQLite 连接（每次操作独立、用完即关）。"""
    # 取出缓存库文件路径，并确保它所在的目录存在（parents=True 会一路把上级目录建好）。
    path: Path = settings.cache_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=3：被别的写操作挡住时最多等 3 秒，超时才报 database is locked。
    conn = sqlite3.connect(str(path), timeout=3)
    # row_factory=Row：查询结果可以像字典一样按列名取值（row["answer"]），比位置下标好读。
    conn.row_factory = sqlite3.Row
    try:
        # WAL 模式：读写更少互相阻塞（与 conversations.db 保持一致）。
        conn.execute("PRAGMA journal_mode=WAL")
        # busy_timeout=3000：被占用时自动排队等待 3 秒，而不是立刻抛异常。
        conn.execute("PRAGMA busy_timeout=3000")
        yield conn
    finally:
        # 无论如何都关闭连接。
        conn.close()


def _ensure_schema(conn: sqlite3.Connection, path: Path) -> None:
    """确保表已建好（每个库文件只建一次）。"""
    # 把路径转成字符串风格，作为集合里的键。
    key = str(path)
    # 已经建过就直接返回，省掉一次 DDL 执行。
    if key in _schema_ready:
        return
    # 加锁：保证同一时刻只有一个线程在建表。
    with _schema_lock:
        # 【双重检查】拿到锁后再确认一次：可能刚才别人已经建好了。
        if key in _schema_ready:
            return
        # 执行建表语句（IF NOT EXISTS，重复执行也无害）。
        conn.execute(_DDL)
        # 【老库兜底】表已存在但缺 kb_version 列（老版本建的表）时补一列。
        # PRAGMA table_info 列出全部列名；没有 kb_version 就 ALTER TABLE 加上。
        # ALTER 语句本身对新库无害：新库建表时已有该列，这里不会执行到。
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(answer_cache)")}
        if "kb_version" not in cols:
            conn.execute(_ALTER_ADD_KB_VERSION)
        conn.commit()
        # 记下「这个库已经建过表」。
        _schema_ready.add(key)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 文本，作为 created_at。统一用 UTC，避免时区换算歧义。"""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(text: str) -> dt.datetime | None:
    """把 created_at 文本解析回时间对象；解析不了返回 None。"""
    try:
        value = dt.datetime.fromisoformat(text)
        # 若库里存的是「没有时区信息」的老格式，就补上 UTC，避免后面相减报错。
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value
    except Exception:
        return None


def get_cached(question: str, settings: Settings | None = None) -> dict[str, Any] | None:
    """查缓存。命中且未过期返回结果字典；未命中/过期/出错一律返回 ``None``。

    返回字典字段：``question`` / ``answer`` / ``sources``（已还原成列表）/ ``mode`` / ``created_at``。
    """
    # 取配置（不传就用 .env 的缓存）。
    s = settings or get_settings()
    # 【静默降级】任何异常都当作「没缓存」，绝不影响问答主流程。
    try:
        # 把问题规范化，得到主键。
        key = normalize_question(question)
        # 空问题不查（避免把空串当成一个有效缓存）。
        if not key:
            return None
        # 打开连接并确保表存在。
        with _connect(s) as conn:
            _ensure_schema(conn, s.cache_db_path)
            # 按「主键 + 知识库版本」取一条：版本对不上的一律当未命中
            # （知识库重建/更新后旧答案立即失效，见文件头与 app/kb_version.py）。
            row = conn.execute(
                "SELECT question, answer, sources_json, mode, created_at "
                "FROM answer_cache WHERE cache_key = ? AND kb_version = ?",
                (key, kb_version.kb_version(s)),
            ).fetchone()
        # 查不到 → 未命中。
        if row is None:
            return None
        # 判断是否过期：当前时间 - 写入时间 > TTL 就当作未命中。
        created = _parse_iso(row["created_at"])
        if created is not None:
            age_seconds = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
            if age_seconds > s.cache_ttl:
                return None
        # 还原 sources_json（坏数据用空列表兜底，不让它打断命中判断）。
        try:
            sources = json.loads(row["sources_json"] or "[]")
        except Exception:
            sources = []
        return {
            "question": row["question"],
            "answer": row["answer"],
            "sources": sources if isinstance(sources, list) else [],
            "mode": row["mode"],
            "created_at": row["created_at"],
        }
    except Exception:
        # 【关键】缓存坏掉/被锁/磁盘满……一律静默当作未命中。
        return None


def set_cached(
    question: str,
    answer: str,
    sources: list[dict[str, Any]] | None,
    mode: str,
    settings: Settings | None = None,
) -> bool:
    """写缓存。成功返回 ``True``；任何失败静默返回 ``False``（不影响问答）。"""
    s = settings or get_settings()
    # 【静默降级】写缓存失败不该让用户察觉，所以整体包 try。
    try:
        key = normalize_question(question)
        # 空主键或空答案没有缓存价值，直接跳过。
        if not key or not (answer or "").strip():
            return False
        # 把来源列表序列化成 JSON 文本（中文原样保留）。
        sources_json = json.dumps(sources or [], ensure_ascii=False)
        with _connect(s) as conn:
            _ensure_schema(conn, s.cache_db_path)
            # INSERT OR REPLACE：主键已存在就覆盖（等价于「更新」）。
            # kb_version 记下「这份答案是针对哪一版知识库」，查询时按版本命中。
            conn.execute(
                "INSERT OR REPLACE INTO answer_cache "
                "(cache_key, question, answer, sources_json, mode, created_at, kb_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, question, answer, sources_json, mode or "llm", _now_iso(), kb_version.kb_version(s)),
            )
            # 提交事务（不提交的话数据不会真正落盘）。
            conn.commit()
        return True
    except Exception:
        # 【关键】写失败静默吞掉。
        return False


def clear(settings: Settings | None = None) -> bool:
    """手动清空全部问答缓存。成功返回 True；失败静默返回 False（不影响问答）。

    【什么时候用】版本机制已让「知识库一变、旧缓存自动失效」，平时不需要清；
    这里提供的是运维兜底（比如怀疑缓存内容有问题时一键清空）。
    """
    s = settings or get_settings()
    try:
        with _connect(s) as conn:
            _ensure_schema(conn, s.cache_db_path)
            conn.execute("DELETE FROM answer_cache")
            conn.commit()
        return True
    except Exception:
        # 【静默降级】清不掉也不该影响问答主流程。
        return False