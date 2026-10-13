
"""main：FastAPI 应用入口（全部 HTTP / SSE 端点与中间件挂载）。

职责：
  - 问答端点（/api/chat、/api/agent/chat）的 SSE 流式下发 + 缓存 + 限流
  - 知识库、检索、建库、鉴权、历史、指标等 REST 端点
  - 图片解析、异常到状态码的统一映射、应用启动初始化

依赖：agent、rag、retriever、rerank、ingest、vector_store、graph、pg、vision、web_search、
      auth、history、answer_cache、metrics、ratelimit、logging_setup、kb_version、citation、config
被谁调用：uvicorn（app.main:app）
"""

from __future__ import annotations

import asyncio

import json

import threading

import time

from contextlib import contextmanager, suppress

from typing import Any, AsyncIterator, Iterator, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile

from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import StreamingResponse

from pydantic import BaseModel, Field

from . import auth, history as history_store
from . import ratelimit
from .agent import MODES, TOOL_NAME, agent_events

from .config import Settings, get_settings

from .db import DatabaseUnavailable, discover_schema, list_tables, mysql_connection

from .graph import GRAPH_TOOL_NAME, GraphError, load_schema, render_schema_text

from .ingest import run_ingest

from .pg import PG_TOOL_NAME, PgError, load_schema as load_pg_schema, render_schema_text as render_pg_schema_text

from .rag import answer_events

from .rerank import rerank_hits

from .retriever import retrieve

from .vector_store import VectorStoreError, get_store

from .vision import ImageRejected, ImageTooLarge, ImageUnsupported, VisionError

from .vision import analyze_image as run_analyze_image
from .vision import check_size as check_image_size
from .vision import resolve_mime as resolve_image_mime
from .vision import verify_content as verify_image_content

from .web_search import TOOL_NAME as WEB_SEARCH_TOOL_NAME

from . import answer_cache

from . import metrics as metrics_store

from .logging_setup import get_logger, setup_logging

from .kb_version import kb_version

app = FastAPI(
    title="招投标智能问答机器人 API",
    description="MySQL → Qdrant → 混合检索（向量 + BM25）→ 大模型问答，回答附带参考来源。",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

setup_logging(get_settings())
logger = get_logger(__name__)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, description="用户问题")
    top_k: int | None = Field(None, ge=1, le=50, description="最终送入大模型的参考资料条数")


class IngestRequest(BaseModel):
    recreate: bool = Field(False, description="是否清空后重建；仅全量模式允许为 true")
    mode: Literal["full", "incremental"] = Field(
        "full", description="建库模式：full（全量）/ incremental（增量）"
    )


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"] = Field(..., description="发言方")
    content: str = Field(..., description="该轮文本")


class AgentChatRequest(BaseModel):
    question: str = Field(..., min_length=1, description="用户问题")
    top_k: int | None = Field(None, ge=1, le=50, description="检索后最终送入大模型的参考资料条数")
    history: list[ChatTurn] | None = Field(
        None, description="最近几轮对话（由前端持有并回传，后端无状态），按 AGENT_HISTORY_TURNS 截断"
    )
    web_search: bool | None = Field(
        None,
        description="是否允许联网搜索（前端「联网搜索」开关）；不传则用 WEB_SEARCH_DEFAULT，"
        "后端未配置 TAVILY_API_KEY 时该开关无效",
    )
    mode: Literal["react", "plan", "deep"] = Field(
        "react", description="推理模式：react（现状单轮）/ plan（Plan-and-Solve）/ deep（深度思考 = plan + Reflection）"
    )
    conversation_id: str | None = Field(
        None, description="历史会话 id；前端首次提问可不带（后端自动新建并随 conversation 事件返回）"
    )


class AuthRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, description="用户名（3~32 位中英文/数字/下划线）")
    password: str = Field(..., min_length=1, max_length=128, description="口令（至少 8 位）")


class ConversationCreate(BaseModel):
    title: str | None = Field(None, max_length=200, description="会话标题；缺省用「新对话」")
    mode: Literal["react", "plan", "deep"] = Field("react", description="该会话的推理模式")


class ConversationRename(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="新标题")


