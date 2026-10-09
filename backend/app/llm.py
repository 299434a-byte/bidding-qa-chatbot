# ============================================================================
# 文件：llm.py —— 和大模型（AI）通话的「电话机」
#
# 【这个文件在整个项目里是干什么的】
#   项目里所有「需要 AI 动脑」的地方，最终都要经过这个文件去调用大模型。
#   它像一个「电话机」：谁能打电话（客户端），怎么打电话（同步/异步/一次性），
#   打不通怎么办（统一抛异常），都封装在这里，别处只管「拨号 + 说事」。
#   本文件对外提供四样东西：
#     · 两个异常类 LLMNotConfigured / LLMError —— 区分「没配电话卡」和「电话打不通」；
#     · _client / aclient  —— 造出「同步/异步」两种客户端；
#     · stream_chat / astream_chat —— 「边生成边返回」的流式对话（打字机效果）；
#     · complete_chat —— 「一次性拿完整结果」的调用（用于重排等要解析 JSON 的场景）。
#
# 【谁调用它（上游）】
#   · rag.py —— /api/chat 链路：用 stream_chat 把答案一个字一个字推给前端；
#   · rerank.py —— 用 complete_chat 拿到完整的一段 JSON 排序结果；
#   · agent.py / deep_agent.py —— 用 astream_chat 在异步流程里边调模型边推事件；
#   · main.py —— 用 llm_configured / complete_chat 做「配置探测」接口。
#
# 【它给谁用（下游）】
#   大模型服务商（如 DeepSeek）的 HTTP 接口。本文件本质上是「用 openai 这个通用 SDK
#   去访问一个兼容 OpenAI 接口的服务」——所以换厂商通常只需改 .env，不用改这里。
#
# 【先懂的四个概念（大白话）】
#   1) OpenAI 兼容接口：很多大模型厂商（DeepSeek、智谱、Moonshot…）都把自己的 HTTP 接口
#      做得和 OpenAI 一模一样，这样大家就能用同一个 SDK（openai 库）去访问它们的服务，
#      只是把「网址（base_url）」和「钥匙（api_key）」换成自己的。这叫「接口兼容」。
#   2) 流式（stream）与非流式（非 stream）：
#      · 非流式：你说完，AI 想完整段话，再一次性给你。简单，但要等，且没法边看边等。
#      · 流式：AI 想到一点就吐一点。用户能看到字一个个冒出来（打字机效果），体验更好，
#        但代码要处理「一段一段接」的逻辑，稍微复杂一点。
#   3) 同步（sync）与异步（async）：
#      · 同步：代码执行到「等 AI 回复」这一步，会一直卡在那里干等，别的活也干不了（阻塞）；
#      · 异步：等 AI 的同时可以先去干别的（不阻塞）。
#      本文件两套都提供。注意：同步版本（stream_chat）在异步场景里用会拖慢整体性能，
#      所以异步场景必须用 astream_chat（见 stream_chat 的 docstring 提醒）。
#   4) max_tokens（最大输出字数上限）：AI 一次最多能输出多少「词元（token）」。
#      这是本项目反复踩坑的地方，见 complete_chat 里的详细解释。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring）】
# 【为什么放在这里】注释不算语句，所以放在这一大段注释之后，它依然是本模块的
#   第一条语句、依然是官方自我介绍，内容与原文一字不差。
"""大模型客户端（OpenAI 兼容接口，流式输出）。

通过 .env 配置，不写死任何厂商信息：

    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_API_KEY=sk-xxxx
    LLM_MODEL=deepseek-chat

未配置时抛 ``LLMNotConfigured``，上层自动降级为「仅检索」模式。
"""

# 【这行在干什么】打开「类型注解延迟求值」开关。
# 【为什么这么写】本文件用了 int | None、Settings | None 这类新式标注，
#   老版本 Python（3.8/3.9）不认；加上这行后标注先被当成字符串存着，需要时才解析。
# 【注意事项】必须放在文件最顶部（所有执行代码之前），这是 Python 的硬性要求。
from __future__ import annotations

