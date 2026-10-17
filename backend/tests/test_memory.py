# ============================================================================
# 文件：tests/test_memory.py —— 长期记忆（抽取 / 计数淘汰 / 原子写 / API）
#
# 【这个测试文件整体在测什么】
#   记忆只做增强、绝不拖垮主链路。本文件锁住：
#     ① 抽取正则：项目编号 / 法规标准名 / 企业名各中一条（多抽少抽都不算过）；
#     ② 频次累加：同一实体问 3 次 → count=3；
#     ③ 容量淘汰：实体超上限后按频次保留，高频的活下来、低频的被挤掉；
#     ④ 原子写：落盘是合法 JSON 且不留 .tmp 半截文件；
#     ⑤ 摘要 ≤500 字，无记忆时返回空串（不注入、链路照旧）；
#     ⑥ API：GET/DELETE 需登录；memory_configured=False 时回 503 而不是 500；
#        记问答后 GET 能看到实体，DELETE 后画像清空。
#   全部离线：画像落在 conftest 的临时沙箱，不碰项目 data/memory/。
# ============================================================================

from __future__ import annotations

# json：校验落盘内容是合法 JSON。
import json

# dataclasses.replace：改 Settings（frozen）只能造新实例，不能原地改。
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import memory
from app.config import get_settings


# ----------------------------------------------------------------------------
# 用例 1：三类实体抽取正则各中一条
# ----------------------------------------------------------------------------
def test_extract_entities_each_class():
    """项目编号 / 法规名 / 企业名各抽一条；多抽少抽都不算过。"""
    project = memory.extract_entities("2024BFAAZ01297 这个项目进展如何")
    assert "2024BFAAZ01297" in project, f"项目编号没抽出来：{project}"

    law = memory.extract_entities("《招标投标法》适用于哪些场景")
    assert "招标投标法" in law, f"法规名没抽出来：{law}"

    company = memory.extract_entities("中建三局有限公司的中标情况")
    assert "中建三局有限公司" in company, f"企业名没抽出来：{company}"


# ----------------------------------------------------------------------------
# 用例 2：同一实体反复问 → 频次累加
# ----------------------------------------------------------------------------
def test_record_accumulates_frequency(tmp_path):
    """同一个项目编号问 3 次 → entities 里 count=3。"""
    s = replace(get_settings(), memory_dir=tmp_path)
    question = "2024BFAAZ01297 的招标公告有哪些"
    for _ in range(3):
        assert memory.record_qa("u-freq", question, s) is True

    profile = memory.get_profile("u-freq", s)
    counts = {item["name"]: item["count"] for item in profile["entities"]}
    assert counts.get("2024BFAAZ01297") == 3, f"频次没累加：{counts}"
    # 最近提问也记进去了
    assert len(profile["recent"]) == 3
    assert profile["updated_at"]


# ----------------------------------------------------------------------------
# 用例 3：实体超上限 → 按频次淘汰，高频的活下来
# ----------------------------------------------------------------------------
def test_entities_cap_evicts_low_frequency(tmp_path):
    """先埋一个高频实体，再灌 35 个一次性实体 → 只留 30 个，高频那个必须还在。"""
    s = replace(get_settings(), memory_dir=tmp_path)
    hot_question = "中建三局有限公司的中标情况"
    for _ in range(5):
        memory.record_qa("u-cap", hot_question, s)
    for i in range(35):
        memory.record_qa("u-cap", f"甲公司{i}有限公司的业绩", s)

    profile = memory.get_profile("u-cap", s)
    names = [item["name"] for item in profile["entities"]]
    assert len(names) <= memory.ENTITIES_CAP, f"实体超上限：{len(names)}"
    assert "中建三局有限公司" in names, f"高频实体被淘汰了：{names}"
    hot = next(item for item in profile["entities"] if item["name"] == "中建三局有限公司")
    assert hot["count"] == 5, f"高频计数丢失：{hot}"


# ----------------------------------------------------------------------------
# 用例 4：原子写 —— 落盘是合法 JSON，且不留 .tmp 半截文件
# ----------------------------------------------------------------------------
def test_atomic_write_leaves_valid_json(tmp_path):
    """写完之后：{user}.json 能被 json.load，目录里没有 .json.tmp。"""
    s = replace(get_settings(), memory_dir=tmp_path)
    memory.record_qa("u-atomic", "2024BFAAZ01297 的进展", s)

    path = tmp_path / "u-atomic.json"
    assert path.exists(), "画像文件没落盘"
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)  # 抛错 = 写坏了
    assert "entities" in data and "recent" in data
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"半截文件没清掉：{leftovers}"


# ----------------------------------------------------------------------------
# 用例 5：摘要 ≤500 字；没有记忆时返回空串（不注入、链路照旧）
# ----------------------------------------------------------------------------
def test_summary_bounded_and_empty_when_no_memory(tmp_path):
    s = replace(get_settings(), memory_dir=tmp_path)
    # 没有记忆 → 空串
    assert memory.summary_text("u-none", s) == ""

    for _ in range(3):
        memory.record_qa("u-sum", "2024BFAAZ01297 的项目进展如何", s)
    text = memory.summary_text("u-sum", s)
    assert text, "有记忆却没摘要"
    assert len(text) <= memory.SUMMARY_MAX_CHARS, f"摘要超长：{len(text)}"
    assert "2024BFAAZ01297" in text
    # 不占引用位、不算来源：摘要自己写明了这一点
    assert "不占引用位" in text


