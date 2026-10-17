# ============================================================================
# 文件：tests/test_intent.py —— 意图路由（classify_intent + 工具裁剪）
#
# 【这个测试文件整体在测什么】
#   意图路由的全部承诺：
#     ① 分类纯规则零模型调用（模型构造器被换成会抛异常的桩，照样算得出）；
#     ② 四类各有一个真实正例；
#     ③ unknown 判不准 → 退回全量工具（原链路）；
#     ④ 裁剪后的工具名集合精确符合口径表；
#     ⑤ 裁到空集（配置不支持该意图）→ 退回全量，绝不饿死模型；
#     ⑥ 系统提示词只介绍「本轮真在工具表里」的工具，不骗模型；
#     ⑦ intent / intent_fallback 进 metrics 白名单，运维能看到路由分布。
#   全部离线：不连大模型、不连 PG/图谱、不联网。
# ============================================================================

from __future__ import annotations

import dataclasses

from app import agent as agent_mod
from app import intent as intent_mod
from app import metrics as metrics_mod
from app.config import Settings, get_settings
from app.intent import classify_intent


def _configured(**extra) -> Settings:
    """造一个「PG + 图谱 + 联网都已配置」的 Settings（沙箱里默认全是空的）。"""
    base = dict(
        pg_host="127.0.0.1",
        pg_user="u",
        pg_password="p",
        pg_database="db",
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_user="n",
        neo4j_password="p",
        tavily_api_key="tvly-test",
    )
    base.update(extra)
    return dataclasses.replace(get_settings(), **base)


# ----------------------------------------------------------------------------
# 用例 1：三类各一个真实正例
# ----------------------------------------------------------------------------
def test_classify_one_positive_per_intent() -> None:
    cases = {
        "regulation": "招标投标法第五条怎么理解？",
        "enterprise": "中建三局有限公司中标了哪些项目？",
        "project": "最近有哪些市政道路类的招标公告？",
    }
    for expected, question in cases.items():  # dict: 意图 → 该意图的正例问题
        assert classify_intent(question) == expected, question


# ----------------------------------------------------------------------------
# 用例 2：判不准一律 unknown —— 闲聊 / 空串 / 两类打平
# ----------------------------------------------------------------------------
def test_classify_unknown_fallback() -> None:
    assert classify_intent("你好，请介绍一下你自己") == "unknown"
    assert classify_intent("") == "unknown"
    assert classify_intent("   ") == "unknown"
    assert classify_intent(None) == "unknown"  # type: ignore[arg-type]
    # 「公司」(enterprise=1) vs 「项目」(project=1) 打平 → 说不准，退回原链路
    assert classify_intent("这个公司负责哪个项目？") == "unknown"
    # 法规优先："招标投标法" 同时含「招标」，不能被 project 抢走
    assert classify_intent("招标投标法第五条说了什么？") == "regulation"


# ----------------------------------------------------------------------------
# 用例 3：零模型调用 —— 模型构造器换会抛的桩，分类与裁剪照常工作
# ----------------------------------------------------------------------------
def test_intent_routing_never_touches_llm(monkeypatch) -> None:
    """分类与工具裁剪不建模型、不发请求：把构造器换成抛异常的桩仍应全绿。"""

    def _boom(*_a, **_k):
        raise AssertionError("意图路由不应构造模型")

    monkeypatch.setattr(agent_mod, "_make_chat_model", _boom)

    assert classify_intent("近五年有哪些百万级软件项目？") == "project"
    names = [t.name for t in agent_mod._collect_tools(_configured(), use_web=True, intent="project")]
    assert agent_mod.TOOL_NAME in names
    # 提示词同理：只拼字符串，不碰模型
    prompt = agent_mod._agent_system_prompt(_configured(), web_search_enabled=True, intent="project")
    assert "工具使用规则" in prompt


# ----------------------------------------------------------------------------
# 用例 4：unknown → 全量工具；三类意图 → 各自的裁剪集合
# ----------------------------------------------------------------------------
def test_collect_tools_trims_by_intent() -> None:
    s = _configured()

    unknown = {t.name for t in agent_mod._collect_tools(s, use_web=True, intent="unknown")}
    assert unknown == {
        agent_mod.TOOL_NAME,
        agent_mod.SKILL_TOOL_NAME,
        agent_mod.PG_TOOL_NAME,
        agent_mod.GRAPH_TOOL_NAME,
        agent_mod.WEB_TOOL_NAME,
    }, "unknown 必须拿到全量工具（原链路）"

    regulation = {t.name for t in agent_mod._collect_tools(s, use_web=True, intent="regulation")}
    assert regulation == {
        agent_mod.TOOL_NAME,
        agent_mod.SKILL_TOOL_NAME,
        agent_mod.WEB_TOOL_NAME,
    }

    enterprise = {t.name for t in agent_mod._collect_tools(s, use_web=True, intent="enterprise")}
    assert enterprise == {
        agent_mod.PG_TOOL_NAME,
        agent_mod.GRAPH_TOOL_NAME,
        agent_mod.WEB_TOOL_NAME,
    }

    project = {t.name for t in agent_mod._collect_tools(s, use_web=True, intent="project")}
    assert project == {
        agent_mod.TOOL_NAME,
        agent_mod.PG_TOOL_NAME,
        agent_mod.GRAPH_TOOL_NAME,
    }


