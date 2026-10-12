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
# 【HTTP 状态码语义】（按 RFC 区分「谁的锅」，不再一律收拢成 400）
#   400 = 「你（调用方）给的东西有问题」—— 文件头识别不出是什么、空文件、
#         声称的类型（后缀/content-type）本身就不支持；
#   413 = 「你给的东西太大」—— 体积超过 vision_max_bytes；
#   415 = 「格式不支持」—— 文件头能识别，但真实格式不是 jpg / png / webp
#         （比如把 PDF 改名成 .png 上传），或与声称的类型对不上；
#   502 = 「我们这一侧上游（模型服务）出问题了」（没配 Key、模型报错、超时）。
#   本文件用四个自定义异常分别对应，main.py 据此返回不同状态码。
#
# ---------------------------------------------------------------------------- #
# 硬约束 & 实测结论
# ---------------------------------------------------------------------------- #
# - 智谱的 API Key 必须来自 .env（ZHIPUAI_API_KEY），代码里绝不出现密钥。
# - 只允许 jpg / png / webp，且体积不超过上限（配置 vision_max_bytes，默认 10MB）。
# - 类型识别不出 → 抛 ImageRejected（400）；超限 → ImageTooLarge（413）；
#   文件头真实格式不符 → ImageUnsupported（415）；
#   没配 Key / 模型失败 / 超时 / 返回空 → 抛 VisionError（对应 502）。
# - 【为什么要嗅探文件头】content-type 和文件名后缀都能伪造：把 PDF 改名成
#   .png 上传，只看后缀能过校验、发给模型才报错（原来的 502 就是这么来的）。
#   先看文件头（magic bytes）确认真实格式，能在调模型之前就把假图挡掉。
# - 没有上传图片时，本模块完全不参与，绝不影响原有问答流程。
# - 血泪：PyPI 上名为 'zai' 的包是空壳，官方智谱 SDK 叫 'zai-sdk'，但导入名仍是 zai。
# - 调用 SDK 是同步阻塞的，在 main.py 里用 asyncio.to_thread 包起来避免卡事件循环。
#
# ---------------------------------------------------------------------------- #
# 阅读顺序指南
# ---------------------------------------------------------------------------- #
#   1. 顶部常量（_EXT_MIME / _ALLOWED_MIME / DEFAULT_PROMPT）；
#   2. 四个自定义异常（ImageRejected → 400、ImageTooLarge → 413、
#      ImageUnsupported → 415，VisionError → 502）；
#   3. resolve_mime / check_size / sniff_mime / verify_content —— 校验「声明类型、体积、真实格式」；
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

# 真实格式（文件头嗅探结果）的允许名单。
# 与上面 _ALLOWED_MIME 的区别：这里没有 "image/jpg"（非标准写法，嗅探绝不会吐出来），
# 供 verify_content 判断「文件头认出的格式是否可接受」。
_ALLOWED_MIME_REAL = {"image/jpeg", "image/png", "image/webp"}

# 默认解析指令：文字、表格、版式都要，尽量完整
# 这是发给视觉模型的「要求」，写得越具体，识别结果越符合期望。
DEFAULT_PROMPT = "请识别这张图片的全部内容，包括文字、表格和版式，尽量完整输出"


# 图片本身不合规（声明的类型不支持 / 文件头识别不出 / 空文件）时抛这个。
# 继承 ValueError。main.py 捕获后返回 HTTP 400（表示「是调用方给的数据有问题」）。
class ImageRejected(ValueError):
    """图片本身不合规（类型不支持 / 内容识别不出 / 空文件），对应 HTTP 400。"""


# 体积超限：继承 ImageRejected，但 main.py 会**先**捕获它并返回 HTTP 413
# （Content Too Large）。用子类而不是同一个异常，是为了让状态码各归各位。
class ImageTooLarge(ImageRejected):
    """体积超过 vision_max_bytes，对应 HTTP 413。"""