# 【这行在干什么】引入两种「类型标注」工具：AsyncIterator（异步迭代器）和 Iterator（迭代器）。
# 【为什么需要】
#   · Iterator[str] —— 表示「一个能一个接一个吐出字符串的东西」，用来标注 stream_chat 的
#     返回值（它用 yield 一段段吐文字）；
#   · AsyncIterator[str] —— 同样是「一段段吐字符串」，但吐的时候是「异步」的
#     （要用 async for 去取），用来标注 astream_chat 的返回值。
# 【通俗理解】普通迭代器像「自动售货机」，你按一下掉一件；异步迭代器像「外卖骑手」，
#   你下单后要等骑手送到（await），期间你还能干别的。
from typing import AsyncIterator, Iterator

# 【这行在干什么】引入本项目配置类 Settings 和取配置函数 get_settings。
# 【为什么这么写】模型名、超时时间、密钥等一律从 .env 读（走 config.py），
#   本文件不写死任何厂商信息——这是项目硬约束。
# 【注意事项】开头那个点「.config」表示「同一个包里的模块」，是相对导入。
from .config import Settings, get_settings


# 【这行在干什么】定义一个自定义异常类 LLMNotConfigured，继承自 RuntimeError。
# 【为什么需要单独定义一个异常（关键设计）】「没配大模型」和「大模型调用失败」是完全不同的两种
#   情况：前者是「预期内的正常降级」（例如故意不配密钥，只用检索功能），后者是「真的出错了」。
#   上层（rag.py）需要能区分它们：前者走「仅检索模式」继续服务，后者要报错。
#   如果都用一个异常，上层就只能靠「猜」——所以这里专门分成了两个类。
# 【继承 RuntimeError 的意思】RuntimeError 是 Python 的标准异常之一，表示「运行时的问题」。
#   继承它之后，这个异常既能被 `except LLMNotConfigured` 精确接住，也能被更宽泛的
#   `except RuntimeError` 兜住（兼容性更好）。
class LLMNotConfigured(RuntimeError):
    """未配置 LLM，属于预期内的降级场景，不是错误。"""


# 【这行在干什么】定义另一个自定义异常类 LLMError，同样继承 RuntimeError。
# 【为什么和上面分开】见上一条注释：这个代表「配置齐了、但调用过程中真的失败了」
#   （比如网络不通、密钥无效、模型名写错、返回为空）。这种是要如实报给用户的错误。
class LLMError(RuntimeError):
    """调用大模型失败。"""


