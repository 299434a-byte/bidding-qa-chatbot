
"""deep_agent：Plan-and-Solve + Reflection 深度问答模式（deep 模式）。

职责：
  - 先让模型产出执行计划，再逐步执行并收集证据
  - 每步后做反思（Reflection），判断证据是否充分、是否继续
  - 预算四本账（工具 / 检索 / LLM / 时间）：任何一本超限都立即收敛到已有证据作答
  - 以与 react 模式同构的事件流对外暴露

依赖：agent、rag、rerank、llm、config、metrics
被谁调用：agent（/api/agent/chat 的 deep 模式分发）
"""

from __future__ import annotations

import asyncio

import time

from typing import Any, AsyncIterator

from langchain_core.messages import HumanMessage

from . import metrics as metrics_store

from .agent import (
    TOOL_NAME,
    _build_agent,
    _citation_check,
    _new_states,
    _new_turn_state,
    _snapshot_rag_state,
    _stream_react_turn,
)

from .config import Settings, get_settings

from .llm import LLMError, astream_chat, complete_chat

from .rag import (
    NO_DATA_MESSAGE,
    SYSTEM_PROMPT,
    render_reference_blocks,
    retrieval_only_answer,
)

from .rerank import extract_json

_PLAN_SYSTEM_PROMPT = (
    "你是「招投标智能问答助手」的任务规划器。请判断用户问题是否需要拆解成多个子任务。\n"
    "若问题简单（单一事实、单一查询），只输出 1 个子任务；\n"
    "若问题需要多角度、多数据源或多步推理才能回答完整，拆成 2~{max_steps} 个子任务。\n"
    "子任务之间应尽量不重复，各自回答一个方面。\n\n"
    '只输出 JSON，不要任何解释，形如：{{"steps": [{{"title": "不超过 20 字的子任务标题", '
    '"detail": "这一步具体要查什么、怎么做"}}]}}'
)

_REFLECT_SYSTEM_PROMPT = (
    "你是严格的答案质检员。请对照【用户问题】【参考资料】【各子任务结论】审查【待审答案】。\n"
    "判定标准：是否直接回答了问题、是否只依据参考资料、关键数字/名称/日期是否与资料一致、"
    "是否有遗漏或编造。\n\n"
    '只输出 JSON，不要任何解释，形如：{"verdict": "ok", "issues": []} 或 '
    '{"verdict": "revise", "issues": ["问题1", "问题2"]}\n'
    "若答案已足够好就输出 ok（issues 可为空）；只有确实需要改动时才输出 revise。"
)


