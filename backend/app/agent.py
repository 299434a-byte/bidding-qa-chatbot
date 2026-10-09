# ============================================================================
# 文件：agent.py —— 让大模型「自己决定要不要查资料」的调度中心（ReAct 模式）
#
# 【这个文件在整个项目里是干什么的，和 rag.py 有什么不同】
#   rag.py 是一条「固定管道」：不管用户问什么，都先检索 → 再作答。
#   这样处理闲聊（比如「你好」「你能干什么」）就会很傻：明明不需要查资料，也硬要检索一遍。
#   本文件换了个思路：把「检索知识库」包装成一个「工具」，交给大模型自己判断——
#     · 问题跟招投标业务有关（项目名称、金额、资质、条款……）→ 模型自己去调工具查；
#     · 问题只是闲聊 → 模型不调工具，直接回答。
#   这种「边想边决定要不要动手（Reasoning + Acting）」的套路，业界叫 ReAct。
#
# 【本项目一共支持三种模式（与本文件末尾的 agent_events 参数 mode 对应）】
#   · react —— 就是本文件的主要逻辑：单轮 ReAct，工具由模型自主调用（最轻量）；
#   · plan  —— Plan-and-Solve：先让模型规划出几个子任务，再逐个执行，最后综合成答案；
#   · deep  —— 深度思考：在 plan 的基础上再加「自我评价（Reflection）」，必要时修订答案。
#   后两种模式的实际代码在 deep_agent.py 里，但**驱动它们的核心逻辑就写在本文件**
#   （函数 _stream_react_turn），两个文件共用同一份驱动，避免两处逻辑各写一遍会逐渐"漂移"。
#
# 【先懂的六个概念（大白话）】
#   1) 工具（tool）：给大模型配的「外挂功能」。模型不能自己上网、查数据库，但它可以
#      "说我要调用某个工具、参数是某某"，由程序替它执行、再把结果喂回给它。
#   2) 事件流（event stream）：本文件对外吐出的不是"一段文字"，而是一连串小字典，
#      每个字典有个 type 字段标明"这是什么"。前端像一个分拣员，按 type 分发给不同模块。
#   3) 流式（stream）：模型一个字一个字往外冒，用户能看到打字机效果。
#   4) 异步（async / await）：等待网络时先去干别的，不白白卡住。本文件全程异步。
#   5) 线程（thread）与阻塞：有些活儿（比如向量化、查数据库）是"同步"的，跑起来会独占
#      一段时间。为了不拖住整个服务，本文件把它们扔到"旁支线程"里去跑——用
#      asyncio.to_thread 实现。线程可以理解成"同一台电脑上的另一条流水线"。
#   6) 递归上限（recursion_limit）：模型可能"调工具→看结果→再调工具→……"无限循环下去。
#      设一个上限，超过就报错停止，防止程序卡死。本项目设的是 6（见 config 的 agent_max_steps）。
#
# 【谁调用它（上游）】
#   backend/app/main.py 的 /api/agent/chat 端点 —— 前端发起提问时驱动 agent_events。
#
# 【它给谁用（下游）】
#   · 前端（Next.js）—— 消费事件流，展示「已调用知识库检索 / 知识图谱 / PostgreSQL / 联网搜索」
#     徽标、可折叠的 SQL 与 Cypher、参考来源卡片、以及流式正文；
#   · deep_agent.py —— 复用本文件的 _stream_react_turn、_new_states 等内部设施。
#
# 【本文件最需要记住的两条实测结论（docstring 里也写了，注释在此再强调）】
#   ① Agent 必须用 langgraph 的 create_react_agent，不能用 langchain 的 create_agent：
#      实测后者只吐一整段完整消息、没有逐字流式效果。
#   ② 工具调用的参数（模型到底传了什么给工具）必须从 updates 流里取：messages 流里那条
#      AI 消息虽然也带 tool_calls，但参数是空的，还会多出一条"名字为空"的假调用。
#      所以本文件同时订阅 ["messages", "updates"] 两条流，各取所需。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring）】
# 【为什么放在这里】注释不算语句，所以放在这一大段注释之后，它依然是本模块的
#   第一条语句、依然是官方自我介绍，内容与原文一字不差。
"""Agent 编排：把 RAG 检索注册成「工具」，由大模型自主决定是否调用。

与 ``rag.answer_events`` 的区别：``rag`` 是固定管道（无论问什么都先检索再回答），
这里则先让 Agent 判断意图 —— 与招投标业务相关才去检索知识库，闲聊直接回答。

事件协议在 ``rag`` 的 5 种之上新增 4 种：

    {"type": "tool_call",   "name": "...", "query": "...", "arguments": {...}}
    {"type": "tool_result", "name": "...", "count": N}
    {"type": "sql_query",   "sql": "...", "columns": [...], "row_count": N,
                            "rows": [...], "error": null}
    {"type": "graph_query", "cypher": "...", "row_count": N, "rows": [...], "error": null}
    {"type": "web_search",  "query": "...", "results": [{"title", "url", "content", "score"}],
                            "error": null}

``sql_query`` 只在 Agent 调用 PostgreSQL 工具后发出（``tool_result`` 之前），
供前端展示生成的 SQL 与结果表格；``error`` 非空表示这次查询失败。
``graph_query`` 同理，在 Agent 调用 Neo4j 图谱工具后发出，供前端展示生成的 Cypher 与结果。
``web_search`` 在 Agent 调用 Tavily 联网搜索后发出，结果自带相关性评分（``score``）。

两点实测结论（本地验证过，勿凭记忆改写）：

1. 用 ``langgraph.prebuilt.create_react_agent``，而不是 ``langchain.agents.create_agent``。
   实测在 langchain 1.4.2 / langgraph 1.2.12 下：前者能逐 token 流式输出
   （``stream_mode="messages"`` 收到 77 个分片），后者只吐一个整段 AIMessage，
   会丢掉打字机效果。
2. 工具调用的参数必须从 ``updates`` 流里取。``messages`` 流里的 AIMessage 虽然也带
   ``tool_calls``，但 ``args`` 是空的，而且还会多出一条 name 为空的假调用。
   因此这里同时订阅 ``["messages", "updates"]``：``messages`` 取正文增量与工具结果，
   ``updates`` 取完整的工具调用参数。
"""

# 【这行在干什么】打开「类型注解延迟求值」开关。
# 【为什么这么写】本文件用了 dict[str, Any]、AsyncIterator[dict[str, Any]] 这类新式标注，
#   老版本 Python（3.8/3.9）不认；加上这行后标注先被当成字符串存着，需要时才解析。
# 【注意事项】必须放在文件最顶部（所有执行代码之前），这是 Python 的硬性要求。
from __future__ import annotations

# 【这行在干什么】引入 asyncio 模块（Python 的异步工具箱，标准库）。
# 【为什么需要】本文件有一处必须用 asyncio.to_thread 把「同步阻塞的检索」丢到旁支线程去跑，
#   否则会卡住整个服务（详见 _run_retrieval 与 search_bidding_knowledge 的注释）。
import asyncio

# 【这行在干什么】引入两种类型标注工具。
# 【参数逐个解释】
#   · Any —— 事件字典里的值形态不定（可能是字符串、数字、列表、字典），说不准就用 Any 兜住；
#   · AsyncIterator —— 「异步迭代器」：表示"能一段段吐出东西、且取的时候要 await"的对象。
#     本文件两个核心生成器（_stream_react_turn、agent_events）都返回它。
from typing import Any, AsyncIterator

# 【这行在干什么】引入 LangChain 的三种「对话消息」类型。
# 【参数逐个解释】
#   · AIMessage —— AI 说的话（含它决定要调用哪些工具的信息）；
#   · HumanMessage —— 用户说的话（提问、历史对话里的用户发言）；
#   · ToolMessage —— 工具执行完返回的内容（比如检索到的参考资料）。
# 【为什么要有这些类型而不是纯字符串（关键）】大模型接口需要区分"谁说的这句话"。
#   历史上 messages 就是一个带 role 标签的列表（system/user/assistant），
#   LangChain 把这些角色做成了不同的类，类型更明确、也便于框架内部识别处理。
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# 【这行在干什么】引入 RunnableConfig 类型（LangChain 里的"运行配置"的类型标注）。
# 【为什么需要】本文件的检索工具函数签名里要写 config: RunnableConfig，用它来标注类型。
# 【它是干什么用的（重要）】这个 config 是 LangChain 在调用工具时自动传进来的一个"随身包"，
#   里面可以挂自定义数据（本项目就把 _rag_state 等状态字典挂在里面），
#   工具函数从包里取回自己的那份状态——这是本项目「工具把执行细节回传给编排层」的关键手段。
from langchain_core.runnables import RunnableConfig

# 【这行在干什么】引入 tool 装饰器（LangChain 提供）。
# 【为什么需要】在一个普通 async 函数头上写 @tool，LangChain 就会把它包装成一个"工具"：
#   自动生成工具的描述（取自函数的 docstring）、参数说明，并使其能被大模型识别和调用。
#   【重点】函数的 docstring 会被原样送给模型当"工具说明书"，所以**工具函数的 docstring
#   必须写清楚"这个工具干什么、什么时候该用"**——它是模型决定是否调用本工具的唯一依据。
from langchain_core.tools import tool

