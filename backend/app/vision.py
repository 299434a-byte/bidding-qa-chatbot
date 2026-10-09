# ============================================================================ #
# 本文件在干什么
# ---------------------------------------------------------------------------- #
# vision.py 是「图片解析」模块。用户在前端点附件按钮（📎）上传一张图片
# （比如招标公告的截图、扫描件），后端把它交给「智谱视觉大模型」识别成文字，
# 再回给前端——前端把识别结果放进一张卡片，用户点一下就能填进提问框。
#
# 对应接口：POST /analyze-image（在 main.py 里实现，本文件负责核心逻辑）。
#
# ---------------------------------------------------------------------------- #
# 上游 / 下游
# ---------------------------------------------------------------------------- #
# - 上游：main.py 的 /analyze-image 接口收到上传文件后，会先做「格式+体积」校验，
#   然后调用本文件的 analyze_image 得到识别文本。
# - 下游：识别出的文本返回给前端展示（不影响原有的问答流程）。
#
# ---------------------------------------------------------------------------- #
# 概念预习（大白话）
# ---------------------------------------------------------------------------- #
# 【视觉模型】= 能「看图」的大模型（这里用智谱的 glm-4.6v-flashx）。你把图片和
#   一句指令发过去，它回一段文字描述/识别结果。
# 【base64】= 把「二进制图片」转成一串纯文本字符的办法。因为很多接口只能传文本，
#   所以图片要先编码成 base64 文本再塞进请求里。
# 【data URL】= 形如 data:image/png;base64,xxxx 的一整串。它把「这是什么类型」
#   和「图片内容（base64）」拼在一起，很多模型接口直接认这种格式。
# 【MIME 类型】= 表示文件类型的标准名字，如 image/jpeg、image/png。
# 【HTTP 400 / 502】=
#   400 = 「你（调用方）给的东西有问题」（比如类型不支持、图片太大）；
#   502 = 「我们这一侧上游（模型服务）出问题了」（比如没配 Key、模型报错、超时）。
#   本文件用两种自定义异常分别对应这两种情况，main.py 据此返回不同状态码。
#
# ---------------------------------------------------------------------------- #
# 硬约束 & 实测结论
# ---------------------------------------------------------------------------- #
# - 智谱的 API Key 必须来自 .env（ZHIPUAI_API_KEY），代码里绝不出现密钥。
# - 只允许 jpg / png / webp，且体积不超过 10MB（上限由配置 vision_max_bytes 控制）。
# - 无效类型/超限 → 抛 ImageRejected（对应 400）；
#   没配 Key / 模型失败 / 超时 / 返回空 → 抛 VisionError（对应 502）。
# - 没有上传图片时，本模块完全不参与，绝不影响原有问答流程。
# - 血泪：PyPI 上名为 'zai' 的包是空壳，官方智谱 SDK 叫 'zai-sdk'，但导入名仍是 zai。
# - 调用 SDK 是同步阻塞的，在 main.py 里用 asyncio.to_thread 包起来避免卡事件循环。
#
# ---------------------------------------------------------------------------- #
# 阅读顺序指南
# ---------------------------------------------------------------------------- #
#   1. 顶部常量（_EXT_MIME / _ALLOWED_MIME / DEFAULT_PROMPT）；
#   2. 两个自定义异常（ImageRejected → 400，VisionError → 502）；
#   3. resolve_mime / check_size —— 校验「格式、体积」；
#   4. analyze_image —— 组装请求、调用模型、返回识别结果。
# ============================================================================ #
"""图片解析：把上传的图片交给智谱视觉模型识别成结构化文本。

对应接口 ``POST /analyze-image``。与 pg.py / graph.py 一样，
API Key 等配置一律从 ``.env`` 读取，代码中不出现任何密钥。
"""

# 让类型标注延迟求值，兼容性更好。
from __future__ import annotations

# base64：把二进制图片编码成纯文本，便于放进请求体。
import base64
# os：用来从文件名里取后缀名（如 .png）。
import os
# 只用于类型标注的 Any（表示「任意类型」）。
from typing import Any

# Settings/get_settings：读取 .env 配置（含智谱 Key、模型名、超时等，绝不硬编码）。
from .config import Settings, get_settings

# 允许的图片后缀 -> 构造 data URL 时使用的 MIME
# 大白话：用户上传的文件名后缀（.jpg/.png/...）对应哪个标准类型名。
_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# 允许的 content-type（部分浏览器/系统会给出不带后缀时的类型）
# 有些上传不带文件后缀，但会带 content-type，就用这个集合来认。
_ALLOWED_MIME = {"image/jpeg", "image/jpg", "image/png", "image/webp"}

# 默认解析指令：文字、表格、版式都要，尽量完整
# 这是发给视觉模型的「要求」，写得越具体，识别结果越符合期望。
DEFAULT_PROMPT = "请识别这张图片的全部内容，包括文字、表格和版式，尽量完整输出"


# 图片本身不合规（类型不支持 / 体积超限）时抛这个。
# 继承 ValueError。main.py 捕获后返回 HTTP 400（表示「是调用方给的数据有问题」）。
class ImageRejected(ValueError):
    """图片本身不合规（类型不支持 / 体积超限），对应 HTTP 400。"""


# 解析过程失败（未配 Key / 模型报错 / 超时 / 返回为空）时抛这个。
# 继承 RuntimeError。main.py 捕获后返回 HTTP 502（表示「我们上游模型服务出问题」）。
class VisionError(RuntimeError):
    """解析失败（未配 Key / 模型报错 / 超时 / 返回为空），对应 HTTP 502。"""


