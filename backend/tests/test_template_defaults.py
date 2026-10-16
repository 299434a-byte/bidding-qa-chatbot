# ============================================================================
# 文件：tests/test_template_defaults.py —— 启动告警（模板默认值）行为测试
#
# 【这个测试文件整体在测什么】
#   ① template_default_keys：当前生效值 == .env.example 示例值 → 命中；
#      换成真实值 → 不命中；example 可注入（不依赖仓库里 .env.example 的实际内容）；
#   ② /api/health 新增布尔字段 template_default_risk（只新增、不改已有字段），
#      且与 template_default_keys 的判定同源；
#   ③ 命中只打 WARNING 不拒绝启动 —— 用「启动后接口照常 200」间接断言。
# ============================================================================
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import get_settings, template_default_keys
from app.main import app


# 一组「模板示例值」（测试注入，绝不读真 .env.example，保证用例与仓库文件解耦）
_EXAMPLE = {
    "LLM_API_KEY": "sk-example",
    "PG_PASSWORD": "example-pg-pass",
    "NEO4J_PASSWORD": "example-neo4j-pass",
    "TAVILY_API_KEY": "tvly-example",
    "ZHIPUAI_API_KEY": "zhipu-example",
}


# ----------------------------------------------------------------------------
# 用例 1：仍是模板默认值 → 全部命中
# ----------------------------------------------------------------------------
def test_all_defaults_hit() -> None:
    s = get_settings()
    defaults = replace(
        s,
        llm_api_key=_EXAMPLE["LLM_API_KEY"],
        pg_password=_EXAMPLE["PG_PASSWORD"],
        neo4j_password=_EXAMPLE["NEO4J_PASSWORD"],
        tavily_api_key=_EXAMPLE["TAVILY_API_KEY"],
        zhipuai_api_key=_EXAMPLE["ZHIPUAI_API_KEY"],
    )
    hits = template_default_keys(defaults, example=_EXAMPLE)
    assert sorted(hits) == sorted(_EXAMPLE.keys())


# ----------------------------------------------------------------------------
# 用例 2：换成真实值 → 一个都不命中（拿不准/已更换的不误报）
# ----------------------------------------------------------------------------
def test_replaced_values_not_hit() -> None:
    s = get_settings()
    real = replace(
        s,
        llm_api_key="sk-real-key",
        pg_password="real-pg",
        neo4j_password="real-neo4j",
        tavily_api_key="tvly-real",
        zhipuai_api_key="zhipu-real",
    )
    assert template_default_keys(real, example=_EXAMPLE) == []


# ----------------------------------------------------------------------------
# 用例 3：只换掉其中一个 → 只有没换的命中（逐键独立判定）
# ----------------------------------------------------------------------------
def test_partial_replacement_hits_only_remaining() -> None:
    s = get_settings()
    # 先全部置为模板默认值，再只换掉 LLM_API_KEY
    defaults = replace(
        s,
        llm_api_key=_EXAMPLE["LLM_API_KEY"],
        pg_password=_EXAMPLE["PG_PASSWORD"],
        neo4j_password=_EXAMPLE["NEO4J_PASSWORD"],
        tavily_api_key=_EXAMPLE["TAVILY_API_KEY"],
        zhipuai_api_key=_EXAMPLE["ZHIPUAI_API_KEY"],
    )
    partial = replace(defaults, llm_api_key="sk-real-key")
    hits = template_default_keys(partial, example=_EXAMPLE)
    assert "LLM_API_KEY" not in hits
    assert "PG_PASSWORD" in hits and "TAVILY_API_KEY" in hits


# ----------------------------------------------------------------------------
# 用例 4：example 缺某个键 → 该键跳过不报错（容忍模板缺项）
# ----------------------------------------------------------------------------
def test_missing_key_in_example_skipped() -> None:
    s = get_settings()
    only_llm = {"LLM_API_KEY": "sk-example"}
    assert template_default_keys(replace(s, llm_api_key="sk-example"), example=only_llm) == ["LLM_API_KEY"]
    # 当前值不同 → 不命中；键不存在于 example → 也安全跳过
    assert template_default_keys(replace(s, llm_api_key="sk-real"), example=only_llm) == []


# ----------------------------------------------------------------------------
# 用例 5：/api/health 新增 template_default_risk 字段（布尔，只新增）
# ----------------------------------------------------------------------------
def test_health_reports_template_default_risk() -> None:
    resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    # 新增字段存在且为布尔
    assert isinstance(body.get("template_default_risk"), bool)
    # 与判定函数同源
    assert body["template_default_risk"] == bool(template_default_keys(get_settings()))
    # 已有字段一个不少（护栏：接口只允许新增字段）
    for key in ("status", "llm_configured", "index", "ingesting", "modes"):
        assert key in body, f"已有字段被删：{key}"
