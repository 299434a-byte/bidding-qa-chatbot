
"""citation：回答引用校验与标准话术（REF_1~REF_4）。

职责：
  - 校验回答中的 [N] 引用是否超出实际来源范围
  - 剔除无效引用序号（strip_bad_refs）
  - 把校验结果聚合成逐句可信度分数（score_answer：confidence / hallucination_flags）
  - 维护四条标准拒答 / 兜底话术常量

依赖：无
被谁调用：rag（无数据文案与引用清洗、可信度评分）、agent / deep_agent（引用校验与可信度评分）
"""

from __future__ import annotations

import re

import unicodedata

from typing import Any

REF_1_RETRIEVAL_EMPTY = (
    "已在知识库中检索，但没有找到与您问题相关的记录。"
    "可能是该信息尚未收录，建议补充项目名称或换个更具体的问法再试。"
)
REF_2_INSUFFICIENT = (
    "检索到了相关资料，但资料内容不足以回答该问题。"
    "请补充更具体的信息（如项目名称、时间范围或编号）后重试。"
)
REF_3_OUT_OF_SCOPE = (
    "这个问题超出了本系统的职责范围：本系统只负责招投标相关问答，"
    "因此这次没有进行检索，也不予作答。"
)
REF_4_CAPABILITY = (
    "这个问题超出了当前系统的能力边界（例如需要主观判断、预测或外部实时数据），"
    "因此没有进行检索，也无法给出可靠回答。"
)

_CITE_RE = re.compile(r"\[参考资料(\d+)\]|\[参考(\d+)\]")

# 单位按「长的在前」排列：亿元 先于 亿、万元 先于 万，保证「3.5 亿元」整段命中。
_AMOUNT_RE = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>亿元|亿|万元|万|元)")

_PCT_RE = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<pct>[%％])")

_DATE_RE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?")


def _to_halfwidth(text: str | None) -> str:
    """全角 → 半角（NFKC 归一化），让「５０％」与「50%」能比出相等。"""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text)


def _to_number(raw: str) -> float | None:
    """把「1,590.00」这样的文本转成 float；解析不了返回 None。"""
    try:
        return float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _amount_factor(unit: str) -> float:
    """金额单位换算成「元」：亿→1e8、万→1e4、元→1。"""
    if unit.startswith("亿"):
        return 100_000_000.0
    if unit.startswith("万"):
        return 10_000.0
    return 1.0


def normalize_numbers(text: str | None) -> set[float | str]:
    """把文本里的金额/百分比/日期归一化为可比较的集合。"""
    s = _to_halfwidth(text)
    out: set[float | str] = set()

    for m in _AMOUNT_RE.finditer(s):
        num = _to_number(m.group("num"))
        if num is None:
            continue
        out.add(round(num * _amount_factor(m.group("unit")), 4))

    for m in _PCT_RE.finditer(s):
        num = _to_number(m.group("num"))
        if num is None:
            continue
        out.add(round(num, 6))

    for m in _DATE_RE.finditer(s):
        year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            out.add(f"{year}-{month:02d}-{day:02d}")

    return out


def _extract_refs(text: str | None) -> list[int]:
    """抽出回答里的引用编号（[参考资料N] / [参考N] → N）。"""
    refs: list[int] = []
    for m in _CITE_RE.finditer(text or ""):
        raw = m.group(1) or m.group(2)
        try:
            refs.append(int(raw))
        except (TypeError, ValueError):
            continue
    return refs


def _source_numbers(source: dict[str, Any]) -> set[float | str]:
    """把一条来源的正文与原始字段里的数字，归一化成一个集合。"""
    pieces: list[str] = [source.get("text") or ""]
    fields = source.get("fields") or {}
    for value in fields.values():
        if value not in (None, ""):
            pieces.append(str(value))
    return normalize_numbers("\n".join(pieces))


