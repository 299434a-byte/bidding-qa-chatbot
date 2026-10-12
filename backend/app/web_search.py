# ============================================================================ #
# 本文件在干什么
# ---------------------------------------------------------------------------- #
# web_search.py 是「联网搜索工具」。本地知识库里装的只有 MySQL 三张表里的存量数据，
# 它回答不了「最新」的东西（比如昨天刚发的招标公告、刚修订的法规）。
# 本工具调用外面的搜索服务 Tavily，去公开互联网上找最新信息，把结果（带相关性评分）
# 交回给大模型参考作答。
#
# 典型触发场景：Agent 先在本地知识库里查，发现「查不到相关记录」，而用户问的又是
# 「最新公告 / 近期政策 / 法规更新」这类有时效性的问题——这时才联网补充。
# 注意：只有前端勾选了「联网搜索」、并且后端配了 Tavily 的 Key，这个工具才会被
# 注册给 Agent（否则保持原有行为，不出现联网事件）。
#
# ---------------------------------------------------------------------------- #
# 概念预习（大白话）
# ---------------------------------------------------------------------------- #
# 【Tavily】= 一个专门给 AI 用的搜索接口。你给它一个关键词，它返回一批网页
#   标题、链接、摘要，以及每条的相关性评分（0~1，越大越相关）。
# 【to_thread】= 把「会卡住的同步操作」挪到后台线程去跑。搜索要发网络请求、可能
#   等好几秒，直接在主线程做会把整个服务卡住，所以用它挪走。
# 【相关性评分】= 这条结果和你的问题有多match。前端会显示成「（相关性 0.93）」，
#   方便用户判断可信度。
# 【lambda】= 一个「没有名字的临时小函数」。这里 results.sort(key=lambda r: ...)
#   就是告诉排序器「按每条结果的 score 排序」。
#
# ---------------------------------------------------------------------------- #
# 硬约束 & 实测结论
# ---------------------------------------------------------------------------- #
# - Tavily 的 API Key 必须来自 .env（TAVILY_API_KEY），代码里绝不出现密钥。
# - 搜索失败（没装 SDK / 没配 Key / 网络异常 / 超时）不能拖垮整轮问答：
#   本工具会把失败记下来，返回一段「请如实告知用户没搜到、不要编造」的提示，
#   让模型基于已有信息继续作答。
# - 实测：在 Anaconda base 环境（Python 3.8）装 tavily-python 会出问题，必须用
#   本项目的 .venv（Python 3.10）环境。本项目用 tavily-python 0.8.4。
#
# ---------------------------------------------------------------------------- #
# 阅读顺序指南
# ---------------------------------------------------------------------------- #
#   1. 顶部常量（TOOL_NAME / _SNIPPET_CHARS ...）—— 有哪些规则；
#   2. _search —— 真正调用 Tavily、整理结果、按评分排序；
#   3. render_results —— 把结果渲染成给模型看的「参考资料」；
#   4. search_web_bidding —— 工具本体，串起配置检查 → 搜索 → 渲染。
# ============================================================================ #
"""联网搜索：用 Tavily 检索最新的招投标公告与法规更新。

用途：本地知识库只有 MySQL 三张表里的存量数据，没有「最新」信息。
当检索一无所获、而问题又涉及近期公告或法规变动时，由 Agent 调用本工具
从公开互联网补充，并在结果里带上 Tavily 的相关性评分（0~1，越大越相关）。

与 pg.py / graph.py 一致：只读，配置一律从 ``.env`` 读取，代码中不出现密钥。
"""

# 让类型标注延迟求值，兼容性更好（详见 pg.py 里的同样说明）。
from __future__ import annotations

# 异步工具箱：用 asyncio.to_thread 把同步的网络搜索挪到后台线程。
import asyncio
# 只用于类型标注的 Any（表示「任意类型」）。
from typing import Any

# RunnableConfig：工具靠它拿到事件层塞进来的状态（settings、用于回填的 state）。
from langchain_core.runnables import RunnableConfig
# @tool：把下面这个函数注册成大模型可调用的工具。
from langchain_core.tools import tool

# Settings/get_settings：读取 .env 配置（含 Tavily 的 Key，绝不在代码里硬编码）。
from .config import Settings, get_settings

# 工具的展示名；函数名即注册给模型的名字，这里保持一致
# 把名字抽成常量，方便事件层/前端引用同一份名字。
TOOL_NAME = "search_web_bidding"

# keyword 无关的兜底条数（正常由 settings.tavily_max_results 控制）
# 意思是：万一状态里没带 max_results，就用这个 5 作为默认条数。
DEFAULT_MAX_RESULTS = 5
# 单条结果截断长度，避免把上下文塞满
# 网页摘要可能很长，全塞给模型会浪费大量 token，所以每条最多取 800 个字符。
_SNIPPET_CHARS = 800


