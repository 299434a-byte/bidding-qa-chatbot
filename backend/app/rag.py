
"""rag：RAG 主编排（检索判定 → 检索 → 重排 → 组装提示词 → 流式生成，含引用兜底）。

职责：
  - Self-RAG 检索判定（needs_retrieval）：寒暄/致谢/元问题/纯算术/改写指令不进检索
  - 调 retriever / rerank 得到证据块，组装带来源约束的系统提示词
  - 覆盖率闸门：检索块与问题关键词重合度不足时拒绝进入生成（防答非所问）
  - 流式生成回答并做引用校验（检索无结果时输出 REF_1 标准文案），
    校验通过/失败都发 citation_check 事件，携带逐句可信度（confidence / hallucination_flags）
  - 渲染来源引用文本块
  - 追问复用：以上一问证据直接作答（reused_answer_events，跳过检索/规划）

依赖：retriever、rerank、llm、citation、config、metrics
被谁调用：main（/api/chat、/api/agent/chat 的追问复用）、agent、deep_agent
"""

from __future__ import annotations

import re

import unicodedata

from typing import Any, Iterator

from . import metrics as metrics_store

from .config import Settings, get_settings

from .llm import LLMError, LLMNotConfigured, stream_chat

from .rerank import rerank_hits

from .retriever import RetrievalResult, retrieve, term_coverage

from .citation import REF_1_RETRIEVAL_EMPTY, score_answer, strip_bad_refs, verify_citations

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

# 覆盖率不足时的固定拒答文案（不进生成，避免模型顺着答非所问的资料编造）。
COVERAGE_REFUSAL_MESSAGE = (
    "暂无明确依据：检索到的资料与您的问题关键词重合度不足，"
    "为避免答非所问，本次不作答。\n\n"
    "**排查建议：**\n"
    "- 换更具体的关键词重问（如项目名称、编号、公告标题）；\n"
    "- 开启联网搜索，补充时效性信息；\n"
    "- 或切换图谱查询，查实体之间的关系。"
)

# ============================ Self-RAG 检索判定（规则表） ============================
# 命中下列规则的「整句」明确不需要检索；其余一律 True —— 宁可多检索，不可漏检索。
# 所有匹配都在 NFKC 归一（全角→半角）+ 去掉句末标点后的整句（fullmatch）上做，
# 保证「招标方是谁」不会被「你是谁」的子串规则误伤。

# 寒暄：单独一句问候（可带语气词）
_GREETING_RE = re.compile(
    r"(?:你?好|您好|hi|hello|hey|howdy|哈喽|嗨|yo"
    r"|早上好|早安|上午好|中午好|下午好|晚上好|在吗|在不在|在么)"
    r"[呀啊哦嘛嗯~～!！\s]*",
    re.IGNORECASE,
)

# 致谢 / 客套收尾
_THANKS_RE = re.compile(
    r"(?:(?:你好|您好|好的?|太|真是)\s*)?"
    r"(?:谢谢|多谢|感谢|thank you|thanks)"
    r"(?:你|您){0,2}[呀啊啦咯哦了嗯~～!！\s]*"
    r"|辛苦了[~～!！\s]*",
    re.IGNORECASE,
)

# 语气词应答（嗯 / 好的 / 收到 …）：允许多个词连用，词尾可带「了/的/啦」等语气字
_ACK_RE = re.compile(
    r"(?:(?:嗯|哦|噢|好|行|收到|知道了|明白|了解|可以|ok|okay)"
    r"[的了呀啊啦咯哦嘛呢~～!！\s]*)+",
    re.IGNORECASE,
)

# 元问题：问「你是谁 / 你能做什么 / 怎么用」这类机器人本身的话题
_META_RE = re.compile(
    r"(?:你是谁|你叫什么(?:名字)?|你是什么(?:系统|机器人|助手|工具)?|你是ai吗"
    r"|你能做什么|你能干什么|你会(?:做)?什么|你能干吗|你能帮我做什么"
    r"|你有什么(?:功能|本事|用)|你的(?:功能|用途)是什么"
    r"|怎么(?:用|使用)(?:你|这个)|如何(?:使用|操作)(?:你|这个)"
    r"|使用(?:说明|指南|方法)|操作说明|帮助|help"
    r"|介绍(?:一)?下?(?:你|你们)自己|你(?:的)?名字是什么)"
    r"[呀啊哦嘛吗呢~～?？!！\s]*",
    re.IGNORECASE,
)

