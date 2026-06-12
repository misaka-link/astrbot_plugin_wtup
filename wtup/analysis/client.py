from __future__ import annotations

import asyncio
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
    return await asyncio.wait_for(context.llm_generate(**llm_kwargs), timeout=settings.timeout_seconds)
