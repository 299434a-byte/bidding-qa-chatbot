# ============================================================================
# 文件：tests/test_api_error_codes.py —— 向量库故障与图片上传的状态码语义
#
# 【这个测试文件整体在测什么】
#   ① 向量库故障统一 503：/api/retrieve 与 /api/kb/samples 此前没包
#      VectorStoreError（retriever.retrieve 只兜住 dense 那一路，get_store /
#      get_embedder / load_bm25 / search_sparse 都在 try 之外）→ 向量库被锁、
#      维度不符时直接吐 500。现在必须与 /api/kb/stats 同口径返回 503。
#   ② 图片上传状态码分级（不再一律收拢成 400）：
#      · 文件头识别不出任何格式 → 400；
#      · 文件头是真格式但不支持（把 PDF 改名成 .png）→ 415；
#      · 体积超 vision_max_bytes → 413；
#      · 真 PNG + 未配视觉 Key → 502（正常路径回归：验证没被新校验挡住）。
#
# 【为什么用 monkeypatch 而不是真连向量库】
#   conftest 把一切外部依赖都关了；这里直接让 get_store 抛 VectorStoreError，
#   精确模拟「向量库被锁」那一刻，端点必须接住并翻译成 503。
# ============================================================================

from __future__ import annotations

# pytest：fixture / monkeypatch。
import pytest
# TestClient：把 FastAPI 应用当普通对象发请求，不起真实服务器。
from fastapi.testclient import TestClient

# 被测对象。
from app.config import get_settings
from app.main import app
from app.vector_store import VectorStoreError


@pytest.fixture()
def client():
    """TestClient；每个用例独立，顺手清掉限流桶避免连打被限。"""
    from app import ratelimit

    ratelimit._buckets.clear()
    return TestClient(app)


def _register(client: TestClient, username: str) -> str:
    """注册并拿令牌；同名已存在时（重跑测试）改走登录，保证用例可重复执行。"""
    resp = client.post("/api/auth/register", json={"username": username, "password": "test-pass-123"})
    if resp.status_code == 200:
        return resp.json()["token"]
    resp = client.post("/api/auth/login", json={"username": username, "password": "test-pass-123"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# --------------------------------------------------------------------------- #
# ① 向量库故障 → 503
# --------------------------------------------------------------------------- #
def _boom_store(settings):
    """假的 get_store：一被调用就抛向量库专用异常（模拟被锁 / 维度不符）。"""
    raise VectorStoreError("向量库不可用（目录被其它进程锁定）")


def test_retrieve_returns_503_when_vector_store_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/api/retrieve：get_store 抛 VectorStoreError → 503（此前是 500）。"""
    # retrieve() 在 app.retriever 模块里调 get_store，所以替换那个模块的引用。
    monkeypatch.setattr("app.retriever.get_store", _boom_store)
    resp = client.get("/api/retrieve", params={"q": "市政道路中标公告"})
    assert resp.status_code == 503, resp.text
    # 错误说明要透传给前端，方便排障（不是笼统的 "Internal Server Error"）。
    assert "向量库" in resp.json()["detail"]


def test_kb_samples_returns_503_when_vector_store_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/api/kb/samples：get_store 抛 VectorStoreError → 503（此前是 500）。"""
    # kb_samples 在 main 模块里调 get_store，替换 main 的引用。
    monkeypatch.setattr("app.main.get_store", _boom_store)
    resp = client.get("/api/kb/samples")
    assert resp.status_code == 503, resp.text
    assert "向量库" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# ② 图片上传：400 / 413 / 415 / 502 分级
# --------------------------------------------------------------------------- #
# 最小合法 PNG 文件头（8 字节签名）+ 一些凑数内容。
# 文件头嗅探只看开头，所以这样就够过 verify_content；内容本身不需要是真的 PNG 图。
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _upload(client: TestClient, token: str, name: str, payload: bytes, mime: str = "image/png"):
    """上传一张图（multipart），返回 Response。"""
    return client.post(
        "/analyze-image",
        files={"file": (name, payload, mime)},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_image_unrecognized_bytes_is_400(client: TestClient) -> None:
    """文件头识别不出任何格式 → 400（连格式都谈不上，不是「格式不支持」）。"""
    token = _register(client, "img_err_user_1")
    resp = _upload(client, token, "fake.png", "这根本不是图片，只是一段中文文本".encode())
    assert resp.status_code == 400, resp.text
    assert "文件头" in resp.json()["detail"]


def test_image_pdf_disguised_as_png_is_415(client: TestClient) -> None:
    """把 PDF 改名成 .png → 415（此前能过校验、调模型才报错，表现为 502）。"""
    token = _register(client, "img_err_user_2")
    resp = _upload(client, token, "notice.png", b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
    assert resp.status_code == 415, resp.text
    # 提示里要点名真实格式，用户才知道自己传错了什么。
    assert "application/pdf" in resp.json()["detail"]


def test_image_oversize_is_413(client: TestClient) -> None:
    """体积超上限 → 413（不是 400：是「太大」而不是「不对」）。"""
    token = _register(client, "img_err_user_3")
    s = get_settings()
    # 上限临时改成 8 字节：只比 PNG 头小、比我们传的文件小，必触发超限。
    original = s.vision_max_bytes
    object.__setattr__(s, "vision_max_bytes", 8)
    try:
        resp = _upload(client, token, "big.png", _PNG_BYTES)
        assert resp.status_code == 413, resp.text
        assert "图片过大" in resp.json()["detail"]
    finally:
        # 恢复真实上限，避免污染同进程里的其它测试。
        object.__setattr__(s, "vision_max_bytes", original)


def test_image_valid_png_reaches_vision_layer_as_502(client: TestClient) -> None:
    """真 PNG 头 + 未配视觉 Key → 502（新校验不误伤正常图片，且 502 语义保留）。"""
    token = _register(client, "img_err_user_4")
    # conftest 已把 ZHIPUAI_API_KEY 清空 → analyze_image 抛 VisionError → 502。
    resp = _upload(client, token, "real.png", _PNG_BYTES)
    assert resp.status_code == 502, resp.text
    assert "ZHIPUAI_API_KEY" in resp.json()["detail"]
