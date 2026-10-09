# ============================================================================
# 文件：retriever.py —— 「两路召回」的调度台与融合算法（RRF）
#
# 【这个文件在整个项目里是干什么的】
#   用户问一句话，系统怎么找到「和这句话相关的公告/项目」？这个文件负责整条链路的
#   前半段：它派两条检索通道同时去找（一条靠意思、一条靠字面），然后把两路的战果
#   用一套数学规则「融合、排序、去重、砍门槛」，最终产出一份干净的「候选名单」，
#   交给后面的大模型（RAG 环节）去组织答案。
#
# 【执行顺序：这个文件要「从上往下」读】
#   ① load_bm25()      —— 把上次建库时算好的 BM25 统计结果读回内存（带缓存）；
#   ② RetrievalResult  —— 检索结果的「包装盒」：装问题、命中的列表、两路各召回几条、重排情况；
#   ③ rrf_fuse()       —— 核心融合算法：把两路结果按「排名」合并打分、去重、过滤；
#   ④ retrieve()       —— 对外的主入口：把上面三样串起来，跑一次完整的混合检索。
#
# 【谁调用它（上游）】
#   backend/app/rag.py        —— RAG 问答：调 retrieve() 拿候选，再交给大模型作答。
#   backend/app/agent.py 等    —— Agent 的检索工具内部复用它（见 project 记忆里的
#                                 search_bidding_knowledge）。
#
# 【它给谁用（下游）】
#   rerank.py —— 拿 retrieve 的候选去做「重排序」（精排）；
#   rag.py / agent —— 拿融合后的 hits 作为参考来源（sources），生成带引用的回答。
#
# 【先懂的三个概念（大白话）】
#   1) 两路召回（召回 = 先把可能相关的都捞回来）：
#      · dense 路：用向量比「意思」，擅长语义相近（「道路施工」≈「市政工程」）；
#      · bm25 路：用关键词比「字面」，擅长精确命中（项目编号、公司全称、资质词）。
#      两路互补，缺一路检索质量都会明显下降，所以叫「混合检索」。
#   2) RRF（Reciprocal Rank Fusion，倒数排名融合）：
#      核心思想一句话：「在谁家排名靠前，谁就值得加分，而且不看重名次以外的分数」。
#      两路分数不是一个量纲（一个是 0~1 的余弦相似度，一个是可能大于 1 的 BM25 分），
#      直接相加没有意义；RRF 只看排名，天然规避了这个「量纲不同」的世纪难题。
#      公式与举例见 rrf_fuse 函数里的大段注释。
#   3) 排名（rank）：一条结果在两路里各自的第几名（1 是第一名、2 是第二名……）。
#      RRF 只看这个，不碰原始分数。
# ============================================================================

# 【下面是本文件原有的「模块说明」（docstring）】
# 【为什么放在这里】注释不算语句，所以放在这一大段注释之后，它依然是本模块的
#   第一条语句、依然是官方自我介绍，内容与原文一字不差。
"""混合检索：稠密向量召回 + BM25 稀疏召回，再用 RRF 融合。

为什么要融合两路：向量擅长语义（「道路施工招标」≈「市政工程采购公告」），
BM25 擅长精确词命中（项目编号、公司全称、特定资质词）。两者互补。

融合算法用 RRF（Reciprocal Rank Fusion）::

    score(d) = Σ_lists 1 / (k + rank_list(d))

RRF 只看排名不看原始分数，天然规避了「余弦相似度」与「BM25 分数」量纲不同的问题，
是混合检索里最稳、最少调参的做法。
"""

# 【这行在干什么】打开「类型注解延迟求值」开关。
# 【为什么这么写】下面用了 list[dict[str, Any]]、tuple[str, ...]、BM25Index | None
#   这类新式写法，老版本 Python（3.8/3.9）不认。加上这行后这些标注会先被当作字符串
#   存着、真正需要时才解析，于是老版本也能跑。
# 【注意事项】必须放在文件最顶部（所有执行代码之前），这是 Python 的硬性要求。
from __future__ import annotations

# 【这行在干什么】从 dataclasses 引入 dataclass 和 field 两个工具。
# 【为什么这么写】本文件用它们定义 RetrievalResult 这个「数据盒子」：
#   · dataclass 装饰器让 Python 自动生成构造函数等样板代码，省得手写 __init__；
#   · field(default_factory=list) 给「列表字段」设默认值（详见下方定义处注释）。
from dataclasses import dataclass, field

# 【这行在干什么】从 typing 引入 Any，用于类型标注。
# 【为什么需要】hits 里的字典内容五花八门（数字、字符串、嵌套字典……），
#   事先说不准，所以用 Any 兜住，也提醒读代码的人「这里不挑类型」。
from typing import Any

# 【这行在干什么】引入本项目的 BM25 索引类。
# 【为什么需要】加载 BM25 统计结果（load_bm25）以及 encode_query 编码用户问题。
from .bm25 import BM25Index

# 【这行在干什么】引入本项目的配置类和取配置函数。
# 【为什么这么写】各路召回数量、RRF 的 k 值、分数门槛……全部来自 .env，不写死在代码里。
# 【注意事项】开头的点「.」表示「同一个包里的模块」，不能脱离项目单独运行。
from .config import Settings, get_settings

# 【这行在干什么】引入向量模型包装器和它的单例获取函数。
# 【为什么需要】retrieve() 里要拿问题向量（embedder.embed_query）。
from .embeddings import Embedder, get_embedder

