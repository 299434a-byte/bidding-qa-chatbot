# ============================================================================
# 文件：deep_agent.py —— 「深度思考」模式的大脑（Plan-and-Solve + Reflection）
#
# 【这个文件在整个项目里是干什么的】
#   本项目一共三种问答模式（由前端「深度思考」开关选择），三者的关系是层层加码：
#     · react —— 单轮 ReAct：模型自己判断要不要查资料，查完就答（代码在 agent.py，最轻量）；
#     · plan  —— 先规划出几个子任务，逐个执行，最后把所有结论综合成一份答案（本文件）；
#     · deep  —— 在 plan 的基础上，再让模型给自己打分（自我评价 Reflection），
#                觉得答案不够好就重写一版（本文件）。
#   所以本文件 = 「plan 与 deep 两种模式的编排大脑」。
#
# 【「编排」是什么意思（大白话）】
#   编排 = 当导演。它自己不查资料、不写字，只负责指挥别人按剧本走：
#     先喊「规划！」（让模型列出子任务清单）
#     → 再喊「第 1 步，上！」（跑一轮 ReAct 去查资料）
#     → 「第 2 步，上！」 → ……
#     → 最后喊「把所有材料合成答案！」（这一步产出的文字才是给用户看的）
#   deep 模式还多一步：合成完再问模型「这答案行不行？不行说哪不行」，不行就重写。
#
# 【为什么不用 LangGraph 自带的 StateGraph 来做这件事（重要设计取舍，必须理解）】
#   唯一的判据是：**模型吐出来的字，必须能精确分流进不同的「桶」，不能串味**。
#     · 规划阶段吐的字是 JSON，只能进「计划」这个桶，绝不能当正文漏给用户看；
#     · 每个子任务吐的字是「中间材料」，只能进「观察结果」桶，也不能当正文；
#     · solve 阶段吐的字才是真正给用户看的答案；
#     · 修订阶段吐的字要单独一个桶（前端另开一个框显示）。
#   而 LangGraph 的 StateGraph 会把子图里每一步模型说的话全部「冒泡」上来，
#   你只能事后靠 metadata（元数据）去猜「这条是谁说的」——猜错一次，
#   规划用的 JSON 文字就漏进用户看到的正文里了；而且父状态会把所有子步骤的消息
#   全部累积起来，把 solve 阶段的上下文撑爆。
#   本项目已经被 langchain/langgraph 的版本行为坑过两次（详见 agent.py 的注释），
#   实在不想再引入第三个「版本一变就可能出问题」的东西。
#   → 结论：宁可自己手写这几步的调度逻辑，换来「token 分流绝对可控」。
#   代价是要自己维护流式驱动（就是"一个字一个字往外吐"那套），
#   而这个代价通过复用 agent.py 的 _stream_react_turn 抵消掉了。
#
# 【四个阶段（读本文件时请对照这四个词）】
#   1. plan     —— 一次「不带任何工具」的模型调用，让它产出 JSON 格式的子任务列表
#                  → 推一个 plan 事件给前端。
#                  【特别注意】这次调用吐出来的文字是 JSON，**绝不能当作 delta 推给前端**，
#                  否则用户会先看到一坨 JSON。
#   2. 逐步执行 —— 复用 agent.py 里同一个 ReAct agent，让每个子任务各跑一轮；
#                  传入**同一组**请求级状态字典（这样 sql_query/graph_query/web_search
#                  这些事件照旧能回传给前端）；但**丢弃子任务吐出的 delta**，
#                  只把它的最终文字当「观察结果」（截断保存，防止上下文爆炸）。
#                  子任务**必须一个接一个串行执行**（因为它们共用同一组状态字典）。
#   3. solve    —— 把「原问题 + 计划 + 全部观察结果 + 命中的来源」喂给模型，
#                  逐字推 channel="answer" 的 delta —— 这才是用户看到的答案。
#   4. reflect  —— 仅 deep 模式：让模型自评输出 JSON 判定 → 推 reflection 事件；
#                  若要改进，就用流式调用推 channel="revision" 的修订稿（前端单独渲染）。
#
# 【谁调用它（上游）】
#   backend/app/main.py 的 /api/agent/chat 端点 —— 当 mode 是 plan 或 deep 时调用 run_deep_events。
#
# 【它给谁用（下游）】
#   前端（Next.js）—— 消费本文件吐出的 plan / plan_step / reflection / delta(channel) 等事件，
#   渲染「计划卡片」「子任务进度」「自评与修订块」以及流式正文。
#
# 【本文件与 agent.py 的循环依赖关系（怎么破解的）】
#   本文件要 import agent.py 的内部工具（_build_agent、_stream_react_turn 等）；
#   而 agent.py 在需要跑深度模式时又要 import 本文件 —— 两边互相引用（循环依赖）。
#   破解办法写在 agent.py 里：那里用的是「用到时才导入」（延迟导入，import 写在函数体内部），
#   所以模块刚加载时不会真的绕成死循环。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring），内容与原文一字不差】
# 【为什么放在注释之后】注释不算「语句」，所以放在这一大段注释后面，
#   它依然是本模块的**第一条语句**、依然是官方自我介绍。
"""Plan-and-Solve + Reflection 深度推理编排。

对应两种模式：

- ``plan``：规划 → 逐步执行 → 综合成答案
- ``deep``：在 ``plan`` 之上追加 Reflection（自评 + 必要时修订）

### 为什么是「显式 async 编排」而不是新建 LangGraph StateGraph

唯一判据是 **token 路由必须可控**：正文、每步观察、修订稿必须落进不同缓冲区。

StateGraph 的 ``messages`` 流会把子图每一步的 AIMessage 全部冒泡上来，只能事后按
``metadata.langgraph_node`` 过滤 —— 过滤错一次，规划文本就会漏进用户看到的正文；
父 state 还会累积全部子步消息、撑爆 solve 阶段的上下文。本项目已经两次被
langchain/langgraph 的版本行为坑过（``astream_events`` 不吐 chat model 流、
``create_agent`` 不流式），不宜再增加第三个版本敏感面。

代价是自己维护流驱动 —— 通过复用 ``agent._stream_react_turn`` 消除这个代价。

### 四个阶段

1. **plan**：一次不带工具的 ``complete_chat``（``DEEP_MAX_TOKENS``）产出 JSON 子任务列表
   → 推 ``plan`` 事件。**这次调用的文本绝不能当 ``delta`` 推给前端。**
2. **逐步执行**：复用 ``_build_agent()`` 的同一个 ReAct agent，每个子任务跑一轮；
   传入**同一组** per-request state 字典，让 ``sql_query`` / ``graph_query`` /
   ``web_search`` 事件照旧回传；但**丢弃该子任务的 delta**，只把最终文本当「观察结果」
   （截断至 ``DEEP_OBSERVATION_CHARS``）。子任务**必须串行**（共用 state 字典），
   每步用新的 config，``recursion_limit=DEEP_STEP_MAX_STEPS``，不带 history。
3. **solve**：``astream_chat`` 把「原问题 + 计划 + 全部观察结果 + 命中来源」喂给模型，
   逐字推 ``channel="answer"`` 的 delta —— 这才是用户看到的答案。
4. **reflect**（仅 ``deep``）：``complete_chat`` 自评输出 JSON 判定 → 推 ``reflection`` 事件；
   若需改进，用 ``astream_chat`` 推 ``channel="revision"`` 的修订稿（前端单独渲染）。
"""

# 【这行在干什么】打开「类型注解延迟求值」开关（做法与 agent.py 一致）。
# 【为什么这么写】本文件用了 dict[str, Any]、AsyncIterator[...] 这类新式标注，
#   老版本 Python（3.8/3.9）不认识；加上这行后，标注先被当成字符串存着，需要时才解析。
# 【注意事项】必须放在所有执行代码之前，这是 Python 的硬性要求。
from __future__ import annotations

# 【这行在干什么】引入 asyncio（Python 自带的「异步工具箱」）。
# 【为什么需要】本文件有两处「同步阻塞」的活儿（规划调用、自评调用），
#   要用 asyncio.to_thread 把它们丢到旁支线程去跑，免得卡住整个服务。
import asyncio

