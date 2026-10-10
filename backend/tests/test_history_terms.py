# ============================================================================
# 文件：tests/test_history_terms.py —— 给「历史搜索的中文分词与高亮」做体检
#
# 被测对象是 backend/app/history.py 的 _index_terms / _query_terms（FTS 词元）
# 与 _build_preview（结果预览高亮）。都是纯函数，不起数据库。
# ============================================================================

from __future__ import annotations

from app.history import _build_preview, _index_terms, _query_terms


def test_index_terms_contain_two_char_substring():
    """建索引时「市政道路」要被二字组合覆盖，用户搜「市政」才可能命中。"""
    # jieba 会把「市政道路」切成整体；若只索引整词，子串查询永远命中不了，
    # 所以这里固定「二字组合确实进了索引」这一关键行为。
    assert "市政" in _index_terms("市政道路")


def test_query_terms_keep_two_char_substring():
    """查询侧同样要产出「市政」，才能与索引对上。"""
    assert "市政" in _query_terms("市政")


def test_index_terms_add_unigrams_but_query_terms_do_not():
    """单字只在建索引时加入，查询侧不放单字，避免结果过宽。"""
    # 「招标公告」经 jieba 得到「招标」「公告」，本身不含单字；因此单字「招」的
    # 有无，正好能验证 unigrams 开关只作用于索引侧。
    assert "招" in _index_terms("招标公告")
    assert "招" not in _query_terms("招标公告")


def test_build_preview_escapes_html_before_highlighting():
    """高亮前必须先转义，恶意标签不能原样透出。"""
    preview = _build_preview("<script>市政道路</script>", ["市政"])
    # 这是防 XSS 的核心：注入的 <script> 变成 &lt;script&gt; 后不再可执行。
    assert "<script>" not in preview
    assert "&lt;script&gt;" in preview
    # 转义与高亮互不干扰：命中词仍被 <em> 包住。
    assert "<em>市政</em>" in preview


def test_build_preview_without_hit_truncates_from_start():
    """一个词都没命中时安全退化为「从开头截断」，不报错。"""
    # 对应 LIKE 兜底路径：命中词可能定位不到，此时不能崩。
    preview = _build_preview("这是一段没有任何命中词的文本", ["不存在的词"])
    assert preview.startswith("这是一段")