class _Budget:
    """深度模式的预算四本账：工具调用 / 检索 / 大模型调用 / 整体时间。

    任何一本账超限都立即收敛到已有证据作答 —— 不抛异常、不空手结束。
    次数类上限 ``<=0`` 表示该本账不限；时间账复用 DEEP_TIMEOUT。
    """

    _NAMES = {
        "tool_calls": "工具调用",
        "retrievals": "检索",
        "llm_calls": "大模型调用",
        "time_seconds": "时间",
    }

    def __init__(self, s: Settings, started: float) -> None:
        self.tool_calls = 0
        self.retrievals = 0
        self.llm_calls = 0
        self.started = started
        self.tool_call_limit = int(s.deep_tool_call_limit)
        self.retrieval_limit = int(s.deep_retrieval_limit)
        self.llm_call_limit = int(s.deep_llm_call_limit)
        self.time_limit = int(s.deep_timeout)

    def count_tool(self, *, retrieval: bool = False) -> None:
        self.tool_calls += 1
        if retrieval:
            self.retrievals += 1

    def count_llm(self) -> None:
        self.llm_calls += 1

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started

    def exceeded(self) -> list[str]:
        """已超限的账本名列表（空 = 四本账都还有余量）。"""
        out: list[str] = []
        if self.tool_call_limit > 0 and self.tool_calls > self.tool_call_limit:
            out.append("tool_calls")
        if self.retrieval_limit > 0 and self.retrievals > self.retrieval_limit:
            out.append("retrievals")
        if self.llm_call_limit > 0 and self.llm_calls > self.llm_call_limit:
            out.append("llm_calls")
        if self.time_limit > 0 and self.elapsed_seconds > self.time_limit:
            out.append("time_seconds")
        return out

    def over_budget(self) -> bool:
        return bool(self.exceeded())

    def reason_text(self) -> str:
        exceeded = self.exceeded()
        return "、".join(f"{self._NAMES.get(n, n)}预算已超限" for n in exceeded) or "预算未超限"

    def meta(self) -> dict[str, Any]:
        """四本账的实际消耗与上限（写进 done 事件的 meta，前端可观测）。"""
        return {
            "tool_calls": self.tool_calls,
            "tool_call_limit": self.tool_call_limit,
            "retrievals": self.retrievals,
            "retrieval_limit": self.retrieval_limit,
            "llm_calls": self.llm_calls,
            "llm_call_limit": self.llm_call_limit,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "time_limit": self.time_limit,
            "exceeded": self.exceeded(),
        }


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨步累积的来源去重（按 doc_id），保持首次出现的顺序。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for src in sources:
        key = str(src.get("doc_id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(src)
    return out


def _hit_id_set(sources: list[dict[str, Any]] | None) -> set[str]:
    """本轮命中的 chunk 标识集合（doc_id 是「表:主键:段号」级的稳定标识）。"""
    return {str(s.get("doc_id") or "") for s in (sources or []) if s.get("doc_id")}


def _merge_meta(metas: list[dict[str, Any]], final_count: int, s: Settings) -> dict[str, Any]:
    """把多个子任务的检索统计合并成一份，供 ``retrieval`` 事件使用。"""
    dense = sum(int(m.get("dense_count") or 0) for m in metas)
    sparse = sum(int(m.get("sparse_count") or 0) for m in metas)
    last = metas[-1] if metas else {}
    return {
        "dense_count": dense,
        "sparse_count": sparse,
        "bm25_ready": bool(last.get("bm25_ready")),
        "final_count": final_count,
        "llm_configured": s.llm_configured,
        "steps": len(metas),
        "rerank_applied": False,
    }


def _plan_messages(question: str, s: Settings) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _PLAN_SYSTEM_PROMPT.format(max_steps=max(2, s.deep_max_steps))},
        {"role": "user", "content": f"【用户问题】\n{question}"},
    ]


def _parse_plan(raw: str, question: str, max_steps: int) -> list[dict[str, str]]:
    """解析规划 JSON。任何解析失败都退化为「单个子任务 = 原问题」。"""
    fallback = [{"title": "直接作答", "detail": question, "fallback": True}]
    data = extract_json(raw)
    steps_raw: Any = None
    if isinstance(data, dict):
        steps_raw = data.get("steps") or data.get("plan") or data.get("tasks")
    elif isinstance(data, list):
        steps_raw = data
    if not isinstance(steps_raw, list):
        return fallback

    steps: list[dict[str, str]] = []
    for item in steps_raw:
        if isinstance(item, str):
            title, detail = item.strip(), item.strip()
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("task") or item.get("name") or "").strip()
            detail = str(item.get("detail") or item.get("description") or item.get("desc") or title).strip()
        else:
            continue
        if not title and not detail:
            continue
        steps.append({"title": (title or detail)[:40], "detail": detail or title})
        if len(steps) >= max_steps:
            break
    return steps or fallback


def _step_instruction(question: str, step: dict[str, str], index: int, total: int) -> str:
    return (
        f"【总目标】{question}\n\n"
        f"【当前子任务 {index}/{total}】{step['title']}\n{step['detail']}\n\n"
        "请只针对当前子任务执行必要的工具调用，然后用简洁的中文给出该子任务的结论。"
        "这只是一步中间结论，不要展开成最终的长答案，也不要重复总目标。"
    )