# 【这行在干什么】引入 time 模块，用来计时。
# 【为什么需要】深度模式很贵（要调好几次大模型），所以设了总时限 deep_timeout；
#   用 time.monotonic() 取「单调递增的秒数」来判断是否超时。
# 【注意事项】monotonic 不受系统时间被修改的影响（普通 time.time() 会被改钟影响），
#   测「过了多久」必须用它。
import time

# 【这行在干什么】引入两种类型标注工具。
# 【参数逐个解释】
#   · Any —— 事件字典里的值形态不定（字符串/数字/列表/字典都可能），说不准就用它兜住；
#   · AsyncIterator —— 「异步迭代器」：表示「能被 async for 一段段取出内容」的对象。
#     本文件的核心生成器 run_deep_events 就返回这种类型。
from typing import Any, AsyncIterator

# 【这行在干什么】引入 HumanMessage —— LangChain 里代表「用户说的话」的消息类型。
# 【为什么需要】每个子任务都要以「用户提问」的形式塞给 ReAct agent 去执行。
from langchain_core.messages import HumanMessage

# 【这行在干什么】从同目录的 agent.py 里借 5 个内部工具函数（本文件复用它们，避免重写一遍）。
# 【参数逐个解释】
#   · _build_agent        —— 组装 ReAct agent（把模型、工具、提示词拼装起来）；
#   · _new_states         —— 新建「一次请求内共享的状态字典组」（用来存检索结果、来源等）；
#   · _new_turn_state     —— 新建「一轮对话的状态」；
#   · _snapshot_rag_state —— 把状态里的检索结果「快照」出来（本文件靠它取出子任务命中的来源）；
#   · _stream_react_turn  —— 驱动一轮 ReAct 的核心生成器（本文件最关键的复用点，
#                            见下方「阶段 2」处的注释）。
# 【为什么可以借用下划线开头的私有函数】它们本就是为 agent.py 与本文件的协作而设计的，
#   属于「项目内部接口」；两个文件是同一个 package 里的兄弟，互相借用是刻意的安排。
# 【注意事项】这里用的是三行式的括号导入，行尾没有逗号问题，改动时务必保持括号闭合。
from .agent import (
    _build_agent,
    _new_states,
    _new_turn_state,
    _snapshot_rag_state,
    _stream_react_turn,
)

# 【这行在干什么】引入配置类与「取配置」函数。
# 【为什么这么写】用哪个模型、最多拆几步、总时限多久……全部来自 .env，不写死在代码里。
from .config import Settings, get_settings

# 【这行在干什么】引入大模型相关的三样东西。
# 【参数逐个解释】
#   · LLMError      —— 大模型调用失败的异常类型（捕获它，好优雅降级而不是整个崩掉）；
#   · astream_chat  —— 流式调用（一个字一个字往外冒），给 solve 与修订阶段用；
#   · complete_chat —— 一次性调用（要完整结果），给规划与自评用
#                      （它们要的是结构化 JSON，不需要边生成边给用户看）。
from .llm import LLMError, astream_chat, complete_chat

# 【这行在干什么】从 rag.py 借四样东西，保证本文件产出的答案与 react 模式的风格一致。
# 【参数逐个解释】
#   · NO_DATA_MESSAGE         —— 「知识库没数据」时的统一话术；
#   · SYSTEM_PROMPT           —— 统一的人设与规矩提示词（要求只依据资料、标注 [参考资料N] 等）；
#   · render_reference_blocks —— 把来源列表渲染成「参考资料」文本块；
#   · retrieval_only_answer   —— 模型不可用时的「仅检索」兜底答案。
from .rag import (
    NO_DATA_MESSAGE,
    SYSTEM_PROMPT,
    render_reference_blocks,
    retrieval_only_answer,
)

# 【这行在干什么】从 rerank.py 借 extract_json —— 一个「从模型回复里抠出 JSON」的容错函数。
# 【为什么需要】规划和自评都要求模型只输出 JSON，但模型偶尔会包一层 ```json 代码围栏，
#   或者前后多几句废话；extract_json 能把这些噪音剥掉再解析，提高成功率。
from .rerank import extract_json

# 【下面这段注释是原有的，先保留】规划阶段：只产出 JSON 子任务列表，不要解释文字
# 注意：本串会被 ``.format(max_steps=...)`` 处理，示例 JSON 里的花括号必须写成
# ``{{`` / ``}}``，否则会被当成占位符而抛 KeyError（曾因此让规划阶段整段失效）。
# ----------------------------------------------------------------------------
# 【这块在干什么】定义「规划阶段」用的系统提示词（告诉模型：你是任务规划器，请把问题拆成子任务）。
# 【为什么这么写】规划要的是**结构化的机器可读结果**（JSON），所以要反复叮嘱「只输出 JSON」。
# 【潜在风险（血泪教训）】下面这串文字后面会用 .format(max_steps=...) 填占位符，
#   而 Python 的 format 把花括号 {} 当占位符；所以示例 JSON 里的每个花括号都必须写成
#   双写 {{ }} ——一旦漏了，.format 就会把 {"steps" 当成一个字段名去取值，
#   结果抛 KeyError，规划阶段直接整段失效（本项目踩过这个坑，务必留意）。
# 【参数逐个解释（下面每一行字符串）】
#   第 1 行：给模型定角色 + 交代核心任务（判断要不要拆）；
#   第 2 行：简单问题只出 1 个子任务（不为了凑数而硬拆）；
#   第 3 行：复杂问题拆成 2~N 个（N 由 {max_steps} 占位符在运行时填进去）；
#   第 4 行：要求子任务之间尽量不重复（避免两个步骤查同一件事，浪费钱）；
#   最后两行：给出 JSON 输出的格式示例（{{ 是转义后的 { ）。
# 【拼接方式说明】Python 中相邻的字符串字面量会自动首尾相接（这叫「隐式字符串拼接」），
#   所以下面这些行其实是一整条很长的提示词，只是拆成多行方便阅读。\n 表示换行符。
_PLAN_SYSTEM_PROMPT = (
    "你是「招投标智能问答助手」的任务规划器。请判断用户问题是否需要拆解成多个子任务。\n"
    "若问题简单（单一事实、单一查询），只输出 1 个子任务；\n"
    "若问题需要多角度、多数据源或多步推理才能回答完整，拆成 2~{max_steps} 个子任务。\n"
    "子任务之间应尽量不重复，各自回答一个方面。\n\n"
    '只输出 JSON，不要任何解释，形如：{{"steps": [{{"title": "不超过 20 字的子任务标题", '
    '"detail": "这一步具体要查什么、怎么做"}}]}}'
)

# 【下面这段注释是原有的，先保留】自评阶段：判定答案是否已足够好，不够好则指出问题
# ----------------------------------------------------------------------------
# 【这块在干什么】定义「自评阶段」用的系统提示词（告诉模型：你是严格的质检员，请审查答案）。
# 【为什么这么写】让模型扮演挑剔的审查员，才能把答案里的毛病（遗漏、与资料不符、编造）挑出来；
#   同时要求它「没问题就别乱改」，避免无谓的二次生成（省钱、也避免越改越差）。
# 【参数逐个解释（下面每一行字符串）】
#   第 1 行：给模型定角色（严格质检员）+ 列出要对照的材料；
#   第 2 行：给出判定标准（是否回答问题、是否只依据资料、关键数字/名称/日期是否一致、有无遗漏或编造）；
#   第 3 行：规定 JSON 输出格式（两种示例：ok 或 revise）；
#   第 4 行：强调「够好就输出 ok，只有确实要改才输出 revise」（防止模型总是要求修订）。
# 【注意事项】本串没有用 .format，所以里面的花括号**不需要**双写，保持单写即可。
_REFLECT_SYSTEM_PROMPT = (
    "你是严格的答案质检员。请对照【用户问题】【参考资料】【各子任务结论】审查【待审答案】。\n"
    "判定标准：是否直接回答了问题、是否只依据参考资料、关键数字/名称/日期是否与资料一致、"
    "是否有遗漏或编造。\n\n"
    '只输出 JSON，不要任何解释，形如：{"verdict": "ok", "issues": []} 或 '
    '{"verdict": "revise", "issues": ["问题1", "问题2"]}\n'
    "若答案已足够好就输出 ok（issues 可为空）；只有确实需要改动时才输出 revise。"
)


