
"""rerank：LLM 重排（大模型按真实相关度对候选二次排序）。

职责：
  - 压缩候选摘要 → 提示词 → 容错解析模型返回的 JSON 顺序
  - should_rerank 双信号触发（dense 分并列 / 过低），auto 模式跳过低收益场景
  - 超时或解析失败时静默降级回 RRF 原序（fail-open）

依赖：llm、config
被谁调用：rag、main（/api/retrieve?rerank=true）、agent、deep_agent
"""

from __future__ import annotations

import logging

import json

import re

from typing import Any

from .config import Settings, get_settings

from .llm import LLMError, LLMNotConfigured, complete_chat

logger = logging.getLogger(__name__)

RERANK_SYSTEM_PROMPT = """你是检索结果重排助手。给定一个用户问题和若干候选资料片段，\
请按「与问题的相关程度」从高到低排序。

要求：
1. 只输出 JSON，不要解释，不要加 Markdown 代码块，不要输出任何推理过程。
2. 输出格式（JSON schema）：{"order": [序号, 序号, ...]}，例如 {"order": [2, 1, 3]}
3. 序号是候选资料的编号，必须覆盖全部候选，且每个序号只出现一次。
4. 判断依据是「这段资料能否直接回答该问题」，而不是字面上重合的词有多少。"""

_RERANK_MAX_TOKENS = 8192

_FENCE_RE = re.compile(r"```[A-Za-z]*")
_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.S)
_ORDER_KEYS = ("order", "ranking", "rank", "result", "indices", "indexes")


def _summarize(hit: dict[str, Any], max_chars: int) -> str:
    """把一条候选压成一行摘要，控制提示词体积。"""
    lines = [line.strip() for line in (hit.get("text") or "").splitlines() if line.strip()]
    if lines and lines[0].startswith("【"):
        lines = lines[1:]
    summary = " ".join(" ".join(lines).split())
    if not summary:
        fields: dict[str, Any] = hit.get("fields") or {}
        summary = "；".join(f"{k}: {v}" for k, v in fields.items() if v not in (None, ""))
    return summary[:max_chars]


