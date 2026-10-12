# 功能说明：联网搜索与图片解析

本文覆盖两个「按需启用」的增强能力：**联网搜索（Tavily）** 与 **图片解析（智谱视觉模型）**。两者都属于「没有配置就完全不参与、不破坏原有问答流程」的旁路能力。

涉及文件（相对 `backend/` 与 `frontend/`）：

- `backend/app/web_search.py` —— 联网搜索工具本体
- `backend/app/vision.py` —— 图片解析核心逻辑
- `backend/app/config.py` —— 配置项与 `web_search_configured` / `vision_configured` 判定
- `backend/app/main.py` —— `/analyze-image` 接口、`/api/agent/chat` 里联网开关的合成、`/api/health` 回显
- `frontend/components/chat/Composer.tsx` —— 联网开关与附件按钮
- `frontend/app/page.tsx` —— 开关状态与图片解析调用
- `frontend/lib/api.ts` —— `analyzeImage()` 请求

---

## 一、联网搜索（Tavily）

### 用途

本地知识库只装 MySQL 三张表里的**存量**数据，回答不了「最新」的东西（昨天刚发的招标公告、刚修订的法规）。当本地检索一无所得、而问题又涉及近期公告 / 政策 / 法规更新时，由 Agent 调用本工具去公开互联网补充，结果**带上 Tavily 的相关性评分（0~1）**交回大模型参考作答。工具名 `TOOL_NAME = "search_web_bidding"`（`web_search.py`）。

### 开关

**前端开关**：`frontend/components/chat/Composer.tsx` 的「联网搜索」chip 按钮，受 `webSearchOn` / `canWebSearch` 控制，`onToggleWebSearch` 回调由 `page.tsx` 持有。开关状态由 `page.tsx` 计算：

```ts
const canWebSearch = Boolean(health?.web_search_configured);          // 后端是否配了 Key
const webSearchOn  = canWebSearch && (webSearch ?? health?.web_search_default ?? false);
```

未配置时按钮 `disabled` 且提示「当前未开启联网搜索能力」；开启后提示「本地知识库查不到时会自动联网搜索最新公告与法规更新」。

**后端配置与注册**：

- `backend/app/config.py`：开关默认值 `web_search_default = _bool("WEB_SEARCH_DEFAULT", True)`（默认开）；Key 来自 `TAVILY_API_KEY`，其余参数 `tavily_search_depth`（默认 `basic`）、`tavily_max_results`（默认 5）、`tavily_timeout`（默认 20）。
- `web_search_configured` 属性：`return bool(self.tavily_api_key)` —— **只看有没有配 Key**。
- **工具注册**（`main.py`）：只有配了 Key 才把 `WEB_SEARCH_TOOL_NAME` 追加进 Agent 工具清单：
  ```python
  "tools": [TOOL_NAME]
           + ([PG_TOOL_NAME] if s.pg_configured else [])
           + ([GRAPH_TOOL_NAME] if s.graph_configured else [])
           + ([WEB_SEARCH_TOOL_NAME] if s.web_search_configured else []),
  ```
- **请求侧合成**（`main.py`，`/api/agent/chat`）：未显式传 `web_search` 时用配置默认值，再叠加「后端是否配了 Key」：
  ```python
  use_web = s.web_search_default if payload.web_search is None else bool(payload.web_search)
  use_web = bool(use_web and s.web_search_configured)
  ```

> 设计要点：**`web_search=true` 但后端没配 Key 时，工具干脆不注册**，从而「维持改动前行为」，不让用户以为开了联网却搜不到。

### 降级行为

- **未配置 Key**：`search_web_bidding()` 检测 `s.web_search_configured` 为假时，记下 `state["error"]`、清空 `state["results"]`，返回「联网搜索不可用：后端未配置 TAVILY_API_KEY。请如实告知用户，不要编造搜索结果。」
- **搜索失败**（未装 SDK / 网络异常 / 超时）：捕获 `WebSearchError` 后**不让整轮问答失败**——记下错误、清空结果，返回「请如实告知用户联网搜索未成功，不要编造任何搜索结果」，模型仍能基于已有本地信息继续作答。
- **一切意外异常**：工具承诺「绝不把异常抛回 Agent」（与 `pg.py` / `graph.py` 同一口径）。`WebSearchError` 之外的异常（SDK 返回结构异常、状态字典缺 `settings` 等）被外层 `except Exception` 兜住，转成「联网搜索失败：……」的可读提示，问答继续。
- **搜索无结果**：`render_results()` 在结果为空时同样明确提示「没有返回任何结果……不要编造任何公告标题、链接或日期」。
- 无论成功失败，本工具都被 `asyncio.to_thread(_search, ...)` 包起来，避免阻塞事件循环。

### 错误码语义

- 本模块自身抛出的 `WebSearchError(RuntimeError)` 不直接对外暴露 HTTP 状态码，而是被**收敛成给模型看的文字提示**（见上）。它的人话原因覆盖：未安装 Tavily SDK、未配置 Key、联网搜索失败（含 SDK 各类异常）；`WebSearchError` 之外的意外异常同样被兜住并转成「联网搜索失败：……」提示（`tests/test_web_search_fallback.py` 覆盖）。
- 搜索结果若成功，回填 `state`：`{"query": ..., "results": [...], "error": None}`；前端据 `state` 渲染「联网搜索」事件与相关性评分。
- **缓存联动**：凡是「会联网」的请求都不进问答缓存（`answer_cache.is_cacheable(..., web_search=use_web, ...)` 为假），因为联网结果有时效性。

