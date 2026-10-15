
"""agent：LangGraph ReAct Agent 编排（工具注册、事件流、react 对话模式）。

职责：
  - 把检索 / PostgreSQL / 图谱 / 联网搜索等工具注册进 ReAct 状态图
  - 流式产出工具调用轨迹与中间消息事件，供 SSE 下发
  - 组装 Agent 系统提示词并维护每轮请求状态快照
  - 步数上限用尽时强制收口（bind_tools tool_choice=none）逼出最终文字答案

依赖：rag、retriever、rerank、llm、graph、pg、web_search、citation、deep_agent、config
被谁调用：main（/api/agent/chat）、deep_agent
"""

from __future__ import annotations

import asyncio

from pathlib import Path

from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from langchain_core.runnables import RunnableConfig

from langchain_core.tools import tool

from langgraph.errors import GraphRecursionError

from langgraph.prebuilt import create_react_agent

from langchain_openai import ChatOpenAI

from . import metrics as metrics_store

from .config import Settings, get_settings

from .graph import GRAPH_TOOL_NAME, query_bidding_graph

from .llm import LLMError, stream_chat

from .logging_setup import get_logger

from .pg import PG_TOOL_NAME, query_bidding_postgres

from .rag import (
    NO_DATA_MESSAGE,
    SYSTEM_PROMPT,
    answer_events,
    build_messages,
    render_reference_blocks,
    retrieval_only_answer,
)

from .citation import strip_bad_refs, verify_citations

from .rerank import rerank_hits

from .retriever import retrieve

from .web_search import TOOL_NAME as WEB_TOOL_NAME

from .web_search import search_web_bidding

logger = get_logger(__name__)

TOOL_NAME = "search_bidding_knowledge"

SKILL_TOOL_NAME = "load_skill"

MODES = ("react", "plan", "deep")

# 业务技能文档目录（项目根 docs/skills/）。目录常驻提示词、正文按需 load_skill 读取。
_SKILLS_DIR = Path(__file__).resolve().parents[2] / "docs" / "skills"

# 目录扫描结果缓存（首次用到时扫描一次；目录不存在或为空时不注册 load_skill）。
_skills_catalog: list[dict[str, str]] | None = None


def _scan_skills() -> list[dict[str, str]]:
    """扫描技能目录，缓存「文件名（技能名）+ 一句话说明」目录。"""
    global _skills_catalog
    if _skills_catalog is not None:
        return _skills_catalog
    entries: list[dict[str, str]] = []
    if _SKILLS_DIR.is_dir():
        for path in sorted(_SKILLS_DIR.glob("*.md")):
            summary = ""
            for line in path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                # 跳过空行与 markdown 标题行，取第一个正文段落作一句话说明。
                if text and not text.startswith("#"):
                    summary = text[:80]
                    break
            entries.append({"name": path.stem, "summary": summary})
    _skills_catalog = entries
    return entries


@tool
def load_skill(name: str) -> str:
    """按需读取一份业务技能说明全文（字段口径、法规速查、回答自查清单等）。目录见系统提示词。"""
    catalog = {entry["name"]: entry for entry in _scan_skills()}
    entry = catalog.get((name or "").strip())
    if entry is None:
        available = "、".join(catalog) or "（当前没有可用技能）"
        return f"没有名为「{name}」的技能。当前可用技能：{available}。请用准确的技能名重试。"
    path = _SKILLS_DIR / f"{entry['name']}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 目录扫描到读取之间文件被移走的极端情况
        return f"技能「{name}」读取失败：{exc}。请直接依据已有知识回答，不要重试。"