# 【这行在干什么】引入向量库类和它的单例获取函数。
# 【为什么需要】retrieve() 里要执行两条检索通道（store.search_dense / search_sparse）。
from .vector_store import VectorStore, get_store

# 【这行在干什么】声明一个模块级的缓存字典，用来缓存「已加载的 BM25 索引」。
# 【为什么需要缓存（重要性能设计）】BM25 索引文件可能很大（几 MB 的 JSON），
#   里面装着几千个词、几十万条的统计数字。如果用户每问一个问题就去读盘解析一次，
#   每次都花几百毫秒，纯属浪费。所以设计成「第一次加载后放这里，以后直接复用」。
# 【类型解释】dict[str, tuple[float, BM25Index]]：
#   · 键（str）—— BM25 索引文件的路径（字符串）；
#   · 值（tuple[float, BM25Index]）—— 一个元组，装着两样东西：
#     第一样 float 是「文件的最后修改时间」；第二样是「BM25 索引对象本身」。
# 【为什么要同时记住修改时间（关键，别删）】因为建库之后索引文件会被重新生成。
#   如果只缓存「对象」而不记时间，服务就会一直用「老的那份统计」，新数据永远检索不到。
#   每次提问都对比一下「当前文件修改时间」和「缓存里记的时间」，变了就重载，
#   既省事又保证不落后。这个技巧叫「按文件修改时间的失效缓存」。
_bm25_cache: dict[str, tuple[float, BM25Index]] = {}


# 【这行在干什么】定义函数 load_bm25：加载 BM25 索引，带缓存。
# 【参数解释】settings=None 表示调用方可以不传配置，不传就用 get_settings() 从 .env 读。
#   传了就用传进来的（测试时可以塞假配置）。
# 【返回值】一个已就绪（或至少可用的）BM25Index 对象。
# 【为什么单独拆个函数】因为「读缓存 → 比较时间 → 决定要不要重载」这串逻辑
#   可能被多个地方调用，拆出来避免重复代码，也便于单独测试。
def load_bm25(settings: Settings | None = None) -> BM25Index:
    """加载 BM25 索引，按文件修改时间做缓存，避免每次提问都解析大 JSON。"""
    # 【这行在干什么】拿到真正要用的配置对象，记为 s（短名字，方便下面反复用）。
    # 【参数解释】settings or get_settings() 意思是「传了就用传的；没传（None 算假）
    #   就调用 get_settings() 从 .env 读一份」。这是常见的「不传就用默认」写法。
    # 【为什么写成 or 而不是 if/else】一行搞定，可读性好；而且 get_settings() 自带缓存，
    #   反复调用不重复读盘，用着很放心。
    s = settings or get_settings()
    # 【这行在干什么】取出 BM25 索引文件的路径（一个 Path 对象）。
    # 【参数解释】s.bm25_index_path 来自 .env（BM25_INDEX_PATH），存盘时写在哪个文件，
    #   这里就从这个文件读。
    path = s.bm25_index_path
    # 【这行在干什么】打开「接住岔子」的网，去取文件的最后修改时间。
    # 【为什么取修改时间可能失败】文件可能还根本不存在（比如还没建过库），
    #   或者没有权限读取。这都算「拿不到时间」，处理方式见下面。
    try:
        # 【这行在干什么】取文件的修改时间戳（一个浮点数，表示「最后一次被改写的时刻」）。
        # 【参数解释】path.stat() 是取文件的各种元信息；.st_mtime 是其中「最后修改时间」。
        # 【为什么用修改时间当「版本号」】建库时 BM25 索引文件会被重新生成，
        #   每次生成的修改时间必然不一样。于是「文件是否变了」几乎等价于「索引是否需要重载」。
        #   用一个时间戳就能判断，比每次重新解析文件内容要便宜得多。
        mtime = path.stat().st_mtime
    # 【这行在干什么】接住「取不到时间」这类错误，绑定到 exc 上（这里其实没用它）。
    except OSError:
        # 【这行在干什么】把时间记为 -1.0。
        # 【为什么用 -1 这个怪数字】它是个「不可能出现的真实时间戳」（文件系统的时间
        #   永远不会是负数），所以缓存里一旦存了 -1，将来无论文件变成什么样，
        #   时间都对不上，于是每次都老老实实重载。也就是说：文件不存在时，缓存自动失效，
        #   每次提问都尝试重载一次 —— 这在「还没建库」的阶段是正确且必要的
        #   （等库建好了文件出现，自然就能缓存上了）。
        mtime = -1.0

    # 【这行在干什么】用文件路径当缓存字典的键。
    # 【为什么用路径当键】因为同一个程序只处理一个 BM25 文件，路径天然唯一，
    #   用来定位缓存条目最合适。将来若支持多套索引，这个键也能区分开。
    key = str(path)
    # 【这行在干什么】从缓存字典里按路径取出「(时间, 索引)」这一组，记为 cached。
    # 【为什么可能是 None】第一次调用、或缓存被清掉时，字典里没有这个键，
    #   用 .get(key) 会安静地返回 None（而不是报错）。
    cached = _bm25_cache.get(key)
    # 【这行在干什么】命中缓存的判断：缓存存在，且「缓存里记的修改时间」等于「当前时间」。
    # 【为什么两个条件缺一不可】①cached is not None —— 缓存得先存在；
    #   ②cached[0] == mtime —— 文件没变过。只有两个都满足，缓存里的索引才是「最新的」，
    #   可以放心直接复用。
    # 【参数解释】cached[0] 就是缓存的元组里那个 float（旧时间），mtime 是刚拿的新时间。
    if cached is not None and cached[0] == mtime:
        # 【这行在干什么】返回缓存里那个索引对象，省去重新读盘解析。
        # 【参数解释】cached[1] 是缓存的元组里第二个元素，即 BM25 索引对象。
        return cached[1]

    # 【这行在干什么】走到这里说明「没有缓存」或「文件变了」，于是真的重新加载索引。
    # 【参数逐个解释】
    #   BM25Index.load(path, s.bm25_k1, s.bm25_b) —— 这是一个「类方法」：
    #     不需要先创建实例，直接用类名就能调。它读文件、按里面的记录重建索引对象。
    #     · path —— 从哪个文件读；
    #     · s.bm25_k1 —— 如果文件里没有记录 k1（老版本文件），用什么默认值顶替；
    #     · s.bm25_b  —— 同上，b 参数的默认值。
    #     这三个值拼在一起，保证「建库参数」和「检索参数」口径一致（不一致会打分错乱）。
    # 【为什么 load 失败也不会炸】BM25Index.load 内部对「文件不存在/内容损坏」做了兜底，
    #     返回一个「空的但可用」的索引（这就是记忆里那个「load 失败返回空索引」的坑）。
    #     空索引的 ready=False，retrieve() 里就只会走 dense 一路，服务不会整体挂掉。
    index = BM25Index.load(path, s.bm25_k1, s.bm25_b)
    # 【这行在干什么】把「新时间 + 新索引」这一组存回缓存字典，替换旧的。
    # 【为什么这么写】_bm25_cache[key] = (mtime, index) 是「往字典的 key 位置写入值」，
    #   旧值自动被覆盖。这样下次提问就直接命中缓存了。
    _bm25_cache[key] = (mtime, index)
    # 【这行在干什么】把刚加载的索引返回出去。
    return index