class _EventCollector:
    """边转发 SSE 边汇总本轮问答的关键信息，供 ``done`` 时一次写入对话历史。"""

    def __init__(self) -> None:
        self.tool_calls: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []
        self.sources: list[dict[str, Any]] = []
        self.rerank_applied = False
        self.rerank_failed = False
        self.rerank_skipped = False
        self.sql: dict[str, Any] | None = None
        self.graph: dict[str, Any] | None = None
        self.web: dict[str, Any] | None = None
        self.plan: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.reflections: list[dict[str, Any]] = []
        self.answer: list[str] = []
        self.revision: list[str] = []
        self.done_mode: str | None = None
        self.error: str | None = None

    def feed(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "tool_call":
            self.tool_calls.append({"name": event.get("name"), "query": event.get("query")})
        elif etype == "tool_result":
            self.tool_results.append({"name": event.get("name"), "count": event.get("count")})
        elif etype == "retrieval":
            meta = event.get("meta") or {}
            self.rerank_applied = bool(meta.get("rerank_applied"))
            self.rerank_failed = bool(meta.get("rerank_error"))
            self.rerank_skipped = bool(meta.get("rerank_skipped"))
        elif etype == "sources":
            self.sources = event.get("sources") or []
        elif etype == "sql_query":
            self.sql = {
                "sql": event.get("sql"),
                "row_count": event.get("row_count"),
                "error": event.get("error"),
            }
        elif etype == "graph_query":
            self.graph = {
                "cypher": event.get("cypher"),
                "row_count": event.get("row_count"),
                "error": event.get("error"),
            }
        elif etype == "web_search":
            self.web = {
                "query": event.get("query"),
                "results": event.get("results"),
                "error": event.get("error"),
            }
        elif etype == "plan":
            self.plan = event.get("steps") or []
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
        elif etype == "reflection":
            self.reflections.append(
                {
                    "round": event.get("round"),
                    "verdict": event.get("verdict"),
                    "issues": event.get("issues") or [],
                }
            )
        elif etype == "delta":
            text = event.get("text") or ""
            if (event.get("channel") or "answer") == "revision":
                self.revision.append(text)
            else:
                self.answer.append(text)
        elif etype == "error":
            self.error = event.get("message")
        elif etype == "done":
            self.done_mode = event.get("mode")

    def answer_text(self) -> str:
        return "".join(self.answer).strip()

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
            "revision": "".join(self.revision).strip() or None,
            "done_mode": self.done_mode,
            "error": self.error,
        }


_state_lock = threading.Lock()
_ingest_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "message": "",
    "logs": [],
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}


def _update_state(**fields: Any) -> None:
    with _state_lock:
        _ingest_state.update(fields)


def _append_log(stage: str, message: str, **_: Any) -> None:
    with _state_lock:
        _ingest_state["stage"] = stage
        _ingest_state["message"] = message
        logs = _ingest_state["logs"]
        logs.append(f"[{time.strftime('%H:%M:%S')}][{stage}] {message}")
        if len(logs) > 200:
            del logs[:-200]


def _ingest_worker(mode: str, recreate: bool) -> None:
    try:
        result = run_ingest(recreate=recreate, progress=_append_log, mode=mode)
        _update_state(result=result, error=None)
    except DatabaseUnavailable as exc:
        _update_state(error=f"数据库不可用：{exc}")
    except VectorStoreError as exc:
        _update_state(error=f"向量库错误：{exc}")
    except Exception as exc:  # noqa: BLE001 - 后台线程必须兜住所有异常
        _update_state(error=f"建库失败：{exc}")
    finally:
        _update_state(running=False, stage="idle", finished_at=time.time())


@app.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    try:
        store_stats = get_store(s).stats()
    except VectorStoreError as exc:
        store_stats = {"error": str(exc)}
    return {
        "status": "ok",
        "llm_configured": s.llm_configured,
        "llm_model": s.llm_model or None,
        "llm_model_display": s.llm_display_name or None,
        "rerank_configured": s.rerank_configured,
        "rerank_model": s.rerank_model or s.llm_model or None,
        "agent_configured": s.agent_configured,
        "agent_model": s.agent_model_or_default or None,
        "mysql_configured": s.mysql_configured,
        "kb_source": "postgres" if s.kb_source_is_postgres else "mysql",
        "pg_configured": s.pg_configured,
        "pg_model": s.pg_model_or_default or None,
        "graph_configured": s.graph_configured,
        "graph_model": s.graph_model_or_default or None,
        "web_search_configured": s.web_search_configured,
        "web_search_provider": "tavily" if s.web_search_configured else None,
        "web_search_default": s.web_search_default,
        "vision_configured": s.vision_configured,
        "history_enabled": s.history_enabled,
        "history_configured": s.history_configured,
        "auth_enabled": s.auth_enabled,
        "modes": list(MODES),
        "index": store_stats,
        "ingesting": _ingest_state["running"],
        "kb_version": kb_version(s),
    }


