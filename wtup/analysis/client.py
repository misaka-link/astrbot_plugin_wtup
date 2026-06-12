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
    llm_kwargs: dict[str, Any] = {"prompt": prompt}
    requested_provider_id = settings.provider_id if provider_id is None else str(provider_id or "").strip()

    if requested_provider_id:
        try:
            provider = context.get_provider_by_id(provider_id=requested_provider_id)
        except Exception as exc:
            provider = None
            logger.warning("[%s] 获取 Provider %s 失败: %s", PLUGIN_NAME, requested_provider_id, exc)
        if provider:
            llm_kwargs["chat_provider_id"] = requested_provider_id
        else:
            logger.warning("[%s] Provider %s 不存在，改用默认模型", PLUGIN_NAME, requested_provider_id)

    return await asyncio.wait_for(context.llm_generate(**llm_kwargs), timeout=settings.timeout_seconds)