# 自定义异常：表示「联网搜索这一路出的、已整理成人话的错误」。
# 继承 RuntimeError。工具主体会捕获它并转成友好提示。
class WebSearchError(RuntimeError):
    """联网搜索失败（未装 SDK / 未配 Key / 网络异常 / 超时）。"""


# _search：真正去调用 Tavily 搜索，并整理结果。同步阻塞函数。
# 参数：
#   query —— 搜索关键词；
#   settings —— 配置对象（含 key、搜索深度、超时等）；
#   max_results —— 最多要几条结果。
# 返回：结果列表，每项是 {title, url, content, score}，且已按 score 从高到低排序。
# 注意：本函数是同步的，由外层用 asyncio.to_thread 丢到工作线程跑。
def _search(query: str, settings: Settings, max_results: int) -> list[dict[str, Any]]:
    """同步调用 Tavily；由工具侧用 ``asyncio.to_thread`` 丢到工作线程里跑。"""
    # 延迟导入：只有真的要搜索时才导入 SDK。
    # 好处：没装这个库也不影响模块导入；真要用时才报「人话」错误。
    try:
        from tavily import TavilyClient
    # 依赖未装时给出明确原因（并保留原始错误链，方便排查）。
    except ImportError as exc:  # 依赖未装时给出明确原因
        raise WebSearchError("未安装 Tavily SDK（tavily-python），无法联网搜索") from exc

    try:
        # 用配置里的 Key 创建客户端（Key 来自 .env，不硬编码）。
        client = TavilyClient(api_key=settings.tavily_api_key)
        # 发起搜索：
        #   max_results：最多返回几条；
        #   search_depth：搜索深度（basic/advanced，越深越慢但可能更准）；
        #   include_answer=False：不要 Tavily 自己生成的答案，只要原始结果；
        #   timeout：超时时间（秒）。
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth=settings.tavily_search_depth,
            include_answer=False,
            timeout=settings.tavily_timeout,
        )
    # SDK 抛出的异常种类很多，统一收敛成我们的 WebSearchError（带人话原因）。
    except Exception as exc:  # noqa: BLE001 - SDK 异常种类多，统一收敛
        raise WebSearchError(f"联网搜索失败：{exc}") from exc

    # 准备整理后的结果列表。
    results: list[dict[str, Any]] = []
    # 遍历返回的原始结果；response.get("results") or [] 兜底防止空。
    for item in response.get("results") or []:
        # 防御性检查：万一某条不是字典（结构异常）就跳过，避免后面取键报错。
        if not isinstance(item, dict):
            continue
        results.append(
            {
                # 标题：可能缺失，统一去空白。
                "title": (item.get("title") or "").strip(),
                # 链接。
                "url": item.get("url") or "",
                # 摘要：可能缺失，统一去空白。
                "content": (item.get("content") or "").strip(),
                # Tavily 给出的相关性评分（0~1），前端据此展示「相关性」
                # round(..., 4) 保留 4 位小数；float(... or 0.0) 兜底缺失/None。
                "score": round(float(item.get("score") or 0.0), 4),
            }
        )
    # 按相关性评分从高到低排序，让最相关的排在最前面。
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# render_results：把搜索结果渲染成给模型看的「参考资料」文本（带编号与相关性评分）。
# 参数：query 搜索关键词；results 上一步整理好的结果列表。
# 关键设计：没有结果时要「明确告诉模型没搜到、不要编造」（防止模型瞎编标题/链接）。
def render_results(query: str, results: list[dict[str, Any]]) -> str:
    """把搜索结果整理成给模型看的参考资料（带编号与相关性评分）。"""
    # 没有结果：返回「没搜到」的说明 + 「不要编造」的明确指令。
    if not results:
        return (
            f"联网搜索：关键词「{query}」没有返回任何结果。\n\n"
            "请如实告知用户联网也没有查到，不要编造任何公告标题、链接或日期。"
        )

    # 开头：说明关键词、结果条数、以及「已按相关性降序」。
    # 注意列表最后一个空串 ""，作用是让后续内容之间多一个空行，更好读。
    lines = [f"联网搜索：关键词「{query}」返回 {len(results)} 条结果（按相关性降序）：", ""]
    # enumerate(..., start=1) 让编号从 1 开始，与前端展示的 [网页N] 对应。
    for index, item in enumerate(results, start=1):
        # 标题（没有标题就退而显示链接）+ 相关性评分（保留 2 位小数）。
        lines.append(f"[网页{index}] {item['title'] or item['url']}（相关性 {item['score']:.2f}）")
        # 链接单独一行。
        lines.append(f"链接：{item['url']}")
        content = item["content"]
        # 摘要过长就截断，避免塞满上下文。
        if len(content) > _SNIPPET_CHARS:
            content = content[:_SNIPPET_CHARS] + "…"
        lines.append(f"摘要：{content}")
        # 每条结果之间加一个空行，方便阅读。
        lines.append("")

    # 最后给模型一句「使用须知」：联网信息时效好但权威性需自判，引用要标注来源。
    lines.append(
        "以上为互联网公开信息，时效性优于本地知识库，但权威性需自行判断；"
        "引用时请标明 [网页N] 并附上链接，不要把它与知识库来源混为一谈。"
    )
    # 拼成多行文本返回。
    return "\n".join(lines)