# 【这行在干什么】引入 create_react_agent（来自 langgraph 这个库）。
# 【为什么需要】它负责把「模型 + 工具 + 系统提示词」拼装成一个会自动"想→调工具→再想"的智能体。
# 【为什么不自己写循环】ReAct 的循环逻辑（多轮工具调用、消息拼接、终止判断）很琐碎，
#   这个现成函数已经处理好了，没必要重造轮子。
from langgraph.prebuilt import create_react_agent

# 【这行在干什么】引入 ChatOpenAI（LangChain 对 OpenAI 兼容接口的封装）。
# 【为什么用它而不是直接用 llm.py 里的客户端】LangChain 的 Agent 框架要求传入它自己
#   认识的对象（一个"聊天模型"），而 llm.py 里那个是"裸"的 openai 客户端，
#   框架不认识。这里用 ChatOpenAI 适配一层即可，连接的仍是 .env 里配的同一个服务。
# 【为什么把这一行放在其他 langchain 导入之后】只是书写顺序，不影响功能。
from langchain_openai import ChatOpenAI

# 【这行在干什么】引入本项目的配置类与取配置函数。
# 【为什么这么写】Agent 用哪个模型、能调几步、带几轮历史……全部来自 .env，不写死。
from .config import Settings, get_settings

# 【这行在干什么】引入知识图谱工具及其工具名常量。
# 【参数逐个解释】
#   · GRAPH_TOOL_NAME —— 图谱工具的名字（字符串），本文件用它来"认出"某个工具消息是不是图谱工具返回的；
#   · query_bidding_graph —— 真正干活的图谱查询工具函数。
from .graph import GRAPH_TOOL_NAME, query_bidding_graph

# 【这行在干什么】引入大模型相关的两样东西。
# 【参数逐个解释】
#   · LLMError —— 大模型调用失败的异常（兜底补答时要用它捕获错误）；
#   · stream_chat —— 同步流式调用（在"已经查到来源、但 Agent 出错"时用来补一段回答）。
from .llm import LLMError, stream_chat

# 【这行在干什么】引入 PostgreSQL 工具及其工具名常量（同图谱工具，成对引入）。
from .pg import PG_TOOL_NAME, query_bidding_postgres

# 【这行在干什么】从 rag.py 引入 6 个东西，让两条链路"口径一致"。
# 【参数逐个解释】
#   · NO_DATA_MESSAGE —— 「知识库是空的」的统一提示文案（保证两种模式说法一致）；
#   · SYSTEM_PROMPT —— 作答纪律（只依据资料、标 [参考资料N]、不得编造……）；
#   · answer_events —— 纯 RAG 的事件流（没配大模型时本文件会直接委托给它）；
#   · build_messages —— 把来源拼成给模型的消息（兜底补答时用）；
#   · render_reference_blocks —— 把来源渲染成「[参考资料N]」文本块（检索工具返回给模型的内容）；
#   · retrieval_only_answer —— 仅检索模式的整段回答（兜底时用）。
# 【为什么不用外面再实现一份（关键设计）】引用编号规则、提示词措辞、"没数据"的说法
#   都必须两处完全一致，否则用户会在两种模式间看到对不上的引用编号。共用是最省心的做法。
from .rag import (
    NO_DATA_MESSAGE,
    SYSTEM_PROMPT,
    answer_events,
    build_messages,
    render_reference_blocks,
    retrieval_only_answer,
)

# 【这行在干什么】引入重排函数（检索完再精筛一遍，同 rag.py 的做法）。
from .rerank import rerank_hits

# 【这行在干什么】引入混合检索主入口（向量语义 + BM25 字面，RRF 融合）。
from .retriever import retrieve

# 【这行在干什么】引入联网搜索工具的名字常量，起个别名 WEB_TOOL_NAME 以免和本文件的 TOOL_NAME 混淆。
# 【写法解释】`from .web_search import TOOL_NAME as WEB_TOOL_NAME` 里的 as 表示"改个名字引入"。
#   因为本文件自己也定义了一个 TOOL_NAME（指知识库检索工具），直接叫同一个名字就撞车了。
from .web_search import TOOL_NAME as WEB_TOOL_NAME

# 【这行在干什么】引入联网搜索工具的执行函数。
from .web_search import search_web_bidding

# 工具的展示名；函数名即注册给模型的名字，这里保持一致
# 【这行在干什么】定义知识库检索工具的名字常量 TOOL_NAME。
# 【为什么单独定义成常量】本文件好几处要判断"这条工具消息是不是知识库检索返回的"，
#   用一个常量比到处写字符串更安全（改名字只改一处，不会漏）。
TOOL_NAME = "search_bidding_knowledge"

# 三种推理模式（与前端三选一控件、AgentChatRequest.mode 一一对应）
#   react —— 现状：单轮 ReAct，工具由模型自主调用
#   plan  —— Plan-and-Solve：先规划子任务，再逐步执行，最后综合成答案
#   deep  —— 深度思考：在 plan 之上追加 Reflection 自评与（必要时）修订
# 【这行在干什么】定义三种模式的名字元组 MODES。
# 【为什么定义在这里、还要注释一遍】agent_events 收到模式参数后要校验"这个模式名认不认识"，
#   不认识就退回 react。把合法值集中成一个常量，前后端对接时有据可查。
MODES = ("react", "plan", "deep")