def _agent_system_prompt(s: Settings, web_search_enabled: bool = False) -> str:
    """在 rag 的作答纪律之上，补充「什么时候该调哪个工具」。"""
    lines = [
        "工具使用规则：",
        "1. 问题涉及招标公告原文、招投标项目内容、公司信息、条款措辞、资质描述等「语义/原文」类内容时，"
        "先调用 search_bidding_knowledge 检索知识库，再依据它返回的参考资料作答。",
    ]
    next_no = 2
    if s.pg_configured:
        lines.append(
            f"{next_no}. 问题涉及统计、计数、求和/平均、排序 Top-N、精确条件筛选、多表关联等「结构化/精确」类内容时"
            "（例如「预算超过一千万的项目有几个」「按地区统计公告数量」「某公司中标金额合计」），"
            "调用 query_bidding_postgres 精确查询。"
        )
        next_no += 1
    if s.graph_configured:
        lines.append(
            f"{next_no}. 问题涉及实体之间的关联、多跳、路径、关系计数等「关系/图谱」类内容时"
            "（例如「有哪些关于空调的采购」「某公司中标了哪些项目」「某项目有哪些参与单位」"
            "「两家公司共同参与过哪些项目」），调用 query_bidding_graph 沿关系查询。"
        )
        next_no += 1
    if next_no > 3:
        lines.append(f"{next_no}. 需要多种视角时，把相应工具都调用，再综合作答。")
        next_no += 1
    lines += [
        f"{next_no}. 与招投标业务无关的闲聊、自我介绍、能力询问等，不要调用工具，直接回答，"
        "并简要说明你能查什么。",
        f"{next_no + 1}. 调用工具时可以按需改写检索词（例如补全项目类型、资质关键词），以便命中更准。",
        f"{next_no + 2}. 引用知识库来源时沿用 [参考资料N] 标注；表格类结论用 Markdown 表格呈现。",
    ]

    catalog = _scan_skills()
    if catalog:
        skill_lines = "\n".join(f"- {entry['name']}：{entry['summary']}" for entry in catalog)
        lines += [
            "",
            "业务技能（load_skill 工具，渐进式披露：这里只有目录，正文要用工具换取）：",
            "需要某份口径/规范的完整内容时（例如字段含义与统计口径、法规条文细节、回答自查清单），"
            "先调用 load_skill(name) 读取对应技能全文，再据此回答；不要凭记忆猜口径。",
            "可用技能目录：",
            skill_lines,
        ]
    if web_search_enabled:
        lines += [
            "",
            "联网搜索规则（search_web_bidding）：",
            "1. 本地知识库是存量数据。若 search_bidding_knowledge 返回「知识库中没有找到相关记录」，"
            "或问题明确要求「最新 / 近期 / 今年 / 最近发布」的招标公告、政府采购信息、法规政策更新，"
            "必须再调用 search_web_bidding 联网搜索，不要直接回「没有数据」。",
            "2. 联网结果比知识库新，但权威性需自行判断；引用时用 [网页N] 标注并给出链接，"
            "与知识库的 [参考资料N] 分开标注，不要把两者混为一谈。",
            "3. 联网搜索也失败或无结果时，如实说明「本地知识库与联网都没有查到」，绝不编造公告标题、链接或日期。",
        ]
    return SYSTEM_PROMPT + "\n\n" + "\n".join(lines)

_NO_HIT_NOTICE = (
    "检索结果：知识库中没有找到与该查询相关的记录。\n\n"
    "请直接向用户说明下方内容，不要编造任何项目名称、金额、日期或资质：\n"
    f"{NO_DATA_MESSAGE}"
)

_agent_cache: dict[tuple[Any, ...], Any] = {}


class ReasoningChatOpenAI(ChatOpenAI):
    """透传推理模型思维链的 ChatOpenAI。

    langchain_openai 基类不提取 ``delta.reasoning_content``（文档明说要用
    provider 子类），DeepSeek 的思维链会被整段丢掉、用户在思考期间干等。
    这里在 chunk → message 的转换点把 reasoning_content 挂进
    ``additional_kwargs``，同步/异步流式共用该转换，一处覆盖全链路。
    """

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is not None:
            choices = chunk.get("choices") or []
            if choices:
                reasoning = (choices[0].get("delta") or {}).get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation


