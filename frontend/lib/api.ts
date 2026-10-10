/**
 * 后端接口封装。
 * SSE 用 fetch + ReadableStream 手动解析（EventSource 只支持 GET，
 * 而 /api/chat 需要 POST 传问题正文）。
 */

// 回落规则：只有「完全没设置过」才用本地开发默认值；显式留空（=""）表示同源部署，
// 请求走相对路径 /api/*，由 Nginx 反代转给后端（|| 会把空串也当成假值，做不到这一点）。
const raw = process.env.NEXT_PUBLIC_API_BASE;
export const API_BASE = (raw === undefined ? "http://127.0.0.1:8000" : raw).replace(/\/+$/, "");

export type ColumnInfo = {
  name: string;
  type: string;
  column_type: string;
  nullable: boolean;
  key: string;
  comment: string;
  position: number;
};

export type TableInfo = {
  table: string;
  label?: string;
  exists: boolean;
  row_count?: number;
  primary_key?: string | null;
  columns?: ColumnInfo[];
  error?: string | null;
};

export type SchemaResponse = {
  connected: boolean;
  error: string | null;
  database: string;
  tables: Record<string, TableInfo>;
  available_tables: { table: string; approx_rows: number }[];
  total_rows?: number;
};

export type KbStats = {
  collection: string;
  path: string;
  exists: boolean;
  points: number;
  by_table: Record<string, number>;
  vector_dim: number;
  empty: boolean;
  bm25_docs?: number;
  embed_model?: string;
  table_labels?: Record<string, string>;
  error?: string;
};

export type Health = {
  status: string;
  llm_configured: boolean;
  llm_model: string | null;
  /** 界面展示用的大模型名（优先于 llm_model，由后端 LLM_MODEL_DISPLAY 决定） */
  llm_model_display?: string | null;
  rerank_configured?: boolean;
  rerank_model?: string | null;
  /** Agent 是否可用（依赖大模型配置） */
  agent_configured?: boolean;
  agent_model?: string | null;
  mysql_configured: boolean;
  /** 建库管道当前实际使用的数据源：mysql（默认）/ postgres（本机副本） */
  kb_source?: "mysql" | "postgres";
  /** PostgreSQL 查询工具是否可用（连接信息齐备才算配置好） */
  pg_configured?: boolean;
  pg_model?: string | null;
  /** Neo4j 知识图谱查询工具是否可用（连接信息齐备才算配置好） */
  graph_configured?: boolean;
  graph_model?: string | null;
  /** 联网搜索工具是否可用（取决于后端是否配置 TAVILY_API_KEY） */
  web_search_configured?: boolean;
  web_search_provider?: string | null;
  /** 「联网搜索」开关的后端默认值 */
  web_search_default?: boolean;
  /** 图片解析是否可用（取决于后端是否配置 ZHIPUAI_API_KEY） */
  vision_configured?: boolean;
  /** 对话历史是否开启（后端 HISTORY_ENABLED） */
  history_enabled?: boolean;
  /** 对话历史是否可用（历史与账号体系都开启才算配置好） */
  history_configured?: boolean;
  /** 账号体系是否开启（后端 AUTH_ENABLED） */
  auth_enabled?: boolean;
  /** 后端支持的推理模式 */
  modes?: ChatMode[];
  index: KbStats;
  ingesting: boolean;
};

/** 后台问答可观测性指标（GET /api/metrics 的返回体，含重排成功/失败计数） */
export type MetricsSummary = {
  /** 累计问答次数 */
  total: number;
  avg_elapsed_ms: number;
  max_elapsed_ms: number;
  avg_first_token_ms: number;
  mode_distribution: Record<string, number>;
  cache_hit_rate: number;
  /** 重排应用率：成功次数 / 全部问答（0~1） */
  rerank_applied_rate: number;
  /** 重排成功的次数 */
  rerank_applied: number;
  /** 重排失败降级的次数 */
  rerank_failed: number;
  /** 重排成功率：成功 /（成功 + 失败），只统计真正尝试过重排的问答（0~1） */
  rerank_success_rate: number;
};

