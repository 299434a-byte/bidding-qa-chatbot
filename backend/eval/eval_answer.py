# ============================================================================
# 文件：eval_answer.py —— 用大模型当「考官」给回答质量打分
#
# 【这个文件是干什么的】
#   检索指标只能回答「该找的原文有没有被捞回来」，回答不了「最终答复好不好」。
#   本文件对每道题跑一遍完整的问答链路（Agent，会用工具），把「问题 + 参考答案要点 +
#   模型回答」一起交给大模型考官，让它按三个维度各打 1~5 分：
#     · 忠实度（faithfulness）：有没有忠于资料、没瞎编；
#     · 相关度（relevance）：有没有答到点上；
#     · 完整性（completeness）：参考答案要点覆盖了没有。
#   另外零标注产出两个**生成层代理指标**（复用 citation 引用校验，非 RAGAS 原版）：
#     · faithfulness：引用校验通过率 + 无依据条数占比；
#     · context_precision：回答实际引用的来源编号数 ÷ 本轮来源总数（逐题取均值）。
#
# 【两条硬性纪律】
#   1) 考官提示词固定、temperature=0，保证同一份回答多次评分尽量一致；
#   2) 要求考官「只输出 JSON」。解析不出来时记 None（表示没评上），绝不自己乱编一个分数 ——
#      编出来的分数会让报告失真，宁可空着。
#
# 【怎么运行】
#   cd backend
#   ..\.venv\Scripts\python.exe eval\eval_answer.py
# ============================================================================
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent import agent_events  # noqa: E402
from app.citation import _extract_refs, verify_citations  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm import LLMError, LLMNotConfigured, complete_chat  # noqa: E402
from app.rerank import extract_json  # noqa: E402

# 维度顺序固定，后面渲染表格、算平均都按这个顺序来。
DIMENSIONS = ("faithfulness", "relevance", "completeness")
DIM_LABELS = {"faithfulness": "忠实度", "relevance": "相关度", "completeness": "完整性"}

# 【固定考官提示词】写死在这里、不随题目变化，是「评分口径统一」的前提。
JUDGE_SYSTEM_PROMPT = """你是一名严格、客观的评审员，负责评审问答系统的回答质量。
只输出 JSON，不要输出任何解释、前后缀或 Markdown 代码块。"""


def load_eval_set(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["items"]


def build_judge_messages(question: str, key_points: list[str], answer: str) -> list[dict[str, str]]:
    """拼考官提示词：问题 + 参考答案要点 + 待评回答 + 固定打分规则。"""
    points = "\n".join(f"- {p}" for p in key_points) or "（无）"
    user = (
        f"【问题】\n{question}\n\n"
        f"【参考答案要点】\n{points}\n\n"
        f"【待评回答】\n{answer or '（回答为空）'}\n\n"
        "请从以下三个维度各打 1~5 分的整数：\n"
        "- faithfulness（忠实度）：回答是否忠于参考资料、没有编造（1=大量编造，5=完全忠实）\n"
        "- relevance（相关度）：回答是否切题（1=完全不相关，5=完全切题）\n"
        "- completeness（完整性）：是否覆盖了参考答案要点（1=完全没覆盖，5=全部覆盖）\n"
        '只输出这个 JSON：{"faithfulness": 整数, "relevance": 整数, "completeness": 整数}'
    )
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_scores(raw: str) -> dict[str, int] | None:
    """把考官输出解析成三个整数分；任何一步不合法都返回 None（不编分）。"""
    data = extract_json(raw)
    if not isinstance(data, dict):
        return None
    scores: dict[str, int] = {}
    for dim in DIMENSIONS:
        value = data.get(dim)
        # 【为什么要求必须是 1~5 的整数】模型偶尔会返回 0、7、"优秀" 之类的东西；
        #   这些都不是有效评分，宁可整条记 None，也不做「夹到边界」的猜测。
        if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 5):
            return None
        scores[dim] = value
    return scores


