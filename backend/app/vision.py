"""vision：图片解析（大小 / 类型校验 + 智谱视觉模型识别为结构化文本）。

职责：
  - 三道关卡：MIME 声明校验、体积上限（413）、文件头嗅探防伪造（415 / 400）
  - 调用智谱视觉模型生成图片描述（上游失败返回 502）
  - 异常细分（ImageTooLarge / ImageUnsupported / ImageRejected）

依赖：config
被谁调用：main（/analyze-image）
"""

from __future__ import annotations

import base64
import os
from typing import Any

from .config import Settings, get_settings

_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_ALLOWED_MIME = {"image/jpeg", "image/jpg", "image/png", "image/webp"}

_ALLOWED_MIME_REAL = {"image/jpeg", "image/png", "image/webp"}

DEFAULT_PROMPT = "请识别这张图片的全部内容，包括文字、表格和版式，尽量完整输出"


class ImageRejected(ValueError):
    """图片本身不合规（类型不支持 / 内容识别不出 / 空文件），对应 HTTP 400。"""


class ImageTooLarge(ImageRejected):
    """体积超过 vision_max_bytes，对应 HTTP 413。"""


class ImageUnsupported(ImageRejected):
    """文件头显示的真实格式不是 jpg / png / webp，对应 HTTP 415。"""


class VisionError(RuntimeError):
    """解析失败（未配 Key / 模型报错 / 超时 / 返回为空），对应 HTTP 502。"""


def resolve_mime(filename: str | None, content_type: str | None) -> str:
    """由文件名后缀或上传的 content-type 推断 MIME；都不支持时抛 ImageRejected。"""
    ext = os.path.splitext((filename or "").lower())[1]
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]

    mime = (content_type or "").lower().split(";")[0].strip()
    if mime in _ALLOWED_MIME:
        return "image/jpeg" if mime == "image/jpg" else mime

    raise ImageRejected("只支持 jpg / png / webp 格式的图片")


def check_size(size: int, settings: Settings) -> None:
    """体积校验：为空抛 ImageRejected（400），超上限抛 ImageTooLarge（413）。"""
    if size <= 0:
        raise ImageRejected("图片内容为空，请重新选择")
    if size > settings.vision_max_bytes:
        raise ImageTooLarge(f"图片过大，最大支持 {settings.vision_max_bytes // (1024 * 1024)} MB")


_MAGIC_RULES: tuple[tuple[str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF", "application/pdf"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def sniff_mime(data: bytes) -> str | None:
    """按文件头判断真实类型；识别不出返回 None（调用方据此报 400）。"""
    if not data:
        return None
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for prefix, mime in _MAGIC_RULES:
        if data.startswith(prefix):
            return mime
    return None


def verify_content(data: bytes, declared_mime: str) -> str:
    """嗅探文件头并核对与声称类型是否一致；返回真实 MIME 或抛 400/415。"""
    actual = sniff_mime(data)
    if actual is None:
        raise ImageRejected("无法识别文件内容：文件头不是任何已知格式，请重新导出图片")
    if actual not in _ALLOWED_MIME_REAL:
        raise ImageUnsupported(f"不支持的图片格式：{actual}，只支持 jpg / png / webp")
    if actual != declared_mime:
        raise ImageUnsupported(f"文件实际格式（{actual}）与声称的（{declared_mime}）不一致")
    return actual


def analyze_image(
    data: bytes,
    mime: str,
    question: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """调用智谱视觉模型解析图片，返回 ``{"text": ..., "model": ...}``。"""
    s = settings or get_settings()
    if not s.vision_configured:
        raise VisionError("未配置智谱 API Key（ZHIPUAI_API_KEY），无法解析图片")

    prompt = DEFAULT_PROMPT
    if question.strip():
        prompt = f"{DEFAULT_PROMPT}。\n\n用户附带的提问是：{question.strip()}，请一并留意与之相关的内容。"

    image_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    try:
        from zai import ZhipuAiClient
    except ImportError as exc:
        raise VisionError("未安装智谱 SDK（zai-sdk），无法解析图片") from exc

    try:
        client = ZhipuAiClient(api_key=s.zhipuai_api_key, timeout=s.vision_timeout)
        response = client.chat.completions.create(
            model=s.vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001 - SDK 异常种类多，统一转成 502 文案
        raise VisionError(f"调用视觉模型失败：{exc}") from exc

    if not text:
        raise VisionError("视觉模型返回了空内容，请换一张更清晰的图片重试")

    return {"text": text, "model": s.vision_model}