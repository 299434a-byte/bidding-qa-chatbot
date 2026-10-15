# ============================================================================
# 文件：tests/test_documents_cleaning.py —— 清洗阶段「不静默丢数据」的计数
#
# 【这个测试文件整体在测什么】
#   build_documents 的 stats 计数：清洗阶段的跳过/合并逐类计数（缺主键兜底、
#   重复主键保首条、空文本跳过、超长字段截断），调用方（ingest）把计数并进
#   建库/增量摘要并打日志。锁两件事：
#     ① 喂一条重复主键 + 一条空文本：对应计数各 +1、不抛异常、其余行照常成文；
#     ② 主键缺失用行号兜底照常成文（计数），超长字段截断（计数 + 截断后缀）。
#   纯函数离线测试：不连数据源、不写向量库。
# ============================================================================

from __future__ import annotations

from types import SimpleNamespace

from app import documents
from app.documents import build_documents

_COLUMNS = [
    {"name": "id", "type": "int", "comment": "编号"},
    {"name": "title", "type": "varchar", "comment": "标题"},
    {"name": "bio", "type": "text", "comment": "简介"},
]


def _settings(**overrides) -> SimpleNamespace:
    """轻量配置替身：只带 build_documents 用到的三个字段。"""
    values = dict(max_doc_chars=100_000, chunk_size=1000, chunk_overlap=100)
    values.update(overrides)
    return SimpleNamespace(**values)


# ----------------------------------------------------------------------------
# 用例 1：重复主键保首条 + 空文本跳过，逐类计数且不抛异常
# ----------------------------------------------------------------------------
def test_cleaning_counts_duplicate_pk_and_empty_text(monkeypatch) -> None:
    """重复主键保首条（后写不再静默覆盖前写）、空文本跳过，计数进 stats。"""
    # 现有渲染逻辑下正文恒非空（第一行是固定抬头），空文本属于防御分支——
    # 用假渲染构造「渲染结果为空」的脏行来触发它。
    real_render = documents.render_row

    def fake_render(row, columns, label, primary_key):
        if row.get("id") == 2:
            return ""
        return real_render(row, columns, label, primary_key)

    monkeypatch.setattr(documents, "render_row", fake_render)

    rows = [
        {"id": 1, "title": "首条内容"},
        {"id": 2, "title": "会渲染成空的行"},
        {"id": 1, "title": "重复主键的内容"},
    ]
    stats: dict[str, int] = {}

    docs = list(
        build_documents(
            iter(rows),
            _COLUMNS,
            table="t_demo",
            label="演示表",
            settings=_settings(),
            primary_key_column="id",
            stats=stats,
        )
    )

    # 逐类计数：重复主键 1 行、空文本 1 行。
    assert stats.get("duplicate_pk") == 1
    assert stats.get("empty_text") == 1
    # 结果文档：只剩 id=1 的首条（重复行不再生成同 doc_id 的文档互相覆盖）。
    assert [d.doc_id for d in docs] == ["t_demo:1:0"]
    assert "首条内容" in docs[0].text
    assert "重复主键的内容" not in docs[0].text


# ----------------------------------------------------------------------------
# 用例 2：主键缺失用行号兜底、超长字段截断，同样计数
# ----------------------------------------------------------------------------
def test_cleaning_counts_missing_pk_and_truncation() -> None:
    """主键缺失不丢行（行号兜底 + 计数）；超长字段截断 + 计数（按行计）。"""
    rows = [
        {"id": None, "title": "缺主键的行"},
        {"id": 5, "title": "正常行", "bio": "长" * 5000},
    ]
    stats: dict[str, int] = {}

    docs = list(
        build_documents(
            iter(rows),
            _COLUMNS,
            table="t_demo",
            label="演示表",
            settings=_settings(),
            primary_key_column="id",
            stats=stats,
        )
    )

    assert stats.get("missing_primary_key") == 1
    assert stats.get("truncated_fields") == 1
    # 缺主键的行用行号（第 1 行 → 1）兜底，照常成文而不是被丢掉。
    assert docs[0].doc_id == "t_demo:1:0"
    # 超长字段：payload 里的值被截断并带提示后缀。
    bio = docs[1].payload["fields"]["bio"]
    assert bio.endswith("…（已截断）")
    assert len(bio) < 5000