async def run_question(question: str, s: Any) -> tuple[str, list[str], str | None, list[dict[str, Any]]]:
    """跑一遍完整问答链路，返回 (回答正文, 调用过的工具名列表, 错误信息, 本轮来源)。"""
    parts: list[str] = []
    tools: list[str] = []
    error: str | None = None
    sources: list[dict[str, Any]] = []
    async for event in agent_events(question, settings=s, mode="react"):
        etype = event.get("type")
        if etype == "delta":
            parts.append(event.get("text") or "")
        elif etype == "tool_call":
            name = event.get("name")
            if name and name not in tools:
                tools.append(name)
        elif etype == "sources":
            sources = event.get("sources") or []
        elif etype == "error":
            error = event.get("message")
    return "".join(parts).strip(), tools, error, sources


def proxy_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """生成层代理指标：faithfulness / context_precision（复用引用校验，零标注成本）。

    · faithfulness_pass_rate = 引用校验通过的回答数 ÷ 有回答的总数；
    · faithfulness_unsupported_rate = 无依据的数值/引用条数 ÷ 总校验条数；
    · context_precision = 每个回答「实际引用的来源编号数 ÷ 本轮来源总数」的均值。

    这是**代理指标，不是 RAGAS 原版**：本项目相关性标注只到 doc 级、没有多级相关性，
    硬套 RAGAS 的分级口径会失真；这里只用现成的 citation 校验产出可对比的量化数字。
    分母为 0（没有可算的题）时返回 None，渲染为「—」，不编造 0 分。
    """
    answered = [r for r in rows if (r.get("answer") or "").strip()]
    faith_pass = 0
    unsupported = 0
    checked_total = 0
    precisions: list[float] = []
    for row in answered:
        sources = row.get("sources") or []
        check = verify_citations(row["answer"], sources)
        if check.get("ok"):
            faith_pass += 1
        unsupported += len(check.get("issues") or [])
        checked_total += int(check.get("checked") or 0)
        if sources:
            cited = {n for n in _extract_refs(row["answer"]) if 1 <= n <= len(sources)}
            precisions.append(len(cited) / len(sources))
    return {
        "answered": len(answered),
        "faith_pass": faith_pass,
        "faith_pass_rate": (faith_pass / len(answered)) if answered else None,
        "unsupported": unsupported,
        "checked_total": checked_total,
        "unsupported_rate": (unsupported / checked_total) if checked_total else None,
        "with_sources": len(precisions),
        "context_precision": (mean(precisions) if precisions else None),
    }


def judge(question: str, key_points: list[str], answer: str, s: Any) -> dict[str, int] | None:
    """让考官打分；调用失败或解析失败都返回 None。"""
    try:
        raw = complete_chat(
            build_judge_messages(question, key_points, answer),
            settings=s,
            temperature=0.0,
            # 【为什么要显式给大一点的 max_tokens】本项目用的推理型模型会先把额度花在
            #   「思维链」上，回答越长、思考越久；默认的 1024 会不够，导致
            #   finish_reason=length 且正文为空（实测长回答的题就是这么丢分的）。
            #   给足 3072 后仍会自动提前停下，多给没有额外成本。
            max_tokens=3072,
        )
    except (LLMNotConfigured, LLMError) as exc:
        print(f"  [考官] 调用失败：{exc}", file=sys.stderr)
        return None
    return parse_scores(raw)