def _make_chat_model(s: Settings) -> ChatOpenAI:
    """构造 Agent 用的对话模型（建图与强制收口共用同一套参数）。"""
    return ReasoningChatOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.agent_model_or_default,
        temperature=s.llm_temperature,
        max_tokens=s.agent_max_tokens,
        timeout=s.llm_timeout,
    )


def _collect_tools(s: Settings, use_web: bool) -> list[Any]:
    """按配置装配本轮可用的工具列表（建图与强制收口共用）。"""
    tools: list[Any] = [search_bidding_knowledge]
    # 技能目录非空才注册（与「未配置的工具不注册」一致：没有技能文件就不给模型这个工具）。
    if _scan_skills():
        tools.append(load_skill)
    if s.pg_configured:
        tools.append(query_bidding_postgres)
    if s.graph_configured:
        tools.append(query_bidding_graph)
    if use_web:
        tools.append(search_web_bidding)
    return tools


def _build_agent(s: Settings, web_search_enabled: bool = False) -> Any:
    """构造（并缓存）ReAct Agent。"""
    use_web = bool(web_search_enabled and s.web_search_configured)
    key = (
        s.llm_base_url,
        s.llm_api_key,
        s.agent_model_or_default,
        s.llm_temperature,
        s.agent_max_tokens,
        s.llm_timeout,
        s.pg_configured,
        s.graph_configured,
        use_web,
    )
    agent = _agent_cache.get(key)
    if agent is not None:
        return agent

    model = _make_chat_model(s)
    tools = _collect_tools(s, use_web)

    agent = create_react_agent(
        model,
        tools=tools,
        prompt=_agent_system_prompt(s, web_search_enabled=use_web),
    )
    _agent_cache[key] = agent
    return agent


def _run_retrieval(query: str, state: dict[str, Any]) -> tuple[str, int]:
    """执行「混合检索 + 重排」，返回（给模型看的参考资料文本, 命中条数）。"""
    s: Settings = state["settings"]
    want_k: int = state["final_k"]
    pool_k = max(want_k, s.rerank_candidates)

    result = retrieve(query, settings=s, final_k=pool_k)
    hits, rerank_meta = rerank_hits(query, result.hits, s, top_k=want_k)
    result.hits = hits
    result.rerank_meta = rerank_meta

    sources = result.to_sources()
    state["sources"] = sources
    state["meta"] = {
        "dense_count": result.dense_count,
        "sparse_count": result.sparse_count,
        "bm25_ready": result.bm25_ready,
        "final_count": len(sources),
        "llm_configured": s.llm_configured,
        **result.rerank_meta,
    }

    if not sources:
        return _NO_HIT_NOTICE, 0
    return render_reference_blocks(sources), len(sources)


def _snapshot_rag_state(state: dict[str, Any]) -> dict[str, Any]:
    """把 ``_rag_state`` 里的检索结果快照一份。"""
    return {
        "sources": list(state.get("sources") or []),
        "meta": dict(state.get("meta") or {}),
        "hit_count": int(state.get("hit_count") or 0),
    }


@tool
async def search_bidding_knowledge(query: str, config: RunnableConfig) -> str:
    """检索招投标知识库，返回与查询最相关的公告、项目与公司原文。"""

    state = (config.get("configurable") or {}).get("_rag_state")
    if state is None:
        state = {"settings": get_settings(), "final_k": get_settings().retrieve_final_top_k}
    text, hit_count = await asyncio.to_thread(_run_retrieval, query, state)
    state["hit_count"] = hit_count
    return text


def _trim_history(history: list[dict[str, Any]] | None, turns: int) -> list[dict[str, Any]]:
    """只保留最近 ``turns`` 轮（一问一答算一轮）；turns <= 0 表示不带历史。"""
    if not history or turns <= 0:
        return []
    return history[-turns * 2 :]


def _build_messages(
    question: str,
    history: list[dict[str, Any]] | None,
    s: Settings,
) -> list[Any]:
    """把历史对话与当前问题组装成 LangChain 消息列表。"""
    messages: list[Any] = []
    for turn in _trim_history(history, s.agent_history_turns):
        role = str(turn.get("role") or "")
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        messages.append(AIMessage(content=content) if role == "assistant" else HumanMessage(content=content))
    messages.append(HumanMessage(content=question))
    return messages