# 【这行在干什么】@dataclass 给下面的 RetrievalResult 类贴标签，让 Python 自动生成构造函数。
# 【为什么用 dataclass 而不是手写类】省样板代码：字段写出来就有构造函数，
#   一目了然、不容易漏。参见 documents.py 里对 dataclass 的详细解释。
# 【这个类是什么】它是「一次检索结果」的包装盒。retrieve() 把结果装进这个盒子里返回，
#   调用方（rag/agent）从这个盒子取出题目、命中列表、两路数量、重排情况等。
# 【为什么需要它而不直接返回一个列表】因为一次检索的「副产品」很多（两路各命中几条、
#   BM25 就绪没有、重排结果如何），用列表装不下；用一个结构化的小盒子，语义最清楚。
@dataclass
class RetrievalResult:
    # 【这行在干什么】声明字段 question：用户这次问的问题（字符串）。
    # 【为什么检索结果还要回带问题】调用方需要确认「这次检索是对应哪个问题」的，
    #   尤其在前端并发多个问题时，能避免拿错结果。也便于打日志排查。
    question: str
    # 【这行在干什么】声明字段 hits：最终融合排序后的命中列表。
    # 【类型解释】list[dict[str, Any]]：一个列表，里面每个元素是一份「命中」字典。
    # 【为什么默认值是 field(default_factory=list) 而不是 = []（重要坑）】
    #   在 Python 里写 `hits: list = []` 会让「所有实例共用同一个列表」——初学者经典陷阱。
    #   因为默认值只在类定义时创建一次，之后每个实例都指向这同一个列表，改一个全被改。
    #   field(default_factory=list) 的意思是「每次新建实例时，现场调用一次 list() 造一个
    #   全新的空列表」。这样每个实例都有自己的独立一份。
    # 【注意事项】凡是默认值是「可变对象」（列表、字典、集合）的字段，都必须用 default_factory。
    hits: list[dict[str, Any]] = field(default_factory=list)
    # 【这行在干什么】声明字段 dense_count：稠密（语义）这一路各召回了多少条。
    # 【为什么需要】前端徽标要显示「两路各命中几条」，方便判断检索行为是否正常。
    dense_count: int = 0
    # 【这行在干什么】声明字段 sparse_count：BM25（关键词）这一路各召回了多少条。
    sparse_count: int = 0
    # 【这行在干什么】声明字段 bm25_ready：BM25 索引是否就绪（可用的布尔值）。
    # 【为什么需要】如果 BM25 没拟合（空知识库/未建库），sparse 那一路根本不会跑，
    #   前端徽标要根据它说明「只有语义一路在工作」。
    bm25_ready: bool = False
    # 【这行在干什么】声明字段 rerank_meta：重排序的执行情况，默认空字典。
    # 【重要注释（原文如此，保留）】重排序的执行情况（由 rerank.rerank_hits 回填，
    #   未重排时为空字典）。
    # 【为什么需要】后端要能告诉前端「重排用了多久、消耗多少 token、结果几条→几条」，
    #   前端据此展示徽标、以及重排失败时的红色警告。这份元数据就装在这里，
    #   由 rerank.py 在重排时回填进来。
    rerank_meta: dict[str, Any] = field(default_factory=dict)

    # 【这行在干什么】@property 把下面的 empty 方法包装成「属性」。
    # 【通俗解释】有了它，调用方写 result.empty 就能拿到「是不是空」，
    #   不用写 result.empty()。
    # 【为什么需要这个属性】「检索结果是不是空的」这个问题被问得非常频繁
    #   （空了要回「暂无数据」，不空要走答案生成），给它一个专门的属性，
    #   比让调用方自己写 not result.hits 更直观，也集中了一处逻辑。
    @property
    def empty(self) -> bool:
        # 【这行在干什么】返回「命中列表是不是空的」。
        # 【为什么用 not self.hits】在 Python 里，空列表算「假」，非空列表算「真」，
        #   所以 not self.hits 就是「没有命中 → 真」。
        # 【注意事项】这里判断的是「融合后」的 hits（已经过滤、截断过的），
        #   不是两路各自的原始数量 —— 融合后为空，才算真正没有可用来源。
        return not self.hits

    # 【这行在干什么】定义方法 to_sources：把命中列表转成「前端参考来源卡片」需要的精简结构。
    # 【参数解释】text_limit=900 表示正文最多带 900 个字符（多了截断）。
    # 【为什么需要这个转换】前端要展示「命中的原文」卡片，但卡片需要的信息比 hits 里的
    #   原始字段少得多，而且正文太长会撑爆响应。这里按前端约定「精洗」成一份专门的结构，
    #   相当于给前端定制了一份「好看版」的数据。
    # 【返回值】一个列表，每个元素是「一张卡片」所需的字典。
    def to_sources(self, text_limit: int = 900) -> list[dict[str, Any]]:
        """转成前端「参考来源」卡片所需的精简结构。"""
        # 【这行在干什么】准备一个空列表，用来装「卡片」。
        sources: list[dict[str, Any]] = []
        # 【这行在干什么】遍历融合后的命中列表，一条条处理。
        for hit in self.hits:
            # 【这行在干什么】取出这一条的重排名次（如果有的话）。
            # 【为什么可能没有】只有「重排过」的结果才会带 rerank_rank 这个字段；
            #   没重排过的，.get 会安静地返回 None。
            # 【参数解释】hit.get("rerank_rank") 不带第二个参数，表示
            #   「有就取，没有就返回 None」。
            rerank_rank = hit.get("rerank_rank")
            # 【这行在干什么】把这一条「精洗」成一张卡片所需的内容，追加进列表。
            # 【为什么用 .get 而不是 [键]】因为某些字段（如 rerank_rank）可能不存在，
            #   用方括号取值会直接报错；用 .get 带默认值则永远安全。
            sources.append(
                {
                    # 【这行在干什么】文档编号（表:主键:块序号），供定位。
                    "doc_id": hit["doc_id"],
                    # 【这行在干什么】来自哪张表（英文表名）。
                    # 【为什么这里用方括号而不是 .get】doc_id/table/label 这几个字段
                    #   是 rrf_fuse 生成的「必有字段」（见 rrf_fuse 里的 item 初始化），
                    #   一定存在，用方括号取值既简洁又能「早发现 bug」——万一哪天上游
                    #   没给这几个字段，这里立刻报错，而不是悄悄显示空白。
                    "table": hit["table"],
                    # 【这行在干什么】表的中文展示名。
                    "label": hit["label"],
                    # 【这行在干什么】主键值，用来精确指向原始记录。
                    "primary_key": hit["primary_key"],
                    # 【这行在干什么】第几块（从 0 开始），默认 0。
                    "chunk_index": hit.get("chunk_index", 0),
                    # 【这行在干什么】一共几块，默认 1。
                    "chunk_total": hit.get("chunk_total", 1),
                    # 【这行在干什么】RRF 融合分，四舍五入到 6 位小数。
                    # 【为什么 round(..., 6)】原始分数是很多位的小数，传到前端既占空间又难看；
                    #   保留 6 位足够展示「谁的融合分更高」了。
                    "rrf_score": round(hit.get("rrf_score", 0.0), 6),
                    # 【这行在干什么】把「每一路各自的原始分」也带上，同样四舍五入到 6 位。
                    # 【读法（字典推导式）】`{k: round(v, 6) for k, v in 字典.items()}` 是
                    #   「把字典里每个键值对 k/v 都套一层 round，生成一个新字典」。
                    #   【为什么前端需要原始分】能展示「这条是靠语义（0.87）还是靠关键词（12.3）命中的」，
                    #   对理解检索行为很有用。
                    "scores": {k: round(v, 6) for k, v in hit.get("scores", {}).items()},
                    # 【这行在干什么】把「每条在每一路的第几名」也带上（排名，不是分数）。
                    # 【为什么需要排名】前端可以据此展示「语义第 2、关键词第 5」，
                    #   也能协助调试 RRF 融合是否合理。
                    "ranks": hit.get("ranks", {}),
                    # 【这行在干什么】这一条被哪几路命中（如 ["dense", "bm25"]）。
                    # 【为什么需要】前端可以显示「双路命中」徽标，也是一种质量信号。
                    "matched_by": hit.get("matched_by", []),
                    # 【这行在干什么】正文，且只取前 text_limit 个字符。
                    # 【为什么截断（重要）】①响应体积：几千字的公告全带会让接口响应变大；
                    #   ②前端卡片空间有限；③真正生成答案时，模型需要的只是「命中内容」
                    #   而不是全文。所以这里统一截断。
                    # 【注意事项】截断是「硬截断」（一刀切），可能在句子中间断开，
                    #   但对「展示参考」来说完全可接受。
                    "text": (hit.get("text") or "")[:text_limit],
                    # 【这行在干什么】原始字段展示副本（前端可渲染成小表格）。
                    "fields": hit.get("fields", {}),
                    # 【这行在干什么】重排名次（1 起，1 = 重排后最相关），没有就 None。
                    # 【重要注释（原文如此，保留）】仅重排过的结果才有该字段（1 起；
                    #   1 = 重排后最相关）。
                    "rerank_rank": rerank_rank,
                }
            )
        # 【这行在干什么】把装好卡片的结果列表返回出去。
        return sources