# @tool 装饰器：把这个函数「登记」为大模型可调用的工具。
# 模型靠函数名和这段 docstring 判断「什么时候该用它」，所以 docstring 写清楚触发
# 条件（本地查不到 + 涉及最新信息）很重要。
# 注意：@tool 与 async def 之间允许插入注释行，不影响语法。
@tool
# search_web_bidding：工具本体，异步函数。
# 参数：
#   query —— 搜索关键词（模型从用户问句里提炼）；
#   config —— 运行时配置，里面藏着事件层塞的 _web_state。
# 返回：给模型看的参考资料文本（成功是结果/失败是提示）。
async def search_web_bidding(query: str, config: RunnableConfig) -> str:
    """联网搜索最新的招投标公告、政府采购信息与法规更新（Tavily）。

    仅在本地知识库（search_bidding_knowledge）查不到相关记录，
    且问题涉及「最新公告 / 近期政策 / 法规更新 / 时效性信息」时使用。
    返回的结果带有相关性评分，请优先采用评分较高的来源。
    """
    # 从 config 里取事件层放好的状态字典；configurable 可能为 None，用 or {} 兜底。
    state = (config.get("configurable") or {}).get("_web_state")
    # 理论上事件层一定会传；万一没有就兜底造一个「默认配置」的状态，保证工具不崩。
    if state is None:
        # 理论上不会发生（agent_events 一定会传）；兜底成一次默认配置的搜索
        fallback = get_settings()
        state = {"settings": fallback, "max_results": fallback.tavily_max_results}

    # 【工具永不抛异常】与 pg.py / graph.py 同一承诺：搜索失败不拖垮问答。
    # 下面任何异常——配置缺失、WebSearchError 以外的意外错误（SDK 返回结构异常、
    # 结果整理崩了……）——一律转成「可读提示」返回给模型，由模型如实告知用户。
    try:
        # 取出配置对象（state 缺 settings 时转成可读失败，不让 KeyError 冒出工具）。
        s: Settings = state.get("settings")  # type: ignore[assignment]
        if s is None:
            raise WebSearchError("工具未注入配置（state 缺少 settings），无法联网搜索")
        # 配置检查：若没配 Tavily 的 Key，就不该联网。
        # 记下错误、清空结果，并返回「如实告知未配置」的提示（绝不编造）。
        if not s.web_search_configured:
            state["error"] = "未配置 Tavily API Key（TAVILY_API_KEY）"
            state["results"] = []
            return "联网搜索不可用：后端未配置 TAVILY_API_KEY。请如实告知用户，不要编造搜索结果。"

        # 用 asyncio.to_thread 把同步的 _search 挪到后台线程执行，await 拿结果。
        # max_results 优先用状态里的；缺失就用 DEFAULT_MAX_RESULTS 兜底。
        results = await asyncio.to_thread(
            _search, query, s, state.get("max_results") or DEFAULT_MAX_RESULTS
        )
        # 成功：回填状态（供事件层/前端展示），并渲染成参考资料返回。
        state["query"] = query
        state["results"] = results
        state["error"] = None
        return render_results(query, results)
    # 搜索失败：不让整轮问答失败——记下错误、清空结果，返回友好提示。
    # 这样模型仍能基于已有信息（比如本地检索结果）继续作答。
    except WebSearchError as exc:
        # 搜索失败不该让整轮问答失败：记下错误，让模型基于已有信息继续作答
        state["query"] = query
        state["results"] = []
        state["error"] = str(exc)
        return f"{exc}\n\n请如实告知用户联网搜索未成功，不要编造任何搜索结果。"
    except Exception as exc:  # noqa: BLE001 - 工具承诺绝不把异常抛回给 Agent
        # 兜住「WebSearchError 之外的一切意外」：同样转成可读提示，问答继续。
        state["query"] = query
        state["results"] = []
        state["error"] = f"联网搜索失败：{exc}"
        return f"联网搜索失败：{exc}\n\n请如实告知用户联网搜索未成功，不要编造任何搜索结果。"