# 【这行在干什么】定义函数 _agent_system_prompt：生成 Agent 的"工作守则"。
# 【参数逐个解释】
#   · s —— 配置对象（要读 pg_configured / graph_configured 决定写哪几条规则）；
#   · web_search_enabled=False —— 本轮是否启用了联网搜索。
# 【返回值】一整段系统提示词文本。
# 【为什么规则要"按配置动态拼"（关键设计）】如果 PostgreSQL 没配置，那个工具根本不会注册给模型；
#   这时提示词里若还写着"遇到统计问题请调用 PostgreSQL 工具"，模型就会去调一个不存在的工具，
#   结果是白转一圈、浪费一次调用。所以规则必须和"实际注册了哪些工具"严格对应。
#   同理联网搜索没开时，也不该出现相关规则。
def _agent_system_prompt(s: Settings, web_search_enabled: bool = False) -> str:
    """在 rag 的作答纪律之上，补充「什么时候该调哪个工具」。

    未配置的工具既不会注册给模型，也不会在这里出现对应规则，
    避免模型去调用一个并不存在的工具。
    """
    # 【这行在干什么】建一个列表，逐条收集"用了哪些工具、什么时候用"的规则行。
    # 【为什么用列表再 join，而不是直接拼字符串】见前面文件的解释：字符串不可变，
    #   反复 += 会不断创建新对象；先收集再一次性 join 更省内存、可读性也更好。
    lines = [
        # 【这行在干什么】加一行小标题。
        "工具使用规则：",
        # 【这行在干什么】第 1 条规则：什么情况下该用知识库检索工具。
        # 【这条为什么永远无条件加】因为知识库检索工具是所有情况下都会注册的（见 _build_agent），
        #   所以它的规则也永远有效。
        "1. 问题涉及招标公告原文、招投标项目内容、公司信息、条款措辞、资质描述等「语义/原文」类内容时，"
        "先调用 search_bidding_knowledge 检索知识库，再依据它返回的参考资料作答。",
    ]
    # 【这行在干什么】准备一个"下一条规则的编号"计数器，从 2 开始。
    # 【为什么要用变量计数（关键）】后面有几条规则是"条件性添加"的（PG 有才加、图谱有才加），
    #   如果写死编号，一旦某条没加，编号就会断号（1、3、4……）看起来很怪。
    #   用变量递增，编号永远是连续的。
    next_no = 2
    # 【这行在干什么】只有当 PostgreSQL 已配置时才添加对应规则。
    if s.pg_configured:
        # 【这行在干什么】追加 PG 规则：什么类型的问法该用 PG 工具（统计、计数、求和、排序、精确筛选）。
        # 【写法拆解】f"..." 里的 {next_no} 会被替换成当前编号；后面还给了几个具体例子，
        #   因为"给模型举例"比"讲抽象概念"更能让它做对判断。
        lines.append(
            f"{next_no}. 问题涉及统计、计数、求和/平均、排序 Top-N、精确条件筛选、多表关联等「结构化/精确」类内容时"
            "（例如「预算超过一千万的项目有几个」「按地区统计公告数量」「某公司中标金额合计」），"
            "调用 query_bidding_postgres 精确查询。"
        )
        # 【这行在干什么】编号加一，为下一条做准备。
        next_no += 1
    # 【这行在干什么】只有当 Neo4j 知识图谱已配置时才添加对应规则（理由同 PG）。
    if s.graph_configured:
        # 【这行在干什么】追加图谱规则：涉及"实体之间的关系、多跳、路径、关系计数"时用图谱工具。
        # 【为什么这类问题不能用普通检索解决】普通检索是"按相似度找文本片段"，它回答不了
        #   "A 公司和 B 公司共同参与过哪些项目"这种需要顺着关系一步步走（多跳）的问题。
        #   图谱天然就是存关系的数据结构，所以这类问题交给它。
        lines.append(
            f"{next_no}. 问题涉及实体之间的关联、多跳、路径、关系计数等「关系/图谱」类内容时"
            "（例如「有哪些关于空调的采购」「某公司中标了哪些项目」「某项目有哪些参与单位」"
            "「两家公司共同参与过哪些项目」），调用 query_bidding_graph 沿关系查询。"
        )
        # 【这行在干什么】编号加一。
        next_no += 1
    # 【这行在干什么】判断"是不是除了检索之外还注册了别的工具"。
    # 【条件拆解】next_no > 3 的含义：编号从 2 起步，每加一个工具规则就 +1。
    #   如果到最后 next_no 还是 3（说明只加过 1 条工具规则，即只有检索），
    #   那就没必要讲"多工具协作"；只有 >3（即至少还有第 2 个工具）时这条规则才有意义。
    if next_no > 3:
        # 只有在「除了检索之外还有第 2 个工具」时，这条多工具协作规则才有意义
        # 【这行在干什么】追加"需要多角度时可以同时调用多个工具"的规则。
        lines.append(f"{next_no}. 需要多种视角时，把相应工具都调用，再综合作答。")
        # 【这行在干什么】编号加一。
        next_no += 1
    # 【这行在干什么】继续追加两条"通用规则"，它们和有没有额外工具无关。
    # 【写法解释】lines += [...] 等价于对列表里的每个元素逐个 append，是批量追加的简写。
    lines += [
        # 【这行在干什么】强调"闲聊不要调工具"，直接答，并顺带说明自己能查什么。
        # 【为什么这条很重要】这正是 Agent 模式区别于 rag.py 固定管道的核心价值：
        #   闲聊不该浪费一次检索（既慢又可能返回一堆无关资料干扰回答）。
        f"{next_no}. 与招投标业务无关的闲聊、自我介绍、能力询问等，不要调用工具，直接回答，"
        "并简要说明你能查什么。",
        # 【这行在干什么】允许模型"改写检索词"。
        # 【为什么特意写这条】用户口语化的问法（"那个空调的标"）往往和库里原文用词不同，
        #   允许模型自己补全专业词（如补上"采购项目""设备类"），检索命中率会明显提高。
        f"{next_no + 1}. 调用工具时可以按需改写检索词（例如补全项目类型、资质关键词），以便命中更准。",
        # 【这行在干什么】统一输出格式：引用沿用 [参考资料N]（与 rag.py 一致），表格类结论用 Markdown 表格。
        f"{next_no + 2}. 引用知识库来源时沿用 [参考资料N] 标注；表格类结论用 Markdown 表格呈现。",
    ]
    # 【这行在干什么】如果本轮开了联网搜索，再追加一节联网搜索规则。
    if web_search_enabled:
        # 联网搜索是「兜底」工具：本地知识库查不到、或问题本身要最新信息时才用
        # 【这行在干什么】追加联网搜索的三条规则。
        # 【写法解释】先加一个空字符串 ""，效果是在提示词里留一个空行，把两节内容隔开。
        lines += [
            "",
            "联网搜索规则（search_web_bidding）：",
            # 【这行在干什么】第 1 条：什么时候必须联网。
            # 【为什么这条如此关键（业务设计）】本地知识库是"存量数据"（灌库那一刻的快照），
            #   问"最近/今年/最新的公告"时，库里根本没有这些新内容。如果不明确要求模型联网，
            #   它就会直接回"没有数据"——用户会觉得这个系统很废。所以这里写死：
            #   遇到"知识库没找到"或"要最新信息"必须联网，不许直接说没有。
            "1. 本地知识库是存量数据。若 search_bidding_knowledge 返回「知识库中没有找到相关记录」，"
            "或问题明确要求「最新 / 近期 / 今年 / 最近发布」的招标公告、政府采购信息、法规政策更新，"
            "必须再调用 search_web_bidding 联网搜索，不要直接回「没有数据」。",
            # 【这行在干什么】第 2 条：联网结果的标注方式要和知识库区分开。
            # 【为什么必须区分（重要）】网上抓来的内容权威性不如自家数据库，两种来源的可信度
            #   完全不同。用 [网页N] 和 [参考资料N] 两套编号分开标注，用户一眼就能看出
            #   "这条结论是从自家库来的，还是从网上搜的"。
            "2. 联网结果比知识库新，但权威性需自行判断；引用时用 [网页N] 标注并给出链接，"
            "与知识库的 [参考资料N] 分开标注，不要把两者混为一谈。",
            # 【这行在干什么】第 3 条：都没查到就老实说，绝不编造。
            # 【为什么这条要反复强调】"编造公告标题/链接/日期"是本业务最严重的错误——
            #   用户可能据此去投标、去报价，危害远超"回答不出来"。
            "3. 联网搜索也失败或无结果时，如实说明「本地知识库与联网都没有查到」，绝不编造公告标题、链接或日期。",
        ]
    # 【这行在干什么】拼成最终提示词：rag 的作答纪律 + 两个换行 + 上面收集的工具规则。
    # 【为什么用两个换行分隔】让模型清楚看到"这是两节内容"（一节讲怎么答、一节讲用哪些工具），
    #   结构清晰时模型更不容易串味。
    return SYSTEM_PROMPT + "\n\n" + "\n".join(lines)

# 工具在知识库中一无所获时返回给模型的内容
# 【这行在干什么】定义常量 _NO_HIT_NOTICE：检索工具"什么都没查到"时，返回给模型的那段话。
# 【为什么不是简单返回"没有结果"（关键设计）】这段话是给**模型**读的，不是给用户看的。
#   所以要写成"指令"的形式：明确告诉模型"你去向用户说明下面这段话，并且绝对不要编造"。
#   若只返回一句干巴巴的"无结果"，模型很可能自作主张"那我凭常识答一下吧"——这正是我们要避免的。
_NO_HIT_NOTICE = (
    "检索结果：知识库中没有找到与该查询相关的记录。\n\n"
    "请直接向用户说明下方内容，不要编造任何项目名称、金额、日期或资质：\n"
    f"{NO_DATA_MESSAGE}"
)

# Agent 实例缓存。Settings 是 frozen dataclass 但含 dict 字段（不可哈希），
# 因此按「影响 Agent 构造的几个值」做键，而不是直接拿 Settings 当键。
# 【这行在干什么】建一个字典当"Agent 对象缓存"。
# 【为什么需要缓存（性能设计）】每来一次提问就重建一遍 Agent 对象是有代价的（要组装模型、
#   工具、提示词）。而绝大多数提问用的配置完全一样，所以缓存起来复用。
# 【为什么键写得这么讲究（Python 知识点）】字典的键必须是"可哈希"的（意思是可以被稳定地
#   算出一个数字指纹来比较）。Settings 里含有字典类型的字段，而字典是可变的、不可哈希，
#   所以不能直接拿 Settings 当键。这里的做法是：只挑"影响 Agent 构造的那几个值"
#   （模型地址、密钥、模型名、温度、上限、超时、以及"哪些工具注册了"），
#   把它们凑成一个元组当键。只要这些值不变，就认为可以复用同一个 Agent。
_agent_cache: dict[tuple[Any, ...], Any] = {}


# 【这行在干什么】定义函数 _build_agent：构造（并尽量复用缓存里的）ReAct Agent。
# 【参数逐个解释】
#   · s —— 配置对象；
#   · web_search_enabled=False —— 本轮是否允许联网搜索。
# 【返回值】一个装配好的 Agent 对象。
def _build_agent(s: Settings, web_search_enabled: bool = False) -> Any:
    """构造（并缓存）ReAct Agent。

    工具集：知识库检索（始终注册）+ PostgreSQL 查询 / Neo4j 图谱查询 / 联网搜索
    （各自配置齐备、且该轮开关打开时才注册）。
    ``pg_configured`` / ``graph_configured`` / ``web_search_enabled`` 必须参与缓存键，
    否则工具集变化后会串用旧实例。
    """
    # 【这行在干什么】算出"本轮到底要不要真的注册联网工具"：既要开关打开、又要后端配好了密钥。
    # 【写法解释】bool(... and ...) 是把结果强制转成真假值。两个条件都满足才为真。
    # 【为什么两个条件都要（重要）】用户可能在前端勾了"联网搜索"，但后端根本没配 TAVILY_API_KEY。
    #   这时若还是注册工具，模型调用它必然失败、白转一圈。所以这里必须双重确认。
    use_web = bool(web_search_enabled and s.web_search_configured)
    # 【这行在干什么】拼出缓存键（一个元组），内容就是"所有会影响 Agent 构造的配置值"。
    # 【为什么这几个值都要放进来（每条都必要）】
    #   · llm_base_url / llm_api_key —— 换了服务地址或密钥，Agent 必须重建；
    #   · agent_model_or_default —— 换了模型必须重建；
    #   · llm_temperature / agent_max_tokens / llm_timeout —— 这几个都直接传给模型构造；
    #   · pg_configured / graph_configured —— 决定工具集里有几个工具；
    #   · use_web —— 同上。
    #   【漏掉任何一个的后果】配置改了但缓存键没变，就会一直复用旧 Agent——
    #   表现为"配置明明改了却不生效"，而且重启进程才会好，极难排查。
    key = (
        s.llm_base_url,
        s.llm_api_key,
        s.agent_model_or_default,
        s.llm_temperature,
        s.agent_max_tokens,
        s.llm_timeout,
        s.pg_configured,
        s.graph_configured,
        use_web,
    )
    # 【这行在干什么】拿这个键去缓存里查一下，看是不是已经有现成的 Agent。
    # 【写法解释】dict.get(key) 取不到时返回 None（不会报错），所以下面用 is not None 判断。
    agent = _agent_cache.get(key)
    if agent is not None:
        # 【这行在干什么】缓存命中！直接返回旧对象，省掉重建的开销。
        return agent

    # 【这行在干什么】创建一个"聊天模型"对象（LangChain 认识的模型封装）。
    # 【参数逐个解释】
    #   · base_url / api_key —— 服务地址与密钥（来自 .env）；
    #   · model —— 用哪个模型（agent_model_or_default：配了专用 Agent 模型就用它，否则用默认）；
    #   · temperature —— 随机性旋钮（业务问答要稳，配得比较低）；
    #   · max_tokens —— 单次最多输出多少词元（agent_max_tokens；本项目曾因 1024 太小导致
    #     引用 5 条资料时被截断、content 为空，后来调大到 3072）；
    #   · timeout —— 单次请求最长等多久。
    model = ChatOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.agent_model_or_default,
        temperature=s.llm_temperature,
        max_tokens=s.agent_max_tokens,
        timeout=s.llm_timeout,
    )
    # 【这行在干什么】建工具列表，先把"知识库检索"放进去——它是永远注册的基础工具。
    tools: list[Any] = [search_bidding_knowledge]
    # 【这行在干什么】PostgreSQL 配置齐全时，把 PG 工具也加进来。
    if s.pg_configured:
        tools.append(query_bidding_postgres)
    # 【这行在干什么】Neo4j 图谱配置齐全时，把图谱工具加进来。
    if s.graph_configured:
        tools.append(query_bidding_graph)
    # 【这行在干什么】本轮允许（且后端配好了）联网时，把联网搜索工具加进来。
    if use_web:
        tools.append(search_web_bidding)

    # 【这行在干什么】调用 langgraph 的函数，把模型、工具、系统提示词装配成一个 ReAct Agent。
    # 【参数逐个解释】
    #   · model —— 上面创建的聊天模型；
    #   · tools —— 工具列表（模型只能调这里面的工具）；
    #   · prompt —— 系统提示词（工作守则，见 _agent_system_prompt）。
    # 【为什么必须用它（实测结论，别照记忆改）】本项目实测过：langchain 的
    #   create_agent 只返回一整段完整消息、没有逐字流式效果，打字机效果会丢；
    #   而 langgraph 的 create_react_agent 能逐 token 流式输出（实测收到 77 个分片）。
    agent = create_react_agent(
        model,
        tools=tools,
        prompt=_agent_system_prompt(s, web_search_enabled=use_web),
    )
    # 【这行在干什么】把刚建好的 Agent 存进缓存，下次同样的配置就能直接复用。
    _agent_cache[key] = agent
    # 【这行在干什么】返回这个 Agent。
    return agent


