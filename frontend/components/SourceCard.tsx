"use client";

import { useState } from "react";
import type { Source } from "@/lib/api";

/** 从原始字段里挑一个最有辨识度的值当标题。 */
function pickTitle(source: Source): string {
  const entries = Object.entries(source.fields ?? {});
  const preferred = entries.find(
    ([key, value]) => /title|name|subject|标题|名称|项目名/i.test(key) && typeof value === "string" && value.trim(),
  );
  if (preferred) return String(preferred[1]);
  const firstText = entries.find(([, value]) => typeof value === "string" && value.trim());
  if (firstText) return String(firstText[1]);
  return `记录 ${source.primary_key}`;
}

export default function SourceCard({ source, index }: { source: Source; index: number }) {
  const [open, setOpen] = useState(index === 0);
  const fields = Object.entries(source.fields ?? {}).filter(([, v]) => v !== null && v !== "");

  return (
    <div className="source">
      <div className="source-head" onClick={() => setOpen((v) => !v)}>
        <span className="source-idx">{index + 1}</span>
        <span className="badge badge-blue">{source.label}</span>
        <span className="badge badge-plain">记录 {String(source.primary_key)}</span>
        {source.chunk_total > 1 && (
          <span className="badge badge-plain">
            片段 {source.chunk_index + 1}/{source.chunk_total}
          </span>
        )}
        {typeof source.rerank_rank === "number" && (
          <span className="badge badge-mint">重排 #{source.rerank_rank}</span>
        )}
        {source.matched_by.includes("dense") && <span className="badge badge-mint">向量命中</span>}
        {source.matched_by.includes("bm25") && <span className="badge badge-warn">BM25 命中</span>}
        <span className="source-title">{pickTitle(source)}</span>
        <span className="source-spacer" />
        <span className="source-metrics">
          RRF {source.rrf_score.toFixed(5)}
          {source.scores.dense !== undefined && ` · 向量 ${source.scores.dense.toFixed(4)}`}
          {source.scores.bm25 !== undefined && ` · BM25 ${source.scores.bm25.toFixed(3)}`}
        </span>
      </div>

      {open && (
        <div className="source-body">
          <div className="source-snippet">{source.text}</div>
          {fields.length > 0 && (
            <div className="fields">
              {fields.map(([key, value]) => (
                <div className="field-row" key={key}>
                  <span className="field-key">{key}</span>
                  <span className="field-val">{String(value)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}