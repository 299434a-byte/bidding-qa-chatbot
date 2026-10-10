# ============================================================================
# 文件：rag.py —— RAG 问答的「总导演」（RAG = 检索 + 生成）
#
# 【这个文件在整个项目里是干什么的】
#   用户在前端问一个问题（比如「最近有哪些市政道路招标公告？」），问答链路是：
#   检索（retriever 拿候选）→ 重排（rerank 精筛）→ 组织提示词 → 大模型流式作答。
#   本文件就是负责把这三段串起来、并且把「命中的原文」作为参考资料带进答案、
#   还能处理各种异常情况（知识库为空、没配大模型、大模型调用失败）的「总导演」。
#   它最终通过一个生成器 answer_events()，一条一条地「吐出」SSE 事件字典，
#   前端拿到这些事件就能边收边渲染（打字机效果），不用等大模型全部生成完。
#
# 【执行顺序：这个文件要「从上往下」读】
#   ① 顶部常量：SYSTEM_PROMPT（给大模型立规矩）和 NO_DATA_MESSAGE（空库提示文案）；
#   ② render_reference_blocks() —— 把命中原文排版成「参考资料块」；
#   ③ build_messages()          —— 组装发给大模型的对话消息；
#   ④ _preview() / retrieval_only_answer() —— 降级模式的摘要与整段回答；
#   ⑤ answer_events()           —— 主入口生成器，串起上面全部。
#
# 【谁调用它（上游）】
#   backend/app/main.py 的 /api/chat 端点 —— 用户提问时直接驱动 answer_events()。
#   backend/app/agent.py 里的 RAG 工具（search_bidding_knowledge）—— 它内部复用
#   retriever + rerank，组织答案的口径与本文件保持一致（见 render_reference_blocks 注释）。
#
# 【它给谁用（下游）】
#   前端（Next.js）—— 消费 SSE 事件流，展示检索徽标、参考来源卡片、流式回答。
#
# 【先懂的三个概念（大白话）】
#   1) SSE（Server-Sent Events，服务器推送事件）：服务器不一次性给完整答案，
#     而是像水龙头一样「一滴一滴」推送文字片段；前端收到一滴渲染一滴。
#     好处：用户早早就看到字在往外冒，体验上「快」；坏处是中间状态多，要设计事件类型。
#   2) 生成器（generator，带 yield 的函数）：调用它并不会立刻执行函数体，
#     而是得到一个「可迭代对象」。每次 for 循环要一个值，函数就往下跑到 yield
#     就暂停、把值交出来，下次再接着跑。这样就不必把所有结果一次性存进内存。
#   3) 事件字典：本文件每个 yield 出来的都是一个「字典」，用 "type" 键区分类型
#     （retrieval 检索统计 / sources 来源 / delta 正文增量 / done 结束 / error 出错）。
#     前端像一个信差，看见 type 不同就分发给不同地方处理。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring）】
# 【为什么放在这里】注释不算语句，所以放在这一大段注释之后，它依然是本模块的
#   第一条语句、依然是官方自我介绍，内容与原文一字不差。
"""RAG 编排：混合检索 → 大模型重排 → 组织提示词 → 流式生成，并全程携带「参考来源」。

对外只暴露一个生成器 ``answer_events``，逐条产出 SSE 事件字典：

    {"type": "retrieval", "meta": {...}}   检索统计（两路各召回多少、BM25 是否可用、重排是否生效）
    {"type": "sources",   "sources": [...]} 参考来源（命中的公告/项目/公司原文）
    {"type": "delta",     "text": "..."}   回答正文增量
    {"type": "done",      "mode": "..."}   结束（mode 表明是 llm / retrieval_only / no_data）
    {"type": "error",     "message": "..."} 出错
"""

# 【这行在干什么】打开「类型注解延迟求值」开关。
# 【为什么这么写】本文件用了 list[dict[str, Any]]、Settings | None 这类新式标注，
#   老版本 Python（3.8/3.9）不认；加上这行后标注先被当成字符串存着，需要时才解析。
# 【注意事项】必须放在文件最顶部（所有执行代码之前），这是 Python 的硬性要求。
from __future__ import annotations

# 【这行在干什么】引入类型标注工具：Any（任意类型）和 Iterator（迭代器）。
# 【为什么需要】
#   · Any —— 事件字典、来源字典里的值五花八门（数字、字符串、列表……），说不准，用 Any 兜住；
#   · Iterator —— 本文件核心函数 answer_events 的返回类型标注：它返回一个「可迭代对象」，
#     即「能一个接一个吐出字典的东西」。
from typing import Any, Iterator

# 【这行在干什么】引入本项目配置类 Settings 和取配置函数 get_settings。
# 【为什么这么写】retrieve_final_top_k、rerank_candidates、llm_configured 等参数全部来自
#   .env，代码里不写死数字；开头点号「.」表示「同一个包里的模块」。
from .config import Settings, get_settings

