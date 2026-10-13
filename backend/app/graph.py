
"""graph：Neo4j 知识图谱查询工具（自然语言 → 只读 Cypher）。

职责：
  - 读取图谱 schema（标签、关系类型、属性）供提示词组装
  - 让 LLM 生成 Cypher，经只读护栏校验后执行并渲染结果
  - 查询超时保护；未配置时工具不注册

依赖：config、llm（Cypher 生成）
被谁调用：main（/api/graph/schema、/api/agent/chat）、agent（图谱查询工具）
"""

from __future__ import annotations

import asyncio

import atexit

import json

import re

import time

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from .config import Settings, get_settings

from .llm import LLMError, LLMNotConfigured, complete_chat

GRAPH_TOOL_NAME = "query_bidding_graph"

_CELL_TEXT_LIMIT = 300

_ROWS_TO_STATE = 20

_FORBIDDEN_KEYWORDS = (
    "create", "merge", "delete", "detach", "set", "remove", "drop", "foreach",
    "load", "csv", "grant", "deny", "revoke", "apoc", "dbms", "start", "stop",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)
_CALL_SUBQUERY_RE = re.compile(r"\bcall\s*\{", re.IGNORECASE)
_START_RE = re.compile(r"^\s*(match|optional\s+match|with|unwind|return)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)
_LIMIT_ANY_RE = re.compile(r"\blimit\b", re.IGNORECASE)
_LIMIT_ALL_RE = re.compile(r"\blimit\s+all\b", re.IGNORECASE)
_FETCH_FIRST_RE = re.compile(r"\bfetch\s+(?:first|next)\s+(?:(\d+)\s+)?rows?\s+only\b", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

_schema_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

_drivers: dict[tuple[str, str, str, str], Any] = {}


class GraphError(RuntimeError):
    """Neo4j 查询的可读错误（连不上 / 生成失败 / 被护栏拒绝）。"""


def _get_driver(s: Settings):
    """取（并缓存）连接驱动。``connection_timeout`` 限制建连时间；全程只读访问。"""
    try:
        from neo4j import GraphDatabase, READ_ACCESS
    except ImportError as exc:  # pragma: no cover
        raise GraphError("缺少依赖 neo4j，请执行 pip install neo4j") from exc

    key = (s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
    driver = _drivers.get(key)
    if driver is None:
        try:
            driver = GraphDatabase.driver(
                s.neo4j_uri,
                auth=(s.neo4j_user, s.neo4j_password),
                default_access_mode=READ_ACCESS,
                connection_timeout=max(1, s.neo4j_timeout),
                max_transaction_retry_time=max(1, s.neo4j_timeout),
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
            raise GraphError(f"创建 Neo4j 连接失败（{s.neo4j_uri}）：{exc}") from exc
        _drivers[key] = driver
    return driver


def _close_drivers() -> None:
    for driver in list(_drivers.values()):
        try:
            driver.close()
        except Exception:  # noqa: BLE001 - 退出钩子里绝不抛异常
            pass
    _drivers.clear()


atexit.register(_close_drivers)


def _query(cypher: str, s: Settings):
    from neo4j import Query

    seconds = s.neo4j_query_timeout
    return Query(cypher, timeout=float(seconds) if seconds and seconds > 0 else None)


def _session(s: Settings):
    return _get_driver(s).session(database=s.neo4j_database or None)


def _read_rows(s: Settings, cypher: str) -> list[dict[str, Any]]:
    """在**只读事务**里执行生成的 Cypher（第 1 层安全边界）。"""
    try:
        with _session(s) as session:
            query = _query(cypher, s)
            return session.execute_read(lambda tx: tx.run(query).data())
    except GraphError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        raise GraphError(f"连接或执行 Neo4j 失败（{s.neo4j_uri}）：{exc}") from exc


def _introspect_rows(s: Settings, cypher: str) -> list[dict[str, Any]]:
    """执行**固定**的结构自省语句（语句由代码写死，不由模型生成）。"""
    try:
        with _session(s) as session:
            return session.run(_query(cypher, s)).data()
    except GraphError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示
        raise GraphError(f"连接或执行 Neo4j 失败（{s.neo4j_uri}）：{exc}") from exc


def _quote_ident(name: str) -> str:
    """用反引号包裹标签/属性名，避免特殊字符导致的语法错误。"""
    return "`" + str(name).replace("`", "``") + "`"


def _labels_text(labels: Any) -> str:
    """把节点标签列表渲染成 ``:A:B`` 形式。"""
    if not isinstance(labels, (list, tuple)):
        return str(labels)
    return "".join(f":{label}" for label in labels) or ":（无标签）"


def _read_schema_live(s: Settings) -> dict[str, Any]:
    """读取图谱结构：标签、关系类型、关系三元组、各标签的属性/样例/计数。"""
    labels = [
        row["label"]
        for row in _introspect_rows(s, "CALL db.labels() YIELD label RETURN label ORDER BY label")
        if row.get("label")
    ]
    rel_types = [
        row["relationshipType"]
        for row in _introspect_rows(
            s, "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType"
        )
        if row.get("relationshipType")
    ]

    triples = _introspect_rows(
        s,
        "MATCH (a)-[r]->(b) "
        "RETURN labels(a) AS from_labels, type(r) AS rel, labels(b) AS to_labels, count(*) AS c "
        "ORDER BY c DESC LIMIT 500",
    )

    nodes: list[dict[str, Any]] = []
    for label in labels:
        quoted = _quote_ident(label)
        try:
            key_rows = _introspect_rows(
                s, f"MATCH (n:{quoted}) WITH n LIMIT 20 UNWIND keys(n) AS k RETURN DISTINCT k AS key ORDER BY key"
            )
            props = [row["key"] for row in key_rows if row.get("key")]
        except Exception:  # noqa: BLE001 - 属性只是辅助信息，取不到不影响主流程
            props = []
        try:
            sample_rows = _introspect_rows(
                s, f"MATCH (n:{quoted}) RETURN properties(n) AS props LIMIT 2"
            )
            samples = [row.get("props") or {} for row in sample_rows]
        except Exception:  # noqa: BLE001
            samples = []
        try:
            count_rows = _introspect_rows(s, f"MATCH (n:{quoted}) RETURN count(n) AS c")
            count = int(count_rows[0]["c"]) if count_rows else 0
        except Exception:  # noqa: BLE001
            count = None
        nodes.append({"label": label, "count": count, "properties": props, "samples": samples})

    return {
        "database": s.neo4j_database,
        "labels": labels,
        "relationship_types": rel_types,
        "triples": triples,
        "nodes": nodes,
    }


def load_schema(s: Settings, refresh: bool = False) -> dict[str, Any]:
    """读取图谱结构（带 TTL 缓存）。结构变动后可等过期或用 ``refresh=True`` 强制重取。"""
    key = (s.neo4j_uri, s.neo4j_database)
    cached = _schema_cache.get(key)
    now = time.time()
    if not refresh and cached is not None and now - cached[0] < max(0, s.graph_schema_ttl):
        return cached[1]
    data = _read_schema_live(s)
    _schema_cache[key] = (now, data)
    return data


def render_schema_text(data: dict[str, Any]) -> str:
    """把结构字典渲染成给大模型看的紧凑文本。"""
    lines = [f"Neo4j 数据库：{data.get('database')}"]
    nodes = data.get("nodes") or []
    if not nodes:
        lines.append("（图谱中还没有任何节点）")
        return "\n".join(lines)

    lines.append("\n节点标签：")
    for node in nodes:
        count = node.get("count")
        count_text = f"约 {count} 个节点" if isinstance(count, int) else "节点数未知"
        lines.append(f'  "{node["label"]}"（{count_text}）')
        if node.get("properties"):
            lines.append("    属性：" + ", ".join(f'"{p}"' for p in node["properties"]))
        for sample in node.get("samples") or []:
            lines.append("    样例：" + json.dumps(sample, ensure_ascii=False, default=str))

    lines.append("\n关系类型：" + (", ".join(data.get("relationship_types") or []) or "（无）"))

    triples = data.get("triples") or []
    lines.append("\n关系三元组（起点标签 -[关系]-> 终点标签，含数量）：")
    if not triples:
        lines.append("  （图谱中还没有任何关系）")
    for row in triples:
        lines.append(
            f'  {_labels_text(row.get("from_labels"))} -[{row.get("rel")}]-> '
            f'{_labels_text(row.get("to_labels"))}（约 {row.get("c")} 条）'
        )
    return "\n".join(lines)


def _cypher_system_prompt(max_rows: int) -> str:
    return (
        "你是 Neo4j Cypher 专家，负责把用户的问题翻译成一条**只读** Cypher 查询。\n"
        "硬性要求：\n"
        "1. 只生成只读查询，严禁任何写操作（CREATE / MERGE / DELETE / SET / REMOVE / DROP 等）；\n"
        "2. 只能使用下面 schema 中真实出现的节点标签、关系类型与属性名，不要臆造；\n"
        "3. 关系方向的写法：若不确定方向，用无向匹配 `-[r:TYPE]-`；\n"
        f"4. 必须带 LIMIT，且不超过 {max_rows}；\n"
        "5. 只输出 Cypher 本身：不要解释、不要 markdown 代码块、不要结尾分号、不要多条语句。\n"
        "示例：MATCH (c:Company)-[:WINS]->(p:Project) WHERE c.name CONTAINS '空调' "
        f"RETURN c.name AS 公司, p.name AS 项目 LIMIT {max_rows}"
    )


def build_cypher_messages(question: str, schema_text: str, max_rows: int) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _cypher_system_prompt(max_rows)},
        {"role": "user", "content": f"图谱结构：\n{schema_text}\n\n用户问题：{question}"},
    ]


def _scan_cypher_text(cypher: str) -> tuple[str, int | None]:
    out = list(cypher)
    cut: int | None = None
    quote: str | None = None
    i, n = 0, len(cypher)
    while i < n:
        ch = cypher[i]
        if quote is not None:
            if ch == "\\" and quote != "`":
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and cypher[i + 1] == quote:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                quote = None
                i += 1
                continue
            out[i] = " "
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            i += 1
            continue
        if cypher[i : i + 2] in ("//", "--"):
            cut = i
            while i < n and cypher[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out), cut


def _strip_line_comments(text: str) -> str:
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and quote != "`" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    out.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if text[i : i + 2] in ("//", "--"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_comments(cypher: str) -> str:
    text = (cypher or "").strip()
    while text:
        _, cut = _scan_cypher_text(text)
        if cut is None:
            return text
        tail = text[cut:]
        nl = tail.find("\n")
        if nl != -1 and tail[nl + 1 :].strip():
            return text
        text = text[:cut].rstrip()
    return text


def clean_cypher(raw: str) -> str:
    """剥掉代码围栏、行注释、结尾分号（大模型输出不总是听话）。"""
    text = (raw or "").strip()
    text = _CODE_FENCE_RE.sub("", text).strip()
    text = _strip_line_comments(text)
    text = re.sub(r"[;\s]+$", "", text)
    return text.strip()


def _has_multiple_statements(cypher: str) -> bool:
    """判断引号/反引号外的分号（一条语句里不该出现）。"""
    in_single = False
    in_double = False
    in_backtick = False
    i = 0
    while i < len(cypher):
        ch = cypher[i]
        if in_single:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
        elif in_backtick:
            if ch == "`":
                in_backtick = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`":
            in_backtick = True
        elif ch == ";":
            return True
        i += 1
    return False


def validate_cypher(cypher: str, max_rows: int) -> str:
    """只读护栏 + 单语句 + 强制 LIMIT。返回规范化后的 Cypher，违规时抛 GraphError。"""
    if not cypher:
        raise GraphError("大模型没有生成可用的 Cypher")
    if not _START_RE.match(cypher):
        raise GraphError("生成的语句不是只读查询（必须以 MATCH / OPTIONAL MATCH / WITH / UNWIND / RETURN 开头）")
    hit = _FORBIDDEN_RE.search(cypher)
    if hit:
        raise GraphError(f"生成的语句包含被禁止的关键字：{hit.group(0).upper()}")
    if _CALL_SUBQUERY_RE.search(cypher):
        raise GraphError("生成的语句包含 CALL { ... } 子查询，已拒绝执行")
    if _has_multiple_statements(cypher):
        raise GraphError("生成的语句包含多条语句，已拒绝执行")

    cypher = _strip_trailing_comments(cypher)
    probe, _ = _scan_cypher_text(cypher)
    limit_all = _LIMIT_ALL_RE.search(probe)
    fetch = _FETCH_FIRST_RE.search(probe)
    if limit_all:
        cypher = f"{cypher[: limit_all.start()]}LIMIT {max_rows}{cypher[limit_all.end():]}"
    elif fetch:
        if fetch.group(1) is None or int(fetch.group(1)) > max_rows:
            cypher = f"{cypher[: fetch.start()]}LIMIT {max_rows}{cypher[fetch.end():]}"
    elif not _LIMIT_ANY_RE.search(probe):
        cypher = f"{cypher} LIMIT {max_rows}"
    else:
        matches = list(_LIMIT_RE.finditer(probe))
        if matches:
            last = matches[-1]
            if int(last.group(1)) > max_rows:
                cypher = f"{cypher[: last.start(1)]}{max_rows}{cypher[last.end(1):]}"
    return cypher


def _cell_text(value: Any) -> str:
    """把 Neo4j 返回的各种类型统一转成可读字符串（只依赖标准库 + 驱动自带转换）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_cell_text(v) for v in value) + "]"
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _truncate(text: str, limit: int = _CELL_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def run_query(s: Settings, cypher: str) -> list[dict[str, Any]]:
    """在只读事务中执行查询，返回行列表（每行为 列名→值 的字典）。"""
    rows = _read_rows(s, cypher)
    return rows[: max(1, s.neo4j_max_rows)]


def render_result(cypher: str, rows: list[dict[str, Any]]) -> str:
    """把查询结果渲染成给模型看的文本。"""
    header = f"【图谱查询结果】\nCypher：{cypher}"
    if not rows:
        return (
            header
            + "\n查询成功，但返回 0 条记录。\n"
            "请直接告诉用户图谱中没有查到相关记录，不要编造任何实体或关系。"
        )

    lines = [header, f"共 {len(rows)} 条："]
    for index, row in enumerate(rows, start=1):
        pairs = ", ".join(f"{key}={_truncate(_cell_text(val))}" for key, val in row.items())
        lines.append(f"{index}) {pairs}")
    return "\n".join(lines)


def _failure_text(reason: str) -> str:
    return (
        "【图谱查询结果】\n"
        f"查询失败：{reason}\n\n"
        "请如实向用户说明知识图谱这次查询没有取到结果（可附上失败原因），不要编造数据。"
    )


def _run_graph(question: str, state: dict[str, Any]) -> str:
    """同步阻塞段：读 schema → 生成 Cypher → 校验 → 执行 → 渲染。"""
    s: Settings | None = state.get("settings")
    cypher: str | None = None
    try:
        if s is None:
            raise GraphError("工具未注入配置（state 缺少 settings），已跳过执行")
        schema_text = render_schema_text(load_schema(s))
        raw = complete_chat(
            build_cypher_messages(question, schema_text, s.neo4j_max_rows),
            settings=s,
            model=s.graph_model_or_default,
            temperature=0.0,
            max_tokens=s.graph_max_tokens,
            timeout=s.graph_timeout,
        )
        cypher = validate_cypher(clean_cypher(raw), s.neo4j_max_rows)
        rows = run_query(s, cypher)

        state["cypher"] = cypher
        state["row_count"] = len(rows)
        state["rows"] = [
            {key: _truncate(_cell_text(val)) for key, val in row.items()} for row in rows[:_ROWS_TO_STATE]
        ]
        state["error"] = None
        return render_result(cypher, rows)
    except (GraphError, LLMError, LLMNotConfigured) as exc:
        reason = str(exc)
    except Exception as exc:  # noqa: BLE001 - 工具只返回可读提示，不向上抛
        reason = f"{type(exc).__name__}: {exc}"

    state["cypher"] = cypher
    state["row_count"] = 0
    state["rows"] = []
    state["error"] = reason
    return _failure_text(reason)


@tool
async def query_bidding_graph(question: str, config: RunnableConfig) -> str:
    """查询 Neo4j 知识图谱中的实体关系，用于关联、多跳、路径与关系计数类问题。"""
    state = (config.get("configurable") or {}).get("_graph_state")
    if state is None:
        state = {"settings": get_settings()}
    return await asyncio.to_thread(_run_graph, question, state)