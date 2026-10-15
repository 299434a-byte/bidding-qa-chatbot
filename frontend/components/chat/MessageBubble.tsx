"use client";

import { memo, useEffect, useMemo, useRef, useState } from "react";
import Icon from "@/components/ui/icons";
import ToolTrace from "@/components/chat/ToolTrace";
import { mdToHtml } from "@/lib/markdown";
import { formatHm } from "@/lib/timefmt";
import type {
  CitationIssue,
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


const PG_TOOL = "query_bidding_postgres";
const GRAPH_TOOL = "query_bidding_graph";
const WEB_TOOL = "search_web_bidding";

const STEP_STATUS: Record<PlanStepRun["status"], { label: string; cls: string }> = {
  running: { label: "执行中", cls: "badge-blue" },
  done: { label: "已完成", cls: "badge-mint" },
  failed: { label: "失败", cls: "badge-danger" },
};

export type MsgError = {
  kind: "network" | "server";
  message: string;
  detail?: string;
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  ts?: number | null;
  image?: { url: string; name: string };
  analyzing?: boolean;
  imageResult?: { text: string; model: string | null };
  sources?: Source[];
  meta?: RetrievalMeta;
  /** 推理模型的思维链全文（thinking 事件逐段累积；没有则整块不渲染） */
  thinking?: string;
  toolCalls?: (ToolCallInfo & { count?: number })[];
  sqlQueries?: SqlQueryInfo[];
  graphQueries?: GraphQueryInfo[];
  webSearches?: WebSearchInfo[];
  plan?: PlanStep[];
  planFallback?: boolean;
  steps?: PlanStepRun[];
  reflections?: ReflectionInfo[];
  revision?: string;
  citationIssues?: (string | CitationIssue)[];
  revisionByCitation?: boolean;
  adoptedVersion?: "v1" | "v2";
  mode?: string;
  streaming?: boolean;
  aborted?: boolean;
  error?: MsgError | null;
};

type Props = {
  msg: Message;
  highlighted?: boolean;
  busy?: boolean;
  onFillInput: (text: string) => void;
  onAdopt: (id: string, version: "v1" | "v2") => void;
  onOpenEvidence?: (sourceIndex?: number) => void;
  onRegenerate?: (id: string) => void;
  onToast: (text: string, kind?: "success" | "error" | "info") => void;
};

function pickTitle(source: Source): string {
  const entries = Object.entries(source.fields ?? {});
  const preferred = entries.find(
    ([key, value]) => /title|name|subject|标题|名称|项目名/i.test(key) && typeof value === "string" && value.trim(),
  );
  if (preferred) return String(preferred[1]);
  const firstText = entries.find(([, value]) => typeof value === "string" && value.trim());
  if (firstText) return String(firstText[1]);
  return `记录 ${source.primary_key ?? "—"}`;
}

/** 思考状态条文案：随事件推进 */
function stageText(msg: Message): string {
  if (!msg.streaming) return "";
  if (msg.aborted) return "已停止生成，内容已保留";
  if (msg.steps && msg.steps.some((s) => s.status === "running")) return "正在按计划逐步执行…";
  const pendingTool = (msg.toolCalls ?? []).some((c) => typeof c.count !== "number");
  if (pendingTool) return "正在检索资料…";
  if (msg.meta) {
    const tail = msg.meta.rerank_applied ? "已完成重排" : "正在整理答案…";
    return `已检索到 ${msg.meta.final_count} 条来源 · ${tail}`;
  }
  if (msg.content) return "正在生成回答…";
  return "正在分析问题…";
}

/** 工具调用徽标的人话文案 */
function toolLabel(name: string): string {
  if (name === WEB_TOOL) return "已调用联网搜索";
  if (name === GRAPH_TOOL) return "已调用关系查询";
  if (name === PG_TOOL) return "已调用统计查询";
  return "已调用知识库检索";
}

function fmtTime(ts?: number | null): string {
  // 【展示本地化】原先用浏览器本地时区的 getHours()；统一改为 lib/timefmt 的东八区展示。
  return formatHm(ts);
}

function MessageBubble({ msg, highlighted, busy, onFillInput, onAdopt, onOpenEvidence, onRegenerate, onToast }: Props) {
  // Markdown 只在 content / 来源条数变化时重解析（流式增量由 page.tsx 的 50ms 节流控制频率）
  const citeMax = msg.sources?.length ?? 0;
  const contentHtml = useMemo(
    () => (msg.content ? mdToHtml(msg.content, { citeMax }) : ""),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [msg.content, citeMax],
  );
  const revisionHtml = useMemo(() => (msg.revision ? mdToHtml(msg.revision, { citeMax }) : ""), [msg.revision, citeMax]);

  const bodyRef = useRef<HTMLDivElement>(null);
  /** 思考过程折叠卡：流式期间默认展开，结束后自动折叠一次（用户仍可手动展开） */
  const thinkingRef = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (!msg.streaming && thinkingRef.current) thinkingRef.current.open = false;
  }, [msg.streaming]);
  /** 悬停中的引用信息（编号 + 相对 .msg-body 的定位） */
  const [citeHover, setCiteHover] = useState<{ n: number; top: number; left: number } | null>(null);

  const isAssistant = msg.role === "assistant";
  const showingRevision = msg.revision && (msg.adoptedVersion ?? "v1") === "v2";

  /** 复制内容（纯文本 / Markdown 带来源清单） */
  const copyContent = (asMarkdown: boolean) => {
    let text = msg.content;
    if (asMarkdown && msg.sources && msg.sources.length > 0) {
      const list = msg.sources
        .map((s, i) => `${i + 1}. ${pickTitle(s)}（${s.label ?? "来源"}）`)
        .join("\n");
      text = `${msg.content}\n\n**参考来源**\n\n${list}\n`;
    }
    navigator.clipboard
      .writeText(text)
      .then(() => onToast(asMarkdown ? "已复制为 Markdown" : "已复制到剪贴板", "success"))
      .catch(() => onToast("复制失败，请手动选择内容复制", "error"));
  };

  /** 容器事件委托：引用点击 / 代码块复制按钮 */
  const onBodyClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    const cite = target.closest<HTMLElement>("[data-cite]");
    if (cite) {
      onOpenEvidence?.(Number(cite.dataset.cite) - 1);
      return;
    }
    const copyBtn = target.closest<HTMLElement>(".code-copy");
    if (copyBtn) {
      const block = copyBtn.closest(".code-block");
      const code = block?.querySelector("pre code")?.textContent ?? "";
      navigator.clipboard
        .writeText(code)
        .then(() => {
          copyBtn.classList.add("is-copied");
          copyBtn.textContent = "已复制";
          window.setTimeout(() => {
            copyBtn.classList.remove("is-copied");
            copyBtn.textContent = "复制";
          }, 1500);
        })
        .catch(() => onToast("复制失败，请手动选择代码", "error"));
    }
  };

  /** 引用悬停弹层：计算相对位置（Canvas 由 .msg-body 的 position:relative 提供） */
  const onBodyMouseOver = (e: React.MouseEvent<HTMLDivElement>) => {
    const cite = (e.target as HTMLElement).closest<HTMLElement>("[data-cite]");
    const body = bodyRef.current;
    if (!cite || !body) {
      setCiteHover(null);
      return;
    }
    const n = Number(cite.dataset.cite);
    const bodyRect = body.getBoundingClientRect();
    const citeRect = cite.getBoundingClientRect();
    const top = citeRect.bottom - bodyRect.top + 6;
    const left = Math.min(Math.max(citeRect.left - bodyRect.left, 0), bodyRect.width - 220);
    setCiteHover({ n, top, left });
  };

  const hoveredSource = citeHover ? (msg.sources ?? [])[citeHover.n - 1] : undefined;

  return (
    <div className={`msg msg-${msg.role}${highlighted ? " msg-highlight" : ""}`} id={`msg-${msg.id}`}>
      <div className="msg-avatar" aria-hidden="true">
        {msg.role === "user" ? "你" : <Icon name="message" size={15} />}
      </div>
      <div
        className="msg-body"
        ref={bodyRef}
        onClick={onBodyClick}
        onMouseOver={onBodyMouseOver}
        onMouseLeave={() => setCiteHover(null)}
      >
        <div className="msg-head">
          <span className="msg-name">{msg.role === "user" ? "你" : "助手"}</span>
          <span className="msg-time">{fmtTime(msg.ts)}</span>
          {msg.mode && !msg.streaming && <span className="badge badge-plain">{msg.mode}</span>}
        </div>

        {msg.role === "user" ? (
          <>
            {msg.image && <img className="msg-image" src={msg.image.url} alt={msg.image.name} />}
            <div className="msg-text">{msg.content}</div>
            <div className="msg-actions">
              <button
                className="act-btn"
                type="button"
                onClick={() =>
                  navigator.clipboard
                    .writeText(msg.content)
                    .then(() => onToast("已复制到剪贴板", "success"))
                    // 剪贴板权限可能被浏览器策略拒绝（如非 HTTPS / 未授权），必须兜底
                    .catch(() => onToast("复制失败，请手动选择内容复制", "error"))
                }
              >
                <Icon name="copy" size={14} />
                复制
              </button>
            </div>
          </>
        ) : (
          <>
            {/* 思考状态条（生成中不空白等待；aria-live 供读屏感知阶段变化） */}
            {(msg.streaming || msg.aborted) && (
              <div className="think-strip" role="status" aria-live="polite" data-testid="streaming">
                {!msg.aborted && <span className="think-spinner" aria-hidden="true" />}
                <span className="think-text">{stageText(msg)}</span>
              </div>
            )}

            {/* 图片解析 / 识别结果 */}
            {msg.analyzing && (
              <div className="think-strip">
                <span className="think-spinner" aria-hidden="true" />
                <span className="think-text">正在识别图片内容…</span>
              </div>
            )}
            {msg.imageResult && (
              <div className="vision-card">
                <div className="vision-card-head">
                  <span className="badge badge-mint">图片识别结果</span>
                  {msg.imageResult.model && <span className="badge badge-plain">{msg.imageResult.model}</span>}
                  <button className="chip" type="button" onClick={() => onFillInput(msg.imageResult!.text)} disabled={busy}>
                    将识别结果填入提问框
                  </button>
                </div>
                <div className="vision-card-body">{msg.imageResult.text}</div>
              </div>
            )}

            {/* 思考过程（推理模型思维链）：可折叠卡，样式复用 ToolTrace 的 trace-item。
                没有 thinking 事件（老流程 / 非推理模型）时整块不出现。 */}
            {msg.thinking && (
              <details className="trace-item thinking-card" ref={thinkingRef} open data-testid="thinking">
                <summary className="trace-summary">
                  <span className="badge badge-plain">思考过程</span>
                  <span className="mono-num">{msg.thinking.length} 字</span>
                  <span className="badge badge-plain">{msg.streaming ? "思考中…" : "已折叠"}</span>
                </summary>
                <pre className="trace-code">{msg.thinking}</pre>
              </details>
            )}

            {/* 首 token 前：骨架行占位（内容来了就消失） */}
            {msg.streaming && !msg.content && !msg.aborted && (
              <div aria-hidden="true">
                <div className="skeleton skel-line" />
                <div className="skeleton skel-line" />
                <div className="skeleton skel-line short" />
              </div>
            )}

            {/* 采纳修订版标记 */}
            {showingRevision && msg.revisionByCitation && (
              <div className="adopted-tag">
                <span className="badge badge-warn">已采纳引用校验修订稿</span>
              </div>
            )}
            {showingRevision && !msg.revisionByCitation && (
              <div className="adopted-tag">
                <span className="badge badge-warn">已采纳修订稿</span>
              </div>
            )}

            {/* 正文（流式期间挂 aria-live=polite——读屏可感知生成进展，且 50ms 节流避免了逐 token 播报） */}
            {msg.content && (
              <div
                className="answer"
                data-testid="answer"
                aria-live={msg.streaming ? "polite" : undefined}
                aria-busy={msg.streaming || undefined}
                dangerouslySetInnerHTML={{ __html: contentHtml }}
              />
            )}
            {msg.streaming && msg.content && <span className="cursor" aria-hidden="true" />}

            {/* 引用 hover 弹层 */}
            {citeHover && hoveredSource && (
              <div className="cite-pop" style={{ top: citeHover.top, left: citeHover.left }} role="tooltip">
                <div className="cite-pop-title">
                  <span className="src-n">{citeHover.n}</span>
                  {hoveredSource.label ?? "来源"}
                </div>
                <div className="cite-pop-snippet">{hoveredSource.text}</div>
                <div className="cite-pop-footer">
                  {typeof hoveredSource.rerank_rank === "number" && <span>重排 #{hoveredSource.rerank_rank}</span>}
                  {hoveredSource.scores.dense !== undefined && <span>相似度 {hoveredSource.scores.dense.toFixed(3)}</span>}
                  <span>点击定位原文</span>
                </div>
              </div>
            )}

            {/* 错误块：人话 + 重试 + 可折叠技术详情（不清空已输出内容） */}
            {msg.error && (
              <div className="err-block">
                <div className="err-title">
                  <Icon name="alert" size={16} />
                  {msg.error.kind === "network" ? "网络中断" : "服务出错了"}
                </div>
                <div className="err-msg">{msg.error.message}</div>
                <div className="err-actions">
                  {(onRegenerate || msg.aborted) && (
                    <button className="btn btn-primary" type="button" onClick={() => onRegenerate?.(msg.id)}>
                      {msg.aborted ? "继续生成" : "重试"}
                    </button>
                  )}
                </div>
                {msg.error.detail && (
                  <details className="err-detail">
                    <summary>技术详情</summary>
                    <pre>{msg.error.detail}</pre>
                  </details>
                )}
              </div>
            )}

            {/* 引用校验问题提示（保留 revision 语义） */}
            {msg.citationIssues && msg.citationIssues.length > 0 && (
              <div className="notice notice-warn">
                引用校验发现 {msg.citationIssues.length} 处问题
                {msg.revision && "，已生成修订稿（见下方切换）"}
                <ul className="reflect-issues">
                  {msg.citationIssues.map((issue, i) => (
                    // 后端 issue 是 {type, value, message} 对象，取 message 展示；兼容字符串/缺字段
                    <li key={i}>{typeof issue === "string" ? issue : issue.message ?? "引用校验发现问题"}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* 修订稿：标签 + v1/v2 对比切换（引用校验与深度反思共用此卡片） */}
            {msg.revision && (
              <div className="revision-card">
                <details open>
                  <summary className="revision-head">
                    <span className="badge badge-warn">{msg.revisionByCitation ? "引用校验修订稿" : "修订稿"}</span>
                    <span className="mono-num">v1 原回答 / v2 修订稿 可切换</span>
                  </summary>
                  <div className="revision-toggle" role="group" aria-label="采纳版本">
                    <button
                      className={`seg-item ${(msg.adoptedVersion ?? "v1") === "v1" ? "seg-active" : ""}`}
                      type="button"
                      aria-pressed={(msg.adoptedVersion ?? "v1") === "v1"}
                      onClick={() => onAdopt(msg.id, "v1")}
                    >
                      原回答 v1
                    </button>
                    <button
                      className={`seg-item ${(msg.adoptedVersion ?? "v1") === "v2" ? "seg-active" : ""}`}
                      type="button"
                      aria-pressed={(msg.adoptedVersion ?? "v1") === "v2"}
                      onClick={() => onAdopt(msg.id, "v2")}
                    >
                      修订稿 v2
                    </button>
                  </div>
                  <div className="revision-hint current">
                    下一轮对话将回传所选版本：当前采纳「{(msg.adoptedVersion ?? "v1") === "v2" ? "修订稿 v2" : "原回答 v1"}」。
                  </div>
                  <div className="revision-body answer" dangerouslySetInnerHTML={{ __html: revisionHtml }} />
                </details>
              </div>
            )}

            {/* 来源条（Perplexity 式编号卡）：点击在证据面板定位 */}
            {msg.sources && msg.sources.length > 0 && (
              <div className="sources-title" id={`sources-${msg.id}`}>
                <Icon name="file-text" size={13} />
                参考来源 · {msg.sources.length} 条
                <span className="badge badge-plain">点击在右侧核对原文</span>
              </div>
            )}
            {msg.sources && msg.sources.length > 0 && (
              <div className="src-strip">
                {msg.sources.map((source, index) => (
                  <button
                    className="src-chip"
                    type="button"
                    key={`${source.doc_id}-${index}`}
                    data-testid="src-chip"
                    onClick={() => onOpenEvidence?.(index)}
                  >
                    <span className="src-chip-head">
                      <span className="src-n">{index + 1}</span>
                      <span className="src-kind">{source.label}</span>
                    </span>
                    <span className="src-title">{pickTitle(source)}</span>
                    <span className="src-meta">
                      {typeof source.rerank_rank === "number" && <>重排 #{source.rerank_rank}</>}
                      {/* 分隔符只在「前后两段都有内容」时出现，避免悬空的「 · 」 */}
                      {typeof source.rerank_rank === "number" &&
                        source.matched_by.includes("dense") &&
                        source.scores.dense !== undefined &&
                        <> · </>}
                      {source.matched_by.includes("dense") && source.scores.dense !== undefined && <>相似度 {source.scores.dense.toFixed(2)}</>}
                    </span>
                  </button>
                ))}
              </div>
            )}

            {/* 操作条：复制 / 复制为 Markdown / 重新生成（中断后是继续生成）/ 引用定位 */}
            <div className="msg-actions">
              <button className="act-btn" type="button" onClick={() => copyContent(false)}>
                <Icon name="copy" size={14} />
                复制
              </button>
              <button className="act-btn" type="button" onClick={() => copyContent(true)} disabled={!msg.content}>
                <Icon name="file-text" size={14} />
                复制为 Markdown
              </button>
              {onRegenerate && (
                <button className="act-btn" type="button" onClick={() => onRegenerate(msg.id)} disabled={busy && !msg.aborted}>
                  <Icon name="regenerate" size={14} />
                  {msg.aborted ? "继续生成" : "重新生成"}
                </button>
              )}
              {msg.sources && msg.sources.length > 0 && onOpenEvidence && (
                <button className="act-btn act-primary" type="button" onClick={() => onOpenEvidence()}>
                  <Icon name="locate" size={14} />
                  引用定位
                </button>
              )}
            </div>

            {/* 规划 / 步骤 / 反思：保留原有语义，样式对齐 token */}
            {msg.plan && msg.plan.length > 0 && (
              <div className="plan-card">
                <div className="plan-card-head">
                  <span className="badge badge-blue">规划</span>
                  <span className="badge badge-plain">{msg.plan.length} 个子任务</span>
                  {msg.planFallback && <span className="badge badge-warn">已回退为单轮模式</span>}
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
                        <span>
                          {step.index}. {step.title || "（无标题）"}
                        </span>
                      </summary>
                      {step.observation && <div className="step-observation">{step.observation}</div>}
                      {step.error && <div className="notice notice-danger">{step.error}</div>}
                    </details>
                  );
                })}
              </div>
            )}

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
                    <span className="mono-num">第 {reflection.round} 轮</span>
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

            {/* 工具调用痕迹（SQL / Cypher / 联网搜索结果，可折叠） */}
            <ToolTrace sqlQueries={msg.sqlQueries} graphQueries={msg.graphQueries} webSearches={msg.webSearches} />
          </>
        )}
      </div>
    </div>
  );
}

// React.memo + props 浅比较：流式期间只有「正在增长的那条」因 msg 引用变化而重渲染
export default memo(MessageBubble);