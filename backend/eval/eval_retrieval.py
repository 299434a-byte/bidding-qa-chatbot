# ============================================================================
# 文件：eval_retrieval.py —— 检索质量的「客观指标」评测（不调用大模型作答）
#
# 【这个文件是干什么的】
#   拿 eval_set.json 里的题目，对每一题跑三种检索配置，算三个纯客观的指标：
#     · 纯稠密（只用向量）
#     · 稠密 + BM25 + RRF（项目的默认混合检索）
#     · 在此基础上再加一层大模型重排
#   指标是 recall@5 / recall@10 / MRR，全部只看「期望的原文有没有被捞回来、排第几」，
#   不涉及任何自然语言判断，所以同一次运行结果可复现、也不花钱调模型。
#
# 【为什么要把「原文类」和「统计/关系类」分开看（关键）】
#   recall/MRR 衡量的是「能不能从向量库里找回指定原文」。统计类和关系类题目的答案本来
#   就不在向量库里（在 PostgreSQL / 图库里），它们没有 expect_doc_ids，也就不参与
#   检索指标 —— 硬把这 6 题算成 0 分会把平均值拉低，反而看不出检索本身的好坏。
#
# 【怎么运行】
#   cd backend
#   ..\.venv\Scripts\python.exe eval\eval_retrieval.py
#   （也可以加 --out eval\_retrieval.md 把 Markdown 表格写成文件）
# ============================================================================
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 【为什么要手工加 sys.path】脚本在 backend/eval/ 下，而项目导入写法是 `import app.xxx`
#   （即 app 包的父目录 backend/ 必须在搜索路径里）。这里按 tests/conftest.py 同款做法
#   把 backend/ 插进去，脚本才能脱离 pytest 单独运行。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.bm25 import BM25Index  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.rerank import rerank_hits  # noqa: E402
from app.retriever import retrieve  # noqa: E402

# 【候选深度】三配置统一先从库里取 20 条候选。
# 【为什么不用默认的 5 条】因为要算 recall@10，池子至少得有 10 条；
#   更重要的是重排只有在「候选数 > 要保留的条数」时才会真正执行（见 rerank_hits 的短路逻辑），
#   池子太小会让重排这一档等于白跑。
CANDIDATE_DEPTH = 20
# 【重排后保留条数】留 10 条，保证 recall@10 能算出来。
RERANK_TOP_K = 10

CONFIGS = ("纯稠密", "稠密+BM25+RRF", "稠密+BM25+RRF+LLM重排")


