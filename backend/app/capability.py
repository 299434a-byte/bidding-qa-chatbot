# ============================================================================
# 文件：app/capability.py —— 数据能力口径的「重算 + 写回」（ingest 侧）
#
# 【整体在做什么】
#   data_capability.yaml 是「这批数据到底能回答什么」的单一事实来源，问答最前面
#   那道闸门（app/boundary.py）只读它。这份口径不能靠人手维护——行数、缺失率、
#   时间范围每次建库都可能变，所以 ingest 结束时由本模块自动重算并写回。
#
# 【谁说了算】
#   · 机器重算（本模块每次写回都会刷新）：records.*.rows / records.*.chunk_count
#     / fields.*.missing_rate / coverage.date_from / coverage.date_to /
#     coverage.months / generated_at
#   · 人手写、写回时**原样保留**：coverage.province_scope / province_note /
#     graph.* / fields.*.note / fields.*.keywords，以及任何我们不认识的顶层键
#     （宁可留着也不敢删，删了就再也问不回来）。
#
# 【写文件必须原子】
#   先写同目录的临时文件，再 os.replace 整体换掉。这样即使写到一半进程被杀，
#   磁盘上也只会是「旧的完整文件」或「新的完整文件」，不会出现半截 YAML。
#
# 【失败不影响建库】
#   调用方（ingest）用 try/except 包着，写不进去只记 warning —— 口径旧一点总比
#   把整次建库搞失败强。
# ============================================================================

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Any, Iterator

from .config import Settings, get_settings
from .logging_setup import get_logger

logger = get_logger(__name__)

# 能力文件默认位置（与 boundary.load_capability 同一个默认值，可用 DATA_CAPABILITY_PATH 覆盖）
CAPABILITY_PATH = Path(__file__).resolve().with_name("data_capability.yaml")

# 重写文件时前置的说明性头注释：yaml 解析器不保留注释，所以每次写回都重新贴上，
# 保证这份文件拿给谁看都能自解释（哪些字段手写、哪些机器重算、换语料要改什么）。
_HEADER = """\
# ============================================================================
# 数据能力边界 —— 单一事实来源（data_capability.yaml）
#
# 【这份文件整体在做什么】
#   把「当前这批数据到底能回答什么」写成一份可读、可机读的口径说明，供
#   app/boundary.py 在问答最前面判定「这问题数据根本不覆盖 → 直接拒答」，
#   而不是让模型拿 15 个月的局部样本硬编一个答案。
#
# 【哪些字段谁说了算】
#   · 手写、ingest 写回时原样保留：coverage.province_scope / province_note /
#     graph.* / fields.*.note / fields.*.keywords（这些是「口径说明」，机器算不出来）
#   · ingest 每次建库结束由 app/capability.py 重算并写回：records.*.rows /
#     records.*.chunk_count / fields.*.missing_rate / coverage.date_from /
#     coverage.date_to / coverage.months / generated_at
#   写回用「临时文件 + os.replace」原子替换，读到一半的半成品文件不会出现。
#
# 【判据可调】
#   时间越界 / 地区越界 / 字段缺失(>=0.8) / 图谱越界，阈值在 app/boundary.py；
#   换语料后必须重新核对本文件（时间范围、省份、缺失率都会变）。
# ============================================================================
"""

# 日期型列的识别：类型名（PG/MySQL 返回）或列名本身
_DATE_TYPE_HINT = ("date", "timestamp", "datetime", "time with", "time without")


# ----------------------------------------------------------------------------
# 逐行统计：在 generate_documents 的生成器外面包一层，边读边攒，不额外读一遍库
# ----------------------------------------------------------------------------
def new_tracker(columns: list[dict[str, Any]]) -> dict[str, Any]:
    """为一张表开一个统计器：行数、各列缺失计数、日期列的最小/最大值。"""
    names: list[str] = []
    missing: dict[str, int] = {}
    date_columns: list[str] = []
    for col in columns:
        name = str(col.get("name") or "")
        if not name:
            continue
        names.append(name)
        missing[name] = 0
        kind = str(col.get("type") or "").lower()
        if any(hint in kind for hint in _DATE_TYPE_HINT) or re.search(
            r"(^|_)date$|^date", name, re.I
        ):
            date_columns.append(name)
    return {
        "rows": 0,
        "names": names,
        "missing": missing,
        "date_columns": date_columns,
        "date_from": "",
        "date_to": "",
    }


