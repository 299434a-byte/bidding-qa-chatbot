# ============================================================================
# 文件：app/memory.py —— 长期记忆（按用户记住关注实体 / 主题 / 最近提问）
#
# 【这个文件整体在做什么】
#   把每个用户问过的问题沉淀成一份小画像（data/memory/{user_id}.json）：
#     · entities —— 项目编号 / 法规标准名 / 企业名，各计次数；
#     · topics   —— jieba 分词高频词（去停用词），各计次数；
#     · recent   —— 最近 20 条提问（带时间戳）。
#   问答前取一份 ≤500 字的摘要注入 system prompt（**不占引用位、不算来源**），
#   让模型知道用户一直关注什么；问答后再把本轮问题记进去。
#   记忆读写失败绝不影响主链路（静默降级 + warning 日志）。
#
# 存储约定：一个用户一个 JSON 文件，写入走「临时文件 + 原子替换」，
#          半截文件绝不会顶掉完整画像。
# ============================================================================

from __future__ import annotations

import json
import os
import re

from pathlib import Path
from typing import Any

from .bm25 import tokenize
from .config import Settings
from .logging_setup import get_logger
from .timeutil import now_iso

logger = get_logger(__name__)

# ----------------------------------------------------------------------------
# 容量上限（超限按频次淘汰：次数少的先丢）
# ----------------------------------------------------------------------------
RECENT_CAP = 20
ENTITIES_CAP = 30
TOPICS_CAP = 30
SUMMARY_MAX_CHARS = 500

# ----------------------------------------------------------------------------
# 抽取规则（纯正则 / 纯分词，不调模型）
# ----------------------------------------------------------------------------

# 项目编号：2024BFAAZ01297 这类「年份 + 大写字母 + 数字」
_PROJECT_NO_RE = re.compile(r"\d{4}[A-Z]{2,}[A-Z0-9]+")

# 法规 / 标准名：含「法 / 条例 / 办法 / 规范」的名词，或 GB 国标号
_LAW_RE = re.compile(r"[一-龥]{2,}(?:条例|办法|规范|规定|通则|细则|法律?)|GB[/T\s]*\d+(?:\.\d+)*")

# 企业名：含「有限公司 / 集团 / 研究院 / 中心」的名词
_COMPANY_RE = re.compile(r"[一-龥A-Za-z0-9]{2,}(?:有限公司|股份有限公司|集团公司|集团|研究院|中心)")

# 书名号包起来的文件名（法规/项目名的常见写法）
_BRACKET_RE = re.compile(r"《([^》]+)》")

# 主题词过滤：长度 ≥2、不是纯数字，且不在停用词表里
_STOPWORDS = frozenset(
    """
    我们 你们 他们 大家 自己 这个 那个 这些 那些 什么 哪些 哪个 怎么 怎样 如何 为什么
    可以 应该 需要 进行 相关 情况 问题 内容 方面 一下 一些 一种 一直 已经 还是 不是
    请问 告诉 帮我 帮忙 查询 查一下 了解 说说 说下 讲讲 介绍 说明 有没有 是不是 对不对
    今天 昨天 明天 最近 目前 现在 以前 之后 之前 时候 时间 年度 季度 月份
    的 了 是 在 有 和 与 或 及 等 就 都 也 很 更 最 把 被 让 给 对 从 向 往
    在于 关于 对于 根据 按照 通过 由于 因为 所以 但是 而且 并且 或者 还是
    个 项 条 家 名 位 次 只 张 台 套 件 种 类 批
    什么 怎么 多少 几个 几家 是否 能否 可否 能不能 会不会
    """
).union({str(i) for i in range(10)})


# ----------------------------------------------------------------------------
# 抽取
# ----------------------------------------------------------------------------


def extract_entities(text: str) -> list[str]:
    """从问题里抽出实体：项目编号 / 法规标准名 / 企业名（书名号优先）。"""
    if not text:
        return []
    found: list[str] = []
    # 书名号里的整名优先（《招标投标法》→ 招标投标法）
    for name in _BRACKET_RE.findall(text):
        name = name.strip()
        if name and name not in found:
            found.append(name)
    stripped = _BRACKET_RE.sub(" ", text)
    for rx in (_PROJECT_NO_RE, _LAW_RE, _COMPANY_RE):
        for match in rx.finditer(stripped):
            token = match.group(0).strip()
            if token and token not in found:
                found.append(token)
    return found


def extract_topics(text: str) -> list[str]:
    """主题词：jieba 分词（复用 bm25.tokenize）+ 去停用词 + 去单字/纯数字。"""
    if not text:
        return []
    topics: list[str] = []
    for token in tokenize(text):
        if len(token) < 2 or token in _STOPWORDS or token.isdigit():
            continue
        if token not in topics:
            topics.append(token)
    return topics


