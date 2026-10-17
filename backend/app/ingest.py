
"""ingest：建库管道（源表 → 文档化 → BM25 → 向量化 → 写入 Qdrant）。

职责：
  - full（必须 recreate）全量重建与 incremental（算差集）增量更新
  - 差集比对（依赖主键 / doc_id），删除源表已移除数据对应的点
  - 后台线程执行 + 进度状态查询（进程内锁串行化，Qdrant 单进程独占）

依赖：db / pg_source（源数据）、documents、embeddings、bm25、vector_store、kb_version、logging_setup、config
被谁调用：main（/api/ingest、/api/ingest/status）
"""

from __future__ import annotations

import time

from typing import Any, Callable

from .bm25 import BM25Index, tokenize

from .capability import new_tracker, observe_rows, summarize_table, write_capability

from .config import Settings, get_settings

from .db import DatabaseUnavailable, discover_schema, iter_rows, mysql_connection

from .documents import Document, build_documents

from .embeddings import get_embedder

from .vector_store import get_store, text_hash

from .logging_setup import get_logger
from .kb_version import write_kb_version

logger = get_logger(__name__)

_EMBED_BATCH = 64

ProgressFn = Callable[..., None]


def _columns_for_docs(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 information_schema 的列定义精简成文档构建所需的最小结构。"""
    return [
        {
            "name": col["name"],
            "type": col.get("type", ""),
            "comment": col.get("comment", ""),
        }
        for col in columns
    ]


def generate_documents(
    settings: Settings | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list[Document], list[list[str]], dict[str, Any]]:
    """读取数据源并生成全量文档（只读，不写向量库）。"""

    def report(stage: str, message: str, **extra: Any) -> None:
        if progress:
            progress(stage=stage, message=message, **extra)

    s = settings or get_settings()

    stage_summary: dict[str, Any] = {
        "tables": {},
        "errors": [],
        "warnings": [],
        "documents": 0,
        "cleaning": {},
        # 数据能力口径（行数 / 缺失率 / 时间范围）：边读边攒，建库结束写回 yaml
        "capability": {},
    }
    documents: list[Document] = []
    token_lists: list[list[str]] = []
    # 清洗计数跨表累计：跳过/合并不再静默（见 build_documents 的 stats 参数）。
    cleaning: dict[str, int] = {
        "missing_primary_key": 0,
        "duplicate_pk": 0,
        "empty_text": 0,
        "truncated_fields": 0,
    }

    if s.kb_source_is_postgres:
        from .pg_source import (
            discover_schema as _discover_schema,
            iter_rows as _iter_rows,
            pg_connection as _open_connection,
        )

        def _discover(conn: Any) -> dict[str, dict]:
            return _discover_schema(conn, s.kb_tables, s)

        def _iter(conn: Any, table: str) -> Any:
            return _iter_rows(conn, table, settings=s)

        source_label = "postgres"
        report("postgres", f"连接 PostgreSQL {s.pg_host}:{s.pg_port}/{s.pg_database} …")
    else:
        from .db import (
            discover_schema as _discover_schema,
            iter_rows as _iter_rows,
            mysql_connection as _open_connection,
        )

        def _discover(conn: Any) -> dict[str, dict]:
            return _discover_schema(conn, s.kb_tables)

        def _iter(conn: Any, table: str) -> Any:
            return _iter_rows(conn, table)

        source_label = "mysql"
        report("mysql", f"连接 MySQL {s.mysql_host}:{s.mysql_port}/{s.mysql_database} …")

    with _open_connection(s) as conn:
        schema = _discover(conn)

        for table in s.kb_tables:
            info = schema.get(table, {})
            label = s.label_of(table)
            if not info.get("exists"):
                error = info.get("error") or "表不存在"
                stage_summary["tables"][table] = {
                    "label": label,
                    "exists": False,
                    "rows": 0,
                    "documents": 0,
                    "error": error,
                }
                stage_summary["errors"].append(f"{table}（{label}）：{error}")
                report(source_label, f"跳过 {table}：{error}", table=table)
                continue

            report(source_label, f"读取 {table}（{label}）…", table=table)
            doc_count = 0
            columns = _columns_for_docs(info["columns"])
            # 统计器只「看」不「改」：包在源数据外面，边读边攒行数 / 缺失率 / 日期范围，
            # 文档化逻辑一行不动。建库结束时这些数字会写回 data_capability.yaml。
            tracker = new_tracker(columns)
            for doc in build_documents(
                observe_rows(_iter(conn, table), tracker),
                columns,
                table=table,
                label=label,
                settings=s,
                primary_key_column=info.get("primary_key"),
                stats=cleaning,
            ):
                documents.append(doc)
                token_lists.append(tokenize(doc.text))
                doc_count += 1

            stage_summary["tables"][table] = {
                "label": label,
                "exists": True,
                "rows": info.get("row_count", 0),
                "documents": doc_count,
                "error": None,
            }
            stage_summary["capability"][table] = summarize_table(tracker, doc_count)
            report(
                source_label,
                f"{table}（{label}）：{info.get('row_count', 0)} 行 → {doc_count} 个文档块",
                table=table,
            )

    stage_summary["documents"] = len(documents)
    stage_summary["cleaning"] = dict(cleaning)
    if any(cleaning.values()):
        logger.info(
            "清洗计数：主键缺失 %d 行（已用行号兜底）/ 重复主键 %d 行（保首条跳过）/"
            " 空文本 %d 行（跳过）/ 字段截断 %d 行",
            cleaning["missing_primary_key"],
            cleaning["duplicate_pk"],
            cleaning["empty_text"],
            cleaning["truncated_fields"],
        )
    return documents, token_lists, stage_summary


def _refresh_capability(stage: dict[str, Any], s: Settings, report: ProgressFn | None = None) -> None:
    """建库结束：重算数据能力口径并原子写回 data_capability.yaml。

    口径写不进去只记 warning —— 旧口径照样能用，绝不能把整次建库拖下水。
    """
    try:
        path = write_capability(stage.get("capability") or {}, s)
    except Exception as exc:  # noqa: BLE001 - 写文件失败不影响建库结果
        logger.warning("数据能力口径写回失败（本次建库结果不受影响）：%s", exc)
        return
    if path and report:
        report("capability", f"数据能力口径已更新：{path.name}")


def run_ingest(
    settings: Settings | None = None,
    recreate: bool = True,
    progress: ProgressFn | None = None,
    mode: str = "full",
) -> dict[str, Any]:
    """执行一次完整建库（默认）或增量更新，返回结构化报告。"""
    if mode == "incremental":
        return run_incremental(settings=settings, progress=progress)

    def report(stage: str, message: str, **extra: Any) -> None:
        if progress:
            progress(stage=stage, message=message, **extra)

    s = settings or get_settings()
    started = time.time()
    summary: dict[str, Any] = {
        "tables": {},
        "documents": 0,
        "points": 0,
        "bm25_terms": 0,
        "bm25_docs": 0,
        "errors": [],
        "warnings": [],
        "empty": True,
        "elapsed": 0.0,
    }

    documents: list[Document] = []
    token_lists: list[list[str]] = []

    documents, token_lists, _stage = generate_documents(s, progress)
    summary["tables"] = _stage["tables"]
    summary["errors"] = _stage["errors"]
    summary["warnings"] = _stage["warnings"]
    summary["documents"] = _stage["documents"]
    summary["cleaning"] = _stage["cleaning"]

    report("bm25", "拟合 BM25 词表与文档频率 …")
    bm25 = BM25Index(k1=s.bm25_k1, b=s.bm25_b).fit(token_lists)
    bm25.save(s.bm25_index_path)
    summary["bm25_terms"] = len(bm25.term_to_id)
    summary["bm25_docs"] = bm25.n_docs
    report("bm25", f"BM25 就绪：{bm25.n_docs} 篇文档 / {len(bm25.term_to_id)} 个词元")
    sparse_vectors = [bm25.encode_document(tokens) for tokens in token_lists]

    store = get_store(s)
    report("qdrant", f"准备集合 {s.qdrant_collection}（recreate={recreate}）…")
    store.ensure_collection(recreate=recreate)

    if documents:
        report("embed", f"向量化 {len(documents)} 个文档块（模型 {s.embed_model}）…")
        embedder = get_embedder(s)
        dense_vectors: list[list[float]] = []
        for start in range(0, len(documents), _EMBED_BATCH):
            batch = documents[start : start + _EMBED_BATCH]
            dense_vectors.extend(embedder.embed_documents([d.text for d in batch]))
            report(
                "embed",
                f"向量化进度 {min(start + _EMBED_BATCH, len(documents))}/{len(documents)}",
            )

        report("upsert", "写入向量库 …")
        summary["points"] = store.upsert_documents(documents, dense_vectors, sparse_vectors)
    else:
        summary["warnings"].append(
            "三张表均无数据，已建好空集合与空 BM25 索引；灌入数据后重跑建库即可。"
        )
        report("upsert", "无文档可写入，已完成空库初始化")

    summary["empty"] = summary["documents"] == 0
    summary["elapsed"] = round(time.time() - started, 2)
    report("done", f"建库完成，耗时 {summary['elapsed']} 秒")
    write_kb_version(
        s,
        points=summary.get("points"),
        added=summary.get("added"),
        updated=summary.get("updated"),
        deleted=summary.get("deleted"),
    )
    _refresh_capability(_stage, s, report)
    return summary


def run_incremental(
    settings: Settings | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """增量更新：对比内容指纹，只写新增/变更的文档块，最后删除失效的旧块。"""

    def report(stage: str, message: str, **extra: Any) -> None:
        if progress:
            progress(stage=stage, message=message, **extra)

    s = settings or get_settings()
    started = time.time()
    summary: dict[str, Any] = {
        "mode": "incremental",
        "tables": {},
        "documents": 0,
        "points": 0,
        "bm25_terms": 0,
        "bm25_docs": 0,
        "errors": [],
        "warnings": [],
        "empty": True,
        "elapsed": 0.0,
        "added": 0,
        "updated": 0,
        "deleted": 0,
        "unchanged": 0,
    }

    documents, token_lists, stage = generate_documents(s, progress)
    summary["tables"] = stage["tables"]
    summary["errors"] = stage["errors"]
    summary["warnings"] = stage["warnings"]
    summary["documents"] = stage["documents"]
    summary["cleaning"] = stage["cleaning"]

    report("bm25", "拟合 BM25（增量：保留旧词表编号）…")
    bm25 = BM25Index(k1=s.bm25_k1, b=s.bm25_b).fit(token_lists)
    old_index = BM25Index.load(s.bm25_index_path, s.bm25_k1, s.bm25_b)
    if old_index.term_to_id:
        merged = dict(old_index.term_to_id)
        next_id = max(merged.values()) + 1
        for term in bm25.term_to_id:
            if term not in merged:
                merged[term] = next_id
                next_id += 1
        bm25.term_to_id = merged
    bm25.save(s.bm25_index_path)
    summary["bm25_terms"] = len(bm25.term_to_id)
    summary["bm25_docs"] = bm25.n_docs
    report("bm25", f"BM25 就绪：{bm25.n_docs} 篇文档 / {len(bm25.term_to_id)} 个词元")

    store = get_store(s)
    store.ensure_collection(recreate=False)
    report("qdrant", "读取现有文档指纹（分批 scroll，避免一次拉爆内存）…")
    existing = store.scroll_existing()

    new_ids: set[str] = set()
    added_idx: list[int] = []
    updated_idx: list[int] = []
    unchanged = 0
    for i, doc in enumerate(documents):
        new_ids.add(doc.doc_id)
        old_hash = existing.get(doc.doc_id)
        if old_hash is None:
            added_idx.append(i)
        elif old_hash != text_hash(doc.text):
            updated_idx.append(i)
        else:
            unchanged += 1
    deleted_ids = [doc_id for doc_id in existing if doc_id not in new_ids]

    summary["added"] = len(added_idx)
    summary["updated"] = len(updated_idx)
    summary["deleted"] = len(deleted_ids)
    summary["unchanged"] = unchanged
    report(
        "diff",
        f"差集完成：新增 {summary['added']} / 更新 {summary['updated']} / "
        f"删除 {summary['deleted']} / 未变 {summary['unchanged']}",
    )

    write_idx = added_idx + updated_idx
    if write_idx:
        to_write = [documents[i] for i in write_idx]
        write_tokens = [token_lists[i] for i in write_idx]
        report("embed", f"向量化 {len(to_write)} 个文档块（模型 {s.embed_model}）…")
        embedder = get_embedder(s)
        dense_vectors: list[list[float]] = []
        for start in range(0, len(to_write), _EMBED_BATCH):
            batch = to_write[start : start + _EMBED_BATCH]
            dense_vectors.extend(embedder.embed_documents([d.text for d in batch]))
            report(
                "embed",
                f"向量化进度 {min(start + _EMBED_BATCH, len(to_write))}/{len(to_write)}",
            )
        sparse_vectors = [bm25.encode_document(tokens) for tokens in write_tokens]
        report("upsert", f"写入 {len(to_write)} 个文档块…")
        summary["points"] = store.upsert_documents(to_write, dense_vectors, sparse_vectors)
    else:
        report("upsert", "无新增/变更文档，跳过写入")

    if deleted_ids:
        report("delete", f"删除 {len(deleted_ids)} 个已失效文档块…")
        summary["deleted_points"] = store.delete_documents(deleted_ids)
    else:
        summary["deleted_points"] = 0

    summary["empty"] = summary["documents"] == 0
    summary["elapsed"] = round(time.time() - started, 2)
    report("done", f"增量更新完成，耗时 {summary['elapsed']} 秒")
    write_kb_version(
        s,
        points=summary.get("points"),
        added=summary.get("added"),
        updated=summary.get("updated"),
        deleted=summary.get("deleted"),
    )
    _refresh_capability(stage, s, report)
    return summary


def main() -> None:
    """命令行入口：打印带阶段前缀的进度，最后输出汇总。"""
    import json

    import argparse

    parser = argparse.ArgumentParser(description="建库管道：全量重建或增量更新")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="增量更新：只写入新增/内容变化的文档块，并删除数据源已消失的旧块（默认全量重建）",
    )
    args = parser.parse_args()
    mode = "incremental" if args.incremental else "full"

    def printer(stage: str, message: str, **_: Any) -> None:
        logger.info("[%6s] %s", stage, message)

    try:
        result = run_ingest(progress=printer, mode=mode)
    except DatabaseUnavailable as exc:
        logger.error("[错误] %s", exc)
        raise SystemExit(1)
    except Exception:
        logger.exception("[错误] 建库失败：")
        raise SystemExit(1)

    logger.info("\n===== 建库结果 =====")
    logger.info("%s", json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()