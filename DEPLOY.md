# 部署指南

面向把本项目迁到部署服务器的场景。只讲「怎么搬、怎么重建」，业务功能见 README。

---

## 0. 关键前提（先读这段）

**部署服务器无法访问 192.168.10.120**——那是本项目的内网原始数据源（MySQL），只在开发机所在内网可达。
所以在服务器上：

- ❌ 不能重跑「从原始库同步数据」的管道，原始库连不上；
- ✅ 业务数据以**本机 PostgreSQL 的 `chatbot` 库**为准，用 dump/restore 文件搬运；
- ✅ 知识库（Qdrant 向量）**在新环境重建**（`python -m app.ingest`），**不要拷贝 `data\qdrant` 目录**到不同版本的环境——向量库文件与 Qdrant 版本、嵌入模型、集合结构强绑定，跨环境直接拷贝轻则起不来，重则检索结果错乱。

数据流概括：

```
[开发机] 本机 PostgreSQL chatbot 库
    │  scripts/dump_pg.ps1  →  data/backup/chatbot-<日期>.sql
    ▼  （拷贝 .sql 文件到服务器）
[服务器] psql 导入（scripts/restore_pg.ps1）
    │  python -m app.ingest  （从 PG 读业务数据 → 重建向量/关键词索引）
    ▼
[服务器] Qdrant 知识库在新环境全新生成
```

---

## 1. 数据搬家：三步

### 第 1 步（开发机）：导出

在**仓库根目录**（读取根目录 `.env` 的 `PG_*` 连接参数）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dump_pg.ps1
```

产出 `data\backup\chatbot-<yyyy-MM-dd>.sql`，并当场做行数校验：

```
导出 chatbot @ 127.0.0.1:5432 → ...\data\backup\chatbot-2026-10-11.sql
导出完成：...chatbot-2026-10-11.sql（7.18 MB）
行数校验（期望各 933 行）：
  [OK] procurement_notices = 933
  [OK] projects = 933
全部通过：把 ...chatbot-2026-10-11.sql 拷到目标机，用 scripts\restore_pg.ps1 导入。
```

校验不通过（行数 ≠ 933）时脚本以非零退出，**不要**把这种 dump 带去服务器。

### 第 2 步：拷贝 .sql 到服务器

把 `chatbot-<日期>.sql` 拷到服务器上仓库的 `data\backup\` 目录（放别处也行，第 3 步用 `-BackupFile` 指定路径即可）。

### 第 3 步（服务器）：导入 + 再次校验

前置条件：

1. 服务器已安装 PostgreSQL，服务已启动；
2. 服务器仓库根目录 `.env` 里 `PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DATABASE` 按服务器实际值配好（**不要**沿用开发机的密码习惯，按服务器的来）。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\restore_pg.ps1
# 或显式指定文件：
powershell -ExecutionPolicy Bypass -File scripts\restore_pg.ps1 -BackupFile D:\tmp\chatbot-2026-10-11.sql
```

脚本行为：

- 目标库不存在 → `createdb` 后导入；
- 目标库存在但为空 → 直接导入；
- 目标库**已有业务表 → 拒绝导入并退出**（dump 未带 `--clean`，覆盖必然表冲突；是否删库交给人决定，脚本不会替你 `DROP`）；
- 导入完成后再次校验 `procurement_notices` / `projects` 行数，两边对上才算搬成功。

> Linux 服务器上没有 PowerShell 时，等效手工步骤：
> `pg_dump --no-owner --no-privileges -f dump.sql`（开发机）→ 拷贝 →
> `psql -d chatbot -v ON_ERROR_STOP=1 -f dump.sql`（服务器）→
> `SELECT COUNT(*) FROM procurement_notices, projects;` 各应为 933。

## 2. 重建知识库（导入后必做）

```powershell
cd backend
python -m app.ingest
```

`app.ingest` 从 PostgreSQL 读业务数据，重新切块、重新算向量、重建关键词索引——**这是新环境拿到可用知识库的唯一正确方式**。

再次强调：**不要拷贝 `data\qdrant` 到不同版本环境**。它只在「同一 Qdrant 版本 + 同一嵌入模型 + 同一套集合结构」下才可移植，版本稍有差异就会加载失败或静默检索错乱；重建成本只有几分钟，拷贝省下的时间不值得冒这个险。

### 知识图谱（Neo4j / Aura）数据重建（可选，供 Agent 关系查询）

向量库与 PG 之外，关系查询走 Neo4j。图数据与它们同源，都是业务两张表 `projects` / `procurement_notices`
（各 933 行，以 `project_name` 关联），建成 `(:Project)-[:PUBLISHED]->(:Notice)`。