# 改写上一条的指令（「换个说法」「再短一点」……），允许前面带「帮我把xxx」类前缀
_REWRITE_RE = re.compile(
    r"(?:帮我)?(?:把|将)?[一-鿿]{0,12}?"
    r"(?:换个说法|换种说法|换个讲法|换个问法|换一种说法|换个表达(?:方式)?"
    r"|重新(?:回答|说|讲|表述|组织|生成|写)|再说一遍|重说一遍|再说下?一次"
    r"|再(?:短|长|简洁|详细|简单|精简)(?:一)?点(?:儿)?"
    r"|说得?(?:更)?(?:简短|简洁|详细|简单|清楚|明白)(?:一)?点(?:儿)?"
    r"|简短一点|长话短说|展开(?:说说|讲讲|一下|一点))"
    r"[呀啊啦咯哦嘛呢~～!！\s]*"
)

# 纯算术表达式的字符集（数字、四则运算符号与括号）；运算符用于要求「至少有一个运算」
_ARITH_BODY_RE = re.compile(r"[-+*/×÷=()\d\s.（）％%]+")
_ARITH_OPERATOR_RE = re.compile(r"[-+*/×÷]")

# 判定前剥掉的句末标点与「等于几 / 是多少」类后缀
_TRAILING_JUNK_RE = re.compile(r"[\s。．.,，!！?？~～、;；:：…]+$")
_ARITH_SUFFIX_RE = re.compile(r"(?:等于|是)\s*(?:几|多少)?[吗呢嘛?？]*\s*$")


def _looks_arithmetic(compact: str) -> bool:
    """「1+1」「100-25 等于多少」这类纯算术表达式（必须至少含一个运算符）。"""
    body = _ARITH_SUFFIX_RE.sub("", compact).strip()
    if not body or not _ARITH_BODY_RE.fullmatch(body):
        return False
    # 只有数字（如「2024」）不算算术题——它更可能是想问那一年的公告，保守放行检索。
    return bool(_ARITH_OPERATOR_RE.search(body))


def needs_retrieval(question: str) -> bool:
    """判断这个问题要不要进知识库检索（规则优先；判不准一律 True 走原链路）。

    直接返回 ``False`` 的类别：寒暄、致谢客套、问机器人本身的元问题、
    纯算术表达式、「改写上一条」类指令、语气词应答。
    其余（含规则打半转的混合问题，如「你好，请问最近有哪些公告」）一律 ``True``
    —— 宁可多检索，不可漏检索。
    """
    normalized = unicodedata.normalize("NFKC", question or "").strip()
    compact = _TRAILING_JUNK_RE.sub("", normalized).strip()
    if not compact:
        return True
    return not (
        _GREETING_RE.fullmatch(compact)
        or _THANKS_RE.fullmatch(compact)
        or _ACK_RE.fullmatch(compact)
        or _META_RE.fullmatch(compact)
        or _looks_arithmetic(compact)
        or _REWRITE_RE.fullmatch(compact)
    )


# 跳过检索的直答提示词：明确告诉模型「这题不用查资料」，避免它套用
# 「只依据参考资料」的纪律生硬拒答。
DIRECT_CHAT_SYSTEM_PROMPT = (
    "你是「招投标智能问答助手」，当前问题不需要查询知识库"
    "（例如打招呼、致谢、能力询问、简单算术或改写表述）。\n"
    "请直接、简短、友好地回答，不要检索资料，也不要编造任何招投标数据。"
)

