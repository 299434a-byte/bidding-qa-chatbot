# ============================================================================
# 文件：tests/test_boundary.py —— 数据边界闸门（数据不覆盖直接拒答）
#
# 【这个测试文件整体在测什么】
#   闸门的全部承诺：
#     ① 四类规则各有正例：时间越界 / 字段缺失 / 图谱越界 / 未来 / 地区越界 → blocked；
#     ② 反例（正常问题）一律放行，且返回的六个键恒定存在；
#     ③ 判不准就放行：能力文件缺失 / 语法坏 / 覆盖期没登记 → 全部 blocked=False；
#     ④ **零模型调用证明**：把所有建模型、发请求的入口换成会抛异常的桩，
#       正例照样能立刻拒答（拦截发生在检索与生成之前）；
#     ⑤ 替代问法必须「问得出去」：每条 suggestion 重新过一遍闸门必须不被拦，
#       否则用户点了又弹卡，等于给个死循环；
#     ⑥ 两个问答入口最前面就拦：agent 通路 agent_events 根本不跑，
#       RAG 通路 answer_events 根本不跑，metrics 各记一笔 boundary_blocked；
#     ⑦ ingest 侧的口径重算与原子写回：手写口径原样保留，临时文件不落地。
#   全部离线：不连大模型、不连 PG/图谱、不联网。
# ============================================================================

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import boundary as boundary_mod
from app import capability as capability_mod
from app import metrics as metrics_mod
from app.boundary import check_boundary, clear_capability_cache, load_capability
from app.config import get_settings

# ----------------------------------------------------------------------------
# 测试用口径：与 backend/app/data_capability.yaml 同构（覆盖期 15 个月 / 仅安徽 /
# budget_amount 缺失率 90% / 图谱没有公司与股权关系）
# ----------------------------------------------------------------------------
CAP: dict = {
    "coverage": {
        "date_from": "2023-07-06",
        "date_to": "2024-09-09",
        "months": 15,
        "province_scope": ["安徽"],
    },
    "records": {
        "procurement_notices": {"rows": 933, "chunk_count": 8573},
        "projects": {"rows": 933, "chunk_count": 933},
    },
    "fields": {
        "projects": {
            "project_name": {"missing_rate": 0.0, "note": "主键"},
            "budget_amount": {
                "missing_rate": 0.9,
                "note": "933 行里只有 3 行有值",
                "keywords": ["预算", "预算金额", "金额", "造价"],
            },
        },
    },
    "graph": {
        "labels": ["Project", "Notice"],
        "relations": ["PUBLISHED"],
        "not_covered": ["公司", "供应商", "股权"],
    },
    "generated_at": "2026-10-17T09:36:38Z",
}

# 正例 5 类：越界时间 / 缺失字段 / 图谱越界 / 未来 / 超范围地区（每类至少一条）。
# 「未来」在实现上归入时间规则（rule="time"），所以这里连期望 rule 一起写死。
POSITIVE: tuple[tuple[str, str], ...] = (
    ("近五年有哪些百万级软件项目？", "time"),
    ("这些项目的预算金额分别是多少？", "field"),
    ("这两家公司的股权关系是什么？", "graph"),
    ("2099年有哪些项目？", "time"),  # 未来年份：数据不可能有
    ("2019年的项目有哪些？", "time"),  # 覆盖期之前：剥完时间片段后句子以「的」开头
    ("广东省有哪些招标项目？", "region"),
    ("全国有哪些项目？", "region"),  # 数据只有一省，问全国同样越界
)
POSITIVE_QUESTIONS: tuple[str, ...] = tuple(question for question, _ in POSITIVE)

# 反例 5 类：正常问题一律放行（含「有年份但在覆盖期内」这种最容易误伤的）
NEGATIVE: tuple[str, ...] = (
    "最近有哪些市政道路类的招标公告？",  # 「最近」后面没跟年数，不算时间越界
    "一千万以上的项目有几个？",  # 拿金额做限定，但没点名「预算」这个高缺失字段
    "招标投标法第五条怎么理解？",  # 「第五条」不是年份
    "2024年有哪些项目？",  # 年份落在 2023~2024 覆盖范围内 → 放行
    "中建三局有限公司中标了哪些项目？",  # 提到公司但没问关系 → 知识库还答得了
)