# 【这行在干什么】定义内部函数 _client：造一个「同步」的大模型客户端。
# 【名字前下划线的含义】约定俗成：下划线开头表示「本模块内部专用」，不对外承诺、外部也不该引用。
# 【参数逐个解释】
#   · settings —— 配置对象（含 base_url / api_key / llm_timeout 等）；
#   · timeout —— 本次调用允许等多久（秒）。默认 None 表示「用配置里那个统一超时」。
# 【返回值】一个 OpenAI 客户端对象（后面用它发聊天请求）。
# 【为什么不直接用 openai 的类、要包一层】因为要把三件麻烦事一次性做掉：
#   ① 检查有没有配置（没配就立刻抛 LLMNotConfigured，让上层走降级）；
#   ② 延迟导入 openai（见下）；
#   ③ 统一填好 base_url / api_key / timeout，避免每处调用都重复写。
def _client(settings: Settings, timeout: int | None = None):
    # 【这行在干什么】检查配置是否完整（base_url、api_key、model 三样都得有）。
    # 【为什么第一时间检查】如果不检查，直接去创建客户端，会在更靠后的地方报一个
    #   「缺少 api_key」之类的底层错误，上层很难分辨这到底是「没配」还是「配错了」。
    #   提前在这里拦截，就能给出一个语义明确的 LLMNotConfigured。
    if not settings.llm_configured:
        # 【这行在干什么】抛出「未配置」异常，并把缺了哪几项写进消息里，便于排查。
        raise LLMNotConfigured("未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    # 【这行在干什么】打开「接住岔子」的网，尝试导入 openai 库。
    # 【为什么把 import 写在函数内部而不是文件顶部（关键设计）】这叫「延迟导入」：
    #   只有在真的要调用大模型时才需要这个库。如果写在文件顶部，那么任何 import 本模块的
    #   代码（哪怕只是用一下异常类）都必须先装好 openai，否则连带启动失败。
    #   放进来之后，「没装 openai 也没关系，只要不去调模型就不会报错」。
    try:
        # 【这行在干什么】从 openai 库里拿出同步客户端类。
        from openai import OpenAI
    # 【这行在干什么】如果 openai 没安装，ImportError 会被这里接住，转成一个更友好的 LLMError。
    # 【参数解释】`as exc` 是把捕获到的原异常对象存进变量 exc，便于后面「挂」在新异常上。
    # 【`# pragma: no cover` 是什么】这是给「测试覆盖率工具」看的注释，表示「这行代码
    #   在正常测试里跑不到（因为测试环境装了 openai），统计覆盖率时可以忽略它」。
    except ImportError as exc:  # pragma: no cover
        # 【这行在干什么】抛出 LLMError，提示用户去装依赖。
        # 【写法拆解】`raise ... from exc` 叫「异常链」：把「因为 ImportError 才导致这个错误」
        #   这条因果线索一起带上。好处是排查时能看到完整的错误来源，而不是只看到最后一层
        #   ——就像快递破损时，既能看到「包裹坏了」，也能看到「是运输途中被压的」。
        raise LLMError("缺少依赖 openai，请执行 pip install openai") from exc

    # 【这行在干什么】创建并返回同步客户端。
    # 【参数逐个解释】
    #   · base_url —— 服务地址（如 https://api.deepseek.com/v1），来自 .env；
    #   · api_key  —— 身份钥匙，来自 .env（绝不写死在代码里）；
    #   · timeout  —— 等待多久算超时。`timeout or settings.llm_timeout` 的意思是：
    #     如果本次调用专门传了 timeout 就用它，否则用配置里的默认值。
    #     【注意】这里用 or 而不是 `if timeout is None`，所以传 0 也会被当成「没传」
    #     ——但超时填 0 本身没有意义，实际上不会有人这么用，无风险。
    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=timeout or settings.llm_timeout,
    )


# 【这行在干什么】定义内部函数 aclient：造一个「异步」的大模型客户端。
# 【参数逐个解释】settings —— 配置对象；timeout —— 本次调用允许等多久（秒），None 用默认。
# 【返回值】一个 AsyncOpenAI 客户端（异步版本）。
# 【和上面 _client 的区别（重点）】一个是同步、一个是异步，代码结构几乎一样，但类型不同。
#   为什么必须两套？因为「异步函数里用了同步客户端」会让整个进程卡住（阻塞事件循环），
#   这一点在 stream_chat 的 docstring 里被特别警告过。
def aclient(settings: Settings, timeout: int | None = None):
    """异步客户端。深度模式需要在 async 生成器里边调模型边推事件，必须用异步版。"""
    # 【这行在干什么】同样先检查配置完整性，没配就抛 LLMNotConfigured 让上层降级。
    if not settings.llm_configured:
        raise LLMNotConfigured("未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    # 【这行在干什么】延迟导入异步客户端；没装 openai 就转成 LLMError。
    try:
        # 【这行在干什么】从 openai 库里拿出「异步」客户端类。
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover
        # 【这行在干什么】把导入失败包装成 LLMError，并保留因果链（from exc）。
        raise LLMError("缺少依赖 openai，请执行 pip install openai") from exc

    # 【这行在干什么】创建并返回异步客户端，参数含义与 _client 完全一致。
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=timeout or settings.llm_timeout,
    )


