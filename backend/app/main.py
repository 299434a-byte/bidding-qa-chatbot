# ============================================================================ #
# 本文件是干什么的
# ---------------------------------------------------------------------------- #
# 这是整个后端的「大门」：前端/浏览器发的每一个 HTTP 请求都先到这里，由这里决定
# 交给哪个函数处理，最后按约定格式（多为 JSON，问答类接口是 SSE 事件流）返回结果。
# 可以把它想成餐厅的「前台」：客人（前端）说需求（请求），前台把单子转给后厨
# （retriever / rag / agent / history 等模块），再把做好的菜端回去。
#
# 上游谁调用它
# ---------------------------------------------------------------------------- #
# 1) 启动服务时：命令行执行
#    `..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`，
#    uvicorn 是个 Web 服务器，它会导入本文件的 `app` 对象并持续监听 8000 端口。
# 2) 前端 Next.js 页面（localhost:3000）用 fetch 调下面这些 /api/... 接口。
# 3) 本文件最底部 `if __name__ == "__main__":` 也允许直接 `python -m app.main` 启动。
#
# 它下游用到谁（本文件自己不干重活，只做「转发 + 组装」）
# ---------------------------------------------------------------------------- #
#   config.py      读 .env 配置（数据库/地址/口令/模型名等都在这汇总）
#   db.py          MySQL 连接与「三张表结构自检」
#   pg.py          PostgreSQL 只读查询工具（Agent 的 SQL 能力）
#   graph.py       Neo4j 图谱只读查询工具（Agent 的 Cypher 能力）
#   retriever.py   混合检索（向量 + BM25 关键词）
#   rerank.py      大模型二次排序
#   rag.py         纯 RAG 问答事件流（/api/chat 用它）
#   agent.py       Agent 问答事件流（/api/agent/chat 用它，含 react/plan/deep 三模式）
#   deep_agent.py  plan / deep 两种模式的实现细节
#   vision.py      图片解析（智谱视觉模型）
#   web_search.py  联网搜索（Tavily）
#   auth.py        账号注册 / 登录 / 令牌校验
#   history.py     对话历史落库与检索（SQLite）
#
# 概念预习（用大白话解释本文件反复出现的词，后面不再展开）
# ---------------------------------------------------------------------------- #
# 【FastAPI 应用】一个 Python Web 框架对象。用 `@app.get("/xxx")` 这种写法
#   把一个普通函数注册成「访问 /xxx 时执行的函数」。
# 【装饰器】写在函数上一行、以 @ 开头的标记。作用是在不改函数内部代码的前提下，
#   给函数「挂」上额外功能。这里 `@app.get(...)` 就是把函数挂到某个网址上。
# 【SSE（Server-Sent Events）】服务器持续往前端「推送」小段文本的技术。
#   好处是「边生成边发送」：大模型吐一个字，前端立刻显示一个字，不用等整段生成完。
#   数据格式形如 `data: {"type":"delta","text":"你"}`，每块以 `data: ` 开头、
#   以两个换行结尾；最后发一个 `data: [DONE]` 表示结束。
# 【async / await（异步）】一种「等待时不占着线程干等」的写法。比如等数据库返回时，
#   异步写法会先把 CPU 让给别人用，结果到了再回来继续，人多时也不容易卡死。
#   注意：`async def` 定义的函数，必须用 `await` 调用才会真正执行。
# 【asyncio.to_thread】把「会卡住的同步函数」（数据库驱动、第三方 SDK）丢到后台线程里跑，
#   避免它把异步主循环堵住。本文件大量使用它。
# 【contextmanager + with】`@contextmanager` 能把「带 yield 的函数」变成可用
#   `with xxx():` 的资源管理器，常用于统一做「底层出错就翻译成 HTTP 错误」。
# 【Pydantic 模型（BaseModel）】声明「请求体长什么样」的类。FastAPI 会据此自动校验
#   前端传来的 JSON：字段缺失或类型不对会自动报 422，省掉手写一堆判断。
#   `Field(...)` 用来给字段加约束和说明文字。
# 【HTTPException】抛出它就会直接变成 HTTP 错误响应（如 400 / 401 / 404 / 503）。
# 【Depends（依赖注入）】`Depends(auth.current_user)` 表示「先调用 auth.current_user
#   校验身份，再把结果当参数传进来」；未登录会被自动拦截，不用每个接口手写鉴权。
#
# 硬约束与实测结论（改代码前必读，都是踩过坑换来的）
# ---------------------------------------------------------------------------- #
# · 【不可改动】/api/chat 的行为必须保持原样 —— 它是「纯 RAG」基线接口，
#   改动会影响历史对比与既有前端逻辑。新功能一律加到 /api/agent/chat。
# · 数据库 / 大模型 / Neo4j / PostgreSQL / 智谱 / Tavily 的所有连接信息与口令，
#   一律从 .env 读取（经 config.py 汇总），本文件绝不写死任何地址或密钥。
# · 未配置 PostgreSQL / Neo4j 时，对应查询工具不会注册，Agent 也不会调用；
#   本文件的 /api/pg/schema、/api/graph/schema 会返回「未配置」而不是报错。
# · 允许知识库为空时照常运行：检索命中 0 条时，回答如实说明「暂无数据」，
#   并给出「无参考来源」提示，绝不编造内容。
# · 重排参数是踩坑换来的：`rerank_timeout=45` 秒、`rerank_max_tokens=3072`。
#   早期 20 秒超时会导致「悄悄降级」（时间不够就悄悄退回未重排的顺序）；
#   早期 200~1024 tokens 会导致输出被截断（finish_reason=length）且内容为空。
# · Agent 的 `agent_max_steps=6`：子任务最多 6 步。早期设 3 步会因多次调用工具
#   触发 `Recursion limit`（递归次数上限）而报错。
# · 落库（写对话历史）绝不能影响问答：下面所有写库动作都包在 try/except 里，
#   写失败就当没写，问答流照常进行。
# · 本地向量库（Qdrant 嵌入式模式）同一时刻只允许一个进程打开，因此「重建知识库」
#   用「后台线程 + 进程内状态」实现，并用锁串行化（见下面的 _ingest_worker）。
#
# 阅读顺序建议
# ---------------------------------------------------------------------------- #
# ① 先看「请求模型」（ChatRequest 等）—— 它决定每个接口收什么参数。
# ② 再看 _EventCollector —— 它解释了「为什么问答要边转发边汇总」。
# ③ 然后按「基础信息 → MySQL/PG/图谱结构 → 账号 → 历史 → 图片 → 知识库 →
#    问答(SSE) → 建库」的顺序读接口，逻辑最顺。
# ④ 最后看底部 `if __name__ == "__main__"` —— 怎么自己启动服务。
# ============================================================================ #