# 【这行在干什么】定义函数 _run_retrieval：执行"混合检索 + 重排"，返回给模型看的资料文本。
# 【参数逐个解释】
#   · query —— 检索用的关键词（注意：可能是模型改写过的，不一定等于用户原话）；
#   · state —— 本次请求的"_rag_state"字典（用来把检索统计、来源等结果"带回去"）。
# 【返回值】二元组（给模型看的参考资料文本, 命中条数）。
# 【为什么写成"返回二元组"】调用方（工具函数）既要把资料文本交给模型，又要把命中条数
#   记进 state 供前端展示徽标（"命中 N 条"），所以两个都返回。
def _run_retrieval(query: str, state: dict[str, Any]) -> tuple[str, int]:
    """执行「混合检索 + 重排」，返回（给模型看的参考资料文本, 命中条数）。

    这是同步阻塞的一段（向量化、Qdrant 查询、重排的大模型调用），
    由工具侧用 ``asyncio.to_thread`` 丢到工作线程里跑，避免阻塞事件循环。
    """
    # 【这行在干什么】从 state 里取出本次请求的配置对象。
    # 【写法解释】`s: Settings = state["settings"]` 里的 `: Settings` 是类型标注，
    #   只是给人和工具看的提示，运行时不强制检查。
    s: Settings = state["settings"]
    # 【这行在干什么】取出"最终要保留几条给模型"（final_k）。
    want_k: int = state["final_k"]
    # 【这行在干什么】算出"要先捞多少条候选"：取「最终条数」和「配置里的重排候选数」中较大的那个。
    # 【为什么这样算（关键设计）】重排的输入必须比输出多才有意义——先捞一大把候选池，
    #   让重排从中精选出 want_k 条。如果候选池和最终条数一样多，重排就"无从选择"了。
    #   max(...) 是保证候选池至少不少于最终条数。
    pool_k = max(want_k, s.rerank_candidates)

    # 【这行在干什么】调用混合检索，从"候选池"规模开始捞。
    # 【参数解释】final_k=pool_k 表示"这一轮先要这么多条"。
    result = retrieve(query, settings=s, final_k=pool_k)
    # 【这行在干什么】对捞到的候选做重排，精筛出 want_k 条。
    # 【返回值解释】hits 是排好序（可能按原序，若重排失败）的候选，rerank_meta 是重排过程的诊断信息。
    hits, rerank_meta = rerank_hits(query, result.hits, s, top_k=want_k)
    # 【这行在干什么】把精筛后的结果写回 result 对象，保持数据一致。
    result.hits = hits
    # 【这行在干什么】把重排诊断信息也写回（前端要展示"重排是否生效/失败原因"）。
    result.rerank_meta = rerank_meta

    # 【这行在干什么】把命中结果转成前端要的"来源卡片"格式（含表名、记录ID、原文等）。
    sources = result.to_sources()
    # 【这行在干什么】把来源列表存进 state，供编排层（agent_events）在合适时机发给前端。
    # 【关键】工具函数不能直接 yield 事件（它是普通函数，不是生成器），
    #   所以用"写进 state"这种办法把结果"寄存"起来，由外层统一发。
    state["sources"] = sources
    # 【这行在干什么】把本次检索的统计信息打包存进 state（前端要展示检索徽标）。
    # 【写法拆解】`**result.rerank_meta` 是"解包"：把 rerank_meta 这个字典里的键值对
    #   一股脑展开拼进当前字典里（相当于把它的所有条目都并进来）。
    #   【为什么把它放最后】这样即使前面某个键名和它重名，也会被重排的元信息覆盖
    #   （以重排的为准），行为明确。
    state["meta"] = {
        "dense_count": result.dense_count,
        "sparse_count": result.sparse_count,
        "bm25_ready": result.bm25_ready,
        "final_count": len(sources),
        "llm_configured": s.llm_configured,
        **result.rerank_meta,
    }

    # 【这行在干什么】如果一条都没命中，返回"没数据"的提示语和 0。
    # 【为什么返回提示语而不是空字符串】见 _NO_HIT_NOTICE 的注释：空字符串会让模型
    #   无所适从，而这段提示语明确要求它"如实说没查到、不许编造"。
    if not sources:
        return _NO_HIT_NOTICE, 0
    # 【这行在干什么】正常有结果：把来源渲染成「[参考资料N]……」的文本块交给模型，并返回命中条数。
    return render_reference_blocks(sources), len(sources)


# 【这行在干什么】定义函数 _snapshot_rag_state：给当前检索结果"拍张快照"。
# 【参数解释】state —— 本次请求的 "_rag_state" 字典。
# 【返回值】一个只含本次检索成果的新字典（来源列表、统计、命中条数）。
# 【为什么需要"快照"（深层设计，务必理解）】
#   _run_retrieval 对 state["sources"] / state["meta"] 是**覆盖写**（直接赋值，不留旧值）。
#   普通 ReAct 只有一个回合，覆盖也没关系；但深度模式会跑好几个子任务，
#   第二个子任务一检索就会把第一个的成果冲掉，最后前端只能看到最后一步的结果。
#   所以在每步子任务刚跑完、结果还没被冲掉时，立刻复制一份存起来，最后统一发。
def _snapshot_rag_state(state: dict[str, Any]) -> dict[str, Any]:
    """把 ``_rag_state`` 里的检索结果快照一份。

    ``_run_retrieval`` 对 ``state["sources"]`` / ``state["meta"]`` 是**覆盖写**，
    深度模式的多个子任务会互相冲掉。因此每步子任务结束后必须立刻快照，
    之后再统一编号、一次发给前端。
    """
    # 【这行在干什么】造一个"浅拷贝"的新字典返回。
    # 【写法逐个解释】
    #   · list(state.get("sources") or []) —— 复制一份来源列表：state 里可能是 None，
    #     用 `or []` 兜底成空列表，再 list(...) 复制成新列表（这样后续 state 被覆盖也不影响快照）；
    #   · dict(state.get("meta") or {}) —— 同样复制一份统计字典；
    #   · int(state.get("hit_count") or 0) —— 命中条数，取不到就是 0，并强制转成整数类型。
    # 【为什么要复制而不是直接引用（重要）】如果直接返回原对象，后面 state 被覆盖写时，
    #   快照里的内容也会跟着变——快照就白做了。复制一份才能"冻结"住当时的成果。
    return {
        "sources": list(state.get("sources") or []),
        "meta": dict(state.get("meta") or {}),
        "hit_count": int(state.get("hit_count") or 0),
    }