def _solve_messages(
    question: str,
    steps: list[dict[str, str]],
    observations: list[str],
    sources: list[dict[str, Any]],
    memory_text: str = "",
) -> list[dict[str, str]]:
    plan_text = "\n".join(f"{i}. {st['title']}：{st['detail']}" for i, st in enumerate(steps, start=1))
    obs_text = (
        "\n\n".join(f"【子任务{i}结论】\n{obs}" for i, obs in enumerate(observations, start=1))
        or "（无）"
    )
    context = render_reference_blocks(sources) or "（无）"
    user_content = (
        f"【用户问题】\n{question}\n\n"
        f"【执行计划】\n{plan_text}\n\n"
        f"【各子任务结论】\n{obs_text}\n\n"
        f"【参考资料】\n{context}\n\n"
        "请综合上述信息，给出完整、准确、结构化的最终回答。"
        "严格依据参考资料，引用处标注 [参考资料N]，不要编造任何资料之外的信息。"
    )
    # 长期记忆摘要拼在 system 末尾：只帮理解用户关注点，不占引用位、不算来源。
    system = f"{SYSTEM_PROMPT}\n\n{memory_text}" if memory_text else SYSTEM_PROMPT
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def _reflect_messages(
    question: str,
    answer: str,
    observations: list[str],
    sources: list[dict[str, Any]],
) -> list[dict[str, str]]:
    context = render_reference_blocks(sources) or "（无）"
    obs_text = (
        "\n\n".join(f"【子任务{i}结论】\n{obs}" for i, obs in enumerate(observations, start=1))
        or "（无）"
    )
    user_content = (
        f"【用户问题】\n{question}\n\n"
        f"【各子任务结论】\n{obs_text}\n\n"
        f"【参考资料】\n{context}\n\n"
        f"【待审答案】\n{answer}\n\n"
        "请审查上述待审答案，只输出 JSON 判定。"
    )
    return [
        {"role": "system", "content": _REFLECT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _parse_reflection(raw: str) -> dict[str, Any]:
    """解析自评 JSON。解析不出来一律当「无需修订」，避免无谓的二次生成。"""
    data = extract_json(raw)
    if not isinstance(data, dict):
        return {"verdict": "ok", "issues": []}
    verdict = str(data.get("verdict") or data.get("result") or "").strip().lower()
    issues_raw = data.get("issues") or data.get("problems") or []
    if isinstance(issues_raw, str):
        issues = [issues_raw]
    elif isinstance(issues_raw, list):
        issues = [str(x).strip() for x in issues_raw if str(x).strip()]
    else:
        issues = []
    if verdict not in ("ok", "revise"):
        verdict = "revise" if issues else "ok"
    return {"verdict": verdict, "issues": issues[:5]}


def _fallback_answer(sources: list[dict[str, Any]]) -> str:
    return retrieval_only_answer(sources) if sources else NO_DATA_MESSAGE


async def run_deep_events(
    question: str,
    settings: Settings | None = None,
    final_k: int | None = None,
    mode: str = "plan",
    web_search: bool | None = None,
    memory_text: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """Plan-and-Solve（+ Reflection）事件流。

    外层只负责一件事：无论正常收口还是中途断开，都把本轮「预算四本账」消耗记进 metrics。
    """
    s = settings or get_settings()
    done_mode = "agent_deep" if mode == "deep" else "agent_plan"
    started = time.monotonic()
    budget = _Budget(s, started)
    try:
        async for event in _stream_deep_events(
            question, s, final_k, mode, web_search, budget, memory_text
        ):
            yield event
    finally:
        metrics_store.record_budget(
            mode=done_mode,
            tool_calls=budget.tool_calls,
            retrievals=budget.retrievals,
            llm_calls=budget.llm_calls,
            elapsed_ms=budget.elapsed_seconds * 1000,
            exceeded=budget.exceeded(),
            settings=s,
        )


async def _stream_deep_events(
    question: str,
    s: Settings,
    final_k: int | None,
    mode: str,
    web_search: bool | None,
    budget: _Budget,
    memory_text: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """Plan-and-Solve（+ Reflection）事件流本体（预算记账由 run_deep_events 外层收口）。"""
    mode = "deep" if mode == "deep" else "plan"
    done_mode = "agent_deep" if mode == "deep" else "agent_plan"
    over_budget = budget.over_budget

    use_web = s.web_search_default if web_search is None else bool(web_search)
    use_web = bool(use_web and s.web_search_configured)

    steps: list[dict[str, str]] = [{"title": "直接作答", "detail": question}]
    plan_fallback = True
    try:
        budget.count_llm()
        raw_plan = await asyncio.to_thread(
            complete_chat,
            _plan_messages(question, s),
            s,
            s.agent_model_or_default,
            0.0,
            s.deep_max_tokens,
            s.deep_timeout,
        )
        parsed = _parse_plan(raw_plan, question, max(1, s.deep_max_steps))
        plan_fallback = bool(parsed and parsed[0].get("fallback"))
        steps = parsed
    except Exception:  # noqa: BLE001 - 规划失败不应中断整轮，退化为单步
        steps = [{"title": "直接作答", "detail": question}]

    yield {
        "type": "plan",
        "steps": [
            {"index": i, "title": st["title"], "detail": st["detail"]}
            for i, st in enumerate(steps, start=1)
        ],
        "fallback": plan_fallback,
    }

    states = _new_states(s, final_k)
    observations: list[str] = []
    collected: list[dict[str, Any]] = []
    metas: list[dict[str, Any]] = []
    # 无增益判定：只跟「上一次真正发生过检索」的步骤比（retrieval_seq 未变 = 本轮没调检索）。
    prev_hit_ids: set[str] | None = None
    prev_seq = 0
    no_gain_round = 0

    try:
        agent = _build_agent(s, web_search_enabled=use_web)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"深度模式初始化失败：{exc}"}
        yield {"type": "done", "mode": "agent_failed", "budget": budget.meta()}
        return

    for i, step in enumerate(steps, start=1):
        yield {
            "type": "plan_step",
            "index": i,
            "title": step["title"],
            "status": "running",
            "observation": None,
            "error": None,
        }

        if over_budget():
            yield {
                "type": "plan_step",
                "index": i,
                "title": step["title"],
                "status": "failed",
                "observation": None,
                "error": f"{budget.reason_text()}，跳过该步骤",
            }
            observations.append(f"（该步骤因{budget.reason_text()}被跳过）")
            continue

        turn = _new_turn_state()
        parts: list[str] = []
        step_error: str | None = None
        budget.count_llm()
        try:
            async for event in _stream_react_turn(
                agent,
                {"messages": [HumanMessage(content=_step_instruction(question, step, i, len(steps)))]},
                turn,
                states,
                question=question,
                recursion_limit=max(1, s.deep_step_max_steps),
                emit_sources=False,
                emit_delta=False,
            ):
                etype = event.get("type")
                if etype == "tool_call":
                    budget.count_tool(retrieval=(event.get("name") == TOOL_NAME))
                if etype == "step_text":
                    parts.append(str(event.get("text") or ""))
                elif etype in ("tool_call", "tool_result", "sql_query", "graph_query", "web_search", "thinking"):
                    yield event
                if budget.over_budget():
                    # 任一账本超限：本步立即收手（已有 observation 照常进结论），后续步骤不再起跑。
                    break
        except Exception as exc:  # noqa: BLE001 - 单步失败只标记该步，不中断整轮
            step_error = str(exc)

        snapshot = _snapshot_rag_state(states["_rag_state"])
        if snapshot["sources"]:
            collected.extend(snapshot["sources"])
        if snapshot["meta"]:
            metas.append(snapshot["meta"])

        observation = " ".join("".join(parts).split())[: max(0, s.deep_observation_chars)]
        if step_error and not observation:
            observation = f"（该步骤执行失败：{step_error}）"
        observations.append(observation or "（该步骤没有产出结论）")

        yield {
            "type": "plan_step",
            "index": i,
            "title": step["title"],
            "status": "failed" if step_error and not parts else "done",
            "observation": observations[-1],
            "error": step_error,
        }

        # —— 无增益判定：最近两轮检索命中的 chunk 集合完全相同 → 立即终止重试 ——
        # 改写检索词后还是同一批切片，继续跑只会白烧 LLM；转入 FALLBACK（仅检索列证据）。
        seq = int(states["_rag_state"].get("retrieval_seq") or 0)
        if seq != prev_seq:
            step_ids = _hit_id_set(snapshot["sources"])
            if prev_hit_ids is not None and step_ids == prev_hit_ids:
                no_gain_round = i
                for j in range(i + 1, len(steps) + 1):
                    yield {
                        "type": "plan_step",
                        "index": j,
                        "title": steps[j - 1]["title"],
                        "status": "failed",
                        "observation": None,
                        "error": "最近两轮检索命中完全相同（无增益），已提前终止",
                    }
                break
            prev_hit_ids = step_ids
        prev_seq = seq

    if no_gain_round:
        metrics_store.bump("no_gain_stop", settings=s)
        yield {"type": "no_gain", "reason": "no_gain", "round": no_gain_round}
        sources = _dedupe_sources(collected)
        yield {"type": "retrieval", "meta": _merge_meta(metas, len(sources), s)}
        yield {"type": "sources", "sources": sources}
        yield {"type": "delta", "text": _fallback_answer(sources), "channel": "answer"}
        yield {"type": "done", "mode": done_mode, "reason": "no_gain", "budget": budget.meta()}
        return

    sources = _dedupe_sources(collected)
    yield {"type": "retrieval", "meta": _merge_meta(metas, len(sources), s)}
    yield {"type": "sources", "sources": sources}

    if over_budget():
        # 任一账本超限：不再进综合生成，立即以已有证据收敛作答（不抛异常、不空手结束）。
        yield {"type": "delta", "text": _fallback_answer(sources), "channel": "answer"}
        yield {"type": "done", "mode": done_mode, "reason": "budget", "budget": budget.meta()}
        return

    answer_parts: list[str] = []
    # 思维链缓冲：综合阶段的 reasoning 增量转成独立 thinking 事件（不混进正文）。
    thinking_parts: list[str] = []

    def _on_thinking(text: str) -> None:
        thinking_parts.append(text)

    def _drain_thinking():
        while thinking_parts:
            yield {"type": "thinking", "delta": thinking_parts.pop(0)}

    try:
        budget.count_llm()
        async for piece in astream_chat(
            _solve_messages(question, steps, observations, sources, memory_text),
            s,
            model=s.agent_model_or_default,
            max_tokens=s.deep_max_tokens,
            timeout=s.deep_timeout,
            on_thinking=_on_thinking,
        ):
            # async def 里不允许 yield from（93bf6e1 曾在此引入 SyntaxError），
            # 逐个转手 _drain_thinking 的产出，行为等价。
            for drain_event in _drain_thinking():
                yield drain_event
            answer_parts.append(piece)
            yield {"type": "delta", "text": piece, "channel": "answer"}
        for drain_event in _drain_thinking():
            yield drain_event
    except LLMError as exc:
        yield {"type": "error", "message": str(exc)}

    answer = "".join(answer_parts)
    if not answer.strip():
        fallback = _fallback_answer(sources)
        yield {"type": "delta", "text": fallback, "channel": "answer"}
        answer = fallback

    citation_events, answer = _citation_check(answer, sources)
    for event in citation_events:
        yield event

    if mode == "deep" and s.deep_reflect_rounds > 0 and not over_budget():
        current = answer
        for round_no in range(1, s.deep_reflect_rounds + 1):
            if over_budget():
                break
            try:
                budget.count_llm()
                raw = await asyncio.to_thread(
                    complete_chat,
                    _reflect_messages(question, current, observations, sources),
                    s,
                    s.agent_model_or_default,
                    0.0,
                    s.deep_max_tokens,
                    s.deep_timeout,
                )
                verdict = _parse_reflection(raw)
            except LLMError:
                break

            need_revise = verdict["verdict"] == "revise"
            yield {
                "type": "reflection",
                "round": round_no,
                "verdict": verdict["verdict"],
                "issues": verdict["issues"],
                "revision_offered": need_revise,
            }
            if not need_revise:
                break

            revision_parts: list[str] = []
            revision_thinking: list[str] = []

            def _on_revision_thinking(text: str) -> None:
                revision_thinking.append(text)

            try:
                budget.count_llm()
                async for piece in astream_chat(
                    _solve_messages(question, steps, observations, sources, memory_text)
                    + [
                        {
                            "role": "user",
                            "content": "上一版答案存在以下问题，请据此重写一版更准确的完整答案：\n"
                            + "\n".join(f"- {x}" for x in verdict["issues"]),
                        }
                    ],
                    s,
                    model=s.agent_model_or_default,
                    max_tokens=s.deep_max_tokens,
                    timeout=s.deep_timeout,
                    on_thinking=_on_revision_thinking,
                ):
                    while revision_thinking:
                        yield {"type": "thinking", "delta": revision_thinking.pop(0)}
                    revision_parts.append(piece)
                    yield {"type": "delta", "text": piece, "channel": "revision"}
                while revision_thinking:
                    yield {"type": "thinking", "delta": revision_thinking.pop(0)}
            except LLMError as exc:
                yield {"type": "error", "message": str(exc)}

            revised = "".join(revision_parts)
            if revised.strip():
                current = revised
            else:
                break

    yield {"type": "done", "mode": done_mode, "budget": budget.meta()}