# 【这行在干什么】定义函数 rrf_fuse：RRF 融合多路召回结果。
# 【参数逐个解释】
#   result_lists —— 多路召回结果，类型是「列表套元组」：
#     每个元组是 (来源名, 这一路的命中列表)。例如
#     [("dense", [命中1, 命中2, ...]), ("bm25", [命中1, 命中2, ...])]。
#   rrf_k        —— 公式里的常数 k（调和参数），作用见函数体内。
#   final_k      —— 融合后最多保留几条（截断数）。
#   dense_min_score —— 稠密一路的分数门槛（默认 0，表示不过滤），作用见函数体内。
# 【返回值】融合、排序、去重后的最终命中列表。
# 【什么是 RRF（重要，先看这里）】
#   公式：score(某条) = 1/(k+它在dense路的排名) + 1/(k+它在bm25路的排名)
#   —— 即「每在一路出现一次，就按它的名次加一次分」；名次越靠前、加的越多。
#   举例（k=60）：一条结果两路都是第 1 名，得分 1/61+1/61≈0.0328；
#   另一条只在 dense 路排第 10，得分 1/70≈0.0143。前者明显更高。
#   因为分母里有 k，名次再靠前也只加「有限的分」，这就是「只看排名、不看原始分」。
#   它天然规避了「余弦相似度（0~1）」与「BM25 分（可能很大）」量纲不同的老难题。
def rrf_fuse(
    result_lists: list[tuple[str, list[dict[str, Any]]]],
    rrf_k: int,
    final_k: int,
    dense_min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """RRF 融合多路召回结果，同一 doc_id 会被合并并累计得分。

    ``dense_min_score`` 是召回门槛：余弦相似度天然会给每个问题都返回 top-k，
    哪怕问的是完全无关的内容。因此约定「被 BM25 命中」或「向量相似度达标」
    才算有效来源，避免把无关公告硬塞给大模型导致编造答案。
    设为 0 可关闭该过滤。
    """
    # 【这行在干什么】准备一个空字典 merged，用来「按文档合并」所有路的命中。
    # 【类型解释】dict[str, dict[str, Any]]：键是文档标识（doc_id 或 point_id），
    #   值是「合并后的命中字典」。为什么要按文档合并？因为同一条文档可能同时被两路命中，
    #   如果不去重，它会出现两次，既重复又算双份分。合并后它只出现一次、分数累加。
    merged: dict[str, dict[str, Any]] = {}

    # 【这行在干什么】外层循环：一路一路地处理。
    # 【参数解释】for source, hits in result_lists —— 每圈解包一个元组：
    #   source 是来源名（如 "dense"），hits 是这一路的结果列表。
    for source, hits in result_lists:
        # 【这行在干什么】内层循环：一条一条地处理这一路的结果。
        # 【参数解释（enumerate，重点）】
        #   enumerate(hits, start=1) 会在遍历时自动给每条编号：
        #     · rank —— 编号（第几条），从 1 开始。这正是「排名」：第一条排名 1、第二条排名 2……
        #     · hit —— 当前这条命中的字典。
        #   【为什么 start=1 而不是默认的 0】因为「排名」习惯上从第 1 名开始数；
        #     RRF 公式里的 rank 必须是 1、2、3…… 否则公式算出来会偏。
        #   【为什么说排序是隐含前提】这一路的列表本来就是数据库按相似度从高到低排好的，
        #     所以「第几条」就是「第几名」。
        for rank, hit in enumerate(hits, start=1):
            # 【这行在干什么】取这一条的「文档标识」用来当合并的钥匙。
            # 【为什么优先 doc_id】doc_id（如 bidding_notices:123:0）是我们自己的业务编号，
            #   语义清晰；万一它缺失（理论上不会），退回用 point_id（UUID）兜底。
            # 【这行在读什么】`A or B` = 「A 有值就用 A，否则用 B」。
            key = hit.get("doc_id") or hit.get("point_id")
            # 【这行在干什么】从合并字典里取出这条文档目前的合并记录。
            # 【为什么可能是 None】这是这条文档第一次被遇到（还没合并过），
            #   此时字典里没有它，.get 返回 None。
            item = merged.get(key)
            # 【这行在干什么】如果这是第一次遇到这条文档，就给它「建档」。
            if item is None:
                # 【这行在干什么】造一条全新的合并记录，字段先给默认值，分数先记 0。
                # 【为什么字段这么多】因为前端卡片需要的所有信息都要在这个盒子里备齐
                #   （doc_id、表名、中文名、主键、第几块、正文、原始字段、各路分数、排名、
                #   命中来源、RRF 分）。后面每一路见到它时，只需要往里补自己的分数和排名即可。
                item = {
                    "doc_id": hit.get("doc_id", ""),
                    "table": hit.get("table", ""),
                    "label": hit.get("label", ""),
                    "primary_key": hit.get("primary_key"),
                    "chunk_index": hit.get("chunk_index", 0),
                    "chunk_total": hit.get("chunk_total", 1),
                    "text": hit.get("text", ""),
                    "fields": hit.get("fields", {}),
                    "scores": {},
                    "ranks": {},
                    "matched_by": [],
                    "rrf_score": 0.0,
                }
                # 【这行在干什么】把这条新记录登记进合并字典，供后面继续累计。
                # 【注意事项】必须在这里就放进去，否则「同一文档第二次遇到」时
                #   merged.get(key) 还是 None，会再次建档、覆盖掉第一次的累计。放了之后
                #   第二次遇到就能取到同一份记录接着累加了。
                merged[key] = item
            # 【这行在干什么】把这一路的原始分数记进「scores 字典」，键是来源名。
            # 【为什么 scores 要按来源分键】因为 dense 分和 bm25 分不是一个量纲，
            #   必须分开存、分开看，前端才能展示「这一路给了它多少分」。
            # 【参数解释】float(hit.get("score", 0.0))：取这一路的分数，转成浮点数；
            #   没有就给 0。
            item["scores"][source] = float(hit.get("score", 0.0))
            # 【这行在干什么】把「这一路里它的排名」记进 ranks 字典，键也是来源名。
            # 【为什么需要排名】①前端展示；②调试 RRF 时能看到「它在哪路排第几」。
            item["ranks"][source] = rank
            # 【这行在干什么】把来源名追加进「命中来源列表」（matched_by）。
            # 【为什么叫 matched_by】意思就是「它被谁命中了」。第一次 append 后列表可能
            #   变成 ["dense"]；两路都命中则变成 ["dense", "bm25"]。注意此时还没有去重
            #   （去重放在最后统一做，见下面）。
            item["matched_by"].append(source)
            # 【这行在干什么】把这一路的 RRF 贡献分累加到总分上。
            # 【公式拆解（重要）】1.0 / (rrf_k + rank)：
            #   · rank —— 这条文档在这一路的排名（1、2、3……）；
            #   · rrf_k + rank —— 分母。为什么分母里要有 k？因为如果直接写 1/rank，
            #     第 1 名得 1 分、第 2 名得 0.5 分，前两名差距巨大，融合结果几乎被第一名
            #     垄断，很不稳定。加上 k（比如 60）后，第一名 1/61≈0.016、第二名 1/62≈0.016，
            #     差距被压缩得很温和，各路之间更均衡 —— 这叫「阻尼（damping）」，
            #     k 越大越温和、越小越激进。
            #   · += —— 「累加」：这条文档每出现在一路，就加一次贡献。
            #     两路都出现且都靠前，总分自然最高。这正是「两路命中比一路命中更可信」的落地。
            item["rrf_score"] += 1.0 / (rrf_k + rank)

    # 【这行在干什么】把合并字典里的所有记录拿出来，按 RRF 总分从高到低排序。
    # 【参数逐个解释】
    #   merged.values() —— 所有合并后的记录（去掉了键，只要内容）；
    #   key=lambda x: x["rrf_score"] —— 排序依据：取每条记录的 rrf_score 字段。
    #     【专业词解释：lambda】一个「用完即丢的小函数」，相当于
    #     def 取分(x): return x["rrf_score"]，只是不占名字、只写一行。
    #   reverse=True —— 从大到小（分高的排前面）。为什么？我们要的是「最相关的排最前」，
    #     RRF 分越高说明越相关，所以要降序。
    ordered = sorted(merged.values(), key=lambda x: x["rrf_score"], reverse=True)
    # 【这行在干什么】遍历排序后的每条记录，把 matched_by 列表整理成「去重且排序」的样子。
    # 【为什么要这一步】上面累计时是「每见一路就 append 一次」，同一条文档不会被同一路
    #   遇到两次，但为了保险起见（万一上游把同一条重复送了两遍），还是统一处理一下：
    #   set(...) 先去重（去掉重复的来源名），sorted(...) 再按字母序排整齐
    #   （这样 ["dense","bm25"] 恒等于 ["bm25","dense"]，前端展示不会时此时彼）。
    for item in ordered:
        item["matched_by"] = sorted(set(item["matched_by"]))

    # 【这行在干什么】如果设了稠密分数门槛（dense_min_score > 0），就执行过滤。
    # 【为什么需要这个门槛（重要设计）】稠密检索有个毛病：不管用户问得多离谱，
    #   余弦相似度也总能从库里「矬子里拔将军」选出 top-k，凑数给你。
    #   于是明明库里没有相关内容，也会硬塞几条最像的给大模型，大模型就可能据此编造答案。
    #   因此约定一条纪律：只有「被 BM25 精确命中」或「向量相似度真的达标」的记录，
    #   才算有效来源；两条都不满足的，直接请出去。
    # 【为什么默认值是 0（即默认关闭）】门槛是调参旋钮，不同数据集的合理值不同，
    #   由 .env（DENSE_MIN_SCORE）决定；默认 0 表示「先不过滤」，交给配置去开。
    if dense_min_score > 0:
        # 【这行在干什么】用列表推导式重建 ordered，只保留「合格」的记录。
        # 【条件拆解（读作「或」逻辑）】
        #   "bm25" in item["scores"] —— 它被 BM25 命中了（scores 里有 bm25 这个键）。
        #     BM25 只会在「问题里有真实词命中」时才给分，所以「被它命中」本身就是
        #     一条很强的质量信号，直接放行。
        #   或 item["scores"].get("dense", 0.0) >= dense_min_score —— 它没被 BM25 命中，
        #     但稠密分达到了门槛（比如 0.3），说明语义上确实够像，也算合格。
        # 【为什么用「或」而不是「与」】两路是互补的：只要有一路证明它靠谱，就值得留下。
        # 【注意事项】这个过滤在排序之后进行，被过滤掉的记录直接从结果里消失，
        #   不会影响剩下记录的相对顺序。
        ordered = [
            item
            for item in ordered
            if "bm25" in item["scores"]
            or item["scores"].get("dense", 0.0) >= dense_min_score
        ]

    # 【这行在干什么】只返回前 final_k 条，并作为最终结果返回。
    # 【读法（切片）】ordered[:final_k] 是「取列表从开头到第 final_k 个位置之前的元素」，
    #   也就是前 final_k 条。为什么要截断？因为下游（rerank/生成）处理的条数有限，
    #   候选太长既费 token 又费时间，截到合理数量即可。
    # 【注意事项】如果 ordered 不足 final_k 条（比如库里本来就没几条），切片会自动
    #   只给现有的，不会因为「不够」而报错 —— 这是切片语法的好处。
    return ordered[:final_k]


# 【这行在干什么】定义函数 retrieve：对外的主入口，跑一次完整的混合检索。
# 【为什么是「主入口」】上层（rag/agent）不直接调用 load_bm25/rrf_fuse 这些零件，
#   只调这一个函数，所有零件在这里被组装起来。这样上层代码保持简洁，
#   也方便未来增加「第三路召回」——只需要改这一个函数。
# 【参数逐个解释】
#   question   —— 用户的问题（字符串）。
#   settings   —— 配置对象；不传就用 .env（None 兜底）。
#   store      —— 向量库对象；不传就用全局单例。为什么要留参数？为了测试：测试时可以
#                传一个假的向量库进来，不碰真实数据库。
#   embedder   —— 向量模型包装器；不传就用全局单例（同理，为了测试可注入）。
#   bm25       —— BM25 索引；不传就自动加载。留参数同样是为了测试可注入。
#   final_k    —— 最终保留条数；不传就用配置里的默认值。为什么也要留参数？方便调用方
#                在特殊场景（比如只想取 3 条）时临时覆盖配置。
# 【返回值】一个 RetrievalResult 盒子（里面装着题目、命中、两路数量、BM25 状态）。
# 【一个很重要的承诺】知识库为空时，本函数不抛异常，返回「空结果」——
#   这是项目「支持空库运行」硬约束的具体落地。
def retrieve(
    question: str,
    settings: Settings | None = None,
    store: VectorStore | None = None,
    embedder: Embedder | None = None,
    bm25: BM25Index | None = None,
    final_k: int | None = None,
) -> RetrievalResult:
    """执行一次混合检索。知识库为空时返回空结果（不抛异常）。"""
    # 【这行在干什么】确定真正要用的配置对象，记为 s（短名字方便下面反复用）。
    # 【参数解释】settings or get_settings()：传了用传的，没传就从 .env 读。
    s = settings or get_settings()
    # 【这行在干什么】把问题「清洗」一下：去掉首尾空白。
    # 【为什么必须处理】用户提交的问题可能带着多余的空格、换行，
    #   不清洗的话这些空白会被当成问题内容的一部分（比如去算向量、去切词），
    #   既浪费又可能造成「空字符串问题」这种边界情况。
    # 【参数解释】(question or "") 先兜住 None（None 算假，取 ""），再 .strip() 去空白。
    question = (question or "").strip()
    # 【这行在干什么】如果清洗后的问题为空，直接返回一个「空结果」盒子。
    # 【为什么必须提前返回（关键）】空问题没有任何内容可检索；如果继续往下走，
    #   ①算向量可能出错（空文本丢给模型），②BM25 编码空文本也没意义。
    #   提前返回既省事又安全。
    # 【参数解释】RetrievalResult(question=question) 只填了问题，hits 用默认值空列表，
    #   其余字段也全是默认值。此时 result.empty 是 True，上层会回答「暂无数据」之类。
    if not question:
        return RetrievalResult(question=question)

    # 【这行在干什么】确定三个要用到的「工具对象」，原则都是「传了就用传的，没传就用单例」。
    # 【为什么写成 store = store or get_store(s) 而不是 if】因为「有值就保留，没有就创建」，
    #   用 or 一行搞定、逻辑更清晰（后面两行同理）。
    # 【参数逐个解释】
    #   get_store(s) —— 拿全局唯一的向量库客户端（本地模式必须单例，见 vector_store.py）；
    #   get_embedder(s) —— 拿全局唯一的向量模型包装器（懒加载，见 embeddings.py）；
    #   load_bm25(s) —— 加载 BM25 索引（带缓存，见文件头部）。
    # 【为什么 bm25 这行写法跟别人不一样】因为 bm25 的默认动作是「加载」，
    #   而「加载」可能返回一个未就绪（ready=False）的空索引 —— 所以这里用
    #   `bm25 if bm25 is not None else load_bm25(s)` 而不是 or：
    #   万一调用方真的传了一个「空索引」进来（它不为 None，但 ready=False），
    #   or 的写法会把它当成「假」而忽略掉、重新加载一份 —— 传了个寂寞。
    #   用 is not None 判断则能区分「没传」和「传了空索引」，尊重调用方。
    store = store or get_store(s)
    embedder = embedder or get_embedder(s)
    bm25 = bm25 if bm25 is not None else load_bm25(s)

    # 【这行在干什么】准备一个空列表，用来装第一路（稠密/语义）的召回结果。
    # 【为什么注释写「第一路」】这是两路召回里的第一路，下面紧接着是第二路。
    dense_hits: list[dict[str, Any]] = []
    # 【这行在干什么】打开「接住岔子」的网，去跑语义召回。
    # 【为什么这里必须 try（重要）】向量模型可能加载失败（下载失败、模型名错）、
    #   算向量可能出问题。如果直接让它抛出去，一次提问就整体失败，
    #   可明明是「检索的一个配件坏了」，不该让整个问答都瘫痪。
    try:
        # 【这行在干什么】把用户问题算成查询向量。
        # 【参数解释】embedder.embed_query(question) —— 调 embeddings.py 里那个
        #   「查询专用」的向量方法（会自动加 bge 查询前缀，见 embeddings.py）。
        query_vector = embedder.embed_query(question)
        # 【这行在干什么】拿这个向量去向量库做语义检索。
        # 【参数解释】store.search_dense(query_vector, s.retrieve_dense_top_k)：
        #   返回按相似度降序排好的若干条命中（每条 source 是 "dense"）；
        #   s.retrieve_dense_top_k 来自 .env（RETRIEVE_DENSE_TOP_K），
        #   控制「语义这一路先召回多少条」—— 召回得多、融合时可选的就多，
        #   但越多后面处理和传输的负担也越大，是个平衡值。
        dense_hits = store.search_dense(query_vector, s.retrieve_dense_top_k)
    # 【这行在干什么】接住上面语义召回过程中的任何异常。
    except Exception:
        # 【这行在干什么】把 dense_hits 置回空列表。
        # 【为什么这样处理（降级设计）】重要注释（原文如此，保留）：
        #   向量模型不可用时仍可指望 BM25，保证服务不整体挂掉。
        #   意思就是：语义这路坏了没关系，还有关键词这路兜底；两路都坏，那也是返回
        #   「空结果」而不是让服务崩溃。这是「单点故障不拖垮全局」的容错思路。
        dense_hits = []

    # 【这行在干什么】准备一个空列表，用来装第二路（BM25/关键词）的召回结果。
    sparse_hits: list[dict[str, Any]] = []
    # 【这行在干什么】只有当 BM25 索引「就绪」时才跑关键词这一路。
    # 【为什么必须判断 ready（重要）】BM25 索引可能因为「还没建库/文件损坏/加载失败」
    #   而处于「空壳」状态（ready=False）。此时拿它去编码问题，会得到一堆没意义的空值，
    #   甚至编码结果与词表对不上而闹出张冠李戴。判断 ready 后，就自然跳过这一路，
    #   让检索只走语义一路 —— 服务照常工作，只是少了一条腿。
    if bm25.ready:
        # 【这行在干什么】把用户问题编码成「稀疏向量」（下标 + 权重两串数字）。
        # 【参数解释】bm25.encode_query(question) —— 问题分词后，对照词表给每个词
        #   查编号、算权重（这就是 bm25.py 的 ⑥ encode_query）。返回
        #   (indices, values)：indices 是词编号列表，values 是权重列表。
        # 【为什么权重能直接这么算】encode_query 内部用的是和建库时相同的
        #   k1/b/词表统计，所以「同一口径」的向量才能在库里比出对的分数。
        indices, values = bm25.encode_query(question)
        # 【这行在干什么】拿稀疏向量去向量库做关键词检索。
        # 【参数解释】store.search_sparse(indices, values, s.retrieve_sparse_top_k)：
        #   返回按 BM25 分降序的若干条命中（每条 source 是 "bm25"）；
        #   s.retrieve_sparse_top_k 来自 .env（RETRIEVE_SPARSE_TOP_K），
        #   控制关键词这一路先召回多少条。
        sparse_hits = store.search_sparse(indices, values, s.retrieve_sparse_top_k)

    # 【这行在干什么】把两路结果交给 RRF 融合，得到最终排序的命中列表。
    # 【参数逐个解释】
    #   [("dense", dense_hits), ("bm25", sparse_hits)] —— 两路的「(来源名, 结果)」清单。
    #     【为什么要用元组包来源名】rrf_fuse 需要知道每一条来自哪一路（记入 scores/ranks/
    #     matched_by），这个来源名就是 "dense" / "bm25"。
    #     【为什么把 dense 放前面】约定俗成的展示顺序，也便于阅读；融合算法不偏袒位置。
    #   rrf_k=s.rrf_k —— RRF 公式里的阻尼常数，来自 .env（RRF_K）。
    #   final_k=final_k or s.retrieve_final_top_k —— 最终保留条数：
    #     调用方传了就用传的（final_k 不为 None），没传就用配置的默认值。
    #     【为什么用 or 而不是 is not None】两者几乎等价（None 算假），这里用 or 更短；
    #     唯一的理论差异是「传了 0」会被当成没传 —— 但 final_k=0 本来就没有意义，
    #     所以可接受。
    #   dense_min_score=s.dense_min_score —— 稠密分门槛，来自 .env（DENSE_MIN_SCORE）。
    hits = rrf_fuse(
        [("dense", dense_hits), ("bm25", sparse_hits)],
        rrf_k=s.rrf_k,
        final_k=final_k or s.retrieve_final_top_k,
        dense_min_score=s.dense_min_score,
    )

    # 【这行在干什么】把这次检索的完整结果装进盒子返回。
    # 【参数逐个解释】
    #   question=question —— 清洗过的问题（不是原始输入，用于回显）；
    #   hits=hits —— 融合后的最终命中；
    #   dense_count=len(dense_hits) —— 语义一路召回了多少条（融合前的数）；
    #   sparse_count=len(sparse_hits) —— 关键词一路召回了多少条（融合前的数）；
    #   bm25_ready=bm25.ready —— BM25 索引是否就绪，前端徽标要用。
    # 【为什么要把「融合前的数量」也带上】两路可能召回 20 条、最终只留 5 条；
    #   前端徽标显示「召回 20 条 → 保留 5 条」能让用户理解检索在做什么，
    #   而「融合前数量」正是计算这个比值的关键数字。
    return RetrievalResult(
        question=question,
        hits=hits,
        dense_count=len(dense_hits),
        sparse_count=len(sparse_hits),
        bm25_ready=bm25.ready,
    )