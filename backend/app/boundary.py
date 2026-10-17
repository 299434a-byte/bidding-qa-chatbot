# ============================================================================
# 文件：app/boundary.py —— 数据边界闸门（这道闸门挡的是「数据根本不覆盖」）
#
# 【为什么需要这道闸门】
#   原来只有「检索覆盖率不足」这一层判据，挡不住另一类更危险的问题：数据本身
#   不覆盖。库里只有 2023-07 ~ 2024-09 这 15 个月、只覆盖安徽一省、图谱没有公司
#   与股权关系，却照样会检索、照样会让模型答——于是「近五年有哪些百万级软件
#   项目？」会得到一个建立在局部样本上的答案。本模块在**问答最前面**拦掉它。
#
# 【四条铁律】
#   ① check_boundary 是**纯函数纯规则**：零模型调用、零 I/O、零网络；
#   ② 判不准一律放行 —— 能力文件缺失 / 读失败 / 解析失败 / 关键字没配，全部
#      返回 blocked=False（绝不因为缺一个文件就拒答）；
#   ③ 拦截文案统一「yaml 里的真实口径 + 模板」生成，不许各处各写一套；
#   ④ 拦截时不检索、不调模型，直接发 boundary_blocked 事件后 done。
#
# 【四类规则】（任一命中即拦，命中即返回，顺序固定）
#   time   —— 时间越界：近N年 / N年内 / YYYY年至今 / YYYY年区间 / 超出年份 / 未来
#   field  —— 字段缺失：问题点名的字段 missing_rate >= MISSING_RATE_LIMIT
#   graph  —— 图谱越界：问 not_covered 的主体 + 关系类问法
#   region —— 地区越界：点名了 province_scope 之外的省份，或问「全国 / 各省」
# ============================================================================

from __future__ import annotations

import datetime as dt
import re
import threading
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

logger = get_logger(__name__)

# 能力文件默认位置（可用 DATA_CAPABILITY_PATH 覆盖；测试会指到沙箱外的临时文件）
CAPABILITY_PATH = Path(__file__).resolve().with_name("data_capability.yaml")

# 字段缺失率阈值：达到它且问题点名该字段 → 拦截
MISSING_RATE_LIMIT = 0.8

# 一份「不拦截」的标准结论（所有分支共用，键永远齐，前端拿到啥都不缺）
_ALLOW: dict[str, Any] = {
    "blocked": False,
    "rule": "",
    "reason": "",
    "real_scope": "",
    "suggestion": [],
    "message": "",
}