# SSE 里出现的事件必须能被前端按契约忽略 / 识别
ALLOW_KEYS = {"blocked", "rule", "reason", "real_scope", "suggestion", "message"}


def _write_yaml(path: Path, data: dict | None = None) -> Path:
    """把口径写成真实 yaml 文件（用的就是 ingest 写回的同一套渲染函数）。"""
    path.write_text(
        capability_mod.render_capability(CAP if data is None else data),
        encoding="utf-8",
    )
    return path


# ----------------------------------------------------------------------------
# 用例 1：四类正例全部拦下，且结论六键齐全、真实口径来自 yaml
# ----------------------------------------------------------------------------
def test_positive_cases_blocked_with_full_payload() -> None:
    for question, rule in POSITIVE:
        verdict = check_boundary(question, CAP)
        assert verdict["blocked"] is True, f"应拦截：{question}"
        assert set(verdict) == ALLOW_KEYS, "六个键必须恒定，前端拿到什么字段都不缺"
        assert verdict["rule"] == rule, f"{question} → {verdict['rule']} != {rule}"
        assert verdict["reason"], "拒答必须给出原因"
        assert verdict["real_scope"], "拒答必须给出真实口径"
        assert verdict["message"], "拒答必须给出统一模板文案"


# ----------------------------------------------------------------------------
# 用例 2：反例全部放行，返回值仍是一份「键齐全」的空结论
# ----------------------------------------------------------------------------
def test_negative_cases_pass_through() -> None:
    for question in NEGATIVE:
        verdict = check_boundary(question, CAP)
        assert verdict["blocked"] is False, f"不应拦截：{question}"
        assert set(verdict) == ALLOW_KEYS
        assert verdict["suggestion"] == []


# ----------------------------------------------------------------------------
# 用例 3：替代问法必须真的问得出去（点了不会再弹卡 = 没有死循环）
# ----------------------------------------------------------------------------
def test_suggestions_survive_the_gate_themselves() -> None:
    # 「剥掉越界片段」不能留下悬空的「的 / 了 / 在 / 至」当替代问法的开头
    dangling = re.compile(r"[，,]\s*[的了在至]|[，,]\s*$")
    for question in POSITIVE_QUESTIONS:
        suggestion = check_boundary(question, CAP)["suggestion"]
        assert 1 <= len(suggestion) <= 3, f"替代问法要 2~3 条：{question}"
        for alt in suggestion:
            assert alt.endswith("？"), f"替代问法要是完整问句：{alt}"
            assert not dangling.search(alt), f"替代问法留了残渣：{alt}"
            again = check_boundary(alt, CAP)
            assert again["blocked"] is False, f"替代问法又被拦了：{alt} → {again['reason']}"


# ----------------------------------------------------------------------------
# 用例 4：零模型调用证明 —— 所有建模型 / 发请求的入口换成会抛的桩，照样能拒答
# ----------------------------------------------------------------------------
def test_positive_cases_never_touch_llm(monkeypatch) -> None:
    from app import agent as agent_mod
    from app import llm as llm_mod

    def _boom(*_args, **_kwargs):
        raise AssertionError("数据边界闸门不允许触发任何模型调用")

    # Agent 通路与 RAG 通路的模型入口全部钉死
    monkeypatch.setattr(agent_mod, "_make_chat_model", _boom)
    for name in ("_client", "aclient", "stream_chat", "complete_chat"):
        monkeypatch.setattr(llm_mod, name, _boom)

    for question in POSITIVE_QUESTIONS:
        verdict = check_boundary(question, CAP)
        assert verdict["blocked"] is True, f"模型不可用也必须照样拒答：{question}"
        assert verdict["message"]


# ----------------------------------------------------------------------------
# 用例 5：能力文件缺失 / 坏文件 / 覆盖期没登记 —— 一律「不拦截」
# ----------------------------------------------------------------------------
def test_capability_missing_degrades_to_allow(tmp_path) -> None:
    clear_capability_cache()
    # 文件根本不存在（首次 ingest 之前的真实状态）
    assert load_capability(tmp_path / "nope.yaml") == {}
    # 文件是坏 yaml
    bad = tmp_path / "bad.yaml"
    bad.write_text("coverage: [unclosed", encoding="utf-8")
    assert load_capability(bad) == {}
    # 文件不是映射（是个列表）
    listing = tmp_path / "list.yaml"
    listing.write_text("- a\n- b\n", encoding="utf-8")
    assert load_capability(listing) == {}

    # 空口径 = 判不出结论 = 全部放行（绝不因为缺文件拒答）
    for question in (*POSITIVE_QUESTIONS, *NEGATIVE):
        verdict = check_boundary(question, {})
        assert verdict["blocked"] is False, f"缺文件时不得拒答：{question}"
    assert check_boundary(question=None, capability=None)["blocked"] is False  # type: ignore[arg-type]


def test_capability_without_dates_allows_time_rules() -> None:
    """覆盖期没登记（手写文件还没填）时，时间规则算不出结论 → 放行，其它规则照常。"""
    partial = {k: v for k, v in CAP.items() if k != "coverage"}
    partial["coverage"] = {"province_scope": ["安徽"]}
    assert check_boundary("近五年有哪些百万级软件项目？", partial)["blocked"] is False
    # 但图谱规则仍能判（它不依赖覆盖期）
    assert check_boundary("这两家公司的股权关系是什么？", partial)["blocked"] is True


# ----------------------------------------------------------------------------
# 用例 6：load_capability 的 mtime 缓存 —— 改了文件立刻生效，没改就吃缓存
# ----------------------------------------------------------------------------
def test_load_capability_invalidates_on_mtime(tmp_path, monkeypatch) -> None:
    clear_capability_cache()
    path = _write_yaml(tmp_path / "cap.yaml")

    first = load_capability(path)
    assert first["coverage"]["date_from"] == "2023-07-06"
    # 同一份文件再读一次 → 缓存命中（同一对象）
    assert load_capability(path) is first

    # 改内容 + 把 mtime 往后拨（写文件的 mtime 粒度在部分文件系统上是秒级）
    later = dict(CAP)
    later["coverage"] = {**CAP["coverage"], "date_from": "2024-01-01"}
    _write_yaml(path, later)
    import os

    os.utime(path, (path.stat().st_atime + 10, path.stat().st_mtime + 10))

    second = load_capability(path)
    assert second is not first, "文件变了必须失效缓存"
    assert second["coverage"]["date_from"] == "2024-01-01"


# ----------------------------------------------------------------------------
# 用例 7：/api/agent/chat —— 最前面就拦，Agent 一次都不跑，metrics 记一笔
# ----------------------------------------------------------------------------
def _events(resp_text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in resp_text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def _settings_with_capability(path: Path):
    return dataclasses.replace(get_settings(), data_capability_path=path)


def test_agent_chat_blocks_before_running_agent(tmp_path, monkeypatch) -> None:
    from app.main import app

    clear_capability_cache()
    path = _write_yaml(tmp_path / "cap.yaml")
    monkeypatch.setattr("app.main.get_settings", lambda: _settings_with_capability(path))

    async def _agent_boom(*_args, **_kwargs):
        raise AssertionError("被拦时不得进入 Agent（不检索、不调模型）")
        yield  # pragma: no cover

    monkeypatch.setattr("app.main.agent_events", _agent_boom)
    bumped: list[str] = []
    monkeypatch.setattr("app.main.metrics_store.bump", lambda name, **kw: bumped.append(name) or True)

    resp = TestClient(app).post(
        "/api/agent/chat", json={"question": "近五年有哪些百万级软件项目？"}
    )
    assert resp.status_code == 200, resp.text
    events = _events(resp.text)
    assert [e["type"] for e in events] == ["conversation", "boundary_blocked", "done"]
    blocked = events[1]
    assert blocked["reason"]
    assert blocked["real_scope"]
    assert blocked["suggestion"], "边界卡要给得出可点的替代问法"
    assert events[2]["mode"] == "boundary_blocked"
    assert bumped == ["boundary_blocked"]
    assert "data: [DONE]" in resp.text


def test_chat_blocks_before_answer_events(tmp_path, monkeypatch) -> None:
    from app.main import app

    clear_capability_cache()
    path = _write_yaml(tmp_path / "cap.yaml")
    monkeypatch.setattr("app.main.get_settings", lambda: _settings_with_capability(path))

    def _rag_boom(*_args, **_kwargs):
        raise AssertionError("被拦时不得进入 RAG 链路")
        yield  # pragma: no cover

    monkeypatch.setattr("app.main.answer_events", _rag_boom)
    bumped: list[str] = []
    monkeypatch.setattr("app.main.metrics_store.bump", lambda name, **kw: bumped.append(name) or True)

    resp = TestClient(app).post(
        "/api/chat", json={"question": "广东省有哪些招标项目？"}
    )
    assert resp.status_code == 200, resp.text
    events = _events(resp.text)
    assert [e["type"] for e in events] == ["boundary_blocked", "done"]
    assert events[0]["rule"] == "region"
    assert events[1]["mode"] == "boundary_blocked"
    assert bumped == ["boundary_blocked"]


def test_agent_chat_normal_question_not_blocked(tmp_path, monkeypatch) -> None:
    """正常问题：闸门放行，原链路（Agent）照常跑，一个 boundary 事件都不该有。"""
    from app.main import app

    clear_capability_cache()
    path = _write_yaml(tmp_path / "cap.yaml")
    monkeypatch.setattr("app.main.get_settings", lambda: _settings_with_capability(path))

    async def _fake_agent(*_args, **_kwargs):
        yield {"type": "delta", "text": "答"}
        yield {"type": "done", "mode": "agent_rag"}

    monkeypatch.setattr("app.main.agent_events", _fake_agent)
    bumped: list[str] = []
    monkeypatch.setattr("app.main.metrics_store.bump", lambda name, **kw: bumped.append(name) or True)

    resp = TestClient(app).post(
        "/api/agent/chat", json={"question": "最近有哪些市政道路类的招标公告？"}
    )
    assert resp.status_code == 200, resp.text
    types = [e["type"] for e in _events(resp.text)]
    assert "boundary_blocked" not in types
    assert types[0] == "conversation"
    assert types[-1] == "done"
    assert bumped == []


def test_capability_yaml_missing_keeps_endpoints_open(tmp_path, monkeypatch) -> None:
    """沙箱里没有口径文件时，两个入口都必须照常作答（缺文件绝不等于拒答）。"""
    from app.main import app

    clear_capability_cache()
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: _settings_with_capability(tmp_path / "does-not-exist.yaml"),
    )

    async def _fake_agent(*_args, **_kwargs):
        yield {"type": "done", "mode": "agent_rag"}

    monkeypatch.setattr("app.main.agent_events", _fake_agent)

    resp = TestClient(app).post(
        "/api/agent/chat", json={"question": "近五年有哪些百万级软件项目？"}
    )
    assert resp.status_code == 200, resp.text
    assert all(e["type"] != "boundary_blocked" for e in _events(resp.text))


# ----------------------------------------------------------------------------
# 用例 8：metrics 白名单收下 boundary_blocked
# ----------------------------------------------------------------------------
def test_metrics_accepts_boundary_counter(tmp_path) -> None:
    s = dataclasses.replace(get_settings(), metrics_db_path=tmp_path / "boundary.db")
    assert "boundary_blocked" in metrics_mod.COUNTER_NAMES
    assert metrics_mod.bump("boundary_blocked", settings=s) is True
    assert metrics_mod.bump("boundary_not_a_counter", settings=s) is False
    assert metrics_mod.summary(s)["boundary_blocked"] == 1


# ----------------------------------------------------------------------------
# 用例 9：ingest 侧 —— 重算并原子写回，手写口径原样保留
# ----------------------------------------------------------------------------
def _partial() -> dict:
    """generate_documents 攒出来的那份统计（两张表 / budget_amount 九成缺失）。"""
    return {
        "projects": {
            "rows": 933,
            "chunk_count": 933,
            "missing_rate": {"project_name": 0.0, "budget_amount": 0.9},
            "date_from": "2023-07-06",
            "date_to": "2024-09-09",
        },
        "procurement_notices": {
            "rows": 400,
            "chunk_count": 800,
            "missing_rate": {"content": 0.1},
            "date_from": "2023-08-01",
            "date_to": "2024-06-30",
        },
    }


def test_write_capability_recalculates_and_keeps_manual_fields(tmp_path) -> None:
    path = tmp_path / "data_capability.yaml"
    # 先有一份「手写口径」在盘上
    _write_yaml(path)

    out = capability_mod.write_capability(_partial(), path=path)
    assert out == path
    assert not (tmp_path / ".data_capability.yaml.tmp").exists(), "临时文件必须被原子换掉"

    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # 机器重算的部分
    assert data["records"]["projects"] == {"rows": 933, "chunk_count": 933}
    assert data["records"]["procurement_notices"]["rows"] == 400
    assert data["fields"]["projects"]["budget_amount"]["missing_rate"] == 0.9
    assert data["coverage"]["date_from"] == "2023-07-06"  # 各表取交集后的最早
    assert data["coverage"]["date_to"] == "2024-09-09"  # 各表取并集后的最晚
    assert data["coverage"]["months"] == 15
    assert data["generated_at"]
    # 手写口径必须原样保留
    assert data["coverage"]["province_scope"] == ["安徽"]
    assert data["graph"]["not_covered"] == ["公司", "供应商", "股权"]
    assert data["fields"]["projects"]["budget_amount"]["keywords"] == [
        "预算", "预算金额", "金额", "造价",
    ]
    # 头注释重新贴上（yaml 解析器不保留注释）
    assert "单一事实来源" in path.read_text(encoding="utf-8")


def test_write_capability_with_no_stats_is_a_no_op(tmp_path) -> None:
    """一张表都没读到（源表全挂）时宁可不写，也不能把口径写成空表。"""
    path = tmp_path / "data_capability.yaml"
    _write_yaml(path)
    before = path.read_text(encoding="utf-8")
    assert capability_mod.write_capability({}, path=path) is None
    assert path.read_text(encoding="utf-8") == before


def test_observe_rows_counts_missing_and_dates() -> None:
    columns = [
        {"name": "budget_amount", "type": "numeric"},
        {"name": "project_date", "type": "date"},
    ]
    tracker = capability_mod.new_tracker(columns)
    rows = [
        {"budget_amount": None, "project_date": "2024-03-05"},
        {"budget_amount": "", "project_date": None},
        {"budget_amount": 120, "project_date": "2023-12-31 00:00:00"},
    ]
    out = list(capability_mod.observe_rows(iter(rows), tracker))

    assert out == rows, "统计器只看不改，行必须原样放行给 build_documents"
    stats = capability_mod.summarize_table(tracker, chunk_count=7)
    assert stats["rows"] == 3
    assert stats["chunk_count"] == 7
    assert stats["missing_rate"]["budget_amount"] == 0.6667  # 2/3
    assert stats["missing_rate"]["project_date"] == 0.3333  # 1/3
    assert stats["date_from"] == "2023-12-31"
    assert stats["date_to"] == "2024-03-05"


def test_refresh_capability_never_raises(tmp_path, monkeypatch) -> None:
    """口径写不进去只记 warning，绝不能把整次建库拖失败。"""
    from app import ingest as ingest_mod

    def _boom(*_args, **_kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(ingest_mod, "write_capability", _boom)
    # 指到一个「不可能存在」的目录当文件路径：即便没被 monkeypatch 拦住也不该抛
    broken = dataclasses.replace(get_settings(), data_capability_path=tmp_path / "no" / "cap.yaml")
    ingest_mod._refresh_capability(_partial(), broken, None)


# ----------------------------------------------------------------------------
# 用例 10：仓库里那份 data_capability.yaml 本身就是合格的单一事实来源
# ----------------------------------------------------------------------------
def test_committed_capability_yaml_is_well_formed() -> None:
    path = Path(boundary_mod.__file__).resolve().with_name("data_capability.yaml")
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(data) >= {"coverage", "records", "fields", "graph", "generated_at"}
    assert data["coverage"]["date_from"] <= data["coverage"]["date_to"]
    assert data["coverage"]["province_scope"], "地区口径不能为空，否则地区规则永远判不了"
    assert isinstance(data["graph"]["not_covered"], list) and data["graph"]["not_covered"]
    # 真实语料事实的回归锚点（换语料后这两个数字会变，届时同步改口径）
    assert data["records"]["projects"]["rows"] == 933
    assert data["coverage"]["months"] == 15
