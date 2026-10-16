# 技术决策记录（DECISIONS）

本文记录本项目里「**原计划 → 实际实现 → 为什么**」的关键取舍。每一条都对应代码里的真实常量、配置项或函数，引用位置可在仓库中直接检索核对；不写没有落地依据的设想。

涉及文件（相对 `backend/`）：

- `app/rerank.py`、`app/config.py` —— 重排参数
- `app/history.py` —— 对话历史的全文检索（FTS5）
- `sync_pg.py` / `copy_mysql.py` / `import_neo4j.py` —— 数据同步脚本
- `app/answer_cache.py` —— 问答结果缓存

---

## 1. 重排的 `max_tokens`：计划 200 → 实现 3072

- **计划**：重排这一步，模型理论上只需要输出一个候选序号数组（十几个数字而已），给 **200** 个 token 就够。
- **实现**：在 `backend/app/rerank.py` 定义模块常量 `_RERANK_MAX_TOKENS = 3072`，并在 `rerank()` 里通过 `complete_chat(..., max_tokens=_RERANK_MAX_TOKENS, timeout=s.rerank_timeout)` 传入。源码注释原文：「推理型模型（如 DeepSeek 的 v4-pro / flash）会先把预算花在『思维链』上，思考完才轮到输出正式答案」。
- **原因**：预算给小了会出现「`finish_reason=length`（被截断）且 `content` 为空」——排序结果压根拿不到，白白降级成 **RRF 原序**，重排功能等于白做。给足 3072 几乎无成本（模型答完会自行停止，不会硬凑字数）。

## 2. 重排超时：计划 20s → 实现 45s

- **计划**：重排是一次短调用，给 20 秒超时足够。
- **实现**：配置项 `RERANK_TIMEOUT` 默认值改为 45 —— `backend/app/config.py`：`rerank_timeout=_int("RERANK_TIMEOUT", 45)`；`rerank()` 用 `timeout=s.rerank_timeout` 生效。
- **原因**：源码注释原文「曾经设成 20 秒时，推理模型还没想完就被掐断，导致『**静默降级**』（表面上没报错，实际重排没生效），极难发现；所以宁可等久一点，也不要偷偷降级」。20s 会让用户**以为重排生效、其实没有**。

## 3. FTS5 预览片段：计划用 `snippet()` → 实现手工切片段 + 自插高亮

- **计划**：用 SQLite FTS5 自带的 `snippet()` 生成检索预览片段。
- **实现**：改为「FTS 只负责**找出命中的消息行**，预览在 Python 里从 `messages.content` 原文截取」，并用 `backend/app/history.py` 里的 `_build_preview(...)` 手工插入 `<em>` 高亮标记（`search_messages()` 返回结果里的 `snippet` 字段即由此生成）。
- **原因**：本项目用的是 FTS5 **外部内容表**（索引列只存分词结果 `messages.seg`），此时 `snippet()` 能读到的只有**分词后的 `seg`**（词与词之间带空格，如「招标 公告 市政」），展示很难看。改成从原文截取后，预览是自然语句且高亮可控。

## 4. 数据同步：计划「不做 ETL」→ 实现三个脚本

- **计划**：后端直连远程 MySQL 读业务数据，不做 ETL（数据抽取/转换/加载）。
- **实现**：落地为三个**互不冲突**的独立同步脚本，均为「只读源库 + 全量重建 + 失败隔离」：
  - `backend/copy_mysql.py` —— 远程 MySQL → **本机 MySQL**，目的「数据留底 / 离线可用」，表结构与源库一模一样（靠 `SHOW CREATE TABLE`）。
  - `backend/sync_pg.py` —— 远程 MySQL → **本机 PostgreSQL**，目的「结构化统计查询」（`count` / `sum` 等精确问题）。
  - `backend/import_neo4j.py` —— 远程 MySQL → **本机 Neo4j 知识图谱**，建「两类节点（`:Project` / `:Notice`）+ 一类关系（`:Project)-[:PUBLISHED]->(:Notice)`」，服务多跳关系类问题。
- **原因**：不同问题需要不同的存储形态——向量库擅长语义检索，统计类问题走 PG 更精确，关系/多跳问题只有图库能答；三者各管一摊，因此没有合并成一个通用 ETL，而是拆成三个目标明确的脚本。脚本都由**人工在命令行执行**，后端服务不会自动调用。

## 5. 问答缓存：计划语义匹配 → 实现「规范化精确匹配」