# ----------------------------------------------------------------------------
# 能力文件读取：mtime 失效缓存 + 任何失败都降级成「不拦截」
# ----------------------------------------------------------------------------
_CACHE: dict[str, tuple[int, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def _default_path() -> Path:
    """默认能力文件路径（走配置，便于测试指到沙箱外）。"""
    try:
        from .config import get_settings

        return Path(get_settings().data_capability_path)
    except Exception:  # noqa: BLE001 - 配置读不到就用模块默认，绝不因此拒答
        return CAPABILITY_PATH


def load_capability(path: Any = None) -> dict[str, Any]:
    """读数据能力 yaml（按 mtime 失效缓存）。

    ``path`` 三种给法都行：不给（走 ``DATA_CAPABILITY_PATH``）、给路径、直接给一个
    ``Settings``（问答入口就是这么调的）。认不出来的一律当「口径不明」处理。

    **读失败一律降级**：文件缺失 / 不可读 / 语法坏 / 不是 dict / 没装 pyyaml，
    全部返回 ``{}``。空能力 = 所有规则都算不出结论 = 不拦截 ——
    绝不因为缺一个文件就把用户的问题拒掉。
    """
    try:
        if path is None:
            target = _default_path()
        elif hasattr(path, "data_capability_path"):  # Settings：问答入口直接丢 settings 进来
            target = Path(path.data_capability_path)
        else:
            # str / PathLike；给个不是路径的东西 → TypeError 走下面的 except → 不拦截
            target = Path(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据能力路径解析失败（本次不拦截）：%s", exc)
        return {}

    key = str(target)
    try:
        mtime = target.stat().st_mtime_ns
    except OSError:
        # 最常见：文件还没生成（首次 ingest 之前）→ 不拦截
        return {}

    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]

    try:
        import yaml
    except Exception as exc:  # noqa: BLE001 - 依赖缺失也降级，不拦截
        logger.warning("PyYAML 不可用（本次不拦截）：%s", exc)
        return {}

    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 坏文件降级，不拦截
        logger.warning("数据能力文件读取失败 path=%s err=%s（本次不拦截）", target, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("数据能力文件不是映射结构 path=%s（本次不拦截）", target)
        return {}

    with _CACHE_LOCK:
        _CACHE[key] = (mtime, data)
    return data


def clear_capability_cache() -> None:
    """清空缓存（测试用；生产路径靠 mtime 自动失效）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _shift_years(day: dt.date, delta: int) -> dt.date:
    """按年平移日期（2/29 平移到非闰年时夹到 2/28），不引第三方库。"""
    year = day.year + delta
    try:
        return day.replace(year=year)
    except ValueError:
        return day.replace(year=year, month=2, day=28)


# 中文数字（问题里常写「近五年」「十年内」而不是 5 / 10）
_CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _to_int(text: str) -> int | None:
    """把「5」或「三十五」「二十」转成整数；认不出来返回 None（= 不判）。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    section = 0
    num = 0
    for ch in text:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        else:
            return None
    total = section + num
    return total if total > 0 else None


# 全国 34 个省级行政区短名（判「省外地区」用；命中名单内的视为范围内）
PROVINCE_NAMES: tuple[str, ...] = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "广西", "海南",
    "重庆", "四川", "贵州", "云南", "西藏",
    "陕西", "甘肃", "青海", "宁夏", "新疆",
    "台湾", "香港", "澳门",
)

# 图谱答不了的「关系」问法（not_covered 的主体 + 这些词 = 图谱越界）
_RELATION_RE = re.compile(
    r"股权|控股|参股|股东|关联|关系|供应链|上下游|投资人|实际控制|出资|合资"
    r"|并购|收购|母子公司|穿透"
)

# —— 时间规则用到的四组模式 ——
# 相对窗口：近五年 / 最近3年 / 过去十年 / 5年内（前面不能是数字，避免吃掉 2024年）
_REL_YEAR_RE = re.compile(
    r"(?<!\d)(?:最近|近|过去|未来|今后)?\s*(\d{1,3}|[一二两三四五六七八九十百]{1,4})"
    r"\s*年(?:内|来|以来)?"
)
_YEAR_RANGE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*年?\s*(?:至|到|—|–|-|~|～)\s*((?:19|20)\d{2})\s*年"
)
_SINCE_YEAR_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*年\s*(?:至今|到现在|以来)"
)
_ABS_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年")
# 「全国 / 各省」：数据只有一省，一问全国就是越界
_NATION_RE = re.compile(r"全国|各省|全省|各省市|全国各省|全国各地")


# ----------------------------------------------------------------------------
# 拒答文案：统一「真实口径 + 模板」，不允许各处各写一套
# ----------------------------------------------------------------------------
def _real_scope(cap: dict[str, Any]) -> str:
    """用 yaml 拼一句「真实口径」，四类规则共用。"""
    cov = _as_dict(cap.get("coverage"))
    graph = _as_dict(cap.get("graph"))
    parts: list[str] = []

    date_from = str(cov.get("date_from") or "")
    date_to = str(cov.get("date_to") or "")
    months = cov.get("months")
    if date_from and date_to:
        span = f"时间 {date_from} 至 {date_to}"
        if months:
            span += f"（约 {int(months)} 个月）"
        parts.append(span)

    scope = [str(x).strip() for x in _as_list(cov.get("province_scope")) if str(x).strip()]
    if scope:
        parts.append("地区限 " + "、".join(scope))

    records = _as_dict(cap.get("records"))
    rows = sum(int(_as_float(_as_dict(v).get("rows")) or 0) for v in records.values())
    chunks = sum(int(_as_float(_as_dict(v).get("chunk_count")) or 0) for v in records.values())
    if rows or chunks:
        parts.append(f"{rows} 行记录 / {chunks} 个文档块")

    not_covered = [str(x).strip() for x in _as_list(graph.get("not_covered")) if str(x).strip()]
    if not_covered:
        parts.append("图谱未覆盖 " + "、".join(not_covered))

    return "；".join(parts) if parts else "（数据口径未登记）"