```powershell
cd backend
# 缺省跟随 KB_SOURCE；从本机 PostgreSQL 建图（幂等，可重复执行）
python import_neo4j.py --source pg --batch 500
# 从原远程 MySQL 建图（旧行为，--source mysql 完全不变）
python import_neo4j.py --source mysql
```

成功判据：打印 Project / Notice / PUBLISHED 三个数字（**933 / 933 / 933**，关系数 < 900 以非 0 退出码报错）；
连跑两遍三个数字不变即幂等。

Aura 云服务注意事项：

- `.env` 的 `NEO4J_URI` 必须 `neo4j+s://`（TLS），**用户名是实例 ID 而不是 `neo4j`**，库名通常也是
  实例 ID（`SHOW DATABASES` 可查）；本地 Neo4j 则用 `bolt://127.0.0.1:7687`，二选一见 `.env.example` 注释；
- 免费层**闲置 3 天自动暂停**，需去 console.neo4j.io 点 Resume 后才能建图/查询；建图与查询都要走网络出口；
- 库名配错（如删建实例后实例 ID 变了）时后端会回退默认库并打 WARNING，图谱工具不整体挂掉，但应尽快改对 `.env`；
- 建图脚本复用 `app/pg_source.py` 只读连接（`default_transaction_read_only=on`），对源库只 SELECT 不写。

### Neo4j 连接池与超时（什么时候才需要调）

现状：**全部用 driver 默认值**——连接池 100、连接寿命 3600 秒、建连超时与事务重试超时都取
`NEO4J_TIMEOUT`（默认 30 秒，见 `backend/app/graph.py`）。

- **单用户/低并发场景不要动这些值**：driver 默认值本身就是为这个量级设计的，多配一个旋钮只会多一处要与代码保持一致的地方；
- 只有**并发查询数接近或超过 100** 时才需要显式设 `max_connection_pool_size`（pool 必须 ≥ 并发数）。
  而且它的症状是**"偶发变慢"而不是报错**（查询在排队等连接），比报错难查得多——真上并发前先把这条写进监控；
- Aura 会回收长连接，`max_connection_lifetime` 保持默认即可，driver 会自动重连；
  **不要为了"省连接"把它调得很小**，否则每次查询都要重建 TCP+TLS，反而更慢。

## 3. 启动顺序（服务器）

1. Qdrant（向量库）；
2. 后端：`cd backend && python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
   （公网只开 Nginx 443，后端保持只监听 127.0.0.1）；
3. 前端：`npm run build && npm start`（或 Nginx 托管 `out/` 静态产物）。

健康检查：`GET /api/health` 保持公开（探活用），`GET /api/metrics` 已需登录。

## 4. 前端同源部署（Nginx 反代）

前端默认 API 地址在 `frontend/.env.local` / 构建环境变量 `NEXT_PUBLIC_API_BASE` 控制：

- **部署（推荐，同源）**：把该变量**显式留空**（`NEXT_PUBLIC_API_BASE=`，保留行、不填值），
  请求走相对路径 `/api/*`，由 Nginx 反代到 `127.0.0.1:8000`，天然无跨域；
- **本地开发**：填 `http://127.0.0.1:8000`（或整行删除——未设置时自动回落到这个默认值）。

Nginx 侧把 `/api/` 转给后端即可（SSE 流式响应需要 `proxy_buffering off`）。

## 5. 已知限制（如实记录）

- **限流是内存实现**（`backend/app/ratelimit.py`）：令牌桶只在当前进程内，单进程部署（uvicorn 不加 `--workers`）完全有效；多进程部署每进程一套桶，实际放行量按进程数翻倍，限不住总量。要多进程限流需换 Redis 共享状态，本项目尚未实现。
- **日志轮转已开**（每天午夜轮转、保留 7 份），但 `data\backup\` 下的 dump 文件不会自动清理，旧的自己定期删。
- **向量库支持双模式**（`backend/app/vector_store.py`）：`QDRANT_URL` 留空＝本地嵌入式（默认：离线可演示、数据不出本机、零运维）；非空＝服务端模式（容器或云端，解除"同一目录只能一个进程占用"的限制，也是多进程部署的前提）。**切换模式不会自动搬数据**：换库后必须重建知识库（`cd backend && python -m app.ingest`）或用 Qdrant 快照迁移，否则 `/api/health` 的 `points` 会是 0。`/api/health` 的 `qdrant_mode` 字段可直接看出当前是哪种模式。
- **本机 Qdrant 目录单进程独占**：本地模式下命令行建库与在线服务不能同时跑，建库前先 `scripts\stop.bat`；换服务端模式后此限制不再存在。
