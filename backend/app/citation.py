
"""citation：回答引用校验与标准话术（REF_1~REF_4）。

职责：
  - 校验回答中的 [N] 引用是否超出实际来源范围
  - 剔除无效引用序号（strip_bad_refs）
  - 维护四条标准拒答 / 兜底话术常量

依赖：无
被谁调用：rag（无数据文案与引用清洗）、agent（引用校验）
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

_AMOUNT_RE = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万元|万|元)")

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


def normalize_numbers(text: str | None) -> set[float | str]:
    """把文本里的金额/百分比/日期归一化为可比较的集合。"""
    s = _to_halfwidth(text)
    out: set[float | str] = set()

    for m in _AMOUNT_RE.finditer(s):
        num = _to_number(m.group("num"))
        if num is None:
            continue
        factor = 10000.0 if m.group("unit") in ("万元", "万") else 1.0
        out.add(round(num * factor, 4))

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

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "bad_refs": sorted(set(bad_refs)),
        "checked": len(unique_refs) + len(answer_numbers),
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