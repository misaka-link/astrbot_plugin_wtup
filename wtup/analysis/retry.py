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
from .merge import merge_chunk_analyses, order_chunk_results
from .models import ChunkAnalysis
from .normalize import normalize_analysis, parse_analysis_json
from .prompts import build_chunk_refinement_prompt, build_prompt, build_refinement_prompt
from .repair import parse_or_repair_analysis
from .responses import ensure_usable_llm_response, extract_response_text


async def analyze_chunk(context: Any, settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> dict[str, Any]:
    prompt = build_prompt(settings, summary, chunk)
    response = await request_llm(context, settings, prompt)
    ensure_usable_llm_response(response)
    raw_text = extract_response_text(response)
    return await parse_or_repair_analysis(context, settings, summary, chunk, raw_text)

async def refine_merged_analysis(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    merged_analysis: dict[str, Any],
) -> dict[str, Any]:
    prompt = build_refinement_prompt(settings, summary, merged_analysis)
    try:
        response = await request_llm(context, settings, prompt, provider_id=settings.effective_summary_provider_id)
        text = extract_response_text(response)
        parsed = parse_analysis_json(text)
        if parsed is None:
            raise ValueError("总结模型输出不是有效 JSON")
        return parsed
    except Exception as exc:
        record_model_error(settings, "summary_refine_failed", exc, summary=summary)
        logger.warning("[%s] 总结模型分析失败，使用程序合并结果: %s", PLUGIN_NAME, exc)
        return normalize_analysis(merged_analysis)

async def analyze_chunks(context: Any, settings: PluginConfig, summary: DiffSummary) -> list[ChunkAnalysis]:
    concurrency = max(1, int(getattr(settings, "model_concurrency", 1) or 1))
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        analyze_chunk_with_retry(context, settings, summary, chunk, semaphore)
        for chunk in summary.chunks
    ]
    results = await asyncio.gather(*tasks)
    return order_chunk_results(summary.chunks, results)

async def analyze_chunk_with_retry(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    semaphore: asyncio.Semaphore,
) -> ChunkAnalysis:
    return await analyze_chunk_with_retry_attempt(context, settings, summary, chunk, semaphore, attempt=0)

async def analyze_chunk_with_retry_attempt(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    semaphore: asyncio.Semaphore,
    *,
    attempt: int,
) -> ChunkAnalysis:
    try:
        return await analyze_chunk_once(context, settings, summary, chunk, semaphore)
    except Exception as exc:
        max_retry_count = max(0, int(getattr(settings, "max_retry_count", 2) or 0))
        if len(chunk.files) <= 1 or attempt >= max_retry_count:
            record_model_error(
                settings,
                "chunk_analysis_failed",
                exc,
                summary=summary,
                chunk=chunk,
                extra={"attempt": attempt, "max_retry_count": max_retry_count},
            )
            logger.warning(
                "[%s] 模型分析失败 chunk %d/%d，已重试 %d/%d，无法继续拆分: %s",
                PLUGIN_NAME,
                chunk.index,
                chunk.total,
                attempt,
                max_retry_count,
                exc,
            )
            return ChunkAnalysis(
                chunk.index,
                chunk.total,
                fallback_analysis("模型分析失败，相关文件需要结合 GitHub 原始 diff 复核。"),
                error=str(exc),
            )

        retry_chunks = split_chunk_for_retry(chunk)
        record_model_error(
            settings,
            "chunk_analysis_retry_split",
            exc,
            summary=summary,
            chunk=chunk,
            extra={"attempt": attempt + 1, "max_retry_count": max_retry_count, "retry_chunks": len(retry_chunks)},
        )
        logger.warning(
            "[%s] 模型分析失败 chunk %d/%d，拆分为 %d 个更小请求后重试 (第 %d/%d 次): %s",
            PLUGIN_NAME,
            chunk.index,
            chunk.total,
            len(retry_chunks),
            attempt + 1,
            max_retry_count,
            exc,
        )
        retry_results = await asyncio.gather(
            *[
                analyze_chunk_with_retry_attempt(
                    context,
                    settings,
                    summary,
                    retry_chunk,
                    semaphore,
                    attempt=attempt + 1,
                )
                for retry_chunk in retry_chunks
            ]
        )
        try:
            merged = merge_chunk_analyses(summary, retry_chunks, retry_results)
        except Exception as merge_exc:
            logger.warning("[%s] 拆分重试结果合并失败 chunk %d/%d: %s", PLUGIN_NAME, chunk.index, chunk.total, merge_exc)
            return ChunkAnalysis(
                chunk.index,
                chunk.total,
                fallback_analysis("模型拆分重试已完成，但结果合并失败，需要结合 GitHub 原始 diff 复核。"),
                error=f"{exc}; retry merge failed: {merge_exc}",
            )

        retry_errors = "; ".join(result.error for result in retry_results if result.error)
        retry_raw_text = "\n\n".join(result.raw_text for result in retry_results if result.raw_text)
        return ChunkAnalysis(chunk.index, chunk.total, merged, error=retry_errors, raw_text=retry_raw_text)

