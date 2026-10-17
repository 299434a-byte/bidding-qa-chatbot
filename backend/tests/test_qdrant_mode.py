"""向量库「双模式」的离线单测：本地嵌入式 vs 服务端。

【测试纪律】断言行为，不断言源码文本：
  这里锁的是「用哪种构造参数连库」——这属于对外契约（决定连哪个库），不是实现细节。
  做法：monkeypatch 掉 QdrantClient 只记录构造参数，不发起任何网络/文件访问。
"""

from __future__ import annotations

import pytest

from app import config, vector_store


class _FakeClient:
    """假客户端：只记录构造时收到的参数。"""

    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        _FakeClient.calls.append(kwargs)

    def close(self) -> None:  # _close_quietly 在退出时会调它
        return None


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch, tmp_path):
    _FakeClient.calls = []
    monkeypatch.setattr(vector_store, "QdrantClient", _FakeClient)
    # 本地模式会 mkdir(qdrant_path)，指向临时目录避免污染项目 data/
    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "qdrant"))
    yield tmp_path
    monkeypatch.undo()


def _fresh_settings():
    """直接重新构建一份 Settings：绕开 get_settings() 的 lru_cache，让新环境变量生效。"""
    return config._build_settings()


def test_local_mode_uses_path_and_not_url(monkeypatch, tmp_path):
    """QDRANT_URL 为空 → 用 path 构造，且不传 api_key/timeout（保持旧行为）。"""
    monkeypatch.setenv("QDRANT_URL", "")
    store = vector_store.VectorStore(_fresh_settings())
    assert store.mode == "local"
    assert len(_FakeClient.calls) == 1
    call = _FakeClient.calls[0]
    assert "path" in call and "url" not in call
    assert "api_key" not in call and "timeout" not in call
    assert (tmp_path / "qdrant").exists()  # 本地模式会建目录


def test_server_mode_uses_url_and_credentials(monkeypatch):
    """QDRANT_URL 非空 → 用 url + api_key + timeout + prefer_grpc 构造。"""
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:16333")
    monkeypatch.setenv("QDRANT_API_KEY", "secret-key")
    monkeypatch.setenv("QDRANT_TIMEOUT", "12")
    monkeypatch.setenv("QDRANT_PREFER_GRPC", "true")
    store = vector_store.VectorStore(_fresh_settings())
    assert store.mode == "server"
    call = _FakeClient.calls[0]
    assert call["url"] == "http://127.0.0.1:16333"
    assert call["api_key"] == "secret-key"
    assert call["timeout"] == 12
    assert call["prefer_grpc"] is True
    assert "path" not in call


def test_server_mode_without_api_key_passes_none(monkeypatch):
    """服务端模式但没配 api_key → 传 None（传空串会被客户端当成无效凭据）。"""
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:16333")
    monkeypatch.setenv("QDRANT_API_KEY", "")
    vector_store.VectorStore(_fresh_settings())
    assert _FakeClient.calls[0]["api_key"] is None


def test_whitespace_url_falls_back_to_local(monkeypatch):
    """QDRANT_URL 只有空格 → 视为未配置，走本地模式（避免拼出非法 URL）。"""
    monkeypatch.setenv("QDRANT_URL", "   ")
    store = vector_store.VectorStore(_fresh_settings())
    assert store.mode == "local"
    assert "path" in _FakeClient.calls[0]


def test_local_mode_does_not_touch_timeout(monkeypatch):
    """本地模式即使配了 timeout，也不能把它传给本地客户端（旧行为保持不变）。"""
    monkeypatch.setenv("QDRANT_URL", "")
    monkeypatch.setenv("QDRANT_TIMEOUT", "5")
    vector_store.VectorStore(_fresh_settings())
    assert "timeout" not in _FakeClient.calls[0]


def test_mask_url_hides_credentials():
    """日志打码：query 里的 api_key 必须被隐藏，其它参数原样保留。"""
    masked = vector_store.mask_url("https://x.qdrant.io:6333?api_key=abc123&tls=true")
    assert "abc123" not in masked
    assert "api_key=***" in masked
    assert "tls=true" in masked
    assert vector_store.mask_url("") == ""
    assert vector_store.mask_url("http://127.0.0.1:6333") == "http://127.0.0.1:6333"
