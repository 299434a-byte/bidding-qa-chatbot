# ============================================================================
# 文件：app/clarify.py —— 对话澄清（纯规则、纯函数、不调模型）
#
# 【这个文件整体在做什么】
#   问题太短 / 含无上文的指代词 / 统计口径缺限定时，先问清楚再回答，
#   避免拿模糊问题去检索、白烧一次大模型。判定**只看问题文本本身**，
#   判不准一律返回 {"needed": False} 退回原链路（宁可多答一次，不乱打断）。
#
#   返回结构（需要澄清时）：
#       {"needed": True, "question": "澄清问题文案", "options": ["选项1", ...]}
#   options 为 2~4 条**完整可用的问题**——前端把 chip 点成新问题直接发送。
# ============================================================================

from __future__ import annotations

import re
import unicodedata

# ----------------------------------------------------------------------------
# 词表 / 正则（全部为静态规则，无外部依赖、无模型调用）
# ----------------------------------------------------------------------------

# 指代词：依赖上文才能落定指代对象
_DEMONSTRATIVE_RE = re.compile(r"(这个|那个|这些|那些|它|它们|其|该|此|上述|前面说的|刚才说的)")

# 统计口径问法（「哪些」是列举不算；这里只认「问数量」）
_STAT_ASK_RE = re.compile(r"(多少|[几][家个条项座次]|数量|一共|总共|共计)")

# 时间范围限定
_TIME_RE = re.compile(
    r"(20\d{2}|今年|去年|明年|前年|当年|最近|近期|目前|至今|以来"
    r"|近[一二三四五六七八九十两\d]+[年月日周天]|本[月周季度年]|上[月周季度年]|[一二三四五六七八九十\d]+月)"
)

# 主题限定（招投标领域名词：问数量时至少得说清"数什么"）
_TOPIC_RE = re.compile(
    r"(公告|招标|投标|中标|采购|项目|标段|包件|企业|公司|供应商|投标人|采购人|代理机构"
    r"|法规|条例|办法|规定|规范|标准|资质|业绩|人员|合同|预算|金额|工程|货物|服务"
    r"|废标|流标|答疑|澄清文件|候选人|开标|评标)"
)

# 模糊名词：说不清要哪份材料/哪方面（"空调的采购情况"）；有具体问点的短问题（"市政道路有哪些"）不打断
_VAGUE_RE = re.compile(r"(情况|进展|现状|概况|情形|如何|怎么样|怎样|啥|什么|相关|方面|东西|信息)")

# 实体识别（用于"问题虽短但已带项目名/编号/年份/金额/企业名"——够清楚就不打断）
_ENTITY_RES = (
    # 项目编号：2024BFAAZ01297、2024AB12 这类「年份 + 大写字母 + 数字」
    re.compile(r"\d{4}[A-Z]{2,}[A-Z0-9]*"),
    # 标准号 / 文号：GB/T 50500-2013、财办库〔2020〕12 号 这类
    re.compile(r"[A-Z]{1,5}[/\-－][A-Z]?\d{2,}(?:[.\-－]\d+)*"),
    re.compile(r"[〔\[（(]\d{4}[〕\]）)]\s*\d+\s*号"),
    # 年份
    re.compile(r"20\d{2}"),
    # 金额
    re.compile(r"\d+(?:\.\d+)?\s*(?:万|亿)?\s*[元圆]"),
    # 企业名（常见机构后缀；宁可宽一点也不乱打断）
    re.compile(r"(有限公司|股份有限公司|总公司|集团公司|研究院|设计院|事务所|银行|工厂|公司|集团|中心|局)"),
    # 项目/工程/标段名（前面至少两个汉字的专有名；"这个项目"剥掉指代词后不成立）
    re.compile(r"(?<![一-龥])[一-龥]{2,}(?:项目|工程|标段)"),
    # 书名号包起来的文件/法规名
    re.compile(r"《[^》]+》"),
)

# 归一化时剥掉的噪音：空白 + 中英文标点
_NOISE_RE = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()《》\[\]【】…—\-－~～]+")

# 指代词剥除（做实体识别前先剥，避免"这个项目"被当成项目名）
_PRONOUN_STRIPPED_RE = re.compile(r"(这个|那个|这些|那些|它们|它|该|此|其|上述)")