def _fmt_num(value: float | str) -> str:
    """把数值格式化成大人话，用于 issues 里的提示文案。"""
    if isinstance(value, str):
        return value
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _number_checks(answer: str, basis: set[float | str]) -> list[dict[str, Any]]:
    """抽取答案里每个金额/百分比/日期的逐字命中明细（按出现顺序，同写法去重）。"""
    entries: list[tuple[int, str, str, float | str]] = []
    half = _to_halfwidth(answer)

    for m in _AMOUNT_RE.finditer(half):
        num = _to_number(m.group("num"))
        if num is None:
            continue
        entries.append((m.start(), "number", m.group(0).strip(), round(num * _amount_factor(m.group("unit")), 4)))

    for m in _PCT_RE.finditer(half):
        num = _to_number(m.group("num"))
        if num is None:
            continue
        entries.append((m.start(), "percent", m.group(0).strip(), round(num, 6)))

    for m in _DATE_RE.finditer(half):
        year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            entries.append((m.start(), "date", m.group(0).strip(), f"{year}-{month:02d}-{day:02d}"))

    entries.sort(key=lambda e: e[0])
    checks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for _, kind, text, value in entries:
        if (kind, text) in seen:
            continue
        seen.add((kind, text))
        checks.append({"kind": kind, "text": text, "found": value in basis})
    return checks


def verify_citations(answer: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """校验回答里的引用编号与数字是否有依据。

    返回 ``checked`` = 本轮被检查的条目数（去重引用编号数 + 归一化数值数），
    供评测侧计算「无依据条数 ÷ 总校验条数」这类代理指标。
    """
    sources = list(sources or [])
    n_sources = len(sources)
    issues: list[dict[str, Any]] = []
    bad_refs: list[int] = []

    refs = _extract_refs(answer)
    unique_refs = sorted(set(refs))
    for n in unique_refs:
        if n < 1 or n > n_sources:
            issues.append({
                "type": "ref_out_of_range",
                "ref": n,
                "message": f"回答引用了不存在的 [参考资料{n}]（本轮来源共 {n_sources} 条）。",
            })
            bad_refs.append(n)

    answer_without_refs = _CITE_RE.sub(" ", answer or "")
    answer_numbers = normalize_numbers(answer_without_refs)

    cited_valid = sorted({n for n in refs if 1 <= n <= n_sources})
    if cited_valid:
        basis_sources = [sources[n - 1] for n in cited_valid]
    else:
        basis_sources = sources

    basis: set[float | str] = set()
    for src in basis_sources:
        basis |= _source_numbers(src)

    for value in sorted(answer_numbers, key=lambda v: str(v)):
        if value not in basis:
            issues.append({
                "type": "number_no_source",
                "value": value,
                "message": f"回答中的数值「{_fmt_num(value)}」未能在引用来源中找到依据。",
            })

    # 数字逐字命中明细（新增字段，不改 issues 的既有语义）：
    # 按答案中的出现顺序列出每个金额/百分比/日期原文写法及是否在引用来源中找到依据，
    # 供前端给「未命中」的数字加醒目标记（found=True 的只记录、不改变 ok 判定）。
    number_checks = _number_checks(answer_without_refs, basis)

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "bad_refs": sorted(set(bad_refs)),
        "checked": len(unique_refs) + len(answer_numbers),
        "number_checks": number_checks,
    }


# 断句分隔符：中文句末标点（。！？；…）与换行；英文 .!?; 只有在不把数字拆开时才是断点
# （「3.5 万元」不能被切成「3」和「5 万元」，所以 . 两侧不能都是数字）。
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;…\n\r]+|(?<!\d)\.(?!\d)")

# _split_sentences 里引用编号占位符的回填模式（占位符本身不含任何标点，不会被断句切开）。
_CITE_MASK_RE = re.compile(r"\x00(\d+)\x00")


