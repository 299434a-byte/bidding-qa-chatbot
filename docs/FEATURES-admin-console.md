# 功能说明：后台管理台（Admin Console）

后台管理页入口为 `frontend/app/admin/page.tsx`，由它**统一拉取数据**（`Promise.all([api.health(), api.kbStats(), api.schema()])`），子组件只负责展示；建库进度单独轮询。页面支持「自动刷新 30s」（常量 `AUTO_REFRESH_MS = 30_000`）与手动「刷新」。登录态由 `api.me()` 决定，供写操作按钮判断。

涉及文件（相对 `frontend/`）：

- `app/admin/page.tsx` —— 页面与数据编排
- `components/admin/StatusCards.tsx`（系统状态）、`DataOverview.tsx`（数据概览）、`DebugConsole.tsx`（调试控制台 / 重建进度）、`EvalCard.tsx`（评测结果）、`SchemaCard.tsx`（数据表结构）、`HistorySearch.tsx`（历史检索）
- `app/api/eval-report/route.ts` —— 评测报告的 Next.js 路由
- `lib/api.ts` —— 各接口封装

下表先给总览，后文逐卡展开。

| 卡片 | 组件 | 数据来源 | 对应接口 |
| --- | --- | --- | --- |
| 系统状态 | `StatusCards.tsx` | `health` / `kb` / `schema` | `GET /api/health`、`GET /api/kb/stats`、`GET /api/mysql/schema` |
| 数据概览 | `DataOverview.tsx` | `kb` / `schema` | 同上 |
| 调试控制台（重建进度） | `DebugConsole.tsx` | `ingest` | `POST /api/ingest`、`GET /api/ingest/status` |
| 评测结果 | `EvalCard.tsx` | `backend/eval/report.md` | `GET /api/eval-report`（Next.js 路由） |
| 数据表结构 | `SchemaCard.tsx` | `schema` | `GET /api/mysql/schema` |
| 历史检索 | `HistorySearch.tsx` | `user` + 检索请求 | `GET /api/history/search` |

---

## 1. 系统状态（StatusCards）

**数据来源**：页面同时拉取的 `health`（`Health`）、`kb`（`KbStats`）、`schema`（`SchemaResponse`）三个对象。
**对应接口**：

- `health` ← `GET /api/health`（`lib/api.ts`：`health: () => req<Health>("/api/health")`）
- `kb` ← `GET /api/kb/stats`
- `schema` ← `GET /api/mysql/schema`

**展示内容**：按「模型 / 数据 / 工具」三组渲染，每张卡统一为「图标 + 名称 + 值 + 说明 + 语义色状态点」（灰=未启用、绿=正常、红=异常）。引用的字段均来自 `/api/health`：`llm_configured` / `llm_model_display` / `llm_model`、`rerank_configured` / `rerank_model`、`web_search_configured`、`pg_configured`、`graph_configured`、`mysql_configured`、`kb_source`；向量维度与块数来自 `kb`（如 `kb.points`、`kb.vector_dim`、`kb.embed_model`）。例如「数据源」卡片据 `health.kb_source === "postgres"` 显示为「PostgreSQL 副本」。

## 2. 数据概览（DataOverview）

**数据来源**：`kb` 与 `schema`。
**对应接口**：`GET /api/kb/stats`（提供 `points`、`bm25_docs`、`by_table`、`table_labels`）与 `GET /api/mysql/schema`（提供 `tables`，含每张表的 `label` / `exists` / `row_count`）。
**展示内容**：知识库文档块分布（总数 + 按来源 `by_table` 分块）与「业务数据表（行数）」列表；数字统一千分位 + 单位，空值显示 `—`。表不可读时给黄色提示。

## 3. 调试控制台 / 重建进度（DebugConsole）

**数据来源**：`ingest`（`IngestStatus`），由页面维护并轮询。
**对应接口**：

