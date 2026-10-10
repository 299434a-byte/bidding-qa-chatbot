"""扫描 DENSE_MIN_SCORE，观察它对 recall@5 的影响（不调用大模型，快）。

复用 eval_retrieval.py 的取数/指标函数，只把配置里的 dense_min_score 换掉重复跑一遍。
结果用于 eval/report.md 的「阈值敏感性」表。
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = _EVAL_DIR.parent
for _p in (str(BACKEND_DIR), str(_EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.config import get_settings  # noqa: E402
import eval_retrieval as er  # noqa: E402

# 要扫描的稠密相似度门槛（0.35 即项目默认值）。
THRESHOLDS = (0.2, 0.3, 0.35, 0.4)


def main() -> int:
    parser = argparse.ArgumentParser(description="DENSE_MIN_SCORE 敏感性扫描")
    parser.add_argument("--set", default=str(_EVAL_DIR / "eval_set.json"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    items = er.load_eval_set(Path(args.set))
    base = get_settings()

    rows: list[tuple[float, float, float]] = []
    for thr in THRESHOLDS:
        # dataclasses.replace 在冻结配置上「复制一份、只改一个字段」，不影响原配置。
        s = dataclasses.replace(base, dense_min_score=thr)
        dense_scores: list[float] = []
        hybrid_scores: list[float] = []
        for item in items:
            expect = set(item.get("expect_doc_ids") or [])
            # 统计/关系类题目没有 expect_doc_ids，不参与检索指标。
            if not expect:
                continue
            dense_scores.append(er.recall_at_k(er._retrieve_dense_only(item["question"], s), expect, 5))
            hybrid_scores.append(er.recall_at_k(er._retrieve_hybrid(item["question"], s), expect, 5))
        n = len(dense_scores) or 1
        rows.append((thr, sum(dense_scores) / n, sum(hybrid_scores) / n))

    lines = [
        "### DENSE_MIN_SCORE 对 recall@5 的影响\n",
        "| DENSE_MIN_SCORE | 纯稠密 recall@5 | 稠密+BM25+RRF recall@5 |",
        "| --- | --- | --- |",
    ]
    for thr, dense, hybrid in rows:
        lines.append(f"| {thr} | {dense:.3f} | {hybrid:.3f} |")
    md = "\n".join(lines)
    print(md)
    if args.out:
        Path(args.out).write_text(md + "\n", encoding="utf-8")
        print(f"\n（已写入 {args.out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())