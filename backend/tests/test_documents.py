# ============================================================================
# 文件：tests/test_documents.py —— 给「建库翻译车间」做体检
#
# 【这个测试文件整体在测什么】
#   被测对象是 backend/app/documents.py，它负责把数据库的「表格数据」
#   翻译成检索系统能用的「自然语言文本」，并在过长时切成小块。
#   这里测其中三个纯函数：
#     · render_row       —— 一行数据 → 多行「字段: 值」文本（并跳过空字段、二进制字段）；
#     · split_text       —— 长文本按滑动窗口切块（相邻块有重叠）；
#     · _truncate_fields —— 给超长字段做截断，防止撑爆存储。
#   全程不连数据库、不读 data/ 目录：这些函数只吃传进去的字典和字符串。
# ============================================================================

from __future__ import annotations

from app.documents import _FIELD_TEXT_LIMIT, _truncate_fields, render_row, split_text


def test_render_row_skips_empty_values():
    """render_row：空值、纯空白值的字段，都不该出现在结果文本里。"""
    # 【设计意图】空字段渲染出来只会得到「字段名: 」这样的空壳，是纯噪声：
    #   既占篇幅（浪费后面的模型输入预算），又稀释语义。所以要整条跳过。
    columns = [
        {"name": "id", "type": "int", "comment": "编号"},
        {"name": "title", "type": "varchar", "comment": "标题"},
        {"name": "note", "type": "text", "comment": "备注"},
    ]
    # title 是空字符串；note 是一串空格（会被 strip 成空）。
    row = {"id": 7, "title": "", "note": "   "}

    text = render_row(row, columns, "招标公告", 7)
    lines = text.splitlines()

    # 【第一行是抬头】任何一段文字都自带「我来自哪张表、哪条记录」的溯源信息。
    assert lines[0] == "【招标公告】记录ID：7"
    # 【有值的字段正常渲染】这里用的是列注释当显示名（中文标签）。
    assert "编号: 7" in lines
    # 【空字段被跳过】两个空字段的名字都不该出现在文本里。
    assert all("标题" not in line for line in lines)
    assert all("备注" not in line for line in lines)


def test_render_row_uses_comment_falls_back_to_name_and_skips_binary():
    """render_row：优先用列注释当显示名、没注释退回英文列名；二进制列整列跳过。"""
    # 【设计意图】这正是项目「不写死业务字段名」的落地：
    #   中文名来自数据库运行时读到的列注释，改表结构不用改代码。
    #   同时二进制列（附件、图片）转成文字只会是乱码，必须整列跳过。
    columns = [
        {"name": "amount", "type": "decimal", "comment": "金额"},
        {"name": "raw_pdf", "type": "longblob", "comment": "附件"},
        {"name": "code", "type": "varchar"},  # 故意没有 comment
    ]
    row = {"amount": 1000, "raw_pdf": b"\x00\x01\x02", "code": "ABC"}

    text = render_row(row, columns, "招投标项目", 1)

    # 【有注释 → 用注释当显示名】
    assert "金额: 1000" in text
    # 【没注释 → 退回英文列名】
    assert "code: ABC" in text
    # 【二进制列被整列跳过】连它的中文注释「附件」都不该出现。
    assert "附件" not in text


def test_split_text_returns_single_chunk_for_short_text():
    """split_text：文本不长时，原样当作单块返回（不做任何切分）。"""
    # 【设计意图】短记录本身就是一个完整的语义单元，硬切反而会把上下文割裂。
    #   源码的判断是「文本长度 <= 每块上限」就原样返回。
    assert split_text("短文本", chunk_size=10, overlap=2) == ["短文本"]


def test_split_text_slides_window_with_overlap():
    """split_text：长文本按滑窗切块，相邻块之间保留重叠字符。"""
    # 【设计意图】「重叠」是为了防止关键句正好被切断在两块之间导致两边都读不全。
    #   步长 = 每块长度 - 重叠长度 = 4 - 1 = 3，所以起点依次是 0、3、6。
    text = "abcdefghij"  # 共 10 个字符
    chunks = split_text(text, chunk_size=4, overlap=1)

    # 第 1 块 [0:4]="abcd"、第 2 块 [3:7]="defg"（与上一块重叠 "d"）、第 3 块 [6:10]="ghij"。
    assert chunks == ["abcd", "defg", "ghij"]


def test_split_text_returns_original_for_non_positive_chunk_size():
    """split_text：块大小配成 0 或负数（非法配置）时，安全退回原文，绝不死循环。"""
    # 【设计意图】若不加这层防御，步长会算出 0，按 0 步长推进的循环会「原地打转」永不结束，
    #   整个建库进程就卡死了。所以源码开头就拦住了这种情况。
    assert split_text("abc", chunk_size=0, overlap=1) == ["abc"]


def test_truncate_fields_cuts_overlong_text_only():
    """_truncate_fields：只截断超长的字符串字段，其他字段原样保留。"""
    # 【设计意图】payload 里要存原始字段给前端看，万一某字段几万字，会拖慢建库与传输。
    #   所以写入前先截断，并加个「已截断」标记让人知道内容不完整。
    #   注意：数字、短文本不受影响 —— 尽量少动原始数据。
    long_value = "字" * (_FIELD_TEXT_LIMIT + 10)  # 比上限长 10 个字符

    out = _truncate_fields({"备注": long_value, "金额": 1000, "其他": "短"})

    # 【超长字段被砍到上限，并带上截断标记】
    assert out["备注"].startswith("字" * 10)
    assert out["备注"].endswith("…（已截断）")
    assert len(out["备注"]) == _FIELD_TEXT_LIMIT + len("…（已截断）")
    # 【非字符串、短字符串字段原样保留】
    assert out["金额"] == 1000
    assert out["其他"] == "短"