# 【这个函数在干什么】把多个子任务命中的来源合并去重（按 doc_id 判重），并保持首次出现的顺序。
# 【为什么需要】每个子任务都会各自检索、各自命中一些来源，多个子任务之间很可能命中同一份文件；
#   如果不去重，参考来源卡片里会出现重复项，[参考资料N] 的编号也会乱。
# 【参数逐个解释】
#   · sources —— 累积起来的所有来源字典组成的列表（每个字典至少含 doc_id 字段）。
# 【返回值】去重后的新列表。
# 【为什么这么写（关键设计）】
#   去重后的顺序，就是前端展示顺序、也是正文里 [参考资料N] 的编号顺序。
#   所以这一步等价于「全局重编号」——必须在 solve 阶段之前做，让正文引用与卡片编号对得上。
# 【一个纯函数（无副作用）的好处】同样的输入必定得到同样的输出，不依赖任何外部状态，
#   因此可以放心在任何地方调用、也确实不需要担心线程安全问题。
def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨步累积的来源去重（按 doc_id），保持首次出现的顺序。

    去重后数组顺序即前端展示与 [参考资料N] 的编号顺序，因此这一步等价于「全局重编号」。
    """
    # 【这行在干什么】建一个"已见过"的集合，用来快速判断某个 doc_id 是否出现过。
    # 【为什么用 set 而不是 list】set（集合）判断"某元素在不在里面"几乎是瞬时完成
    #   （哈希查找），而 list 需要从头挨个比一遍。来源可能很多，用 set 更高效。
    seen: set[str] = set()
    # 【这行在干什么】准备一个空列表，用来装最终结果（保持入队顺序）。
    out: list[dict[str, Any]] = []
    # 【这行在干什么】逐个遍历每一条来源。src 是单个来源字典。
    for src in sources:
        # 【这行在干什么】取出这条来源的 doc_id，转成字符串；取不到就用空串兜底。
        # 【为什么用 str() 包一层】doc_id 有时可能是数字，统一转字符串好比较。
        # 【为什么用 or ""】如果 doc_id 是 None，str(None) 会得到 "None" 这个脏值，
        #   用 or "" 可以让 None 直接变成空串。
        key = str(src.get("doc_id") or "")
        # 【这行在干什么】如果这个 key 非空、且之前见过，就跳过它（这正是去重）。
        # 【为什么要求 key 非空】没有 doc_id 的来源无法判重，宁多留也不误删。
        if key and key in seen:
            continue
        # 【这行在干什么】如果 key 非空，把它登记进"已见过"集合。
        if key:
            seen.add(key)
        # 【这行在干什么】把这条来源追加到结果列表末尾（保持首次出现的顺序）。
        out.append(src)
    # 【这行在干什么】返回去重后的结果列表。
    return out


# 【这个函数在干什么】把多个子任务的检索统计数字合并成一份，供前端 的 retrieval 事件使用。
# 【为什么需要】react 模式下一轮只检索一次，直接取那份统计即可；
#   但深度模式下有 N 个子任务、各自检索过，前端只想要一条汇总统计，所以这里做个合并。
# 【参数逐个解释】
#   · metas       —— 每个子任务各自产生的统计字典组成的列表；
#   · final_count —— 去重后最终的来源条数（由外部算好传进来）；
#   · s           —— 本次请求的配置对象（要读 llm_configured 等字段）。
# 【返回值】合并后的一本统计字典。
# 【注意事项】这里不做字段平均值之类的高级统计，只用「求和 + 取最后一个」这种简单规则，
#   因为前端只关心"大概检索了多少、有没有重排"。
def _merge_meta(metas: list[dict[str, Any]], final_count: int, s: Settings) -> dict[str, Any]:
    """把多个子任务的检索统计合并成一份，供 ``retrieval`` 事件使用。"""
    # 【这行在干什么】把所有子任务的「稠密检索条数」加起来。
    # 【为什么用 int(... or 0)】统计字典里该字段可能是 None 或缺失，先兜底成 0 再转整数，避免报错。
    dense = sum(int(m.get("dense_count") or 0) for m in metas)
    # 【这行在干什么】把所有子任务的「稀疏检索（BM25）条数」加起来，规则同上。
    sparse = sum(int(m.get("sparse_count") or 0) for m in metas)
    # 【这行在干什么】取最后一个子任务的统计字典（用来代表"当前环境状态"）。
    # 【为什么取最后一个】像 bm25_ready 这种"环境是否就绪"的开关，各步都一样，
    #   取最后一个即可；若列表为空则退回空字典 {}，防止下面取值报错。
    last = metas[-1] if metas else {}
    # 【这行在干什么】拼出一本汇总统计字典并返回。
    return {
        # 稠密检索总条数
        "dense_count": dense,
        # 稀疏检索总条数
        "sparse_count": sparse,
        # BM25 是否就绪（取最后一步的状态，转成布尔值）
        "bm25_ready": bool(last.get("bm25_ready")),
        # 最终（去重后）来源条数
        "final_count": final_count,
        # 大模型是否已配置（决定前端是否显示"仅检索"提示）
        "llm_configured": s.llm_configured,
        # 本次深度模式一共跑了几个子任务
        "steps": len(metas),
        # 逐步执行时不做单轮重排，这里如实标注，避免前端误显示「已重排」
        "rerank_applied": False,
    }


# 【这个函数在干什么】把「规划阶段」要发给模型的消息拼好（系统提示词 + 用户问题）。
# 【为什么单独抽成一个函数】消息怎么拼是「协议细节」，单独集中一处，将来要改只改这里。
# 【参数逐个解释】
#   · question —— 用户的原始问题；
#   · s        —— 配置对象（要读 deep_max_steps 决定"最多拆几步"）。
# 【返回值】一个消息列表（每条是 {"role": ..., "content": ...} 格式）。
def _plan_messages(question: str, s: Settings) -> list[dict[str, str]]:
    return [
        # 【这行在干什么】系统消息：告诉模型它是规划器，并把"最多几步"填进提示词的占位符。
        # 【为什么用 max(2, s.deep_max_steps)】提示词里写的是"拆成 2~N 个子任务"，
        #   所以 N 至少要 2 才符合语义；即使配置写成 1（或 0），也兜到 2，避免出现"2~1"这种怪话。
        {"role": "system", "content": _PLAN_SYSTEM_PROMPT.format(max_steps=max(2, s.deep_max_steps))},
        # 【这行在干什么】用户消息：把原始问题原样交给模型。
        # 【为什么加 f 前缀】f 开头的字符串里 {question} 会被替换成变量的值（这叫 f-string 格式化）。
        {"role": "user", "content": f"【用户问题】\n{question}"},
    ]


# 【这个函数在干什么】解析「规划阶段」模型返回的 JSON，抽出子任务列表。
# 【核心思路（容错优先）】模型不一定听话，可能返回格式不对、字段名不对、甚至吐出废话。
#   所以这里对任何解析失败都**退化成「单个子任务 = 原问题」**，保证流程能继续跑下去，绝不中断。
# 【参数逐个解释】
#   · raw        —— 模型返回的原始文本（可能是 JSON，也可能夹带别的东西）；
#   · question   —— 用户原始问题（用于兜底）；
#   · max_steps  —— 最多允许几个子任务（超出就截断，防止模型列了 20 步把预算烧光）。
# 【返回值】子任务字典组成的列表，每个形如 {"title": 标题, "detail": 具体要做什么}。
def _parse_plan(raw: str, question: str, max_steps: int) -> list[dict[str, str]]:
    """解析规划 JSON。任何解析失败都退化为「单个子任务 = 原问题」。"""
    # 【这行在干什么】先准备好"兜底方案"：一个子任务，标题叫「直接作答」，内容就是原问题。
    # 【为什么带 fallback=True 标记】这个标记会一路传到事件里，前端据此知道
    #   "这次是降级（规划失败）"，可以做一个提示。真实的规划结果不会有这个标记。
    fallback = [{"title": "直接作答", "detail": question, "fallback": True}]
    # 【这行在干什么】调用容错函数 extract_json，从模型回复里抠出 JSON 并解析成 Python 对象。
    # 【注意事项】extract_json 解析失败时通常返回 None（或空），下面会据此走兜底。
    data = extract_json(raw)
    # 【这行在干什么】先声明一个变量 steps_raw，表示"还没找到子任务数组"。
    # 【为什么标注 Any】它可能是列表、字符串或 None，形态不确定，用 Any 兜住。
    steps_raw: Any = None
    # 【这行在干什么】判断解析出来的 data 是不是字典（形如 {"steps": [...]}）。
    if isinstance(data, dict):
        # 【这行在干什么】从字典里找子任务数组，依次尝试三个可能的字段名：steps / plan / tasks。
        # 【为什么试多个名字】不同模型对字段名的习惯不一样；用 or 串联，谁先有值就用谁。
        steps_raw = data.get("steps") or data.get("plan") or data.get("tasks")
    # 【这行在干什么】如果 data 本身就是个列表（形如 [...]），那它直接就是子任务数组。
    elif isinstance(data, list):
        steps_raw = data
    # 【这行在干什么】如果最终找到的东西不是列表（可能是 None、字符串、数字等），
    #   说明格式完全不认识，直接返回兜底方案。
    if not isinstance(steps_raw, list):
        return fallback

    # 【这行在干什么】准备一个空列表，装"规范化之后"的子任务。
    steps: list[dict[str, str]] = []
    # 【这行在干什么】逐个处理原始数组里的每一项（每项可能是字符串，也可能是字典）。
    for item in steps_raw:
        # 【这行在干什么】情形一：这一项直接就是个字符串 —— 标题和详情都用它。
        if isinstance(item, str):
            # 【这行在干什么】strip() 去掉首尾空白字符；标题与详情都设为这段文字。
            title, detail = item.strip(), item.strip()
        # 【这行在干什么】情形二：这一项是字典 —— 从若干个可能的键名里取值。
        elif isinstance(item, dict):
            # 【这行在干什么】取标题：依次尝试 title / task / name，取不到就用空串。
            # 【为什么套 str(... or "")】值可能是 None 或数字，先兜底成空串再转字符串，最后 strip 去空白。
            title = str(item.get("title") or item.get("task") or item.get("name") or "").strip()
            # 【这行在干什么】取详情：依次尝试 detail / description / desc，都取不到就用标题顶上。
            detail = str(item.get("detail") or item.get("description") or item.get("desc") or title).strip()
        # 【这行在干什么】情形三：其它类型（数字、列表、None 等）—— 认不出来，跳过这一项。
        else:
            continue
        # 【这行在干什么】如果标题和详情都是空的，说明这一项没意义，跳过。
        if not title and not detail:
            continue
        # 【这行在干什么】把这一项规范化后加进结果列表。
        # 【参数逐个解释】
        #   · (title or detail) —— 标题为空就用详情顶替（保证标题不为空）；
        #   · [:40]             —— 标题最多保留 40 个字符，防止模型写一大段当标题；
        #   · detail or title   —— 详情为空就用标题顶替（保证详情不为空）。
        steps.append({"title": (title or detail)[:40], "detail": detail or title})
        # 【这行在干什么】如果已经收集到 max_steps 个子任务，就停下（不再往后看）。
        # 【为什么限制】防止模型一口气列 10 个步骤，把时间和 token 预算烧光。
        if len(steps) >= max_steps:
            break
    # 【这行在干什么】返回结果：如果解析出了子任务就用它们，否则（空列表）用兜底方案。
    # 【为什么用 or】空列表在布尔判断里是"假"，所以 steps 为空时会返回 fallback。
    return steps or fallback


# 【这个函数在干什么】给某个子任务生成一句"指令文本"，作为这一轮 ReAct 的输入。
# 【为什么需要】ReAct agent 每一轮都要收到一段"用户消息"；深度模式下，
#   这段消息不是原始问题，而是"总目标 + 当前子任务 + 请只做这一步"。
# 【参数逐个解释】
#   · question —— 用户原始问题（作为"总目标"，给模型全局背景，防止它跑偏）；
#   · step     —— 当前子任务字典；
#   · index    —— 当前是第几步（从 1 开始）；
#   · total    —— 一共几步。
# 【返回值】拼好的指令文本。
def _step_instruction(question: str, step: dict[str, str], index: int, total: int) -> str:
    return (
        # 【这行在干什么】先交代总目标 + 一个空行（\n\n）。让模型知道整件事的大背景。
        f"【总目标】{question}\n\n"
        # 【这行在干什么】交代"当前是第几步/共几步"和这一步的具体要求。
        # 【为什么写 {index}/{total}】让模型有"我在流程中的位置"感，也便于它判断最后一步该收尾。
        f"【当前子任务 {index}/{total}】{step['title']}\n{step['detail']}\n\n"
        # 【这行在干什么】强调"只做这一步、只调必要的工具"。
        "请只针对当前子任务执行必要的工具调用，然后用简洁的中文给出该子任务的结论。"
        # 【这行在干什么】再强调"这只是中间结论，别写成最终长答案，也别重复总目标"。
        # 【为什么反复强调】模型很容易把子任务当最终问题，写出一大篇；那样既浪费 token，
        #   又会让"观察结果"变得很长（虽然有截断保护，但仍希望从源头控制住）。
        "这只是一步中间结论，不要展开成最终的长答案，也不要重复总目标。"
    )


# 【这个函数在干什么】拼出「solve（综合成答案）」阶段要发给模型的消息。
# 【为什么需要】solve 阶段要把前面所有材料汇总起来：原问题 + 计划 + 各步结论 + 参考资料，
#   再让模型写一份完整的最终答案。
# 【参数逐个解释】
#   · question     —— 用户原始问题；
#   · steps        —— 子任务列表（用于渲染"执行计划"）；
#   · observations —— 每个子任务产出的结论文本列表（顺序与 steps 对应）；
#   · sources      —— 去重后的参考来源列表。
# 【返回值】消息列表（系统提示词 + 用户消息）。
def _solve_messages(
    question: str,
    steps: list[dict[str, str]],
    observations: list[str],
    sources: list[dict[str, Any]],
) -> list[dict[str, str]]:
    # 【这行在干什么】把子任务列表渲染成带序号的多行文本，形如「1. 标题：详情」。
    # 【参数逐个解释】
    #   · enumerate(steps, start=1) —— 遍历时同时拿到"序号"和"元素"，序号从 1 开始；
    #   · f"{i}. {st['title']}：{st['detail']}" —— 拼成"序号. 标题：详情"；
    #   · "\n".join(...) —— 用换行符把这些行"缝"成一整段文字。
    plan_text = "\n".join(f"{i}. {st['title']}：{st['detail']}" for i, st in enumerate(steps, start=1))
    # 【这行在干什么】把各子任务的结论渲染成一段文本，形如「【子任务1结论】…… 空行 【子任务2结论】……」。
    obs_text = (
        # 【这行在干什么】各结论之间用两个换行分隔（空一行），读起来更清晰。
        "\n\n".join(f"【子任务{i}结论】\n{obs}" for i, obs in enumerate(observations, start=1))
        # 【这行在干什么】如果压根没有结论（join 出来是空串），就显示「（无）」占位。
        # 【为什么用 or】空字符串是"假"，所以此时返回「（无）」；这样提示词里不会出现空白段落。
        or "（无）"
    )
    # 【这行在干什么】把来源列表渲染成「参考资料」文本块；若没有来源则显示「（无）」。
    # 【为什么复用 rag.render_reference_blocks】保证与 react 模式下的参考资料格式完全一致，
    #   这样模型对 "参考资料N" 的理解也一致。
    context = render_reference_blocks(sources) or "（无）"
    # 【这行在干什么】拼出给模型的用户消息正文（把所有材料按段落组织起来）。
    user_content = (
        # 原问题
        f"【用户问题】\n{question}\n\n"
        # 执行计划（让模型知道"我们打算分几步做"）
        f"【执行计划】\n{plan_text}\n\n"
        # 各子任务的结论（真正的"素材"）
        f"【各子任务结论】\n{obs_text}\n\n"
        # 参考资料原文（要求模型只依据它作答）
        f"【参考资料】\n{context}\n\n"
        # 最后一句是"硬要求"：综合出完整答案、只依据参考资料、引用要标 [参考资料N]、不许编造。
        "请综合上述信息，给出完整、准确、结构化的最终回答。"
        "严格依据参考资料，引用处标注 [参考资料N]，不要编造任何资料之外的信息。"
    )
    # 【这行在干什么】返回两条消息：系统人设（复用 rag 的 SYSTEM_PROMPT）+ 上面拼好的用户消息。
    # 【为什么系统提示词也复用 rag 的】保证三种模式的"人设与规矩"完全统一，不会各说各的。
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# 【这个函数在干什么】拼出「自评（reflect）」阶段要发给模型的消息。
# 【为什么需要】deep 模式要审查已经生成的答案，需要把"问题 + 各步结论 + 参考资料 + 待审答案"
#   一起给模型，它才好判断答案对不对、有没有编造。
# 【参数逐个解释】
#   · question     —— 用户原始问题；
#   · answer       —— 待审查的答案（就是 solve 阶段产出的那份）；
#   · observations —— 各子任务结论；
#   · sources      —— 参考来源。
# 【返回值】消息列表（自评系统提示词 + 用户消息）。
def _reflect_messages(
    question: str,
    answer: str,
    observations: list[str],
    sources: list[dict[str, Any]],
) -> list[dict[str, str]]:
    # 【这行在干什么】渲染参考资料块，没有就显示「（无）」。
    context = render_reference_blocks(sources) or "（无）"
    # 【这行在干什么】渲染各子任务结论，没有就显示「（无）」。
    obs_text = (
        "\n\n".join(f"【子任务{i}结论】\n{obs}" for i, obs in enumerate(observations, start=1))
        or "（无）"
    )
    # 【这行在干什么】拼出自评用的用户消息正文。
    user_content = (
        # 原问题
        f"【用户问题】\n{question}\n\n"
        # 各子任务结论（对照材料之一）
        f"【各子任务结论】\n{obs_text}\n\n"
        # 参考资料原文（对照材料之二，也是"不许编造"的基准）
        f"【参考资料】\n{context}\n\n"
        # 待审答案（就是要被挑毛病的对象）
        f"【待审答案】\n{answer}\n\n"
        # 收尾指令：只输出 JSON 判定
        "请审查上述待审答案，只输出 JSON 判定。"
    )
    # 【这行在干什么】返回两条消息：自评系统提示词 + 用户消息。
    return [
        {"role": "system", "content": _REFLECT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# 【这个函数在干什么】解析「自评」模型返回的 JSON，得到"要不要改、哪里要改"。
# 【核心思路（同样是容错优先）】解析不出来时，一律当作「无需修订」，
#   因为"误改"比"不改"风险更大（可能把本来对的答案改坏），而且还能省下一次生成的钱。
# 【参数逐个解释】
#   · raw —— 模型返回的原始文本。
# 【返回值】形如 {"verdict": "ok"/"revise", "issues": [问题描述, ...]} 的字典。
def _parse_reflection(raw: str) -> dict[str, Any]:
    """解析自评 JSON。解析不出来一律当「无需修订」，避免无谓的二次生成。"""
    # 【这行在干什么】抠出 JSON 并解析。
    data = extract_json(raw)
    # 【这行在干什么】如果解析结果不是字典（模型没按格式来），直接返回"无需修订"。
    if not isinstance(data, dict):
        return {"verdict": "ok", "issues": []}
    # 【这行在干什么】取判定结论：依次尝试 verdict / result 两个键，取不到用空串。
    # 【为什么 .strip().lower()】去掉首尾空白 + 转成小写，这样 "OK"、"Revise " 也能正确识别。
    verdict = str(data.get("verdict") or data.get("result") or "").strip().lower()
    # 【这行在干什么】取"问题列表"：依次尝试 issues / problems，没有就用空列表。
    issues_raw = data.get("issues") or data.get("problems") or []
    # 【这行在干什么】情形一：问题列表是个字符串（模型只写了一句话）—— 包成单元素列表。
    if isinstance(issues_raw, str):
        issues = [issues_raw]
    # 【这行在干什么】情形二：问题列表本来就是列表 —— 逐项转字符串、去空白、丢掉空项。
    elif isinstance(issues_raw, list):
        issues = [str(x).strip() for x in issues_raw if str(x).strip()]
    # 【这行在干什么】情形三：其它类型（数字、字典等）—— 认不出来，当作没有问题。
    else:
        issues = []
    # 【这行在干什么】如果 verdict 不是我们认识的 "ok"/"revise"，就靠"有没有问题"来推断：
    #   有问题就当 revise，没问题就当 ok。这样即便模型乱写判定词，也能得到合理结果。
    if verdict not in ("ok", "revise"):
        verdict = "revise" if issues else "ok"
    # 【这行在干什么】返回结果，并把问题列表截断到最多 5 条。
    # 【为什么截断】问题太多会把"修订提示词"撑得很长、也可能让模型无从下手；
    #   取前 5 条最关键的就够了。
    return {"verdict": verdict, "issues": issues[:5]}


# 【这个函数在干什么】生成"模型不可用/零输出"时的兜底答案。
# 【为什么需要】solve 阶段如果模型一个字符都没吐（超时或异常），不能让用户看到空白，
#   要退回"仅检索"的答案（把命中的参考资料原文摆出来），保证体验不比 react 模式差。
# 【参数逐个解释】
#   · sources —— 命中的参考来源列表。
# 【返回值】兜底答案字符串。
def _fallback_answer(sources: list[dict[str, Any]]) -> str:
    # 【这行在干什么】三目运算符：有来源就用"仅检索答案"，没有来源就用"无数据"话术。
    # 【语法说明】`A if 条件 else B` 读作"条件成立就用 A，否则用 B"，等价于一个简写的 if-else。
    return retrieval_only_answer(sources) if sources else NO_DATA_MESSAGE


# 【这个函数在干什么】深度模式的主入口 —— 一个「异步生成器」，按顺序吐出整个流程的所有事件。
# 【什么是异步生成器（大白话）】普通函数 return 一个结果就结束了；
#   生成器可以"吐一个、停一下、再吐一个"（用 yield），调用方能边收边处理；
#   加上 async 之后，它在"停一下"的时候还能去干别的（比如等网络），这就是异步生成器。
#   前端拿到它后，用 async for 一段段取出事件，实时更新界面。
# 【为什么是生成器而不是一次性返回整个结果】因为要"边执行边推送"——
#   每执行一步就立刻告诉前端（进度、工具调用、正文），用户不用干等到最后。
# 【参数逐个解释】
#   · question   —— 用户问题；
#   · settings   —— 配置对象；不传就现取（None 表示"用默认配置"）；
#   · final_k    —— 最终要保留几条来源（传给检索，None 表示用配置默认值）；
#   · mode       —— 模式："plan" 或 "deep"（其它值一律当 "plan" 处理）；
#   · web_search —— 是否启用联网搜索；None 表示"用配置里的默认值"。
# 【返回值】一个异步迭代器，逐个产出事件字典。
async def run_deep_events(
    question: str,
    settings: Settings | None = None,
    final_k: int | None = None,
    mode: str = "plan",
    web_search: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Plan-and-Solve（+ Reflection）事件流。

    事件协议在既有类型之外新增 ``plan`` / ``plan_step`` / ``reflection``，
    并给 ``delta`` 增加 ``channel``（``answer`` / ``revision``，缺省 ``answer``）。
    """
    # 【这行在干什么】确定配置对象：调用方传了就用它，没传（None）就现取一份默认配置。
    # 【为什么允许传参】测试时可以把配置"换掉"，不用去改 .env，方便又安全。
    s = settings or get_settings()
    # 【这行在干什么】把 mode 规范化：只有明确是 "deep" 才算 deep，其它任何值都当 "plan"。
    # 【为什么这么写】对输入做"白名单式"规整，防止前端传了意外值导致后面判断走偏。
    mode = "deep" if mode == "deep" else "plan"
    # 【这行在干什么】根据模式决定"结束时"上报的模式名（done 事件里带，前端据此判断）。
    # 【为什么用两个不同的名字】让前端能区分"这轮到底跑的是 plan 还是 deep"，
    #   好在历史记录里准确标注。
    done_mode = "agent_deep" if mode == "deep" else "agent_plan"
    # 【这行在干什么】记下开始时刻（单调递增秒数），用于后面判断是否超时。
    started = time.monotonic()

    # 【这个内层函数在干什么】判断"是否已超出总时限"。
    # 【为什么定义成内层函数】它要读外层的 started 和 s，定义在这里最方便（这叫闭包：
    #   内层函数可以直接用到外层函数的变量）。
    def over_budget() -> bool:
        # 【这行在干什么】规则：如果配置了正的总时限（>0），且已经用掉的时间超过它，就算超时。
        # 【为什么 monotonic() - started】两者都是单调秒数，相减就是"已经过去了多少秒"。
        return s.deep_timeout > 0 and (time.monotonic() - started) > s.deep_timeout

    # 【这行在干什么】决定本轮是否联网搜索：调用方没指定（None）就用配置默认值，否则用调用方给的。
    use_web = s.web_search_default if web_search is None else bool(web_search)
    # 【这行在干什么】再与"是否配置好了联网搜索"取与：即使要求联网，但没配密钥也不能真联网。
    # 【为什么用 bool(... and ...)】把结果规整成明确的 True/False，便于后面传参。
    use_web = bool(use_web and s.web_search_configured)

    # --- 阶段 1：规划 ------------------------------------------------------- #
    # 【这行在干什么】先给 steps 一个"兜底值"：万一规划阶段出任何意外，就按"单步=原问题"来跑。
    # 【为什么先给兜底值】保证后面代码始终拿到一个可用的 steps 列表，不用到处判空。
    steps: list[dict[str, str]] = [{"title": "直接作答", "detail": question}]
    # 【这行在干什么】用 plan_fallback 记录"本轮计划是不是降级来的"，默认先当作 True（降级）。
    # 【为什么默认 True】只有真正成功解析出模型计划后，才把它改成 False；这样"出错"的默认值更安全。
    plan_fallback = True
    # 【下面这段在干什么】用 try 包住规划调用 —— 规划失败绝不能连累整轮对话。
    try:
        # 【这行在干什么】调用模型产出规划（要完整 JSON，所以用"一次性调用"而不是流式）。
        # 【为什么用 asyncio.to_thread 包一层】complete_chat 是"同步阻塞"的（调用期间会一直等），
        #   直接 await 会卡住整个服务的事件循环；把它丢到旁支线程去跑，
        #   等它的时候本协程就能去处理别的请求（这叫"不阻塞事件循环"）。
        # 【参数逐个解释】
        #   · _plan_messages(...)      —— 拼好的"规划提示词"消息；
        #   · s                        —— 配置（里面含 API 密钥、地址等）；
        #   · s.agent_model_or_default —— 用哪个模型（没显式配就用默认）；
        #   · 0.0                      —— 温度（temperature），0 表示"尽量稳定、别发挥"，规划要的是规矩；
        #   · s.deep_max_tokens        —— 本次最多生成多少 token（要够长才能装下 JSON 计划）；
        #   · s.deep_timeout           —— 本次调用的超时时间（秒）。
        raw_plan = await asyncio.to_thread(
            complete_chat,
            _plan_messages(question, s),
            s,
            s.agent_model_or_default,
            0.0,
            s.deep_max_tokens,
            s.deep_timeout,
        )
        # 【这行在干什么】把模型返回的原始文本解析成子任务列表。
        # 【为什么用 max(1, s.deep_max_steps)】传给解析函数的"上限"至少要是 1，
        #   否则模型给了子任务也会被截成空；这里兜底到 1 更稳。
        parsed = _parse_plan(raw_plan, question, max(1, s.deep_max_steps))
        # 【这行在干什么】判断解析结果是不是"降级方案"（看第一个子任务有没有 fallback 标记）。
        # 【为什么 parsed and ...】先确保 parsed 非空再取第一项，防止空列表取值报错。
        plan_fallback = bool(parsed and parsed[0].get("fallback"))
        # 【这行在干什么】把解析出的子任务列表赋值给 steps，供后续逐步执行使用。
        steps = parsed
    # 【下面这段在干什么】捕获"规划阶段"发生的任何异常，并降级处理。
    # 【为什么抓这么宽（Exception）】规划是"锦上添花"的一步，它失败不该影响主流程；
    #   所以宁可全部接住、退化成单步，也不让整轮对话挂掉。
    # 【noqa: BLE001 是啥】这是给代码检查工具看的"忽略提示"，意思是"我知道这里抓得太宽，是有意为之"。
    except Exception:  # noqa: BLE001 - 规划失败不应中断整轮，退化为单步
        # 【这行在干什么】退化成"单步=原问题"，也就是"不拆了，直接当一个问题处理"。
        steps = [{"title": "直接作答", "detail": question}]

    # 【这行在干什么】向前端推送一个 plan 事件，把"计划"告诉用户（好让界面显示计划卡片）。
    yield {
        "type": "plan",
        # 【这行在干什么】把每个子任务加工成带序号的形式（index 从 1 开始），方便前端展示。
        # 【为什么重新组织成列表推导】原始字典里可能多带字段，这里只挑出前端要的三个，接口更干净。
        "steps": [
            {"index": i, "title": st["title"], "detail": st["detail"]}
            for i, st in enumerate(steps, start=1)
        ],
        # 【这行在干什么】告诉前端"这份计划是不是降级来的"（降级则前端可做提示）。
        "fallback": plan_fallback,
    }

    # --- 阶段 2：逐步执行 --------------------------------------------------- #
    # 【这行在干什么】新建"本次请求共享的状态字典组"（存检索结果、来源、统计等）。
    # 【为什么叫"共享"】整轮请求里所有子任务都往这同一组字典里写，最后一次性汇总；
    #   这也是"子任务必须串行"的原因（同时跑就会互相踩状态）。
    states = _new_states(s, final_k)
    # 【这行在干什么】准备一个列表，收集每个子任务的"结论文字"。
    observations: list[str] = []
    # 【这行在干什么】准备一个列表，收集各子任务命中的所有来源（稍后统一去重）。
    collected: list[dict[str, Any]] = []
    # 【这行在干什么】准备一个列表，收集各子任务的检索统计（稍后合并成一份）。
    metas: list[dict[str, Any]] = []

    # 【下面这段在干什么】组装 ReAct agent —— 万一失败，要立刻告诉前端并停止。
    try:
        # 【这行在干什么】组装 agent（模型 + 工具 + 提示词），并告知"本轮是否启用联网搜索工具"。
        # 【为什么单独 try】如果这里就失败（比如模型名写错了），后面所有步骤都没法跑，
        #   不如就地报错、干净退出。
        agent = _build_agent(s, web_search_enabled=use_web)
    # 【下面这段在干什么】捕获组装失败并报错退出。
    except Exception as exc:  # noqa: BLE001
        # 【这行在干什么】推一个 error 事件，把失败原因告诉前端（f"{exc}" 是异常的文字描述）。
        yield {"type": "error", "message": f"深度模式初始化失败：{exc}"}
        # 【这行在干什么】推一个 done 事件，模式标记为 agent_failed（表示"这轮失败了"）。
        # 【为什么用 agent_failed】与项目既有的失败语义保持一致，前端能统一处理。
        yield {"type": "done", "mode": "agent_failed"}
        # 【这行在干什么】直接 return，结束这个生成器（后面不再执行）。
        return

    # 【这行在干什么】逐个执行子任务（enumerate 从 1 开始，i 是序号，step 是子任务字典）。
    # 【为什么必须"串行"（一个接一个）】所有子任务共用同一组状态字典 states；
    #   要是并发跑，两个子任务会同时往同一组字典里写，结果互相覆盖、彻底乱套。
    for i, step in enumerate(steps, start=1):
        # 【这行在干什么】推一个 plan_step 事件，告诉前端"第 i 步开始跑了"。
        yield {
            "type": "plan_step",
            "index": i,
            "title": step["title"],
            # 【这行在干什么】status 用 "running" 表示"正在执行"，前端据此显示转圈动画。
            "status": "running",
            # 【这行在干什么】此时结论还没有，先填 None。
            "observation": None,
            # 【这行在干什么】此时也还没有错误，先填 None。
            "error": None,
        }

        # 【这行在干什么】检查是否已超总时限。
        if over_budget():
            # 【下面这段在干什么】总时限已到：不再继续烧 token，把这个步骤标记成失败。
            # 【为什么这么做】深度模式可能很贵；到点就该止损，而不是硬跑完。
            # 总时限已到：不再继续烧 token，剩余步骤直接标记失败
            yield {
                "type": "plan_step",
                "index": i,
                "title": step["title"],
                # 【这行在干什么】status 用 "failed" 表示这一步没做成。
                "status": "failed",
                "observation": None,
                # 【这行在干什么】给出失败原因（超时），前端可显示给用户看。
                "error": f"总时限 {s.deep_timeout}s 已到，跳过该步骤",
            }
            # 【这行在干什么】给这一步补一条"占位结论"，让 observations 的长度与 steps 保持对齐。
            # 【为什么必须对齐】solve 阶段是按序号把"子任务"和"结论"配对展示的（子任务i结论），
            #   如果不补占位，后面的编号就会错位。
            observations.append("（该步骤因超时被跳过）")
            # 【这行在干什么】跳过本步剩下的所有处理，直接进入下一个子任务。
            continue

        # 【这行在干什么】为这一轮新建"轮状态"（一个子任务算一轮）。
        # 【为什么要新建】每轮 reset 一份，避免上一轮的中间状态串到这一轮。
        turn = _new_turn_state()
        # 【这行在干什么】准备一个列表，收集本轮模型吐出的正文片段（稍后拼成"观察结果"）。
        parts: list[str] = []
        # 【这行在干什么】准备一个变量记录本步的错误信息，先当作"没有错误"（None）。
        step_error: str | None = None
        # 【下面这段在干什么】驱动这一轮 ReAct 执行（这是本文件最关键的复用点）。
        try:
            # 【这行在干什么】用 async for 一段段取出这一轮产生的事件。
            # 【为什么直接复用 _stream_react_turn】把"驱动一轮 ReAct"的公共逻辑集中在 agent.py，
            #   本文件只做"编排"，避免两处各写一份、日后逐渐不一致（这就是 docstring 里说的"消除代价"）。
            # 【参数逐个解释】
            #   · agent       —— 前面组装好的 ReAct agent；
            #   · {"messages": [HumanMessage(...)]} —— 本轮的输入消息（即那条"子任务指令"）；
            #   · turn        —— 本轮的轮状态；
            #   · states      —— 整轮共享的状态字典组（工具把检索结果写进这里）；
            #   · question=question —— 原始问题（供内部记录/上下文使用）；
            #   · recursion_limit=max(1, s.deep_step_max_steps) —— 本步允许"想-调用-再想"的次数上限
            #       （至少 1；配置太小会让多工具调用报 Recursion limit 错，本项目踩过坑）；
            #   · emit_sources=False —— 本步**不**直接发来源事件（来源要等 solve 前统一去重再发）；
            #   · emit_delta=False   —— 本步**不**把正文当答案 delta 冒泡（正文只留作"观察结果"）。
            async for event in _stream_react_turn(
                agent,
                {"messages": [HumanMessage(content=_step_instruction(question, step, i, len(steps)))]},
                turn,
                states,
                question=question,
                recursion_limit=max(1, s.deep_step_max_steps),
                emit_sources=False,  # 来源等 solve 前全局重编号后一次发出
                emit_delta=False,  # 子任务正文只当观察结果，不冒泡成答案
            ):
                # 【这行在干什么】取出这个事件的类型（type 字段）。
                etype = event.get("type")
                # 【这行在干什么】情形一：这是"本步正文"事件。
                # 【为什么会有 step_text】因为我们上面设了 emit_delta=False，
                #   _stream_react_turn 不会把它当答案推，而是改用 step_text 类型回传，
                #   正好让我们在这里接住、收集为"观察结果"。
                if etype == "step_text":
                    # 【这行在干什么】把这段文字追加进 parts（转成字符串，防止收到 None）。
                    # 子任务正文（emit_delta=False 时由 _stream_react_turn 以该类型回传）
                    parts.append(str(event.get("text") or ""))
                # 【这行在干什么】情形二：这是"工具相关"的事件（调用、结果、SQL、图谱、联网搜索）。
                # 【为什么直接转发】这些信息对用户有参考价值（能看到 Agent 正在查什么），
                #   而且不影响 token 分流（它们不是正文），所以原样透传给前端。
                elif etype in ("tool_call", "tool_result", "sql_query", "graph_query", "web_search"):
                    # 让用户看得到 Agent 正在调什么工具、查到了什么
                    yield event
        # 【下面这段在干什么】捕获单步执行失败。
        # 【为什么只记录不抛出】一个子任务失败不该让整轮报废；记下来，继续跑后续步骤。
        except Exception as exc:  # noqa: BLE001 - 单步失败只标记该步，不中断整轮
            # 【这行在干什么】把错误信息（转成字符串）记到 step_error 里，稍后写进事件。
            step_error = str(exc)

        # 【这行在干什么】从共享状态里"快照"出本步的检索结果（来源 + 统计）。
        # 【参数逐个解释】states["_rag_state"] 是专门存检索信息的子字典；快照=取一份当前副本。
        snapshot = _snapshot_rag_state(states["_rag_state"])
        # 【这行在干什么】如果本步命中了来源，就把它们并进"总收集列表"。
        if snapshot["sources"]:
            collected.extend(snapshot["sources"])
        # 【这行在干什么】如果本步有统计信息，就把它们并进"统计列表"。
        if snapshot["meta"]:
            metas.append(snapshot["meta"])

        # 【这行在干什么】把本步正文拼成一句话作为"观察结果"，并截断到配置的最大字符数。
        # 【参数逐个解释】
        #   · "".join(parts)          —— 把正文片段拼成一整段；
        #   · .split()                —— 按空白切开（会把连续的空白/换行都处理掉）；
        #   · " ".join(...)           —— 再用单个空格缝回去，相当于"压平多余空白"；
        #   · [: max(0, s.deep_observation_chars)] —— 只保留前 N 个字符；
        #     用 max(0, ...) 是防止配置写成负数导致切片报错。
        # 【为什么"压平 + 截断"】观察结果会长得很快；压平让格式干净，
        #   截断防止把 solve 阶段的上下文撑爆（这是 docstring 里说的 DEEP_OBSERVATION_CHARS）。
        observation = " ".join("".join(parts).split())[: max(0, s.deep_observation_chars)]
        # 【这行在干什么】如果这步报错了、而且没有任何正文产出，就把错误信息当作"观察结果"。
        # 【为什么要这一步】保证 solve 阶段能看到"这步失败了"，而不是一片空白、莫名其妙。
        if step_error and not observation:
            observation = f"（该步骤执行失败：{step_error}）"
        # 【这行在干什么】把本步结论加进结论列表；若连错误信息都没有（纯空），就填一句占位说明。
        observations.append(observation or "（该步骤没有产出结论）")

        # 【这行在干什么】推一个 plan_step 事件，告诉前端"第 i 步结束了，结论是什么"。
        yield {
            "type": "plan_step",
            "index": i,
            "title": step["title"],
            # 【这行在干什么】决定状态：报错且没有任何正文 → failed；否则 → done。
            # 【为什么还要看 parts】如果报错了但已经产出了正文，说明基本跑通了，仍算 done。
            "status": "failed" if step_error and not parts else "done",
            # 【这行在干什么】observations[-1] 就是刚刚追加的、本步的结论（-1 表示"最后一个"）。
            "observation": observations[-1],
            # 【这行在干什么】把错误信息一并带上（没有则为 None）。
            "error": step_error,
        }

    # solve 前把来源全局去重（等价于重编号），一次性发出
    # 【这行在干什么】把所有子任务命中的来源合并去重（保证 [参考资料N] 编号与卡片顺序一致）。
    sources = _dedupe_sources(collected)
    # 【这行在干什么】推 retrieval 事件，带上合并后的检索统计（前端显示"检索了多少条"）。
    yield {"type": "retrieval", "meta": _merge_meta(metas, len(sources), s)}
    # 【这行在干什么】推 sources 事件，把去重后的全部来源一次性发给前端（渲染来源卡片）。
    # 【为什么在这里发、而不在每一步发】因为要"全局重编号"后再发一次，
    #   否则每步发一次、编号会重复和错乱（所以上面 emit_sources=False）。
    yield {"type": "sources", "sources": sources}

    # --- 阶段 3：综合成答案 ------------------------------------------------- #
    # 【这行在干什么】准备一个列表，收集 solve 阶段模型吐出的答案片段。
    answer_parts: list[str] = []
    # 【下面这段在干什么】流式生成最终答案，并逐字推给前端。
    try:
        # 【这行在干什么】用 async for 一段段取出模型吐出的文字（piece 是一小段文字）。
        # 【参数逐个解释】
        #   · _solve_messages(...)     —— 拼好的 solve 提示词；
        #   · s                        —— 配置；
        #   · model=...                —— 用哪个模型；
        #   · max_tokens=...           —— 本次最多生成多少 token（答案要够长，配小了会被截断）；
        #   · timeout=...              —— 本次调用超时（秒）。
        async for piece in astream_chat(
            _solve_messages(question, steps, observations, sources),
            s,
            model=s.agent_model_or_default,
            max_tokens=s.deep_max_tokens,
            timeout=s.deep_timeout,
        ):
            # 【这行在干什么】把这一小段文字收集起来（最后拼成完整答案，做兜底判断用）。
            answer_parts.append(piece)
            # 【这行在干什么】立刻把这一小段推给前端。
            # 【channel 字段的用途】标明"这是正文"，与后面修订稿的 channel="revision" 区分开，
            #   前端据此把正文和修订稿放进不同的框里显示。
            yield {"type": "delta", "text": piece, "channel": "answer"}
    # 【下面这段在干什么】捕获大模型调用失败（LLMError 是本项目自定义的异常类型）。
    except LLMError as exc:
        # 【这行在干什么】推 error 事件告知前端失败原因；注意不中断流程，后面还有兜底。
        yield {"type": "error", "message": str(exc)}

    # 【这行在干什么】把收集到的片段拼成完整答案，供后续判断和自评使用。
    answer = "".join(answer_parts)
    # 【这行在干什么】如果答案是空的（模型异常或超时导致零输出），就走兜底。
    if not answer.strip():
        # 【这行在干什么】生成兜底答案（有来源就用"仅检索答案"，没有就用"无数据"话术）。
        # solve 零 delta（模型异常/超时）→ 回落「仅检索答案」，绝不比 react 更差
        fallback = _fallback_answer(sources)
        # 【这行在干什么】把兜底答案也作为正文推给前端（不然用户会看到一片空白）。
        yield {"type": "delta", "text": fallback, "channel": "answer"}
        # 【这行在干什么】把 answer 更新为兜底内容，这样后面的自评能拿到一份非空答案。
        answer = fallback

    # --- 阶段 4：自评与修订（仅 deep） -------------------------------------- #
    # 【这行在干什么】三个条件同时满足才做自评：①是 deep 模式；②配置允许至少 1 轮自评；
    #   ③还没超总时限。
    # 【为什么第三个条件也要有】自评很贵，快超时了就别做了，留着预算把主流程跑完。
    if mode == "deep" and s.deep_reflect_rounds > 0 and not over_budget():
        # 【这行在干什么】用 current 记录"当前这一版答案"，它会在修订后更新。
        # 【为什么需要它】多轮自评时，第二轮审的是第一轮修订后的新答案，所以要滚动更新。
        current = answer
        # 【这行在干什么】按配置的轮数循环自评（range(1, N+1) 得到 1..N）。
        for round_no in range(1, s.deep_reflect_rounds + 1):
            # 【这行在干什么】每轮开始前再检查一次是否超时，超了就中断自评循环。
            if over_budget():
                break
            # 【下面这段在干什么】让模型自评，拿到判定结果。
            try:
                # 【这行在干什么】调用模型做自评（要完整 JSON，所以用一次性调用）。
                # 【为什么用 asyncio.to_thread】同 planning：complete_chat 是同步阻塞的，要丢到旁支线程。
                # 【参数逐个解释】
                #   · _reflect_messages(...) —— 拼好的自评提示词；
                #   · 0.0                    —— 温度 0，判定要稳定；
                #   · s.deep_max_tokens      —— 最多生成多少 token（够装下 JSON 判定即可）；
                #   · s.deep_timeout         —— 超时（秒）。
                raw = await asyncio.to_thread(
                    complete_chat,
                    _reflect_messages(question, current, observations, sources),
                    s,
                    s.agent_model_or_default,
                    0.0,
                    s.deep_max_tokens,
                    s.deep_timeout,
                )
                # 【这行在干什么】解析自评结果，得到 {"verdict": ..., "issues": [...]}。
                verdict = _parse_reflection(raw)
            # 【下面这段在干什么】捕获自评失败。
            # 【为什么直接 break】自评失败不影响已经产出的答案；只是"这一轮没法修订"，
            #   所以中断自评循环、把已有答案交给用户即可。
            except LLMError:
                # 自评失败：不影响已产出的答案，只是没有修订
                break

            # 【这行在干什么】判断模型是否要求"修订"（verdict 为 "revise" 才需要）。
            need_revise = verdict["verdict"] == "revise"
            # 【这行在干什么】推 reflection 事件，把自评判定告诉前端（渲染自评卡片）。
            yield {
                "type": "reflection",
                # 【这行在干什么】第几轮自评。
                "round": round_no,
                # 【这行在干什么】判定结论（ok / revise）。
                "verdict": verdict["verdict"],
                # 【这行在干什么】模型列出的问题清单。
                "issues": verdict["issues"],
                # 【这行在干什么】是否真的会提供修订稿（供前端决定是否显示"已修订"区块）。
                "revision_offered": need_revise,
            }
            # 【这行在干什么】如果不需要修订，就结束自评循环（答案已达标）。
            if not need_revise:
                break

            # 【这行在干什么】准备一个列表，收集本轮修订稿的文字片段。
            revision_parts: list[str] = []
            # 【下面这段在干什么】流式生成修订稿，并单独用 channel="revision" 推给前端。
            try:
                # 【这行在干什么】把问题、计划、结论、来源**原样再给一次**，并在末尾追加一句
                #   "上一版答案存在以下问题，请据此重写"——把自评挑出的毛病一并告诉模型。
                # 【参数逐个解释】
                #   · _solve_messages(...) + [ {...} ] —— 基础 solve 消息后面再追加一条用户消息；
                #   · "正文".join(...) —— 把 issues 列表拼成"- 问题1\n- 问题2"这样的要点列表；
                #   · 其余参数与 solve 阶段相同（同模型、同 token 上限、同超时）。
                async for piece in astream_chat(
                    _solve_messages(question, steps, observations, sources)
                    + [
                        {
                            "role": "user",
                            "content": "上一版答案存在以下问题，请据此重写一版更准确的完整答案：\n"
                            + "\n".join(f"- {x}" for x in verdict["issues"]),
                        }
                    ],
                    s,
                    model=s.agent_model_or_default,
                    max_tokens=s.deep_max_tokens,
                    timeout=s.deep_timeout,
                ):
                    # 【这行在干什么】收集这一小段修订文字。
                    revision_parts.append(piece)
                    # 【这行在干什么】推 delta 事件，注意 channel="revision" 与正文区分开。
                    yield {"type": "delta", "text": piece, "channel": "revision"}
            # 【下面这段在干什么】捕获修订过程中大模型失败，报错但不中断。
            except LLMError as exc:
                yield {"type": "error", "message": str(exc)}

            # 【这行在干什么】把修订片段拼成完整的修订稿。
            revised = "".join(revision_parts)
            # 【这行在干什么】如果修订稿非空，就用它更新 current，供下一轮自评继续审查。
            if revised.strip():
                current = revised
            # 【这行在干什么】如果修订稿是空的（模型没吐出东西），就别再继续自评了，直接结束循环。
            else:
                break

    # 【这行在干什么】推 done 事件，宣告整轮结束，并带上模式名（agent_deep / agent_plan）。
    # 【为什么放在最后】done 是"这一轮说完了"的信号，前端收到后就可以收尾（停掉加载动画等）。
    yield {"type": "done", "mode": done_mode}