def _build_message(reason: str, scope: str, suggestion: list[str]) -> str:
    """统一拒答模板：为什么拒 + 真实口径 + 第一条替代问法。"""
    lines = ["这个问题超出了当前数据的覆盖范围，先不作答（不拿局部数据编答案）。"]
    if reason:
        lines.append(f"原因：{reason}")
    if scope:
        lines.append(f"真实口径：{scope}")
    if suggestion:
        lines.append(f"可以换成：{suggestion[0]}")
    return "\n".join(lines)


def _blocked(
    rule: str,
    reason: str,
    cap: dict[str, Any],
    suggestion: list[str],
) -> dict[str, Any]:
    scope = _real_scope(cap)
    clean = [s for s in suggestion if isinstance(s, str) and s.strip()][:3]
    return {
        "blocked": True,
        "rule": rule,
        "reason": reason,
        "real_scope": scope,
        "suggestion": clean,
        "message": _build_message(reason, scope, clean),
    }


# ----------------------------------------------------------------------------
# 主体提取：把「越界的那一截」从问题里剥掉，剩下的才是还能问的东西
# ----------------------------------------------------------------------------
def _strip_time(question: str) -> str:
    spans = [
        m.span()
        for pattern in (_YEAR_RANGE_RE, _SINCE_YEAR_RE, _REL_YEAR_RE, _ABS_YEAR_RE)
        for m in pattern.finditer(question)
    ]
    return _tidy(question, spans)


def _strip_region(question: str, scope: list[str]) -> str:
    spans: list[tuple[int, int]] = []
    if _NATION_RE.search(question):
        spans.extend(m.span() for m in _NATION_RE.finditer(question))
    for name in PROVINCE_NAMES:
        if name in scope:
            continue
        pattern = re.compile(re.escape(name) + r"(?:省|市|自治区|特别行政区)?")
        spans.extend(m.span() for m in pattern.finditer(question))
    return _tidy(question, spans)


def _tidy(question: str, spans: list[tuple[int, int]]) -> str:
    """按区间删除，再收拾残留的连接词与标点；剥空了就退回原文。"""
    if not spans:
        body = question
    else:
        kept: list[str] = []
        cursor = 0
        for start, end in sorted(set(spans)):
            if start < cursor:
                continue
            kept.append(question[cursor:start])
            cursor = end
        kept.append(question[cursor:])
        body = "".join(kept)
    body = re.sub(r"\s+", " ", body)
    # 剥掉时间 / 地区片段后，句子开头常剩个悬空的「的 / 在 / 了」（「2030年的项目」→
    # 「的项目」），连同连接词与标点一起收拾掉，别把残渣塞进替代问法里。
    body = re.sub(r"^[至到在了的,，、;；:：\s]+|[至到,，、;；:：\s]+$", "", body)
    body = body.strip()
    return body if len(body) >= 4 else question.strip()


def _body_of(question: str) -> str:
    """去掉句末标点，方便在后面统一补「？」。"""
    return question.rstrip("？?。.！!；; ").strip()


# ----------------------------------------------------------------------------
# 四类规则
# ----------------------------------------------------------------------------
def _time_suggestions(body: str, cap: dict[str, Any]) -> list[str]:
    cov = _as_dict(cap.get("coverage"))
    date_from = str(cov.get("date_from") or "")
    date_to = str(cov.get("date_to") or "")
    if not (date_from and date_to):
        return _generic_suggestions(cap)
    # 剥时间片段时没剥干净（剩下的自己还会越界）→ 换通用问法，
    # 宁可给一条笼统的，也不给「点一下又被拦」的死循环选项。
    if any(p.search(body) for p in (_YEAR_RANGE_RE, _SINCE_YEAR_RE, _REL_YEAR_RE, _ABS_YEAR_RE)):
        return _generic_suggestions(cap)
    months = cov.get("months")
    records = _as_dict(cap.get("records"))
    rows = sum(int(_as_float(_as_dict(v).get("rows")) or 0) for v in records.values())
    out = [f"{date_from} 至 {date_to} 期间，{body}？"]
    if months:
        out.append(f"{date_from[:7]} 到 {date_to[:7]}（数据覆盖的 {int(months)} 个月）里，{body}？")
    if rows:
        out.append(f"在已入库的 {rows} 条记录中，{body}？")
    return out