# ----------------------------------------------------------------------------
# 读写（一个用户一个 JSON 文件；写入原子替换）
# ----------------------------------------------------------------------------


def _empty_profile() -> dict[str, Any]:
    return {"entities": {}, "topics": {}, "recent": [], "updated_at": None}


def _file_path(user_id: str, settings: Settings) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", str(user_id or ""))[:64] or "anonymous"
    return settings.memory_dir / f"{safe}.json"


def load_profile(user_id: str, settings: Settings) -> dict[str, Any]:
    """读画像；文件缺失/损坏一律回空画像（不抛错）。"""
    path = _file_path(user_id, settings)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _empty_profile()
    except Exception as exc:  # noqa: BLE001 - 损坏的画像按「没有记忆」处理
        logger.warning("长期记忆读取失败 user=%s err=%s，按空画像继续", user_id, exc)
        return _empty_profile()
    if not isinstance(data, dict):
        return _empty_profile()
    profile = _empty_profile()
    for key in ("entities", "topics"):
        raw = data.get(key)
        if isinstance(raw, dict):
            profile[key] = {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}
    recent = data.get("recent")
    if isinstance(recent, list):
        profile["recent"] = [
            {"ts": str(item.get("ts") or ""), "question": str(item.get("question") or "")}
            for item in recent
            if isinstance(item, dict)
        ][-RECENT_CAP:]
    profile["updated_at"] = data.get("updated_at")
    return profile


def _save_profile(user_id: str, settings: Settings, profile: dict[str, Any]) -> None:
    """临时文件 + os.replace 原子替换：半截文件绝不会顶掉完整画像。"""
    path = _file_path(user_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _merge_counts(target: dict[str, int], additions: list[str], cap: int) -> None:
    """把新词并进计数表；超上限按次数淘汰（次数相同先丢后进的）。"""
    for token in additions:
        target[token] = target.get(token, 0) + 1
    if len(target) > cap:
        kept = sorted(target.items(), key=lambda kv: (-kv[1], kv[0]))[:cap]
        target.clear()
        target.update(dict(kept))


def record_qa(user_id: str, question: str, settings: Settings) -> bool:
    """问答后追加一条记忆。任何失败只打 warning，绝不影响主链路。"""
    if not user_id or not (question or "").strip():
        return False
    try:
        profile = load_profile(user_id, settings)
        _merge_counts(profile["entities"], extract_entities(question), ENTITIES_CAP)
        _merge_counts(profile["topics"], extract_topics(question), TOPICS_CAP)
        profile["recent"] = (
            profile["recent"] + [{"ts": now_iso(), "question": question.strip()}]
        )[-RECENT_CAP:]
        profile["updated_at"] = now_iso()
        _save_profile(user_id, settings, profile)
        return True
    except Exception as exc:  # noqa: BLE001 - 记忆写失败不能带崩问答
        logger.warning("长期记忆写入失败 user=%s err=%s（已跳过，不影响问答）", user_id, exc)
        return False


def clear_profile(user_id: str, settings: Settings) -> None:
    """清空画像（文件不存在就当已清空）。"""
    path = _file_path(user_id, settings)
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("长期记忆清空失败 user=%s err=%s", user_id, exc)


# ----------------------------------------------------------------------------
# 画像与摘要
# ----------------------------------------------------------------------------


def get_profile(user_id: str, settings: Settings) -> dict[str, Any]:
    """记忆画像 API 载荷：实体/主题按次数降序 + 最近提问时间线。"""
    profile = load_profile(user_id, settings)
    return {
        "entities": [
            {"name": name, "count": count}
            for name, count in sorted(profile["entities"].items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "topics": [
            {"name": name, "count": count}
            for name, count in sorted(profile["topics"].items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "recent": list(profile["recent"]),
        "updated_at": profile["updated_at"],
    }


def summary_text(user_id: str, settings: Settings) -> str:
    """≤500 字的记忆摘要，注入 system prompt（**不占引用位、不算来源**）。

    没有记忆返回空串——调用方直接跳过注入，链路与改动前完全一致。
    """
    profile = load_profile(user_id, settings)
    entities = sorted(profile["entities"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    topics = sorted(profile["topics"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    recent = profile["recent"][-3:]
    if not entities and not topics and not recent:
        return ""

    lines = ["【用户记忆】（仅用于理解用户关注点，不是回答依据、不占引用位、不得标注为来源）"]
    if entities:
        lines.append("持续关注的实体：" + "、".join(f"{name}（{count}次）" for name, count in entities))
    if topics:
        lines.append("常问的主题：" + "、".join(f"{name}（{count}次）" for name, count in topics))
    if recent:
        lines.append("最近提问：" + "；".join(item["question"] for item in recent if item["question"]))
    text = "\n".join(lines)
    return text if len(text) <= SUMMARY_MAX_CHARS else text[: SUMMARY_MAX_CHARS - 1] + "…"
