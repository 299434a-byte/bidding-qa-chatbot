"""answer_cache 的单元测试。

覆盖三件事（对应用户明确要求）：
  1) 规范化键命中 —— 大小写 / 全角半角 / 多余空白的差异都算同一个问题；
  2) TTL 过期 —— 超过 CACHE_TTL 的条目当作未命中；
  3) 不该缓存的请求不写缓存 —— is_cacheable 的各项「一票否决」判断。

【为什么用 SimpleNamespace 造「配置替身」】
  真正的 Settings 是个冻结的大对象，构造起来麻烦。而 answer_cache 读取的只有
  `cache_db_path` 和 `cache_ttl` 两个字段，所以用 SimpleNamespace 造个只有这两个
  字段的小对象就够了，测试更聚焦、也不依赖 .env。
"""

from __future__ import annotations

# datetime：测试 TTL 时要手工把 created_at 改成「过去的时间」。
import datetime as dt
# sqlite3：直接改数据库里那一行的 created_at，模拟「条目已变老」。
import sqlite3
# SimpleNamespace：造一个「只有指定字段」的轻量配置替身。
from types import SimpleNamespace

# answer_cache：被测模块。
from app import answer_cache


def _settings(tmp_path, ttl: int = 3600) -> SimpleNamespace:
    """造一个只含缓存相关字段的配置替身（数据库落在 pytest 的临时目录里）。"""
    return SimpleNamespace(cache_db_path=tmp_path / "answer_cache.db", cache_ttl=ttl)


def test_normalize_question_unifies_forms() -> None:
    """规范化：大小写、全角半角、多余空白都应被抹平成一个键。"""
    # 基本差异：大小写 + 连续空格。
    assert answer_cache.normalize_question("Hello   World") == "hello world"
    # 全角：NFKC 会把全角字母/空格转成半角，再小写化。
    assert answer_cache.normalize_question("Ｈｅｌｌｏ　World") == "hello world"
    # 中文 + 首尾空白：只去空白、不改语义。
    assert answer_cache.normalize_question("  市政道路招标  ") == "市政道路招标"


def test_normalized_key_hits(tmp_path) -> None:
    """写入后，用「书写不同但规范后相同」的问题查询应命中。"""
    s = _settings(tmp_path)
    # 写入一条缓存。
    assert answer_cache.set_cached("Hello   World", "答案正文", [{"title": "来源1"}], "llm", s) is True

    # 用「大小写 + 空白」不同的写法查询 → 规范后相同 → 命中。
    hit = answer_cache.get_cached("hello world", s)
    assert hit is not None
    assert hit["answer"] == "答案正文"
    # sources 应当被 JSON 还原成列表。
    assert hit["sources"] == [{"title": "来源1"}]
    assert hit["mode"] == "llm"

    # 换一个语义不同的问题 → 不应命中（证明是「精确匹配」而非模糊匹配）。
    assert answer_cache.get_cached("hello there", s) is None


def test_ttl_expiry(tmp_path) -> None:
    """超过 TTL 的条目应被当作未命中。"""
    # TTL 设为 1 小时。
    s = _settings(tmp_path, ttl=3600)
    assert answer_cache.set_cached("问题", "答案", [], "llm", s) is True

    # 刚写入时应当命中（未过期）。
    assert answer_cache.get_cached("问题", s) is not None

    # 手工把 created_at 改成「2 小时前」——已超过 1 小时 TTL。
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=7200)).isoformat()
    conn = sqlite3.connect(str(s.cache_db_path))
    try:
        conn.execute("UPDATE answer_cache SET created_at = ?", (past,))
        conn.commit()
    finally:
        conn.close()

    # 再查 → 应判定为过期，返回 None。
    assert answer_cache.get_cached("问题", s) is None


def test_is_cacheable_guardrails() -> None:
    """is_cacheable 是「读写缓存共用的一票否决准绳」，逐条验证。

    这些正是「不该缓存的请求」：未登录 / deep / 带历史 / 联网 / 带图片 / 疑似敏感。
    两个问答入口在「查缓存」和「写缓存」前都会先问它，返回 False 就绝不触碰缓存。
    """
    # 基线：已登录 + react + 无历史/联网/图片 + 普通问题 → 可缓存。
    assert answer_cache.is_cacheable(logged_in=True, mode="react", question="市政道路招标") is True

    # ① 未登录 → 不缓存（避免匿名请求互相串成果、避免无上限刷缓存）。
    assert answer_cache.is_cacheable(logged_in=False, question="市政道路招标") is False
    # ② deep 模式 → 不缓存。
    assert answer_cache.is_cacheable(logged_in=True, mode="deep", question="市政道路招标") is False
    # ③ 带多轮历史 → 不缓存（上下文不同，答案可能不同）。
    assert answer_cache.is_cacheable(logged_in=True, has_history=True, question="市政道路招标") is False
    # ④ 开启联网搜索 → 不缓存（结果有时效性）。
    assert answer_cache.is_cacheable(logged_in=True, web_search=True, question="市政道路招标") is False
    # ⑤ 带图片解析 → 不缓存（可能含隐私）。
    assert answer_cache.is_cacheable(logged_in=True, has_image=True, question="市政道路招标") is False
    # ⑥ 疑似敏感信息 → 不缓存（明文落盘有泄密风险）。
    assert answer_cache.is_cacheable(logged_in=True, question="我的银行卡密码是多少") is False


def test_set_cached_failure_is_silent(tmp_path) -> None:
    """写缓存失败必须静默降级：返回 False，且不抛异常（硬要求）。"""
    # 把路径指到一个「父级是普通文件」的不可能目录，逼写入失败。
    bogus = tmp_path / "not_a_dir"
    bogus.write_text("x", encoding="utf-8")  # 占位成一个文件
    s = SimpleNamespace(cache_db_path=bogus / "answer_cache.db", cache_ttl=3600)

    # 不抛异常，只返回 False。
    assert answer_cache.set_cached("问题", "答案", [], "llm", s) is False
    # 读也应静默返回 None（同样不抛）。
    assert answer_cache.get_cached("问题", s) is None