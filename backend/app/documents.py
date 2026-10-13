
"""documents：源表行 → 自然语言文档（带中文标签，供切块与向量化）。

职责：
  - 把表格行翻译成连贯中文文本（字段名取库内列注释）
  - 生成稳定 doc_id（表名:主键:块号）与元数据
  - 控制切块粒度（块大小与重叠）

依赖：config
被谁调用：ingest（建库管道）、vector_store（文档元数据约定）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any, Iterator

from .config import Settings

_FIELD_TEXT_LIMIT = 4000

_SKIP_TYPES = {"blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary"}


@dataclass
class Document:
    """一条可检索的最小单元。"""

    doc_id: str
    table: str
    label: str
    primary_key: Any
    chunk_index: int
    chunk_total: int
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


def render_row(
    row: dict[str, Any],
    columns: list[dict[str, Any]],
    label: str,
    primary_key: Any,
) -> str:
    """把一行记录渲染成「字段: 值」的多行文本。"""
    lines = [f"【{label}】记录ID：{primary_key}"]
    for col in columns:
        name = col["name"]
        if col.get("type") in _SKIP_TYPES:
            continue
        value = row.get(name)
        if value is None or value == "":
            continue
        text = str(value).strip()
        if not text:
            continue
        field_label = col.get("comment") or name
        lines.append(f"{field_label}: {text}")
    return "\n".join(lines)


def split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """按字符滑窗切块；文本不长时原样返回单块。"""
    if chunk_size <= 0 or len(text) <= chunk_size:
        return [text]
    step = max(1, chunk_size - max(0, overlap))
    chunks: list[str] = []
    for start in range(0, len(text), step):
        piece = text[start : start + chunk_size]
        if piece.strip():
            chunks.append(piece)
        if start + chunk_size >= len(text):
            break
    return chunks or [text]


def _truncate_fields(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, str) and len(value) > _FIELD_TEXT_LIMIT:
            out[key] = value[:_FIELD_TEXT_LIMIT] + "…（已截断）"
        else:
            out[key] = value
    return out


def build_documents(
    rows: Iterator[dict[str, Any]],
    columns: list[dict[str, Any]],
    table: str,
    label: str,
    settings: Settings,
    primary_key_column: str | None = None,
) -> Iterator[Document]:
    """逐行生成文档（行内容过长时会产生多块）。"""
    for index, row in enumerate(rows):
        pk = row.get(primary_key_column) if primary_key_column else None
        if pk is None or pk == "":
            pk = index + 1

        body = render_row(row, columns, label, pk)
        if not body.strip():
            continue

        if len(body) <= settings.max_doc_chars:
            chunks = [body]
        else:
            chunks = split_text(body, settings.chunk_size, settings.chunk_overlap)

        payload_fields = _truncate_fields(row)
        for i, chunk in enumerate(chunks):
            yield Document(
                doc_id=f"{table}:{pk}:{i}",
                table=table,
                label=label,
                primary_key=pk,
                chunk_index=i,
                chunk_total=len(chunks),
                text=chunk,
                payload={
                    "table": table,
                    "label": label,
                    "primary_key": pk,
                    "chunk_index": i,
                    "chunk_total": len(chunks),
                    "fields": payload_fields,
                },
            )