async def analyze_chunk_without_retry(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    semaphore: asyncio.Semaphore,
) -> ChunkAnalysis:
    try:
        return await analyze_chunk_once(context, settings, summary, chunk, semaphore)
    except Exception as exc:
        record_model_error(settings, "chunk_retry_failed", exc, summary=summary, chunk=chunk)
        logger.warning("[%s] 拆分重试仍失败 chunk %d/%d: %s", PLUGIN_NAME, chunk.index, chunk.total, exc)
        return ChunkAnalysis(
            chunk.index,
            chunk.total,
            fallback_analysis("模型拆分重试仍失败，相关文件需要结合 GitHub 原始 diff 复核。"),
            error=str(exc),
        )

async def analyze_chunk_once(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    semaphore: asyncio.Semaphore,
) -> ChunkAnalysis:
    prompt = build_prompt(settings, summary, chunk)
    async with semaphore:
        response = await request_llm(context, settings, prompt)
    ensure_usable_llm_response(response)
    raw_text = extract_response_text(response)
    analysis = await parse_or_repair_analysis(context, settings, summary, chunk, raw_text, semaphore=semaphore)
    return ChunkAnalysis(chunk.index, chunk.total, analysis, raw_text=raw_text)

def split_chunk_for_retry(chunk: DiffChunk) -> list[DiffChunk]:
    target_size = max(1, (len(chunk.files) + 1) // 2)
    groups = [chunk.files[index : index + target_size] for index in range(0, len(chunk.files), target_size)]
    total = len(groups)
    return [
        DiffChunk(index=index + 1, total=total, files=files, patch_chars=sum(len(str(item.get("patch") or "")) + len(str(item.get("filename") or "")) for item in files))
        for index, files in enumerate(groups)
    ]

async def refine_chunk_analyses(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    *,
    merge_error: str = "",
) -> dict[str, Any]:
    prompt = build_chunk_refinement_prompt(settings, summary, chunks, results, merge_error=merge_error)
    try:
        response = await request_llm(context, settings, prompt, provider_id=settings.effective_summary_provider_id)
        text = extract_response_text(response)
        parsed = parse_analysis_json(text)
        if parsed is None:
            raise ValueError("总结模型输出不是有效 JSON")
        return parsed
    except Exception as exc:
        record_model_error(
            settings,
            "summary_from_chunks_failed",
            exc,
            summary=summary,
            extra={"merge_error": merge_error},
        )
        logger.warning("[%s] 分片总结模型分析失败，使用兜底报告: %s", PLUGIN_NAME, exc)
        return fallback_analysis("模型分片分析已完成，但程序合并和总结模型分析均失败，需要结合 GitHub 原始 diff 复核。")
