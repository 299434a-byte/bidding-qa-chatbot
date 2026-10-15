"""web_search：联网搜索工具（Tavily 检索招投标政策与公告动态）。

职责：
  - 以 @tool 形式注册给 Agent，补充知识库不具备的时效性信息
  - 搜索失败时返回兜底文案（如实告知未搜到，防止模型编造结果）
  - 未配置 TAVILY_API_KEY 时工具不注册

依赖：config
被谁调用：agent（web_search 开关）、main（/api/agent/chat）
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from .config import Settings, get_settings

TOOL_NAME = "search_web_bidding"

DEFAULT_MAX_RESULTS = 5
_SNIPPET_CHARS = 800


class WebSearchError(RuntimeError):
    """联网搜索失败（未装 SDK / 未配 Key / 网络异常 / 超时）。"""


def _search(query: str, settings: Settings, max_results: int) -> list[dict[str, Any]]:
    """同步调用 Tavily；由工具侧用 ``asyncio.to_thread`` 丢到工作线程里跑。"""
    try:
        from tavily import TavilyClient
    except ImportError as exc:
        raise WebSearchError("未安装 Tavily SDK（tavily-python），无法联网搜索") from exc

    try:
        client = TavilyClient(api_key=settings.tavily_api_key)
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth=settings.tavily_search_depth,
            include_answer=False,
            timeout=settings.tavily_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - SDK 异常种类多，统一收敛
        raise WebSearchError(f"联网搜索失败：{exc}") from exc

    results: list[dict[str, Any]] = []
    for item in response.get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": item.get("url") or "",
                "content": (item.get("content") or "").strip(),
                "score": round(float(item.get("score") or 0.0), 4),
            }
        )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def render_results(query: str, results: list[dict[str, Any]]) -> str:
    """把搜索结果整理成给模型看的参考资料（带编号与相关性评分）。"""
    if not results:
        return (
            f"联网搜索：关键词「{query}」没有返回任何结果。\n\n"
            "建议更换关键词再搜一次（简化查询、换同义词）；再搜仍为空时，"
            "请如实告知用户联网也没有查到，不要编造任何公告标题、链接或日期。"
        )

    lines = [f"联网搜索：关键词「{query}」返回 {len(results)} 条结果（按相关性降序）：", ""]
    for index, item in enumerate(results, start=1):
        lines.append(f"[网页{index}] {item['title'] or item['url']}（相关性 {item['score']:.2f}）")
        lines.append(f"链接：{item['url']}")
        content = item["content"]
        if len(content) > _SNIPPET_CHARS:
            content = content[:_SNIPPET_CHARS] + "…"
        lines.append(f"摘要：{content}")
        lines.append("")

    lines.append(
        "以上为互联网公开信息，时效性优于本地知识库，但权威性需自行判断；"
        "引用时请标明 [网页N] 并附上链接，不要把它与知识库来源混为一谈。"
    )
    return "\n".join(lines)


@tool
async def search_web_bidding(query: str, config: RunnableConfig) -> str:
    """联网搜索最新的招投标公告、政府采购信息与法规更新（Tavily）。"""
    state = (config.get("configurable") or {}).get("_web_state")
    if state is None:
        fallback = get_settings()
        state = {"settings": fallback, "max_results": fallback.tavily_max_results}

    try:
        s: Settings = state.get("settings")  # type: ignore[assignment]
        if s is None:
            raise WebSearchError("工具未注入配置（state 缺少 settings），无法联网搜索")
        if not s.web_search_configured:
            state["error"] = "未配置 Tavily API Key（TAVILY_API_KEY）"
            state["results"] = []
            return "联网搜索不可用：后端未配置 TAVILY_API_KEY。请如实告知用户，不要编造搜索结果。"

        results = await asyncio.to_thread(
            _search, query, s, state.get("max_results") or DEFAULT_MAX_RESULTS
        )
        state["query"] = query
        state["results"] = results
        state["error"] = None
        return render_results(query, results)
    except WebSearchError as exc:
        state["query"] = query
        state["results"] = []
        state["error"] = str(exc)
        return (
            f"{exc}\n\n可尝试更换关键词重试一次；仍失败则如实告知用户联网搜索未成功，"
            "不要编造任何搜索结果。"
        )
    except Exception as exc:  # noqa: BLE001 - 工具承诺绝不把异常抛回给 Agent
        state["query"] = query
        state["results"] = []
        state["error"] = f"联网搜索失败：{exc}"
        return (
            f"联网搜索失败：{exc}\n\n可尝试更换关键词重试一次；仍失败则如实告知用户"
            "联网搜索未成功，不要编造任何搜索结果。"
        )