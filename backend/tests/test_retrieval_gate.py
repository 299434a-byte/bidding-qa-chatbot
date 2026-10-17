# ============================================================================
# 文件：tests/test_retrieval_gate.py —— Self-RAG 检索判定（needs_retrieval）测试
#
# 【这个测试文件整体在测什么】
#   背景：不是每个用户输入都值得进知识库检索——寒暄、致谢、问机器人本身、
#   纯算术、「换个说法」这类改写指令，检索回来的资料只会干扰生成。
#   本文件锁定 rag.needs_retrieval 的判定口径与流式入口的接线：
#     ① 六类「不需要检索」的正例（寒暄/致谢/元问题/纯算术/改写指令/语气词应答）；
#     ② 十类「需要检索」的反例——含任务书点名的「最近有哪些市政道路的招标公告」
#        与「预算超过一百万的项目有几个」，宁可多检索，不可漏检索；
#     ③ 流式入口：跳过检索时绝不调 retriever、不发 sources 事件，
#        retrieval.meta.skipped_retrieval=true，done.mode="direct"；
#     ④ metrics：每次跳过让 retrieval_skipped 计数 +1。
#   全部离线：纯函数 / monkeypatch，不连大模型、不连向量库。
# ============================================================================

from __future__ import annotations

# 被测对象：检索判定纯函数。
from app.rag import needs_retrieval

# ----------------------------------------------------------------------------
# 用例 1：六类「不需要检索」的正例 → needs_retrieval 返回 False
# ----------------------------------------------------------------------------
def test_six_categories_of_questions_skip_retrieval():
    """寒暄 / 致谢 / 元问题 / 纯算术 / 改写指令 / 语气词应答：全部判 False。"""
    skip_cases = {
        # ① 寒暄
        "寒暄": ["你好", "您好", "在吗", "hello", "早上好"],
        # ② 致谢
        "致谢": ["谢谢", "多谢你", "辛苦了", "thank you"],
        # ③ 元问题（问机器人本身）
        "元问题": ["你是谁", "你能做什么", "怎么用这个", "帮助"],
        # ④ 纯算术表达式
        "纯算术": ["1+1等于几", "12*8", "(100-25)/5", "30%+15%"],
        # ⑤ 改写上一条的指令
        "改写指令": ["换个说法", "再短一点", "重新回答", "展开说说"],
        # ⑥ 语气词应答
        "语气词应答": ["嗯好的", "收到", "ok", "明白了"],
    }
    for category, questions in skip_cases.items():
        for question in questions:
            assert needs_retrieval(question) is False, f"{category}: {question!r} 不该进检索"


# ----------------------------------------------------------------------------
# 用例 2：十类「需要检索」的反例 → needs_retrieval 返回 True
# ----------------------------------------------------------------------------
def test_ten_categories_of_questions_still_need_retrieval():
    """业务问题（含点名的两条）一律 True——判不准就检索，宁可多检索，不可漏检索。"""
    need_cases = [
        # ① 点名：时间范围 + 业务关键词
        "最近有哪些市政道路的招标公告",
        # ② 点名：带阈值的统计问题
        "预算超过一百万的项目有几个",
        # ③ 主体 + 业务动作
        "某某公司中标了哪些项目",
        # ④ 年份 + 预算（数字长得像算术题，但不是算术）
        "2024年市政道路项目的预算是多少",
        # ⑤ 资质类业务问题
        "空调采购项目需要什么资质",
        # ⑥ 关系类问题（关系 + 统计的复合场景）
        "A公司和B公司共同参与过哪些项目",
        # ⑦ 规定 / 制度类问题
        "招标公告的投标截止时间一般怎么规定",
        # ⑧ 极值统计
        "哪家公司的中标金额最高",
        # ⑨ 疑问词 + 角色（「招标方是谁」绝不能被「你是谁」的子串规则误伤）
        "招标方是谁",
        # ⑩ 寒暄打头的混合问题（前缀命中规则也不放行，主体仍是业务问题）
        "你好，请问最近有哪些市政道路的招标公告",
    ]
    for question in need_cases:
        assert needs_retrieval(question) is True, f"{question!r} 必须进检索"

    # 边界：空 / 纯空白 / None 判不准 → 一律 True 走原链路。
    for question in ("", "   ", None):
        assert needs_retrieval(question) is True


