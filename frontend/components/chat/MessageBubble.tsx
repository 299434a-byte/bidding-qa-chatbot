"use client";

import SourceCard from "@/components/SourceCard";
import ToolTrace from "@/components/chat/ToolTrace";
import { mdToHtml } from "@/lib/markdown";
import type {
  GraphQueryInfo,
  PlanStep,
  PlanStepRun,
  ReflectionInfo,
  RetrievalMeta,
  Source,
  SqlQueryInfo,
  ToolCallInfo,
  WebSearchInfo,
} from "@/lib/api";

/**
 * 单条消息气泡：用户消息靠右、助手消息靠左（左右由外层 CSS 的 .msg-user / .msg-assistant 决定）。
 *
 * 【为什么单独抽成组件】
 *   它是消息列表里「一条消息」的完整长相，占了原 page.tsx 五百多行。
 *   抽出来后，page.tsx 只负责「有哪些消息、什么时候高亮」，具体怎么渲染由这里负责。
 *
 * 【为什么不在这里存状态】
 *   它是「受控组件」：所有数据来自 props.msg，改动（填回输入框、切换采纳版本）
 *   通过 onFillInput / onAdopt 回调交回给 page.tsx，避免流式回答时的状态被拆散。
 */

/** PostgreSQL 查询工具名（后端 pg.PG_TOOL_NAME，前端据此区分三个工具） */
const PG_TOOL = "query_bidding_postgres";
/** Neo4j 图谱查询工具名（后端 graph.GRAPH_TOOL_NAME） */
const GRAPH_TOOL = "query_bidding_graph";
/** 联网搜索工具名（后端 web_search.TOOL_NAME） */
const WEB_TOOL = "search_web_bidding";

/** 子任务状态展示文案 */
const STEP_STATUS: Record<PlanStepRun["status"], { label: string; cls: string }> = {
  running: { label: "执行中", cls: "badge-blue" },
  done: { label: "已完成", cls: "badge-mint" },
  failed: { label: "失败", cls: "badge-danger" },
};

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** 用户消息附带的图片（data URL，用于缩略图展示） */
  image?: { url: string; name: string };
  /** 图片解析中（后端 /analyze-image 尚未返回） */
  analyzing?: boolean;
  /** 图片识别结果（后端 /analyze-image 返回） */
  imageResult?: { text: string; model: string | null };
  sources?: Source[];
  meta?: RetrievalMeta;
  /** Agent 的工具调用记录（未调用则为空） */
  toolCalls?: (ToolCallInfo & { count?: number })[];
  /** Agent 调用 PostgreSQL 工具后返回的 SQL 与结果 */
  sqlQueries?: SqlQueryInfo[];
  /** Agent 调用 Neo4j 图谱工具后返回的 Cypher 与结果 */
  graphQueries?: GraphQueryInfo[];
  /** Agent 调用联网搜索工具后返回的结果（含相关性评分） */
  webSearches?: WebSearchInfo[];
  /** Plan-and-Solve / 深度模式：规划出的子任务 */
  plan?: PlanStep[];
  /** 规划失败已回退为 ReAct 单轮 */
  planFallback?: boolean;
  /** 各子任务的执行状态与观察结果 */
  steps?: PlanStepRun[];
  /** 深度模式的自评结果 */
  reflections?: ReflectionInfo[];
  /** 改进稿正文（channel=revision 的 delta，单独渲染） */
  revision?: string;
  /** 用户采纳的版本：v1=原回答正文，v2=改进稿（默认 v1，决定下一轮回传哪份） */
  adoptedVersion?: "v1" | "v2";
  mode?: string;
  error?: string;
  streaming?: boolean;
};

export type { Message };

type Props = {
  /** 要渲染的这一条消息 */
  msg: Message;
  /** 是否高亮（从历史检索结果跳转过来时的短暂高亮） */
  highlighted?: boolean;
  /** 正在回答时为 true（用于禁用「填入提问框」等按钮） */
  busy?: boolean;
  /** 把图片识别结果填回提问输入框 */
  onFillInput: (text: string) => void;
  /** 切换该条回复采纳的版本（v1 原回答 / v2 修订稿） */
  onAdopt: (id: string, version: "v1" | "v2") => void;
};