# 【这行在干什么】@tool 装饰器把下面这个普通函数"变身"成一个 LangChain 工具。
# 【为什么需要它】只有打了这个标签，模型才能"看见"并调用它；LangChain 会自动从函数签名
#   和 docstring 生成给模型的工具说明。
# 【为什么同时是 async】工具执行要调用检索（耗时），写成异步函数才能配合
#   asyncio.to_thread 把阻塞部分丢到旁支线程，避免卡住整个服务。
@tool
async def search_bidding_knowledge(query: str, config: RunnableConfig) -> str:
    """检索招投标知识库，返回与查询最相关的公告、项目与公司原文。

    知识库内容来自 MySQL 的 bidding_notices（招标公告）、bidding_project（招投标项目）、
    companies（公司信息）三张表。凡涉及具体项目名称、招标编号、预算金额、开标时间、
    资质要求、投标人资格、公司信息等事实性问题，都应先调用本工具获取依据。
    """
    # 【注意】上面这段 docstring 不是写给人看的注释，而是"工具说明书"，会被原样送给模型。
    #   所以措辞要特意写成"什么时候该用我"的口吻，例如"凡涉及……都应先调用本工具"。
    #   改这段文字会直接影响模型的调用决策，属于功能代码，不是普通注释。

    # 【这行在干什么】从 LangChain 传进来的 config 里，取出本项目挂钩的 "_rag_state" 字典。
    # 【写法逐步拆解】
    #   · config.get("configurable") —— 取 config 里的 configurable 这一节（可挂自定义数据的地方）；
    #   · or {} —— 万一没有这一节，用空字典兜底，避免下一步对 None 调 .get 报错；
    #   · .get("_rag_state") —— 再取我们自己的那格状态。
    state = (config.get("configurable") or {}).get("_rag_state")
    # 【这行在干什么】万一没取到状态（理论上不会发生，因为 agent_events 一定会传），
    #   就临时造一个"无状态"的兜底配置，保证工具照样能跑，不至于崩。
    if state is None:
        # 理论上不会发生（agent_events 一定会传）；兜底成一次无状态检索
        # 【这行在干什么】用全局配置现造一份最小的 state（含配置与最终条数）。
        state = {"settings": get_settings(), "final_k": get_settings().retrieve_final_top_k}
    # 【这行在干什么】真正执行检索。
    # 【关键：为什么用 asyncio.to_thread 包一层（性能设计，务必理解）】
    #   _run_retrieval 内部是"同步阻塞"的：要算向量、查 Qdrant、还要调大模型做重排，
    #   可能跑好几秒。如果直接在异步函数里 → 这几秒内**整个服务的所有请求都会被卡住**
    #   （因为单线程的事件循环被占满了）。asyncio.to_thread 把它丢到旁支线程去跑，
    #   主线程这几秒可以去服务别的请求，互不影响。
    #   【写法解释】await 表示"等这件事做完"；to_thread 的返回值就是被调用函数的返回值，
    #   所以这里拿到的是一个 (文本, 条数) 二元组，用了"元组解包"一次赋给两个变量。
    text, hit_count = await asyncio.to_thread(_run_retrieval, query, state)
    # 【这行在干什么】把命中条数记回 state，供前端展示"命中 N 条"徽标，也供快照使用。
    state["hit_count"] = hit_count
    # 【这行在干什么】把资料文本返回给模型（模型会把它当成"工具执行结果"读到）。
    # 【注意事项】工具函数只能返回字符串（或简单类型），不能返回我们的事件字典——
    #   事件由外层编排层统一发（见 _stream_react_turn）。
    return text


# 【这行在干什么】定义函数 _trim_history：把历史对话裁到最近的若干轮。
# 【参数逐个解释】
#   · history —— 完整历史对话列表（每条是一问或一答，形如 {"role":..., "content":...}）；
#   · turns —— 想保留几轮（一问一答算一轮）。
# 【返回值】裁剪后的历史列表。
# 【为什么必须裁剪（工程必要性）】
#   ① 模型能接收的内容长度有上限，历史太长会挤掉资料、甚至报错；
#   ② 历史按词元计费，全带上很费钱；
#   ③ 太早的对话往往和当前问题无关，带上反而干扰判断。
def _trim_history(history: list[dict[str, Any]] | None, turns: int) -> list[dict[str, Any]]:
    """只保留最近 ``turns`` 轮（一问一答算一轮）；turns <= 0 表示不带历史。"""
    # 【这行在干什么】没有历史、或者 turns<=0（明确表示不要历史）时，直接返回空列表。
    if not history or turns <= 0:
        return []
    # 【这行在干什么】从历史列表末尾往前切出 turns*2 条。
    # 【写法解释】history[-turns * 2 :] 是"负索引切片"：从倒数第 turns*2 个元素一直取到末尾。
    #   负数表示"从结尾往回数"，例如 history[-4:] 是"最后 4 条"。
    # 【为什么乘 2（关键）】一轮对话 = 用户问一句 + 助手答一句 = 2 条消息，
    #   所以要保留 turns 轮就得取 turns×2 条。
    # 【边界情况】如果历史本身不足 turns*2 条，切片会返回全部（不会报错），行为自然。
    return history[-turns * 2 :]


# 【这行在干什么】定义函数 _build_messages：把历史对话 + 当前问题，组装成 LangChain 消息列表。
# 【参数逐个解释】
#   · question —— 用户当前的问题；
#   · history —— 历史对话（可能为 None）；
#   · s —— 配置对象（用其中的 agent_history_turns 决定带几轮）。
# 【返回值】LangChain 消息对象列表（AIMessage / HumanMessage 混排）。
# 【为什么不直接用 rag.build_messages】那个函数处理的是"单次独立问答"（只含资料和问题，
#   不带历史）。Agent 模式要支持多轮上下文，所以历史消息要单独拼进来。
def _build_messages(
    question: str,
    history: list[dict[str, Any]] | None,
    s: Settings,
) -> list[Any]:
    """把历史对话与当前问题组装成 LangChain 消息列表。"""
    # 【这行在干什么】建空列表收集消息。
    messages: list[Any] = []
    # 【这行在干什么】遍历裁剪后的历史，逐条转换成 LangChain 消息对象。
    # 【写法解释】_trim_history(history, s.agent_history_turns) 先按配置裁好轮数，
    #   turns 这个"带几轮"的值来自 .env（agent_history_turns），不写死。
    for turn in _trim_history(history, s.agent_history_turns):
        # 【这行在干什么】取出这条历史消息的角色（"user" 还是 "assistant"），转成字符串。
        # 【写法解释】`str(turn.get("role") or "")`：安全取值再强转字符串，
        #   万一历史数据里角色字段缺失或类型不对，也不会崩。
        role = str(turn.get("role") or "")
        # 【这行在干什么】取出内容并去掉首尾空白。
        content = str(turn.get("content") or "").strip()
        # 【这行在干什么】内容为空的消息直接跳过。
        # 【为什么必须跳过】模型接口对空内容消息可能报错；而且空消息也没任何信息量。
        if not content:
            continue
        # 【这行在干什么】按角色创建对应类型的消息对象，追加进列表。
        # 【写法拆解】`AIMessage(...) if role == "assistant" else HumanMessage(...)`
        #   是三目表达式（"如果……就……否则……"的一行写法）：
        #   角色是 assistant（助手说的）→ 造 AIMessage；其他情况（用户说的）→ 造 HumanMessage。
        # 【为什么这样分】LangChain 靠对象类型来区分这句话是谁说的，让模型能正确理解多轮上下文。
        messages.append(AIMessage(content=content) if role == "assistant" else HumanMessage(content=content))
    # 【这行在干什么】把用户当前的问题作为最后一条消息追加进去。
    # 【为什么放最后】这样模型读到的是一个自然的时间顺序：先看历史、再看当前问题。
    messages.append(HumanMessage(content=question))
    # 【这行在干什么】返回组装好的消息列表。
    return messages


# 【这行在干什么】定义函数 _iter_update_messages：从 updates 流的输出里，挑出本步新增的消息。
# 【参数解释】payload —— updates 流给出的一个数据块（结构是嵌套字典，比较绕）。
# 【返回值】本次新增的消息对象列表。
# 【为什么需要这个函数（关键，解释那段"结构绕"）】
#   updates 流的 payload 形如 {"某个节点名": {"messages": [消息1, 消息2]}}——
#   外面一层是"哪个处理节点"，里面一层是"这个节点产出了什么"。
#   我们要的是把所有节点产出的消息都收集出来，所以得做两层遍历。
#   把它单独抽成一个函数，是为了让主循环读起来清爽（只看见一个函数名，不用看两层嵌套）。
def _iter_update_messages(payload: Any) -> list[Any]:
    """从 ``updates`` 流的 payload 里取出本步新增的消息。"""
    # 【这行在干什么】准备结果列表。
    out: list[Any] = []
    # 【这行在干什么】如果 payload 根本不是字典（结构不符合预期），直接返回空列表。
    # 【为什么必须判断（防崩）】下面要调 .values()，对非字典类型调用会报错；先挡住更安全。
    if not isinstance(payload, dict):
        return out
    # 【这行在干什么】遍历外层字典的"每个节点的产出"（外层字典的值）。
    for update in payload.values():
        # 【这行在干什么】如果这一项不是字典，跳过（结构不符合预期，避免下一行报错）。
        if not isinstance(update, dict):
            continue
        # 【这行在干什么】取出该节点产出的消息列表，逐条收进结果。
        # 【写法解释】`update.get("messages") or []`：取不到就用空列表兜底，
        #   这样即使某个节点没产出消息，也能安全跳过而不是报错。
        for msg in update.get("messages") or []:
            out.append(msg)
    # 【这行在干什么】返回收集到的消息。
    return out


# 【这行在干什么】定义函数 _new_turn_state：造一个"本轮对话的公共状态"，用于跨事件协作。
# 【返回值】含两个标记的字典。
# 【为什么需要它（协作机制）】事件翻译函数（_stream_react_turn）要产出很多事件，
#   但有些判断需要"记住之前发过什么"，比如：
#   · tool_called —— 本轮到底有没有调过工具？决定最终 done 事件是 agent_rag 还是 agent_direct；
#   · sources_emitted —— 来源是否已经发过？因为前端收到 sources 是"覆盖写"，
#     发两次会丢掉前面的（深度模式下尤其要小心，所以需要这个标记来避免重复发）。
#   这两个标记被"翻译函数"写、"调用方"读，所以要放在一个共享的字典里传递。
def _new_turn_state() -> dict[str, Any]:
    """一次 ReAct 轮次的跨事件状态（供事件翻译与调用方共同读取）。"""
    # 【这行在干什么】返回初始状态：还没调过工具、还没发过来源。
    return {"tool_called": False, "sources_emitted": False}