# 未配置大模型时直答通道的固定说明（如实降级，不假装检索过）。
NO_LLM_DIRECT_MESSAGE = (
    "当前处于**仅检索模式**（未配置大模型），无法生成自然语言回答。"
    "配置 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 后即可正常对话。"
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


def _stream_direct_answer(question: str, s: Settings) -> Iterator[dict[str, Any]]:
    """不需要检索的直答通道：跳过检索/来源/引用校验，直接生成一句寒暄类回复。

    事件顺序：delta("") → delta* → done(mode="direct")；``done`` 带
    ``skipped_retrieval=true``，且绝不出现 sources / citation_check。
    """
    yield {"type": "delta", "text": ""}
    if not s.llm_configured:
        yield {"type": "delta", "text": NO_LLM_DIRECT_MESSAGE}
        yield {"type": "done", "mode": "direct", "skipped_retrieval": True}
        return
    messages = [
        {"role": "system", "content": DIRECT_CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    try:
        for piece in stream_chat(messages, s):
            yield {"type": "delta", "text": piece}
    except LLMNotConfigured:
        yield {"type": "delta", "text": NO_LLM_DIRECT_MESSAGE}
        yield {"type": "done", "mode": "direct", "skipped_retrieval": True}
        return
    except LLMError as exc:
        yield {"type": "error", "message": str(exc)}
        yield {"type": "done", "mode": "llm_failed", "skipped_retrieval": True}
        return
    yield {"type": "done", "mode": "direct", "skipped_retrieval": True}


def answer_events(
    question: str,
    settings: Settings | None = None,
    final_k: int | None = None,
) -> Iterator[dict[str, Any]]:
    """完整的问答事件流。"""
    s = settings or get_settings()

    if not needs_retrieval(question):
        # Self-RAG 检索判定：寒暄/致谢/元问题/纯算术/改写指令不进检索，
        # 直接走生成；retrieval 事件标注 skipped_retrieval=true（宁可多检索，不可漏检索，
        # 所以只跳过整句明确命中的问题，判不准仍走下面的完整链路）。
        metrics_store.bump("retrieval_skipped", settings=s)
        yield {
            "type": "retrieval",
            "meta": {
                "skipped_retrieval": True,
                "final_count": 0,
                "llm_configured": s.llm_configured,
            },
        }
        yield from _stream_direct_answer(question, s)
        return

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

    # 第二道判据（与 dense_min_score 并列）：问题实词在最终候选上的最大覆盖率
    coverage = term_coverage(question, result.hits)

    yield {
        "type": "retrieval",
        "meta": {
            "dense_count": result.dense_count,
            "sparse_count": result.sparse_count,
            "bm25_ready": result.bm25_ready,
            "final_count": len(result.hits),
            "llm_configured": s.llm_configured,
            "coverage": round(coverage, 4),
            "wider_recall": result.wider_recall,
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
        # 未配置大模型：仅检索列举原文、不生成不编造，覆盖率闸门不介入（保持既有降级行为）。
        yield {"type": "delta", "text": retrieval_only_answer(sources)}
        yield {"type": "done", "mode": "retrieval_only"}
        return

    if coverage < s.coverage_min:
        # 覆盖率不足 → 不进入生成（字面像但答非所问的资料不给模型编造的机会）
        metrics_store.bump("coverage_refused", settings=s)
        yield {
            "type": "coverage_check",
            "coverage": round(coverage, 4),
            "threshold": s.coverage_min,
            "refused": True,
        }
        yield {"type": "delta", "text": COVERAGE_REFUSAL_MESSAGE}
        yield {"type": "done", "mode": "coverage_refused"}
        return

    metrics_store.bump("coverage_pass", settings=s)

    yield from _stream_generated_answer(question, sources, s)


def _stream_generated_answer(
    question: str,
    sources: list[dict[str, Any]],
    s: Settings,
    *,
    done_extra: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """对**已确定的来源**流式生成答案（完整链路与追问复用链路共用）。

    事件顺序：delta("") → thinking/delta* → done → [citation_check + revision]。
    ``done_extra`` 会并入 done 事件（如追问复用标注 reused=true）。
    """
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
        yield {"type": "done", "mode": "retrieval_only", **(done_extra or {})}
        return
    except LLMError as exc:
        yield {"type": "error", "message": str(exc)}
        yield {"type": "done", "mode": "llm_failed", **(done_extra or {})}
        return

    yield {"type": "done", "mode": "llm", **(done_extra or {})}

    if not sources:
        # 没有来源就没有比对依据（如「跳过检索」的直答通道）：不校验、不发 citation_check。
        return

    try:
        full_answer = "".join(answer_parts)
        check = verify_citations(full_answer, sources)
        score = score_answer(full_answer, sources, check)
    except Exception:  # noqa: BLE001
        return

    if not check.get("ok"):
        revised = strip_bad_refs(full_answer, check.get("bad_refs") or [])
        yield {"type": "delta", "channel": "revision", "text": revised}
    # 校验通过也发 citation_check：事件新增 confidence / hallucination_flags 等逐句
    # 可信度字段（纯新增），ok / issues / number_checks 的语义与取值保持不变。
    yield {
        "type": "citation_check",
        "ok": check.get("ok"),
        "issues": check.get("issues") or [],
        "number_checks": check.get("number_checks") or [],
        **score,
    }


def reused_answer_events(
    question: str,
    sources: list[dict[str, Any]],
    settings: Settings | None = None,
) -> Iterator[dict[str, Any]]:
    """追问复用链路：跳过检索与规划，直接以上一问证据流式作答。

    事件结构与 :func:`answer_events` 同构（retrieval / sources / delta / done），
    retrieval.meta 与 done 里都带 ``reused=true``，前端据此可提示「复用了上一问证据」。
    """
    s = settings or get_settings()
    yield {
        "type": "retrieval",
        "meta": {
            "reused": True,
            "final_count": len(sources),
            "llm_configured": s.llm_configured,
        },
    }
    yield {"type": "sources", "sources": sources}
    if not sources:
        yield {"type": "delta", "text": NO_DATA_MESSAGE}
        yield {"type": "done", "mode": "no_data", "reused": True}
        return
    if not s.llm_configured:
        yield {"type": "delta", "text": retrieval_only_answer(sources)}
        yield {"type": "done", "mode": "retrieval_only", "reused": True}
        return
    yield from _stream_generated_answer(question, sources, s, done_extra={"reused": True})