@app.get("/api/config")
def read_config(user: dict[str, Any] = Depends(auth.current_user)) -> dict[str, Any]:
    """回显当前生效配置，方便排查「.env 没生效」类问题。口令一律不回显。需要登录。"""
    s: Settings = get_settings()
    return {
        "mysql": {
            "user": s.mysql_user,
            "password_set": bool(s.mysql_password),
            "charset": s.mysql_charset,
        },
        "qdrant": {
            "mode": "embedded-local",
            "collection": s.qdrant_collection,
        },
        "embedding": {"model": s.embed_model, "dim": s.embed_dim},
        "bm25": {"k1": s.bm25_k1, "b": s.bm25_b},
        "retrieval": {
            "dense_top_k": s.retrieve_dense_top_k,
            "sparse_top_k": s.retrieve_sparse_top_k,
            "final_top_k": s.retrieve_final_top_k,
            "rrf_k": s.rrf_k,
            "dense_min_score": s.dense_min_score,
        },
        "rerank": {
            "enabled": s.rerank_configured,
            "model": s.rerank_model or s.llm_model or None,
            "candidates": s.rerank_candidates,
            "max_chars": s.rerank_max_chars,
            "timeout": s.rerank_timeout,
        },
        "llm": {
            "configured": s.llm_configured,
            "model": s.llm_model or None,
            "model_display": s.llm_display_name or None,
            "temperature": s.llm_temperature,
        },
        "agent": {
            "framework": "langchain-langgraph",
            "enabled": s.agent_configured,
            "model": s.agent_model_or_default or None,
            "max_tokens": s.agent_max_tokens,
            "history_turns": s.agent_history_turns,
            "max_steps": s.agent_max_steps,
            "modes": list(MODES),
            "deep": {
                "max_steps": s.deep_max_steps,
                "step_max_steps": s.deep_step_max_steps,
                "reflect_rounds": s.deep_reflect_rounds,
                "max_tokens": s.deep_max_tokens,
                "timeout": s.deep_timeout,
                "observation_chars": s.deep_observation_chars,
            },
            "tools": [TOOL_NAME]
            + ([PG_TOOL_NAME] if s.pg_configured else [])
            + ([GRAPH_TOOL_NAME] if s.graph_configured else [])
            + ([WEB_SEARCH_TOOL_NAME] if s.web_search_configured else []),
        },
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
        "neo4j": {
            "configured": s.graph_configured,
            "password_set": bool(s.neo4j_password),
            "model": s.graph_model_or_default or None,
            "max_rows": s.neo4j_max_rows,
            "timeout": s.neo4j_timeout,
            "schema_ttl": s.graph_schema_ttl,
            "tools": [GRAPH_TOOL_NAME],
        },
        "web_search": {
            "configured": s.web_search_configured,
            "provider": "tavily" if s.web_search_configured else None,
            "api_key_set": bool(s.tavily_api_key),
            "search_depth": s.tavily_search_depth,
            "max_results": s.tavily_max_results,
            "timeout": s.tavily_timeout,
            "default_enabled": s.web_search_default,
            "tools": [WEB_SEARCH_TOOL_NAME] if s.web_search_configured else [],
        },
        "vision": {
            "configured": s.vision_configured,
            "model": s.vision_model,
            "timeout": s.vision_timeout,
            "max_bytes": s.vision_max_bytes,
        },
        "history": {
            "enabled": s.history_enabled,
            "configured": s.history_configured,
            "storage": "sqlite",
            "page_size": s.history_page_size,
            "search_limit": s.history_search_limit,
            "title_max_chars": s.history_title_max_chars,
            "max_message_chars": s.history_max_message_chars,
            "fts_available": history_store.fts_available(s) if s.history_configured else False,
            "counts": history_store.stats(s),
        },
        "auth": {
            "enabled": s.auth_enabled,
            "lockout_seconds": s.auth_lockout_seconds,
        },
    }