# 【这行在干什么】引入大模型相关的三个东西。
# 【参数逐个解释】
#   · LLMError —— 大模型调用出错时的自定义异常（统一被这里捕获，转成 SSE 的 error 事件）；
#   · LLMNotConfigured —— 专用异常：表示「没配置大模型」（配置不齐也算），
#     触发后本文件会走「仅检索模式」降级；
#   · stream_chat —— 真正调用大模型的「流式聊天」函数，一段一段地返回文字。
from .llm import LLMError, LLMNotConfigured, stream_chat

# 【这行在干什么】引入重排函数 rerank_hits。
# 【为什么需要】检索出来的候选可能混着不相关的噪音，重排用大模型把最相关的挑出来，
#   让答案质量更高。本文件把「检索 → 重排」焊在一起，用户无感知。
from .rerank import rerank_hits

# 【这行在干什么】引入检索的两样东西。
# 【参数逐个解释】
#   · RetrievalResult —— 检索结果的包装盒（装着问题、命中列表、两路数量、重排情况等）；
#   · retrieve —— 混合检索的主入口（dense 语义路 + BM25 字面路 → RRF 融合）。
from .retriever import RetrievalResult, retrieve

# 【这行在干什么】引入引用校验器的两个函数。
# 【为什么需要】回答已通过 SSE 流式发到前端、无法撤回，只能在生成完之后补做一次「引用体检」：
#   发现引用了不存在的来源、或数字没依据时，产出修订稿让前端覆盖展示。
# 【参数逐个解释】
#   · verify_citations —— 校验回答里的引用编号与数字有没有依据（不联网、纯本地）；
#   · strip_bad_refs —— 只把「校验没通过」的引用标记删掉，通过的保持原样。
from .citation import strip_bad_refs, verify_citations

# 【这行在干什么】定义系统提示词 SYSTEM_PROMPT：写给大模型看的「工作守则」。
# 【为什么它如此重要】大模型本身是个「什么都知道」的通才，但招投标问答要求「只信资料、
#   不编造」。这份守则把它框成：①只依据参考资料；②引用要标 [参考资料N]；③资料不够就
#   老实承认缺什么；④金额、日期、编号必须一字不差；⑤简体中文、结构清晰。
# 【注意事项】这段文字会被原样拼进每次对话的 system 消息里，是行为约束，不能乱改；
#   一旦与前端约定好的引用格式（[参考资料N]）不一致，前端高亮就会失效。
SYSTEM_PROMPT = """你是「招投标智能问答助手」，服务对象是招投标业务人员。

作答要求：
1. 只依据【参考资料】回答，不要引入资料之外的知识，也不要凭常识推测。
2. 引用某条资料时，用 [参考资料N] 标注出处。
3. 如果参考资料不足以回答问题，直接说明「当前知识库中没有找到相关依据」，并说明你缺什么信息，
   绝对不要编造项目名称、金额、日期、资质要求等内容。
4. 金额、日期、编号、公司名称等关键信息必须与资料完全一致，不得改写。
5. 回答用简体中文，结构清晰；能列表就列表，避免空话套话。"""


# 【这行在干什么】定义知识库为空时的统一提示文案 NO_DATA_MESSAGE。
# 【为什么单独定义成常量（关键设计）】「知识库是空的」这个场景在 /api/chat 和 Agent
#   两条链路里都可能出现，把文案抽出来共用，保证两处说法一致、只改一处即可生效。
# 【内容拆解】文案分三部分：先直说「没有可用数据」；再给出两个可能原因（表是空的 /
#   数据灌了但没重建库）；最后给出解决办法（后台点重建 / 命令行跑 ingest）。
# 【注意事项】这里的反引号 ``bidding_notices`` 是 Markdown 代码样式，前端按 Markdown
#   渲染时会被显示成等宽字体，纯文本环境下则会原样显示两个反引号——可接受。
NO_DATA_MESSAGE = (
    "知识库当前没有可用数据，无法给出有依据的回答。\n\n"
    "可能的原因：\n"
    "1. `bidding_notices` / `bidding_project` / `companies` 三张表还是空的；\n"
    "2. 数据已灌入，但还没有重建知识库。\n\n"
    "请在后台点击「重建知识库」，或执行 `python -m app.ingest`（需先停掉后端服务）。"
)