# ----------------------------------------------------------------------------
# 用例 3：流式入口接线——跳过检索时不调 retriever、不发 sources，标 skipped_retrieval
# ----------------------------------------------------------------------------
def test_stream_skips_retriever_and_marks_skipped_retrieval(monkeypatch):
    """「你好」走直答通道：retrieve 绝不被调用、无 sources/citation_check 事件，
    retrieval.meta.skipped_retrieval=true，done.mode=\"direct\"。"""
    from app import rag
    from app.config import get_settings

    def _boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("跳过检索的直答通道不该调用 retriever")

    monkeypatch.setattr(rag, "retrieve", _boom)
    monkeypatch.setattr(rag, "stream_chat", lambda *args, **kwargs: iter(["你好！有什么可以帮你？"]))

    events = list(rag.answer_events("你好", settings=get_settings()))
    types = [e["type"] for e in events]

    # 检索事件打头并标注跳过；sources / citation_check 绝不出现。
    assert types[0] == "retrieval"
    assert events[0]["meta"]["skipped_retrieval"] is True
    assert events[0]["meta"]["final_count"] == 0
    assert "sources" not in types
    assert "citation_check" not in types
    # 正文流 + 直答收尾。
    assert "delta" in types
    done = events[types.index("done")]
    assert done["mode"] == "direct"
    assert done["skipped_retrieval"] is True


# ----------------------------------------------------------------------------
# 用例 4：业务问题照走完整检索链路（gate 不误伤）
# ----------------------------------------------------------------------------
def test_business_question_still_retrieves(monkeypatch):
    """需要检索的问题走原链路：retrieval.meta 里没有 skipped_retrieval 标注。"""
    from app import rag
    from app.config import get_settings
    from app.retriever import RetrievalResult

    fake_hit = {
        "doc_id": "bidding_notices:1:0",
        "table": "bidding_notices",
        "label": "招标公告",
        "primary_key": 1,
        "score": 0.9,
        "text": "本项目预算金额为 1590.0 万元，资金已落实。",
        "fields": {"project_name": "某某工程"},
    }
    monkeypatch.setattr(
        rag,
        "retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[dict(fake_hit)], dense_count=1, sparse_count=0, bm25_ready=True
        ),
    )
    monkeypatch.setattr(
        rag, "stream_chat", lambda *args, **kwargs: iter(["该项目预算为 1590 万元 [参考资料1]。"])
    )

    events = list(
        rag.answer_events("最近有哪些市政道路的招标公告", settings=get_settings())
    )
    types = [e["type"] for e in events]

    assert types[0] == "retrieval"
    assert events[0]["meta"].get("skipped_retrieval") is not True
    assert "sources" in types  # 检索链路照常发来源事件


# ----------------------------------------------------------------------------
# 用例 5：metrics——每次跳过让 retrieval_skipped 计数 +1
# ----------------------------------------------------------------------------
def test_retrieval_skip_bumps_metrics_counter(tmp_path):
    """连问两句寒暄：summary 的 retrieval_skipped 从 0 变 2（独立库文件，互不污染）。"""
    import dataclasses

    from app import metrics as metrics_store
    from app import rag
    from app.config import get_settings

    settings = dataclasses.replace(get_settings(), metrics_db_path=tmp_path / "m.db")
    assert metrics_store.summary(settings)["retrieval_skipped"] == 0

    monkey = _NoRetrievalStub()
    monkey.install(rag)
    try:
        list(rag.answer_events("你好", settings=settings))
        list(rag.answer_events("谢谢", settings=settings))
    finally:
        monkey.restore()

    assert metrics_store.summary(settings)["retrieval_skipped"] == 2


class _NoRetrievalStub:
    """把 rag.retrieve / rag.stream_chat 换成不联网的桩，测完还能还原。"""

    def __init__(self):
        self._saved: list[tuple[object, str, object]] = []

    def install(self, module) -> None:
        self._saved.append((module, "retrieve", module.retrieve))
        self._saved.append((module, "stream_chat", module.stream_chat))
        module.retrieve = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("跳过检索不该调 retriever")
        )
        module.stream_chat = lambda *a, **k: iter(["好的"])

    def restore(self) -> None:
        for module, name, original in reversed(self._saved):
            setattr(module, name, original)
