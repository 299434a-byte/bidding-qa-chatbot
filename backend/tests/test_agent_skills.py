# ============================================================================
# 文件：tests/test_agent_skills.py —— load_skill 渐进式披露（业务技能工具）行为
#
# 【这个测试文件整体在测什么】
#   技能文档（docs/skills/*.md）正文不进提示词，只有「文件名 + 一句话说明」的
#   目录常驻，正文靠模型调用 load_skill(name) 按需换取。本文件锁四件事：
#     ① 目录扫描：*.md 的文件名与首段说明被解析成目录条目；
#     ② 加载存在的技能：返回该文件全文（不是摘要）；
#     ③ 加载不存在的技能：返回可读提示（含可用清单），不抛异常；
#     ④ 注册纪律：目录非空才把 load_skill 注册进工具列表，目录为空则不注册
#       （与「未配置的工具不注册」一致）。
#   全部离线：技能目录用 tmp_path 造，不读真实 docs/skills/，不连大模型。
# ============================================================================

from __future__ import annotations

from app import agent as agent_mod
from app.config import get_settings


def _use_tmp_skills(monkeypatch, tmp_path, files: dict[str, str]) -> None:
    """把技能目录指到临时目录并写入给定文件，同时清掉扫描缓存。"""
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(agent_mod, "_SKILLS_DIR", tmp_path)
    # 缓存置 None：让下一次 _scan_skills 重新扫临时目录（monkeypatch 会在测试结束还原原值）。
    monkeypatch.setattr(agent_mod, "_skills_catalog", None)


# ----------------------------------------------------------------------------
# 用例 1：目录扫描——文件名成为技能名，首个正文段落成为一句话说明
# ----------------------------------------------------------------------------
def test_skill_catalog_scans_names_and_summaries(monkeypatch, tmp_path) -> None:
    """目录条目 = 文件名（去扩展名）+ 首个非标题正文段落（截断说明）。"""
    _use_tmp_skills(
        monkeypatch,
        tmp_path,
        {
            "notice-fields.md": "# 招标公告字段解读\n\n公告表各字段的含义与解读口径。\n\n## 字段\n...",
            "answer-selfcheck.md": "# 回答自查清单\n\n生成最终回答前逐条自查。\n",
        },
    )

    catalog = agent_mod._scan_skills()

    names = [entry["name"] for entry in catalog]
    assert names == ["answer-selfcheck", "notice-fields"]  # 按文件名排序
    by_name = {entry["name"]: entry["summary"] for entry in catalog}
    assert by_name["notice-fields"] == "公告表各字段的含义与解读口径。"
    assert by_name["answer-selfcheck"] == "生成最终回答前逐条自查。"


# ----------------------------------------------------------------------------
# 用例 2：加载存在的技能——返回全文
# ----------------------------------------------------------------------------
def test_load_skill_returns_full_document(monkeypatch, tmp_path) -> None:
    """load_skill(name) 返回整份 markdown 正文，而不是只有目录里的那句摘要。"""
    _use_tmp_skills(
        monkeypatch,
        tmp_path,
        {"notice-fields.md": "# 招标公告字段解读\n\n摘要句。\n\nbudget_amount 单位是万元，不要换算成元。"},
    )

    result = agent_mod.load_skill.invoke({"name": "notice-fields"})

    assert "budget_amount 单位是万元" in result
    assert result.startswith("# 招标公告字段解读")


# ----------------------------------------------------------------------------
# 用例 3：加载不存在的技能——可读提示 + 可用清单，不抛异常
# ----------------------------------------------------------------------------
def test_load_skill_unknown_name_returns_readable_hint(monkeypatch, tmp_path) -> None:
    """名字打错时模型应拿到「没有这个技能 + 可用哪些」，而不是异常。"""
    _use_tmp_skills(
        monkeypatch,
        tmp_path,
        {"notice-fields.md": "# 公告字段\n\n摘要。\n", "nl2sql-guide.md": "# NL2SQL\n\n摘要。\n"},
    )

    result = agent_mod.load_skill.invoke({"name": "不存在的技能"})

    assert "没有名为" in result
    assert "notice-fields" in result and "nl2sql-guide" in result


# ----------------------------------------------------------------------------
# 用例 4：注册纪律——目录为空不注册 load_skill，非空才注册
# ----------------------------------------------------------------------------
def test_collect_tools_registers_load_skill_only_when_catalog_nonempty(monkeypatch, tmp_path) -> None:
    """空目录 → 工具列表里没有 load_skill；有技能文件 → 出现。"""
    s = get_settings()

    _use_tmp_skills(monkeypatch, tmp_path, {})
    names_empty = [t.name for t in agent_mod._collect_tools(s, use_web=False)]
    assert agent_mod.SKILL_TOOL_NAME not in names_empty

    _use_tmp_skills(monkeypatch, tmp_path, {"notice-fields.md": "# 公告字段\n\n摘要。\n"})
    names_loaded = [t.name for t in agent_mod._collect_tools(s, use_web=False)]
    assert agent_mod.SKILL_TOOL_NAME in names_loaded


# ----------------------------------------------------------------------------
# 用例 5：提示词常驻目录——系统提示词含技能名与说明（正文不进提示词）
# ----------------------------------------------------------------------------
def test_system_prompt_keeps_catalog_but_not_body(monkeypatch, tmp_path) -> None:
    """渐进式披露：提示词里有目录（名字+说明），没有技能正文的细节内容。"""
    _use_tmp_skills(
        monkeypatch,
        tmp_path,
        {"notice-fields.md": "# 公告字段\n\nbudget_amount 单位是万元。\n\n细节正文特征串XYZ。\n"},
    )

    prompt = agent_mod._agent_system_prompt(get_settings())

    assert "notice-fields" in prompt
    assert "budget_amount 单位是万元。" in prompt  # 一句话说明常驻
    assert "细节正文特征串XYZ" not in prompt  # 正文不进提示词
    assert agent_mod.SKILL_TOOL_NAME in prompt  # 告诉模型工具名