# 【这行在干什么】定义函数 render_reference_blocks：把命中的来源列表渲染成「参考资料」文本块。
# 【参数解释】
#   · sources —— 检索结果里转换好的来源列表（每个元素是一张「卡片」所需的信息，
#     通常来自 RetrievalResult.to_sources()：有 label 表中文名、table 表名、primary_key 记录ID、
#     chunk_index/chunk_total 第几段、text 原文等）。
# 【返回值】一段排版好的文本；没有任何来源时返回空字符串 ""。
# 【为什么必须和 Agent 共用这套渲染（关键约定）】Agent 的 RAG 工具和这里的 /api/chat 都把
#   来源渲染成「[参考资料N]」的固定格式。只有两处编号规则完全一致，用户在两种模式下
#   看到的引用标注才都能对上号、不会出现「引用了不存在的第 3 条」这种错位。
def render_reference_blocks(sources: list[dict[str, Any]]) -> str:
    """把命中的来源渲染成 ``[参考资料N]`` 文本块；无来源时返回空字符串。

    Agent 的工具与这里的提示词组织共用同一套渲染，保证引用编号一致。
    """
    # 【这行在干什么】建一个空列表 blocks，用来逐块收集渲染好的文本。
    # 【类型解释】list[str]：里面每个元素是一段「完整的一条参考资料」文本。
    # 【为什么用列表最后再 join】先积攒再一次性拼接，比反复 += 拼字符串更省内存、
    #   性能更好（字符串是不可变的，+= 每次都会创建新对象）。
    blocks: list[str] = []
    # 【这行在干什么】遍历每一条来源，并按顺序编号。
    # 【参数逐个解释】
    #   · enumerate(sources, start=1) —— 给列表里的每个元素配一个序号：
    #     第一个元素是 (1, 第一条)，第二个是 (2, 第二条)……start=1 表示从 1 开始数。
    #     为什么不从 0 开始？因为用户看到的是「参考资料1、参考资料2」，编号从 1 才自然。
    #   · i —— 编号（第几条）；
    #   · src —— 当前这条来源的字典（里面的键就是上文说的那些字段）。
    for i, src in enumerate(sources, start=1):
        # 【这行在干什么】拼出这一条参考资料的「抬头行」。
        # 【写法拆解】这是一条多行拼接语句，用括号包起来、分成多行写，是 Python 常见的
        #   长字符串写法（括号内可以随意换行，语法不会错）。
        #   · f"..." —— 格式化字符串，{变量} 会被替换成变量的实际值；
        #   · [参考资料{i}] —— 编号，如「参考资料1」；
        #   · src['label'] —— 表的中文名（如「招标公告」）；
        #   · （src['table']）—— 英文表名（如 bidding_notices），方便技术人员核对；
        #   · src['primary_key'] —— 这条记录在数据库里的主键（第几条记录）。
        # 【为什么抬头带这么多信息】用户看到答案时，需要知道「这条依据到底来自哪张表、
        #   哪条记录」，将来想复核原文才能按图索骥。
        header = (
            f"[参考资料{i}] 来源表：{src['label']}（{src['table']}）"
            f"｜记录ID：{src['primary_key']}"
        )
        # 【这行在干什么】如果这条记录被切成了多段（chunk_total > 1），就在抬头补上「第几段」。
        # 【为什么这么写】一条超长公告可能被切成好几段入库，命中可能只命中其中一段；
        #   告诉用户「这是第 1/3 段」就不至于误以为整篇只有这么点内容。
        # 【参数解释】src.get("chunk_total", 1) —— 用 get 带默认值：万一某条来源没带
        #   chunk_total 字段，就按「共 1 段」处理（默认值兜底，不报错）。
        if src.get("chunk_total", 1) > 1:
            # 【这行在干什么】在已有抬头后面追加「第 N/M 段」。
            # 【注意事项】chunk_index 从 0 开始数（编程惯例），展示给用户时要 +1，
            #   否则用户会看到「第 0/2 段」这种怪话。
            header += f"｜第 {src['chunk_index'] + 1}/{src['chunk_total']} 段"
        # 【这行在干什么】把「抬头行 + 换行 + 原文」拼成完整的一条参考资料，收进列表。
        # 【为什么 \n 连接】抬头在上一行、正文在下一行，层次清晰；
        #   等会儿多个参考资料块之间再用两个换行分隔，大模型能清楚分辨「一块是一份资料」。
        blocks.append(f"{header}\n{src['text']}")
    # 【这行在干什么】把所有块用「两个换行」连接成一段完整的上下文。
    # 【为什么是两个 \n】块与块之间留空行，视觉和语义上都更易读；
    #   大模型解析多份参考资料时，也能靠空行切分「这是新的参考资料X了」。
    # 【注意事项】若 blocks 为空，join 的结果是空字符串 ""，调用方据此判断「没有来源」。
    return "\n\n".join(blocks)


