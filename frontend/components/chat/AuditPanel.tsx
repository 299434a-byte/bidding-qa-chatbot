"use client";

import type { HallucinationFlag, PhaseTimings, ToolCallInfo } from "@/lib/api";


/** 工具调用（tool_result 补上耗时与成败；旧后端没有这两个字段就显示「—」） */
export type AuditToolCall = ToolCallInfo & {
  count?: number;
  elapsedMs?: number | null;
  ok?: boolean;
};

type Props = {
  /** 逐句可信度（citation_check 带来）；没有就不显示徽标 */
  confidence?: number;
  supportedSentences?: number;
  totalSentences?: number;
  /** 无依据句（no_citation = 无引用 / number_no_source = 数值未在来源中找到） */
  hallucinationFlags?: HallucinationFlag[];
  /** 阶段耗时（done.phase_timings）；缓存命中/降级回答没有 */
  phaseTimings?: PhaseTimings;
  toolCalls?: AuditToolCall[];
  /** 本次是缓存命中（后端 cache_hit 事件） */
  cacheHit?: boolean;
  /** 点击无依据句时回调：滚动定位到正文对应位置 */
  onLocateSentence?: (sentence: string) => void;
};

const REASON_TEXT: Record<string, string> = {
  no_citation: "无引用",
  number_no_source: "数值未在来源中找到",
};

const TOOL_TEXT: Record<string, string> = {
  search_bidding_knowledge: "知识库检索",
  query_bidding_postgres: "统计查询",
  query_bidding_graph: "关系查询",
  search_web_bidding: "联网搜索",
  load_skill: "技能加载",
};

function reasonText(reason: string): string {
  return REASON_TEXT[reason] ?? reason;
}

function toolText(name: string): string {
  return TOOL_TEXT[name] ?? name;
}

/** 句子摘要：过长只留前 48 字 */
function excerpt(sentence: string): string {
  const text = (sentence || "").trim();
  return text.length > 48 ? `${text.slice(0, 48)}…` : text || "（空句）";
}

export default function AuditPanel({
  confidence,
  supportedSentences,
  totalSentences,
  hallucinationFlags,
  phaseTimings,
  toolCalls,
  cacheHit,
  onLocateSentence,
}: Props) {
  const timings: { label: string; ms: number }[] = [];
  if (typeof phaseTimings?.retrieval_ms === "number") {
    timings.push({ label: "检索", ms: phaseTimings.retrieval_ms });
  }
  if (typeof phaseTimings?.rerank_ms === "number") {
    timings.push({ label: "重排", ms: phaseTimings.rerank_ms });
  }
  if (typeof phaseTimings?.generation_ms === "number") {
    timings.push({ label: "生成", ms: phaseTimings.generation_ms });
  }

  const tools = toolCalls ?? [];
  const flags = hallucinationFlags ?? [];
  const hasConfidence = typeof confidence === "number";
  const hasData = hasConfidence || timings.length > 0 || tools.length > 0 || flags.length > 0;

  return (
    <div className="sources" data-testid="audit-panel">
      <div className="sources-title">
        回答审计
        <span className="badge badge-plain">可信度 / 耗时 / 工具 / 无依据句</span>
      </div>

      {/* 缓存命中 / 降级回答：没有审计数据，明说而不是空着 */}
      {cacheHit ? (
        <div className="trace-note">本次为缓存命中，无审计数据。</div>
      ) : !hasData ? (
        <div className="trace-note">本次为降级回答，无审计数据。</div>
      ) : (
        <>
          {/* 顶部一行：可信度徽标（≥0.7 绿 / ≥0.5 黄 / <0.5 红）+ 百分比 + 有支撑句数 */}
          <div className="adopted-tag">
            {hasConfidence && (
              <span
                className={`badge ${
                  (confidence as number) >= 0.7
                    ? "badge-mint"
                    : (confidence as number) >= 0.5
                      ? "badge-warn"
                      : "badge-danger"
                }`}
              >
                可信度 {Math.round((confidence as number) * 100)}%
              </span>
            )}
            {typeof totalSentences === "number" && totalSentences > 0 && (
              <span className="badge badge-plain">
                {supportedSentences ?? 0}/{totalSentences} 句有支撑
              </span>
            )}
          </div>

          {/* 阶段耗时：没有的阶段不显示 */}
          {timings.length > 0 && (
            <div className="adopted-tag" data-testid="audit-timings">
              {timings.map((item) => (
                <span className="badge badge-plain" key={item.label}>
                  {item.label} {item.ms} ms
                </span>
              ))}
            </div>
          )}

          {/* 工具调用：名称 + 耗时 + 成功/失败 */}
          {tools.length > 0 && (
            <details className="trace-item">
              <summary className="trace-summary">
                工具调用 · {tools.length} 次
                <span className="badge badge-plain">点击展开耗时与成败</span>
              </summary>
              <ul className="reflect-issues">
                {tools.map((tool, index) => (
                  <li key={`${tool.name}-${index}`}>
                    {toolText(tool.name)}
                    <span className="mono-num">
                      {" · "}
                      {typeof tool.elapsedMs === "number" ? `${tool.elapsedMs} ms` : "耗时 —"}
                    </span>
                    {tool.ok === false ? (
                      <span className="badge badge-danger">失败</span>
                    ) : tool.ok === true ? (
                      <span className="badge badge-mint">成功</span>
                    ) : (
                      <span className="badge badge-plain">成败 —</span>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {/* 无依据句：摘要 + 原因，点击滚动定位到正文 */}
          {flags.length > 0 && (
            <details className="trace-item" open>
              <summary className="trace-summary">
                无依据句 · {flags.length} 句
                <span className="badge badge-warn">点击句子可定位正文</span>
              </summary>
              <ul className="reflect-issues">
                {flags.map((flag, index) => (
                  <li key={`${flag.sentence}-${index}`}>
                    <button
                      className="act-btn"
                      type="button"
                      onClick={() => onLocateSentence?.(flag.sentence)}
                      title={flag.sentence}
                    >
                      {excerpt(flag.sentence)}
                    </button>
                    <span className="badge badge-warn">{reasonText(flag.reason)}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}