# 【这行在干什么】定义函数 _new_states：造出一次问答所需的全部"回收字典"。
# 【参数逐个解释】
#   · s —— 配置对象；
#   · final_k —— 最终保留条数（可能为 None，表示用配置默认值）。
# 【返回值】一个字典，装着 4 个"每请求状态字典"。
# 【为什么叫"回收位"（核心机制说明）】
#   工具函数（检索/PG/图谱/联网）执行时会产生一些细节（生成的 SQL、Cypher、结果行……），
#   这些细节不能通过"工具返回值"传出来（模型只该看到文字答案）。本项目的做法是：
#   预先准备好一批"格子盒"，通过 config 塞给工具；工具执行时把细节写进盒子；
#   编排层随后打开盒子，把细节做成事件发给前端。
#   这样"模型看到的内容"和"前端看到的技术细节"就分开了。
# 【为什么要"共用同一组"（docstring 强调的重点）】深度模式会跑多个子任务，
#   如果每个子任务各造一套盒子，那第二步步就看不到第一步的检索结果，无法累积。
def _new_states(s: Settings, final_k: int | None) -> dict[str, dict[str, Any]]:
    """构造一次问答所需的 4 个 per-request 回收字典。

    工具通过 ``config["configurable"]`` 里的这四个字典把执行细节回传给编排层
    （见各工具的 ``@tool`` 实现）。深度模式的多个子任务必须**共用同一组**，
    否则步骤之间的检索结果无法累积。
    """
    # 【这行在干什么】返回 4 个盒子，键名以下划线开头表示"内部使用"。
    return {
        # 【这行在干什么】知识库检索的盒子：装配置、目标条数、索到的来源、统计、命中条数。
        # 【为什么 sources/meta 初始是 None 而不是 []】None 表示"还没检索过"，
        #   与"检索过但零结果"（空列表）是不同的语义。外层靠这个区分。
        "_rag_state": {
            "settings": s,
            "final_k": final_k or s.retrieve_final_top_k,
            "sources": None,
            "meta": None,
            "hit_count": 0,
        },
        # PostgreSQL 工具的每请求回收位（生成的 SQL、结果行、错误）
        # 【这行在干什么】PG 盒子：装配置、生成的 SQL、列名、数据行、行数、错误信息。
        "_pg_state": {
            "settings": s,
            "sql": None,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": None,
        },
        # Neo4j 图谱工具的每请求回收位（生成的 Cypher、结果行、错误）
        # 【这行在干什么】图谱盒子：装配置、生成的 Cypher、数据行、行数、错误信息。
        "_graph_state": {
            "settings": s,
            "cypher": None,
            "rows": [],
            "row_count": 0,
            "error": None,
        },
        # 联网搜索工具的每请求回收位（关键词、结果、错误）
        # 【这行在干什么】联网盒子：装配置、最大结果数、搜索关键词、搜索结果、错误信息。
        "_web_state": {
            "settings": s,
            "max_results": s.tavily_max_results,
            "query": None,
            "results": [],
            "error": None,
        },
    }


