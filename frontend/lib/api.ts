
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
  llm_model_display?: string | null;
  rerank_configured?: boolean;
  rerank_model?: string | null;
  agent_configured?: boolean;
  agent_model?: string | null;
  mysql_configured: boolean;
  kb_source?: "mysql" | "postgres";
  pg_configured?: boolean;
  pg_model?: string | null;
  graph_configured?: boolean;
  graph_model?: string | null;
  web_search_configured?: boolean;
  web_search_provider?: string | null;
  web_search_default?: boolean;
  vision_configured?: boolean;
  history_enabled?: boolean;
  history_configured?: boolean;
  auth_enabled?: boolean;
  modes?: ChatMode[];
  index: KbStats;
  ingesting: boolean;
  /** 新增：仍有配置等于 .env.example 的模板默认值（启动 WARNING 同源判定） */
  template_default_risk?: boolean;
};

export type MetricsSummary = {
  total: number;
  avg_elapsed_ms: number;
  max_elapsed_ms: number;
  avg_first_token_ms: number;
  mode_distribution: Record<string, number>;
  cache_hit_rate: number;
  rerank_applied_rate: number;
  rerank_applied: number;
  rerank_failed: number;
  rerank_skipped: number;
  rerank_success_rate: number;
};

export type ChatMode = "react" | "plan" | "deep";

export type User = {
  id: string;
  username: string;
  created_at?: string;
  /** 角色：user（普通）/ admin（管理员）；老后端可能没有该字段 */
  role?: string;
  /** 账号状态：active / disabled；老后端可能没有该字段 */
  status?: string;
};

export type AuthResponse = {
  token: string;
  user: { id: string; username: string; role?: string; status?: string };
};

/** 后台用户管理列表项（只含非敏感字段） */
export type AdminUser = {
  id: string;
  username: string;
  role: string;
  status: string;
  created_at?: string | null;
  last_login_at?: string | null;
};

export type Conversation = {
  id: string;
  title: string;
  mode: string;
  created_at: string;
  updated_at: string;
  message_count: number;
};

export type HistoryMeta = {
  mode?: string;
  tool_calls?: { name: string; query: string }[];
  tool_results?: { name: string; count: number }[];
  sources?: Source[];
  sql?: { sql: string; row_count: number; error?: string | null } | null;
  graph?: { cypher: string; row_count: number; error?: string | null } | null;
  web_search?: { query: string; results: WebSearchResult[]; error?: string | null } | null;
  plan?: PlanStep[];
  steps?: PlanStepRun[];
  reflections?: ReflectionInfo[];
  revision?: string | null;
  done_mode?: string | null;
  error?: string | null;
};

export type HistoryMessage = {
  id: string;
  conversation_id: string;
  seq: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  meta: HistoryMeta | null;
};

export type HistoryHit = {
  message_id: string;
  conversation_id: string;
  conversation_title: string;
  role: string;
  created_at: string;
  mode: string;
  snippet: string;
};

export type PlanStep = {
  index: number;
  title: string;
  detail: string;
};

export type PlanStepRun = {
  index: number;
  title: string;
  status: "running" | "done" | "failed";
  observation: string | null;
  error: string | null;
};

export type ReflectionInfo = {
  round: number;
  verdict: "ok" | "revise";
  issues: string[];
};

export type CitationIssue = {
  type?: string;
  value?: unknown;
  message?: string;
};

/** 数字逐字命中明细：答案里一处金额/百分比/日期是否在引用来源中找到依据 */
export type NumberCheck = {
  kind: "number" | "date" | "percent";
  text: string;
  found: boolean;
};

/** 逐句幻觉标记：该句无支撑的原因（缺引用 / 数值在来源里找不到） */
export type HallucinationFlag = {
  sentence: string;
  reason: "no_citation" | "number_no_source" | string;
};

export type ConversationEvent = {
  type: "conversation";
  id: string | null;
  title: string | null;
  mode: string;
  saved: boolean;
};

export type ToolCallInfo = {
  name: string;
  query?: string;
  arguments?: Record<string, unknown>;
};

export type ToolResultInfo = {
  name: string;
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
  rerank_rank?: number | null;
};

export type RetrievalMeta = {
  dense_count: number;
  sparse_count: number;
  bm25_ready: boolean;
  final_count: number;
  llm_configured: boolean;
  rerank_applied?: boolean;
  rerank_model?: string | null;
  rerank_candidates?: number;
  rerank_error?: string | null;
  rerank_skipped?: boolean;
  rerank_skip_reason?: string | null;
  /** Self-RAG 检索判定：寒暄类问题跳过检索时为 true（老后端没有该字段） */
  skipped_retrieval?: boolean;
};

