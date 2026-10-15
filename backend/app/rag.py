
"""rag：RAG 主编排（检索 → 重排 → 组装提示词 → 流式生成，含引用兜底）。

职责：
  - 调 retriever / rerank 得到证据块，组装带来源约束的系统提示词
  - 流式生成回答并做引用校验（检索无结果时输出 REF_1 标准文案）
  - 渲染来源引用文本块

依赖：retriever、rerank、llm、citation、config
被谁调用：main（/api/chat）、agent、deep_agent
"""

from __future__ import annotations

from typing import Any, Iterator

from .config import Settings, get_settings

from .llm import LLMError, LLMNotConfigured, stream_chat

from .rerank import rerank_hits

from .retriever import RetrievalResult, retrieve

from .citation import REF_1_RETRIEVAL_EMPTY, strip_bad_refs, verify_citations

SYSTEM_PROMPT = """你是「招投标智能问答助手」，服务对象是招投标业务人员。

作答要求：
1. 只依据【参考资料】回答，不要引入资料之外的知识，也不要凭常识推测。
2. 引用某条资料时，用 [参考资料N] 标注出处。
3. 如果参考资料不足以回答问题，直接说明「当前知识库中没有找到相关依据」，并说明你缺什么信息，
   绝对不要编造项目名称、金额、日期、资质要求等内容。
4. 金额、日期、编号、公司名称等关键信息必须与资料完全一致，不得改写。
5. 回答用简体中文，结构清晰；能列表就列表，避免空话套话。"""


NO_DATA_MESSAGE = (
    f"{REF_1_RETRIEVAL_EMPTY}\n\n"
    "（若三张源表 `bidding_notices` / `bidding_project` / `companies` 为空，"
    "或数据已灌入但尚未建库，请在后台点击「重建知识库」，"
    "或执行 `python -m app.ingest`（需先停掉后端服务）。）"
)


def render_reference_blocks(sources: list[dict[str, Any]]) -> str:
    """把命中的来源渲染成 ``[参考资料N]`` 文本块；无来源时返回空字符串。"""
    blocks: list[str] = []
    for i, src in enumerate(sources, start=1):
        header = (
            f"[参考资料{i}] 来源表：{src['label']}（{src['table']}）"
            f"｜记录ID：{src['primary_key']}"
        )
        if src.get("chunk_total", 1) > 1:
            header += f"｜第 {src['chunk_index'] + 1}/{src['chunk_total']} 段"
        blocks.append(f"{header}\n{src['text']}")
    return "\n\n".join(blocks)


def build_messages(question: str, sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把命中的原文拼成上下文，交给大模型。"""
    context = render_reference_blocks(sources) or "（无）"

    user_content = (
        f"【参考资料】\n{context}\n\n"
        f"【用户问题】\n{question}\n\n"
        "请严格依据上述参考资料作答，并在引用处标注 [参考资料N]。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _preview(hit: dict[str, Any], max_fields: int = 4) -> str:
    """从命中的原始字段里挑出几条非空信息，拼成一行摘要。"""
    fields: dict[str, Any] = hit.get("fields") or {}
    parts: list[str] = []
    for key, value in fields.items():
        if value in (None, ""):
            continue
        text = str(value).strip().replace("\n", " ")
        if not text:
            continue
        parts.append(f"{key}={text[:60]}")
        if len(parts) >= max_fields:
            break
    return "；".join(parts) if parts else (hit.get("text", "")[:120])


def retrieval_only_answer(sources: list[dict[str, Any]]) -> str:
    """未配置大模型时的降级回答：直接列举命中的原文，信息不打折、不编造。"""
    lines = [
        f"当前处于**仅检索模式**（未配置大模型），已从知识库命中 {len(sources)} 条相关记录，原文摘要如下：",
        "",
    ]
    for i, src in enumerate(sources, start=1):
        metric = f"{' + '.join(src.get('matched_by', []))} 命中，RRF={src['rrf_score']}"
        if src.get("rerank_rank"):
            metric += f"，重排 #{src['rerank_rank']}"
        lines.append(f"**{i}. 【{src['label']}｜记录 {src['primary_key']}】**（{metric}）")
        lines.append(_preview(src))
        lines.append("")
    lines.append("> 配置 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 后即可获得自然语言综合回答。")
    return "\n".join(lines)


def answer_events(
    question: str,
    settings: Settings | None = None,
    final_k: int | None = None,
) -> Iterator[dict[str, Any]]:
    """完整的问答事件流。"""
    s = settings or get_settings()
    want_k = final_k or s.retrieve_final_top_k
    pool_k = max(want_k, s.rerank_candidates)

    try:
        result: RetrievalResult = retrieve(question, settings=s, final_k=pool_k)
        hits, rerank_meta = rerank_hits(question, result.hits, s, top_k=want_k)
        result.hits = hits
        result.rerank_meta = rerank_meta
    except Exception as exc:
        yield {"type": "error", "message": f"检索失败：{exc}"}
        return

    yield {
        "type": "retrieval",
        "meta": {
            "dense_count": result.dense_count,
            "sparse_count": result.sparse_count,
            "bm25_ready": result.bm25_ready,
            "final_count": len(result.hits),
            "llm_configured": s.llm_configured,
            **result.rerank_meta,
        },
    }

    sources = result.to_sources()
    yield {"type": "sources", "sources": sources}

    if not sources:
        yield {"type": "delta", "text": NO_DATA_MESSAGE}
        yield {"type": "done", "mode": "no_data"}
        return

    if not s.llm_configured:
        yield {"type": "delta", "text": retrieval_only_answer(sources)}
        yield {"type": "done", "mode": "retrieval_only"}
        return

    yield {"type": "delta", "text": ""}
    answer_parts: list[str] = []
    # 思维链缓冲：stream_chat 在正文间隙回调 on_thinking，这里转成独立的 thinking 事件
    # 下发（绝不混进 delta 正文）。
    thinking_parts: list[str] = []

    def _on_thinking(text: str) -> None:
        thinking_parts.append(text)

    def _drain_thinking():
        while thinking_parts:
            yield {"type": "thinking", "delta": thinking_parts.pop(0)}

    try:
        for piece in stream_chat(build_messages(question, sources), s, on_thinking=_on_thinking):
            yield from _drain_thinking()
            answer_parts.append(piece)
            yield {"type": "delta", "text": piece}
        yield from _drain_thinking()
    except LLMNotConfigured:
        yield {"type": "delta", "text": retrieval_only_answer(sources)}
        yield {"type": "done", "mode": "retrieval_only"}
        return
    except LLMError as exc:
        yield {"type": "error", "message": str(exc)}
        yield {"type": "done", "mode": "llm_failed"}
        return

    yield {"type": "done", "mode": "llm"}

    try:
        check = verify_citations("".join(answer_parts), sources)
    except Exception:  # noqa: BLE001
        check = None

    if check is not None and not check.get("ok"):
        revised = strip_bad_refs("".join(answer_parts), check.get("bad_refs") or [])
        yield {"type": "delta", "channel": "revision", "text": revised}
        yield {"type": "citation_check", "ok": False, "issues": check.get("issues") or []}