def load_eval_set(path: Path) -> list[dict[str, Any]]:
    """读题目集（只取 items 列表）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["items"]


def _retrieve_dense_only(question: str, s: Any) -> list[dict[str, Any]]:
    """第一种配置：纯稠密。

    【怎么实现的】retrieve() 内部固定会融合两路，无法直接「只取稠密」。
    但它的 BM25 那一路只有在 ``bm25.ready`` 为真时才跑；所以我们传入一个「未拟合的空索引」
    （BM25Index() 的 n_docs=0、词表为空，ready=False），关键词那路自然被跳过，
    融合结果等于「只有稠密」的排序。这样仍然走的是 retrieve() 本身，没有另写检索逻辑。
    """
    result = retrieve(question, settings=s, bm25=BM25Index(), final_k=CANDIDATE_DEPTH)
    return result.hits


def _retrieve_hybrid(question: str, s: Any) -> list[dict[str, Any]]:
    """第二种配置：项目默认的稠密 + BM25 + RRF 混合检索。"""
    result = retrieve(question, settings=s, final_k=CANDIDATE_DEPTH)
    return result.hits


def _retrieve_hybrid_rerank(question: str, s: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """第三种配置：混合检索候选再交给大模型重排，返回 (结果, 重排元信息)。"""
    result = retrieve(question, settings=s, final_k=CANDIDATE_DEPTH)
    # 【重排候选池怎么定（关键修正）】历史上把 20 条候选整包喂给重排，推理模型的
    #   思维链把 token 预算吃光（finish_reason=length），6/10 题静默降级、C 档等于 B。
    #   现在池子 = max(生产 RERANK_CANDIDATES, 保留条数) + 1：
    #   · 「+1」是因为 rerank_hits 有短路——候选数 ≤ 保留条数就直接跳过重排
    #     （本脚本保留 RERANK_TOP_K=10 条），恰好 10 条时重排一次都不会跑，C 档又会退化成 B；
    #   · 基准取生产配置 RERANK_CANDIDATES，让评测喂给重排的量级与线上一致
    #     （当前 .env 是 10，池子即 11 条，比历史的 20 条几乎减半）。
    pool = max(s.rerank_candidates, RERANK_TOP_K) + 1
    hits, meta = rerank_hits(question, result.hits[:pool], s, top_k=RERANK_TOP_K)
    return hits, meta


def recall_at_k(hits: list[dict[str, Any]], expect: set[str], k: int) -> float:
    """recall@k：期望原文里，有多少比例出现在前 k 条里。"""
    if not expect:
        return float("nan")
    got = {h.get("doc_id") for h in hits[:k]}
    return len(expect & got) / len(expect)


def reciprocal_rank(hits: list[dict[str, Any]], expect: set[str]) -> float:
    """MRR 的单项：第一条命中出现在第几名，就取它的倒数（1/rank）；一条都没命中记 0。"""
    if not expect:
        return float("nan")
    for rank, h in enumerate(hits, start=1):
        if h.get("doc_id") in expect:
            return 1.0 / rank
    return 0.0


def _fmt(value: float) -> str:
    """把指标格式化成好看的三位小数；NaN（不适用）显示为 —。"""
    return "—" if value != value else f"{value:.3f}"


def _print_markdown(rows: list[dict[str, Any]], rerank_note: str) -> str:
    """把每题的指标 + 三配置平均，渲染成 Markdown 表格文本。"""
    lines: list[str] = []
    lines.append("### 检索客观指标（recall@5 / recall@10 / MRR）\n")
    lines.append("> 配置 A=纯稠密，B=稠密+BM25+RRF（默认），C=B+大模型重排。")
    lines.append("> 统计类 / 关系类题目答案不在向量库，无 expect_doc_ids，不参与检索指标（显示为 —）。\n")
    if rerank_note:
        lines.append(f"> 备注：{rerank_note}\n")

    header_cfg = " | ".join(CONFIGS)
    lines.append(f"| 题号 | 类型 | expect_doc_ids | {header_cfg} |")
    # 每题一行展示：A/B/C 各自「命中数/期望数（排名）」简况。
    # 【为什么表格里要放三列配置】一眼能看出「加 BM25、加重排」分别在哪些题上有提升。
    lines.append("| --- | --- | --- | --- | --- | --- |")

    for r in rows:
        if not r["expect"]:
            lines.append(
                f"| {r['id']} | {r['type']} | （无） | A: — | B: — | C: — |"
            )
            continue
        detail = []
        for cfg in CONFIGS:
            m = r["per_cfg"][cfg]
            detail.append(
                f"r@5={m['recall5']:.2f} r@10={m['recall10']:.2f} rr={m['rr']:.2f}"
            )
        exp = ", ".join(sorted(r["expect"]))
        lines.append(
            f"| {r['id']} | {r['type']} | {exp} | A: {detail[0]} | B: {detail[1]} | C: {detail[2]} |"
        )

    # 汇总平均（只在有 expect_doc_ids 的题目上平均）
    lines.append("")
    lines.append("| 配置 | 平均 recall@5 | 平均 recall@10 | 平均 MRR |")
    lines.append("| --- | --- | --- | --- |")
    for cfg in CONFIGS:
        r5 = [r["per_cfg"][cfg]["recall5"] for r in rows if r["expect"]]
        r10 = [r["per_cfg"][cfg]["recall10"] for r in rows if r["expect"]]
        rr = [r["per_cfg"][cfg]["rr"] for r in rows if r["expect"]]
        n = len(r5) or 1
        lines.append(
            f"| {cfg} | {sum(r5) / n:.3f} | {sum(r10) / n:.3f} | {sum(rr) / n:.3f} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="检索客观指标评测")
    parser.add_argument("--set", default=str(Path(__file__).with_name("eval_set.json")))
    parser.add_argument("--out", default=None, help="把 Markdown 结果写到这个文件")
    args = parser.parse_args()

    items = load_eval_set(Path(args.set))
    s = get_settings()

    rows: list[dict[str, Any]] = []
    rerank_unconfigured = not s.rerank_configured
    for item in items:
        q = item["question"]
        expect = set(item.get("expect_doc_ids") or [])
        per_cfg: dict[str, dict[str, float]] = {}

        # 配置 A / B：不调模型，失败也不该让整轮评测崩掉，单题兜底成空结果。
        try:
            a_hits = _retrieve_dense_only(q, s)
        except Exception as exc:  # noqa: BLE001
            print(f"[{item['id']}] 纯稠密检索失败：{exc!r}", file=sys.stderr)
            a_hits = []
        try:
            b_hits = _retrieve_hybrid(q, s)
        except Exception as exc:  # noqa: BLE001
            print(f"[{item['id']}] 混合检索失败：{exc!r}", file=sys.stderr)
            b_hits = []
        # 配置 C：重排不配模型时会静默降级成 B 的顺序，这里如实记录 meta 以便在报告里点明。
        c_meta: dict[str, Any] = {}
        try:
            c_hits, c_meta = _retrieve_hybrid_rerank(q, s)
        except Exception as exc:  # noqa: BLE001
            print(f"[{item['id']}] 重排检索失败：{exc!r}", file=sys.stderr)
            c_hits = []

        for cfg, hits in zip(CONFIGS, (a_hits, b_hits, c_hits)):
            per_cfg[cfg] = {
                "recall5": recall_at_k(hits, expect, 5),
                "recall10": recall_at_k(hits, expect, 10),
                "rr": reciprocal_rank(hits, expect),
            }
        rows.append({
            "id": item["id"],
            "type": item["type"],
            "expect": expect,
            "per_cfg": per_cfg,
            "rerank_applied": bool(c_meta.get("rerank_applied")),
            "rerank_error": c_meta.get("rerank_error"),
        })

    # 【为什么要统计「重排到底生效没有」】配置 C 在重排失败时会静默退回配置 B 的顺序，
    #   两列数字可能一模一样。不点明的话，读者会误以为「重排没用」；点明后才知道是
    #   「重排这一层当前根本没跑起来」，这是两种完全不同的结论。
    applied = sum(1 for r in rows if r.get("rerank_applied"))
    errs = sorted({r["rerank_error"] for r in rows if r.get("rerank_error")})
    note = ""
    if rerank_unconfigured:
        note = "当前未配置大模型，配置 C 的重排会静默降级为配置 B 的顺序（两列会相同）。"
    else:
        note = f"重排实际生效：{applied}/{len(rows)} 题。"
        if errs:
            note += " 失败原因：" + "；".join(str(e) for e in errs[:2])
    md = _print_markdown(rows, note)

    print(md)
    if args.out:
        Path(args.out).write_text(md + "\n", encoding="utf-8")
        print(f"\n（已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())