- **计划**：对问答结果做缓存，减少重复调用大模型的开销。
- **实现**：`backend/app/answer_cache.py` 采用**规范化精确匹配**：`normalize_question()` 依次做「NFKC 全角转半角 → 去首尾空白 → 转小写 → 连续空白压成一个空格」，结果直接作为缓存主键 `cache_key`。`is_cacheable()` 明确排除：未登录、`mode == "deep"`、带多轮历史、开启联网搜索、带图片解析、以及疑似敏感的提问。所有读写失败**一律静默降级**（当作没缓存）。
- **原因**：源码注释原文「模糊的语义匹配容易『张冠李戴』（把 A 问题的答案给了 B 问题），精确匹配虽然保守，但绝不会答错」——**命中错比不命中更致命**。
- **风险（必须写清）**：只有「问法几乎一模一样」的问题才会命中；**近义改写永远不命中**（例如「市政道路招标」与「城市道路工程招标」会被当成两个问题），因此缓存命中率低于语义方案，收益主要来自完全重复的提问。将来若要语义匹配，应**另建索引**，而不是改这里的精确匹配逻辑。

## 6. 中文全文检索：FTS5 默认分词器不切中文 → 自建分词列 `seg`

- **计划**：直接用 FTS5 默认的 `unicode61` 分词器做关键词检索。
- **实现**：`backend/app/history.py` 专门在 `messages` 表存一列 `seg`（jieba 分词结果，复用 `bm25.tokenize`），查询时同样先分词再 `MATCH`；写入词元由 `_index_terms()` 生成（含单字，保证单字查询也能命中）。
- **原因**：`unicode61` **不切中文词**，中文查询会失效；必须自建分词列并前后一致地分词，FTS5 才能对中文生效。

## 7. FTS5 不可用时：计划只依赖 FTS5 → 实现自动降级为 `LIKE`

- **计划**：关键词检索只走 FTS5。
- **实现**：`backend/app/history.py` 用 `fts_available()` 探测本机 SQLite 是否编译了 FTS5；`search_messages()` 里 `use_fts` 为假或 FTS 查询报错时，回落到 `_search_like()` 用 `LIKE` 做模糊匹配。
- **原因**：源码注释原文「LIKE 是全表扫描，速度不如 FTS，但保证『**搜索永远可用**』」。不同机器的 SQLite 编译差异不应让检索功能整体不可用，降级虽慢但不缺功能。

## 8. LLM 重排：计划「候选够就一律重排」→ 实现「按需触发」（RERANK_MODE=auto）

- **计划**：检索拿到候选后一律调大模型重排，把最相关的排到前面。
- **实现**：`backend/app/rerank.py` 新增纯函数 `should_rerank(hits, settings)`，`rerank_hits` 按 `RERANK_MODE`（`auto` 默认 / `always` 保持旧逻辑 / `off` 关闭）决定排不排；metrics 增记 `rerank_skipped`。触发条件最终只留两条——① 头两名 **dense 分数并列**（相对差 < `RERANK_AUTO_GAP`，默认 0.1）② 融合第一名 dense 分 < `RERANK_AUTO_MIN_DENSE`（默认 0.35）。最初实现的「RRF 分差过小」与「dense 第一 ≠ bm25 第一」两条被实测砍掉：13 个真实问题上分别 13/13、12/13 恒触发（RRF 分是名次倒数之和、天然密集；两路召回集本就不同），留着等于 auto 永远重排。改后触发率 13 题里 4 题，恰好全是本集有标注、重排有收益的原文题。
- **原因**：LLM 重排单次 8.7~13.1 秒，约一半请求在排队重排，实测首字延迟被拖到 6.9 秒、总耗时 16.6 秒。改为按需触发后，13 题首字中位数 **19.4 秒 → 0.7 秒**（详见 `backend/eval/report.md` 表 5）。代价如实记：`auto` 的 MRR 0.800 低于 `always` 的 1.000，差额全部来自 Q1——同一题两次重排结果相反（一次提到第 1、一次排到第 5），LLM 重排输出本身有随机性，不是「该排没排」；recall@5 无下降。

## 9. 问答缓存 key：计划「只按规范化问题」→ 实现「问题 + mode + top_k」

- **计划**：缓存主键只用规范化后的问题文本（第 5 条的精确匹配方案）。
- **实现**：`answer_cache` 的 `cache_key` 在问题之外，追加 **`mode`（react / plan 等）与影响答案的检索参数（如 `top_k`）**；写入侧 `set_cached` 与读取侧 `get_cached` 共用同一套 key 构造逻辑。老 key 不迁移、自然过期失效。同时修掉一处 fail-open：`created_at` 解析失败时原实现跳过 TTL 检查当命中返回，现改为**按未命中返回并记 warning**（与「读失败一律当没缓存」的既定原则对齐）。
- **原因**：同一问题在不同 `mode` 下走的是不同链路、答案不同（react 与 plan 互相命中等于**串答案**——命中错比不命中更致命，与第 5 条同一价值观）；`top_k` 变化会改变进入答案的资料，也必须分键。fail-open 反过来让「解析不了的脏行」被当成长期有效缓存，与「失败即当没缓存」相悖。

