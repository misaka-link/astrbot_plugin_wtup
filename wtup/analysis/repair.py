from __future__ import annotations

import asyncio
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME, PluginConfig
from ..diff_collector import DiffChunk, DiffSummary
from .client import request_llm
from .errors import record_model_error
from .fallback import fallback_analysis
from .normalize import parse_analysis_json
from .prompts import build_json_repair_prompt
from .responses import ensure_usable_llm_response, extract_response_text


async def parse_or_repair_analysis(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    raw_text: str,
    *,
    semaphore: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    parsed = parse_analysis_json(raw_text)
    if parsed is not None:
        return parsed

    logger.warning("[%s] 模型输出 JSON 解析失败，启动 JSON 修复模型请求 chunk %d/%d", PLUGIN_NAME, chunk.index, chunk.total)
    repair_prompt = build_json_repair_prompt(settings, summary, chunk, raw_text)
    try:
        if semaphore is None:
            response = await request_llm(context, settings, repair_prompt)
        else:
            async with semaphore:
                response = await request_llm(context, settings, repair_prompt)
        ensure_usable_llm_response(response)
        repair_text = extract_response_text(response)
        repaired = parse_analysis_json(repair_text)
        if repaired is not None:
            return repaired
        record_model_error(
            settings,
            "json_repair_invalid",
            "JSON 修复模型请求仍未返回有效 JSON",
            summary=summary,
            chunk=chunk,
            extra={"raw_text": raw_text[:4000], "repair_text": repair_text[:4000]},
        )
        logger.warning("[%s] JSON 修复模型请求仍未返回有效 JSON chunk %d/%d", PLUGIN_NAME, chunk.index, chunk.total)
    except Exception as exc:
        record_model_error(
            settings,
            "json_repair_failed",
            exc,
            summary=summary,
            chunk=chunk,
            extra={"raw_text": raw_text[:4000]},
        )
        logger.warning("[%s] JSON 修复模型请求失败 chunk %d/%d: %s", PLUGIN_NAME, chunk.index, chunk.total, exc)

    return fallback_analysis(
        "模型输出格式未按 JSON 返回，JSON 修复模型请求仍失败，相关内容需要结合 GitHub 原始 diff 复核。",
        raw_text=raw_text,
    )