# resolve_mime：判断这张图到底是什么类型（MIME）。
# 优先看文件名后缀，其次看浏览器给的 content-type；都不认识就拒绝（抛 400）。
# 参数：
#   filename —— 上传的文件名（可能为 None）；
#   content_type —— 上传携带的 MIME（可能为 None）。
# 返回：标准 MIME 字符串（如 image/png）。
def resolve_mime(filename: str | None, content_type: str | None) -> str:
    """由文件名后缀或上传的 content-type 推断 MIME；都不支持时抛 ImageRejected。"""
    # 取文件名后缀：先转小写（.PNG 和 .png 要一样对待），splitext 拆出 [0]=名字、[1]=后缀。
    ext = os.path.splitext((filename or "").lower())[1]
    # 后缀在允许表里，直接按表返回对应 MIME。
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]

    # 没有可用后缀，退而看 content-type。
    # split(";")[0] 是因为 content-type 可能带参数（如 "image/png; charset=..."），
    # 只取分号前的部分；strip() 去空白。
    mime = (content_type or "").lower().split(";")[0].strip()
    # content-type 在允许集合里：注意 image/jpg 是「非标准但常见」的写法，统一改成 image/jpeg。
    if mime in _ALLOWED_MIME:
        return "image/jpeg" if mime == "image/jpg" else mime

    # 两种方式都认不出来 → 拒绝（对应 HTTP 400）。
    raise ImageRejected("只支持 jpg / png / webp 格式的图片")


# check_size：校验图片体积。
# 参数：size 图片字节数；settings 配置（提供上限 vision_max_bytes）。
# 为空或超上限都抛 ImageRejected（对应 400）。
def check_size(size: int, settings: Settings) -> None:
    """体积校验：为空或超过上限都抛 ImageRejected。"""
    # 0 字节或负数 → 视为空文件，让用户重选。
    if size <= 0:
        raise ImageRejected("图片内容为空，请重新选择")
    # 超过上限 → 提示最大支持多少 MB。
    # // 是「整除」：vision_max_bytes 除以 (1024*1024) 取整，把字节换算成 MB。
    if size > settings.vision_max_bytes:
        raise ImageRejected(f"图片过大，最大支持 {settings.vision_max_bytes // (1024 * 1024)} MB")


# analyze_image：核心函数——把图片交给智谱视觉模型识别，返回文本。
# 参数：
#   data —— 图片的原始二进制内容；
#   mime —— 图片类型（由 resolve_mime 得到）；
#   question —— 用户附带的提问（可选，带上它能让模型额外留意相关内容）；
#   settings —— 配置对象（可选，不传就现取全局配置）。
# 返回：{"text": 识别文本, "model": 使用的模型名}。
def analyze_image(
    data: bytes,
    mime: str,
    question: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """调用智谱视觉模型解析图片，返回 ``{"text": ..., "model": ...}``。"""
    # 没传配置就取全局配置；or 是「左为真就用左，否则用右」的简写。
    s = settings or get_settings()
    # 没配智谱 Key：抛 VisionError（对应 502），提示无法解析。
    if not s.vision_configured:
        raise VisionError("未配置智谱 API Key（ZHIPUAI_API_KEY），无法解析图片")

    # 默认用「完整识别」的指令。
    prompt = DEFAULT_PROMPT
    # 如果用户还附带了提问，就在指令后追加一句，请模型额外留意该问题相关内容。
    if question.strip():
        # 用户附带了提问：让模型在完整识别之外额外留意该问题相关内容
        prompt = f"{DEFAULT_PROMPT}。\n\n用户附带的提问是：{question.strip()}，请一并留意与之相关的内容。"

    # 把图片编码成 data URL：data:<类型>;base64,<编码内容>。
    # b64encode 得到的是字节，再 .decode("ascii") 转成字符串才能拼进文本。
    image_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    # 延迟导入智谱 SDK（导入名是 zai，安装包名叫 zai-sdk）。
    try:
        from zai import ZhipuAiClient
    # 没装 SDK → 抛 VisionError（对应 502），给出明确原因，而不是 500。
    except ImportError as exc:  # 依赖未装时给出明确原因，而不是 500
        raise VisionError("未安装智谱 SDK（zai-sdk），无法解析图片") from exc

    try:
        # 创建客户端（Key 来自 .env；timeout 控制等待上限）。
        client = ZhipuAiClient(api_key=s.zhipuai_api_key, timeout=s.vision_timeout)
        # 发起对话式请求：
        #   model：用哪个视觉模型；
        #   messages：一条 user 消息，content 是一个列表，同时包含「文字指令」和
        #   「图片」两部分——这是多模态模型的标准写法。
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
        # 取第一条候选回复的正文，去掉首尾空白。
        # 注意 choices[0] 表示第一条（通常只有一条），.message.content 是文本内容。
        text = (response.choices[0].message.content or "").strip()
    # SDK 异常种类多，统一收敛成 VisionError（对应 502），带上人话原因。
    except Exception as exc:  # noqa: BLE001 - SDK 异常种类多，统一转成 502 文案
        raise VisionError(f"调用视觉模型失败：{exc}") from exc

    # 模型返回空内容：也算失败（502），提示换一张更清晰的图。
    if not text:
        raise VisionError("视觉模型返回了空内容，请换一张更清晰的图片重试")

    # 成功：返回识别文本 + 实际使用的模型名（前端可据此展示）。
    return {"text": text, "model": s.vision_model}