def _check_time(question: str, cap: dict[str, Any], today: dt.date) -> dict[str, Any] | None:
    cov = _as_dict(cap.get("coverage"))
    date_from = _parse_date(cov.get("date_from"))
    date_to = _parse_date(cov.get("date_to"))
    if date_from is None or date_to is None:
        return None  # 口径不全 → 判不准 → 放行
    from_year, to_year = date_from.year, date_to.year
    body = _body_of(_strip_time(question))

    # ① 未来年份：数据不可能有
    for match in _ABS_YEAR_RE.finditer(question):
        year = int(match.group(1))
        if year > today.year:
            return _blocked(
                "time",
                f"所问的 {year} 年还在未来，数据不可能覆盖",
                cap,
                _time_suggestions(body, cap),
            )

    # ② 相对窗口（近N年 / N年内 / N年以来）：窗口必须整个落在覆盖期内
    rel = _REL_YEAR_RE.search(question)
    if rel:
        count = _to_int(rel.group(1) or "")
        if count:
            start, end = _shift_years(today, -count), today
            if start < date_from or end > date_to:
                return _blocked(
                    "time",
                    f"所问的近 {count} 年（{start.isoformat()} 至 {end.isoformat()}）"
                    f"超出数据覆盖期 {date_from.isoformat()} ~ {date_to.isoformat()}",
                    cap,
                    _time_suggestions(body, cap),
                )

    # ③ 「YYYY年至今 / 以来」：终点是今天，数据早截止了
    since = _SINCE_YEAR_RE.search(question)
    if since:
        year = int(since.group(1))
        start = dt.date(year, 1, 1)
        if start < date_from or today > date_to:
            return _blocked(
                "time",
                f"「{year}年至今」超出数据截止日 {date_to.isoformat()}（数据并不覆盖到今天）",
                cap,
                _time_suggestions(body, cap),
            )

    # ④ 「YYYY年至YYYY年」：任一端出界即拦
    span = _YEAR_RANGE_RE.search(question)
    if span:
        y1, y2 = int(span.group(1)), int(span.group(2))
        if y1 < from_year or y2 > to_year:
            return _blocked(
                "time",
                f"所问的 {y1}~{y2} 年超出数据覆盖年份 {from_year}~{to_year}",
                cap,
                _time_suggestions(body, cap),
            )
        return None

    # ⑤ 单个绝对年份：落在覆盖年份内就放行（部分月份缺失属可作答范围）
    for match in _ABS_YEAR_RE.finditer(question):
        year = int(match.group(1))
        if year < from_year or year > to_year:
            return _blocked(
                "time",
                f"{year} 年不在数据覆盖年份 {from_year}~{to_year} 内",
                cap,
                _time_suggestions(body, cap),
            )
    return None


def _check_region(question: str, cap: dict[str, Any]) -> dict[str, Any] | None:
    cov = _as_dict(cap.get("coverage"))
    scope = [str(x).strip() for x in _as_list(cov.get("province_scope")) if str(x).strip()]
    if not scope:
        return None
    body = _body_of(_strip_region(question, scope))

    cov = _as_dict(cap.get("coverage"))
    date_from = str(cov.get("date_from") or "")
    date_to = str(cov.get("date_to") or "")
    scoped = "、".join(scope)
    # 替代问法必须真的落在范围内 —— 只换地区口径，不换问题本身；
    # 剥完还剩越界地名（剥不干净）就退回通用问法，绝不给出点了会被再拦的选项。
    if _NATION_RE.search(body) or any(p not in scope and p in body for p in PROVINCE_NAMES):
        alts = _generic_suggestions(cap)
    else:
        alts = [f"只看{scoped}，{body}？", f"{scope[0]}范围内，{body}？"]
        if date_from and date_to:
            alts.append(f"{date_from} 至 {date_to}、{scoped}，{body}？")

    if _NATION_RE.search(question):
        return _blocked(
            "region",
            f"问题问的是全国 / 各省，而数据只覆盖 {scoped}",
            cap,
            alts,
        )
    for name in PROVINCE_NAMES:
        if name in scope:
            continue
        if name in question:
            return _blocked(
                "region",
                f"问题点名了「{name}」，而数据只覆盖 {scoped}",
                cap,
                alts,
            )
    return None