@app.get("/api/mysql/schema")
def mysql_schema() -> dict[str, Any]:
    s = get_settings()
    try:
        with mysql_connection(s) as conn:
            schema = discover_schema(conn, s.kb_tables)
            all_tables = list_tables(conn)
    except DatabaseUnavailable as exc:
        return {
            "connected": False,
            "error": str(exc),
            "database": s.mysql_database,
            "tables": {t: {"table": t, "label": s.label_of(t), "exists": False} for t in s.kb_tables},
            "available_tables": [],
        }

    for table, info in schema.items():
        info["label"] = s.label_of(table)
    return {
        "connected": True,
        "error": None,
        "database": s.mysql_database,
        "tables": schema,
        "available_tables": all_tables,
        "total_rows": sum(int(i.get("row_count", 0)) for i in schema.values()),
    }


@app.get("/api/pg/schema")
def pg_schema(refresh: bool = Query(False, description="忽略缓存，强制重新读取库结构")) -> dict[str, Any]:
    """回显 Agent 生成 SQL 时用到的库结构（表/字段/注释/近似行数/样例值）。"""
    s = get_settings()
    empty = {
        "connected": False,
        "database": s.pg_database,
        "schema": s.pg_schema,
        "tables": [],
        "schema_text": "",
    }
    if not s.pg_configured:
        return {**empty, "error": "未配置 PostgreSQL（PG_HOST / PG_USER / PG_PASSWORD / PG_DATABASE）"}
    try:
        data = load_pg_schema(s, refresh=refresh)
    except PgError as exc:
        return {**empty, "error": str(exc)}
    return {
        "connected": True,
        "error": None,
        "database": data.get("database"),
        "schema": data.get("schema"),
        "tables": data.get("tables") or [],
        "schema_text": render_pg_schema_text(data),
    }


@app.get("/api/graph/schema")
def graph_schema(refresh: bool = Query(False, description="忽略缓存，强制重新读取图谱结构")) -> dict[str, Any]:
    """回显 Agent 生成 Cypher 时用到的图谱结构（标签/关系/三元组/属性/计数）。"""
    s = get_settings()
    empty = {
        "connected": False,
        "database": s.neo4j_database,
        "labels": [],
        "relationship_types": [],
        "triples": [],
        "nodes": [],
        "schema_text": "",
    }
    if not s.graph_configured:
        return {**empty, "error": "未配置 Neo4j（NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD）"}
    try:
        data = load_schema(s, refresh=refresh)
    except GraphError as exc:
        return {**empty, "error": str(exc)}
    return {
        "connected": True,
        "error": None,
        "database": data.get("database"),
        "labels": data.get("labels") or [],
        "relationship_types": data.get("relationship_types") or [],
        "triples": data.get("triples") or [],
        "nodes": data.get("nodes") or [],
        "schema_text": render_schema_text(data),
    }


@contextmanager
def _auth_errors() -> Iterator[None]:
    try:
        yield
    except auth.AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except auth.RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except auth.Unauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except history_store.HistoryConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@contextmanager
def _history_errors() -> Iterator[None]:
    try:
        yield
    except history_store.HistoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except history_store.HistoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/auth/register")
async def auth_register(payload: AuthRequest, user_agent: str | None = Header(None)) -> dict[str, Any]:
    """注册并直接返回令牌（省掉一次登录往返）。口令只以 PBKDF2 哈希落库。"""
    s = get_settings()
    with _auth_errors():
        return await asyncio.to_thread(auth.register, s, payload.username, payload.password, user_agent)


@app.post("/api/auth/login")
async def auth_login(payload: AuthRequest, user_agent: str | None = Header(None)) -> dict[str, Any]:
    s = get_settings()
    with _auth_errors():
        return await asyncio.to_thread(auth.login, s, payload.username, payload.password, user_agent)