# ----------------------------------------------------------------------------
# 澄清文案与选项模板
# ----------------------------------------------------------------------------


def _topic_of(compact: str) -> str:
    """取主题词：优先"的"字前缀（"空调的采购情况"→"空调"），并剥掉疑问词尾。"""
    head = compact.split("的", 1)[0].strip()
    head = re.sub(r"(多少|[几][家个条项座次]|如何|怎样|怎么样|是什么|有哪些|呢|吗)+$", "", head).strip()
    if head and len(head) <= 8:
        return head
    return ""


def _short_question(compact: str) -> dict[str, object]:
    """问题过短且不含实体：问清想看哪类信息。"""
    topic = _topic_of(compact)
    prefix = f"{topic}相关的" if topic else ""
    return {
        "needed": True,
        "question": "你的问题比较简短，想优先了解哪一类信息？也可以直接补充完整问题（项目名称、编号、时间范围等）。",
        "options": [
            f"{prefix}招标公告有哪些",
            f"{prefix}中标结果有哪些",
            f"{prefix}适用的采购政策有哪些",
        ],
    }


def _demonstrative_question() -> dict[str, object]:
    """指代词没有上文：问清指代对象。"""
    return {
        "needed": True,
        "question": "「这个/那个」指的是哪个项目或哪类信息？请点选一个方向，或补充项目名称、编号。",
        "options": [
            "最近的招标项目有哪些",
            "最近的中标结果有哪些",
            "如何按项目名称或编号查询项目",
        ],
    }


def _stat_question() -> dict[str, object]:
    """统计口径缺限定：问清数什么、数哪段时间。"""
    return {
        "needed": True,
        "question": "想统计什么范围的数据？请点选一个方向，或补充主题与时间范围。",
        "options": [
            "最近一年的招标公告有多少",
            "最近一年的中标结果有多少",
            "在招项目有多少",
        ],
    }


# ----------------------------------------------------------------------------
# 判定入口
# ----------------------------------------------------------------------------


def needs_clarification(question: str, has_history: bool) -> dict[str, object]:
    """判断问题是否需要先澄清再回答。

    规则优先、纯函数、不调模型；**判不准一律返回 {"needed": False}**（退回原链路）。

    触发条件（按优先级，命中任一即澄清）：
    1. 含指代词（这个/那个/它/该…）且没有上文（has_history=False）——
       有上文时指代可结合上文理解，直接答不打断；
    2. 问了统计口径（多少/几X家/数量…）但没有主题词（数什么都说不清）；
    3. 问题很短（≤8 字）且不含任何实体（无项目名/编号/年份/金额/企业名）。

    Args:
        question: 用户问题原文
        has_history: 本轮之前是否有对话上文

    Returns:
        {"needed": True, "question": ..., "options": [...]}（options 2~4 条）
        或 {"needed": False}
    """
    if not isinstance(question, str):
        return {"needed": False}
    # 归一化：全角→半角、去空白与标点，按"有效字数"算长度
    compact = _NOISE_RE.sub("", unicodedata.normalize("NFKC", question)).strip()
    if not compact:
        return {"needed": False}

    # —— 规则 1：指代词 ——
    if _DEMONSTRATIVE_RE.search(compact):
        # 有上文 → 指代可结合上文理解，退回原链路
        return {"needed": False} if has_history else _demonstrative_question()

    # —— 规则 2：统计口径缺主题 ——
    if _STAT_ASK_RE.search(compact) and not _TOPIC_RE.search(compact):
        return _stat_question()

    # —— 规则 3：过短 + 无实体 + 问得模糊（含模糊名词）——
    # 只看"短"会误伤"市政道路有哪些"这类有具体问点的问题，故要求模糊名词才打断；
    # 1~2 字的裸名词（"空调"）必然是半句话，一并收进来。
    if len(compact) <= 8 and (_VAGUE_RE.search(compact) or len(compact) <= 2):
        bare = _PRONOUN_STRIPPED_RE.sub("", compact)
        if not any(rx.search(bare) for rx in _ENTITY_RES):
            return _short_question(compact)

    return {"needed": False}