# ----------------------------------------------------------------------------
# 用例 6：memory_configured=False → 503 而不是 500
# ----------------------------------------------------------------------------
def test_memory_api_503_when_not_configured(monkeypatch):
    """未启用长期记忆时 GET/DELETE 回 503（明确告知未启用），绝不能 500。"""
    from app.main import app

    base = get_settings()
    off = replace(base, memory_enabled=False)
    assert off.memory_configured is False
    monkeypatch.setattr("app.main.get_settings", lambda: off)

    async def fake_agent_events(*args, **kwargs):  # pragma: no cover - 不该被走到
        yield {"type": "done", "mode": "agent_rag"}

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)
    token = _register(TestClient(app), "mem_off_user")

    client = TestClient(app)
    resp = client.get("/api/memory", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503, resp.text
    resp = client.delete("/api/memory", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503, resp.text


# ----------------------------------------------------------------------------
# 用例 7：API 全流程 —— 需登录；记问答后画像可见；DELETE 后清空
# ----------------------------------------------------------------------------
def test_memory_api_flow(monkeypatch):
    """未登录 401；登录 + 记 3 问后 GET 能看到实体；DELETE 后画像清空。"""
    from app.main import app

    async def fake_agent_events(*args, **kwargs):
        yield {"type": "delta", "text": "回答"}
        yield {"type": "done", "mode": "agent_rag"}

    monkeypatch.setattr("app.main.agent_events", fake_agent_events)
    client = TestClient(app)
    token = _register(client, "mem_flow_user")
    headers = {"Authorization": f"Bearer {token}"}

    # 未登录 → 401
    assert client.get("/api/memory").status_code == 401
    assert client.delete("/api/memory").status_code == 401

    # 直接沉淀 3 条关于同一项目的记忆（模拟问答后落库）
    s = get_settings()
    assert s.memory_configured is True
    for _ in range(3):
        memory.record_qa(_user_id_of(client, token), "2024BFAAZ01297 的项目进展如何", s)

    resp = client.get("/api/memory", headers=headers)
    assert resp.status_code == 200, resp.text
    profile = resp.json()["profile"]
    names = [item["name"] for item in profile["entities"]]
    assert "2024BFAAZ01297" in names, f"画像里没看到关注项目：{names}"
    assert profile["recent"], "最近提问时间线不能为空"

    # 清空后再查：画像空了
    resp = client.delete("/api/memory", headers=headers)
    assert resp.status_code == 200, resp.text
    resp = client.get("/api/memory", headers=headers)
    assert resp.status_code == 200
    profile = resp.json()["profile"]
    assert profile["entities"] == [] and profile["recent"] == []


# ----------------------------------------------------------------------------
# 用例 8：记忆摘要拼进 system prompt 且不占引用位
# ----------------------------------------------------------------------------
def test_build_messages_memory_not_a_source():
    """RAG 的 build_messages：摘要进 system prompt，参考资料编号从 1 起不受影响。"""
    from app.rag import build_messages

    memory_text = memory.summary_text("u-x", replace(get_settings(), memory_dir=_tmp_dir()))
    assert memory_text == ""  # 无记忆 → 空串 → system 不带记忆段

    # 造一段真实摘要再拼一次
    s = replace(get_settings(), memory_dir=_tmp_dir())
    memory.record_qa("u-y", "2024BFAAZ01297 的项目进展", s)
    memory_text = memory.summary_text("u-y", s)
    messages = build_messages(
        "这个项目如何",
        [{
            "text": "原文", "label": "公告", "table": "bidding_notices",
            "primary_key": "1", "matched_by": ["q"], "rrf_score": 1.0,
        }],
        memory_text,
    )
    assert messages[0]["role"] == "system"
    assert memory_text in messages[0]["content"]
    # 记忆不占引用位：参考资料块仍从 [参考资料1] 起
    assert "【参考资料】" in messages[1]["content"]
    assert "[参考资料1]" in messages[1]["content"]
    # 记忆声明了不算来源
    assert "不占引用位" in messages[0]["content"]


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _tmp_dir():
    import tempfile

    from pathlib import Path

    p = Path(tempfile.mkdtemp(prefix="mem_test_"))
    return p


def _register(client: TestClient, username: str) -> str:
    """注册并拿令牌；同名已存在（重跑）时改走登录。"""
    resp = client.post("/api/auth/register", json={"username": username, "password": "test-pass-123"})
    if resp.status_code == 200:
        return resp.json()["token"]
    resp = client.post("/api/auth/login", json={"username": username, "password": "test-pass-123"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _user_id_of(client: TestClient, token: str) -> str:
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["user"]["id"])