# 真实格式不支持：同样继承 ImageRejected，main.py 先捕获它返回 HTTP 415
# （Unsupported Media Type）—— 文件头能看出是什么格式，只是我们不支持它
# （例如改名成 .png 的 PDF），或与声称的类型对不上。
class ImageUnsupported(ImageRejected):
    """文件头显示的真实格式不是 jpg / png / webp，对应 HTTP 415。"""


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
# 为空抛 ImageRejected（400）；超上限抛 ImageTooLarge（413）。
def check_size(size: int, settings: Settings) -> None:
    """体积校验：为空抛 ImageRejected（400），超上限抛 ImageTooLarge（413）。"""
    # 0 字节或负数 → 视为空文件，让用户重选。
    if size <= 0:
        raise ImageRejected("图片内容为空，请重新选择")
    # 超过上限 → 提示最大支持多少 MB。
    # // 是「整除」：vision_max_bytes 除以 (1024*1024) 取整，把字节换算成 MB。
    if size > settings.vision_max_bytes:
        raise ImageTooLarge(f"图片过大，最大支持 {settings.vision_max_bytes // (1024 * 1024)} MB")


# 【文件头（magic bytes）嗅探】
# 每种文件格式开头都有固定的「指纹字节」：PNG 是 \x89PNG\r\n\x1a\n，
# PDF 是 %PDF…… 看前几十个字节就能判断真实格式，文件名后缀改什么都没用。
# 【为什么用元组列表而不是一串 if】格式越来越多时，往表里加一行即可，
# 逻辑不用动；且「(判断函数, MIME)」的结构让每个格式一目了然。
_MAGIC_RULES: tuple[tuple[str, str], ...] = (
    # (文件头前缀, 对应 MIME)。用 bytes 前缀匹配最常见的几类。
    (b"\xff\xd8\xff", "image/jpeg"),            # JPEG：SOI 标记
    (b"\x89PNG\r\n\x1a\n", "image/png"),        # PNG：固定 8 字节签名
    (b"GIF87a", "image/gif"),                   # GIF（不支持，但要认出来才能报 415）
    (b"GIF89a", "image/gif"),
    (b"%PDF", "application/pdf"),               # PDF：改名成 .png 的常客
    (b"BM", "image/bmp"),                       # BMP
    (b"II*\x00", "image/tiff"),                 # TIFF（小端）
    (b"MM\x00*", "image/tiff"),                 # TIFF（大端）
)


def sniff_mime(data: bytes) -> str | None:
    """按文件头判断真实类型；识别不出返回 None（调用方据此报 400）。

    WEBP 的文件头特殊：前 4 字节是 RIFF、第 8~11 字节才是 WEBP，
    单看前缀会和其它 RIFF 容器（如 WAV/AVI）混淆，所以单独判断。
    """
    # 空内容直接判「识别不出」（正常应被 check_size 先挡成 400）。
    if not data:
        return None
    # WEBP：RIFF....WEBP。
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # 其余格式：逐条比对文件头前缀。
    for prefix, mime in _MAGIC_RULES:
        if data.startswith(prefix):
            return mime
    # 都对不上 → 识别不出。
    return None


# verify_content：用文件头核对「真实格式」，返回可信的 MIME。
# 三步：识别不出 → ImageRejected（400）；
#       识别得出但不支持（PDF/GIF/…）→ ImageUnsupported（415）；
#       支持但与声称的类型对不上（后缀/content-type 造假）→ ImageUnsupported（415）。
def verify_content(data: bytes, declared_mime: str) -> str:
    """嗅探文件头并核对与声称类型是否一致；返回真实 MIME 或抛 400/415。"""
    actual = sniff_mime(data)
    # ① 文件头认不出是什么 → 400（连格式都谈不上，不是「格式不支持」）。
    if actual is None:
        raise ImageRejected("无法识别文件内容：文件头不是任何已知格式，请重新导出图片")
    # ② 认得出但不在允许名单里 → 415（比如改名成 .png 的 PDF）。
    if actual not in _ALLOWED_MIME_REAL:
        raise ImageUnsupported(f"不支持的图片格式：{actual}，只支持 jpg / png / webp")
    # ③ 真实格式支持，但和声称的类型不一致（后缀或 content-type 造假）→ 415。
    #    【为什么】声称 image/png 实际是 image/jpeg 说明文件被动过，
    #    放行等于纵容伪造，也容易在展示/解析时张冠李戴。
    if actual != declared_mime:
        raise ImageUnsupported(f"文件实际格式（{actual}）与声称的（{declared_mime}）不一致")
    # 全部通过：返回真实 MIME（与声称一致，main.py 可放心使用）。
    return actual


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