def observe_rows(rows: Iterator[dict[str, Any]], tracker: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """包在源数据迭代器外面：每读一行就记一笔，再原样放行给 build_documents。

    只做「看一眼」，绝不改动行内容 —— 文档化逻辑必须一点没变。
    """
    for row in rows:
        _track(tracker, row)
        yield row


def _track(tracker: dict[str, Any], row: Any) -> None:
    if not isinstance(row, dict):
        return
    tracker["rows"] = int(tracker["rows"]) + 1
    missing: dict[str, int] = tracker["missing"]
    for name in tracker["names"]:
        value = row.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing[name] = missing.get(name, 0) + 1
            continue
        if name not in tracker["date_columns"]:
            continue
        text = str(value).strip()[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            continue
        low, high = tracker["date_from"], tracker["date_to"]
        if not low or text < low:
            tracker["date_from"] = text
        if not high or text > high:
            tracker["date_to"] = text


def summarize_table(tracker: dict[str, Any], chunk_count: int) -> dict[str, Any]:
    """把一张表的统计器压成写回 yaml 用的扁平结构。"""
    rows = int(tracker["rows"])
    rates: dict[str, float] = {}
    for name in tracker["names"]:
        total = tracker["missing"].get(name, 0)
        rates[name] = round(total / rows, 4) if rows else 0.0
    return {
        "rows": rows,
        "chunk_count": int(chunk_count),
        "missing_rate": rates,
        "date_from": tracker["date_from"],
        "date_to": tracker["date_to"],
    }


# ----------------------------------------------------------------------------
# 写回
# ----------------------------------------------------------------------------
def _capability_path(settings: Settings | None) -> Path:
    try:
        s = settings or get_settings()
        return Path(s.data_capability_path)
    except Exception:  # noqa: BLE001 - 配置读不到就用默认位置
        return CAPABILITY_PATH


def _read_existing(path: Path) -> dict[str, Any]:
    """读旧文件以便把「手写口径」原样保留；读不到就当从零开始（并留痕）。"""
    try:
        import yaml
    except Exception as exc:  # noqa: BLE001
        logger.warning("PyYAML 不可用，数据能力口径无法写回：%s", exc)
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("数据能力文件尚不存在，将新建：%s", path)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("旧数据能力文件读取失败（手写口径将丢失）path=%s err=%s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _merge_dates(cov: dict[str, Any], partial: dict[str, Any]) -> dict[str, Any]:
    """用各表的 min/max 日期刷新 coverage 的时间范围（算不出就不动旧值）。"""
    lows = [t["date_from"] for t in partial.values() if t.get("date_from")]
    highs = [t["date_to"] for t in partial.values() if t.get("date_to")]
    if not lows or not highs:
        return cov
    date_from, date_to = min(lows), max(highs)
    try:
        start = dt.date.fromisoformat(date_from)
        end = dt.date.fromisoformat(date_to)
    except ValueError:
        return cov
    cov["date_from"] = date_from
    cov["date_to"] = date_to
    cov["months"] = (end.year - start.year) * 12 + (end.month - start.month) + 1
    return cov


def merge_capability(existing: dict[str, Any], partial: dict[str, Any]) -> dict[str, Any]:
    """把机器重算的部分并进旧口径；手写部分原样保留。"""
    data: dict[str, Any] = dict(existing)

    cov = dict(data.get("coverage") or {})
    cov = _merge_dates(cov, partial)
    data["coverage"] = cov

    records = dict(data.get("records") or {})
    fields = dict(data.get("fields") or {})
    for table, stats in partial.items():
        if not isinstance(stats, dict):
            continue
        records[table] = {
            "rows": int(stats.get("rows") or 0),
            "chunk_count": int(stats.get("chunk_count") or 0),
        }
        columns = dict(fields.get(table) or {})
        rates = stats.get("missing_rate") or {}
        if isinstance(rates, dict):
            for field, rate in rates.items():
                meta = dict(columns.get(field) or {})
                meta["missing_rate"] = float(rate)
                columns[field] = meta
        if columns:
            fields[table] = columns
    data["records"] = records
    data["fields"] = fields
    data["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return data


def render_capability(data: dict[str, Any]) -> str:
    """把口径渲染成「头注释 + YAML」的最终文件内容。"""
    import yaml

    body = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    return _HEADER + "\n" + body


def write_capability(
    partial: dict[str, Any],
    settings: Settings | None = None,
    path: Path | str | None = None,
) -> Path:
    """把本次建库统计出来的口径并进 data_capability.yaml 并原子落盘。

    Args:
        partial: ``generate_documents`` 攒出来的 ``stage_summary["capability"]``
        settings / path: 目标文件位置（path 优先；默认走 ``DATA_CAPABILITY_PATH``）

    Returns:
        实际写入的文件路径。``partial`` 为空时一条都不写（宁可留旧口径也不写空表），
        此时返回 ``None``（调用方只做日志，不影响建库结果）。
    """
    if not partial:
        logger.warning("本次建库没有任何表格统计，数据能力口径保持原样")
        return None  # type: ignore[return-value]

    import yaml

    target = Path(path) if path is not None else _capability_path(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = merge_capability(_read_existing(target), partial)
    content = render_capability(merged)

    tmp = target.parent / f".{target.name}.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    logger.info(
        "数据能力口径已写回：%s（%d 张表，generated_at=%s）",
        target,
        len(merged.get("records") or {}),
        merged.get("generated_at"),
    )
    return target
