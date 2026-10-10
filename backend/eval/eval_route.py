# ============================================================================
# 文件：eval_route.py —— 检查 Agent「有没有调对我期望的那个工具」
#
# 【这个文件是干什么的】
#   统计类题目应该走 PostgreSQL 工具（query_bidding_postgres），关系类题目应该走
#   知识图谱工具（query_bidding_graph），原文类题目应该走知识库检索工具
#   （search_bidding_knowledge）。本文件对每道题跑一遍 Agent，把它「实际调用了哪些工具」
#   记下来，和 eval_set.json 里的 expect_tool 比对，算出工具路由准确率。
#
# 【为什么要单独评这个】Agent 的价值就在于「自己挑对工具」。如果它拿统计问题去查向量库，
#   答案必然不对——而且这种错误在最终答案里不一定看得出来（模型可能硬编一个数）。
#   单独把「路由」拎出来测，能把这类问题定位得更准。
#
# 【怎么运行】
#   cd backend
#   ..\.venv\Scripts\python.exe eval\eval_route.py
# ============================================================================
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent import agent_events  # noqa: E402
from app.config import get_settings  # noqa: E402


def load_eval_set(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["items"]


async def collect_tools(question: str, s: Any) -> list[str]:
    """跑一遍 Agent，收集本轮实际调用过的工具名（去重、保持出现顺序）。"""
    tools: list[str] = []
    async for event in agent_events(question, settings=s, mode="react"):
        if event.get("type") == "tool_call":
            name = event.get("name")
            if name and name not in tools:
                tools.append(name)
    return tools


def _print_markdown(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("### 工具路由准确率\n")
    lines.append("| 题号 | 类型 | 期望工具 | 实际调用工具 | 命中 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in rows:
        actual = ", ".join(r["actual"]) or "（未调用任何工具）"
        hit = "✅" if r["hit"] else "❌"
        lines.append(f"| {r['id']} | {r['type']} | {r['expect']} | {actual} | {hit} |")
    total = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    lines.append("")
    lines.append(f"> 工具路由准确率：**{hits}/{total} = {hits / total:.0%}**（判定标准：期望工具出现在实际调用列表中）")
    return "\n".join(lines)


async def _run_all(items: list[dict[str, Any]], s: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        expect = item.get("expect_tool")
        print(f"运行 {item['id']}（期望 {expect}）：{item['question']}")
        try:
            actual = await collect_tools(item["question"], s)
        except Exception as exc:  # noqa: BLE001
            print(f"  Agent 运行异常：{exc!r}", file=sys.stderr)
            actual = []
        # 【命中判定】只看「期望工具是否出现在实际调用里」，不要求它只调这一个：
        #   原文类题目 Agent 先调检索再补调别的工具，也是合理的。
        hit = bool(expect) and expect in actual
        rows.append({"id": item["id"], "type": item["type"], "expect": expect, "actual": actual, "hit": hit})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="工具路由准确率")
    parser.add_argument("--set", default=str(Path(__file__).with_name("eval_set.json")))
    parser.add_argument("--out", default=None, help="把 Markdown 结果写到这个文件")
    args = parser.parse_args()

    s = get_settings()
    # 【未配置大模型的可读提示】Agent 的「自主选工具」完全靠大模型；没模型就没法评，正常退出。
    if not s.agent_configured:
        print("未配置大模型，Agent 无法自主选择工具，工具路由评测已跳过。")
        print("请先在 .env 配好 LLM（或 AGENT_MODEL）后重试。")
        return 0

    items = load_eval_set(Path(args.set))
    rows = asyncio.run(_run_all(items, s))
    md = _print_markdown(rows)
    print(md)
    if args.out:
        Path(args.out).write_text(md + "\n", encoding="utf-8")
        print(f"\n（已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())