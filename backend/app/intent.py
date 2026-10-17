# ============================================================================
# 文件：app/intent.py —— 意图路由（纯规则、纯函数、零模型调用）
#
# 【这个文件整体在做什么】
#   把问题归到四类之一，供 Agent 裁剪本轮工具集：
#     regulation —— 法规 / 条例 / 办法 / 条款（→ 知识库 + 联网 + 技能）
#     enterprise —— 公司 / 企业 / 中标 / 供应商（→ PostgreSQL + 图谱 + 联网）
#     project    —— 项目 / 公告 / 编号 / 预算 / 工期（→ 知识库 + 图谱 + PostgreSQL）
#     unknown    —— **判不准，退回全量工具（原链路）**
#
# 三条铁律：
#   ① 纯正则 + 计分，绝不额外调一次大模型（分类本身不许有成本与失败面）；
#   ② 规则优先，判不准一律 "unknown" —— 宁可多给工具，绝不少给；
#   ③ regulation 优先命中即返回（"招标投标法" 同时含「招标」，不能被 project 抢走）。
# ============================================================================

from __future__ import annotations

import re
import unicodedata

# 四类意图（顺序 = 标签展示顺序；unknown 必须是兜底）
INTENTS = ("regulation", "enterprise", "project", "unknown")

# 法规类：命中即定案，不再与其余两类比得分
_REGULATION_RE = re.compile(
    r"(法规|条例|办法|条款|规定|规范|标准|政策|法律|司法解释"
    r"|国家标准|行业标准|招标投标法|政府采购法|实施条例)"
)

# 企业类：主体 / 关系 / 结果
_ENTERPRISE_RE = re.compile(
    r"(公司|企业|中标|供应商|集团|采购人|投标人|代理机构|工商|注册地|资质要求)"
)

# 项目类：标的 / 流程 / 预算
_PROJECT_RE = re.compile(
    r"(项目|公告|标段|编号|预算|工期|招标|采购|中标结果|开标|评标|合同|验收)"
)

# 归一化时剥掉的噪音：空白 + 中英文标点（与 clarify 口径一致）
_NOISE_RE = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()《》\[\]【】…—\-－~～]+")


def _compact(question: str) -> str:
    """归一化：NFKC（全角→半角）+ 去标点空白 + 小写（英文术语口径统一）。"""
    return _NOISE_RE.sub("", unicodedata.normalize("NFKC", question)).lower()


def classify_intent(question: str) -> str:
    """判断问题意图。**纯函数，不调模型**；判不准一律返回 "unknown"。

    Args:
        question: 用户问题原文

    Returns:
        "regulation" | "enterprise" | "project" | "unknown"
    """
    if not isinstance(question, str):
        return "unknown"
    compact = _compact(question)
    if not compact:
        return "unknown"

    # —— 规则 1：法规优先（"招标投标法" 含「招标」，先定案避免被 project 抢走）——
    if _REGULATION_RE.search(compact):
        return "regulation"

    # —— 规则 2/3：企业 vs 项目按命中次数比大小 ——
    enterprise = len(_ENTERPRISE_RE.findall(compact))
    project = len(_PROJECT_RE.findall(compact))

    if enterprise == 0 and project == 0:
        return "unknown"
    if enterprise > project:
        return "enterprise"
    if project > enterprise:
        return "project"

    # —— 打平 = 两类都说得通 = 判不准 → 退回原链路（全量工具）——
    return "unknown"