/** 推理模式：ReAct / Plan-and-Solve / 深度思考 */
export type ChatMode = "react" | "plan" | "deep";

/** 登录用户 */
export type User = {
  id: string;
  username: string;
  created_at?: string;
};

/** 注册 / 登录成功的返回体 */
export type AuthResponse = {
  token: string;
  user: { id: string; username: string };
};

/** 会话列表项 */
export type Conversation = {
  id: string;
  title: string;
  mode: string;
  created_at: string;
  updated_at: string;
  message_count: number;
};

/** 助手消息落库的元数据（恢复会话时据此重建工具/来源/规划等卡片） */
export type HistoryMeta = {
  mode?: string;
  tool_calls?: { name: string; query: string }[];
  tool_results?: { name: string; count: number }[];
  sources?: Source[];
  /** 恢复时只有 sql / row_count / error，没有结果明细行 */
  sql?: { sql: string; row_count: number; error?: string | null } | null;
  /** 恢复时只有 cypher / row_count / error，没有结果明细行 */
  graph?: { cypher: string; row_count: number; error?: string | null } | null;
  web_search?: { query: string; results: WebSearchResult[]; error?: string | null } | null;
  plan?: PlanStep[];
  steps?: PlanStepRun[];
  reflections?: ReflectionInfo[];
  revision?: string | null;
  done_mode?: string | null;
  error?: string | null;
};

/** 历史里的单条消息 */
export type HistoryMessage = {
  id: string;
  conversation_id: string;
  seq: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  meta: HistoryMeta | null;
};

/** 历史搜索结果项（snippet 已含 <em> 高亮且已转义，直接渲染） */
export type HistoryHit = {
  message_id: string;
  conversation_id: string;
  conversation_title: string;
  role: string;
  created_at: string;
  mode: string;
  snippet: string;
};

/** 规划阶段的一个子任务 */
export type PlanStep = {
  index: number;
  title: string;
  detail: string;
};

/** 子任务执行状态 */
export type PlanStepRun = {
  index: number;
  title: string;
  status: "running" | "done" | "failed";
  observation: string | null;
  error: string | null;
};

/** 深度模式的自评结果 */
export type ReflectionInfo = {
  round: number;
  verdict: "ok" | "revise";
  issues: string[];
};

/**
 * 引用校验发现的一处问题（后端 citation.py 产出）。
 * 注意：后端发的是对象而非字符串，旧前端把它当 string 直接渲染会导致
 * 「Objects are not valid as a React child」崩溃，故显式声明真实结构。
 */
export type CitationIssue = {
  /** 问题类型，如 ref_out_of_range / number_no_source */
  type?: string;
  /** 出问题的原始片段（引用编号或数值等，类型不固定） */
  value?: unknown;
  /** 给人看的一句话说明 */
  message?: string;
};

/** SSE 首个事件：告知当前会话的落库情况 */
export type ConversationEvent = {
  type: "conversation";
  id: string | null;
  title: string | null;
  mode: string;
  saved: boolean;
};

/** Agent 发起的一次工具调用 */
export type ToolCallInfo = {
  name: string;
  /** 工具入参里的检索词（Agent 可能对用户原话做了改写） */
  query?: string;
  arguments?: Record<string, unknown>;
};

/** Agent 工具调用的执行结果 */
export type ToolResultInfo = {
  name: string;
  /** 命中条数 */
  count: number;
};

export type Source = {
  doc_id: string;
  table: string;
  label: string;
  primary_key: string | number | null;
  chunk_index: number;
  chunk_total: number;
  rrf_score: number;
  scores: Record<string, number>;
  ranks: Record<string, number>;
  matched_by: string[];
  text: string;
  fields: Record<string, unknown>;
  /** 大模型重排后的名次（1 起）。未重排时为 null */
  rerank_rank?: number | null;
};