# 【这行在干什么】定义核心生成器 _stream_react_turn：驱动一次 ReAct 执行，把过程翻译成事件。
# 【参数逐个解释】
#   · agent —— 装配好的 Agent 对象；
#   · stream_input —— 喂给 Agent 的输入（通常是 {"messages": [...]}）；
#   · turn —— 本轮共享状态（_new_turn_state 造的，用来记录"工具调过没、来源发过没"）；
#   · states —— 4 个回收盒子（_new_states 造的）；
#   · 星号 * 之后的都是"关键字专用参数"，调用时必须写名字（防参数顺序写错）：
#   · question —— 用户原问题（工具调用里若没写检索词，用它兜底）；
#   · recursion_limit —— 递归上限（防止模型"调工具→再调工具"无限循环）；
#   · emit_sources=True —— 是否直接产出 sources 事件（深度模式逐步执行时传 False）；
#   · emit_delta=True —— 是否把正文当作 delta 发给用户（深度模式子任务传 False）。
# 【返回值】一个异步迭代器，逐条产出事件字典。
# 【这个函数的定位（重要）】它是"普通 ReAct 通路"和"深度模式每个子任务"**共用的同一份驱动**。
#   把这段逻辑集中在一处，是为了避免两份代码各自演化、行为逐渐不一致（所谓"逻辑漂移"）。
async def _stream_react_turn(
    agent: Any,
    stream_input: dict[str, Any],
    turn: dict[str, Any],
    states: dict[str, dict[str, Any]],
    *,
    question: str,
    recursion_limit: int,
    emit_sources: bool = True,
    emit_delta: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """消费一次 ReAct ``astream``，把工具消息翻译成既有事件。

    这是普通 ReAct 通路与深度模式每步子任务**共用**的同一份驱动，避免两份逻辑漂移。
    只产出 ``retrieval`` / ``sources`` / ``delta`` / ``tool_call`` / ``tool_result`` /
    ``sql_query`` / ``graph_query`` / ``web_search``；``done`` / ``error`` 由调用方决定。

    - ``emit_sources=False``：深度模式逐步执行时用 —— 来源要等 solve 前全局重编号后
      一次性发出（前端对 ``sources`` 是覆盖写，多次发会丢前面的），但 ``turn``
      仍会标记已产出来源。
    - ``emit_delta=False``：深度模式逐步执行时用 —— 子任务的正文只当「观察结果」，
      不能冒泡成用户看到的答案。此时正文改以 ``step_text`` 事件交给调用方，
      否则调用方拿不到子任务结论（``delta`` 被抑制后就没有任何通道能取到文本）。
    """
    # 【这行在干什么】把 4 个盒子从 states 里取出来，起短名字方便后面反复用。
    rag_state = states["_rag_state"]
    pg_state = states["_pg_state"]
    graph_state = states["_graph_state"]
    web_state = states["_web_state"]

    # 【这行在干什么】启动 Agent 的异步流式执行，拿到一个"事件流"。
    # 【参数逐个解释】
    #   · stream_input —— 输入（含消息列表）；
    #   · stream_mode=["messages", "updates"] —— 同时订阅两种流（本项目实测必需，见下）；
    #   · config 里的 configurable —— 把 4 个盒子挂上去，工具执行时能从里面取到自己的盒子；
    #   · config 里的 recursion_limit —— 递归上限。
    # 【为什么必须同时订阅两条流（实测结论，别改）】
    #   · messages 流：能拿到"逐字的正文增量"和"工具执行结果"，但那条 AI 消息里的
    #     工具调用参数是**空的**；
    #   · updates 流：能拿到**完整的工具调用参数**（模型到底传了什么给工具）。
    #   所以正文和结果从 messages 取，参数从 updates 取，两条流配合使用。
    stream = agent.astream(
        stream_input,
        stream_mode=["messages", "updates"],
        config={
            "configurable": dict(states),
            "recursion_limit": recursion_limit,
        },
    )
    # 【这行在干什么】异步遍历事件流，每次拿到"流类型 mode"和"数据 payload"。
    # 【写法解释】stream_mode 给了两种值，所以每条事件都是 (mode, payload) 这样的二元组。
    async for mode, payload in stream:
        # 【这行在干什么】如果是 messages 流（正文与工具结果都从这里来）。
        if mode == "messages":
            # 【这行在干什么】把 payload 归一成"那一条消息对象"。
            # 【为什么需要这一步（框架细节）】messages 流的 payload 结构在不同版本里可能是
            #   一个元组 (消息, 元信息) 或直接就是消息。这里统一处理：是元组就取第 0 项，
            #   否则原样使用。这样写能兼容两种形态，更稳。
            chunk = payload[0] if isinstance(payload, tuple) else payload
            # 【这行在干什么】如果这条是工具执行结果（ToolMessage），就分情况翻译成事件。
            if isinstance(chunk, ToolMessage):
                # 【这行在干什么】先处理 PostgreSQL 工具的结果。
                # 【怎么认出来的】用工具名字判断（chunk.name 是执行该工具的函数名）。
                if chunk.name == PG_TOOL_NAME:
                    # PostgreSQL 工具执行完：先给 SQL 与结果，再给工具结果
                    # 【这行在干什么】产出 sql_query 事件：把工具写进盒子的 SQL、列名、行数、
                    #   数据行、错误信息，一次性发给前端（前端用它渲染"生成的 SQL + 结果表格"）。
                    # 【写法解释】`pg_state.get("sql") or ""` 这种写法是"安全取值 + 兜底"：
                    #   万一盒子里的字段是 None，就退回一个空字符串/空列表/0，避免前端拿到 null。
                    yield {
                        "type": "sql_query",
                        "sql": pg_state.get("sql") or "",
                        "columns": pg_state.get("columns") or [],
                        "row_count": pg_state.get("row_count") or 0,
                        "rows": pg_state.get("rows") or [],
                        "error": pg_state.get("error"),
                    }
                    # 【这行在干什么】紧接着产出 tool_result 事件，告诉前端"某个工具执行完了、
                    #   产出多少条结果"（前端据此显示"已调用 PostgreSQL 查询 · 命中 N 条"徽标）。
                    # 【为什么顺序是"先细节后结果"】前端拿到 tool_result 时就知道这一步结束了，
                    #   细节必须在它之前发出去，否则徽标出现时细节还没到。
                    yield {
                        "type": "tool_result",
                        "name": PG_TOOL_NAME,
                        "count": pg_state.get("row_count") or 0,
                    }
                # 【这行在干什么】图谱工具的结果处理，思路与 PG 完全一致。
                elif chunk.name == GRAPH_TOOL_NAME:
                    # 图谱工具执行完：先给生成的 Cypher 与结果，再给工具结果
                    # 【这行在干什么】产出 graph_query 事件（含 Cypher、行数、数据行、错误）。
                    yield {
                        "type": "graph_query",
                        "cypher": graph_state.get("cypher") or "",
                        "row_count": graph_state.get("row_count") or 0,
                        "rows": graph_state.get("rows") or [],
                        "error": graph_state.get("error"),
                    }
                    # 【这行在干什么】产出对应的 tool_result（前端显示"已调用知识图谱查询 · 命中 N 条"）。
                    yield {
                        "type": "tool_result",
                        "name": GRAPH_TOOL_NAME,
                        "count": graph_state.get("row_count") or 0,
                    }
                # 【这行在干什么】联网搜索工具的结果处理，思路同上。
                elif chunk.name == WEB_TOOL_NAME:
                    # 联网搜索执行完：先给搜索结果（含相关性评分），再给工具结果
                    # 【这行在干什么】先取回结果列表（下面算条数还要用，所以存成变量）。
                    results = web_state.get("results") or []
                    # 【这行在干什么】产出 web_search 事件（含关键词、结果列表、错误）。
                    # 【为什么结果里要带 score】前端要展示"相关性 0.93"这类评分，
                    #   让用户对搜索结果的可信度有个直观判断。
                    yield {
                        "type": "web_search",
                        "query": web_state.get("query") or "",
                        "results": results,
                        "error": web_state.get("error"),
                    }
                    # 【这行在干什么】产出 tool_result，条数用结果列表的长度。
                    yield {
                        "type": "tool_result",
                        "name": WEB_TOOL_NAME,
                        "count": len(results),
                    }
                # 【这行在干什么】剩下的情况当作"知识库检索工具"的结果处理。
                else:
                    # 知识库检索：先给检索统计与来源，再给工具结果
                    # 【这行在干什么】判断"要不要现在发 sources 事件"。
                    # 【两个条件逐个解释】
                    #   · emit_sources —— 调用方允许发（深度模式逐步执行时是不允许的，
                    #     因为来源要等所有子任务跑完、统一编号后再一次性发）；
                    #   · not turn["sources_emitted"] —— 本轮还没发过（防重复：
                    #     前端收到 sources 是覆盖写，发两次会丢掉前一次的）。
                    if emit_sources and not turn["sources_emitted"]:
                        # 【这行在干什么】先发 retrieval 事件（检索统计：两路各召回多少、
                        #   BM25 是否可用、重排是否生效），前端用它显示检索徽标。
                        yield {"type": "retrieval", "meta": rag_state.get("meta") or {}}
                        # 【这行在干什么】再发 sources 事件（命中来源列表，前端渲染成来源卡片）。
                        yield {"type": "sources", "sources": rag_state.get("sources") or []}
                    # 【这行在干什么】无论这次是否真的发了 events，都把"已产出来源"标上。
                    # 【为什么无条件标记（细节设计）】这样即使调用方选择不发（emit_sources=False），
                    #   调用方事后也能从 turn 里知道"来源其实已经拿到了"，便于它自己决定什么时候发。
                    turn["sources_emitted"] = True
                    # 【这行在干什么】产出 tool_result 事件，条数取盒子里的命中条数。
                    # 【写法解释】`chunk.name or TOOL_NAME`：万一消息没带名字，就默认按知识库工具算。
                    yield {
                        "type": "tool_result",
                        "name": chunk.name or TOOL_NAME,
                        "count": rag_state.get("hit_count", 0),
                    }
            # 【这行在干什么】如果这条是 AI 说的且有正文内容，就把它作为"正文"发出去。
            elif isinstance(chunk, AIMessage) and chunk.content:
                # emit_delta=False 时改发 step_text：深度模式的调用方需要用它当
                # 「观察结果」喂给 solve 阶段，但它绝不能冒泡成用户看到的正文。
                # 【写法解释】`"delta" if emit_delta else "step_text"` 是三目表达式：
                #   允许发正文 → 类型叫 delta（前端会拼进答案里）；不允许 → 类型叫 step_text
                #   （只有编排层自己消费，前端不会当答案显示）。
                # 【为什么要这样区分（血泪教训）】深度模式子任务的正文只是"中间观察结果"，
                #   如果当成 delta 发出去，用户会看到答案里混进一堆半成品。
                #   但如果干脆什么都不发，调用方又拿不到子任务的结论（delta 被抑制后
                #   就没有任何通道能取到文本）——所以改个名字发，各用各的。
                yield {"type": "delta" if emit_delta else "step_text", "text": chunk.content}
            # 【这行在干什么】messages 流处理完了，跳到下一次循环。
            # 【为什么要 continue】防止代码"掉"到下面那段（那段是专门处理 updates 流的）。
            continue

        # updates：这里才有完整的工具调用参数
        # 【这行在干什么】遍历本步新增的消息，找出"模型决定要调用工具"的那些。
        for msg in _iter_update_messages(payload):
            # 【这行在干什么】只关心 AI 消息、且这条消息确实带了工具调用（tool_calls 非空）。
            # 【写法解释】`not isinstance(msg, AIMessage) or not getattr(msg, "tool_calls", None)`
            #   的两个条件：不是 AI 消息 → 跳过；是 AI 消息但没带工具调用 → 也跳过。
            #   用 getattr 安全取值，防止某些消息对象没有 tool_calls 属性时报错。
            if not isinstance(msg, AIMessage) or not getattr(msg, "tool_calls", None):
                continue
            # 【这行在干什么】逐条处理这次的工具调用（模型一轮里可能同时决定调好几个工具）。
            for call in msg.tool_calls:
                # 【这行在干什么】标记"本轮调用过工具"。
                # 【用途】agent_events 最后靠这个标记决定 done 事件是 agent_rag（用工具查过）
                #   还是 agent_direct（直接回答）。前端据此显示不同徽标。
                turn["tool_called"] = True
                # 【这行在干什么】取出模型给这个工具传的参数（一个字典）。
                # 【写法解释】`call.get("args") or {}`：取不到就用空字典兜底，防止下一步报错。
                args = call.get("args") or {}
                # 【这行在干什么】产出 tool_call 事件：告诉前端"模型要调用哪个工具、用什么查询词、
                #   完整参数是什么"，前端显示"正在检索…"之类的提示，并可展开看参数。
                # 【参数逐个解释】
                #   · name —— 工具名（取不到就用知识库工具名兜底）；
                #   · query —— 检索词：优先用模型自己填的（它可能改写得更准），
                #     没填才退回用户原问题（`args.get("query") or question`）；
                #   · arguments —— 完整参数，给前端展示用。
                yield {
                    "type": "tool_call",
                    "name": call.get("name") or TOOL_NAME,
                    "query": str(args.get("query") or question),
                    "arguments": args,
                }


# 【这行在干什么】定义对外主入口 agent_events：Agent 问答的完整事件流（异步生成器）。
# 【参数逐个解释】
#   · question —— 用户问题；
#   · settings —— 配置（可省，便于测试注入）；
#   · final_k —— 最终保留条数（可省）；
#   · history —— 历史对话（可省，用于多轮上下文）；
#   · web_search —— 本轮是否允许联网（None 表示"用配置里的默认开关"）；
#   · mode="react" —— 用哪种模式（react / plan / deep）。
# 【返回值】异步迭代器，一条条产出事件字典。
# 【提醒（重要约定）】当 mode="react" 时，本函数的行为必须与"改动前完全一致"——
#   这是项目硬约束（不要为了新功能顺手改到老路径的行为）。
async def agent_events(
    question: str,
    settings: Settings | None = None,
    final_k: int | None = None,
    history: list[dict[str, Any]] | None = None,
    web_search: bool | None = None,
    mode: str = "react",
) -> AsyncIterator[dict[str, Any]]:
    """Agent 问答事件流（异步生成器）。

    ``web_search`` 为本轮是否允许联网搜索（None 表示用 ``WEB_SEARCH_DEFAULT``）；
    实际生效还需后端配好 ``TAVILY_API_KEY``，否则工具不注册。

    ``mode`` 取 ``MODES`` 之一：``react`` 走原单轮通路（**行为与改动前完全一致**）；
    ``plan`` / ``deep`` 委托 ``deep_agent.run_deep_events``。
    """
    # 【这行在干什么】把模式名"洗一遍"：去掉首尾空白、统一转小写，让输入容错更宽松。
    # 【写法解释】`(mode or "react")` 先处理 None；.strip() 去空白；.lower() 转小写，
    #   这样前端传 "ReAct"、"DEEP " 也都能被正确识别（不区分大小写、忽略空格）。
    mode = (mode or "react").strip().lower()
    # 【这行在干什么】校验模式名是否合法；不认识的模式一律退回默认的 react。
    # 【为什么这样处理而不是报错（设计取舍）】模式只是个"偏好选项"，传错了没必要让整次问答失败；
    #   悄悄退回最稳妥的模式继续服务，用户体验更好。
    if mode not in MODES:
        mode = "react"

    # 【这行在干什么】确定用哪份配置（允许外部注入，便于测试）。
    s = settings or get_settings()
    # 【这行在干什么】把问题去掉首尾空白（防止用户手滑多打了空格/换行）。
    question = (question or "").strip()
    # 【这行在干什么】问题为空时，直接报错并结束，不往下走。
    # 【为什么提前拦】空问题传给模型毫无意义，也浪费一次调用；早报错语义更清晰。
    if not question:
        # 【这行在干什么】产出 error 事件（前端会显示错误提示）。
        yield {"type": "error", "message": "问题不能为空"}
        # 【这行在干什么】产出 done 事件，mode 标记为 agent_failed（Agent 失败）。
        # 【为什么必须发 done】前端要靠这个事件知道"流结束了"，否则会一直转圈等下去。
        yield {"type": "done", "mode": "agent_failed"}
        # 【这行在干什么】结束生成器（后面代码不再执行）。
        return

    # 未配置大模型时 Agent（含深度模式）都无法工作 → 直接委托纯 RAG 通道，行为与 /api/chat 一致
    # 【这行在干什么】如果 Agent 所需的大模型配置不全，就整个改用纯 RAG 通道。
    # 【为什么要这样降级（关键设计）】
    #   ① Agent 的"自主决策调不调工具"完全依赖大模型，没模型就没法工作（深度模式更没法工作）；
    #   ② 但纯 RAG 的"仅检索模式"不依赖模型（直接展示命中的原文摘要），依然有服务价值；
    #   ③ 委托给 answer_events 还能保证"降级后的行为"和 /api/chat 端点完全一致——
    #      用户无论走哪个入口，没配模型时看到的东西都一样，不会困惑。
    if not s.agent_configured:
        # 【这行在干什么】把纯 RAG 的事件流原样转发出去（一个 for 循环"搬运"）。
        # 【写法解释】因为是异步流，搬运也要用 async for。
        for event in answer_events(question, settings=s, final_k=final_k):
            yield event
        # 【这行在干什么】搬运完就结束（不再走下面的 Agent 逻辑）。
        return

    # 【这行在干什么】如果是 plan 或 deep 模式，转交给 deep_agent 处理。
    if mode in ("plan", "deep"):
        # 延迟导入：deep_agent 反向依赖本模块，放在模块顶层会成环
        # 【这行在干什么】把 deep_agent 的导入写在函数内部（"延迟导入"）。
        # 【为什么不能写在文件顶部（重要，否则程序起不来）】deep_agent.py 反过来会 import
        #   本模块的东西，如果两边都在文件顶部互相 import，Python 就会陷入
        #   "要导入 A 得先导入 B，要导入 B 又得先导入 A"的死循环（叫"循环导入"），直接报错。
        #   把导入推迟到"真正要用的时候"（也就是用户真的选了深度模式），就绕开了这个问题。
        from .deep_agent import run_deep_events

        # 【这行在干什么】驱动深度模式的执行流，把它的每个事件转发出去。
        # 【参数解释】mode 原样传下去（deep_agent 要靠它区分 plan 和 deep 两种强度）。
        async for event in run_deep_events(
            question,
            settings=s,
            final_k=final_k,
            mode=mode,
            web_search=web_search,
        ):
            yield event
        # 【这行在干什么】转发完结束（不走下面的单轮 ReAct 逻辑）。
        return

    # 【这行在干什么】算出本轮是否真的启用联网搜索：调用方明确传了就用它，没传就用配置的默认值。
    # 【写法解释】`s.web_search_default if web_search is None else bool(web_search)`：
    #   三目表达式——没传(None)用配置默认；传了就以传的为准，并强制转成真假值。
    use_web = s.web_search_default if web_search is None else bool(web_search)
    # 【这行在干什么】再叠加一层判断：即使开关开了，后端也得真的配好了联网搜索服务，才算数。
    # 【为什么再判一次】用户勾了"联网搜索"但后端没配 TAVILY_API_KEY 时，若还去注册工具，
    #   模型调用必然失败。这里统一按"实际能不能用"来定，避免后面白折腾。
    use_web = bool(use_web and s.web_search_configured)

    # 【这行在干什么】造出本次请求的 4 个回收盒子。
    states = _new_states(s, final_k)
    # 【这行在干什么】从盒子里取出检索专用那个，起短名字 state 便于下面使用。
    state = states["_rag_state"]
    # 【这行在干什么】造本次对话的共享状态（记录"工具调过没、来源发过没"）。
    turn = _new_turn_state()

    # 【这行在干什么】打开"接住岔子"的网——流式响应中途出错时，只能以"事件"的形式告诉前端，
    #   不能直接抛异常（因为这时候响应头早就发出去了，抛异常前端也收不到）。
    try:
        # 【这行在干什么】构造（或复用缓存里的）Agent 实例。
        agent = _build_agent(s, web_search_enabled=use_web)
        # 【这行在干什么】驱动这一轮 ReAct 执行，把产生的事件逐个转发出去。
        # 【参数逐个解释（第一个参数最值得注意）】
        #   · agent —— 装配好的 Agent；
        #   · {"messages": _build_messages(question, history, s)} —— 输入消息：
        #     用 _build_messages 把历史对话和当前问题组装成 LangChain 消息列表，
        #     再包成 Agent 要求的 {"messages": [...]} 结构；
        #   · turn / states —— 共享状态与回收盒子；
        #   · question —— 用户原问题（工具没填检索词时兜底用）；
        #   · recursion_limit=s.agent_max_steps —— 递归上限，本项目配的是 6
        #     （曾设为 3 时，多工具连续调用的场景会触发 "Recursion limit" 报错）。
        async for event in _stream_react_turn(
            agent,
            {"messages": _build_messages(question, history, s)},
            turn,
            states,
            question=question,
            recursion_limit=s.agent_max_steps,
        ):
            yield event
    # 【这行在干什么】兜住执行过程中的任何异常。
    # 【`# noqa: BLE001` 的原因】代码检查工具建议别用这么宽泛的捕获，但流式场景下
    #   我们**必须**接住一切异常，否则用户会看到一个"半截断掉"的响应，无从知晓出了什么事。
    except Exception as exc:  # noqa: BLE001 - 流式响应里出错只能以事件形式告知前端
        # 【这行在干什么】先发一条 error 事件，把失败原因告诉前端。
        yield {"type": "error", "message": f"Agent 执行失败：{exc}"}

        # 【这行在干什么】如果还没产出过任何检索结果，就"无处可补"，只能以失败收尾。
        # 【为什么这样判断】下面那段"补救"逻辑是基于"已经查到的资料"再补一段回答；
        #   一条资料都没有的话，补救也就无从谈起，直接结束更诚实。
        if not turn["sources_emitted"]:
            # 还没产出任何检索结果 → 无处可补，直接以失败收尾
            # 【这行在干什么】发 done 事件，标记这次是失败的。
            yield {"type": "done", "mode": "agent_failed"}
            # 【这行在干什么】结束生成器。
            return

        # 已经产出过来源 → 只补一次基于这些来源的回答
        # 【这行在干什么】取出之前检索到的来源。
        # 【为什么这里能拿到】因为检索结果被写进了盒子（state["sources"]），
        #   即使 Agent 后面的执行失败了，这份成果还在。
        sources = state.get("sources") or []
        # 【这行在干什么】先发一个"空正文"的 delta 事件。
        # 【为什么发个空的（小技巧）】它相当于告诉前端"正文要开始了"，前端据此把界面
        #   从"检索中"切到"正在作答"的状态；也保证前端一定能收到至少一个 delta。
        yield {"type": "delta", "text": ""}
        # 【这行在干什么】用这些来源，走一次"纯 RAG 生成"，把答案补救出来。
        try:
            # 【这行在干什么】逐段调用大模型生成回答，边生成边转发。
            # 【参数解释】build_messages(question, sources) 把来源拼成参考资料提示词
            #   （与 rag.py 完全同一套口径，保证引用编号一致）。
            for piece in stream_chat(build_messages(question, sources), s):
                yield {"type": "delta", "text": piece}
        # 【这行在干什么】如果连这次补救式调用也失败了（比如网络断了），做最后兜底。
        except LLMError as inner:
            # 【这行在干什么】先把失败原因如实告诉前端。
            yield {"type": "error", "message": str(inner)}
            # 【这行在干什么】再发一段"仅检索模式"的回答。
            # 【为什么这也不失为一种好的兜底】retrieval_only_answer 不需要调用大模型，
            #   它直接把命中原文整理成一段摘要展示给用户。虽然不如模型润色得好看，
            #   但"有依据的原始信息"对业务用户依然有价值——总比一个空白错误页强。
            yield {"type": "delta", "text": retrieval_only_answer(sources)}
        # 【这行在干什么】补救完成，发 done 事件（仍标记为 agent_failed，因为这轮确实出过错）。
        yield {"type": "done", "mode": "agent_failed"}
        # 【这行在干什么】结束生成器。
        return

    # 【这行在干什么】一切顺利：发 done 事件收尾，并用"本轮是否调过工具"决定最终标记。
    # 【两个取值的含义】agent_rag = 走了知识库/工具检索（前端显示"已调用…"类徽标）；
    #   agent_direct = 模型判断这是闲聊，直接回答（前端显示"未调用工具 · Agent 直接回答"）。
    yield {"type": "done", "mode": "agent_rag" if turn["tool_called"] else "agent_direct"}