export type SqlQueryInfo = {
  sql: string;
  columns: string[];
  rowCount: number;
  rows: Record<string, unknown>[];
  error?: string | null;
};

export type GraphQueryInfo = {
  cypher: string;
  rowCount: number;
  rows: Record<string, unknown>[];
  error?: string | null;
};

export type WebSearchResult = {
  title: string;
  url: string;
  content: string;
  score: number;
};

export type WebSearchInfo = {
  query: string;
  results: WebSearchResult[];
  error?: string | null;
};

export type ImageAnalysis = {
  text: string;
  model: string | null;
  filename: string | null;
  mime: string;
  size: number;
};

export type ChatEvent =
  | { type: "retrieval"; meta: RetrievalMeta }
  | { type: "sources"; sources: Source[] }
  | { type: "delta"; text: string; channel?: "answer" | "revision"; reason?: "citation" }
  /** 推理模型的思维链增量（与正文 delta 分两路，绝不混入正文） */
  | { type: "thinking"; delta: string }
  | {
      type: "done";
      mode: string;
      reason?: string;
      /** Agent 通路：本轮单轮最大并行工具数；旧后端 / 纯 RAG 通路没有该字段 */
      parallel_tools?: number;
    }
  | { type: "no_gain"; reason: string; round: number }
  | { type: "error"; message: string }
  | ConversationEvent
  | { type: "plan"; steps: PlanStep[]; fallback: boolean }
  | {
      type: "plan_step";
      index: number;
      title: string;
      status: "running" | "done" | "failed";
      observation: string | null;
      error: string | null;
    }
  | { type: "reflection"; round: number; verdict: "ok" | "revise"; issues: string[]; revision_offered: boolean }
  | { type: "tool_call"; name: string; query: string; arguments: Record<string, unknown> }
  | { type: "tool_result"; name: string; count: number }
  | {
      type: "sql_query";
      sql: string;
      columns: string[];
      row_count: number;
      rows: Record<string, unknown>[];
      error?: string | null;
    }
  | {
      type: "graph_query";
      cypher: string;
      row_count: number;
      rows: Record<string, unknown>[];
      error?: string | null;
    }
  | {
      type: "web_search";
      query: string;
      results: WebSearchResult[];
      error?: string | null;
    }
  | {
      type: "citation_check";
      ok: boolean;
      issues: (string | CitationIssue)[];
      number_checks?: NumberCheck[];
      /** 逐句可信度（有支撑句数 ÷ 总句数）；旧后端可能没有这些新增字段 */
      confidence?: number;
      supported_sentences?: number;
      total_sentences?: number;
      unsupported_sentences?: number;
      hallucination_flags?: HallucinationFlag[];
    };

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
    // 401 = 本地令牌已不被服务端认可（缺失/格式错/过期/已注销）。
    // 这里顺手清掉本地令牌做「双保险」：即使调用方忘了清，也不能让一个
    // 已失效的令牌一直躺在 localStorage 里冒充登录态（注销接口尤其如此）。
    if (res.status === 401) clearToken();
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
  /** 注销：通知后端作废令牌。收到 401（令牌本就失效）也已在 authFetch 里清掉本地令牌 */
  logout: () => authReq<{ ok: boolean; username: string }>("/api/auth/logout", { method: "POST" }),
  /** 查询当前登录用户（is_admin 由后端判定：role=admin 或 AUTH_ADMIN_USERS 白名单） */
  me: () => authReq<{ user: User; is_admin?: boolean }>("/api/auth/me"),

  // ------------------------------ 后台用户管理（仅管理员） ------------------------------ //
  /** 账号列表（仅管理员；非管理员 403） */
  adminUsers: () => authReq<{ items: AdminUser[] }>("/api/admin/users"),
  /** 停用 / 启用账号（仅管理员） */
  adminSetUserStatus: (userId: string, status: "active" | "disabled") =>
    authReq<{ user: AdminUser }>(`/api/admin/users/${encodeURIComponent(userId)}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    }),

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
  /** 可选：本轮改用的模型（glm-* 切智谱）；后端未配 Key 或名字非法时返回 400，不静默回退 */
  model?: string,
): AsyncGenerator<ChatEvent> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, top_k: topK ?? null, model: model || null }),
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
  /** 可选：本轮改用的模型（glm-* 切智谱）；后端未配 Key 或名字非法时返回 400，不静默回退 */
  model?: string,
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
      model: model || null,
    }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`Agent 问答接口返回 ${res.status} ${res.statusText}`);
  }
  yield* readSSE(res);
}