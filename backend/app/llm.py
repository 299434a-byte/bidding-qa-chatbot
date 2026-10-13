
"""llm：大模型客户端（OpenAI 兼容接口封装，流式与一次性调用）。

职责：
  - stream_chat 流式生成（yield 文本块）供 SSE 转发
  - complete_chat 一次性完整生成（重排、Cypher / SQL 生成等场景）
  - 统一超时、异常类型与重试语义

依赖：config
被谁调用：rag、rerank、graph、pg、agent、deep_agent
"""

from __future__ import annotations

from typing import AsyncIterator, Iterator

from .config import Settings, get_settings


class LLMNotConfigured(RuntimeError):
    """未配置 LLM，属于预期内的降级场景，不是错误。"""


class LLMError(RuntimeError):
    """调用大模型失败。"""


def _client(settings: Settings, timeout: int | None = None):
    if not settings.llm_configured:
        raise LLMNotConfigured("未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMError("缺少依赖 openai，请执行 pip install openai") from exc

    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=timeout or settings.llm_timeout,
    )


def aclient(settings: Settings, timeout: int | None = None):
    """异步客户端。深度模式需要在 async 生成器里边调模型边推事件，必须用异步版。"""
    if not settings.llm_configured:
        raise LLMNotConfigured("未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMError("缺少依赖 openai，请执行 pip install openai") from exc

    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=timeout or settings.llm_timeout,
    )


def stream_chat(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
) -> Iterator[str]:
    """流式生成回答，逐段 yield 文本增量。"""
    s = settings or get_settings()
    client = _client(s, timeout=timeout)
    try:
        stream = client.chat.completions.create(
            model=model or s.llm_model,
            messages=messages,
            temperature=s.llm_temperature if temperature is None else temperature,
            max_tokens=max_tokens or s.llm_max_tokens,
            stream=True,
        )
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content
    except Exception as exc:
        raise LLMError(f"调用大模型失败：{exc}") from exc


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
    s = settings or get_settings()
    client = aclient(s, timeout=timeout)
    try:
        stream = await client.chat.completions.create(
            model=model or s.llm_model,
            messages=messages,
            temperature=s.llm_temperature if temperature is None else temperature,
            max_tokens=max_tokens or s.llm_max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content
    except Exception as exc:
        raise LLMError(f"调用大模型失败：{exc}") from exc


def complete_chat(
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: int | None = None,
) -> str:
    """非流式一次性补全，返回完整文本。"""
    s = settings or get_settings()
    client = _client(s, timeout=timeout)
    try:
        response = client.chat.completions.create(
            model=model or s.llm_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens or s.llm_max_tokens,
            stream=False,
        )
    except Exception as exc:
        raise LLMError(f"调用大模型失败：{exc}") from exc

    if not getattr(response, "choices", None):
        raise LLMError("大模型返回内容为空")

    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        raise LLMError(
            f"大模型返回内容为空（finish_reason={getattr(choice, 'finish_reason', None)}）。"
            "推理模型会先把 max_tokens 用在思维链上，请调大 max_tokens。"
        )
    return content