## 10. 建库接口：计划沿用「默认 recreate=true」→ 实现 mode/recreate 互斥校验

- **计划（原状）**：`IngestRequest.recreate` 默认 `true`，前端靠 `recreate: mode==="full"` 手动规避。
- **实现**：`recreate` 默认改为 `false`，接口层加两条互斥校验（不满足即 400）——① `mode=incremental` + `recreate=true` →「增量不能重建集合」（先清空再增量自相矛盾）；② `mode=full` + `recreate=false` →「全量必须重建集合，否则已删除数据不会清理」（全量管道只灌不删，差集是增量专属，`recreate=false` 会让源表已删记录变成僵尸点永久留在库里被检索）。互斥表写进 README「建知识库」与 `docs/FEATURES-admin-console.md`。
- **原因**：默认值与增量模式天然互斥（只传 `mode="incremental"` 必撞 400），是个配置陷阱；而「全量 + 不重建」看似合法、实则产生**静默的数据残留**，比报错危险得多。改差集删除属于动建库管道核心逻辑，本轮明确不做，用 400 + 文档把规则立住。

## 11. 图片接口状态码：计划统一 400 → 实现 400 / 413 / 415 分级 + 文件头嗅探

- **计划（原状）**：类型不支持、体积超限统统 400；伪装成 `.png` 的 PDF 能过掉校验、直到调视觉模型才报错（表现为 502）。
- **实现**：`vision.py` 新增 `sniff_mime()` / `verify_content()` 按**文件头（magic bytes）**核对真实格式，异常分三级——识别不出 → `ImageRejected`（400）；真实格式非 jpg/png/webp 或与声称不符 → `ImageUnsupported`（415）；超 `VISION_MAX_BYTES` → `ImageTooLarge`（413）；502 保留给上游模型故障。两个新异常是 `ImageRejected` 的子类，接口里 `except` 先子类后父类。同批把 `/api/retrieve`、`/api/kb/samples` 的 `VectorStoreError` 统一包成 503（与 `/api/kb/stats` 同口径），web_search 工具补上兜底 `except Exception`。
- **原因**：后缀和 `content_type` 都能伪造，只看声明类型等于把校验建在攻击者可控的字段上；按 RFC 区分「东西不对（400）/ 东西太大（413）/ 格式不支持（415）/ 上游故障（502）」，前端才能给出对症提示，排障也不用猜。

## 12. Neo4j 只读：计划「代码审查保证」→ 实现驱动级 READ 访问模式 + 查询超时

- **计划（原状）**：只读保护只覆盖 `_read_rows` 一条路径；`_introspect_rows` 走 Neo4j 驱动默认的 WRITE 访问模式，安全性纯靠人工审查；`NEO4J_TIMEOUT` 注释写「查询超时」、实际只管连接与重试。
- **实现**：建 driver 时指定 `default_access_mode=READ`（内省与查询都落在只读会话上，只读成为**驱动级默认**而非约定）；补真正的单条查询超时（事务超时 `NEO4J_QUERY_TIMEOUT`，已入 `.env.example`），`NEO4J_TIMEOUT` 的注释改回「连接超时与重试」；driver 增加应用关闭钩子（此前只建不关）。
- **原因**：「约定的只读」在新增一条查询路径时就会失守，「配置的只读」不会——漏写代码还有驱动兜底。查询超时缺失意味着跑飞的变长路径（如无限深度的展开）只能靠人 Ctrl+C，服务端到点终止才叫限制。

## 13. 追问复用：计划「每轮都完整检索+规划」→ 实现规则判追问 + 复用上一问证据

