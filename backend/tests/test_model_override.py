# ============================================================================
# 文件：tests/test_model_override.py —— 请求级 model 字段切换智谱（绝不静默回退）
#
# 【这个测试文件整体在测什么】
#   /api/chat 与 /api/agent/chat 的请求体新增可选 model 字段：
#     · 留空 → 用 .env 默认供应商（行为与改造前一致）；
#     · glm-* → 切到智谱 OpenAI 兼容端点（ZHIPU_BASE_URL + ZHIPUAI_API_KEY）；
#     · 未配 Key / 名字非法 → 进 SSE 前 400 如实报错，绝不静默换回默认模型；
#   且指定模型的请求不吃 / 不写默认模型的缓存（缓存键不含模型，共用会串答案）。
#   全部离线：检索与大模型用假桩，不连网。
# ============================================================================

from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app.config import get_settings, resolve_model_settings
from app.main import app
from app.retriever import RetrievalResult

_FAKE_HIT = {
    "doc_id": "bidding_notices:1:0",
    "table": "bidding_notices",
    "label": "招标公告",
    "primary_key": 1,
    "score": 0.9,
    "text": "本项目预算金额为 1590.0 万元，资金已落实。",
    "fields": {"project_name": "某某工程"},
}


def _with_zhipu_key() -> config_mod.Settings:
    """造一份「智谱 Key 已配置」的 Settings（测试环境默认 ZHIPUAI_API_KEY 为空）。"""
    return dataclasses.replace(get_settings(), zhipuai_api_key="test-zhipu-key")


# ----------------------------------------------------------------------------
# 用例 1：resolve_model_settings 纯函数——四种输入的行为
# ----------------------------------------------------------------------------
def test_resolve_model_settings_behaviors() -> None:
    s = get_settings()

    # 留空 / 空白：原样返回（.env 默认供应商）。
    assert resolve_model_settings(s, None) is s
    assert resolve_model_settings(s, "   ") is s

    # glm-*：切智谱端点与 Key，模型名生效，agent_model 清空以跟随。
    switched = resolve_model_settings(_with_zhipu_key(), "glm-4.7")
    assert switched.llm_base_url == s.zhipu_base_url
    assert switched.llm_api_key == "test-zhipu-key"
    assert switched.llm_model == "glm-4.7"
    assert switched.agent_model == ""
    assert switched.agent_model_or_default == "glm-4.7"

    # glm-* 但没配 Key：如实报错，绝不静默回退。
    with pytest.raises(config_mod.ModelOverrideError, match="ZHIPUAI_API_KEY"):
        resolve_model_settings(s, "glm-4.7")

    # 名字非法：如实报错，不猜测供应商。
    with pytest.raises(config_mod.ModelOverrideError, match="不支持"):
        resolve_model_settings(s, "unknown-model")


# ----------------------------------------------------------------------------
# 用例 2：/api/chat 带 glm-* 但未配 Key → 400（进 SSE 前拒绝）
# ----------------------------------------------------------------------------
def test_chat_model_override_without_key_is_400() -> None:
    resp = TestClient(app).post(
        "/api/chat", json={"question": "市政道路招标有哪些？", "model": "glm-4.7"}
    )
    assert resp.status_code == 400
    assert "ZHIPUAI_API_KEY" in resp.json()["detail"]


# ----------------------------------------------------------------------------
# 用例 3：/api/chat 非法模型名 → 400
# ----------------------------------------------------------------------------
def test_chat_model_override_invalid_name_is_400() -> None:
    resp = TestClient(app).post(
        "/api/chat", json={"question": "市政道路招标有哪些？", "model": "bogus-model"}
    )
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


# ----------------------------------------------------------------------------
# 用例 4：/api/chat 配齐 Key 后切换生效——本轮生成用智谱端点，且不吃默认缓存
# ----------------------------------------------------------------------------
def test_chat_model_override_switches_endpoint_and_skips_cache(monkeypatch) -> None:
    captured: list[config_mod.Settings] = []

    monkeypatch.setattr("app.main.get_settings", _with_zhipu_key)
    monkeypatch.setattr(
        "app.rag.retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[dict(_FAKE_HIT)], dense_count=1, sparse_count=0, bm25_ready=True
        ),
    )

    def fake_stream_chat(messages, settings=None, **kwargs):
        captured.append(settings)
        yield "该项目预算为 1590 万元 [参考资料1]。"

    monkeypatch.setattr("app.rag.stream_chat", fake_stream_chat)

    client = TestClient(app)
    payload = {"question": "市政道路招标有哪些？", "model": "glm-4.7"}
    first = client.post("/api/chat", json=payload)
    assert first.status_code == 200, first.text

    # 行为证据：本轮生效的 Settings 指向智谱端点与 Key。
    assert captured, "生成层未被调用"
    assert captured[0].llm_base_url == get_settings().zhipu_base_url
    assert captured[0].llm_api_key == "test-zhipu-key"
    assert captured[0].llm_model == "glm-4.7"

    # 指定模型不吃默认缓存：同题再问仍进主流程（有 retrieval 事件，无 cache_hit）。
    second = client.post("/api/chat", json=payload)
    events = [
        json.loads(line[len("data: ") :])
        for line in second.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert any(e["type"] == "retrieval" for e in events)
    assert not any(e["type"] == "cache_hit" for e in events)


# ----------------------------------------------------------------------------
# 用例 5：/api/agent/chat 同款 400（两条入口的校验一致）
# ----------------------------------------------------------------------------
def test_agent_chat_model_override_without_key_is_400() -> None:
    resp = TestClient(app).post(
        "/api/agent/chat",
        json={"question": "市政道路招标有哪些？", "mode": "react", "model": "glm-4.7"},
    )
    assert resp.status_code == 400
    assert "ZHIPUAI_API_KEY" in resp.json()["detail"]


# ----------------------------------------------------------------------------
# 用例 6：不带 model 字段的请求完全不受影响（默认链路原样）
# ----------------------------------------------------------------------------
def test_chat_without_model_field_keeps_default(monkeypatch) -> None:
    captured: list[config_mod.Settings] = []
    monkeypatch.setattr(
        "app.rag.retrieve",
        lambda q, settings=None, final_k=None: RetrievalResult(
            question=q, hits=[dict(_FAKE_HIT)], dense_count=1, sparse_count=0, bm25_ready=True
        ),
    )

    def fake_stream_chat(messages, settings=None, **kwargs):
        captured.append(settings)
        yield "该项目预算为 1590 万元 [参考资料1]。"

    monkeypatch.setattr("app.rag.stream_chat", fake_stream_chat)

    resp = TestClient(app).post("/api/chat", json={"question": "不带 model 的老请求？"})
    assert resp.status_code == 200, resp.text
    assert captured and captured[0].llm_base_url == get_settings().llm_base_url