def _split_sentences(answer: str | None) -> list[str]:
    """按中英文标点把回答切成句子；引用编号 ``[参考资料N]`` 内部不参与断句。

    切分前先把引用编号整段换成占位符、切完再换回来，保证标记完整地留在
    它所在的句子里。切出来为空白的片段直接丢弃；空回答返回空列表。
    """
    text = answer or ""
    if not text.strip():
        return []
    spans: list[str] = []

    def _mask(m: re.Match[str]) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"

    masked = _CITE_RE.sub(_mask, text)
    sentences: list[str] = []
    for part in _SENT_SPLIT_RE.split(masked):
        restored = _CITE_MASK_RE.sub(lambda mm: spans[int(mm.group(1))], part).strip()
        if restored:
            sentences.append(restored)
    return sentences


def score_answer(
    answer: str,
    sources: list[dict[str, Any]],
    verify_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """把 :func:`verify_citations` 的结果聚合成「逐句可信度」分数（纯函数，不抛异常）。

    判定口径（一句话要同时满足两条才算「有支撑」）：
      1. 句内有指向真实来源的引用编号（在 ``1..len(sources)`` 内且未被校验判为越界）；
      2. 句内的金额 / 百分比 / 日期都能在来源里逐字命中（复用 verify 的 number_checks 结论）。

    返回 ``confidence`` = 有支撑句数 ÷ 总句数；空回答（没有句子）时返回
    ``confidence=0.0``（不除零）。``hallucination_flags`` 逐句给出无支撑原因：
    ``no_citation``（缺引用）或 ``number_no_source``（数值在来源里找不到）。
    """
    sources = list(sources or [])
    verify = verify_result if isinstance(verify_result, dict) else {}
    bad_refs = {int(n) for n in (verify.get("bad_refs") or [])}

    # 比对基准与 verify_citations 同口径：优先「被引用到的真实来源」，
    # 整个回答没有任何有效引用时退回全部来源（数值层面仍按来源核对）。
    cited_valid = sorted(
        {n for n in _extract_refs(answer) if 1 <= n <= len(sources) and n not in bad_refs}
    )
    basis_sources = [sources[n - 1] for n in cited_valid] if cited_valid else sources
    basis: set[float | str] = set()
    for src in basis_sources:
        basis |= _source_numbers(src)

    # verify 的 number_checks 已按写法去重并给出 found 结论：同写法直接复用，
    # 保证 score 与 issues / 前端数字标记三者口径一致。
    found_by_text = {
        str(entry.get("text")): bool(entry.get("found"))
        for entry in (verify.get("number_checks") or [])
        if isinstance(entry, dict) and entry.get("text") is not None
    }

    sentences = _split_sentences(answer)
    supported = 0
    flags: list[dict[str, str]] = []
    for sentence in sentences:
        has_citation = any(
            1 <= n <= len(sources) and n not in bad_refs for n in _extract_refs(sentence)
        )
        numbers = _number_checks(_CITE_RE.sub(" ", sentence), basis)
        numbers_ok = all(
            found_by_text.get(entry["text"], entry["found"]) for entry in numbers
        )
        if has_citation and numbers_ok:
            supported += 1
            continue
        reason = "no_citation" if not has_citation else "number_no_source"
        flags.append({"sentence": sentence, "reason": reason})

    total = len(sentences)
    return {
        "confidence": (supported / total) if total else 0.0,
        "supported_sentences": supported,
        "total_sentences": total,
        "hallucination_flags": flags,
        "unsupported_sentences": len(flags),
    }


def strip_bad_refs(answer: str, bad_refs: list[int]) -> str:
    """只移除校验失败的引用标记，通过的保持原样。"""
    bad = {int(n) for n in (bad_refs or [])}
    if not bad:
        return answer

    def _replace(m: re.Match[str]) -> str:
        n = int(m.group(1) or m.group(2))
        return "" if n in bad else m.group(0)

    cleaned = _CITE_RE.sub(_replace, answer or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned