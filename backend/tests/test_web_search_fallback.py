# ============================================================================
# 文件：tests/test_web_search_fallback.py —— 联网搜索工具「绝不抛异常」兜底测试
#
# 【这个测试文件整体在测什么】
#   web_search 工具对模型承诺「搜索失败不拖垮问答」——任何异常都必须转成
#   一段可读提示返回给模型，由模型如实告知用户，而不是把异常抛回 Agent
#   （抛出去会中断整轮问答）。回归三类场景：
#     ① _search 抛出 WebSearchError 之外的意外异常（SDK 结构异常、整理崩了）；
#     ② 事件层没注入 settings（state 缺键 / 值为 None）；
#     ③ 未配置 TAVILY_API_KEY —— 正常降级提示（不该走到搜索）。
#
# 【为什么用 asyncio.run 而不是 pytest-asyncio】
#   本项目 pytest 没装异步插件（见 pytest.ini），所以同步测试函数里用
#   asyncio.run(...) 手动跑一次协程即可，零额外依赖。
#
# 【怎么调用被 @tool 装饰的函数】
#   search_web_bidding 被 langchain 的 @tool 装饰成 StructuredTool：
#   同步原函数存在 .func（对 async def 是 None），异步原函数存在 .coroutine。
#   直接 asyncio.run(.coroutine(...)) 可以精确控制传入的 config 字典，
#   不用走 ainvoke 的参数解析（那会把 config 混进参数校验里）。
# ============================================================================

from __future__ import annotations

# asyncio：在同步测试里跑异步工具本体。
import asyncio
# pytest 的 monkeypatch：临时替换模块里的函数/配置，测试结束自动还原。
import pytest

# 被测对象与依赖。
from app.config import get_settings
from app.web_search import search_web_bidding


def _run_tool(state: dict | None, query: str = "市政道路招标最新公告") -> str:
    """跑一次搜索工具本体：把 _web_state 塞进 config 同步等结果。

    参数：
      state —— 事件层通常会塞的 _web_state；传 None 表示「config 里压根没这个键」。
    """
    configurable: dict = {} if state is None else {"_web_state": state}
    # .coroutine 拿到被 @tool 装饰前的原 async 函数；它直接读 config["configurable"]。
    return asyncio.run(
        search_web_bidding.coroutine(query, {"configurable": configurable})
    )


def test_unexpected_exception_becomes_readable_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """_search 抛非 WebSearchError 的意外异常 → 工具返回可读提示，绝不抛出。"""
    s = get_settings()
    # 测试环境的 TAVILY_API_KEY 是空的（conftest 清空 → 未配置会提前返回），
    # 这里临时塞个假 Key 让流程走到 _search；用 object.__setattr__ 是因为
    # Settings 是 frozen dataclass，直接赋值会抛 FrozenInstanceError。
    object.__setattr__(s, "tavily_api_key", "fake-key-for-offline-test")
    try:

        def _boom(query: str, settings, max_results: int):
            # 模拟「WebSearchError 之外的一切意外」：SDK 返回结构异常之类。
            raise RuntimeError("boom: 上游返回了看不懂的结构")

        monkeypatch.setattr("app.web_search._search", _boom)
        state = {"settings": s, "max_results": 3}
        # 关键断言：调用本身不抛异常（若抛出，pytest 会直接失败）。
        text = _run_tool(state)
        # 返回的必须是「人话提示」，且带上原因，模型才能如实转述。
        assert isinstance(text, str)
        assert "联网搜索失败" in text
        assert "boom" in text
        # 不要编造的指令必须还在（这是工具提示词的一部分）。
        assert "不要编造" in text
        # 错误同步进 state，供事件层/前端展示。
        assert "boom" in (state.get("error") or "")
        # 失败时不能残留旧结果，避免前端把上一次的搜索结果当成这次的。
        assert state["results"] == []
    finally:
        # 恢复真实配置，避免污染同进程里的其它测试。
        object.__setattr__(s, "tavily_api_key", "")


def test_missing_settings_becomes_readable_hint() -> None:
    """state 缺少 settings（值为 None）→ 可读提示，不让 KeyError 冒出工具。"""
    # 只放一个 settings=None 的残缺 state，模拟事件层注入不完整。
    text = _run_tool({"settings": None})
    assert isinstance(text, str)
    # 提示里要说明「没拿到配置」，且保留「不要编造」的指令。
    assert "无法联网搜索" in text
    assert "不要编造" in text


def test_missing_state_falls_back_to_default_config() -> None:
    """config 里压根没有 _web_state → 兜底成默认配置（未配 Key 时给降级提示）。"""
    # conftest 已把 TAVILY_API_KEY 清空，所以这里必然走「未配置」分支。
    text = _run_tool(None)
    assert isinstance(text, str)
    assert "未配置" in text
    assert "TAVILY_API_KEY" in text