def _print_markdown(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("### LLM 考官评分（1~5 分）\n")
    lines.append("| 题号 | 类型 | 忠实度 | 相关度 | 完整性 | 平均 | 实际调用工具 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        sc = r["scores"]
        if sc is None:
            cells = ["—", "—", "—", "—"]
        else:
            vals = [sc[d] for d in DIMENSIONS]
            cells = [str(v) for v in vals] + [f"{mean(vals):.2f}"]
        tools = ", ".join(r["tools"]) or "—"
        lines.append(f"| {r['id']} | {r['type']} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} | {tools} |")

    # 汇总：三个维度的平均分 + 最低分题目
    scored = [r for r in rows if r["scores"] is not None]
    lines.append("")
    if not scored:
        lines.append("> 没有任何题目拿到有效评分（考官未配置或全部解析失败）。")
        return "\n".join(lines)
    lines.append("| 维度 | 平均分 |")
    lines.append("| --- | --- |")
    for dim in DIMENSIONS:
        lines.append(f"| {DIM_LABELS[dim]} | {mean(r['scores'][dim] for r in scored):.2f} |")

    overall = [(r["id"], mean(r["scores"][d] for d in DIMENSIONS)) for r in scored]
    low_id, low_avg = min(overall, key=lambda x: x[1])
    lines.append("")
    lines.append(f"> 有效评分题数：{len(scored)}/{len(rows)}；最低分题目：**{low_id}**（平均 {low_avg:.2f} 分）。")
    return "\n".join(lines)


def _fmt_rate(value: float | None) -> str:
    """百分比渲染：None（分母为 0、无可算的题）显示为「—」，不冒充 0 分。"""
    if value is None:
        return "—"
    return f"{value:.3f}"


def _print_proxy_section(px: dict[str, Any]) -> str:
    """渲染「生成层代理指标」小节（faithfulness / context_precision）。"""
    lines: list[str] = []
    lines.append("### 生成层代理指标（faithfulness / context_precision）\n")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(
        f"| faithfulness（引用校验通过率） | {_fmt_rate(px['faith_pass_rate'])}"
        f"（{px['faith_pass']}/{px['answered']} 题有回答） |"
    )
    lines.append(
        f"| faithfulness（无依据条数占比） | {_fmt_rate(px['unsupported_rate'])}"
        f"（{px['unsupported']}/{px['checked_total']} 条） |"
    )
    lines.append(
        f"| context_precision（引用来源占比均值） | {_fmt_rate(px['context_precision'])}"
        f"（{px['with_sources']} 题有来源） |"
    )
    lines.append("")
    lines.append(
        "> **代理指标，非 RAGAS 原版**：本项目相关性标注只到 doc 级、没有多级相关性，"
        "硬套 RAGAS 的分级口径会失真；此处复用现成的 citation 引用校验产出可对比的量化数字。"
    )
    return "\n".join(lines)


async def _run_all(items: list[dict[str, Any]], s: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        print(f"运行 {item['id']}：{item['question']}")
        try:
            answer, tools, error, sources = await run_question(item["question"], s)
        except Exception as exc:  # noqa: BLE001
            print(f"  问答链路异常：{exc!r}", file=sys.stderr)
            answer, tools, error, sources = "", [], f"{exc!r}", []
        if error:
            print(f"  链路 error 事件：{error}", file=sys.stderr)
        scores = judge(item["question"], item.get("key_points") or [], answer, s)
        rows.append({
            "id": item["id"],
            "type": item["type"],
            "answer": answer,
            "tools": tools,
            "scores": scores,
            "sources": sources,
        })
        print(f"  回答长度={len(answer)} 工具={tools} 评分={scores} 来源={len(sources)}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 考官评分")
    parser.add_argument("--set", default=str(Path(__file__).with_name("eval_set.json")))
    parser.add_argument("--out", default=None, help="把 Markdown 结果写到这个文件")
    args = parser.parse_args()

    s = get_settings()
    # 【未配置大模型的可读提示】没有模型就既跑不了 Agent、也请不了考官，
    #   此时不该让脚本崩溃，而是说清楚原因、正常退出（返回码 0）。
    if not s.llm_configured:
        print("未配置大模型（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 至少缺一项），")
        print("无法进行 LLM 考官打分，已跳过。请先在 .env 配好模型后重试。")
        return 0

    items = load_eval_set(Path(args.set))
    rows = asyncio.run(_run_all(items, s))
    md = _print_markdown(rows) + "\n\n" + _print_proxy_section(proxy_metrics(rows))
    print(md)
    if args.out:
        Path(args.out).write_text(md + "\n", encoding="utf-8")
        print(f"\n（已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())