export default function MessageBubble({ msg, highlighted, busy, onFillInput, onAdopt }: Props) {
  return (
    <div
      className={`msg msg-${msg.role}${highlighted ? " msg-highlight" : ""}`}
      id={`msg-${msg.id}`}
    >
      <div className="msg-avatar">{msg.role === "user" ? "我" : "AI"}</div>
      <div className="msg-body">
        {msg.role === "user" ? (
          <>
            {msg.image && <img className="msg-image" src={msg.image.url} alt={msg.image.name} />}
            {msg.content}
          </>
        ) : (
          <>
            {/* 图片解析中：先给出明确状态，避免看起来卡住 */}
            {msg.analyzing && (
              <div style={{ marginBottom: 8 }}>
                <span className="badge badge-blue">
                  <span className="spinner" />
                  图片解析中…
                </span>
              </div>
            )}

            {/* 图片识别结果：展示在回答区上方，可一键填回提问框继续追问 */}
            {msg.imageResult && (
              <div className="vision-card">
                <div className="vision-card-head">
                  <span className="badge badge-mint">图片识别结果</span>
                  {msg.imageResult.model && (
                    <span className="badge badge-plain">{msg.imageResult.model}</span>
                  )}
                  <span className="source-spacer" />
                  <button
                    className="chip"
                    type="button"
                    onClick={() => onFillInput(msg.imageResult!.text)}
                    disabled={busy}
                  >
                    将识别结果填入提问框
                  </button>
                </div>
                <div className="vision-card-body">{msg.imageResult.text}</div>
              </div>
            )}

            {(msg.meta || (msg.toolCalls?.length ?? 0) > 0) && (
              <div style={{ marginBottom: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
                {msg.toolCalls && msg.toolCalls.length > 0 ? (
                  msg.toolCalls.map((call, index) => {
                    const isPg = call.name === PG_TOOL;
                    const isGraph = call.name === GRAPH_TOOL;
                    const isWeb = call.name === WEB_TOOL;
                    const label = isWeb
                      ? "已调用联网搜索"
                      : isGraph
                        ? "已调用知识图谱查询"
                        : isPg
                          ? "已调用 PostgreSQL 查询"
                          : "已调用知识库检索";
                    return (
                      <span
                        className="badge badge-blue"
                        key={`${call.name}-${index}`}
                        title={
                          isWeb
                            ? `联网搜索关键词：${call.query ?? ""}`
                            : isGraph
                              ? `生成的 Cypher：${call.query ?? ""}`
                              : isPg
                                ? `PostgreSQL 查询：${call.query ?? ""}`
                                : call.query
                                  ? `Agent 检索词：${call.query}`
                                  : undefined
                        }
                      >
                        {label}
                        {typeof call.count === "number" ? ` · 命中 ${call.count} 条` : ""}
                      </span>
                    );
                  })
                ) : (
                  <span className="badge badge-plain">未调用工具</span>
                )}
                {/* 检索统计只有走了知识库检索才有 */}
                {msg.meta && (
                  <>
                    <span className="badge badge-plain">向量召回 {msg.meta.dense_count}</span>
                    <span className="badge badge-plain">
                      BM25 召回 {msg.meta.bm25_ready ? msg.meta.sparse_count : "未启用"}
                    </span>
                    <span className="badge badge-plain">最终参考 {msg.meta.final_count}</span>
                    {msg.meta.rerank_applied ? (
                      <span className="badge badge-mint">
                        重排已生效（{msg.meta.rerank_candidates} → {msg.meta.final_count}）
                      </span>
                    ) : (
                      <span className="badge badge-plain">未重排</span>
                    )}
                    {msg.meta.rerank_error && (
                      <span className="badge badge-danger" title={msg.meta.rerank_error}>
                        重排失败
                      </span>
                    )}
                  </>
                )}
                {msg.mode && <span className="badge badge-plain">mode: {msg.mode}</span>}
              </div>
            )}

            {/* Agent 判定无需检索时不会产生检索元数据，这里单独说明它的决定 */}
            {!msg.meta && (msg.toolCalls?.length ?? 0) === 0 && msg.mode === "agent_direct" && (
              <div style={{ marginBottom: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
                <span className="badge badge-plain">未调用工具 · Agent 直接回答</span>
              </div>
            )}

            {msg.error && <div className="notice notice-danger">{msg.error}</div>}

            {/* Plan-and-Solve / 深度模式：规划出的子任务 */}
            {msg.plan && msg.plan.length > 0 && (
              <div className="plan-card">
                <div className="plan-card-head">
                  <span className="badge badge-blue">规划</span>
                  <span className="badge badge-plain">{msg.plan.length} 个子任务</span>
                  {msg.planFallback && <span className="badge badge-warn">已回退为 ReAct 单轮</span>}
                </div>
                <ol className="plan-list">
                  {msg.plan.map((step) => (
                    <li className="plan-item" key={step.index}>
                      <span className="plan-item-title">
                        {step.index}. {step.title}
                      </span>
                      {step.detail && <span className="plan-item-detail">{step.detail}</span>}
                    </li>
                  ))}
                </ol>
              </div>
            )}

            {/* Plan-and-Solve / 深度模式：各子任务的执行状态与观察结果 */}
            {msg.steps && msg.steps.length > 0 && (
              <div className="plan-card">
                <div className="plan-card-head">
                  <span className="badge badge-blue">执行步骤</span>
                  <span className="badge badge-plain">{msg.steps.length} 步</span>
                </div>
                {msg.steps.map((step) => {
                  const status = STEP_STATUS[step.status];
                  return (
                    <details className="step-item" key={step.index} open={step.status === "running"}>
                      <summary>
                        <span className={`badge ${status.cls}`}>{status.label}</span>
                        <span className="step-title">
                          {step.index}. {step.title || "（无标题）"}
                        </span>
                      </summary>
                      {step.observation && <div className="step-observation">{step.observation}</div>}
                      {step.error && (
                        <div className="notice notice-danger" style={{ marginTop: 8 }}>
                          {step.error}
                        </div>
                      )}
                    </details>
                  );
                })}
              </div>
            )}

            {/* 深度模式：自评判定与改进建议 */}
            {msg.reflections && msg.reflections.length > 0 && (
              <div className="reflect-card">
                <div className="plan-card-head">
                  <span className="badge badge-warn">自我反思</span>
                  <span className="badge badge-plain">{msg.reflections.length} 轮</span>
                </div>
                {msg.reflections.map((reflection) => (
                  <div className="reflect-item" key={reflection.round}>
                    <span className={`badge ${reflection.verdict === "ok" ? "badge-mint" : "badge-warn"}`}>
                      {reflection.verdict === "ok" ? "自评通过" : "建议修订"}
                    </span>
                    <span className="reflect-round">第 {reflection.round} 轮</span>
                    {reflection.issues.length > 0 && (
                      <ul className="reflect-issues">
                        {reflection.issues.map((issue, i) => (
                          <li key={i}>{issue}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            )}

            {msg.content && (
              <div className="answer" dangerouslySetInnerHTML={{ __html: mdToHtml(msg.content) }} />
            )}
            {msg.streaming && !msg.content && <span className="cursor" />}
            {msg.streaming && msg.content && <span className="cursor" />}

            {/* 改进稿：单独折叠展示，可切换下一轮采纳哪份 */}
            {msg.revision && (
              <details className="revision-card" open>
                <summary className="revision-summary">
                  <span className="badge badge-warn">修订后的回答</span>
                  <span className="source-metrics">v1 / v2 可切换</span>
                </summary>
                <div className="revision-toggle">
                  <button
                    className={`seg-item ${(msg.adoptedVersion ?? "v1") === "v1" ? "seg-active" : ""}`}
                    type="button"
                    onClick={() => onAdopt(msg.id, "v1")}
                  >
                    原回答 v1
                  </button>
                  <button
                    className={`seg-item ${(msg.adoptedVersion ?? "v1") === "v2" ? "seg-active" : ""}`}
                    type="button"
                    onClick={() => onAdopt(msg.id, "v2")}
                  >
                    修订稿 v2
                  </button>
                </div>
                <div className="revision-hint">
                  下一轮对话将回传所选版本：当前采纳「
                  {(msg.adoptedVersion ?? "v1") === "v2" ? "修订稿 v2" : "原回答 v1"}」。
                </div>
                <div className="answer" dangerouslySetInnerHTML={{ __html: mdToHtml(msg.revision) }} />
              </details>
            )}

            {/* 工具调用痕迹：PostgreSQL / Neo4j / 联网搜索 三类可折叠结果 */}
            <ToolTrace
              sqlQueries={msg.sqlQueries}
              graphQueries={msg.graphQueries}
              webSearches={msg.webSearches}
            />

            {msg.sources && msg.sources.length > 0 && (
              <div className="sources">
                <div className="sources-title">
                  参考来源 · {msg.sources.length} 条
                  <span className="badge badge-plain">点击展开原文</span>
                </div>
                {msg.sources.map((source, index) => (
                  <SourceCard key={`${source.doc_id}-${index}`} source={source} index={index} />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}