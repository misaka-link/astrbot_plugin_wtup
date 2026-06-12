from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME, PluginConfig
from .normalize import safe_normalize_analysis
from .responses import extract_response_text


async def generate_analysis_from_prompt(context: Any, settings: PluginConfig, prompt: str) -> dict[str, Any]:
    response = await request_llm(context, settings, prompt)
    text = extract_response_text(response)
    return safe_normalize_analysis(text)


async def request_llm(context: Any, settings: PluginConfig, prompt: str, *, provider_id: str | None = None) -> Any:
    provider_ids = settings.provider_fallback_ids(provider_id)
    last_error: BaseException | None = None

    for requested_provider_id in provider_ids:
        try:
            provider = context.get_provider_by_id(provider_id=requested_provider_id)
        except Exception as exc:
            provider = None
            logger.warning("[%s] 获取 Provider %s 失败: %s", PLUGIN_NAME, requested_provider_id, exc)
        if not provider:
            logger.warning("[%s] Provider %s 不存在，跳过该模型", PLUGIN_NAME, requested_provider_id)
            continue

        try:
            return await _request_llm_once(context, settings, prompt, provider_id=requested_provider_id)
        except Exception as exc:
            last_error = exc
            logger.warning("[%s] Provider %s 请求失败，尝试备用模型: %s", PLUGIN_NAME, requested_provider_id, exc)

    if last_error is not None:
        raise RuntimeError("所有已配置模型请求失败") from last_error

    return await _request_llm_once(context, settings, prompt)


async def _request_llm_once(
    context: Any,
    settings: PluginConfig,
    prompt: str,
    *,
    provider_id: str = "",
) -> Any:
    llm_kwargs: dict[str, Any] = {"prompt": prompt}
    if provider_id:
        llm_kwargs["chat_provider_id"] = provider_id
    if settings.enable_streaming_llm_call:
        provider = _get_stream_provider(context, provider_id)
        if provider is not None:
            logger.info("[%s] 使用流式 Provider 调用%s", PLUGIN_NAME, f": {provider_id}" if provider_id else "")
            return await asyncio.wait_for(_call_provider_stream(provider, llm_kwargs), timeout=settings.timeout_seconds)
        if provider_id:
            raise RuntimeError(f"Provider {provider_id} 不支持流式调用")
        logger.warning("[%s] 未找到可用的流式 Provider，已回退为非流式模型请求", PLUGIN_NAME)
    return await asyncio.wait_for(context.llm_generate(**llm_kwargs), timeout=settings.timeout_seconds)


def _get_stream_provider(context: Any, provider_id: str) -> Any | None:
    provider = None
    if provider_id:
        try:
            provider = context.get_provider_by_id(provider_id=provider_id)
        except Exception as exc:
            logger.warning("[%s] 获取流式 Provider %s 失败: %s", PLUGIN_NAME, provider_id, exc)
            return None
    if provider is None and not provider_id:
        get_all_providers = getattr(context, "get_all_providers", None)
        if callable(get_all_providers):
            try:
                providers = get_all_providers()
            except Exception as exc:
                logger.warning("[%s] 获取可用 Provider 列表失败: %s", PLUGIN_NAME, exc)
                providers = []
            if providers:
                provider = providers[0]
    if provider is not None and callable(getattr(provider, "text_chat_stream", None)):
        return provider
    return None


async def _call_provider_stream(provider: Any, llm_kwargs: dict[str, Any]) -> Any:
    stream_kwargs = dict(llm_kwargs)
    stream_kwargs.pop("chat_provider_id", None)

    final_response = None
    content_parts: list[str] = []
    async for response in provider.text_chat_stream(**stream_kwargs):
        final_response = response
        text = extract_response_text(response)
        if getattr(response, "is_chunk", False) and text:
            content_parts.append(text)

    if final_response is None:
        raise RuntimeError("流式 LLM 调用未返回任何响应")

    final_text = extract_response_text(final_response)
    if final_text and not getattr(final_response, "is_chunk", False):
        return final_response

    return SimpleNamespace(
        role="assistant",
        completion_text="".join(content_parts),
        usage=getattr(final_response, "usage", None),
        raw_completion=getattr(final_response, "raw_completion", None),
    )
