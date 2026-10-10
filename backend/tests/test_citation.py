# ============================================================================
# 文件：tests/test_citation.py —— 给「引用校验器」做体检
#
# 【这个测试文件整体在测什么】
#   被测对象是 backend/app/citation.py 的三个函数：
#     · normalize_numbers —— 金额/百分比/日期的归一化；
#     · verify_citations  —— 引用编号与数字有没有依据；
#     · strip_bad_refs    —— 只删坏的引用标记。
#   citation.py 是「回答生成完之后」的体检员：它误判一次，要么让正常回答被冤枉(误杀)、
#   要么让编造内容溜过去(漏检)。所以这里重点验证 **两类失败 + 一条防误杀红线**。
#   测试全程不调用任何大模型、不连数据库，纯本地函数。
# ============================================================================

from __future__ import annotations

# 【这几行在导入什么】被测的三个函数，以及 4 条拒答文案常量。
from app.citation import (
    REF_1_RETRIEVAL_EMPTY,
    REF_2_INSUFFICIENT,
    REF_3_OUT_OF_SCOPE,
    REF_4_CAPABILITY,
    normalize_numbers,
    strip_bad_refs,
    verify_citations,
)


def _src(text: str, fields: dict | None = None) -> dict:
    """造一条来源字典（校验只需要 text 与 fields 两样）。"""
    return {"text": text, "fields": fields or {}}


# ----------------------------------------------------------------------------
# 用例 1：引用不存在的 [参考资料9] → 必须判为失败
# ----------------------------------------------------------------------------
def test_ref_out_of_range_is_flagged():
    """回答引用了不存在的第 9 条来源，校验必须失败，并记入 bad_refs。"""
    # 【设计意图】「编造出处」比编数字更隐蔽也更严重——用户看到 [参考资料9] 会以为真有依据。
    #   这里来源只有 2 条，第 9 条根本不存在，必须被揪出来。
    sources = [_src("第一条公告正文。"), _src("第二条公告正文。")]
    answer = "根据招标文件，本项目采用公开招标方式 [参考资料9]。"

    result = verify_citations(answer, sources)

    # 整体不通过。
    assert result["ok"] is False
    # 第 9 条被登记为「要删的坏引用」。
    assert 9 in result["bad_refs"]
    # 问题类型必须是「引用越界」，而不是数值问题（两类问题不能混淆）。
    assert any(i["type"] == "ref_out_of_range" for i in result["issues"])


# ----------------------------------------------------------------------------
# 用例 2：编造金额「50万元」但来源里没有 → 必须判为失败
# ----------------------------------------------------------------------------
def test_fabricated_amount_is_flagged():
    """回答写了来源里根本不存在的金额，校验必须失败，并标为「数值无依据」。"""
    # 【设计意图】模型的典型幻觉是「编一个像模像样的数字」。
    #   这里来源正文里没有任何「50万」相关信息，回答却写了 50 万元，必须被抓。
    sources = [_src("某某工程的招标公告，未提及任何预算金额。")]
    answer = "该项目的预算金额为 50 万元 [参考资料1]。"

    result = verify_citations(answer, sources)

    assert result["ok"] is False
    # 问题类型必须是「数值无依据」（而不是引用越界——[参考资料1] 是真实存在的）。
    assert any(i["type"] == "number_no_source" for i in result["issues"])
    # 既然是数值问题，就不该把合法的第 1 条引用误登记成「坏引用」。
    assert result["bad_refs"] == []


# ----------------------------------------------------------------------------
# 用例 3（最重要）：来源写「1590.0万元」、回答写「1590万元」→ 必须判为通过（防误杀）
# ----------------------------------------------------------------------------
def test_amount_format_difference_passes():
    """同一金额的不同写法不能被误判——这是校验器的「防误杀」红线。"""
    # 【设计意图】「1590.0 万元」和「1590 万元」是同一个数，只是写法不同。
    #   如果校验器连这个都判成「编造」，那它会给正常回答疯狂误报，反而没法用。
    #   这条是四条用例里最重要的一条：宁可漏检，也绝不冤枉正常回答。
    sources = [_src("本项目预算金额为 1590.0 万元，资金已落实。")]
    answer = "该项目的预算金额为 1590 万元 [参考资料1]。"

    result = verify_citations(answer, sources)

    assert result["ok"] is True, f"正常写法差异被误杀：{result['issues']}"
    assert result["issues"] == []
    assert result["bad_refs"] == []


