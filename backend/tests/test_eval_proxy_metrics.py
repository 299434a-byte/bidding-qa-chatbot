# ============================================================================
# 文件：tests/test_eval_proxy_metrics.py —— 生成层代理指标的计算口径
#
# 【这个测试文件整体在测什么】
#   eval/eval_answer.py 的 proxy_metrics：复用 citation 引用校验，零标注产出两个
#   生成层代理指标（faithfulness / context_precision）。锁三件事：
#     ① faithfulness 通过率 = 校验通过的回答数 ÷ 有回答的总数（空回答不进分母）；
#     ② 无依据条数占比 = issues 条数 ÷ checked 总条数（checked 见 citation.verify_citations）；
#     ③ context_precision = 逐题「有效引用编号数 ÷ 本轮来源总数」的均值，无来源的题不参与；
#   以及分母为 0 时返回 None（渲染为「—」，不编造 0 分）。
#   纯函数离线测试：不连大模型、不跑问答链路。
# ============================================================================

from __future__ import annotations

import pytest

from eval.eval_answer import proxy_metrics

# 一条 1590 万元有据的来源 + 一条无关来源（供 context_precision 的分母=2）。
_S1 = {"text": "预算 1590.0 万元，资金已落实。", "fields": {}}
_S2 = {"text": "招标人为某某单位，采用公开招标方式。", "fields": {}}


# ----------------------------------------------------------------------------
# 用例 1：两个指标的分子分母口径
# ----------------------------------------------------------------------------
def test_proxy_metrics_rates_and_precision() -> None:
    """通过率按「有回答」算，无依据占比按 checked 条数算，precision 按引用占比均值算。"""
    rows = [
        # 通过：引用 [参考资料1] 且 1590 万元在该来源里有依据；2 条来源只引 1 条 → 0.5
        {"answer": "预算 1590 万元 [参考资料1]。", "sources": [dict(_S1), dict(_S2)]},
        # 不通过：9999 万元无依据（checked 1 条数值、issues 1 条）；没引用 → 0/1
        {"answer": "预算 9999 万元。", "sources": [dict(_S1)]},
        # 空回答：不进任何分母。
        {"answer": "", "sources": []},
    ]

    px = proxy_metrics(rows)

    assert px["answered"] == 2
    assert px["faith_pass"] == 1
    assert px["faith_pass_rate"] == pytest.approx(0.5)
    assert px["unsupported"] == 1
    assert px["checked_total"] == 3  # 题1：1 引用 + 1 数值；题2：1 数值
    assert px["unsupported_rate"] == pytest.approx(1 / 3)
    assert px["with_sources"] == 2
    assert px["context_precision"] == pytest.approx((0.5 + 0.0) / 2)


# ----------------------------------------------------------------------------
# 用例 2：分母为 0 → None（不编造 0 分）
# ----------------------------------------------------------------------------
def test_proxy_metrics_no_denominator_returns_none() -> None:
    """没有可算的题时三个比率都返回 None，由渲染层显示「—」。"""
    px = proxy_metrics([{"answer": "", "sources": []}])

    assert px["answered"] == 0
    assert px["faith_pass_rate"] is None
    assert px["unsupported_rate"] is None
    assert px["context_precision"] is None
