# ============================================================================
# 文件：tests/test_rag_render.py —— 给「参考资料渲染」做体检
#
# 被测对象是 backend/app/rag.py 的 render_reference_blocks 与 build_messages。
# 它们是「[参考资料N]」编号的唯一来源；这里确保编号、分段标注与消息拼装三者
# 相互对应，不会出现「引用了不存在的第 N 条」这类错位。
# ============================================================================

from __future__ import annotations

from app.rag import build_messages, render_reference_blocks


def _src(pk: int, text: str, chunk_index: int = 0, chunk_total: int = 1) -> dict:
    """造一条来源字典（渲染所需字段一个不少）。"""
    return {
        "label": "招标公告",
        "table": "procurement_notices",
        "primary_key": pk,
        "text": text,
        "chunk_index": chunk_index,
        "chunk_total": chunk_total,
    }


def test_render_reference_blocks_empty_returns_blank():
    """无来源时返回空字符串，调用方据此走「（无）」占位。"""
    assert render_reference_blocks([]) == ""


def test_render_reference_blocks_numbers_in_order():
    """编号必须从 1 起、按传入顺序递增，块之间用空行分隔。"""
    out = render_reference_blocks([_src(1, "第一条正文"), _src(2, "第二条正文")])

    assert "[参考资料1]" in out
    assert "[参考资料2]" in out
    # 顺序错位会让用户看到「引用了不存在的第 N 条」，所以顺序也要钉死。
    assert out.index("[参考资料1]") < out.index("[参考资料2]")
    # 空行分隔，保证大模型能把「一块 = 一份资料」切开。
    assert "\n\n" in out


def test_render_reference_blocks_marks_chunk_position():
    """chunk_total>1 时要显示「第 i/n 段」，且 i 从 1 起算。"""
    out = render_reference_blocks([_src(7, "长公告的一段", chunk_index=0, chunk_total=3)])
    # chunk_index=0 是编程下标，展示给用户必须 +1，因此应是「第 1/3 段」。
    assert "第 1/3 段" in out


def test_render_reference_blocks_omits_chunk_mark_when_single():
    """只有一段时不出现「第 x/y 段」，避免给用户制造噪音。"""
    out = render_reference_blocks([_src(7, "短公告", chunk_total=1)])
    assert "段" not in out


def test_build_messages_reference_numbers_match_sources():
    """build_messages 里的 [参考资料N] 必须与来源一一对应、不越界。"""
    sources = [_src(1, "甲"), _src(2, "乙"), _src(3, "丙")]
    messages = build_messages("有哪些项目？", sources)

    assert [m["role"] for m in messages] == ["system", "user"]
    user_content = messages[1]["content"]
    for i in range(1, 4):
        assert f"[参考资料{i}]" in user_content
    # 来源只有 3 条，不该冒出第 4 条引用（防止编号越界）。
    assert "[参考资料4]" not in user_content
    # 问题原文要原样带进 user 消息，否则模型会答非所问。
    assert "有哪些项目？" in user_content