# 【这行在干什么】定义函数 build_messages：把「命中原文」拼成发给大模型的对话消息列表。
# 【参数解释】
#   · question —— 用户这次问的问题原文；
#   · sources —— 命中的来源列表（会被渲染成参考资料）。
# 【返回值】一个消息列表，格式是 OpenAI 风格的 [{"role": ..., "content": ...}, ...]，
#   正是大模型接口（llm.stream_chat）要求的标准输入结构。
# 【为什么需要这个函数】大模型不能「裸聊」——它需要一份结构化的上下文：
#   先有 system 守则、再有带参考资料的 user 消息，它才知道该拿什么作答。
def build_messages(question: str, sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把命中的原文拼成上下文，交给大模型。"""
    # 【这行在干什么】先把来源渲染成参考资料文本；如果没有来源，就用「（无）」占位。
    # 【为什么这么写】context 会被拼进提示词里，不能为空字符串（空了会让提示词出现
    #   连续的换行符，排版难看）。用 or 判断：render_reference_blocks 返回空字符串
    #   （空字符串算「假」）时，就替换成占位符「（无）」。
    context = render_reference_blocks(sources) or "（无）"

    # 【这行在干什么】拼装发给大模型的「用户消息」正文。
    # 【写法拆解】f"..." 格式化字符串，跨多行拼装：
    #   · 【参考资料】\n{context} —— 先给资料，明确告诉模型「依据都在这儿」；
    #   · 【用户问题】\n{question} —— 再给问题；
    #   · 最后补一句指令：「严格依据资料作答 + 标注 [参考资料N]」，把 system 守则
    #     再强调一遍，双保险。
    # 【为什么资料在前、问题在后】模型读到的顺序就是它「先看依据、再想答案」的顺序，
    #   这样它更不容易先入为主地自由发挥。
    user_content = (
        f"【参考资料】\n{context}\n\n"
        f"【用户问题】\n{question}\n\n"
        "请严格依据上述参考资料作答，并在引用处标注 [参考资料N]。"
    )
    # 【这行在干什么】返回标准的消息列表：一条 system 守则 + 一条 user 问题。
    # 【为什么只有这两条】本文件处理的是一次独立问答（没有把历史对话带进来），
    #   所以只需 system + user；Agent 链路带历史对话的版本在 agent.py 里组装。
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# 【这行在干什么】定义函数 _preview：从命中记录的原始字段里挑出几条非空信息，拼成一行摘要。
# 【名字前的下划线含义】约定俗成：以下划线开头的函数是「内部专用」——本文件内部
#   自己用（retrieval_only_answer 里调用），不承诺给外部用，外部也不该 import 它。
# 【参数解释】
#   · hit —— 一条命中记录字典，里面可能有 fields（原始字段）和 text（渲染正文）；
#   · max_fields=4 —— 最多挑 4 个字段进摘要（默认值，调用时可覆盖）。
# 【为什么需要它】降级模式（没配大模型）下，系统要展示「命中的原文摘要」，
#   但原始字段可能又长又多，全展示会把回答撑爆。这个函数负责「挑几条要看的、截短」。
# 【返回值】一行用「；」连接的摘要文本；实在没内容就退而取 text 前 120 字符。
def _preview(hit: dict[str, Any], max_fields: int = 4) -> str:
    """从命中的原始字段里挑出几条非空信息，拼成一行摘要。"""
    # 【这行在干什么】取出命中记录的原始字段字典；没有 fields 就用空字典兜底。
    # 【为什么用 or {}】万一某些来源没带 fields 键，hit["fields"] 会直接报 KeyError，
    #   用 .get 加默认值更稳妥。这里写成 `hit.get("fields") or {}`：
    #   取出来是 None（假）时替换成空字典，保证下面可以安全地 .items() 遍历。
    fields: dict[str, Any] = hit.get("fields") or {}
    # 【这行在干什么】建一个空列表 parts，收集「挑中的字段摘要片段」。
    parts: list[str] = []
    # 【这行在干什么】遍历字段字典里的每一对「键（字段名）→ 值（字段内容）」。
    # 【参数解释】.items() 一次给出 (key, value) 两个值，循环变量 key、value 分别接住。
    for key, value in fields.items():
        # 【这行在干什么】如果值是「空」（None 或空字符串），跳过这条，不展示。
        # 【为什么这么写】空字段展示出来只是「字段名: 」的空壳，纯属噪音；
        #   既占篇幅又让人看不懂，直接过滤掉。
        if value in (None, ""):
            continue
        # 【这行在干什么】把值强制转成字符串，去掉首尾空白，再把内部的换行压成空格。
        # 【为什么必须转字符串】字段值可能是数字、日期等类型，拼接文本必须用字符串。
        # 【为什么 .strip()】去掉首尾看不见的空格/换行，摘要才干净。
        # 【为什么 replace("\n", " ")】摘要要「一行显示」；原始文本里的换行不压平的话，
        #   这一行摘要会突然断成两行，破坏排版。
        text = str(value).strip().replace("\n", " ")
        # 【这行在干什么】清洗后如果变成空字符串，同样跳过。
        # 【为什么这么写】有的值可能原本就是一堆空白，strip 之后才现出「空」的原形，
        #   这里再补一刀过滤，避免空壳字段混进摘要。
        if not text:
            continue
        # 【这行在干什么】把「字段名=内容前60字」拼进摘要片段列表。
        # 【为什么截到 60 字】字段内容可能很长（比如备注有几百字），摘要只求「看个大概」，
        #   截到 60 字足够传达信息，又不会把整段回答撑得太长。
        # 【注意事项】硬截断没有加省略号标记——这是「偷懒但可接受」的取舍：
        #   摘要本来就是给用户看个轮廓，后面还有全文卡片兜底。
        parts.append(f"{key}={text[:60]}")
        # 【这行在干什么】攒够了 max_fields 条就停止。
        # 【为什么这么写】字段可能很多（十几二十个），全展示没必要；
        #   挑前几条最有代表性的即可。注意 break 只跳出当前循环，不影响函数其余部分。
        if len(parts) >= max_fields:
            break
    # 【这行在干什么】把挑中的片段用「；」连成一行返回；如果一条都没挑到，
    #   就退而求其次取渲染正文的前 120 个字符。
    # 【为什么 parts 为空时要兜底】极端情况下该记录所有字段都是空的，那摘要就没内容可写，
    #   此时把 hit 的 text 前 120 字顶上来，保证至少有点东西可看。
    # 【参数解释】hit.get("text", "") —— 没 text 键就用空字符串；[:120] 是切片（取前 120 个字符）。
    return "；".join(parts) if parts else (hit.get("text", "")[:120])


# 【这行在干什么】定义函数 retrieval_only_answer：生成「仅检索模式」下的降级回答全文。
# 【参数解释】sources —— 命中的来源列表（有多条就列多条）。
# 【返回值】一段完整的 Markdown 文本，前端可以直接渲染。
# 【什么时候会用到（三种触发途径）】①.llm_configured 为假（.env 里没配齐大模型）；
#   ②调用 stream_chat 时抛 LLMNotConfigured；③其他异常降级。共同点：没有大模型可用，
#   但检索系统是好的，所以要「信息不打折、不编造」地把原文摘要陈列给用户。
# 【为什么用检索结果顶替而不是报错（关键设计取舍）】用户问的是「有没有 X 的资料」，
#   检索命中了就是实打实的信息；就算没有大模型，把这些原文展示出来也比报错强。
#   代价是用户得不到自然语言的综合回答——所以文末给了配置指引，引导用户补全配置。
def retrieval_only_answer(sources: list[dict[str, Any]]) -> str:
    """未配置大模型时的降级回答：直接列举命中的原文，信息不打折、不编造。"""
    # 【这行在干什么】拼回答的开头几行。
    # 【写法拆解】
    #   · **仅检索模式** —— Markdown 加粗；
    #   · len(sources) —— 命中条数，写出来让用户知道「查到了几条」；
    #   · 结尾空字符串元素 "" —— 故意留一个空行，Markdown 里两段之间要有空行才分得开。
    # 【为什么说是「降级」】因为没有大模型，无法组织通顺的综述，只能用清单代替。
    lines = [
        f"当前处于**仅检索模式**（未配置大模型），已从知识库命中 {len(sources)} 条相关记录，原文摘要如下：",
        "",
    ]
    # 【这行在干什么】逐条遍历命中来源，为每条生成「编号 + 抬头 + 摘要」。
    # 【参数解释】enumerate(sources, start=1) —— 从 1 开始给每条来源编号，
    #   用户看到的就是「1. 2. 3.」这种自然的条目编号。
    for i, src in enumerate(sources, start=1):
        # 【这行在干什么】拼出这一条的「命中指标」描述。
        # 【写法拆解】
        #   · src.get('matched_by', []) —— 这条是被哪几路召回的（如 ['dense','bm25']）；
        #   · ' + '.join(...) —— 把列表用「 + 」拼起来，显示成「dense + bm25 命中」；
        #   · src['rrf_score'] —— RRF 融合后的得分（排名融合分，越大越靠前）。
        metric = f"{' + '.join(src.get('matched_by', []))} 命中，RRF={src['rrf_score']}"
        # 【这行在干什么】如果这条经过了重排（rerank_rank 有值），再补上「重排第几名」。
        # 【为什么这么写】重排会改变最终排名（前端徽标也会显示重排情况），
        #   这里如实标注「重排 #2」，用户才能理解「为什么它排这么靠前」。
        # 【注意】只针对「有 rerank_rank 的」才显示，避免出现空的「重排 」字样。
        if src.get("rerank_rank"):
            metric += f"，重排 #{src['rerank_rank']}"
        # 【这行在干什么】把「条目编号 + 表名 + 记录ID + 命中指标」拼成这一条的粗体抬头。
        # 【写法拆解】**...** 是 Markdown 加粗；label 是中文表名、primary_key 是记录ID，
        #   一起放在方括号里表示「这条是什么」。
        lines.append(f"**{i}. 【{src['label']}｜记录 {src['primary_key']}】**（{metric}）")
        # 【这行在干什么】把这条的字段摘要（_preview 的结果）加进去，作为正文。
        lines.append(_preview(src))
        # 【这行在干什么】追加一个空行，让每条之间隔开，排版更清爽。
        lines.append("")
    # 【这行在干什么】在文末追加配置指引（Markdown 引用块，> 开头）。
    # 【为什么必须写这段】用户看到降级回答后很可能会疑惑「为什么没有综合回答」，
    #   明确告诉他「配好这三个环境变量就有自然语言回答」，等于给了一条升级路径。
    # 【参数解释】反引号包住的是配置项名，Markdown 渲染成代码样式。
    lines.append("> 配置 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 后即可获得自然语言综合回答。")
    # 【这行在干什么】把所有行用换行符连接成最终文本返回。
    # 【为什么用 \n】lines 里每行自带语义分行，join 后就是一整段带空行的 Markdown 文本。
    return "\n".join(lines)


# 【这行在干什么】定义核心函数 answer_events：完整的问答事件流（生成器）。
# 【参数解释】
#   · question —— 用户的问题原文；
#   · settings=None —— 配置对象，不传就内部调 get_settings() 从 .env 读（测试时可塞假配置）；
#   · final_k=None —— 最终送进大模型的条数，不传就取配置里的 retrieve_final_top_k。
# 【返回值】Iterator[dict[str, Any]] —— 一个能逐个产出事件字典的迭代器（生成器）。
# 【调用方式（关键）】因为它是生成器，调用 answer_events(q) 并不会立刻执行函数体，
#   而是返回一个迭代器；main.py 里用 for event in answer_events(q) 逐条取事件并转成 SSE
#   推给前端。函数体里每次 yield 暂停、yield 的值就是「一个事件」。
def answer_events(
    question: str,
    settings: Settings | None = None,
    final_k: int | None = None,
) -> Iterator[dict[str, Any]]:
    """完整的问答事件流。"""
    # 【这行在干什么】拿到真正要用的配置对象（传了就用传的，没传就从 .env 读）。
    # 【为什么写成 settings or get_settings()】get_settings() 自带缓存，反复调用不重复读盘；
    #   一行搞定「不传就用默认」，比 if/else 更简洁。
    s = settings or get_settings()
    # 【这行在干什么】决定「最终送进大模型的条数」。
    # 【参数解释】final_k or s.retrieve_final_top_k：调用方显式传了就用传的（比如 Agent
    #   可能想要不同条数），没传就用配置里的默认值。
    # 【为什么叫 final_k】它只是「重排后最终留下的条数」，不是「检索候选池的大小」——
    #   检索阶段要拉更多候选，详见下一行。
    want_k = final_k or s.retrieve_final_top_k
    # 【这行在干什么】决定「候选池的大小」。
    # 【为什么必须 >= want_k（关键设计，别删）】重排是从候选里「挑最相关的 want_k 条」，
    #   如果候选池比 want_k 还小，重排就没有挑选余地、等于白跑。
    #   max(want_k, s.rerank_candidates) 保证池子至少和 want_k 一样大，通常更大
    #   （rerank_candidates 配置为比 final_top_k 大的数，让重排有的挑）。
    pool_k = max(want_k, s.rerank_candidates)

    # 【这行在干什么】打开「接住岔子」的网：把「检索 + 重排」整个包进来。
    # 【为什么必须 try】检索可能因为知识库没建好、重排可能因为大模型调用失败而出错；
    #   任何一步崩了都不该让整个请求 500，而要转成一条 SSE error 事件告诉前端。
    try:
        # 【这行在干什么】执行混合检索，但按更大的候选池拉取。
        # 【为什么是 pool_k 而不是 want_k】见上面注释：检索先多拉，给重排留挑选空间。
        # 【参数解释】question —— 用户问题；settings=s —— 复用刚拿到的配置（避免重复读 .env）；
        #   final_k=pool_k —— 传给 retrieve 的「最终条数」实为更大的候选池大小。
        result: RetrievalResult = retrieve(question, settings=s, final_k=pool_k)
        # 【这行在干什么】对候选执行大模型重排，筛出最相关的 want_k 条。
        # 【参数逐个解释】
        #   · question —— 重排也需要问题，用于衡量候选与问题的相关性；
        #   · result.hits —— 检索融合后的候选列表；
        #   · s —— 配置（重排的模型、超时、token 上限等都在里面）；
        #   · top_k=want_k —— 重排后保留几条。
        # 【返回值拆包】rerank_hits 返回两个值：hits（重排后的列表）和 rerank_meta
        #   （重排情况：耗时、token、几条→几条、是否降级等），用逗号同时接住。
        hits, rerank_meta = rerank_hits(question, result.hits, s, top_k=want_k)
        # 【这行在干什么】把重排后的列表放回检索结果盒子里。
        # 【为什么直接改 result.hits】RetrievalResult 是普通对象（非冻结），可以就地改字段；
        #   这样后续代码统一读 result.hits，不必为「重排前后的两套列表」写两份逻辑。
        result.hits = hits
        # 【这行在干什么】把重排情况也写回盒子里。
        # 【为什么需要】前端徽标要显示「重排生效：8→5」等信息；这份元数据被放进
        #   result.rerank_meta，下面的 retrieval 事件会把它并进 meta 一起发给前端。
        result.rerank_meta = rerank_meta
    # 【这行在干什么】接住上面任何一步抛出的异常，绑定到 exc 上。
    # 【注意事项】这里捕获的是 Exception（所有异常），范围比较宽——因为「检索失败」的
    #   具体原因五花八门（连接失败、文件损坏、参数错误……），统一转成 error 事件最稳妥。
    except Exception as exc:
        # 【这行在干什么】产出一条 error 事件，把异常信息带给前端。
        # 【为什么用 f"检索失败：{exc}"】把具体的异常文本拼进 message，
        #   用户/开发者能看到「到底哪里失败了」，方便排查。
        yield {"type": "error", "message": f"检索失败：{exc}"}
        # 【这行在干什么】生成器结束（return 不带值 = 收工）。
        # 【为什么必须 return】否则函数会继续往下跑，把「检索失败」的后续步骤也执行了，
        #   产生矛盾的事件序列（先 error 又 sources）。显式 return 立即终止整个事件流。
        return

    # 【这行在干什么】产出第一条事件：retrieval（检索统计）。
    # 【内容拆解】meta 字典里：
    #   · dense_count —— 语义路召回几条；sparse_count —— 关键词路召回几条；
    #   · bm25_ready —— BM25 索引是否就绪（false 说明只有语义路在工作）；
    #   · final_count —— 重排后最终留下几条；
    #   · llm_configured —— 大模型配没配（前端据此决定要不要渲染「仅检索模式」提示）；
    #   · **result.rerank_meta —— 「**」是字典解包：把重排元数据里的键值对全部平铺进
    #     外层字典。例如 rerank_meta = {"applied": True, "time_ms": 1234} 时，这个 meta
    #     就等于 {dense_count:..., **那些键}。这样前端一次拿到所有统计，不用分两次请求。
    # 【为什么这事件排在 sources 之前】前端先看到检索情况（徽标先动），再看到来源卡片，
    #   节奏自然，也避免「卡片出来了统计才到」的闪烁。
    yield {
        "type": "retrieval",
        "meta": {
            "dense_count": result.dense_count,
            "sparse_count": result.sparse_count,
            "bm25_ready": result.bm25_ready,
            "final_count": len(result.hits),
            "llm_configured": s.llm_configured,
            **result.rerank_meta,
        },
    }

    # 【这行在干什么】把检索结果转换成「前端参考来源卡片」所需的精简结构。
    # 【为什么用 result.to_sources()】hits 里的原始字典字段繁多，卡片只需要一小部分；
    #   to_sources 在 retriever.py 里做「精洗」，还顺便做了正文截断（text_limit=900 之类）。
    sources = result.to_sources()
    # 【这行在干什么】产出第二条事件：sources（参考来源）。
    # 【为什么单独一条事件】前端拿到 sources 后要渲染「来源卡片」区域；
    #   把它与正文增量分开，前端就能先渲染卡片、再逐字渲染回答。
    yield {"type": "sources", "sources": sources}

    # 【这行在干什么】情况一判断：知识库为空（没有命中任何来源）。
    # 【为什么「没命中」等于「知识库空」】本项目约定：知识库为空时检索结果必然为空，
    #   二者在用户侧的表现一致，所以统一按「暂无数据」处理。
    # 【注意事项】这与项目硬约束一致：「当知识库为空时，明确说明没有数据，
    #   不要带参考标注」——这里直接给 NO_DATA_MESSAGE，不带任何 sources。
    if not sources:
        # 【这行在干什么】把统一文案作为正文增量产出。
        yield {"type": "delta", "text": NO_DATA_MESSAGE}
        # 【这行在干什么】宣告结束，模式是 no_data（前端可据此隐藏徽标或显示特定样式）。
        yield {"type": "done", "mode": "no_data"}
        # 【这行在干什么】收工，不再往下走。
        return

    # 【这行在干什么】情况二判断：命中了资料，但没配大模型 → 走「仅检索模式」。
    # 【参数解释】s.llm_configured —— 配置模块算好的布尔值：LLM_BASE_URL / LLM_API_KEY /
    #   LLM_MODEL 都齐了才是 True，缺任何一个都是 False。
    # 【为什么这是合理的降级（关键设计）】检索系统完好、资料是实的；没有大模型只是
    #   无法「组织语言」，不代表查不到资料。把原文摘要陈列出来，信息零损失。
    if not s.llm_configured:
        # 【这行在干什么】产出降级回答全文作为正文增量。
        yield {"type": "delta", "text": retrieval_only_answer(sources)}
        # 【这行在干什么】宣告结束，模式是 retrieval_only（前端可显示「仅检索模式」徽标）。
        yield {"type": "done", "mode": "retrieval_only"}
        # 【这行在干什么】收工。
        return

    # 【这行在干什么】情况三（正常 RAG）：先产出一个「空正文增量」事件。
    # 【为什么要空字符串（关键技巧，别删）】SSE 流的第一个事件会「建立连接、让前端
    #   立刻开始渲染界面」。如果第一条 delta 直接是大模型生成的字，用户会感觉「卡顿」；
    #   先发一个空 delta，前端进入「已就绪」状态，打字机效果更平滑。
    # 【注意事项】前端必须能容忍空 text 的 delta 事件（不应渲染出空白段落）。
    yield {"type": "delta", "text": ""}  # 提前建立连接，前端可立即开始渲染
    # 【这行在干什么】准备一个列表，把流式出来的每一段正文顺带攒起来。
    # 【为什么要攒】正文已经一段段发给前端了、无法撤回；等生成完要做「引用校验」时，
    #   必须有一份「完整回答」在手，所以边发边攒。攒下来的内容不改变任何已发出的字节，
    #   因此「校验通过时行为与现在逐字节一致」这条硬要求不受影响。
    answer_parts: list[str] = []
    # 【这行在干什么】打开「接住岔子」的网，包住大模型的流式调用。
    # 【为什么单独再 try 一次】检索部分已经 try 过了；大模型调用有自己的异常类型
    #   （LLMNotConfigured / LLMError），需要区分对待，所以这里单独包一层。
    try:
        # 【这行在干什么】调用流式聊天，逐段获得回答文字。
        # 【参数解释】
        #   · build_messages(question, sources) —— 组装好的消息列表（守则 + 带资料的提问）；
        #   · s —— 配置（模型名、base_url、api_key、温度等）。
        # 【为什么 for 循环逐段取】stream_chat 返回的也是生成器，每次迭代拿到一小段文字；
        #   这里每拿到一段就 yield 给前端，实现「边生成边推送」。
        for piece in stream_chat(build_messages(question, sources), s):
            # 【这行在干什么】先把这一小段收进缓冲区（供结尾校验用）。
            answer_parts.append(piece)
            # 【这行在干什么】把这一小段文字作为 delta 事件产出。
            yield {"type": "delta", "text": piece}
    # 【这行在干什么】接住「没配置大模型」的专用异常。
    # 【为什么还要捕获它】上面的 llm_configured 判断是按配置「预先」算的；
    #   但 stream_chat 内部可能因为配置不完整（如 API key 为空但被视作已配置）
    #   在运行时才抛这个异常。兜住它，避免请求 500。
    except LLMNotConfigured:
        # 【这行在干什么】降级：把命中原文摘要作为正文产出。
        # 【为什么这里还能降级】来源已经有了，降级答案照样信息完整，用户不白问。
        yield {"type": "delta", "text": retrieval_only_answer(sources)}
        # 【这行在干什么】宣告结束，模式 retrieval_only。
        yield {"type": "done", "mode": "retrieval_only"}
        # 【这行在干什么】收工。
        return
    # 【这行在干什么】接住「已配置但调用失败」的异常（网络错误、超时、模型报错等）。
    except LLMError as exc:
        # 【这行在干什么】产出一条 error 事件，把失败原因带给前端。
        # 【注意事项（重要取舍）】这里「不」走降级答案，而是报错——与项目硬约束一致：
        #   「若模型名无效等错误，返回 error + done(agent_failed) 状态」，绝不让错误静默
        #   变成一份看似正常其实没经大模型的回答。不过 sources 已经发过了，前端仍可展示
        #   命中的原文，用户不至于一无所获。
        yield {"type": "error", "message": str(exc)}
        # 【这行在干什么】宣告结束，模式 llm_failed（前端据此显示红色警示）。
        yield {"type": "done", "mode": "llm_failed"}
        # 【这行在干什么】收工。
        return

    # 【这行在干什么】正常走完：宣告结束，模式 llm。
    # 【为什么 done 仍是「正文」的压轴事件】前端收到 done 就停止等待、收尾展示正文；
    #   所以所有「正文 delta」必须全部先于它发出——这一点和改造前完全一样。
    yield {"type": "done", "mode": "llm"}

    # 【引用校验（生成后体检）】正文已全部发往前端、无法撤回，这里再对「完整回答」核对一遍引用。
    # 【为什么放在 done 之后（关键设计）】本项目约定「正文」由 done 收尾；而修订稿是**额外的
    #   补充事件**，走另一条通道（channel="revision"），前端把它单独渲染成「修订卡片」，
    #   完全不干扰已经收尾的正文。这样安排的好处是：校验通过时，下面一段一个事件都不发，
    #   整条链路的行为与改造前逐字节一致（这正是硬性要求）。
    # 【为什么要 try 静默】校验器只是「附加信息」，绝不能因为它的任何意外（正则异常、
    #   sources 缺字段……）把已经成功的问答弄挂。一旦出错就当没做校验，直接收摊。
    try:
        check = verify_citations("".join(answer_parts), sources)
    except Exception:  # noqa: BLE001
        check = None

    # 【这行在干什么】只有「确实没通过」时才追加修订事件；通过时这里什么都不发。
    if check is not None and not check.get("ok"):
        # 【这行在干什么】把校验失败的引用标记删掉，得到「修订全文」。
        revised = strip_bad_refs("".join(answer_parts), check.get("bad_refs") or [])
        # 【这行在干什么】发出修订正文。channel="revision" 让前端把它存进 revision 字段，
        #   渲染成「修订卡片」而不是追加到正文——所以原正文一个字都不会被覆盖或重发。
        yield {"type": "delta", "channel": "revision", "text": revised}
        # 【这行在干什么】再补一条校验结果事件，把问题清单（越界引用/无依据数值）交给前端提示。
        yield {"type": "citation_check", "ok": False, "issues": check.get("issues") or []}