@app.post("/api/auth/logout")
async def auth_logout(
    user: dict[str, Any] = Depends(auth.current_user),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    s = get_settings()
    with _auth_errors():
        await asyncio.to_thread(auth.logout, s, auth.require_bearer(authorization))
    return {"ok": True, "username": user["username"]}


@app.get("/api/auth/me")
def auth_me(user: dict[str, Any] = Depends(auth.current_user)) -> dict[str, Any]:
    return {"user": user}


@app.get("/api/history/conversations")
async def history_conversations(
    limit: int = Query(0, ge=0, le=100, description="每页条数；0 表示用 HISTORY_PAGE_SIZE"),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, description="按会话标题（对话主题）关键词过滤"),
    mode: Literal["react", "plan", "deep"] | None = Query(None, description="按该会话的推理模式过滤"),
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    """会话列表，按最近更新倒序。只返回当前登录用户自己的会话。"""
    s = get_settings()
    page = limit or s.history_page_size
    with _history_errors():
        items, total = await asyncio.to_thread(
            history_store.list_conversations, s, user["id"], page, offset, q, mode
        )
    return {"items": items, "total": total, "limit": page, "offset": offset}


@app.post("/api/history/conversations")
async def history_create_conversation(
    payload: ConversationCreate,
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    s = get_settings()
    title = payload.title or history_store.DEFAULT_CONVERSATION_TITLE
    with _history_errors():
        conversation = await asyncio.to_thread(
            history_store.create_conversation, s, user["id"], title, payload.mode
        )
    return {"conversation": conversation}


@app.get("/api/history/conversations/{conversation_id}")
async def history_conversation_detail(
    conversation_id: str,
    limit: int = Query(0, ge=0, le=2000, description="只取最近 N 条；0 表示全部"),
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    """会话详情：含全部消息与元数据（来源、工具调用、SQL / Cypher / 联网结果、规划与自评）。"""
    s = get_settings()
    with _history_errors():
        conversation = await asyncio.to_thread(history_store.get_conversation, s, user["id"], conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        messages = await asyncio.to_thread(
            history_store.list_messages, s, user["id"], conversation_id, limit or None
        )
    return {"conversation": conversation, "messages": messages}


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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"conversation": conversation}


@app.delete("/api/history/conversations/{conversation_id}")
async def history_delete_conversation(
    conversation_id: str,
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    s = get_settings()
    with _history_errors():
        deleted = await asyncio.to_thread(history_store.delete_conversation, s, user["id"], conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "id": conversation_id}


@app.get("/api/history/search")
async def history_search(
    q: str | None = Query(None, description="关键词（jieba 分词后走 FTS5，同时匹配消息正文与会话标题）"),
    date_from: str | None = Query(None, alias="from", description="起始时间，如 2026-09-01 或 2026-09-01 08:00:00"),
    date_to: str | None = Query(None, alias="to", description="结束时间，只给日期时自动补到当天 23:59:59"),
    mode: Literal["react", "plan", "deep"] | None = Query(None, description="按该会话的推理模式过滤"),
    limit: int = Query(0, ge=0, le=100, description="每页条数；0 表示用 HISTORY_SEARCH_LIMIT"),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    """历史检索：关键词 + 时间范围 + 主题/模式，三个维度可任意组合。"""
    s = get_settings()
    page = limit or s.history_search_limit
    with _history_errors():
        items, total = await asyncio.to_thread(
            history_store.search_messages, s, user["id"], q, date_from, date_to, mode, page, offset
        )
    return {"items": items, "total": total, "limit": page, "offset": offset, "fts": history_store.fts_available(s)}


@app.post("/analyze-image")
async def analyze_image(
    file: UploadFile = File(..., description="待解析的图片（jpg / png / webp；大小上限见配置 VISION_MAX_BYTES，默认 10MB）"),
    question: str = Form("", description="可选：随图片一起提交的提问文字"),
    user: dict[str, Any] = Depends(auth.current_user),
) -> dict[str, Any]:
    """图片解析：把图片交给智谱视觉模型识别成结构化文本。"""
    s = get_settings()
    data = await file.read()
    try:
        mime = resolve_image_mime(file.filename, file.content_type)
        check_image_size(len(data), s)
    except ImageTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ImageUnsupported as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ImageRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        verify_image_content(data, mime)
    except ImageUnsupported as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ImageRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = await asyncio.to_thread(run_analyze_image, data, mime, question, s)
    except VisionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "text": result["text"],
        "model": result.get("model"),
        "filename": file.filename,
        "mime": mime,
        "size": len(data),
    }


@app.get("/api/kb/stats")
def kb_stats() -> dict[str, Any]:
    s = get_settings()
    try:
        stats = get_store(s).stats()
    except VectorStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    stats["bm25_docs"] = _bm25_doc_count(s)
    stats["embed_model"] = s.embed_model
    stats["table_labels"] = {t: s.label_of(t) for t in s.kb_tables}
    return stats


def _bm25_doc_count(s: Settings) -> int:
    from .retriever import load_bm25

    try:
        return load_bm25(s).n_docs
    except Exception:
        return 0


@app.get("/api/kb/samples")
def kb_samples(limit: int = Query(5, ge=1, le=50)) -> dict[str, Any]:
    s = get_settings()
    try:
        return {"samples": get_store(s).sample(limit)}
    except VectorStoreError as exc:
        logger.warning("知识库样本读取失败（向量库）：%s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/retrieve")
def retrieve_only(
    q: str = Query(..., min_length=1, description="查询语句"),
    top_k: int | None = None,
    rerank: bool = Query(False, description="是否调用大模型二次排序（默认关闭，保持纯检索零成本）"),
    _limit: None = Depends(ratelimit.rate_limit),
) -> dict[str, Any]:
    """只走混合检索，不调用大模型 —— 调参和排查检索质量时用。"""
    s = get_settings()
    want_k = top_k or s.retrieve_final_top_k
    pool_k = max(want_k, s.rerank_candidates) if rerank else want_k

    try:
        result = retrieve(q, settings=s, final_k=pool_k)
    except VectorStoreError as exc:
        logger.warning("检索失败（向量库）q=%r：%s", q, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    rerank_meta: dict[str, Any] = {}
    if rerank:
        hits, rerank_meta = rerank_hits(q, result.hits, s, top_k=want_k)
        result.hits = hits

    return {
        "question": q,
        "dense_count": result.dense_count,
        "sparse_count": result.sparse_count,
        "bm25_ready": result.bm25_ready,
        "rerank": rerank_meta,
        "sources": result.to_sources(text_limit=300),
    }


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
    """问答收尾的统一动作：记一条指标 + 打一行日志（两个问答入口共用）。"""
    elapsed_ms = (time.perf_counter() - started) * 1000
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


@app.post("/api/chat")
def chat(
    payload: ChatRequest,
    user: dict[str, Any] | None = Depends(auth.optional_user),
    _limit: None = Depends(ratelimit.rate_limit),
) -> StreamingResponse:
    s = get_settings()
    request_started = time.perf_counter()
    logged_in = bool(user)
    cacheable = answer_cache.is_cacheable(
        logged_in=logged_in,
        mode="llm",
        has_history=False,
        web_search=False,
        has_image=False,
        question=payload.question,
    )
    cached = (
        answer_cache.get_cached(payload.question, s, mode="llm", top_k=payload.top_k)
        if answer_cache.is_cache_readable(
            logged_in=logged_in, mode="llm", has_image=False, question=payload.question
        )
        else None
    )

    def event_stream() -> Iterator[str]:
        first_token_ms: float | None = None
        sources: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        done_mode = "llm"
        rerank_applied = False
        rerank_failed = False
        rerank_skipped = False

        if cached is not None:
            yield _sse({"type": "cache_hit"})
            yield _sse({"type": "sources", "sources": cached.get("sources") or []})
            yield _sse({"type": "delta", "text": cached.get("answer") or ""})
            yield _sse({"type": "done", "mode": cached.get("mode") or "llm"})
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

        try:
            for event in answer_events(payload.question, settings=s, final_k=payload.top_k):
                etype = event.get("type")
                if etype == "delta":
                    if (event.get("channel") or "answer") != "revision":
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
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001 - 流式响应里出错只能以事件形式告知前端
            yield _sse({"type": "error", "message": f"服务异常：{exc}"})

        answer_text = "".join(answer_parts).strip()
        if cacheable and answer_text:
            answer_cache.set_cached(
                payload.question, answer_text, sources, done_mode, s,
                key_mode="llm", top_k=payload.top_k,
            )

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
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/agent/chat")
async def agent_chat(
    payload: AgentChatRequest,
    request: Request,
    user: dict[str, Any] | None = Depends(auth.optional_user),
    _limit: None = Depends(ratelimit.rate_limit),
) -> StreamingResponse:
    """Agent 问答：由大模型自主决定是否调用知识库检索工具。"""
    s = get_settings()
    history = [turn.model_dump() for turn in payload.history] if payload.history else None
    mode = payload.mode if payload.mode in MODES else "react"

    persist = bool(user and s.history_configured)

    request_started = time.perf_counter()
    use_web = s.web_search_default if payload.web_search is None else bool(payload.web_search)
    use_web = bool(use_web and s.web_search_configured)
    cacheable = answer_cache.is_cacheable(
        logged_in=bool(user),
        mode=mode,
        has_history=bool(history),
        web_search=use_web,
        has_image=False,
        question=payload.question,
    )
    cached = (
        answer_cache.get_cached(payload.question, s, mode=mode, top_k=payload.top_k)
        if answer_cache.is_cache_readable(
            logged_in=bool(user), mode=mode, has_image=False, question=payload.question
        )
        else None
    )

    async def event_stream() -> AsyncIterator[str]:
        collector = _EventCollector()
        conversation: dict[str, Any] | None = None
        first_token_ms: float | None = None

        if persist:
            try:
                created = await asyncio.to_thread(
                    history_store.ensure_conversation, s, user["id"], payload.conversation_id, mode
                )
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
                conversation = (
                    await asyncio.to_thread(history_store.get_conversation, s, user["id"], created["id"])
                ) or created
            except Exception:  # noqa: BLE001 - 落库失败绝不影响问答
                conversation = None

        yield _sse(
            {
                "type": "conversation",
                "id": conversation["id"] if conversation else None,
                "title": conversation["title"] if conversation else None,
                "mode": mode,
                "saved": conversation is not None,
            }
        )

        if cached is not None:
            yield _sse({"type": "cache_hit"})
            yield _sse({"type": "sources", "sources": cached.get("sources") or []})
            yield _sse({"type": "delta", "text": cached.get("answer") or ""})
            yield _sse({"type": "done", "mode": cached.get("mode") or mode})

            if conversation is not None:
                try:
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

        disconnected = False
        try:
            async for event in agent_events(
                payload.question,
                settings=s,
                final_k=payload.top_k,
                history=history,
                web_search=payload.web_search,
                mode=mode,
            ):
                collector.feed(event)
                if (
                    first_token_ms is None
                    and event.get("type") == "delta"
                    and (event.get("channel") or "answer") != "revision"
                ):
                    first_token_ms = (time.perf_counter() - request_started) * 1000
                yield _sse(event)
                if await request.is_disconnected():
                    disconnected = True
                    break
        except Exception as exc:  # noqa: BLE001 - 流式响应里出错只能以事件形式告知前端
            yield _sse({"type": "error", "message": f"服务异常：{exc}"})

        answer = collector.answer_text()
        if conversation is not None and not disconnected and answer:
            try:
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
                pass

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
        if writable and not disconnected and answer:
            answer_cache.set_cached(
                payload.question, answer, collector.sources, collector.done_mode or mode, s,
                key_mode=mode, top_k=payload.top_k,
            )

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

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/ingest")
def start_ingest(payload: IngestRequest, user: dict[str, Any] = Depends(auth.current_user)) -> dict[str, Any]:
    if payload.mode == "incremental" and payload.recreate:
        raise HTTPException(
            status_code=400,
            detail="增量不能重建集合：mode=incremental 时请传 recreate=false",
        )
    if payload.mode == "full" and not payload.recreate:
        raise HTTPException(
            status_code=400,
            detail=(
                "全量必须重建集合（recreate=true），否则已删除的数据不会清理、"
                "会以僵尸点残留并继续被检索；若只想同步变化，请改用 mode=incremental"
            ),
        )
    with _state_lock:
        if _ingest_state["running"]:
            raise HTTPException(status_code=409, detail="已有建库任务在运行中")
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

    thread = threading.Thread(
        target=_ingest_worker, args=(payload.mode, payload.recreate), daemon=True
    )
    thread.start()
    return {"started": True, "message": "建库任务已在后台启动，请轮询 /api/ingest/status"}


@app.get("/api/ingest/status")
def ingest_status() -> dict[str, Any]:
    with _state_lock:
        return json.loads(json.dumps(_ingest_state, ensure_ascii=False, default=str))


@app.get("/api/metrics")
def metrics_endpoint(user: dict[str, Any] = Depends(auth.current_user)) -> dict[str, Any]:
    """查看问答运行指标（见 ``metrics.summary``）。需要登录。"""
    return metrics_store.summary(get_settings())


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "招投标智能问答机器人 API",
        "docs": "/docs",
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)