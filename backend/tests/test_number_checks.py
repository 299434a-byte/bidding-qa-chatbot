# ============================================================================
# 文件：tests/test_number_checks.py —— 数字「逐字命中」明细体检
#
# 【这个测试文件整体在测什么】
#   verify_citations 返回的 number_checks 明细：答案里每个金额/百分比/日期
#   是否在引用来源中找到依据（found）。两个核心行为：
#     1) 单位不同但换算后同值（3.5 亿元 vs 35000 万元）→ 归一化判定命中，不误报；
#     2) 来源里完全没有的数字 → found=False，且照旧进入既有 issues 清单。
#   明细是新增字段：ok / issues / checked 的既有语义一个都不能变。
# ============================================================================

from __future__ import annotations

from app.citation import verify_citations


def _src(text: str, fields: dict | None = None) -> dict:
    """造一条来源字典（校验只需要 text 与 fields 两样）。"""
    return {"text": text, "fields": fields or {}}


def test_equivalent_amount_units_count_as_found():
    """答案写「3.5 亿元」、来源写「35000 万元」→ 归一化后同值，判定命中、不误报。"""
    # 【设计意图】模型把来源的「万元」改写成「亿元」是常见且合理的表达，
    #   换算后同值就不该被当成编造——这正是「归一化后判定」的意义。
    sources = [_src("该项目总投资 35000 万元，资金已落实。")]
    answer = "本项目总投资 3.5 亿元。"

    result = verify_citations(answer, sources)

    checks = {c["text"]: c for c in result["number_checks"]}
    assert checks["3.5 亿元"]["found"] is True
    assert checks["3.5 亿元"]["kind"] == "number"
    # 归一化等价 → 整体校验通过（不发冤案）。
    assert result["ok"] is True
    assert not any(i["type"] == "number_no_source" for i in result["issues"])


def test_missing_number_marked_not_found():
    """来源里没有的数字 → found=False，并照旧进入既有 issues（ok 变 False）。"""
    # 【设计意图】明细服务于「前端醒目标记」，但问题判定仍走原有 issues 通道，
    #   两条通道对同一数字的结论必须一致（found=False ⟺ number_no_source）。
    sources = [_src("该项目总投资 35000 万元，资金已落实。")]
    answer = "本项目总投资 4.2 亿元。"

    result = verify_citations(answer, sources)

    missing = [c for c in result["number_checks"] if c["kind"] == "number"]
    assert missing and missing[0]["text"] == "4.2 亿元"
    assert missing[0]["found"] is False
    assert result["ok"] is False
    assert any(i["type"] == "number_no_source" for i in result["issues"])


def test_number_checks_covers_percent_and_date_in_order():
    """明细覆盖百分比与日期两类，按答案中的出现顺序记录原文写法。"""
    sources = [_src("让利率为 50%。该公告于 2024年5月1日 发布。")]
    answer = "让利率 50%，公告发布于 2024-05-01。"

    result = verify_citations(answer, sources)

    assert [c["kind"] for c in result["number_checks"]] == ["percent", "date"]
    assert all(c["found"] is True for c in result["number_checks"])
    assert result["ok"] is True