"""FastAPI 服务入口。

接口一览：
    GET  /api/health            健康检查（含向量库是否就绪、大模型是否已配置）
    GET  /api/config            当前生效的配置（不含任何口令）
    GET  /api/mysql/schema      三张表的真实结构 + 行数 + 库内全部表名
    GET  /api/pg/schema         PostgreSQL 的库结构（供 Agent 生成 SQL 用，只读自检）
    GET  /api/graph/schema      Neo4j 图谱结构（供 Agent 生成 Cypher 用，只读自检）
    POST /analyze-image         图片解析（multipart 上传，智谱视觉模型识别成文本）
    GET  /api/kb/stats          知识库统计（总块数、按表分布）
    GET  /api/kb/samples        知识库样本
    GET  /api/retrieve?q=xxx    只做混合检索（默认不调大模型；带 &rerank=true 则额外重排）
    POST /api/chat              问答（SSE 流式，先推参考来源再推回答）
    POST /api/agent/chat        Agent 问答（SSE 流式，三种模式：react / plan / deep；带 web_search 开关）
    POST /api/ingest            重建知识库（后台任务）
    GET  /api/ingest/status     重建进度

    账号体系（对话历史按用户分区，故必须有账号）
    POST /api/auth/register     注册（返回令牌）
    POST /api/auth/login        登录（返回令牌）
    POST /api/auth/logout       注销（作废当前令牌）
    GET  /api/auth/me           当前登录用户
    对话历史（均需登录，且只能访问自己的数据）
    GET    /api/history/conversations        会话列表（可按标题关键词 / 模式过滤）
    POST   /api/history/conversations        新建会话
    GET    /api/history/conversations/{id}   会话详情（含全部消息与元数据）
    PATCH  /api/history/conversations/{id}   重命名会话
    DELETE /api/history/conversations/{id}   删除会话
    GET    /api/history/search               历史搜索（关键词 + 时间范围 + 主题/模式）

启动::

    cd backend
    ..\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

# 【__future__ 导入】Python 的特殊开关，让本文件「提前」使用新版本语法。
# 这里导入 annotations 表示「类型注解先不求值，只当普通文字记下」。
# 为什么这么写：可以放心用 `int | None` 这类新写法，也避免类之间互相引用时
# 出现「还没定义就被使用」的顺序问题。
from __future__ import annotations

# 【asyncio】Python 自带的异步库。本文件用它的 asyncio.to_thread 把会阻塞的
# 同步调用（数据库、第三方 SDK）丢进后台线程，避免卡住处理请求的主循环。
import asyncio

# 【json】把 Python 字典转成 JSON 文本（反之亦可）。
# 这里主要用于 SSE：把事件字典转成 `data: {...}` 这一行文本发给前端。
import json

# 【threading】线程工具。这里用两样东西：
# ① Lock（锁）：保证多个请求同时进来时，对同一份状态字典的修改不会互相踩踏；
# ② Thread（线程）：把建库这种耗时活儿丢到后台线程慢慢跑，立刻回前端「已启动」。
import threading

# 【time】取当前时间戳 / 格式化时间。用来记录建库的开始与结束时间，
# 并给日志行加 `[HH:MM:SS]` 时间戳，方便排查「哪一步卡了多久」。
import time

# 【contextmanager】把下面带 yield 的函数变成可用 `with` 的上下文管理器；
# 本文件用它把「底层异常 → HTTP 状态码」的翻译逻辑收拢成 _auth_errors() /
# _history_errors() 两个可复用小工具。
# 【suppress】「安静忽略指定异常」的小工具，相当于 try/except pass 的简写。
from contextlib import contextmanager, suppress

# 类型注解工具，只给人和 IDE 看，不影响运行逻辑：
# 【Any】任意类型；
# 【Iterator / AsyncIterator】「迭代器」类型 —— 迭代器就是能一个一个吐出值的对象
#   （如 for 循环里被遍历的东西）；带 Async 的是「异步版」，要用 `async for` 遍历。
# 【Literal】限定「只能取列出的那几个字面值」，例如模式只能是 react/plan/deep 之一。
from typing import Any, AsyncIterator, Iterator, Literal

# 从 FastAPI 框架取各种零件：
# 【FastAPI】应用本体，下面 `app = FastAPI(...)` 就是在创建它；
# 【Depends】依赖注入，用于「先校验登录再进接口」；
# 【File / Form】声明「该参数来自上传的文件 / 表单字段」（对应 multipart 上传）；
# 【Header】声明「该参数从请求头里取」（如 Authorization 里的令牌）；
# 【HTTPException】抛出即变成 HTTP 错误响应；
# 【Query】声明「该参数来自网址问号后的查询串」，可顺带加取值范围限制；
# 【Request】当前请求对象，这里主要用 `request.is_disconnected()` 判断用户是否已关页面；
# 【UploadFile】上传上来的文件对象，用 `await file.read()` 读到二进制内容。
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile

# 【跨域中间件】前端（localhost:3000）与后端（127.0.0.1:8000）端口不同，浏览器
# 默认会拦截这种「跨域」请求；注册它并放行后，前端才能正常调用接口。
from fastapi.middleware.cors import CORSMiddleware

# 【流式响应】用于 SSE：它接收一个「会不断产出文本块」的对象（本文件的 event_stream），
# 并把这些文本块持续写回前端，从而实现「逐字显示」的效果。
from fastapi.responses import StreamingResponse

# 【Pydantic】用来声明请求体结构。BaseModel 是基类；Field 给字段加约束与说明文字。
from pydantic import BaseModel, Field

# 本项目内部模块（相对导入，`.` 表示「同一个 app 包内」）：
# 【auth】账号与令牌；【history as history_store】对话历史存取。
# 起别名 history_store 是为了避免与接口里表示「最近几轮对话」的变量 history 混淆。
from . import auth, history as history_store
# ratelimit：应用层限流（内存令牌桶），给问答类接口套 429 门禁。
from . import ratelimit
# 【Agent 模块】MODES 是允许的三种模式（react/plan/deep）；
# TOOL_NAME 是知识库检索工具的名字（用于 /api/config 里回显「有哪些工具」）；
# agent_events 是「产生问答事件流」的核心异步生成器，/api/agent/chat 消费它。
from .agent import MODES, TOOL_NAME, agent_events

# 【配置】Settings 是配置对象的类型；get_settings() 读取并缓存 .env 配置。
# 注意：配置被缓存（lru_cache），改完 .env 必须重启后端才会生效。
from .config import Settings, get_settings

# 【MySQL】DatabaseUnavailable 是「连不上库」的专用异常；discover_schema 读取表结构；
# list_tables 列出库内所有表名；mysql_connection 是「用完自动关闭」的连接管理器。
from .db import DatabaseUnavailable, discover_schema, list_tables, mysql_connection

# 【Neo4j 图谱】GRAPH_TOOL_NAME 图谱工具名；GraphError 图谱专用异常；
# load_schema 读图谱结构；render_schema_text 把结构渲染成给大模型看的说明文本。
from .graph import GRAPH_TOOL_NAME, GraphError, load_schema, render_schema_text

# 【建库】run_ingest 执行「MySQL 三张表 → 向量库」的导入流程，支持传进度回调。
from .ingest import run_ingest

# 【PostgreSQL】PG_TOOL_NAME 图工具名；PgError 专用异常；load_schema 读库结构；
# render_schema_text 渲染成给大模型看的说明文本。后面两个起了别名以免与图谱同名函数冲突。
from .pg import PG_TOOL_NAME, PgError, load_schema as load_pg_schema, render_schema_text as render_pg_schema_text

# 【纯 RAG】answer_events 产生「先来源、后回答」的事件流，供 /api/chat（基线接口）使用。
from .rag import answer_events

# 【重排】rerank_hits 用大模型把检索结果重新排序，/api/retrieve?rerank=true 会用它做对比。
from .rerank import rerank_hits

# 【检索】retrieve 是混合检索（向量 + BM25）入口，返回带分数的候选结果。
from .retriever import retrieve

# 【向量库】VectorStoreError 向量库专用异常；get_store 取得（并缓存）向量库连接。
from .vector_store import VectorStoreError, get_store

# 【图片解析】ImageRejected 表示图片本身不合规，对应 HTTP 400；
#   ImageTooLarge（体积超限）→ 413、ImageUnsupported（真实格式不支持/不符）→ 415，
#   两者都是 ImageRejected 的子类 —— 捕获顺序必须「先子类后父类」，否则状态码会被 400 盖掉；
# VisionError 表示解析环节出错（没配 Key / 模型报错 / 超时），对应 HTTP 502。
from .vision import ImageRejected, ImageTooLarge, ImageUnsupported, VisionError

# 把 vision 的四个函数各起一个更明确的名字导入，避免和本文件的接口函数重名：
# run_analyze_image 真正调用视觉模型；check_image_size 校验体积；
# resolve_image_mime 推断图片声称的类型（先看文件名后缀，再看浏览器上报的类型）；
# verify_image_content 嗅探文件头，核对真实格式（防「PDF 改名成 .png」这类伪造）。
from .vision import analyze_image as run_analyze_image
from .vision import check_size as check_image_size
from .vision import resolve_mime as resolve_image_mime
from .vision import verify_content as verify_image_content

# 【联网搜索】导入工具名并改名，避免与本文件其它 TOOL_NAME 混淆。
from .web_search import TOOL_NAME as WEB_SEARCH_TOOL_NAME

# 【问答缓存】answer_cache 提供「规范化精确匹配」的问答结果缓存（注意：是精确匹配，
# 不是向量语义匹配），以及判断「这个请求该不该进缓存」的准绳 is_cacheable。
from . import answer_cache

# 【可观测性指标】metrics 记录每轮问答的耗时 / 首字延迟 / 缓存命中 / 重排情况等，
# 并提供 summary() 做汇总统计。起别名 metrics_store 是为了不与接口函数 /api/metrics 混淆。
from . import metrics as metrics_store

# 【日志】setup_logging 统一配置日志的输出位置与格式；get_logger 取一个日志器。
from .logging_setup import get_logger, setup_logging

# 【缓存与知识库版本解耦】kb_version：知识库版本指纹，/api/health 回显它，
# 便于排查「为什么答的是旧内容」（对比缓存写入时的版本与当前版本）。
from .kb_version import kb_version

# 创建 FastAPI 应用本体（就是 uvicorn 启动时导入的那个 app）。
# title / description / version 只影响自动生成的接口文档页（/docs）上显示的说明。
app = FastAPI(
    title="招投标智能问答机器人 API",
    description="MySQL → Qdrant → 混合检索（向量 + BM25）→ 大模型问答，回答附带参考来源。",
    version="1.0.0",
)

# 前端是独立的 Next.js 应用（默认 3000 端口），端口不同即「跨域」，需放行。
# 【为什么收紧（安全加固）】原先 allow_origins=["*"] 会放行任意网站，任何站点都能
#   借用户浏览器跨域调用本接口；改为从 .env 的 CORS_ORIGINS 读取白名单（逗号分隔），
#   默认只允许本机前端的两个地址。
# allow_credentials=False：不放行跨域带 Cookie（令牌走 Authorization 头，无需 Cookie）。
# allow_methods / allow_headers 全部放行，保证 GET/POST 等与自定义头都能通过。
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 初始化全项目日志（幂等：无论导入多少次，只会真正配置一次）。
# 放在这里保证「应用一被导入就配好日志」，控制台与 backend/data/app.log 都能收到日志。
setup_logging(get_settings())
# 本模块专用的日志器（名字形如 "app.main"，日志格式里的「模块」就是它）。
logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
# 下面这些类都用 Pydantic 的 BaseModel 继承而来，作用是「声明前端 POST 上来的 JSON
# 应该长什么样」。FastAPI 会自动按这些声明去校验：
#   · 必填字段没给 → 自动返回 422（参数错误），不会进入我们的函数；
#   · 类型不对（比如 top_k 传了字符串） → 同样自动 422；
#   · 声明里写的取值范围（如 ge=1、le=50）也会被自动检查。
# 这样我们函数体内就可以放心假定参数是合法的，省掉大量手写判断。
# `Field(...)` 里那个 `...`（三个点，Python 里的 Ellipsis）表示「这个字段必填」；
# 写成 `Field(None, ...)` 则表示默认值是 None（也就是可以不传）。

# 【纯 RAG 问答接口的请求体】对应 POST /api/chat。
class ChatRequest(BaseModel):
    # 用户问题。min_length=1 表示不允许空字符串（前端传空会被拦成 422）。
    question: str = Field(..., min_length=1, description="用户问题")
    # 最终送给大模型当「参考资料」的条数。可以不传（默认 None，用配置里的默认值）；
    # ge=1、le=50 表示传了就必须是 1~50 之间的整数，防止有人传 10000 条把模型撑爆。
    top_k: int | None = Field(None, ge=1, le=50, description="最终送入大模型的参考资料条数")


# 【重建知识库接口的请求体】对应 POST /api/ingest。
class IngestRequest(BaseModel):
    # 是否「先清空再重建」。默认改 False（曾默认 True，是个配置陷阱）：
    # 默认 True 意味着「只传 mode="incremental"」会立刻撞上
    # 「增量不能重建集合」的 400 —— 默认值与增量模式天然互斥。
    # 现在默认 False，让唯一的合法组合（增量 + 不重建）开箱即用；
    # 全量必须显式传 recreate=true（否则 400，见 start_ingest 的校验）。
    recreate: bool = Field(False, description="是否清空后重建；仅全量模式允许为 true")
    # 【新增】建库模式："full"（默认，全量重建，行为与改动前一致）/ "incremental"（增量更新）。
    # 【为什么默认 full（关键）】老前端不传这个字段时，行为与之前完全一样，保证向后兼容。
    # 【为什么用 Literal 限定取值】非法值（如拼错的 "incr"）会被 FastAPI 直接拒成 422，
    #   避免「悄悄退化成全量」这种隐蔽的错误行为。
    mode: Literal["full", "incremental"] = Field(
        "full", description="建库模式：full（全量）/ incremental（增量）"
    )


# 【一轮对话】用于把「历史聊天记录」从前端传回后端。
# role 只能是 "user"（用户）或 "assistant"（助手）二者之一（Literal 限定了取值范围）。
class ChatTurn(BaseModel):
    # 发言方：用户还是助手。
    role: Literal["user", "assistant"] = Field(..., description="发言方")
    # 这一轮说的内容。
    content: str = Field(..., description="该轮文本")


# 【Agent 问答接口的请求体】对应 POST /api/agent/chat，字段比 /api/chat 多很多。
class AgentChatRequest(BaseModel):
    # 用户问题，同 ChatRequest。
    question: str = Field(..., min_length=1, description="用户问题")
    # 检索后最终送给大模型的参考资料条数，约束同 ChatRequest。
    top_k: int | None = Field(None, ge=1, le=50, description="检索后最终送入大模型的参考资料条数")
    # 最近几轮对话。设计要点：这些历史「由前端保存并每次回传」，后端自己不记（无状态），
    # 好处是后端可以随意重启/横向扩容而不丢上下文；同时按 AGENT_HISTORY_TURNS 截断，
    # 只保留最近若干轮，防止历史太长把模型的上下文窗口挤爆、也省 token 费用。
    history: list[ChatTurn] | None = Field(
        None, description="最近几轮对话（由前端持有并回传，后端无状态），按 AGENT_HISTORY_TURNS 截断"
    )
    # 是否允许 Agent 联网搜索（前端「联网搜索」开关）。
    # 不传时由配置 WEB_SEARCH_DEFAULT 决定；若后端没配 TAVILY_API_KEY，
    # 联网工具根本不会注册，这个开关自然也就无效（不会报错，只是不起作用）。
    web_search: bool | None = Field(
        None,
        description="是否允许联网搜索（前端「联网搜索」开关）；不传则用 WEB_SEARCH_DEFAULT，"
        "后端未配置 TAVILY_API_KEY 时该开关无效",
    )
    # 推理模式，只能是三者之一，默认 react：
    #   react：单轮「想一步做一步」，与改造前行为一致，最快；
    #   plan ：Plan-and-Solve，先列计划再逐步执行，适合多步骤问题；
    #   deep ：深度思考 = plan + Reflection（执行完自我检查，不满意再修订）。
    mode: Literal["react", "plan", "deep"] = Field(
        "react", description="推理模式：react（现状单轮）/ plan（Plan-and-Solve）/ deep（深度思考 = plan + Reflection）"
    )
    # 历史会话 id。前端第一次提问时可以不传，后端会自动新建一个会话，
    # 并把新建的会话 id 通过第一个 SSE「conversation」事件回传给前端，之后前端就带着它。
    conversation_id: str | None = Field(
        None, description="历史会话 id；前端首次提问可不带（后端自动新建并随 conversation 事件返回）"
    )


# 【注册 / 登录的请求体】两者字段相同，故共用一个模型。
# 注意：这里的 min_length=1 / max_length 只是「便宜的」格式护栏（防止超长输入）；
# 真正的强度规则（用户名 3~32 位、口令至少 8 位）在 auth.py 里校验，
# 违规则返回 400 并带上明确的错误说明。
class AuthRequest(BaseModel):
    # 用户名。
    username: str = Field(..., min_length=1, max_length=64, description="用户名（3~32 位中英文/数字/下划线）")
    # 口令。注意口令全程只以哈希（不可逆摘要）形式落库，绝不存明文。
    password: str = Field(..., min_length=1, max_length=128, description="口令（至少 8 位）")


# 【新建会话的请求体】对应 POST /api/history/conversations。
class ConversationCreate(BaseModel):
    # 会话标题。可以不传，此时后端会用 history_store.DEFAULT_CONVERSATION_TITLE（「新对话」）。
    title: str | None = Field(None, max_length=200, description="会话标题；缺省用「新对话」")
    # 该会话使用的推理模式（记录在会话上，方便列表里按模式筛选）。
    mode: Literal["react", "plan", "deep"] = Field("react", description="该会话的推理模式")


# 【重命名会话的请求体】对应 PATCH /api/history/conversations/{id}。
class ConversationRename(BaseModel):
    # 新标题。必填，且不允许空字符串（min_length=1）。
    title: str = Field(..., min_length=1, max_length=200, description="新标题")


# 【本轮的「信息收集器」】为什么需要它？
# 问答是「流式」的：事件一个接一个产生，我们一边往前端转发，一边还得把关键信息攒起来，
# 等整轮结束时（收到 done 事件）一次性写进对话历史。
# 如果每产生一个小片段就写一次数据库，会让数据库被高频小块写操作压垮，
# 而这些写入对「当下的问答体验」毫无帮助 —— 所以这里选择「只在最后写一次」，
# 让流式热路径没有任何额外开销。
# 收集的字段与「前端渲染的样子」一一对应：前端显示什么，历史里就存什么，
# 这样用户回看历史时看到的和当时一模一样。
class _EventCollector:
    """边转发 SSE 边汇总本轮问答的关键信息，供 ``done`` 时一次写入对话历史。

    只在 ``done`` 时落库一次，**不逐 token 写库** —— SSE 热路径零额外开销。
    汇总口径与前端渲染一一对应：工具调用、来源、SQL / Cypher / 联网结果、
    规划与各步观察、自评与修订稿，全部进消息的 ``meta``。
    """

    # 初始化：把每个要收集的东西都准备好「空容器」。
    # 注意这里把可变对象（列表/字典）放在 __init__ 里创建，而不是写成类属性，
    # 否则所有实例会共用同一份列表，互相污染（这是 Python 很常见的坑）。
    def __init__(self) -> None:
        # 本轮调用过哪些工具（记下工具名与查询内容），供历史回显。
        self.tool_calls: list[dict[str, Any]] = []
        # 各工具返回了多少条（工具名 + 条数）。
        self.tool_results: list[dict[str, Any]] = []
        # 参考来源列表（命中的公告/项目原文片段与出处）。
        self.sources: list[dict[str, Any]] = []
        # 本轮检索是否「真正应用了重排」（来自 retrieval 事件的 meta）。
        # 用于可观测性指标统计「重排应用率」；默认 False（没收到或没生效都算没应用）。
        self.rerank_applied = False
        # 本轮重排是否「尝试过但失败降级」（来自 retrieval meta 的 rerank_error）。
        # 与 rerank_applied 一起构成「重排成功率」的两个计数口径；默认 False。
        self.rerank_failed = False
        # 本轮是否「按需触发判定后主动跳过重排」（来自 retrieval meta 的 rerank_skipped）。
        # 与上面两个互斥，共同回答「这次到底重排了没有、为什么」。
        self.rerank_skipped = False
        # PostgreSQL 查询信息（SQL 文本、返回行数、是否出错）；没查过就是 None。
        self.sql: dict[str, Any] | None = None
        # Neo4j 图谱查询信息（Cypher 文本、返回行数、是否出错）；没查过就是 None。
        self.graph: dict[str, Any] | None = None
        # 联网搜索结果（查询词、结果列表、是否出错）；没搜过就是 None。
        self.web: dict[str, Any] | None = None
        # plan 模式产出的「计划步骤清单」。
        self.plan: list[dict[str, Any]] = []
        # 计划中每一步的执行情况（序号、标题、状态、观察到的结果/错误）。
        self.steps: list[dict[str, Any]] = []
        # deep 模式的「自我检查（Reflection）」记录（第几轮、结论、发现的问题）。
        self.reflections: list[dict[str, Any]] = []
        # 回答正文的碎片。用列表攒碎片、最后再拼成整段，比反复拼接字符串快得多
        # （字符串在 Python 里不可变，每次拼接都要新建一份，碎片多时会很慢）。
        self.answer: list[str] = []
        # 修订稿的碎片（deep 模式自我检查后重写的版本，单独成通道）。
        self.revision: list[str] = []
        # done 事件里带回来的「结束模式」（如 agent_rag / agent_direct / agent_failed）。
        self.done_mode: str | None = None
        # 若本轮中途出错，记下错误信息（用于历史里如实标注「这轮失败了」）。
        self.error: str | None = None

    # 喂一个事件进来，按事件类型归类存好。
    # 这是整个问答流的「分拣机」：上游产生什么事件，这里就归到对应的格子里。
    def feed(self, event: dict[str, Any]) -> None:
        # 取出事件类型（如 "delta"、"tool_call"）。用 .get() 而不是 [] ，
        # 是为了在字典里没有 type 字段时返回 None 而不是抛异常（更稳）。
        etype = event.get("type")
        # 工具被调用的那一刻：记下工具名与它收到的查询内容。
        if etype == "tool_call":
            self.tool_calls.append({"name": event.get("name"), "query": event.get("query")})
        # 工具返回结果的那一刻：记下工具名与命中条数。
        elif etype == "tool_result":
            self.tool_results.append({"name": event.get("name"), "count": event.get("count")})
        # 检索统计事件：meta 里带「重排是否生效」（rerank_applied）。
        # 只取这两个字段（生效与否、失败与否）供指标统计用（其余检索统计前端会自己显示）。
        elif etype == "retrieval":
            meta = event.get("meta") or {}
            self.rerank_applied = bool(meta.get("rerank_applied"))
            self.rerank_failed = bool(meta.get("rerank_error"))
            self.rerank_skipped = bool(meta.get("rerank_skipped"))
        # 参考来源事件：直接整体保存（`or []` 保证即使上游给 None 也存成空列表，
        # 避免后面遍历时对 None 报错）。
        elif etype == "sources":
            self.sources = event.get("sources") or []
        # PostgreSQL 查询事件：只挑我们要展示/回看的三个字段存下来。
        elif etype == "sql_query":
            self.sql = {
                "sql": event.get("sql"),
                "row_count": event.get("row_count"),
                "error": event.get("error"),
            }
        # Neo4j 查询事件：同上，只存 Cypher 文本、行数、错误。
        elif etype == "graph_query":
            self.graph = {
                "cypher": event.get("cypher"),
                "row_count": event.get("row_count"),
                "error": event.get("error"),
            }
        # 联网搜索事件：存查询词、结果列表、错误。
        elif etype == "web_search":
            self.web = {
                "query": event.get("query"),
                "results": event.get("results"),
                "error": event.get("error"),
            }
        # 计划生成事件：整体替换计划清单。
        elif etype == "plan":
            self.plan = event.get("steps") or []
        # 计划中某一步的状态变化（开始/完成/失败）：追加一条记录。
        elif etype == "plan_step":
            self.steps.append(
                {
                    "index": event.get("index"),
                    "title": event.get("title"),
                    "status": event.get("status"),
                    "observation": event.get("observation"),
                    "error": event.get("error"),
                }
            )
        # 自我检查事件：追加一条（可能多轮，所以用列表而不是单个值）。
        elif etype == "reflection":
            self.reflections.append(
                {
                    "round": event.get("round"),
                    "verdict": event.get("verdict"),
                    "issues": event.get("issues") or [],
                }
            )
        # 正文碎片事件：按「通道」分流。channel 为 "revision" 的碎片属于修订稿，
        # 其余（或没写 channel）都算正式回答。这样前端能并列展示「初稿 / 修订稿」。
        elif etype == "delta":
            text = event.get("text") or ""
            if (event.get("channel") or "answer") == "revision":
                self.revision.append(text)
            else:
                self.answer.append(text)
        # 出错事件：记下错误说明。
        elif etype == "error":
            self.error = event.get("message")
        # 结束事件：记下结束模式。
        elif etype == "done":
            self.done_mode = event.get("mode")

    # 把回答碎片拼成完整文本，并去掉首尾空白。
    # 用 "".join(碎片列表) 一次性拼接 —— 这是 Python 里拼大量字符串的标准做法。
    def answer_text(self) -> str:
        return "".join(self.answer).strip()

    # 把所有收集到的信息打包成一个字典，作为消息的 meta（元数据）一起落库。
    # 前端从历史里读回这条消息时，就靠这份 meta 还原工具徽标、来源卡片、SQL 表格等。
    def meta(self, mode: str) -> dict[str, Any]:
        return {
            "mode": mode,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "sources": self.sources,
            "sql": self.sql,
            "graph": self.graph,
            "web_search": self.web,
            "plan": self.plan,
            "steps": self.steps,
            "reflections": self.reflections,
            # 修订稿拼好后如果为空则存 None（前端据此判断「要不要显示修订块」）。
            "revision": "".join(self.revision).strip() or None,
            "done_mode": self.done_mode,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# 建库任务状态（进程内单例，本地向量库不支持多进程并发写入）
# --------------------------------------------------------------------------- #
# 【背景】「重建知识库」是个耗时活儿（要读 MySQL、切块、算向量、写索引），
# 不能让前端一直等着。做法是：接口立刻回「已启动」，真正干活丢到后台线程，
# 前端再每隔一会儿来问「现在到哪一步了」。
# 【为什么状态存在「进程内的字典」而不是数据库】本地向量库是嵌入式模式，
# 同一时刻只允许一个进程打开它，天然就只能单进程运行，所以状态放内存最省事。
# 【锁（Lock）的作用】多个请求可能同时来读/写这份状态字典（比如建库线程在写、
# 前端在查），锁保证同一时刻只有一个在改，避免出现「读到一半的数据」或计数错乱。
_state_lock = threading.Lock()
# 这份字典就是「建库任务的实时状态牌」，前端查 /api/ingest/status 拿到的就是它。
_ingest_state: dict[str, Any] = {
    # 是否正在建库（防止重复启动第二个任务）。
    "running": False,
    # 当前阶段名（如 start / load / embed / done），给前端显示进度用。
    "stage": "idle",
    # 当前阶段的说明文字。
    "message": "",
    # 日志行列表（带时间戳），前端可以像看控制台一样滚动查看。
    "logs": [],
    # 建库成功后的汇总结果（导入了多少条等）。
    "result": None,
    # 出错时的错误说明；成功则为 None。
    "error": None,
    # 开始时间、结束时间（Unix 时间戳，秒）。
    "started_at": None,
    "finished_at": None,
}


# 统一更新状态字典的小工具。所有写入都走它，才能保证每次都正确加锁。
# 【参数解释】**fields 表示「可以传任意个以关键字形式给出的字段」，
# 例如 _update_state(running=False, stage="idle") 会把这两个键更新进字典。
# 这种写法叫「关键字参数收集」，好处是调用处简洁、且不用为每种组合写一个函数。
def _update_state(**fields: Any) -> None:
    # with 语句在离开时会自动释放锁，即使中间抛异常也不会「锁死」，比手写加锁/解锁更安全。
    with _state_lock:
        _ingest_state.update(fields)


# 建库过程的「进度回调」函数：ingest 模块每完成一小步就会调它一次。
# 【参数解释】stage 是阶段名，message 是该步说明；
# **_: Any 用来「吞掉」调用方可能多传的其它关键字参数 —— 因为回调签名是上游定的，
# 多传参数时我们不想因为「参数不匹配」而报错，索性全部忽略。
def _append_log(stage: str, message: str, **_: Any) -> None:
    with _state_lock:
        _ingest_state["stage"] = stage
        _ingest_state["message"] = message
        # 取出日志列表（注意：字典里存的是列表的「引用」，直接 append 就会改到原列表，
        # 不需要再写回字典）。
        logs = _ingest_state["logs"]
        # f-string（形如 f"..."）是「把变量的值直接嵌进字符串」的写法；
        # time.strftime('%H:%M:%S') 生成 时:分:秒 这样的时间串，方便看每步耗时。
        logs.append(f"[{time.strftime('%H:%M:%S')}][{stage}] {message}")
        # 只保留最近 200 条，防止长时间运行内存膨胀
        # 【切片解释】logs[:-200] 表示「除了最后 200 条以外的全部」，
        # del 把它们删掉，于是列表末尾始终最多保留 200 条。
        if len(logs) > 200:
            del logs[:-200]


# 后台线程真正执行建库的函数。它「兜住所有异常」，绝不把异常抛出去 ——
# 因为它在后台线程里跑，异常若没人接住，只会被打印一行警告然后静默消失，
# 前端将永远等不到「失败」状态、一直以为还在建库中。
# 【参数解释】recreate 表示是否先清空再重建（由请求体传入）。
#   mode 表示建库模式："full"（默认，全量）/ "incremental"（增量）。
def _ingest_worker(mode: str, recreate: bool) -> None:
    # try/except/finally 三段式：
    #   try 里做正事；except 分门别类记录失败原因；finally 无论成败都要收尾。
    try:
        # 调 ingest 模块干活，并把 _append_log 作为「进度回调」传进去，
        # 这样建库过程中的每一步都会实时反映到状态牌上（前端能看到进度在动）。
        # mode 由接口传入，决定跑全量还是增量；增量结果里的 added/updated/deleted 会随 result 一起回显。
        result = run_ingest(recreate=recreate, progress=_append_log, mode=mode)
        # 成功：记录结果，并把 error 清空（避免上一轮的旧错误残留）。
        _update_state(result=result, error=None)
    # 数据库连不上：给出明确的「数据库不可用」提示（比笼统的「建库失败」好用得多）。
    except DatabaseUnavailable as exc:
        _update_state(error=f"数据库不可用：{exc}")
    # 向量库异常（索引损坏、锁定冲突等）。
    except VectorStoreError as exc:
        _update_state(error=f"向量库错误：{exc}")
    # 兜底：任何其它没预料到的异常都要接住并记录。
    # 【注意事项】末尾那行 `# noqa: BLE001` 是给代码检查工具看的注释，
    # 意思是「这里故意捕获宽泛的 Exception，请别报警」—— 后台线程确实必须兜住一切。
    except Exception as exc:  # noqa: BLE001 - 后台线程必须兜住所有异常
        _update_state(error=f"建库失败：{exc}")
    # 不管成功还是失败，最后都要把「运行中」标记摘掉，否则以后再也不能启动新任务。
    finally:
        _update_state(running=False, stage="idle", finished_at=time.time())


# --------------------------------------------------------------------------- #
# 基础信息
# --------------------------------------------------------------------------- #
# 健康检查接口。前端顶栏的「图谱 已连接 / 未配置」「PostgreSQL 已连接 / 未配置」
# 以及侧边栏显示的模型名，都是靠调这个接口拿到的。
# 设计要点：**这个接口永远返回 200**，用字段值（True/False）来表达各种能力是否可用。
# 为什么不直接报错？因为「某项功能没配置」是正常状态而非故障，
# 前端需要的是「知道它没配，好把它显示成灰色」而不是收到一个错误。
@app.get("/api/health")
def health() -> dict[str, Any]:
    # 读配置（配置对象是被缓存过的，读取很快，不会每次都重读 .env 文件）。
    s = get_settings()
    # 顺便问一下向量库现在的状况（有多少条数据、是否可用）。
    # 这里单独用 try 包住：向量库没就绪不代表整个服务挂了，
    # 所以失败时只把错误信息放进 store_stats，绝不中断这个健康检查接口。
    try:
        store_stats = get_store(s).stats()
    except VectorStoreError as exc:
        # 失败时把错误说明当成「向量库的统计信息」塞进去，前端照样能显示原因。
        store_stats = {"error": str(exc)}
    return {
        # 固定为 ok：能返回这个响应就说明服务本身活着。
        "status": "ok",
        # 大模型是否已配置（配了 Key 才算，没配就只能走「只检索不生成」的降级模式）。
        "llm_configured": s.llm_configured,
        # 真实调用的模型 id（如 deepseek-flash）。`or None` 把空字符串统一成 None，
        # 让前端判断「有没有值」时只要看是否 null 即可，不用再区分空串。
        "llm_model": s.llm_model or None,
        # 界面展示用的大模型名（可与真实 API model id 不同，如 LLM_MODEL_DISPLAY 设置）
        # 【为什么需要这个】真实可用的模型 id 只有 deepseek-flash / deepseek-v4-pro，
        # 但界面上想显示更友好的名字，于是用一个单独的「显示名」配置项来满足两边需求。
        "llm_model_display": s.llm_display_name or None,
        # 重排功能是否可用（重排会额外调一次大模型，所以要单独判断）。
        "rerank_configured": s.rerank_configured,
        # 重排用的模型名；没单独配就退而用主模型（`or` 是「前者为空就用后者」）。
        "rerank_model": s.rerank_model or s.llm_model or None,
        # Agent 是否可用（Agent 依赖大模型，没配大模型就没有 Agent）。
        "agent_configured": s.agent_configured,
        # Agent 用的模型名。
        "agent_model": s.agent_model_or_default or None,
        # MySQL 是否已配置（没配就没法建知识库）。
        "mysql_configured": s.mysql_configured,
        # 建库管道当前实际使用的数据源（"mysql" / "postgres"）。
        # 【为什么需要】切到 PG 副本后，/api/mysql/schema 会如实显示「未连接」，
        # 前端需要这个字段才能把卡片文案解释成「知识库数据源：PostgreSQL 副本（生产：MySQL）」，
        # 避免与 README 截图里「已连接」的旧状态自相矛盾。
        "kb_source": "postgres" if s.kb_source_is_postgres else "mysql",
        # PostgreSQL 是否已配置。注意：没配置时 PG 查询工具不会注册给 Agent。
        "pg_configured": s.pg_configured,
        # PostgreSQL 查询时用于「生成 SQL」的模型名。
        "pg_model": s.pg_model_or_default or None,
        # Neo4j 图谱是否已配置。没配置时图谱查询工具不会注册给 Agent。
        "graph_configured": s.graph_configured,
        # 图谱查询时用于「生成 Cypher」的模型名。
        "graph_model": s.graph_model_or_default or None,
        # 联网搜索是否已配置（本质是「有没有配 TAVILY_API_KEY」）。
        "web_search_configured": s.web_search_configured,
        # 联网搜索的提供方名字；没配就是 None（前端据此隐藏开关）。
        "web_search_provider": "tavily" if s.web_search_configured else None,
        # 联网搜索的默认开关状态（前端开关的初始值）。
        "web_search_default": s.web_search_default,
        # 图片解析（智谱视觉模型）是否已配置。
        "vision_configured": s.vision_configured,
        # 对话历史（SQLite 本地持久化）与账号体系：历史按用户分区，两者需同时开启
        # 历史功能开关（总开关）。
        "history_enabled": s.history_enabled,
        # 历史功能是否「已就绪」（开关打开且存储路径可用）。
        "history_configured": s.history_configured,
        # 账号体系是否启用（关掉就只能匿名使用，且无法保存/检索历史）。
        "auth_enabled": s.auth_enabled,
        # 支持的推理模式列表。list(MODES) 把元组/集合转成列表，
        # 因为 JSON 没有「元组」这种类型，必须转成列表才能正常序列化。
        "modes": list(MODES),
        # 向量库的统计信息（或错误说明）。
        "index": store_stats,
        # 当前是否正在重建知识库（前端据此禁用「重建」按钮并显示进度）。
        "ingesting": _ingest_state["running"],
        # 知识库版本指纹（8~12 位短 hash，见 app/kb_version.py）。
        # 【为什么要暴露】问答缓存按它判失效：排查「为什么答的是旧内容」时，
        # 先看这里的版本与 data/kb_version.txt 是否对得上，再看缓存行里的版本。
        "kb_version": kb_version(s),
    }


# 配置回显接口：把「当前真正生效的配置」列出来。
# 主要用途是排查「我改了 .env 怎么没生效」这类问题 —— 直接调一下就知道后端眼里是什么值。
# 【安全注意】所有口令类字段一律只回显「是否设置了」（布尔值），绝不回显口令本身。
# 【部署加固】配置含内网主机 / 库名 / 文件路径等拓扑信息，与 /api/metrics 同一
#   口径加 current_user 依赖，未登录返回 401；登录后也不回显主机、端口、库名、
#   表清单、路径等敏感字段——只回显「是否已配置」、模型名与纯调参项。
@app.get("/api/config")
def read_config(user: dict[str, Any] = Depends(auth.current_user)) -> dict[str, Any]:
    """回显当前生效配置，方便排查「.env 没生效」类问题。口令一律不回显。需要登录。"""
    # 这里显式标注类型 Settings，是为了让编辑器能提示字段名（纯注解，不影响运行）。
    s: Settings = get_settings()
    return {
        # MySQL 连接状态（口令只回显「是否已设置」；主机 / 端口 / 库名 / 表清单属
        # 内网拓扑信息，一律不回显）。
        "mysql": {
            "user": s.mysql_user,
            # bool(...) 把字符串转成 True/False，避免口令泄露。
            "password_set": bool(s.mysql_password),
            "charset": s.mysql_charset,
        },
        # 向量库信息。mode 说明是「嵌入式本地模式」（没有 Docker，数据存本地目录）。
        "qdrant": {
            "mode": "embedded-local",
            "collection": s.qdrant_collection,
        },
        # 向量模型信息（维度必须与向量库中已存数据一致，否则无法检索）。
        "embedding": {"model": s.embed_model, "dim": s.embed_dim},
        # BM25 关键词索引参数。k1、b 是 BM25 公式里控制「词频饱和」与
        # 「文档长度归一化」强度的两个系数，属于调参项，故一并回显。
        "bm25": {"k1": s.bm25_k1, "b": s.bm25_b},
        # 检索参数：两路各取多少候选、最终给模型几条、融合时的 RRF 系数、向量相似度下限。
        "retrieval": {
            "dense_top_k": s.retrieve_dense_top_k,
            "sparse_top_k": s.retrieve_sparse_top_k,
            "final_top_k": s.retrieve_final_top_k,
            "rrf_k": s.rrf_k,
            "dense_min_score": s.dense_min_score,
        },
        # 重排参数。这里能看到那两个「血泪参数」：timeout=45、candidates 等。
        "rerank": {
            "enabled": s.rerank_configured,
            "model": s.rerank_model or s.llm_model or None,
            "candidates": s.rerank_candidates,
            "max_chars": s.rerank_max_chars,
            "timeout": s.rerank_timeout,
        },
        # 大模型参数。
        "llm": {
            "configured": s.llm_configured,
            "model": s.llm_model or None,
            "model_display": s.llm_display_name or None,
            "temperature": s.llm_temperature,
        },
        # Agent 参数。framework 说明底层用的是 langchain + langgraph。
        "agent": {
            "framework": "langchain-langgraph",
            "enabled": s.agent_configured,
            "model": s.agent_model_or_default or None,
            "max_tokens": s.agent_max_tokens,
            "history_turns": s.agent_history_turns,
            "max_steps": s.agent_max_steps,
            "modes": list(MODES),
            # plan / deep 模式的细分参数。
            "deep": {
                "max_steps": s.deep_max_steps,
                "step_max_steps": s.deep_step_max_steps,
                "reflect_rounds": s.deep_reflect_rounds,
                "max_tokens": s.deep_max_tokens,
                "timeout": s.deep_timeout,
                "observation_chars": s.deep_observation_chars,
            },
            # 当前实际注册给 Agent 的工具清单。
            # 【读法】先用列表 [TOOL_NAME] 打底（知识库检索工具总是有），
            # 然后按条件用 + 号把 PG 工具、图谱工具、联网工具追加进去；
            # 没配置的就不加 —— 所以「工具列表里没有它」就等于「Agent 不会调用它」。
            "tools": [TOOL_NAME]
            + ([PG_TOOL_NAME] if s.pg_configured else [])
            + ([GRAPH_TOOL_NAME] if s.graph_configured else [])
            + ([WEB_SEARCH_TOOL_NAME] if s.web_search_configured else []),
        },
        # PostgreSQL 配置（口令只回显是否设置；主机 / 端口 / 库名不回显）。
        "postgres": {
            "configured": s.pg_configured,
            "schema": s.pg_schema,
            "user": s.pg_user,
            "password_set": bool(s.pg_password),
            "tables": list(s.pg_tables),
            "model": s.pg_model_or_default or None,
            "max_rows": s.pg_max_rows,
            "statement_timeout": s.pg_statement_timeout,
            "schema_ttl": s.pg_schema_ttl,
            "tools": [PG_TOOL_NAME],
        },
        # Neo4j 配置（口令只回显是否设置；连接串 / 库名 / 账号不回显）。
        "neo4j": {
            "configured": s.graph_configured,
            "password_set": bool(s.neo4j_password),
            "model": s.graph_model_or_default or None,
            "max_rows": s.neo4j_max_rows,
            "timeout": s.neo4j_timeout,
            "schema_ttl": s.graph_schema_ttl,
            "tools": [GRAPH_TOOL_NAME],
        },
        # 联网搜索配置（API Key 只回显是否设置）。
        "web_search": {
            "configured": s.web_search_configured,
            "provider": "tavily" if s.web_search_configured else None,
            "api_key_set": bool(s.tavily_api_key),
            "search_depth": s.tavily_search_depth,
            "max_results": s.tavily_max_results,
            "timeout": s.tavily_timeout,
            "default_enabled": s.web_search_default,
            # 没配置时不注册工具，故这里回显空列表。
            "tools": [WEB_SEARCH_TOOL_NAME] if s.web_search_configured else [],
        },
        # 图片解析配置（智谱视觉模型）。
        "vision": {
            "configured": s.vision_configured,
            "model": s.vision_model,
            "timeout": s.vision_timeout,
            "max_bytes": s.vision_max_bytes,
        },
        # 对话历史配置。存储引擎是 SQLite（单文件数据库，免安装、便于本地部署）。
        "history": {
            "enabled": s.history_enabled,
            "configured": s.history_configured,
            "storage": "sqlite",
            "page_size": s.history_page_size,
            "search_limit": s.history_search_limit,
            "title_max_chars": s.history_title_max_chars,
            "max_message_chars": s.history_max_message_chars,
            # FTS5 是 SQLite 的全文检索扩展；这里回显「当前环境到底能不能用」，
            # 因为某些 SQLite 编译版本没带这个扩展，功能会优雅退化成 LIKE 模糊匹配。
            "fts_available": history_store.fts_available(s) if s.history_configured else False,
            # 当前已有多少会话/消息等统计。
            "counts": history_store.stats(s),
        },
        # 账号体系配置（口令强度、令牌有效期、防爆破参数属安全配置，不回显具体值）。
        "auth": {
            "enabled": s.auth_enabled,
            "lockout_seconds": s.auth_lockout_seconds,
        },
    }


# --------------------------------------------------------------------------- #
# MySQL 结构
# --------------------------------------------------------------------------- #
# 查看 MySQL 的真实表结构：每张表有哪些字段、字段注释是什么、大概多少行数据，
# 以及这个库里还有哪些别的表。前端可以据此显示「数据源」信息，也方便排查
# 「为什么检索不到某张表的数据」（多半是表名配错了或表不存在）。
@app.get("/api/mysql/schema")
def mysql_schema() -> dict[str, Any]:
    s = get_settings()
    # 【与 PG/图谱接口一致的设计】连不上库时返回 200 + 明确原因，
    # 而不是抛 500。理由：前端想的是「把它展示出来」，而不是处理一个报错弹窗。
    try:
        # with 语句：进入时建立连接，离开时（无论是否报错）自动关闭连接，避免连接泄漏。
        with mysql_connection(s) as conn:
            # 只读取「配置里关心参与建库的那几张表」的结构（discover_schema）。
            schema = discover_schema(conn, s.kb_tables)
            # 顺便列出库里所有表名，方便发现「其实库里有别的表」。
            all_tables = list_tables(conn)
    except DatabaseUnavailable as exc:
        # 数据库连不上时返回 200 + 明确原因，前端可直接展示，而不是弹一个笼统的 500
        return {
            "connected": False,
            "error": str(exc),
            "database": s.mysql_database,
            # 连不上时仍然回一份「表清单骨架」，每张表标 exists=False，
            # 这样前端布局不会因为数据缺失而崩，能正常渲染出「未连接」的样子。
            # 【字典推导式】{键: 值 for t in 序列} 是「用循环一次性造出字典」的写法。
            "tables": {t: {"table": t, "label": s.label_of(t), "exists": False} for t in s.kb_tables},
            "available_tables": [],
        }

    # 给每张表补上「中文标签」（标签来自表注释，运行时读取，不写死在代码里）。
    # schema.items() 会一次给出「键和值」，所以循环里可以直接拿到表名和它的信息字典。
    for table, info in schema.items():
        info["label"] = s.label_of(table)
    return {
        "connected": True,
        "error": None,
        "database": s.mysql_database,
        "tables": schema,
        "available_tables": all_tables,
        # 合计总行数。int(...)、.get(...) 双重保护：万一某张表没有 row_count 字段、
        # 或值不是数字，也不会让这个接口崩掉。
        # sum(生成器) 会把括号里逐项算出的数字加起来。
        "total_rows": sum(int(i.get("row_count", 0)) for i in schema.values()),
    }


# --------------------------------------------------------------------------- #
# PostgreSQL 结构（Agent 生成 SQL 的依据，只读自检）
# --------------------------------------------------------------------------- #
# 查看 PostgreSQL 的库结构：有哪些表、字段叫什么、字段注释、近似行数、几行样例数据。
# 【为什么需要它】Agent 要写 SQL，必须先「看见」表结构（就像人得先看菜单才能点菜）。
# 这份结构会被渲染成一段文字（schema_text）塞进给大模型的提示词里。
# 所以这个接口既是给前端看的，也是「排查 Agent 写错 SQL」的第一现场。
@app.get("/api/pg/schema")
def pg_schema(refresh: bool = Query(False, description="忽略缓存，强制重新读取库结构")) -> dict[str, Any]:
    """回显 Agent 生成 SQL 时用到的库结构（表/字段/注释/近似行数/样例值）。"""
    s = get_settings()
    # 先准备一份「未连接」时的默认返回骨架，后面各种失败分支都基于它拼装。
    # 这样能保证无论哪种失败，前端拿到的 JSON 结构都一样（不会某次少字段而崩）。
    empty = {
        "connected": False,
        "database": s.pg_database,
        "schema": s.pg_schema,
        "tables": [],
        "schema_text": "",
    }
    # 没配置 PostgreSQL（缺少必要配置项）时，明确告知「没配」，并说明需要哪些配置项。
    # 【重要】这不是报错 —— 项目允许「只跑知识库、不接 PG」，功能只是不启用而已。
    if not s.pg_configured:
        return {**empty, "error": "未配置 PostgreSQL（PG_HOST / PG_USER / PG_PASSWORD / PG_DATABASE）"}
    try:
        # refresh=True 表示「不用缓存，重新读一次库结构」——
        # 因为库结构会被缓存一段时间（pg_schema_ttl 秒）以省开销，
        # 改完表结构想立刻看到效果时就要强制刷新。
        data = load_pg_schema(s, refresh=refresh)
    except PgError as exc:
        # 连不上时返回 200 + 明确原因，前端可直接展示，而不是弹一个笼统的 500
        return {**empty, "error": str(exc)}
    return {
        "connected": True,
        "error": None,
        "database": data.get("database"),
        "schema": data.get("schema"),
        # `or []` 保证即使上游给 None，也返回空列表（前端循环遍历时空列表是安全的）。
        "tables": data.get("tables") or [],
        # 把结构渲染成「给大模型看的说明文本」，前端也能直接展示这段文字便于排查。
        "schema_text": render_pg_schema_text(data),
    }


# --------------------------------------------------------------------------- #
# Neo4j 图谱结构（Agent 生成 Cypher 的依据，只读自检）
# --------------------------------------------------------------------------- #
# 与 PG 接口完全对称：查看图谱里有哪些「节点标签」「关系类型」「三元组（谁-关系-谁）」
# 以及节点属性，并渲染成文字供大模型参考。
# 【概念说明】Neo4j 是「图数据库」，数据存成「节点 + 关系」，
# 适合回答「谁和谁有什么关系」「经过几跳能连上」这类问题；查询语言叫 Cypher。
@app.get("/api/graph/schema")
def graph_schema(refresh: bool = Query(False, description="忽略缓存，强制重新读取图谱结构")) -> dict[str, Any]:
    """回显 Agent 生成 Cypher 时用到的图谱结构（标签/关系/三元组/属性/计数）。"""
    s = get_settings()
    # 同样先准备「未连接」时的默认骨架。
    empty = {
        "connected": False,
        "database": s.neo4j_database,
        "labels": [],
        "relationship_types": [],
        "triples": [],
        "nodes": [],
        "schema_text": "",
    }
    # 未配置 Neo4j 时明确说明需要的配置项；同样不算报错。
    # 【实测提醒】图谱可能是空的（数据还没导入），此时能连上但查不出东西，
    # Agent 会如实回答「没找到数据」，不会编造 —— 这是刻意设计的行为。
    if not s.graph_configured:
        return {**empty, "error": "未配置 Neo4j（NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD）"}
    try:
        # 读图谱结构（refresh=True 时绕过缓存重新读）。
        data = load_schema(s, refresh=refresh)
    except GraphError as exc:
        # 连不上时返回 200 + 明确原因，前端可直接展示，而不是弹一个笼统的 500
        return {**empty, "error": str(exc)}
    return {
        "connected": True,
        "error": None,
        "database": data.get("database"),
        # 节点标签清单（相当于「图谱里有哪些种类的实体」，如 公司 / 项目 / 公告）。
        "labels": data.get("labels") or [],
        # 关系类型清单（如 中标 / 属于 / 关联）。
        "relationship_types": data.get("relationship_types") or [],
        # 三元组清单：「起点 - [关系] -> 终点」这种样例，帮助大模型理解图怎么连。
        "triples": data.get("triples") or [],
        # 节点及其属性样例。
        "nodes": data.get("nodes") or [],
        # 渲染成给大模型看的说明文本。
        "schema_text": render_schema_text(data),
    }


# --------------------------------------------------------------------------- #
# 异常翻译：存储层异常 → 合适的 HTTP 状态码
# --------------------------------------------------------------------------- #
# 【为什么需要这一层】auth.py / history.py 这些底层模块只管「发现问题并抛异常」，
# 它们不该关心 HTTP（那是 Web 层的事）；而接口函数里如果到处写 try/except 又会很啰嗦。
# 于是这里用「上下文管理器」把翻译工作集中起来，接口里只需写
# `with _auth_errors():` 一行，里面的任何 auth 异常都会自动变成正确的 HTTP 错误。
# 【@contextmanager 原理】被它装饰的「带 yield 的函数」会变成可用 with 的对象：
#   with 块开始时执行到 yield 那行停下，把控制权交给 with 里面的代码；
#   with 块里若抛出异常，会在这里的 yield 处被「扔回」，从而被下面的 except 接住。
# 【HTTP 状态码速记】400=请求有问题；401=未登录/令牌无效；404=找不到；
#   409=冲突（如重名）；429=太频繁（被限流/锁定）；503=服务暂时不可用。
@contextmanager
def _auth_errors() -> Iterator[None]:
    try:
        # 把控制权交给 with 块里的代码；with 块里的异常会在这一行「冒出来」。
        yield
    # 账号系统不可用（比如历史数据库没就绪）→ 503，表示「服务暂时没法用」。
    except auth.AuthUnavailable as exc:
        # 【异常链】`raise ... from exc` 表示「这个新异常是由 exc 引起的」。
        # 好处：日志里能看到「原始原因」，排查时不用猜是谁把谁吃掉了。
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except auth.RateLimited as exc:
        # 429 让前端能区分「口令错」与「被锁定」
        # （连续多次口令错误会触发达次数限制，此时不是用户输错了，而是被暂时锁定）。
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    # 拿不到合法令牌 → 401（未登录）。注意必须写在 AuthError 之前：
    # Unauthorized 是 AuthError 的子类，写反了会被下面那条翻成 400。
    except auth.Unauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    # 其余账号类错误（用户名不合规、口令太短、用户名已存在等）→ 400（请求有问题）。
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 写历史时的「冲突」（如标题重复之类的唯一性约束）→ 409。
    except history_store.HistoryConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# 历史模块的异常翻译：与上面同理，只是换成了历史相关的异常类型。
@contextmanager
def _history_errors() -> Iterator[None]:
    try:
        yield
    # 历史存储不可用（如数据库文件打不开、磁盘满）→ 503。
    except history_store.HistoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # 找不到指定的会话/消息 → 404。
    except history_store.HistoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# 账号体系
# --------------------------------------------------------------------------- #
# 【为什么需要账号】对话历史要「按人分区」存储 —— 每个用户只能看到自己的历史。
# 所以必须先有身份，才能把历史挂到某个用户名下。
# 【令牌（token）机制】登录成功后后端发一串随机字符串（令牌），前端保存它，
# 以后每次请求都在请求头带 `Authorization: Bearer <令牌>`。
# 后端只存令牌的「哈希」（不可逆摘要），所以即使数据库泄露也拿不到可用令牌。
@app.post("/api/auth/register")
async def auth_register(payload: AuthRequest, user_agent: str | None = Header(None)) -> dict[str, Any]:
    """注册并直接返回令牌（省掉一次登录往返）。口令只以 PBKDF2 哈希落库。"""
    s = get_settings()
    # `user_agent: str | None = Header(None)` 表示「从请求头 User-Agent 取值」，
    # 缺省为 None。记录它是为了在令牌列表里能看出「这令牌是哪台设备登的」。
    with _auth_errors():
        # PBKDF2 是 CPU 密集操作，丢线程池避免阻塞事件循环
        # 【细节】asyncio.to_thread(函数, 参数1, 参数2, ...) 会在后台线程里执行该函数，
        # 并把返回值原样交还。注意这里传的是「函数本身」而不是「函数调用」。
        return await asyncio.to_thread(auth.register, s, payload.username, payload.password, user_agent)


# 登录：校验用户名口令，成功则发一个新令牌。
# 【与注册相同的三点】用 _auth_errors 统一翻译异常；用 to_thread 避免阻塞；返回令牌给前端。
@app.post("/api/auth/login")
async def auth_login(payload: AuthRequest, user_agent: str | None = Header(None)) -> dict[str, Any]:
    s = get_settings()
    with _auth_errors():
        return await asyncio.to_thread(auth.login, s, payload.username, payload.password, user_agent)


# 注销：作废当前这个令牌（而不是删账号）。
# 【注意】这里多了一个依赖 `Depends(auth.current_user)`：它会先校验令牌是否有效，
# 无效就直接返回 401，函数体根本不会执行 —— 所以注销只能针对「已登录的人」。
# 拿不到合法令牌绝不返回 ok:true（否则前端以为已登出，令牌却还有效）。
@app.post("/api/auth/logout")
async def auth_logout(
    user: dict[str, Any] = Depends(auth.current_user),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    s = get_settings()
    with _auth_errors():
        # require_bearer 从请求头里把「Bearer xxx」中的 xxx 抠出来，
        # 拿不到合法令牌会抛 Unauthorized（由 _auth_errors 翻译成 401）——
        # 与上面的 current_user 依赖形成双保险，谁也不能单方面放行。
        await asyncio.to_thread(auth.logout, s, auth.require_bearer(authorization))
    # 回显用户名，方便前端提示「xxx 已退出登录」。
    return {"ok": True, "username": user["username"]}


# 查当前登录者是谁。前端刷新页面后，用它来确认「本地存的令牌还有效吗」，
# 有效就恢复登录态、加载该用户的历史；无效则跳回登录页。
@app.get("/api/auth/me")
def auth_me(user: dict[str, Any] = Depends(auth.current_user)) -> dict[str, Any]:
    return {"user": user}


# --------------------------------------------------------------------------- #
# 对话历史
# --------------------------------------------------------------------------- #
# 【本节所有接口的共同点】
# 1) 都带 `Depends(auth.current_user)` —— 必须登录才能访问；
# 2) 都把 `user["id"]` 传给底层 —— 底层查询一律带「用户 id」条件，
#    因此任何人都不可能读到别人的会话（这是「按用户分区」的落实方式）；
# 3) 都用 `_history_errors()` 统一翻译异常，并用 `asyncio.to_thread` 避免阻塞。
# 【概念：分页】列表接口都支持 limit（每页几条）+ offset（跳过前几条）。
# 前端翻页 = 调整 offset。这样即使有上万条历史，也只需一次读一小页就够快。
@app.get("/api/history/conversations")
async def history_conversations(
    # limit=0 是个「哨兵值」：表示「不传就用配置里的默认每页条数」。ge/le 限定 0~100。
    limit: int = Query(0, ge=0, le=100, description="每页条数；0 表示用 HISTORY_PAGE_SIZE"),
    # offset：跳过前面多少条（用于翻页），不允许负数。
    offset: int = Query(0, ge=0),
    # q：按「会话标题」模糊过滤（注意：这里是筛会话，不是搜消息正文，
    # 搜正文请看下面的 /api/history/search）。
    q: str | None = Query(None, description="按会话标题（对话主题）关键词过滤"),
    # mode：只看某种推理模式的会话（react/plan/deep 之一或都不限）。
    mode: Literal["react", "plan", "deep"] | None = Query(None, description="按该会话的推理模式过滤"),
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    """会话列表，按最近更新倒序。只返回当前登录用户自己的会话。"""
    s = get_settings()
    # `limit or 默认值`：如果前端传 0（或没传），就用配置里的每页条数。
    page = limit or s.history_page_size
    with _history_errors():
        # 底层返回「这一页的数据 items」和「总条数 total」两样东西。
        # 【元组解包】`a, b = 函数()` 表示函数返回了一个两项的元组，分别装进 a、b。
        items, total = await asyncio.to_thread(
            history_store.list_conversations, s, user["id"], page, offset, q, mode
        )
    # 把查询条件原样回显，方便前端显示「第 X 页、每页 Y 条、共 Z 条」。
    return {"items": items, "total": total, "limit": page, "offset": offset}


# 新建会话。什么时候需要它？用户想「先建个空会话再开聊」时可以主动建；
# 但更常见的是：第一次提问时不带 conversation_id，由 /api/agent/chat 内部自动建
# （见下面 agent_chat 里的 ensure_conversation）。
@app.post("/api/history/conversations")
async def history_create_conversation(
    payload: ConversationCreate,
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    s = get_settings()
    # 没给标题就用默认标题（「新对话」）；给了就尊重用户输入。
    title = payload.title or history_store.DEFAULT_CONVERSATION_TITLE
    with _history_errors():
        conversation = await asyncio.to_thread(
            history_store.create_conversation, s, user["id"], title, payload.mode
        )
    return {"conversation": conversation}


# 读某个会话的详情：会话本身信息 + 该会话下的所有消息（含每条的元数据）。
# 【路径参数】网址里的 `{conversation_id}` 会被 FastAPI 自动填进同名参数。
# 前端点历史列表里某一项时调的就是它。
@app.get("/api/history/conversations/{conversation_id}")
async def history_conversation_detail(
    conversation_id: str,
    # 只取最近 N 条消息；0（默认）表示全部取。上限 2000 条，防止超大会话拖垮响应。
    limit: int = Query(0, ge=0, le=2000, description="只取最近 N 条；0 表示全部"),
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    """会话详情：含全部消息与元数据（来源、工具调用、SQL / Cypher / 联网结果、规划与自评）。"""
    s = get_settings()
    with _history_errors():
        conversation = await asyncio.to_thread(history_store.get_conversation, s, user["id"], conversation_id)
        # 底层找不到（或不属于当前用户）时返回 None，这里统一转成 404。
        # 【安全说明】「不存在」和「不属于你」都返回同一个 404，
        # 这样攻击者无法通过错误差异探测「别人有哪些会话 id」。
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        # `limit or None`：0 转成 None，表示底层「不限条数，全部返回」。
        messages = await asyncio.to_thread(
            history_store.list_messages, s, user["id"], conversation_id, limit or None
        )
    return {"conversation": conversation, "messages": messages}


# 重命名会话（用户手动改标题）。用 PATCH 而不是 PUT，
# 因为这是「只改一部分字段」（只改标题），符合 PATCH 的语义。
@app.patch("/api/history/conversations/{conversation_id}")
async def history_rename_conversation(
    conversation_id: str,
    payload: ConversationRename,
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    s = get_settings()
    try:
        with _history_errors():
            conversation = await asyncio.to_thread(
                history_store.rename_conversation, s, user["id"], conversation_id, payload.title
            )
    # 底层若发现标题不合规（如全是空白）会抛 ValueError，这里翻译成 400。
    # 【为什么要单独包一层】ValueError 是 Python 内置异常，太通用，
    # 不适合放进 _history_errors（那样会把无关的 ValueError 也误当成 400）。
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 找不到该会话 → 404。
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"conversation": conversation}


# 删除会话（连同它的消息一起删）。删完就不可恢复，所以前端一般会先弹确认框。
@app.delete("/api/history/conversations/{conversation_id}")
async def history_delete_conversation(
    conversation_id: str,
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    s = get_settings()
    with _history_errors():
        # 底层返回「是否真的删掉了」的布尔值。
        deleted = await asyncio.to_thread(history_store.delete_conversation, s, user["id"], conversation_id)
    # 没删到东西说明这个 id 不存在（或不属于当前用户）→ 404，
    # 而不是假装成功（否则用户以为删掉了，其实是删了别人的或本来就没了）。
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "id": conversation_id}


# 历史检索：这是「找以前聊过的内容」用的，与 /api/chat 的「知识库检索」是两码事。
# 【三个维度】关键词（q）+ 时间范围（from/to）+ 模式（mode），可以任意组合，也可以全不填。
# 【实现要点】关键词检索用 SQLite 的 FTS5（全文检索扩展），中文先经 jieba 分词
# 再建索引/查询；某些环境没有 FTS5 会自动退化成 LIKE 模糊匹配（功能仍在，只是慢些）。
@app.get("/api/history/search")
async def history_search(
    # 关键词：同时匹配「消息正文」和「会话标题」。
    q: str | None = Query(None, description="关键词（jieba 分词后走 FTS5，同时匹配消息正文与会话标题）"),
    # 时间范围起止。`alias="from"` 表示网址上写 `from=...`（因为 from 是 Python 关键字，
    # 参数名不能叫 from，只能用别名映射）。
    date_from: str | None = Query(None, alias="from", description="起始时间，如 2026-09-01 或 2026-09-01 08:00:00"),
    # 只给日期时，底层会自动把结束时间补到当天 23:59:59，
    # 否则「to=2026-09-01」会变成「截止到 00:00:00」，导致漏掉当天白天的消息。
    date_to: str | None = Query(None, alias="to", description="结束时间，只给日期时自动补到当天 23:59:59"),
    mode: Literal["react", "plan", "deep"] | None = Query(None, description="按该会话的推理模式过滤"),
    limit: int = Query(0, ge=0, le=100, description="每页条数；0 表示用 HISTORY_SEARCH_LIMIT"),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    """历史检索：关键词 + 时间范围 + 主题/模式，三个维度可任意组合。"""
    s = get_settings()
    # 同样：0 表示用配置里的默认条数（搜索的默认条数单独配，与列表页可以不同）。
    page = limit or s.history_search_limit
    with _history_errors():
        items, total = await asyncio.to_thread(
            history_store.search_messages, s, user["id"], q, date_from, date_to, mode, page, offset
        )
    # 额外回显 `fts`：告诉前端「当前是全文检索模式还是退化后的模糊匹配模式」，
    # 前端可以据此显示一句提示（如「当前环境不支持中文全文索引，结果可能较慢」）。
    return {"items": items, "total": total, "limit": page, "offset": offset, "fts": history_store.fts_available(s)}


# --------------------------------------------------------------------------- #
# 图片解析（智谱视觉模型）
# --------------------------------------------------------------------------- #
# 前端「📎 附件按钮」上传图片后调这个接口：把图片发给智谱视觉模型，
# 让它识别成文本（如「这是招标公告首页，标题是……，预算约……万元」），
# 前端再把识别结果填进提问框（并显示在一张卡片里）。
# 【接口签名里的两个特殊参数】
#   File(...)   —— 从 multipart 表单里取上传的文件；
#   Form("")    —— 同一份表单里的普通文本字段（提问文字，可省略）。
#   multipart 是「一个请求里既带文件又带普通字段」的专用格式。
# 【为什么鉴权】图片解析要调用付费的视觉模型，是消耗型接口；必须登录后才能用，
#   否则任何人都能匿名刷这个接口，白白消耗额度。
@app.post("/analyze-image")
async def analyze_image(
    file: UploadFile = File(..., description="待解析的图片（jpg / png / webp；大小上限见配置 VISION_MAX_BYTES，默认 10MB）"),
    question: str = Form("", description="可选：随图片一起提交的提问文字"),
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    """图片解析：把图片交给智谱视觉模型识别成结构化文本。

    状态码按 RFC 语义分级（不再一律 400）：
    401 未登录；400 文件头识别不出 / 空文件 / 声称的类型不支持；
    413 体积超过 VISION_MAX_BYTES；415 真实格式不是 jpg/png/webp 或与声称不符；
    502 解析失败（未配 Key / 模型报错 / 超时）。
    """
    s = get_settings()
    # `await file.read()`：把上传的文件内容一次性读进内存（bytes 类型）。
    # 注意：文件在读出前一直以临时文件形式存在，读入内存后即可丢弃。
    data = await file.read()
    # 第一道关卡：校验图片「声称的类型」和「体积」。
    try:
        # 推断图片声称的类型（先看文件名后缀，再看浏览器上报的 content_type）。
        # 【为什么自己推断而不信浏览器】content_type 是浏览器说了算、可伪造的，
        # 用它做安全判断不可靠；后缀和它交叉验证才稳。
        mime = resolve_image_mime(file.filename, file.content_type)
        # 校验体积是否超过上限（默认 10MB）。超限会抛 ImageTooLarge（→ 413）。
        check_image_size(len(data), s)
    # 捕获顺序：先子类（413/415）后父类（400），否则子类也会被当成 400。
    except ImageTooLarge as exc:
        # 体积超限 → 413：是「东西太大」而不是「东西不对」，让前端能分开提示。
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ImageUnsupported as exc:
        # 文件头真实格式不支持/与声称不符 → 415。
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ImageRejected as exc:
        # 其余不合规（声称类型不支持、空文件）→ 400：让前端明确知道「是你的图片不行」，
        # 而不是把「图片不合规」伪装成「服务故障」。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 第二道关卡：嗅探文件头，核对「真实格式」。
    # 【为什么必须有这一步】后缀和 content_type 都能伪造——把 PDF 改名成 .png
    # 能一路过掉上面的校验，直到调视觉模型才报错（曾表现为莫名 502）。
    # 看文件头就能在调模型之前识破：识别不出 → 400，格式不符/不支持 → 415。
    try:
        verify_image_content(data, mime)
    except ImageUnsupported as exc:
        # 注意顺序：必须先捕获子类 ImageUnsupported，再捕获父类 ImageRejected，
        # 否则 415 会被父类的 except 抢先匹配、错误地返回 400。
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ImageRejected as exc:
        # 文件头识别不出任何已知格式 → 400。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 第二道关卡：真正调视觉模型解析。
    try:
        # SDK 是同步阻塞调用，丢到线程池避免卡住事件循环
        # 【为什么】视觉模型调用可能要等几十秒（网络 + 推理），
        # 若在事件循环里同步等，所有其它请求都会被堵住。
        result = await asyncio.to_thread(run_analyze_image, data, mime, question, s)
    except VisionError as exc:
        # 解析环节出错（没配 Key / 模型报错 / 超时）→ 502（网关错误），
        # 与 400 区分开：这是「服务这边的锅」，不是用户的图片有问题。
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # 成功：把识别出的文本和图片的元信息回给前端。
    # filename / mime / size 是给前端展示「这次解析的是哪张图、多大」用的。
    return {
        "text": result["text"],
        "model": result.get("model"),
        "filename": file.filename,
        "mime": mime,
        "size": len(data),
    }


# --------------------------------------------------------------------------- #
# 知识库
# --------------------------------------------------------------------------- #
# 知识库统计：向量库里一共多少块（chunk）、按表怎么分布、用的是什么向量模型。
# 前端「数据源」面板用它显示「知识库已就绪 / 有多少条」。
@app.get("/api/kb/stats")
def kb_stats() -> dict[str, Any]:
    s = get_settings()
    try:
        # get_store(s) 取得向量库连接（内部缓存，不会每次重建连接）。
        # .stats() 返回统计信息字典。
        stats = get_store(s).stats()
    except VectorStoreError as exc:
        # 与上面几个接口不同，这里**抛 503**：因为「统计不到向量库」属于真正的服务故障
        #（比如向量库目录被别的进程锁住了），前端应该显示错误而不是「0 条」。
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # 往统计结果里补充几个字段（直接在原字典上加键）。
    # BM25 那边索引里有多少条文档（正常情况下应与向量库条数一致，不一致就说明索引没同步）。
    stats["bm25_docs"] = _bm25_doc_count(s)
    # 当前用的向量模型名（换了模型必须重建索引，维度不一致会检索失败）。
    stats["embed_model"] = s.embed_model
    # 各表的中文标签（来自 MySQL 表注释，运行时读取）。
    stats["table_labels"] = {t: s.label_of(t) for t in s.kb_tables}
    return stats


# 小工具：数一数 BM25 索引里有多少条文档。
def _bm25_doc_count(s: Settings) -> int:
    # 【为什么在函数内部才 import】导入 retriever 会牵连加载 BM25 相关依赖（较重）。
    # 放在函数里「用到才加载」，可以让模块启动更快，也避免循环导入问题（延迟导入技巧）。
    from .retriever import load_bm25

    try:
        # load_bm25(s) 读入 BM25 索引对象，其 n_docs 属性就是文档数。
        return load_bm25(s).n_docs
    except Exception:
        # 索引还没建（文件不存在）等任何异常，都当作「0 条」处理。
        # 【设计意图】这里只是给前端显示一个辅助数字，绝不能因为它没建好就让整个统计接口失败。
        return 0


# 抽样看几条知识库里的内容（默认 5 条，最多 50 条）。
# 用途：确认「数据到底有没有进去」「切块后的文本长什么样」，排查检索效果差的第一步。
@app.get("/api/kb/samples")
def kb_samples(limit: int = Query(5, ge=1, le=50)) -> dict[str, Any]:
    s = get_settings()
    try:
        # .sample(limit) 按存储顺序取前若干条原始片段（scroll，非随机）。
        return {"samples": get_store(s).sample(limit)}
    except VectorStoreError as exc:
        # 与 /api/kb/stats 口径一致：向量库连不上/被锁/维度不符 → 503，
        # 而不是让异常冒成 500（语义是「服务暂时不可用」，前端可提示稍后重试）。
        logger.warning("知识库样本读取失败（向量库）：%s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# 纯检索接口（不调大模型生成答案）。
# 【用途】这是「调参神器」：你可以直接看检索出了哪些候选、各自得了多少分，
# 从而判断「是检索没找到，还是找到了但模型没用上」。
# 默认不重排（因为重排要额外花钱调模型）；带 ?rerank=true 时顺带重排，方便前后对比。
@app.get("/api/retrieve")
def retrieve_only(
    q: str = Query(..., min_length=1, description="查询语句"),
    # 不传就用配置里的默认条数。
    top_k: int | None = None,
    rerank: bool = Query(False, description="是否调用大模型二次排序（默认关闭，保持纯检索零成本）"),
    # 应用层限流（IP+用户维度令牌桶，超限 429）；见 app/ratelimit.py 的「已知限制」说明。
    _limit: None = Depends(ratelimit.rate_limit),
) -> dict[str, Any]:
    """只走混合检索，不调用大模型 —— 调参和排查检索质量时用。

    传 ``rerank=true`` 时会额外调用大模型重排，方便对比「重排前 vs 重排后」的顺序差异。
    """
    s = get_settings()
    # want_k：最终想要几条（重排后只保留这么多）。
    want_k = top_k or s.retrieve_final_top_k
    # pool_k：先取多少候选。要重排时，候选池必须足够大（取 max 保证不少于重排需要的最小候选数），
    # 否则「重排」就变成「只是给仅有的几条排个序」，提升有限。
    # 这是三元表达式：值A if 条件 else 值B。
    pool_k = max(want_k, s.rerank_candidates) if rerank else want_k

    # 混合检索：向量（语义相似）+ BM25（关键词命中），再用 RRF 融合成一份候选列表。
    # 【为什么要包一层】retriever.retrieve 只兜住了 dense 那一路的内部异常；
    # get_store / get_embedder / load_bm25 / search_sparse 都在它自己的 try 之外，
    # 向量库被锁、维度不符时 VectorStoreError 会直接冒出来变 500。
    # 这里统一收敛成 503（与 /api/kb/stats、/api/kb/samples 同口径）并记日志。
    try:
        result = retrieve(q, settings=s, final_k=pool_k)
    except VectorStoreError as exc:
        logger.warning("检索失败（向量库）q=%r：%s", q, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # 预先准备一个空字典装「重排的元信息」（用了哪个模型、耗时、是否降级等）。
    # 显式标注类型是为了让编辑器能提示（纯注解，不影响运行）。
    rerank_meta: dict[str, Any] = {}
    if rerank:
        # rerank_hits 会调大模型重新排序，返回「新顺序的命中列表」和「元信息」。
        # top_k=want_k 表示只保留前 want_k 条。
        hits, rerank_meta = rerank_hits(q, result.hits, s, top_k=want_k)
        # 【注意】这里是把重排结果**替换**回原对象（原地修改），
        # 所以下面 result.to_sources() 用的是重排后的顺序。
        result.hits = hits

    return {
        "question": q,
        # 向量那一路检索出多少条候选。
        "dense_count": result.dense_count,
        # BM25 那一路检索出多少条候选。
        "sparse_count": result.sparse_count,
        # BM25 索引是否已就绪（false 说明只有向量一路在工作，效果会打折）。
        "bm25_ready": result.bm25_ready,
        # 重排元信息（未重排时是空字典 {}）。
        "rerank": rerank_meta,
        # 把命中结果整理成「源文档卡片」形式（原文截断到 300 字，避免响应过大）。
        "sources": result.to_sources(text_limit=300),
    }


# --------------------------------------------------------------------------- #
# 问答（SSE）
# --------------------------------------------------------------------------- #
# 【SSE 格式速成】每条「事件」是一行 `data: <JSON文本>` 再加一个空行（即两个 \n）。
# 前端按行读，读到 `data: [DONE]` 就知道流结束了。
# json.dumps(..., ensure_ascii=False) 保证中文原样输出（不转成 \uXXXX），
# 否则前端会看到一堆转义符，既难看又影响体积。
def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _finalize_qa(
    *,
    settings: Settings,
    question: str,
    mode: str,
    started: float,
    first_token_ms: float | None,
    sources_count: int,
    cache_hit: bool,
    rerank_applied: bool,
    rerank_failed: bool = False,
    rerank_skipped: bool = False,
) -> None:
    """问答收尾的统一动作：记一条指标 + 打一行日志（两个问答入口共用）。

    【为什么单独抽成一个函数】/api/chat 与 /api/agent/chat、命中缓存与未命中，
    共四处收尾逻辑一模一样，抽出来避免复制粘贴、口径也统一。

    【参数解释】
      · settings       —— 配置（指标库路径等）；
      · question       —— 用户原始提问（进日志，便于排查）；
      · mode           —— 结束模式（命中缓存时传 "cache"）；
      · started        —— 请求开始的 time.perf_counter() 值，用来算端到端耗时；
      · first_token_ms —— 首字延迟（毫秒）；没有正文时传 None；
      · sources_count  —— 引用来源条数；
      · cache_hit      —— 是否命中缓存；
      · rerank_applied —— 是否应用了重排；
      · rerank_failed  —— 重排是否「尝试过但失败降级」（默认 False；缓存命中走不到重排）；
      · rerank_skipped —— 是否「按需触发判定后主动跳过重排」（默认 False）。

    【硬要求：静默降级】记指标失败绝不影响问答（metrics.record 内部已兜底）；
    日志是标准库 logging，写日志本身设计上不会抛异常影响主流程。
    """
    # 端到端耗时 = 现在 - 请求开始时刻，单位换算成毫秒。
    elapsed_ms = (time.perf_counter() - started) * 1000
    # 记一条指标（失败静默，返回值这里也不需要）。
    metrics_store.record(
        mode=mode,
        elapsed_ms=elapsed_ms,
        first_token_ms=first_token_ms,
        sources_count=sources_count,
        cache_hit=cache_hit,
        rerank_applied=rerank_applied,
        rerank_failed=rerank_failed,
        rerank_skipped=rerank_skipped,
        settings=settings,
    )
    # 打一行汇总日志：包含「收到提问 / 本次耗时 / 命中缓存」三项关键信息。
    # 【%r 与 %s】%r 会把问题用引号括起来（便于看清是不是空串/带空格）；
    #   首字延迟为 None 时显示 -1，避免日志里出现 "None ms" 这种别扭写法。
    logger.info(
        "收到提问 question=%r mode=%s 本次耗时=%.0fms 首字=%.0fms 命中缓存=%s 来源=%d 重排=%s",
        question,
        mode,
        elapsed_ms,
        first_token_ms if first_token_ms is not None else -1.0,
        "是" if cache_hit else "否",
        sources_count,
        "是" if rerank_applied else "否",
    )


# 【纯 RAG 问答接口 —— 基线接口，行为不许改】
# 【流程】先做混合检索 → 把命中结果当参考资料 → 调大模型生成回答。
# 与 /api/agent/chat 的区别：这里不经过 Agent，模型没有「决定调不调工具」的自由，
# 就是固定的「检索 → 生成」两步。
# 【为什么是 def 而不是 async def】answer_events 本身是同步生成器，
# 内部已经处理好异步，这里不需要 async；保持同步还能简化代码。
@app.post("/api/chat")
def chat(
    payload: ChatRequest,
    user: dict[str, Any] | None = Depends(auth.optional_user),
    # 应用层限流（IP+用户维度令牌桶，超限 429）；见 app/ratelimit.py 的「已知限制」说明。
    _limit: None = Depends(ratelimit.rate_limit),
) -> StreamingResponse:
    # 【新增鉴权依赖 optional_user】登录了就拿到用户信息、没登录就是 None。
    # 为什么加上它：缓存要「未登录不进缓存」，必须知道有没有登录。
    # 这是向后兼容的改动 —— 未登录照样能问答（只是不缓存、不落库），行为不变。
    s = get_settings()
    # 记下「收到请求」的时刻，作为指标里端到端耗时的起点。
    request_started = time.perf_counter()
    # 未登录 → 不缓存。
    logged_in = bool(user)
    # 【该不该缓存】纯 RAG 接口不涉及 deep 模式 / 多轮历史 / 联网 / 图片，故这四项一律为否。
    cacheable = answer_cache.is_cacheable(
        logged_in=logged_in,
        mode="llm",
        has_history=False,
        web_search=False,
        has_image=False,
        question=payload.question,
    )
    # 【先查缓存】读缓存走「读准绳」is_cache_readable（与写准绳刻意不对称，见该函数 docstring）；
    # 命中拿到答案字典，未命中/出错一律 None（静默降级）。写入仍用上面的 cacheable。
    # 【键的口径】mode="llm" + top_k 参与缓存主键：读写两侧必须同一套，
    # 否则 react/plan/llm 或不同 top_k 的答案会互相串（见 answer_cache.cache_key）。
    cached = (
        answer_cache.get_cached(payload.question, s, mode="llm", top_k=payload.top_k)
        if answer_cache.is_cache_readable(
            logged_in=logged_in, mode="llm", has_image=False, question=payload.question
        )
        else None
    )

    # 定义内部「事件流」函数（闭包：能访问外层 payload、s）。
    # 【生成器】函数体里出现 yield 的函数就是生成器：每执行到 yield 就「吐」出一个值，
    # 下次再进来接着从 yield 之后继续。这里的作用是「逐个产出 SSE 文本行」，
    # StreamingResponse 会不断地来取，前端就能边等边收到。
    # 【为什么这么设计】先把「生成器」定义好，等 StreamingResponse 真开始发送时才逐行取，
    # 这就是「边生成边发送」的本质。
    def event_stream() -> Iterator[str]:
        # 首字延迟：第一段正文出现时记一次（毫秒）；没有正文则保持 None。
        first_token_ms: float | None = None
        # 边转发边攒：来源、正文碎片、结束模式、是否应用重排（供写缓存与记指标）。
        sources: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        done_mode = "llm"
        rerank_applied = False
        # 重排是否「尝试过但失败降级」：retrieval meta 里带 rerank_error 就算失败一次。
        # 【为什么要单独记】重排失败后静默退回 RRF 原序，回答照常；不记这一笔，
        # /api/metrics 就只能看到「应用率」，永远发现不了「总在失败」的重排。
        rerank_failed = False
        # 本轮是否「按需触发判定后主动跳过重排」：retrieval meta 里带 rerank_skipped 即算。
        # 与 rerank_applied / rerank_failed 互斥，三者一起回答「这次到底重排了没有、为什么」。
        rerank_skipped = False

        # ---------------- 情况一：缓存命中，直接回放 ----------------
        if cached is not None:
            # 【顺序说明】cache_hit 是个「额外提示」事件，放在正文之前发，
            # 不改变原有事件序列（正文仍需在 done 之前、done 仍是收尾）。
            yield _sse({"type": "cache_hit"})
            yield _sse({"type": "sources", "sources": cached.get("sources") or []})
            yield _sse({"type": "delta", "text": cached.get("answer") or ""})
            yield _sse({"type": "done", "mode": cached.get("mode") or "llm"})
            # 指标 + 日志：命中缓存走的是「捷径」，耗时自然很短。
            _finalize_qa(
                settings=s,
                question=payload.question,
                mode="cache",
                started=request_started,
                first_token_ms=0.0,
                sources_count=len(cached.get("sources") or []),
                cache_hit=True,
                rerank_applied=False,
            )
            yield "data: [DONE]\n\n"
            return

        # ---------------- 情况二：未命中，走完整链路 ----------------
        try:
            # answer_events 逐个产出「事件字典」（retrieval → sources → delta → done）。
            # final_k=payload.top_k：传给生成器的条数（不传就用配置默认值）。
            for event in answer_events(payload.question, settings=s, final_k=payload.top_k):
                # 转发之前先「顺手记账」：这些都是非 SSE 事件，不会改变事件顺序。
                etype = event.get("type")
                if etype == "delta":
                    # 【只累计正式正文】channel 为 revision 的是「引用校验后的修订稿」，
                    # 与 _EventCollector 口径一致，不重复计入缓存正文。
                    if (event.get("channel") or "answer") != "revision":
                        # 第一段正文出现时记录首字延迟（只记第一次）。
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - request_started) * 1000
                        answer_parts.append(event.get("text") or "")
                elif etype == "sources":
                    sources = event.get("sources") or []
                elif etype == "retrieval":
                    meta = event.get("meta") or {}
                    rerank_applied = bool(meta.get("rerank_applied"))
                    rerank_failed = bool(meta.get("rerank_error"))
                    rerank_skipped = bool(meta.get("rerank_skipped"))
                elif etype == "done":
                    done_mode = event.get("mode") or done_mode
                # 原样转发给前端。
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001 - 流式响应里出错只能以事件形式告知前端
            # 【流式接口的坑】连接一旦开始，就不能再返回「普通的错误 JSON」了，
            # 因为前端已经在按 SSE 格式读。所以错误也必须伪装成一个 SSE 事件发出去。
            yield _sse({"type": "error", "message": f"服务异常：{exc}"})

        # 【done 之后写缓存】仅在「可缓存 + 有实际正文」时写。
        # 写失败静默（answer_cache 内部已兜底），绝不打断已完成的问答。
        answer_text = "".join(answer_parts).strip()
        if cacheable and answer_text:
            # key_mode="llm" 与上面 get_cached 的 mode 同口径（done_mode 是结束模式，
            # 只进 mode 列回放，不进缓存键）。
            answer_cache.set_cached(
                payload.question, answer_text, sources, done_mode, s,
                key_mode="llm", top_k=payload.top_k,
            )

        # 记指标 + 打日志（同样不产生 SSE 事件，不影响事件顺序）。
        _finalize_qa(
            settings=s,
            question=payload.question,
            mode=done_mode,
            started=request_started,
            first_token_ms=first_token_ms,
            sources_count=len(sources),
            cache_hit=False,
            rerank_applied=rerank_applied,
            rerank_failed=rerank_failed,
            rerank_skipped=rerank_skipped,
        )
        # SSE 协议规定：流结束时发一行 [DONE] 表示「到此为止」。
        yield "data: [DONE]\n\n"

    # 组装成流式响应返回。
    # media_type 告诉前端「这是 text/event-stream（SSE）」，前端才知道按事件流解析。
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # 不让任何环节缓存这份响应 —— 缓存会让逐字输出变成「攒够了才发一次」。
            "Cache-Control": "no-cache",
            # keep-alive：连接保持不关闭，让多个事件能走同一条连接持续下发。
            "Connection": "keep-alive",
            # 关掉 Nginx 之类的缓冲，保证逐字输出
            # 【Nginx 注意】Nginx 默认会缓冲响应，前端会一直等到整段生成完才收到；
            # 这个头让 Nginx 老实转交，才能实现逐字显示。
            "X-Accel-Buffering": "no",
        },
    )


# 【Agent 问答接口 —— 本项目最核心、最复杂的接口】
# 【与 /api/chat 的关键差异】这里把「要不要查资料、查哪个库、查几次」的决定权交给大模型，
# 它可能会依次调用：知识库检索 / PostgreSQL 查询 / 图谱查询 / 联网搜索，
# 也可能判断「这题不需要查资料」而直接回答。
# 【事件种类】除了 /api/chat 也有的事件，还可能出现：
#   conversation（首个事件，告知会话 id）、tool_call / tool_result（工具调用与结果）、
#   sql_query（PG 查询）、graph_query（图谱查询）、web_search（联网搜索）、
#   plan / plan_step（计划与步骤）、reflection（自我检查）。
# 【三种模式】react（单轮，最快）/ plan（先规划再逐步执行）/ deep（plan + 自我检查 + 修订）。
#
# 【本接口最重要的设计原则：落库绝不影响问答】
# 与用户直接相关的是「问答能不能流畅跑完」，而「历史有没有存上」只是附带功能。
# 所以：只要用户已登录且历史功能已开启，才尝试落库；任何写库动作失败都当无事发生，
# 绝不让写库的异常打断正在进行的问答流（下面多处 try/except 都是为这个）。
#
# 【鉴权用的是 optional_user 而非 current_user】
# 区别：current_user 要求「必须登录」，未登录直接 401；
# optional_user 是「登录了就给我用户信息，没登录就返回 None」。
# 为什么要可选？因为未登录也应该能问答（只是不保存历史），体验更友好。
@app.post("/api/agent/chat")
async def agent_chat(
    payload: AgentChatRequest,
    request: Request,
    user: dict[str, Any] | None = Depends(auth.optional_user),
    # 应用层限流（IP+用户维度令牌桶，超限 429）；见 app/ratelimit.py 的「已知限制」说明。
    _limit: None = Depends(ratelimit.rate_limit),
) -> StreamingResponse:
    """Agent 问答：由大模型自主决定是否调用知识库检索工具。

    事件序列与 ``/api/chat`` 一致，额外可能出现 ``tool_call`` / ``tool_result`` /
    ``sql_query`` / ``graph_query`` / ``web_search``；``done.mode`` 为
    ``agent_rag``（调用了工具）/ ``agent_direct``（未调用工具）/ ``agent_plan`` /
    ``agent_deep`` / ``agent_failed``。

    ``mode`` 三种：``react``（单轮）/ ``plan``（Plan-and-Solve）/ ``deep``（深度思考）。
    携带令牌时对话会落库（首条 ``conversation`` 事件返回会话 id）；未登录照常问答但不落库，
    **落库失败绝不影响问答流**。
    """
    s = get_settings()
    # 把 Pydantic 对象列表转成「纯字典列表」再往下传。
    # 【为什么要转】下游是通用逻辑，喂标准 dict 最省事，不必依赖 Pydantic 类型。
    # model_dump() 就是 Pydantic 提供的「转成普通字典」方法。
    # 若前端没传历史（None），这里保持 None，下游会当作「没有历史」处理。
    history = [turn.model_dump() for turn in payload.history] if payload.history else None
    # 防御性校验：虽然请求体已用 Literal 限定了取值，这里仍再确认一次，
    # 万一不在允许范围就退回 "react"（保证后续分支一定会命中某个已知模式）。
    mode = payload.mode if payload.mode in MODES else "react"

    # 只有「已登录 + 历史已开启」才写库；否则与改动前完全一致（不落库）
    # bool(...) 把结果规范化成 True/False（user 本身是字典或 None，不是布尔值）。
    persist = bool(user and s.history_configured)

    # ---- 问答缓存相关准备 ----
    # 记下「收到请求」的时刻，作为指标里端到端耗时的起点。
    request_started = time.perf_counter()
    # 复算「本次到底会不会联网」（口径与 agent.py 一致）：
    # 未显式传 web_search 时用配置默认值，再叠加「后端是否真的配了 TAVILY_API_KEY」。
    # 为什么要复算：联网结果有时效性，凡是「会联网」的请求都不进缓存。
    use_web = s.web_search_default if payload.web_search is None else bool(payload.web_search)
    use_web = bool(use_web and s.web_search_configured)
    # 【该不该缓存】未登录 / deep 模式 / 带多轮历史 / 联网 / 疑似敏感，任一命中即不缓存。
    cacheable = answer_cache.is_cacheable(
        logged_in=bool(user),
        mode=mode,
        has_history=bool(history),
        web_search=use_web,
        has_image=False,
        question=payload.question,
    )
    # 【先查缓存】读缓存走「读准绳」is_cache_readable（与写准绳刻意不对称，见该函数 docstring）：
    # 带历史 / 允许联网不再拦「读」——缓存里只可能是无历史、未联网的标准答案，精确命中即可复用。
    # 命中拿到答案字典，未命中/出错一律 None（静默降级）。写入侧的准绳在收尾处按「是否真联网」重算。
    # 【键的口径】mode（react/plan）+ top_k 参与缓存主键：react 与 plan 不许互相命中。
    cached = (
        answer_cache.get_cached(payload.question, s, mode=mode, top_k=payload.top_k)
        if answer_cache.is_cache_readable(
            logged_in=bool(user), mode=mode, has_image=False, question=payload.question
        )
        else None
    )

    # 事件流生成器（async 版）。带 yield 的 async 函数是「异步生成器」，
    # 调用方要用 `async for` 遍历，正好符合这里「一边产出、一边被 StreamingResponse 取走」的需求。
    async def event_stream() -> AsyncIterator[str]:
        # 为本轮问答创建一个信息收集器（边转发边攒，最后一次性落库）。
        collector = _EventCollector()
        # 会话信息（落库成功才有值；失败或未登录则为 None）。
        conversation: dict[str, Any] | None = None
        # 首字延迟：第一段正文出现时记一次（毫秒）；没有正文则保持 None。
        first_token_ms: float | None = None

        # 请求一开始就写用户消息：流中途断掉也不丢提问
        # 【为什么提前写】如果等整轮结束才写，用户中途关页面就整条都没了；
        # 先写用户的提问，至少「我问过什么」不会丢。
        if persist:
            try:
                # ensure_conversation：给了 conversation_id 就取那个会话，
                # 没给（或那个 id 不存在）就自动新建一个。返回会话信息字典。
                created = await asyncio.to_thread(
                    history_store.ensure_conversation, s, user["id"], payload.conversation_id, mode
                )
                # 把用户这条提问写进消息表。
                # 【参数说明】倒数第二个参数是 meta（元数据），用户消息没有元数据故传 None。
                await asyncio.to_thread(
                    history_store.append_message,
                    s,
                    user["id"],
                    created["id"],
                    "user",
                    payload.question,
                    None,
                    mode,
                )
                # 重新取一次：append_message 已用首个提问更新了标题
                # 【为什么重取】会话标题可能是「用第一条提问生成的」，
                # 刚才是新建时还没标题，所以写完之后再读一次，才能拿到更新后的标题。
                # 若重取意外返回 None，则退回用 created（`or created` 兜底）。
                conversation = (
                    await asyncio.to_thread(history_store.get_conversation, s, user["id"], created["id"])
                ) or created
            except Exception:  # noqa: BLE001 - 落库失败绝不影响问答
                # 【关键设计】写库失败就把 conversation 置空，后续所有落库动作都会跳过，
                # 但问答流的转发完全不受影响 —— 用户依然能正常提问并拿到回答。
                conversation = None

        # 第一个 SSE 事件：把会话 id / 标题 / 模式 / 是否已保存 告诉前端。
        # 前端收到后记住 id，后续提问带上它，就能接续到同一个会话。
        # 【三元表达式】`值A if 条件 else 值B`：conversation 有值就用它的字段，否则给 None。
        yield _sse(
            {
                "type": "conversation",
                "id": conversation["id"] if conversation else None,
                "title": conversation["title"] if conversation else None,
                "mode": mode,
                "saved": conversation is not None,
            }
        )

        # ---------------- 缓存命中：直接回放，不再跑 Agent ----------------
        # 【为什么放在 conversation 事件之后】保持「首个事件永远是 conversation」的既有约定，
        # 前端拿到会话 id 后再收到 cache_hit；同时不改变正文/ done 的相对顺序。
        if cached is not None:
            yield _sse({"type": "cache_hit"})
            yield _sse({"type": "sources", "sources": cached.get("sources") or []})
            yield _sse({"type": "delta", "text": cached.get("answer") or ""})
            yield _sse({"type": "done", "mode": cached.get("mode") or mode})

            # 落库：把缓存的这条回答也写进会话，保证历史里问答成对、不留「半截提问」。
            # 仍遵守「落库绝不影响问答」——任何失败静默。
            if conversation is not None:
                try:
                    # 先喂给收集器 sources 与 done，让写进历史的 meta 结构与正常路径一致。
                    collector.feed({"type": "sources", "sources": cached.get("sources") or []})
                    collector.feed({"type": "done", "mode": "cache"})
                    await asyncio.to_thread(
                        history_store.append_message,
                        s,
                        user["id"],
                        conversation["id"],
                        "assistant",
                        cached.get("answer") or "",
                        collector.meta("cache"),
                        mode,
                    )
                except Exception:  # noqa: BLE001 - 落库失败不影响问答
                    pass

            # 指标 + 日志：命中缓存走的是「捷径」，耗时自然很短。
            _finalize_qa(
                settings=s,
                question=payload.question,
                mode="cache",
                started=request_started,
                first_token_ms=0.0,
                sources_count=len(cached.get("sources") or []),
                cache_hit=True,
                rerank_applied=False,
            )
            yield "data: [DONE]\n\n"
            return

        # 标记「用户是否中途断开了连接」。
        disconnected = False
        try:
            # 遍历 Agent 事件流。`async for` 是异步版的 for 循环，
            # 每次拿到一个事件就处理，不用等全部产完。
            async for event in agent_events(
                payload.question,
                settings=s,
                final_k=payload.top_k,
                history=history,
                web_search=payload.web_search,
                mode=mode,
            ):
                # 先「喂」给收集器（攒着待会落库）。
                collector.feed(event)
                # 记录首字延迟：第一段「正式正文」出现时记一次（只记第一次）。
                # 【排除 revision】channel 为 revision 的是修订稿，不算「首字」。
                if (
                    first_token_ms is None
                    and event.get("type") == "delta"
                    and (event.get("channel") or "answer") != "revision"
                ):
                    first_token_ms = (time.perf_counter() - request_started) * 1000
                # 再原样转发给前端（前端据此实时渲染工具徽标、逐字回答等）。
                yield _sse(event)
                # 用户中途断开：立刻停止，不再烧 token
                # 【省钱关键】大模型按 token 计费，用户关了页面还继续生成就是白花钱。
                # 这里每个事件检查一次，一断开就跳出循环停止生成。
                if await request.is_disconnected():
                    disconnected = True
                    break
        except Exception as exc:  # noqa: BLE001 - 流式响应里出错只能以事件形式告知前端
            # 同样：流已开始，错误必须以事件形式发出（同 /api/chat）。
            yield _sse({"type": "error", "message": f"服务异常：{exc}"})

        # done 时一次性写助手消息（含来源/工具/规划/自评等元数据）；
        # 断线或没有正文时不写，避免落半截数据
        # 【三个条件缺一不可】① 有会话（说明落库通道可用）；
        # ② 用户没断线（断线时正文可能是残缺的，存下来会误导）；
        # ③ 有实际回答正文（空回答没有保存价值）。
        answer = collector.answer_text()
        if conversation is not None and not disconnected and answer:
            try:
                # 把助手这条回答 + 全部元数据写进历史。
                # collector.meta(mode) 里装着工具调用、来源、SQL/Cypher、规划、自评等，
                # 前端下次打开历史时靠它还原出与当时一致的界面。
                await asyncio.to_thread(
                    history_store.append_message,
                    s,
                    user["id"],
                    conversation["id"],
                    "assistant",
                    answer,
                    collector.meta(mode),
                    mode,
                )
            except Exception:  # noqa: BLE001
                # 这里连状态都不用标记：落库失败对用户已收到的回答毫无影响，静默即可。
                pass

        # 【写缓存前修正 web 口径（缓存命中率修复）】cacheable 是请求开头按「是否允许联网」
        # 预判的（宁可保守）；联网内容的时效性顾虑只对「真的用了联网结果」的答案成立。
        # 允许联网但本轮没用（collector.web 为 None）→ 答案是纯知识库答案，无时效性问题
        # → 把当初被误否决的可缓存判定按 web_search=False 重算一遍（其余闸门照旧）。
        writable = cacheable
        if not writable and use_web and collector.web is None:
            writable = answer_cache.is_cacheable(
                logged_in=bool(user),
                mode=mode,
                has_history=bool(history),
                web_search=False,
                has_image=False,
                question=payload.question,
            )
        # 【done 之后写缓存】仅在「可缓存 + 未断线 + 有实际正文」时写。
        # 【为什么要求未断线】断线时正文可能是残缺的，缓存残缺答案会误导后续用户。
        # 写失败静默（answer_cache 内部已兜底），绝不打断已完成的问答。
        if writable and not disconnected and answer:
            # key_mode=mode：进缓存键的是「请求模式」（react/plan）；collector.done_mode
            # （agent_rag 等结束模式）只进 mode 列回放。与上面 get_cached 同一套键。
            answer_cache.set_cached(
                payload.question, answer, collector.sources, collector.done_mode or mode, s,
                key_mode=mode, top_k=payload.top_k,
            )

        # 记指标 + 打日志（不产生 SSE 事件，不影响事件顺序）。
        _finalize_qa(
            settings=s,
            question=payload.question,
            mode=collector.done_mode or mode,
            started=request_started,
            first_token_ms=first_token_ms,
            sources_count=len(collector.sources),
            cache_hit=False,
            rerank_applied=collector.rerank_applied,
            rerank_failed=collector.rerank_failed,
            rerank_skipped=collector.rerank_skipped,
        )

        # 同 /api/chat：流末尾发 [DONE] 表示结束。
        yield "data: [DONE]\n\n"

    # 与 /api/chat 完全相同的响应头设置（原因见上面注释）。
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 Nginx 之类的缓冲，保证逐字输出
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 重建知识库
# --------------------------------------------------------------------------- #
# 触发「重建知识库」：把 MySQL 三张表重新导入向量库（耗时，故为后台任务）。
# 【为什么用后台线程 + 状态牌，而不是直接做完再返回】
# 建库可能要几分钟，HTTP 请求等这么久早被前端/浏览器判为超时了。
# 所以这里「立刻返回已启动」，真正干活另开线程；前端再定期来查进度。
# 【注意】必须用锁「检查 + 占用」一体化：如果先解锁再启动，
# 两个请求可能同时通过「没在跑」的检查，导致两个建库线程并发抢同一个向量库目录（会锁死/损坏）。
# 【为什么鉴权】重建知识库会整库重灌、长时间独占向量库目录，属高危运维操作；
#   必须登录后才能触发，避免被匿名请求随意触发而打断线上服务。
@app.post("/api/ingest")
def start_ingest(payload: IngestRequest, user: dict[str, Any] = Depends(auth.current_user)) -> dict[str, Any]:
    # 【参数校验：mode 与 recreate 必须成对】两种「自相矛盾」的组合都直接 400，
    # 规则也写进了 README「建知识库」与 docs/FEATURES-admin-console.md：
    #
    # ① 增量 + 重建（mode=incremental, recreate=true）：
    #    增量的前提是保留旧向量库数据，先清空再增量在语义上自相矛盾
    #    （recreate 会把旧数据清掉，增量就失去意义）→ 拒绝，避免误传清空数据。
    if payload.mode == "incremental" and payload.recreate:
        raise HTTPException(
            status_code=400,
            detail="增量不能重建集合：mode=incremental 时请传 recreate=false",
        )
    # ② 全量 + 不重建（mode=full, recreate=false）：
    #    全量模式的管道只「往里灌」，既不算差集也不删除（差集是增量专属），
    #    recreate=false 时源表已删的记录会变成僵尸点永久留在向量库里继续被检索。
    #    要么走增量（自带差集删除），要么全量重建集合 —— 这里选拒绝并说明原因。
    if payload.mode == "full" and not payload.recreate:
        raise HTTPException(
            status_code=400,
            detail=(
                "全量必须重建集合（recreate=true），否则已删除的数据不会清理、"
                "会以僵尸点残留并继续被检索；若只想同步变化，请改用 mode=incremental"
            ),
        )
    with _state_lock:
        # 已有任务在跑就直接拒绝（409 = 冲突），避免并发建库互相破坏数据。
        if _ingest_state["running"]:
            raise HTTPException(status_code=409, detail="已有建库任务在运行中")
        # 一次性把状态牌「重置为刚启动的样子」（清掉上一轮的日志与结果）。
        _ingest_state.update(
            running=True,
            stage="start",
            message="任务已启动",
            logs=[],
            result=None,
            error=None,
            started_at=time.time(),
            finished_at=None,
        )

    # 创建后台线程。参数解释：
    # target=_ingest_worker     —— 这个线程要执行哪个函数；
    # args=(payload.mode, payload.recreate) —— 传给它的参数（顺序与函数签名一致：
    #                              先是 mode，再是 recreate）；
    # daemon=True               —— 守护线程：主程序退出时它会被一起结束，
    #                              不会出现「服务关了但线程还在跑」的情况。
    thread = threading.Thread(
        target=_ingest_worker, args=(payload.mode, payload.recreate), daemon=True
    )
    # 启动线程后立刻往下走（不等它跑完），所以这个接口返回得非常快。
    thread.start()
    return {"started": True, "message": "建库任务已在后台启动，请轮询 /api/ingest/status"}


# 查询建库进度。前端每隔一两秒调一次，拿到日志与阶段就刷新界面。
# 【为什么用 json.loads(json.dumps(...))】这是个「深拷贝 + 序列化安全检查」的小技巧：
#   · json.dumps 先把状态字典转成 JSON 文本（此时若有不能序列化的对象，default=str 兜底成字符串）；
#   · json.loads 再把文本还原成全新的字典。
#   好处：① 复制出一份副本，之后建库线程改原字典也不影响正在返回的这份，避免「边读边改」；
#         ② 保证返回值一定能被 FastAPI 序列化（不会因为混进了特殊对象而报错）。
@app.get("/api/ingest/status")
def ingest_status() -> dict[str, Any]:
    with _state_lock:
        return json.loads(json.dumps(_ingest_state, ensure_ascii=False, default=str))


# 可观测性指标汇总：返回问答总数、平均/最大耗时、平均首字延迟、模式分布、
# 缓存命中率、重排应用率。数字全部来自 metrics.db，读失败时返回一份空统计（不报错）。
# 【部署加固】指标属于运营数据，部署后不对外裸露：加 current_user 依赖，未登录返回 401；
#   /api/health 保持公开（容器探活 / 部署健康检查要用，不能一起收进门禁）。
@app.get("/api/metrics")
def metrics_endpoint(user: dict[str, Any] = Depends(auth.current_user)) -> dict[str, Any]:
    """查看问答运行指标（见 ``metrics.summary``）。需要登录。"""
    return metrics_store.summary(get_settings())


# 根路径：浏览器直接访问 http://127.0.0.1:8000/ 时给一份「有什么接口」的清单，
# 方便人手确认「服务起来了、都有哪些接口」。
# `/docs` 是 FastAPI 自动生成的交互式接口文档页，可以在网页上直接点按钮试接口。
@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "招投标智能问答机器人 API",
        "docs": "/docs",
        # 纯文字罗列全部接口（方法 + 路径），只是给人看的说明，没有程序逻辑依赖它。
        "endpoints": [
            "GET  /api/health",
            "GET  /api/config",
            "GET  /api/mysql/schema",
            "GET  /api/pg/schema",
            "GET  /api/graph/schema",
            "POST /analyze-image",
            "GET  /api/kb/stats",
            "GET  /api/kb/samples",
            "GET  /api/retrieve?q=",
            "POST /api/chat",
            "POST /api/agent/chat",
            "GET  /api/metrics",
            "POST /api/auth/register",
            "POST /api/auth/login",
            "POST /api/auth/logout",
            "GET  /api/auth/me",
            "GET  /api/history/conversations",
            "POST /api/history/conversations",
            "GET  /api/history/conversations/{id}",
            "PATCH /api/history/conversations/{id}",
            "DELETE /api/history/conversations/{id}",
            "GET  /api/history/search",
            "POST /api/ingest",
            "GET  /api/ingest/status",
        ],
    }


# 【启动入口】只有「直接运行本文件」时才会执行这里（被 uvicorn 导入时不会执行）。
# 【__name__ 是什么】每个 Python 文件都有个 __name__ 变量：
#   直接被运行时它的值是 "__main__"；被别人 import 时它的值是模块名（这里是 "app.main"）。
#   所以这句判断的含义是「我是被直接跑的，不是被导入的」——
#   这样既能 `python -m app.main` 启动，也能 `uvicorn app.main:app` 启动，两不耽误。
if __name__ == "__main__":
    # 延迟导入：只有真要走这条启动路径时才 import uvicorn，避免无谓的启动开销。
    import uvicorn

    # 启动 Web 服务器：
    #   "app.main:app" 指向「本模块里的 app 对象」；
    #   host=127.0.0.1 表示只监听本机（局域网其它机器访问不到，本地开发更安全）；
    #   port=8000 与前端默认请求地址对应；
    #   reload=False 关闭「改代码自动重启」—— 开发时可自行改成 True，但会更吃资源。
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)