- **计划（原状）**：多轮追问（「那第二条呢」「它的金额是多少」）与首发问题一样走完整链路（检索 → 规划 → 生成），慢且浪费。
- **实现**：`backend/app/answer_cache.py` 新增纯函数 `is_followup(question)`——**规则版、不引模型**：问题 ≤ 15 字、含指代/延续词（那/它/这个/上面/刚才/第 N 条/呢）、且不含新实体（项目编号、公司名、法规名等）；三条不全中就返回 False。新增 `get_last_evidence(conversation_id)` 从问答缓存表回捞该会话最近一次带来源的回答（表本就有 `sources_json`，新增 `conversation_id` 列并自动补老库）。`main.py` 的 `/api/agent/chat` 在缓存未命中后尝试复用：命中则跳过检索与规划、仍调大模型作答，事件标 `reused=true`。复用前两道失效校验——`kb_version` 与上一问不同、或来源块已不在库（`vector_store.missing_doc_ids`）即放弃复用走完整链路；metrics 新增 `followup_reused` 计数。
- **原因**：追问的价值在「换个角度问同一批证据」，重新检索既慢又可能召回不同的块导致上下文漂移。但**失败必须退回原链路，绝不猜**——判错追问（把新问题当追问）会拿旧证据答新问题，比慢更致命，所以判定是保守的三条件合取，拿不准一律 False。

## 14. deep 预算：计划「单一 over_budget 粗判据」→ 实现工具/检索/LLM/时间四本账

- **计划（原状）**：`deep_agent` 只有一个 `over_budget()` 粗判据，超限行为不细分。
- **实现**：拆成四项独立计数与上限——`DEEP_TOOL_CALL_LIMIT`（工具调用 12）、`DEEP_RETRIEVAL_LIMIT`（检索 8）、`DEEP_LLM_CALL_LIMIT`（大模型 10）、时间账复用 `DEEP_TIMEOUT`（120s），均 ≤0 表示不限。任何一本先超限即**立即收敛到已有证据作答**（本步收手、跳过后续步骤、跳过综合生成；不抛异常、不空手结束）。`done` 事件 `meta` 新增 `budget` 字段（四项实际消耗 + 上限 + 超限项），metrics 新增 `qa_budget_metrics` 表记每次问答的平均消耗。
- **原因**：四类消耗的增长速度完全不同——工具调用便宜但次数最容易膨胀，LLM 调用单次最贵，时间是用户可感知的硬约束。合成一个判据会出现「时间还没到但 LLM 已烧了 15 次」或反过来的漏判；分账后任一维先触顶都能止损，且 `meta.budget` 让超限项在前端/评测里可见，而不是静默截断。

## 15. 账号停用：计划「无禁用机制」→ 实现 status 列 + 403 强制 + 注册不提权

- **计划（原状）**：`users` 表没有状态列，注册接口信请求体字段。
- **实现**：`history.users` 补 `status`（active/disabled）与 `role`（user/admin）两列，`ensure_schema` 里做 ALTER TABLE 兜底（老库自动补列，默认 active/user）。登录与所有鉴权接口：口令校验通过后再看状态，`disabled` 一律 403「账号已停用」（先验口令再查状态，不向未认证者泄露账号是否存在）；`optional_user` 把停用令牌按匿名处理，匿名问答不受影响。注册：`AuthRequest` 设 `extra="ignore"` 丢弃请求体里任何多余字段，`create_user` 硬编码 `status='active'`、`role='user'`——伪造 role/status/is_admin 到不了库里。后台新增 `/api/admin/users` 与 `/api/admin/users/{id}/status`（仅管理员，`AUTH_ADMIN_USERS` 白名单等价 `role=admin`），不能停用自己。
- **原因**：停用是账号体系的最低配治理能力（离职/滥用账号要能立刻止血），没有它只能删库；而注册接口曾信任请求体字段，是教科书级的提权入口——**权限只能来自服务端**，请求体一律忽略比逐字段校验更不容易漏。先验口令后查状态是防账号枚举的常规取舍：403 文案明确但只对持正确口令者可见。

## 16. 启动告警：计划「配错才知情」→ 实现模板默认值检查 + WARNING + health 字段

- **计划（原状）**：`.env` 留着 `.env.example` 的示例值照常启动，没有任何提示。
- **实现**：`config.template_default_keys()` 对照 `.env.example` 检查 `LLM_API_KEY` / `PG_PASSWORD` / `NEO4J_PASSWORD` / `TAVILY_API_KEY` / `ZHIPUAI_API_KEY` 是否仍等于模板值（含空值——没改过模板即命中；example 可注入便于测试）。`main` 在 `setup_logging` 之后执行，命中即 `logger.warning` 列出键名；**绝不拒绝启动**（本地演示优先）。`/api/health` 新增布尔字段 `template_default_risk`（同源判定，只新增不改已有字段），后台状态卡命中时展示醒目 `notice-warn`。
- **原因**：示例密钥照常启动是典型的「本地能跑、上线即事故」隐患，但直接拒绝启动会把本地演示和离线测试一起挡在门外——告警的正确形态是**吵但不拦**。判定放进 health 是为了让风险可轮询、可被前端展示，而不是只存在于一次性的启动日志里。