# ----------------------------------------------------------------------------
# 用例 5：裁到空集 → 退回全量（配置不支持该意图时不能把模型饿死）
# ----------------------------------------------------------------------------
def test_collect_tools_falls_back_when_trim_is_empty() -> None:
    # 沙箱默认：PG / 图谱 / 联网全没配置 → enterprise 裁完一个不剩
    s = get_settings()
    assert not (s.pg_configured or s.graph_configured or s.web_search_configured)

    trimmed = [t.name for t in agent_mod._collect_tools(s, use_web=False, intent="enterprise")]
    full = [t.name for t in agent_mod._collect_tools(s, use_web=False, intent="unknown")]
    assert trimmed == full, "裁到空集必须退回全量工具"
    assert trimmed, "退回后的工具表不能为空"


# ----------------------------------------------------------------------------
# 用例 6：提示词只介绍本轮真在表里的工具（不骗模型去调不存在的工具）
# ----------------------------------------------------------------------------
def test_prompt_only_advertises_tools_that_are_given() -> None:
    s = _configured()

    full_prompt = agent_mod._agent_system_prompt(s, web_search_enabled=True, intent="unknown")
    assert agent_mod.PG_TOOL_NAME in full_prompt
    assert agent_mod.GRAPH_TOOL_NAME in full_prompt
    assert agent_mod.WEB_TOOL_NAME in full_prompt

    # enterprise 只给 PG + 图谱 + 联网 → 提示词里不该出现知识库工具与技能工具
    trimmed_prompt = agent_mod._agent_system_prompt(s, web_search_enabled=True, intent="enterprise")
    assert agent_mod.PG_TOOL_NAME in trimmed_prompt
    assert agent_mod.GRAPH_TOOL_NAME in trimmed_prompt
    assert agent_mod.TOOL_NAME not in trimmed_prompt
    assert agent_mod.SKILL_TOOL_NAME not in trimmed_prompt
    # 裁剪后的兜底口径必须写进提示词
    assert "明确说明你还需要哪类数据" in trimmed_prompt
    # 联网规则不能拿「没给的工具」当前置条件
    assert "本轮没有本地知识库工具" in trimmed_prompt
    assert "若 search_bidding_knowledge 返回" not in trimmed_prompt
    # 没给知识库 → 不教它写 [参考资料N]
    assert "引用知识库来源时沿用" not in trimmed_prompt


# ----------------------------------------------------------------------------
# 用例 7：intent 进 _build_agent 缓存键 —— 同配置不同意图不复用同一张图
# ----------------------------------------------------------------------------
def test_build_agent_cache_key_carries_intent(monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "_agent_cache", {})
    s = _configured()

    agent_mod._build_agent(s, web_search_enabled=False, intent="enterprise")
    agent_mod._build_agent(s, web_search_enabled=False, intent="project")
    agent_mod._build_agent(s, web_search_enabled=False, intent="enterprise")

    assert len(agent_mod._agent_cache) == 2, "不同 intent 各存一张图，重复 intent 走缓存"


# ----------------------------------------------------------------------------
# 用例 8：metrics 白名单收下 intent 计数，summary 能读出来
# ----------------------------------------------------------------------------
def test_metrics_accept_intent_counters(tmp_path) -> None:
    """intent / intent_fallback 必须进白名单：bump 认得、summary 读得出、白名单外仍拒绝。"""
    s = dataclasses.replace(get_settings(), metrics_db_path=tmp_path / "intent.db")
    for name in (
        "intent_regulation",
        "intent_enterprise",
        "intent_project",
        "intent_unknown",
        "intent_fallback",
    ):
        assert name in metrics_mod.COUNTER_NAMES, name
        assert metrics_mod.bump(name, settings=s) is True, name

    assert metrics_mod.bump("intent_does_not_exist", settings=s) is False

    out = metrics_mod.summary(s)
    assert out["intent_fallback"] == 1
    assert out["intent_project"] == 1
    # 分类数 = 一次分类 + 一次 fallback（同一个 unknown 意图计了两笔）
    assert out["intent_unknown"] == 1


# ----------------------------------------------------------------------------
# 用例 9：10 条真实问题的分类结果（交付表格的回归锚点）
# ----------------------------------------------------------------------------
def test_classify_ten_real_questions() -> None:
    table = {
        "招标投标法第五条怎么理解？": "regulation",
        "政府采购法对供应商资格有什么规定？": "regulation",
        "预算超过一千万的项目有几个？": "project",
        "最近有哪些市政道路类的招标公告？": "project",
        "2024BFAAZ01297项目的预算是多少？": "project",
        "中建三局有限公司中标了哪些项目？": "enterprise",
        "哪家供应商中标金额最高？": "enterprise",
        "你好，介绍一下你自己": "unknown",
        "今天天气怎么样？": "unknown",
        "这个公司负责哪个项目？": "unknown",  # enterprise/project 打平
    }
    for question, expected in table.items():
        assert classify_intent(question) == expected, f"{question!r} → {classify_intent(question)!r}"


def test_intent_module_exposes_all_intents() -> None:
    assert set(intent_mod.INTENTS) == {"regulation", "enterprise", "project", "unknown"}