# 【这行在干什么】定义 stream_chat：同步的「流式」对话函数，一段一段把 AI 的文字交出来。
# 【参数逐个解释】
#   · messages —— 发给 AI 的对话消息列表，形如 [{"role":"system","content":"..."}, {...}]，
#     这是 OpenAI 系列接口的标准格式：role 表明「谁在说话」（system 是规则、user 是用户、
#     assistant 是 AI），content 是内容；
#   · settings —— 配置对象（可省，省了就现取，方便测试时注入假配置）；
#   · 后面几个参数前面有一个星号 `*` —— 这叫「关键字专用参数」：调用时必须写成
#     model="xxx" 这种带名字的形式，不能按位置传。目的就是防止「参数太多记错顺序」，
#     逼你写清楚每个值对应哪个参数。
#   · model —— 想用哪个模型（不传就用配置里的默认模型）；
#   · temperature —— 「随机性/创造力」旋钮。取值一般 0~2，越小越稳、越大越发散。
#     招投标问答要求严谨，所以配置里调得比较低；
#   · max_tokens —— 本次最多输出多少词元（不传就用配置里的默认）；
#   · timeout —— 本次允许等多久。
# 【返回值】一个「迭代器」：可以 for 循环逐段取出文字片段（每段是一小截字符串）。
def stream_chat(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
) -> Iterator[str]:
    """流式生成回答，逐段 yield 文本增量。

    所有可选参数缺省时行为与改动前完全一致（沿用 ``LLM_MODEL`` / ``LLM_MAX_TOKENS`` 等）。
    **注意**：这是同步实现，在 async 生成器里调用会阻塞事件循环；那种场合请用 ``astream_chat``。
    """
    # 【这行在干什么】确定用哪份配置：调用方给了就用给的，没给就现取一份全局配置。
    # 【为什么允许传入】测试时可以把「假配置」传进来，不必真连网络，也方便控制开关。
    s = settings or get_settings()
    # 【这行在干什么】造一个同步客户端（内部会做「是否已配置」的检查）。
    # 【注意事项】这一步可能抛 LLMNotConfigured（没配）或 LLMError（没装依赖），
    #   调用方需要自己捕获，分别走降级/报错分支。
    client = _client(s, timeout=timeout)
    # 【这行在干什么】打开「接住岔子」的网，把调用过程中任何异常统一转成 LLMError。
    # 【为什么统一转换】底层 openai 库可能抛出各种五花八门的异常（网络超时、认证失败、
    #   限流……）。上层只想知道「大模型这条路出问题了」，不想逐个去认所有异常类型。
    #   这里统一翻译成 LLMError，上层就只需要认一种。
    try:
        # 【这行在干什么】发起一次「流式」聊天请求，拿到一个「流对象」（stream）。
        # 【参数逐个解释】
        #   · model —— 用哪个模型。`model or s.llm_model`＝传了就用传的，没传用配置默认；
        #   · messages —— 对话内容；
        #   · temperature —— `s.llm_temperature if temperature is None else temperature`：
        #     【注意这里和别处不一样】这里特意用 `is None` 判断而不是 `or`，
        #     因为 temperature=0.0 是完全合法的值（表示「最稳定、不要随机」），
        #     而 0.0 在 Python 里算「假」，用 or 会被误当成「没传」而覆盖成配置值。
        #     这是「0 值陷阱」，很多人在这里踩过坑。
        #   · max_tokens —— 同样 `max_tokens or s.llm_max_tokens`（词元数不会填 0，用 or 安全）；
        #   · stream=True —— 关键开关：告诉服务端「我要流式，请一段段给」。
        stream = client.chat.completions.create(
            model=model or s.llm_model,
            messages=messages,
            temperature=s.llm_temperature if temperature is None else temperature,
            max_tokens=max_tokens or s.llm_max_tokens,
            stream=True,
        )
        # 【这行在干什么】逐个取出流里的「数据块（chunk）」，每次循环拿到一小段。
        # 【通俗理解】像拧开水龙头，每次接一滴。
        for chunk in stream:
            # 【这行在干什么】跳过「没有内容选项」的块。
            # 【为什么要这样防（重要）】流式返回里偶尔会出现「空壳块」——
            #   比如只带统计信息、没有实际文本；或者（某些服务）最后会发一个结束标记。
            #   这种块的 choices 是空列表或不存在。如果不判断就去取 choices[0]，会直接
            #   报「列表越界」错误，导致整个回答中断。所以先看一眼再取。
            # 【写法拆解】getattr(chunk, "choices", None) 的意思是：安全地取 chunk 的 choices 属性，
            #   如果对象上压根没有这个属性，就返回 None（而不是抛异常）。
            if not getattr(chunk, "choices", None):
                continue
            # 【这行在干什么】取出第一项（也是唯一一项）里的 delta（本次新增的那一小段内容）。
            # 【为什么是 choices[0]】OpenAI 格式允许一次给多个候选答案，我们只要第一个。
            delta = chunk.choices[0].delta
            # 【这行在干什么】从 delta 里安全取出 content（本次新增的文本）。
            # 【为什么可能没有】有些块只带「角色信息」或「结束原因」，没有正文，此时 content 是 None。
            content = getattr(delta, "content", None)
            # 【这行在干什么】只有确实拿到文本，才把它交出去。
            # 【为什么判断 "if content" 而不是 "if content is not None"】空字符串也没必要往外吐，
            #   直接跳过更干净。
            if content:
                # 【这行在干什么】yield：把这一小段文字「交出去」并暂停在这里。
                # 【通俗理解】就像售票窗口：手上有一张票就递出来，然后等下一个顾客上门（下一次循环）
                #   才继续。这样调用方能做到「拿到一段就用一段」，而不用等全部生成完。
                yield content
    # 【这行在干什么】兜住上面整段 try 里任何异常，统一转成 LLMError。
    # 【注意事项】异常链（from exc）必须保留原始错误信息，否则排查时只看到「调用大模型失败」，
    #   却不知道到底是超时还是密钥错。
    except Exception as exc:
        raise LLMError(f"调用大模型失败：{exc}") from exc