export type RetrievalMeta = {
  dense_count: number;
  sparse_count: number;
  bm25_ready: boolean;
  final_count: number;
  llm_configured: boolean;
  /** 重排是否真的生效（未配置大模型 / 候选不足 / 调用失败时为 false） */
  rerank_applied?: boolean;
  rerank_model?: string | null;
  rerank_candidates?: number;
  rerank_error?: string | null;
};

/** Agent 调用 PostgreSQL 工具后返回的查询详情（供前端展示 SQL 与结果表格） */
export type SqlQueryInfo = {
  sql: string;
  columns: string[];
  rowCount: number;
  rows: Record<string, unknown>[];
  /** 非空表示这次查询失败 */
  error?: string | null;
};

/** Agent 调用 Neo4j 图谱工具后返回的查询详情（供前端展示 Cypher 与结果） */
export type GraphQueryInfo = {
  cypher: string;
  rowCount: number;
  rows: Record<string, unknown>[];
  /** 非空表示这次查询失败 */
  error?: string | null;
};

/** 联网搜索的单条结果（Tavily） */
export type WebSearchResult = {
  title: string;
  url: string;
  content: string;
  /** 相关性评分（0~1，越大越相关），由 Tavily 返回 */
  score: number;
};

/** Agent 调用联网搜索工具后返回的搜索详情 */
export type WebSearchInfo = {
  /** 实际用于搜索的关键词（Agent 可能对用户原话做了改写） */
  query: string;
  results: WebSearchResult[];
  /** 非空表示这次联网搜索失败 */
  error?: string | null;
};

/** 图片解析结果（POST /analyze-image） */
export type ImageAnalysis = {
  /** 视觉模型识别出的文本内容 */
  text: string;
  model: string | null;
  filename: string | null;
  mime: string;
  size: number;
};

export type ChatEvent =
  | { type: "retrieval"; meta: RetrievalMeta }
  | { type: "sources"; sources: Source[] }
  /** channel 缺省为 answer（正文）；revision 是改进稿，需单独渲染。
   *  reason="citation" 表示这份修订稿来自「引用校验」（与深度模式的修订稿区分）。 */
  | { type: "delta"; text: string; channel?: "answer" | "revision"; reason?: "citation" }
  | { type: "done"; mode: string }
  | { type: "error"; message: string }
  /** SSE 首个事件：会话落库情况（未登录 / 未开启历史时 saved:false, id:null） */
  | ConversationEvent
  /** 规划阶段产出的子任务列表（仅 plan / deep 模式） */
  | { type: "plan"; steps: PlanStep[]; fallback: boolean }
  /** 单个子任务的执行状态（仅 plan / deep 模式） */
  | {
      type: "plan_step";
      index: number;
      title: string;
      status: "running" | "done" | "failed";
      observation: string | null;
      error: string | null;
    }
  /** 深度模式的自评结果（仅 deep 模式） */
  | { type: "reflection"; round: number; verdict: "ok" | "revise"; issues: string[]; revision_offered: boolean }
  /** Agent 决定调用工具（仅 /api/agent/chat 会发） */
  | { type: "tool_call"; name: string; query: string; arguments: Record<string, unknown> }
  /** 工具执行完成（仅 /api/agent/chat 会发） */
  | { type: "tool_result"; name: string; count: number }
  /** Agent 调用了 PostgreSQL 工具（仅 /api/agent/chat 会发，在 tool_result 之前） */
  | {
      type: "sql_query";
      sql: string;
      columns: string[];
      row_count: number;
      rows: Record<string, unknown>[];
      error?: string | null;
    }
  /** Agent 调用了 Neo4j 图谱工具（仅 /api/agent/chat 会发，在 tool_result 之前） */
  | {
      type: "graph_query";
      cypher: string;
      row_count: number;
      rows: Record<string, unknown>[];
      error?: string | null;
    }
  /** Agent 调用了 Tavily 联网搜索（仅 /api/agent/chat 会发，在 tool_result 之前） */
  | {
      type: "web_search";
      query: string;
      results: WebSearchResult[];
      error?: string | null;
    }
  /** 引用校验结果：正文生成后体检发现越界引用/无依据数值时发出（ok:false），供前端提示 */
  | { type: "citation_check"; ok: boolean; issues: (string | CitationIssue)[] };

