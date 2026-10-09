# ============================================================================
# 文件：tests/test_rerank.py —— 给「大模型重排结果解析」做体检
#
# 【这个测试文件整体在测什么】
#   被测对象是 backend/app/rerank.py 里的两个「纯文本处理」函数：
#     · extract_json —— 从大模型输出里抠出并解析 JSON，解析不了返回 None；
#     · parse_order  —— 把「一串序号」解析成合法的候选顺序（0 起）。
#   为什么要专门测它们？因为大模型输出极不可靠：可能包着 Markdown 围栏、
#   可能漏排几条、可能写出越界或重复的序号。这两个函数就是「兜底网」，
#   它俩一旦错，重排就会悄悄出错甚至丢资料。测试全程不调用任何大模型。
# ============================================================================

from __future__ import annotations

from app.rerank import extract_json, parse_order


def test_extract_json_parses_json_embedded_in_text():
    """extract_json：模型在 JSON 前后夹了解释文字，也要能正确抠出来。"""
    # 【设计意图】这是模型最常见的「不守规矩」形态：先客气一句，再给 JSON。
    #   源码用正则找出被 {} 或 [] 包住的那一段优先解析，这里验证它管用。
    raw = '好的，我已完成排序：{"order": [2, 1]} —— 已按相关度从高到低排列'
    assert extract_json(raw) == {"order": [2, 1]}


def test_extract_json_returns_none_when_unparseable():
    """extract_json：完全解析不出来时，必须安静地返回 None，而不是抛异常。"""
    # 【设计意图】重排的设计原则是「失败就降级」——解析不了就退回原顺序继续服务。
    #   所以「解析失败」是一种预期内的正常结果，要用 None 表达，绝不能炸。
    assert extract_json("这里根本没有 JSON") is None
    assert extract_json("") is None


def test_parse_order_strips_fence_and_parses_object():
    """parse_order：能剥掉 Markdown 代码围栏，并从 {"order": [...]} 里读出顺序。"""
    # 【设计意图】模型特别爱把结果包在 ```json ... ``` 里，这层「围栏」必须先洗掉，
    #   否则 json 解析一定失败。同时验证「提示词序号从 1 起」→「返回值从 0 起」的换算。
    raw = '```json\n{"order": [2, 1, 3]}\n```'
    # [2,1,3] 表示「第 2 条最相关、然后第 1 条、最后第 3 条」，
    # 换算成 0 起的下标就是 [1, 0, 2]。
    assert parse_order(raw, 3) == [1, 0, 2]


def test_parse_order_appends_missing_candidates_at_end():
    """parse_order：模型漏排的候选，要按原顺序补到末尾（绝不丢候选）。"""
    # 【设计意图】这是本项目明确的设计要求：不管模型怎么漏，
    #   返回结果都必须是 0..n-1 的完整排列。丢掉的那条可能正是最该展示给用户的资料。
    # 模型只排了「第 3、第 1」条，漏了第 2、第 4 条 → 漏掉的按原顺序补到末尾。
    assert parse_order("[3, 1]", 4) == [2, 0, 1, 3]


def test_parse_order_drops_out_of_range_and_duplicate_indices():
    """parse_order：越界序号与重复序号要被忽略，最终仍补成完整排列。"""
    # 【设计意图】模型会「胡说」：给出超出范围的编号（99）、或把同一条说两遍（2, 2）。
    #   越界的直接丢掉（否则会取到列表外、崩溃），重复的只算第一次。
    # 输入解析后：2→下标1（收下）；2→重复（丢）；99→越界（丢）；1→下标0（收下）
    # 于是先得到 [1, 0]，再补上唯一漏掉的 2 → [1, 0, 2]。
    assert parse_order("[2, 2, 99, 1]", 3) == [1, 0, 2]


def test_parse_order_returns_none_for_unusable_input():
    """parse_order：输入完全没法用时返回 None，交给上层降级。"""
    # 【设计意图】和 extract_json 同理：解析不出来是预期内的失败，
    #   返回 None 让上层「悄悄退回 RRF 原顺序」，用户完全无感知。
    assert parse_order("", 3) is None           # 空文本
    assert parse_order("完全无法解析", 3) is None  # 一个字都不是 JSON
    assert parse_order("[1, 2]", 0) is None       # 没有候选（n<=0），无意义