# 【这行在干什么】定义 astream_chat：stream_chat 的「异步」版本，异步环境必须用这个。
# 【参数逐个解释】与 stream_chat 完全一致（messages / settings / model / temperature /
#   max_tokens / timeout），只是返回的是「异步迭代器」。
# 【关键差异（重要）】
#   · 客户端用 aclient（异步客户端）；
#   · 创建请求要写 `await`（因为异步调用必须「等」结果）；
#   · 取数据用 `async for` 而不是普通 for。
# 【为什么不直接用同步版就好】同步版在异步流程里一执行就会「卡住整个事件循环」，
#   导致同一个服务里其他人的请求也一起被拖慢——这是很隐蔽的性能事故。
async def astream_chat(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
) -> AsyncIterator[str]:
    """``stream_chat`` 的异步版本（用 ``AsyncOpenAI``，不阻塞事件循环）。"""
    # 【这行在干什么】取配置（同同步版）。
    s = settings or get_settings()
    # 【这行在干什么】造异步客户端。
    client = aclient(s, timeout=timeout)
    # 【这行在干什么】统一把异常翻译成 LLMError（同同步版）。
    try:
        # 【这行在干什么】发起异步流式请求。
        # 【参数解释】`await` 是「等这件事做完」的意思：异步请求不会立刻返回流对象，
        #   必须 await 等它把「流对象」交回来，才能开始逐段取内容。
        # 【注意事项】await 只能出现在 async 函数里，这也是本函数写成 async def 的原因。
        stream = await client.chat.completions.create(
            model=model or s.llm_model,
            messages=messages,
            temperature=s.llm_temperature if temperature is None else temperature,
            max_tokens=max_tokens or s.llm_max_tokens,
            stream=True,
        )
        # 【这行在干什么】异步逐个取数据块。
        # 【通俗理解】普通 for 是「排队依次拿」；async for 是「拿之前可能要先等一下（比如网络还没到）」，
        #   等待期间程序可以去做别的事，不干等。
        async for chunk in stream:
            # 【这行在干什么】跳过空壳块（理由同同步版，防止取 choices[0] 报越界）。
            if not getattr(chunk, "choices", None):
                continue
            # 【这行在干什么】取出本块新增内容 delta（同同步版）。
            delta = chunk.choices[0].delta
            # 【这行在干什么】安全取文本，没有就跳过（同同步版）。
            content = getattr(delta, "content", None)
            if content:
                # 【这行在干什么】把这一小段文本交给调用方（异步版 yield 由 async for 消费）。
                yield content
    except Exception as exc:
        # 【这行在干什么】统一转成 LLMError，并保留原始错误（from exc）。
        raise LLMError(f"调用大模型失败：{exc}") from exc