/** 多轮对话中的一轮，由前端持有并回传给后端（后端无状态） */
export type ChatTurn = {
  role: "user" | "assistant";
  content: string;
};

export type IngestStatus = {
  running: boolean;
  stage: string;
  message: string;
  logs: string[];
  result: Record<string, unknown> | null;
  error: string | null;
  started_at: number | null;
  finished_at: number | null;
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store", ...init });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* 忽略解析失败，用状态码兜底 */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

/** 登录令牌在 localStorage 中的键名 */
const TOKEN_KEY = "bidding.token";

/** 读取本地令牌（SSR 环境下返回 null） */
export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

/** 写入 / 清除本地令牌（传 null 即清除） */
export function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 隐私模式下写入可能失败，忽略 */
  }
}

/** 清除本地令牌（令牌失效或主动注销时调用） */
export function clearToken(): void {
  setToken(null);
}

/** 带登录令牌的 fetch：已登录时自动附加 Authorization 头，失败时抛出后端的中文 detail。 */
async function authFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store", ...init, headers });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* 忽略解析失败，用状态码兜底 */
    }
    throw new Error(detail);
  }
  return res;
}

/** authFetch 的 JSON 版本 */
async function authReq<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(path, init);
  return (await res.json()) as T;
}

/** 把可选查询参数拼成 querystring（跳过空值） */
function query(params: Record<string, unknown>): string {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    qs.set(key, String(value));
  }
  const suffix = qs.toString();
  return suffix ? `?${suffix}` : "";
}

export const api = {
  health: () => req<Health>("/api/health"),
  config: () => req<Record<string, unknown>>("/api/config"),
  schema: () => req<SchemaResponse>("/api/mysql/schema"),
  kbStats: () => req<KbStats>("/api/kb/stats"),
  startIngest: (mode: "full" | "incremental" = "full") =>
    authReq<{ started: boolean; message: string }>("/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // 全量：recreate=true（清空重建，行为与改动前一致）；增量：recreate=false（保留旧数据）。
      body: JSON.stringify({ mode, recreate: mode === "full" }),
    }),
  ingestStatus: () => req<IngestStatus>("/api/ingest/status"),
  /** 问答可观测性指标（耗时 / 缓存命中率 / 重排成功率与失败计数）；接口已需登录，带令牌请求 */
  metrics: () => authReq<MetricsSummary>("/api/metrics"),

  // ------------------------------ 账号体系 ------------------------------ //
  /** 注册并直接拿到令牌 */
  register: (username: string, password: string) =>
    authReq<AuthResponse>("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),
  /** 登录 */
  login: (username: string, password: string) =>
    authReq<AuthResponse>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),
  /** 注销（令牌失效即可） */
  logout: () => authReq<{ ok: boolean; username: string }>("/api/auth/logout", { method: "POST" }),
  /** 查询当前登录用户 */
  me: () => authReq<{ user: User }>("/api/auth/me"),

  // ------------------------------ 对话历史 ------------------------------ //
  /** 会话列表（分页，可按关键词 / 模式过滤） */
  conversations: (params: { limit?: number; offset?: number; q?: string; mode?: string } = {}) =>
    authReq<{ items: Conversation[]; total: number; limit: number; offset: number }>(
      `/api/history/conversations${query(params)}`,
    ),
  /** 新建会话 */
  createConversation: (mode: ChatMode, title?: string) =>
    authReq<{ conversation: Conversation }>("/api/history/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title ?? null, mode }),
    }),
  /** 会话详情（含消息与元数据） */
  conversationDetail: (id: string, limit?: number) =>
    authReq<{ conversation: Conversation; messages: HistoryMessage[] }>(
      `/api/history/conversations/${encodeURIComponent(id)}${query({ limit })}`,
    ),
  /** 重命名会话 */
  renameConversation: (id: string, title: string) =>
    authReq<{ conversation: Conversation }>(`/api/history/conversations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }),
  /** 删除会话 */
  deleteConversation: (id: string) =>
    authReq<{ ok: boolean; id: string }>(`/api/history/conversations/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  /** 全文检索历史消息（关键词 + 时间范围 + 模式） */
  searchHistory: (params: {
    q: string;
    from?: string;
    to?: string;
    mode?: string;
    limit?: number;
    offset?: number;
  }) =>
    authReq<{ items: HistoryHit[]; total: number; limit: number; offset: number; fts: boolean }>(
      `/api/history/search${query(params)}`,
    ),
};