def build_rerank_messages(
    question: str,
    hits: list[dict[str, Any]],
    max_chars: int = 300,
) -> list[dict[str, str]]:
    """拼重排提示词：问题 + 带编号的候选摘要列表。"""
    lines: list[str] = []
    for i, hit in enumerate(hits, start=1):
        label = hit.get("label") or hit.get("table") or ""
        lines.append(f"{i}. [{label}|记录 {hit.get('primary_key')}] {_summarize(hit, max_chars)}")

    user_content = (
        f"【用户问题】\n{question}\n\n"
        "【候选资料】\n" + "\n".join(lines) + "\n\n"
        '请只输出 JSON、不要输出任何推理过程，形如 {"order": [2, 1, 3]}。'
    )
    return [
        {"role": "system", "content": RERANK_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def extract_json(raw: str) -> Any | None:
    """从大模型输出里剥掉代码围栏、提取并解析内嵌的 JSON（对象或数组）。"""
    if not raw:
        return None
    text = _FENCE_RE.sub(" ", raw).strip()
    if not text:
        return None

    chunks: list[str] = []
    match = _JSON_RE.search(text)
    if match:
        chunks.append(match.group(0))
    chunks.append(text)
    for chunk in chunks:
        try:
            return json.loads(chunk)
        except (ValueError, TypeError):
            continue
    return None


def _extract_sequence(data: Any) -> list[Any] | None:
    """从解析出的 JSON 里找到那个序号数组。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _ORDER_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
        for value in data.values():
            if isinstance(value, list):
                return value
    return None


def parse_order(raw: str, n: int) -> list[int] | None:
    """把大模型返回的文本解析成 0 起的候选顺序。"""
    if not raw or n <= 0:
        return None

    text = _FENCE_RE.sub(" ", raw).strip()
    if not text:
        return None

    chunks: list[str] = []
    match = _JSON_RE.search(text)
    if match:
        chunks.append(match.group(0))
    chunks.append(text)

    sequence: list[Any] | None = None
    for chunk in chunks:
        try:
            data = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        sequence = _extract_sequence(data)
        if sequence is not None:
            break

    if sequence is None:
        return None

    seen: set[int] = set()
    order: list[int] = []
    for item in sequence:
        try:
            index = int(item) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < n and index not in seen:
            seen.add(index)
            order.append(index)

    if not order:
        return None

    order.extend(i for i in range(n) if i not in seen)
    return order


def should_rerank(hits: list[dict[str, Any]], settings: Settings | None = None) -> bool:
    """判断这批候选的 RRF 顺序是否「不可靠到值得重排」（纯函数，可离线单测）。"""
    s = settings or get_settings()
    if len(hits) < 2:
        return False

    dense1 = (hits[0].get("scores") or {}).get("dense")
    dense2 = (hits[1].get("scores") or {}).get("dense")
    if dense1 is not None and dense2 is not None:
        d1, d2 = float(dense1), float(dense2)
        if d1 > 0 and (d1 - d2) / d1 < s.rerank_auto_gap:
            return True

    dense_top = (hits[0].get("scores") or {}).get("dense")
    if dense_top is not None and float(dense_top) < s.rerank_auto_min_dense:
        return True


    return False


def rerank_hits(
    question: str,
    hits: list[dict[str, Any]],
    settings: Settings | None = None,
    top_k: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """对 RRF 融合后的候选做二次排序，返回 (结果, meta)。"""
    s = settings or get_settings()
    want_k = top_k or s.retrieve_final_top_k

    meta: dict[str, Any] = {
        "rerank_applied": False,
        "rerank_model": None,
        "rerank_candidates": len(hits),
        "rerank_error": None,
        "rerank_skipped": False,
        "rerank_skip_reason": None,
    }

    if not hits:
        return hits, meta

    if not s.rerank_configured:
        return hits[:want_k], meta

    if len(hits) <= want_k:
        meta["rerank_skipped"] = True
        meta["rerank_skip_reason"] = "too_few_candidates"
        logger.debug("跳过重排：候选 %d 条 ≤ 目标 %d 条（too_few_candidates）", len(hits), want_k)
        return hits[:want_k], meta

    mode = (getattr(s, "rerank_mode", "auto") or "auto").lower()
    if mode == "off":
        meta["rerank_skipped"] = True
        meta["rerank_skip_reason"] = "mode_off"
        logger.debug("跳过重排：RERANK_MODE=off（mode_off）")
        return hits[:want_k], meta
    if mode == "auto" and not should_rerank(hits, s):
        meta["rerank_skipped"] = True
        meta["rerank_skip_reason"] = "auto_order_reliable"
        logger.debug("跳过重排：RRF 顺序可靠（auto_order_reliable） candidates=%d", len(hits))
        return hits[:want_k], meta

    meta["rerank_model"] = s.rerank_model_or_default
    try:
        raw = complete_chat(
            build_rerank_messages(question, hits, s.rerank_max_chars),
            settings=s,
            model=s.rerank_model_or_default,
            temperature=0.0,
            max_tokens=_RERANK_MAX_TOKENS,
            timeout=s.rerank_timeout,
        )
        order = parse_order(raw, len(hits))
    except LLMNotConfigured:
        return hits[:want_k], meta
    except LLMError as exc:
        meta["rerank_error"] = str(exc)
        return hits[:want_k], meta
    except Exception as exc:  # noqa: BLE001 - 重排失败绝不能影响问答主流程
        meta["rerank_error"] = f"重排失败：{exc}"
        return hits[:want_k], meta

    if order is None:
        meta["rerank_error"] = "大模型返回内容无法解析为排序结果，已沿用 RRF 顺序"
        return hits[:want_k], meta

    reranked: list[dict[str, Any]] = []
    for rank, index in enumerate(order, start=1):
        item = dict(hits[index])
        item["rerank_rank"] = rank
        reranked.append(item)

    meta["rerank_applied"] = True
    return reranked[:want_k], meta