# 【这行在干什么】定义 complete_chat：一次性拿到完整回答（非流式），返回一整段字符串。
# 【参数逐个解释】
#   · messages —— 对话消息列表（格式同上）；
#   · settings —— 配置（可省）；
#   · model / max_tokens / timeout —— 同 stream_chat；
#   · temperature —— 【注意默认值是 0.0】这里默认「最稳定、不要随机」，因为本函数主要给
#     「重排序」用：重排需要输出格式严格的一段 JSON，随机性越小越不容易出格式错误。
# 【为什么需要这个「非流式」函数（关键设计）】重排的返回是一段结构化结果（JSON），
#   我们必须拿到完整结果才能去解析；流式只会让人一边收一边猜「收齐了没有」，
#   徒增复杂度、没有任何好处。所以重排场景固定用非流式。
def complete_chat(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: int | None = None,
) -> str:
    """非流式一次性补全，返回完整文本。

    给「重排序」这类需要拿到完整结构化输出（如一段 JSON）的场景用：
    流式只会增加解析复杂度，没有任何收益。
    """
    # 【这行在干什么】取配置（同前）。
    s = settings or get_settings()
    # 【这行在干什么】造同步客户端（本函数是同步接口）。
    client = _client(s, timeout=timeout)
    # 【这行在干什么】发起非流式请求，统一异常处理。
    try:
        # 【这行在干什么】调用聊天接口拿完整响应。
        # 【关键参数】stream=False —— 明确告诉服务端「我不要流式，一次给全」。
        # 【注意】这里 temperature 直接透传（没有 `if None` 判断），因为它的默认值就是 0.0，
        #   调用方不传时天然得到 0.0，行为清晰。
        response = client.chat.completions.create(
            model=model or s.llm_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens or s.llm_max_tokens,
            stream=False,
        )
    except Exception as exc:
        # 【这行在干什么】把底层异常统一转成 LLMError（同前，保留因果链）。
        raise LLMError(f"调用大模型失败：{exc}") from exc

    # 【这行在干什么】检查响应里有没有「候选结果」，没有就直接报错。
    # 【为什么会没有】极端情况下服务端可能返回空结构；如果不检查，下面取 choices[0] 会越界报错，
    #   错误信息会很晦涩（IndexError），不如在这里给一个明确的说法。
    if not getattr(response, "choices", None):
        raise LLMError("大模型返回内容为空")

    # 【这行在干什么】取出第一个候选结果（choice），后面从它里面拿文本和结束原因。
    choice = response.choices[0]
    # 【这行在干什么】取出正文并去掉首尾空白。
    # 【写法拆解】`(choice.message.content or "").strip()`：
    #   · choice.message.content —— 正文内容，可能是 None；
    #   · or "" —— 若是 None（假值）就换成空字符串，避免对 None 调方法报错；
    #   · .strip() —— 去掉首尾的换行/空格，后面判断「是不是空的」更准。
    content = (choice.message.content or "").strip()
    # 【这行在干什么】如果正文是空的，抛出一个「带排查线索」的错误。
    # 【为什么这段错误信息写得这么啰嗦（血泪教训）】本项目真的踩过这个坑：
    #   重排时把 max_tokens 设成 200 太小，AI 把额度全花在「思考过程」上，还没写到正式答案
    #   就断了，于是 content 是空的，而错误信息里什么都不说，排查了很久。
    #   所以这里特意把 finish_reason（结束原因）一起带上，并直接给出解决建议：
    #   「推理模型会先把 max_tokens 用在思维链上，请调大 max_tokens」。
    # 【参数解释】`getattr(choice, 'finish_reason', None)` —— 安全取结束原因，
    #   常见值：stop（正常结束）、length（撞到 max_tokens 上限被截断）。
    if not content:
        raise LLMError(
            f"大模型返回内容为空（finish_reason={getattr(choice, 'finish_reason', None)}）。"
            "推理模型会先把 max_tokens 用在思维链上，请调大 max_tokens。"
        )
    # 【这行在干什么】返回完整正文，供调用方（如重排）去解析使用。
    return content