- 触发：`POST /api/ingest`（`lib/api.ts`：`startIngest(mode: "full" | "incremental" = "full")`，走 `authReq` 带登录令牌）。页面里：重建调 `api.startIngest("full")`，增量调 `api.startIngest("incremental")`。
- 进度：`GET /api/ingest/status`（`ingestStatus: () => req<IngestStatus>("/api/ingest/status")`）。

**重建进度**：建库进行中（`ingest.running` 为真）时页面每 **1500ms** 轮询一次 `/api/ingest/status`；进度条 + 阶段文案由 `ingest.message` / `ingest.stage` 驱动，完成后回显 `result`（`documents` / `points` / `bm25_terms` / `elapsed`，增量模式另有 `added` / `updated` / `deleted` / `unchanged`）并滚动展示最近 60 行日志。

**权限**：**未登录时重建 / 增量更新按钮完全不显示**，只保留「登录后可执行重建 / 增量更新等运维操作。」+「去登录」提示。判定来自页面传入的 `loggedIn={Boolean(user)}`，`user` 由启动时 `api.me()` 拉取。重建为高危动作，点击后先弹二次确认（「重建会清空现有知识库并重新灌入全部数据……」）。

> 后端约束（`main.py`）：`/api/ingest` 要求登录（`Depends(auth.current_user)`）；增量模式不允许同时 `recreate=true`（返回 400）；已有任务运行时返回 409。

## 4. 评测结果（EvalCard）

**数据来源**：**不是后端接口**，而是读取评测脚本产出的报告文件 `backend/eval/report.md`。
**对应接口**：Next.js 路由 `GET /api/eval-report`（`frontend/app/api/eval-report/route.ts`）。该路由用 `readFile` 尝试两个候选路径（`../backend/eval/report.md`、`backend/eval/report.md`），命中则返回 `{ available: true, markdown }`，否则 `{ available: false, markdown: "" }`。它是**纯本地文件读取，不依赖后端进程**。
**展示内容**：`EvalCard.tsx` 用 `fetch("/api/eval-report", { cache: "no-store" })` 取回 markdown，经 `mdToHtml` 渲染汇总表；文件缺失或读取失败时展示空态（「还没有评测报告……」，不影响后台其余部分）。

## 5. 数据表结构（SchemaCard）

**数据来源**：`schema`（`SchemaResponse`）。
**对应接口**：`GET /api/mysql/schema`。
**展示内容**：每张业务表一个可展开卡片（点击标题展开字段明细：`col.name` / `col.column_type` / `col.comment`，`col.key === "PRI"` 标主键），并列出 `schema.available_tables`（库内其他表，核对表名用）。`schema.connected` 为假时提示暂时无法读取。

## 6. 历史检索（HistorySearch）

**数据来源**：已登录用户 `user`（来自 `api.me()`）+ 用户输入的检索条件。
**对应接口**：`GET /api/history/search`（`lib/api.ts`：`searchHistory` → `authReq(\`/api/history/search${query(params)}\`)`，参数含 `q` / `from` / `to` / `mode`）。
**展示内容**：关键词 + 起始/结束日期 + 模式（ReAct / Plan-and-Solve / 深度思考）三维度检索，结果列表展示会话标题、模式、时间与 `snippet`（后端已转义并带 `<em>` 高亮标签，用 `dangerouslySetInnerHTML` 渲染）。**未登录时整卡只显示「登录后才能检索历史对话」**。

---

## 附：接口清单（`lib/api.ts` 出处）

| 用途 | 方法与路径 |
| --- | --- |
| 健康检查 / 系统状态 | `GET /api/health` |
| 知识库统计 | `GET /api/kb/stats` |
| MySQL 表结构 | `GET /api/mysql/schema` |
| 启动建库 | `POST /api/ingest` |
| 建库进度 | `GET /api/ingest/status` |
| 当前用户 | `GET /api/auth/me` |
| 历史检索 | `GET /api/history/search` |
| 评测报告（Next.js 路由） | `GET /api/eval-report` |

后端路由定义见 `backend/app/main.py`；本节所有路径均与 `frontend/lib/api.ts` 中的字符串及后端 `@app.get/post(...)` 装饰器一一对应。