---

## 二、图片解析（智谱视觉模型）

### 用途

用户在前端点附件按钮（📎）上传一张图片（招标公告截图 / 扫描件），后端交给**智谱视觉大模型**（`vision_model`，默认 `glm-4.6v-flashx`）识别成文字，前端把识别结果放进卡片，用户点一下即可填进提问框。核心逻辑在 `vision.py` 的 `analyze_image()`，对应接口为 `POST /analyze-image`（`main.py`）。默认解析指令 `DEFAULT_PROMPT`：「请识别这张图片的全部内容，包括文字、表格和版式，尽量完整输出」；若用户附带提问，会在指令后追加「用户附带的提问是：……」请模型一并留意。

### 开关

**前端**：`frontend/components/chat/Composer.tsx` 附件按钮（📎），`accept={IMAGE_ACCEPT}` 限定 `.jpg/.jpeg/.png/.webp`（与后端校验一致）；`page.tsx` 用 `analyzeImage(file, question)` 调用（`lib/api.ts` 的 `analyzeImage()` POST `/analyze-image`，走 `authFetch` 自动带 Bearer 令牌）。**未上传图片时本模块完全不参与。**

**后端配置与判定**：

- `backend/app/config.py`：`vision_model`（默认 `glm-4.6v-flashx`）、`vision_timeout`（默认 60）、`vision_max_bytes`（默认 `10 * 1024 * 1024`，即 10MB）；Key 来自 `ZHIPUAI_API_KEY`。
- `vision_configured` 属性：`return bool(self.zhipuai_api_key)`。
- 无独立「前端开关」——**是否启用取决于用户是否上传了图片 + 后端是否配了 Key**。

### 降级行为

- **未上传图片**：整个流程不经过本模块，对原有问答零影响。
- **未配置 Key**：`analyze_image()` 检测 `s.vision_configured` 为假时抛 `VisionError`，接口返回 **502**（提示「未配置智谱 API Key（ZHIPUAI_API_KEY），无法解析图片」）。
- **未装 SDK**：延迟导入 `zai` 失败时抛 `VisionError`（同样返回 502，而非 500）。
- **前端容错**：`page.tsx` 捕获解析异常后，给助手消息标记 `error`、提示「图片解析失败，请换一张图片重试。」，原始信息折叠在「技术详情」里；若为「停止解析」中断则不算错误。

### 错误码语义

`main.py` 的 `/analyze-image` 接口按四类自定义异常区分状态码（体积上限见配置 `VISION_MAX_BYTES`，默认 10MB）：

| 状态码 | 触发条件 | 对应异常 | 语义 |
| --- | --- | --- | --- |
| **401** | 未登录 | 依赖 `auth.current_user` | 图片解析消耗付费模型额度，要求先登录 |
| **400** | 文件头识别不出任何已知格式 / 内容为空 / 声称的类型（后缀、content-type）不支持 | `ImageRejected(ValueError)` | 「你（调用方）给的数据有问题」 |
| **413** | 体积超过 `vision_max_bytes` | `ImageTooLarge(ImageRejected)` | 「东西太大」而非「东西不对」 |
| **415** | 文件头能识别但真实格式不是 jpg/png/webp（如改名成 `.png` 的 PDF），或与声称的类型不一致 | `ImageUnsupported(ImageRejected)` | 「格式不支持 / 与声称不符」 |
| **502** | 未配 Key / 模型报错 / 超时 / 返回空内容 | `VisionError(RuntimeError)` | 「我们这一侧上游（模型服务）出问题」 |

> `ImageTooLarge` / `ImageUnsupported` 是 `ImageRejected` 的**子类**，接口里的 `except` 必须先捕获子类再捕获父类，否则 413/415 会被 400 抢先匹配。

关键校验函数：

- `resolve_mime(filename, content_type)`：先看文件名后缀，其次看浏览器上报的 `content-type`；都不支持时抛 `ImageRejected`（只支持 jpg / png / webp）。
- `check_size(size, settings)`：`size <= 0` 抛 `ImageRejected`（400）；`size > settings.vision_max_bytes` 抛 `ImageTooLarge`（413，提示最大支持的 MB 数）。
- `sniff_mime(data)` / `verify_content(data, declared_mime)`：**文件头（magic bytes）嗅探**。后缀和 content-type 都能伪造——把 PDF 改名成 `.png` 曾能过掉前两道校验、直到调视觉模型才报错（表现为莫名 502）。现在先看文件头：识别不出 → 400；识别出但不是 jpg/png/webp、或与声称类型不一致 → 415；通过则返回真实 MIME。
- 成功返回体：`{"text": 识别文本, "model": 使用的模型名, "filename", "mime", "size"}`。

> 与项目统一约定：**API key 问题 / 模型失败统一返回 502**（而不是 500），把「服务侧的锅」与「用户图片不行的 4xx」明确区分开；4xx 内部再按 400/413/415 分工，让前端能给出对症的提示。