# ----------------------------------------------------------------------------
# 用例 4：完全无引用的正常回答 → 不能报错
# ----------------------------------------------------------------------------
def test_answer_without_refs_is_not_an_error():
    """没有引用标记的正常回答不是错误，校验应安静通过。"""
    # 【设计意图】并非所有回答都带 [参考资料N]（比如模型用自然语言概述，或问题本就没有
    #   明确出处）。「没有引用」不等于「有问题」，校验器不能因此报警。
    sources = [_src("某公告正文，金额 100 万元。")]
    answer = "根据现有资料，这是一个不含引用标记的正常回答。"

    result = verify_citations(answer, sources)

    assert result["ok"] is True
    assert result["issues"] == []


# ----------------------------------------------------------------------------
# 用例 5：归一化本身要「认得出」全角与千分位（支撑上面三条用例的基础能力）
# ----------------------------------------------------------------------------
def test_normalize_numbers_handles_fullwidth_and_thousands_separator():
    """全角数字、千分位、万元/元 都应归一化到同一个「元」数值。"""
    # 【设计意图】中文公告里「１５９０．０万元」「1,590万元」「15900000元」混着出现，
    #   归一化不统一，后面所有比对都会失效。这里一次性钉死这几种写法等价。
    assert 15900000.0 in normalize_numbers("预算 １５９０．０ 万元")   # 全角
    assert 15900000.0 in normalize_numbers("预算 1,590 万元")          # 千分位
    assert 15900000.0 in normalize_numbers("预算 15900000 元")         # 已是元
    # 日期也要统一成 YYYY-MM-DD（月/日补零）。
    assert "2024-08-02" in normalize_numbers("开标时间：2024年8月2日")


# ----------------------------------------------------------------------------
# 用例 6：strip_bad_refs 只删坏的、保留好的
# ----------------------------------------------------------------------------
def test_strip_bad_refs_removes_only_bad_ones():
    """删除坏引用时，正常的引用标记必须原样保留。"""
    # 【设计意图】用户明确要求「成功的不动」。这里 [参考资料9] 越界要删，
    #   [参考资料1] 合法要留，一刀切删全部就错了。
    answer = "这条依据来自 [参考资料1]，那条出自 [参考资料9]。"
    cleaned = strip_bad_refs(answer, [9])

    assert "[参考资料9]" not in cleaned   # 坏的删掉
    assert "[参考资料1]" in cleaned       # 好的保留
    # 没有坏引用时，函数必须「一字不改」地返回原文（逐字节一致的保证之一）。
    assert strip_bad_refs(answer, []) == answer


# ----------------------------------------------------------------------------
# 附加：4 条拒答文案要能区分「没检索到」和「没检索」
# ----------------------------------------------------------------------------
def test_refusal_texts_distinguish_not_found_from_not_searched():
    """REF-1 说的是「检索过但没找到」，REF-3/REF-4 说的是「没做检索」。"""
    # 【设计意图】用户必须能分清两种处境：知识库里确实没有（该补数据），
    #   还是系统压根没去查（问题不该由本系统回答）。措辞上要能看出区别。
    assert "检索" in REF_1_RETRIEVAL_EMPTY and "没有找到" in REF_1_RETRIEVAL_EMPTY
    assert "没有进行检索" in REF_3_OUT_OF_SCOPE
    assert "没有进行检索" in REF_4_CAPABILITY
    # 四条文案两两不同，避免出现「复制粘贴忘了改」。
    asserted = {REF_1_RETRIEVAL_EMPTY, REF_2_INSUFFICIENT, REF_3_OUT_OF_SCOPE, REF_4_CAPABILITY}
    assert len(asserted) == 4