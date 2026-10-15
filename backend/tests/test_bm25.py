# ============================================================================
# 文件：tests/test_bm25.py —— 给「关键词检索引擎」做体检
#
# 【这个测试文件整体在测什么】
#   被测对象是 backend/app/bm25.py，它是两路召回里的「关键词这一路」。
#   这里只测它的「纯计算」部分：分词、打分公式、空语料兜底、存档读档。
#   全程不连数据库、不连向量库、不调大模型 —— 这些函数本来就只吃「词列表」。
# ============================================================================

from __future__ import annotations

# 【为什么需要 math】测试里要用 math.log 手算一遍 BM25 的期望分数，拿来跟代码对比。
import math

# 【为什么需要 pytest】用 pytest.approx 比较浮点数（浮点计算会有极小误差，
#   不能用 == 直接比，否则会「明明算对了却判失败」）。
import pytest

# 【为什么这么导入】conftest.py 已经把 backend/ 加进搜索路径，
#   所以这里能直接写 `from app.bm25 import ...`（app 就是 backend/app 这个包）。
from app.bm25 import BM25Index, tokenize


def test_tokenize_splits_chinese_and_lowercases_english():
    """分词：中文要切成有意义的词，英文要统一转成小写。"""
    tokens = tokenize("招标公告 Python")
    # 【为什么断言这三个词】它们是常见词，jieba 的默认词典一定能切出来，稳定可靠。
    assert "招标" in tokens
    assert "公告" in tokens
    # 【为什么断言小写形式】源码里做了 lower()，目的就是让「Python/PYTHON/python」
    #   被视为同一个词；测这一条等于把这个「归一化」约定钉死。
    assert "python" in tokens


def test_tokenize_drops_pure_punctuation():
    """分词：纯标点必须被过滤掉，且结果里不留首尾空白。"""
    tokens = tokenize("招标，公告！（已结束）")
    # 【为什么要专门测标点】标点进词表只会让索引变大、检索变慢，没有任何检索价值，
    #   这是源码 _TOKEN_KEEP 白名单的职责，必须守住。
    for punct in ["，", "！", "（", "）"]:
        assert punct not in tokens
    assert "招标" in tokens and "公告" in tokens
    # 【为什么检查首尾空白】源码里做了 strip()，防止「空格」被当成一个词混进词表。
    assert all(token == token.strip() for token in tokens)


def test_tokenize_empty_input_returns_empty_list():
    """分词：空输入要返回空列表（而不是报错或 None）。"""
    # 【为什么测这个边界】上游（建库/检索）可能传进来空字符串，
    #   源码开头就做了「防御性编程」提前返回 []，这里把它固定下来。
    assert tokenize("") == []


def test_bm25_scores_match_hand_derived_value():
    """打分公式：用已知语料手算一个确定的分数，验证代码算出来是一样的。"""
    # 【构造语料】两篇文档，各两个词；「工程」两篇都有，「招标」「投标」各出现在一篇里。
    index = BM25Index().fit([["工程", "招标"], ["工程", "投标"]])

    # 【取两边的稀疏向量】格式都是（下标列表, 权重列表），两边下标含义相同（同一张词表）。
    query_indices, query_values = index.encode_query("工程 招标")
    doc_indices, doc_values = index.encode_document(["工程", "招标"])

    # 【为什么要把「下标→权重」变成字典再对齐】稀疏向量只存有值的词，
    #   用下标当键，才能保证「同一个词」的查询权重和文档权重相乘，而不是错位相乘。
    query_by_term = dict(zip(query_indices, query_values))
    doc_by_term = dict(zip(doc_indices, doc_values))

    # 【点积 = 标准 BM25 分数】只累加两边都有的词。
    score = sum(
        query_by_term[term_id] * doc_by_term[term_id]
        for term_id in query_by_term.keys() & doc_by_term.keys()
    )

    # 【手算过程（这就是「已知输入验证预期数值」）】
    #   n_docs=2、总词数=4 → 平均文档长度=2.0
    #   两篇文档里的每个词都恰好出现 1 次，且文档长度正好等于平均长度，
    #   所以文档侧权重 = 1*(1.5+1)/(1+1.5*(1-0.75+0.75*2/2.0)) = 2.5/2.5 = 1.0
    #   查询侧权重：idf(工程)=log(1+(2-2+0.5)/(2+0.5))=log(1.2)
    #              idf(招标)=log(1+(2-1+0.5)/(1+0.5))=log(2)
    #   点积 = log(1.2)*1.0 + log(2)*1.0
    expected = math.log(1.2) + math.log(2.0)
    # 【为什么用 approx】浮点数运算有微小尾差，approx 允许极小的误差。
    assert score == pytest.approx(expected)


def test_bm25_empty_corpus_encodes_empty():
    """空语料：编码必须安全返回空向量（不抛异常、不除零）。

    【为什么不再断言 ready/k1/b 这类字段】它们是实现细节，防线失效（比如
    fit 丢参数）时字段断言未必抓得住；这里锁的是外部可观察的编码行为。
    """
    # 【为什么专门测空语料】项目硬约束是「知识库为空也要能跑」。
    #   空语料时若还硬算，会出现「除以零」把服务打挂，所以源码里到处在防这件事。
    index = BM25Index().fit([])

    assert index.n_docs == 0
    # 【编码返回一对空列表】表示「这次检索没有关键词结果」，而不是抛异常。
    assert index.encode_query("任何问题") == ([], [])
    assert index.encode_document(["任何", "词"]) == ([], [])


def test_bm25_save_then_load_restores_stats(tmp_path):
    """持久化：save 出来的文件，load 回来必须一字不差。"""
    # 【为什么用 tmp_path】这是 pytest 内置的「临时目录」fixture，
    #   每次测试都是全新的空目录，测完自动清理 —— 既不会污染项目，也不会互相干扰。
    original = BM25Index(k1=1.2, b=0.6).fit([["工程", "招标"], ["工程", "投标"]])
    path = tmp_path / "bm25_index.json"

    original.save(path)
    # 【为什么先断言文件真的生成了】save 里有 mkdir 父目录的逻辑，
    #   这一步顺便验证「多级目录不存在也能创建」。
    assert path.exists()

    loaded = BM25Index.load(path)

    # 【参数复原用行为验证，不断言内部字段】k1/b 决定打分公式——若 load 丢了参数
    # （退回默认 1.5/0.75），同一查询/文档的编码权重会变。这里对比「读回来的索引
    # 对同一输入的编码结果与保存前一致」，比断言字段值更能锁住真实行为。
    assert loaded.encode_query("工程 招标") == original.encode_query("工程 招标")
    assert loaded.encode_document(["工程", "招标"]) == original.encode_document(["工程", "招标"])
    # 【统计量逐项对比】这些是 IDF 与长度归一化的原料，缺一不可。
    assert loaded.n_docs == original.n_docs
    assert loaded.total_length == original.total_length
    assert loaded.avg_doc_length == original.avg_doc_length
    # 【词表与文档频率必须一模一样】它们决定了下标的含义，错位会让检索「张冠李戴」。
    assert loaded.term_to_id == original.term_to_id
    assert loaded.doc_freq == original.doc_freq