/** 上传图片给后端做视觉解析（智谱视觉模型），返回识别出的文本。 */
export async function analyzeImage(file: File, question?: string): Promise<ImageAnalysis> {
  const form = new FormData();
  form.append("file", file);
  if (question && question.trim()) form.append("question", question.trim());

  // 图片解析会消耗付费模型额度，后端要求登录；复用 authFetch 自动带上 Bearer 令牌
  const res = await authFetch("/analyze-image", { method: "POST", body: form });
  return (await res.json()) as ImageAnalysis;
}

/**
 * 解析单个 SSE 事件块（已按空行切开）。
 * 一个事件可能有多行 data:，按规范用「\n」拼接成一份 payload 再做 JSON 解析。
 * 返回 "[DONE]" 表示流结束；返回 null 表示这一块没有可产出的事件（跳过）。
 */
function parseSSEBlock(block: string): ChatEvent | "[DONE]" | null {
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (!line.startsWith("data:")) continue;
    // 去掉 "data:" 前缀，并按规范只吃掉一个前导空格（其余空白原样保留）
    dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (dataLines.length === 0) return null;
  const payload = dataLines.join("\n").trim();
  if (!payload) return null;
  if (payload === "[DONE]") return "[DONE]";
  try {
    return JSON.parse(payload) as ChatEvent;
  } catch {
    return null; // 跳过无法解析的分片
  }
}

/** 解析后端 SSE 响应，逐条产出事件（/api/chat 与 /api/agent/chat 共用）。 */
async function* readSSE(res: Response): AsyncGenerator<ChatEvent> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      // 把 \r\n 统一成 \n，兼容两种换行的后端；stream:true 让多字节字符跨块也能正确解码
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

      // SSE 以空行分隔事件
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";

      for (const block of blocks) {
        const event = parseSSEBlock(block);
        if (event === "[DONE]") return; // 提前返回也走 finally，统一释放 reader
        if (event) yield event;
      }
    }

    // 流正常结束：flush 解码器残留，并处理最后一段「没有以空行结尾」的事件
    buffer += decoder.decode();
    if (buffer.trim()) {
      const event = parseSSEBlock(buffer);
      if (event && event !== "[DONE]") yield event;
    }
  } finally {
    // 无论正常结束 / [DONE] / 抛错 / 被 abort，都取消并释放 reader，避免连接与锁泄漏
    try {
      await reader.cancel();
    } catch {
      /* 已关闭时 cancel 可能抛错，忽略 */
    }
    reader.releaseLock();
  }
}

/** 流式问答（纯 RAG 通道）：逐条产出后端事件。 */
export async function* streamChat(
  question: string,
  topK?: number,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, top_k: topK ?? null }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`问答接口返回 ${res.status} ${res.statusText}`);
  }
  yield* readSSE(res);
}

/** 流式问答（Agent 通道）：Agent 自主决定是否调用知识库检索 / 联网搜索等工具。 */
export async function* streamAgentChat(
  question: string,
  history: ChatTurn[] = [],
  topK?: number,
  webSearch?: boolean,
  mode?: ChatMode,
  conversationId?: string | null,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const headers = new Headers({ "Content-Type": "application/json" });
  // 已登录时带上令牌，后端才会把本轮对话落库
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${API_BASE}/api/agent/chat`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      question,
      top_k: topK ?? null,
      history,
      web_search: webSearch ?? null,
      mode: mode ?? "react",
      conversation_id: conversationId ?? null,
    }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`Agent 问答接口返回 ${res.status} ${res.statusText}`);
  }
  yield* readSSE(res);
}