def _iter_update_messages(payload: Any) -> list[Any]:
    """从 ``updates`` 流的 payload 里取出本步新增的消息。"""
    out: list[Any] = []
    if not isinstance(payload, dict):
        return out
    for update in payload.values():
        if not isinstance(update, dict):
            continue
        for msg in update.get("messages") or []:
            out.append(msg)
    return out


def _new_turn_state() -> dict[str, Any]:
    """一次 ReAct 轮次的跨事件状态（供事件翻译与调用方共同读取）。

    transcript 按序记录本图执行的完整消息（AIMessage/ToolMessage），
    步数用尽触发强制收口时把它重放给模型，让最终答案基于已有工具观察。
    """
    return {
        "tool_called": False,
        "sources_emitted": False,
        "answer_parts": [],
        "transcript": [],
    }


def _citation_check(
    answer_text: str,
    sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """对累计正文做引用校验，返回（要追加的事件, 已校验文本）。"""
    if not sources:
        return [], answer_text
    try:
        check = verify_citations(answer_text, sources)
    except Exception:  # noqa: BLE001 - 校验失败绝不能把已经成功的问答搞挂
        return [], answer_text
    if check.get("ok"):
        return [], answer_text
    revised = strip_bad_refs(answer_text, check.get("bad_refs") or [])
    events = [
        {"type": "citation_check", "ok": False, "issues": check.get("issues") or []},
        {"type": "delta", "channel": "revision", "reason": "citation", "text": revised},
    ]
    return events, revised


def _new_states(s: Settings, final_k: int | None) -> dict[str, dict[str, Any]]:
    """构造一次问答所需的 4 个 per-request 回收字典。"""
    return {
        "_rag_state": {
            "settings": s,
            "final_k": final_k or s.retrieve_final_top_k,
            "sources": None,
            "meta": None,
            "hit_count": 0,
        },
        "_pg_state": {
            "settings": s,
            "sql": None,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": None,
        },
        "_graph_state": {
            "settings": s,
            "cypher": None,
            "rows": [],
            "row_count": 0,
            "error": None,
        },
        "_web_state": {
            "settings": s,
            "max_results": s.tavily_max_results,
            "query": None,
            "results": [],
            "error": None,
        },
    }


async def _stream_react_turn(
    agent: Any,
    stream_input: dict[str, Any],
    turn: dict[str, Any],
    states: dict[str, dict[str, Any]],
    *,
    question: str,
    recursion_limit: int,
    emit_sources: bool = True,
    emit_delta: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """消费一次 ReAct ``astream``，把工具消息翻译成既有事件。"""
    rag_state = states["_rag_state"]
    pg_state = states["_pg_state"]
    graph_state = states["_graph_state"]
    web_state = states["_web_state"]

    stream = agent.astream(
        stream_input,
        stream_mode=["messages", "updates"],
        config={
            "configurable": dict(states),
            "recursion_limit": recursion_limit,
        },
    )
    async for mode, payload in stream:
        if mode == "messages":
            chunk = payload[0] if isinstance(payload, tuple) else payload
            if isinstance(chunk, ToolMessage):
                if chunk.name == PG_TOOL_NAME:
                    yield {
                        "type": "sql_query",
                        "sql": pg_state.get("sql") or "",
                        "columns": pg_state.get("columns") or [],
                        "row_count": pg_state.get("row_count") or 0,
                        "rows": pg_state.get("rows") or [],
                        "error": pg_state.get("error"),
                    }
                    yield {
                        "type": "tool_result",
                        "name": PG_TOOL_NAME,
                        "count": pg_state.get("row_count") or 0,
                    }
                elif chunk.name == GRAPH_TOOL_NAME:
                    yield {
                        "type": "graph_query",
                        "cypher": graph_state.get("cypher") or "",
                        "row_count": graph_state.get("row_count") or 0,
                        "rows": graph_state.get("rows") or [],
                        "error": graph_state.get("error"),
                    }
                    yield {
                        "type": "tool_result",
                        "name": GRAPH_TOOL_NAME,
                        "count": graph_state.get("row_count") or 0,
                    }
                elif chunk.name == WEB_TOOL_NAME:
                    results = web_state.get("results") or []
                    yield {
                        "type": "web_search",
                        "query": web_state.get("query") or "",
                        "results": results,
                        "error": web_state.get("error"),
                    }
                    yield {
                        "type": "tool_result",
                        "name": WEB_TOOL_NAME,
                        "count": len(results),
                    }
                elif chunk.name == SKILL_TOOL_NAME:
                    # 技能读取是「换提示词正文」，不是检索：不触发 sources，结果按 1 份计。
                    yield {"type": "tool_result", "name": SKILL_TOOL_NAME, "count": 1}
                else:
                    if emit_sources and not turn["sources_emitted"]:
                        yield {"type": "retrieval", "meta": rag_state.get("meta") or {}}
                        yield {"type": "sources", "sources": rag_state.get("sources") or []}
                    turn["sources_emitted"] = True
                    yield {
                        "type": "tool_result",
                        "name": chunk.name or TOOL_NAME,
                        "count": rag_state.get("hit_count", 0),
                    }
            elif isinstance(chunk, AIMessage):
                # 思维链单独一路（thinking 事件），绝不混进正文 delta；
                # ReasoningChatOpenAI 已把 delta.reasoning_content 挂进 additional_kwargs。
                reasoning = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    yield {"type": "thinking", "delta": reasoning}
                if chunk.content:
                    if emit_delta:
                        turn.setdefault("answer_parts", []).append(chunk.content)
                    yield {"type": "delta" if emit_delta else "step_text", "text": chunk.content}
            continue

        for msg in _iter_update_messages(payload):
            # updates 流按 super-step 顺序给出完整消息，是强制收口重放的可靠来源
            turn.setdefault("transcript", []).append(msg)
            if not isinstance(msg, AIMessage) or not getattr(msg, "tool_calls", None):
                continue
            for call in msg.tool_calls:
                turn["tool_called"] = True
                args = call.get("args") or {}
                yield {
                    "type": "tool_call",
                    "name": call.get("name") or TOOL_NAME,
                    "query": str(args.get("query") or question),
                    "arguments": args,
                }


def _prune_transcript(messages: list[Any]) -> list[Any]:
    """截掉「assistant 声明了 tool_calls 但工具尚未返回」的尾巴。

    步数上限撞在模型输出 tool_calls 之后、工具执行之前时，transcript 会以
    悬空的 tool_calls 结尾；OpenAI 兼容接口要求每个 tool_calls 都有对应的
    tool 消息，直接重放会被 400 拒绝。截到最后一条完整 ToolMessage 即可。
    """
    last_tool = max(
        (i for i, m in enumerate(messages) if isinstance(m, ToolMessage)),
        default=-1,
    )
    if last_tool < 0:
        return []
    return messages[: last_tool + 1]


def _chunk_text(chunk: Any) -> str:
    """从模型流式分片里取出纯文本（兼容 str 与多模态 list 两种 content 形态）。"""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


async def _forced_finalize_stream(
    s: Settings,
    question: str,
    history: list[dict[str, Any]] | None,
    turn: dict[str, Any],
    *,
    use_web: bool,
) -> AsyncIterator[dict[str, Any]]:
    """强制收口轮：禁止工具调用，基于已重放的工具观察流式产出最终文字答案。

    产出 thinking / delta 两类事件（收口轮的思维链同样单独一路下发）。
    """
    messages: list[Any] = [SystemMessage(content=_agent_system_prompt(s, web_search_enabled=use_web))]
    messages.extend(_build_messages(question, history, s))
    messages.extend(_prune_transcript(turn.get("transcript") or []))

    model = _make_chat_model(s).bind_tools(_collect_tools(s, use_web), tool_choice="none")
    async for chunk in model.astream(messages):
        reasoning = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            yield {"type": "thinking", "delta": reasoning}
        text = _chunk_text(chunk)
        if text:
            yield {"type": "delta", "text": text}


async def agent_events(
    question: str,
    settings: Settings | None = None,
    final_k: int | None = None,
    history: list[dict[str, Any]] | None = None,
    web_search: bool | None = None,
    mode: str = "react",
) -> AsyncIterator[dict[str, Any]]:
    """Agent 问答事件流（异步生成器）。"""
    mode = (mode or "react").strip().lower()
    if mode not in MODES:
        mode = "react"

    s = settings or get_settings()
    question = (question or "").strip()
    if not question:
        yield {"type": "error", "message": "问题不能为空"}
        yield {"type": "done", "mode": "agent_failed"}
        return

    if not s.agent_configured:
        for event in answer_events(question, settings=s, final_k=final_k):
            yield event
        return

    if mode in ("plan", "deep"):
        from .deep_agent import run_deep_events

        async for event in run_deep_events(
            question,
            settings=s,
            final_k=final_k,
            mode=mode,
            web_search=web_search,
        ):
            yield event
        return

    use_web = s.web_search_default if web_search is None else bool(web_search)
    use_web = bool(use_web and s.web_search_configured)

    states = _new_states(s, final_k)
    state = states["_rag_state"]
    turn = _new_turn_state()

    async def _failure_events(message: str) -> AsyncIterator[dict[str, Any]]:
        """最后一道兜底（原有行为）：error → 已有 sources 直问一遍 → done(agent_failed)。"""
        yield {"type": "error", "message": message}
        if not turn["sources_emitted"]:
            yield {"type": "done", "mode": "agent_failed"}
            return
        sources = state.get("sources") or []
        yield {"type": "delta", "text": ""}
        try:
            for piece in stream_chat(build_messages(question, sources), s):
                yield {"type": "delta", "text": piece}
        except LLMError as inner:
            yield {"type": "error", "message": str(inner)}
            yield {"type": "delta", "text": retrieval_only_answer(sources)}
        yield {"type": "done", "mode": "agent_failed"}

    try:
        agent = _build_agent(s, web_search_enabled=use_web)
        async for event in _stream_react_turn(
            agent,
            {"messages": _build_messages(question, history, s)},
            turn,
            states,
            question=question,
            recursion_limit=s.agent_max_steps,
        ):
            yield event
    except GraphRecursionError:
        # 步数上限用尽（模型大概率还想调工具）：强制收口，禁止工具、逼出最终文字答案。
        logger.info(
            "ReAct 达到步数上限（%d 步），触发强制收口 question=%r",
            s.agent_max_steps,
            question,
        )
        metrics_store.bump("forced_finalize", settings=s)
        emitted = ""
        try:
            async for event in _forced_finalize_stream(
                s, question, history, turn, use_web=use_web
            ):
                if event.get("type") == "delta":
                    piece = str(event.get("text") or "")
                    emitted += piece
                    turn["answer_parts"].append(piece)
                yield event
        except Exception as finalize_exc:  # noqa: BLE001 - 收口失败只能降级到兜底
            logger.warning("强制收口调用失败：%s", finalize_exc)

        if not emitted.strip():
            logger.warning("强制收口未产出正文，降级到兜底通道 question=%r", question)
            async for event in _failure_events(
                "Agent 执行失败：已达步数上限且强制收口未产出正文"
            ):
                yield event
            return
        logger.info("强制收口成功，产出 %d 字 question=%r", len(emitted), question)
    except Exception as exc:  # noqa: BLE001 - 流式响应里出错只能以事件形式告知前端
        async for event in _failure_events(f"Agent 执行失败：{exc}"):
            yield event
        return

    citation_events, _ = _citation_check(
        "".join(turn.get("answer_parts") or []),
        state.get("sources") or [],
    )
    for event in citation_events:
        yield event

    yield {"type": "done", "mode": "agent_rag" if turn["tool_called"] else "agent_direct"}