def _generic_suggestions(cap: dict[str, Any]) -> list[str]:
    """字段 / 图谱两类：剥关键词会把句子剥烂，给一组不越界的通用问法。"""
    cov = _as_dict(cap.get("coverage"))
    date_from = str(cov.get("date_from") or "")
    date_to = str(cov.get("date_to") or "")
    scope = [str(x).strip() for x in _as_list(cov.get("province_scope")) if str(x).strip()]
    out: list[str] = []
    if date_from and date_to:
        out.append(f"{date_from} 至 {date_to} 期间，有哪些项目？")
        out.append(f"{date_to[:4]} 年有哪些招标公告？")
    if scope:
        out.append(f"{scope[0]}有哪些项目公告？")
    return out or ["数据里有哪些项目？"]


def _check_field(question: str, cap: dict[str, Any]) -> dict[str, Any] | None:
    fields = _as_dict(cap.get("fields"))
    lowered = question.lower()
    for table, columns in fields.items():
        for field, meta in _as_dict(columns).items():
            rate = _as_float(_as_dict(meta).get("missing_rate"))
            # 缺失率不到阈值 / 算不出来 → 这个字段照常可用，不拦
            if rate is None or rate < MISSING_RATE_LIMIT:
                continue
            keywords = [
                str(k).strip().lower()
                for k in _as_list(_as_dict(meta).get("keywords"))
                if str(k).strip()
            ]
            if not keywords:
                continue  # 没配关键词就不猜 —— 判不准就放行
            hit = next((k for k in keywords if k in lowered), None)
            if hit:
                note = str(_as_dict(meta).get("note") or "").strip()
                detail = f"{table}.{field} 字段缺失率 {rate:.0%}"
                if note:
                    detail += f"（{note}）"
                return _blocked(
                    "field",
                    f"问题问到「{hit}」，但 {detail}，据此得出的结论只会是瞎猜",
                    cap,
                    _generic_suggestions(cap),
                )
    return None


def _check_graph(question: str, cap: dict[str, Any]) -> dict[str, Any] | None:
    graph = _as_dict(cap.get("graph"))
    topics = [str(t).strip() for t in _as_list(graph.get("not_covered")) if str(t).strip()]
    if not topics:
        return None
    if not _RELATION_RE.search(question):
        return None  # 只是提到这些词、没问关系 → 知识库还能答，放行
    hit = next((t for t in topics if t in question), None)
    if not hit:
        return None
    labels = [str(x).strip() for x in _as_list(graph.get("labels")) if str(x).strip()]
    relations = [str(x).strip() for x in _as_list(graph.get("relations")) if str(x).strip()]
    have = "、".join(labels + relations) or "项目 / 公告"
    return _blocked(
        "graph",
        f"问题问的是「{hit}」的关系，但图谱只有 {have}，没有这类节点与关系边",
        cap,
        _generic_suggestions(cap),
    )


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def check_boundary(
    question: str,
    capability: dict[str, Any] | None,
    *,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """判断问题是否超出数据边界。**纯函数、纯规则、零模型调用。**

    Args:
        question: 用户问题原文
        capability: ``load_capability()`` 读到的能力 yaml；空 / None = 不拦截
        today: 判「未来 / 相对窗口」用的当天日期（默认取今天，测试可注入）

    Returns:
        ``{"blocked", "rule", "reason", "real_scope", "suggestion", "message"}``
        —— 键恒定；``blocked=False`` 时其余字段为空串 / 空列表。
    """
    if not isinstance(question, str) or not question.strip():
        return dict(_ALLOW)
    cap = capability if isinstance(capability, dict) else {}
    if not cap:
        # 能力文件缺失或读失败：降级为「不拦截」，绝不因缺文件拒答
        return dict(_ALLOW)

    as_of = today or dt.date.today()
    verdict = (
        _check_time(question, cap, as_of)
        or _check_field(question, cap)
        or _check_graph(question, cap)
        or _check_region(question, cap)
    )
    if verdict is None:
        return dict(_ALLOW)
    return verdict
