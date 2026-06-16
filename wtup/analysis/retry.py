from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME, PluginConfig
from ..diff_collector import DiffChunk, DiffSummary, group_related_files
from .client import request_llm
from .coverage import collect_source_ids, enforce_change_coverage
from .errors import record_model_error
from .fallback import fallback_analysis
from .merge import merge_chunk_analyses, order_chunk_results
from .models import ChunkAnalysis, TokenUsage
from .normalize import normalize_analysis, parse_analysis_json
from .prompts import (
    build_chunk_refinement_prompt,
    build_dynamic_context_prompt,
    build_prompt,
    build_refinement_prompt,
    build_tool_refinement_prompt,
)
from .repair import parse_or_repair_analysis, parse_or_repair_analysis_with_usage
from .responses import ensure_usable_llm_response, extract_response_text, extract_token_usage
from .tools import execute_tool_calls


@dataclass(frozen=True)
class DynamicContextTask:
    parent_chunk: DiffChunk
    chunk: DiffChunk
    request: dict[str, Any]
    previous_analysis: dict[str, Any]
    round_index: int
    request_index: int
    file_statuses: list[dict[str, Any]]


async def analyze_chunk(context: Any, settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> dict[str, Any]:
    prompt = build_prompt(settings, summary, chunk)
    response = await request_llm(context, settings, prompt, summary=summary, chunk=chunk, purpose="分片分析")
    ensure_usable_llm_response(response)
    raw_text = extract_response_text(response)
    analysis = await parse_or_repair_analysis(context, settings, summary, chunk, raw_text)
    analysis, _usage, _raw_texts = await refine_analysis_with_tool_calls(
        context,
        settings,
        summary,
        chunk,
        analysis,
    )
    return analysis

async def refine_merged_analysis(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    merged_analysis: dict[str, Any],
) -> dict[str, Any]:
    analysis, _ = await refine_merged_analysis_with_usage(context, settings, summary, merged_analysis)
    return analysis

async def refine_merged_analysis_with_usage(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    merged_analysis: dict[str, Any],
) -> tuple[dict[str, Any], TokenUsage]:
    prompt = build_refinement_prompt(settings, summary, merged_analysis)
    token_usage = TokenUsage()
    try:
        response = await request_llm(
            context,
            settings,
            prompt,
            provider_id=settings.effective_summary_provider_id,
            summary=summary,
            purpose="程序合并后总结",
        )
        token_usage = extract_token_usage(response)
        text = extract_response_text(response)
        parsed = parse_analysis_json(text)
        if parsed is None:
            raise ValueError("总结模型输出不是有效 JSON")
        return enforce_change_coverage(summary, summary.chunks, parsed), token_usage
    except Exception as exc:
        record_model_error(settings, "summary_refine_failed", exc, summary=summary)
        logger.warning("[%s] 总结模型分析失败，使用程序合并结果: %s", PLUGIN_NAME, exc)
        return enforce_change_coverage(summary, summary.chunks, merged_analysis), token_usage

async def analyze_chunks(context: Any, settings: PluginConfig, summary: DiffSummary) -> list[ChunkAnalysis]:
    concurrency = max(1, int(getattr(settings, "model_concurrency", 1) or 1))
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        analyze_chunk_with_retry(context, settings, summary, chunk, semaphore)
        for chunk in summary.chunks
    ]
    results = await asyncio.gather(*tasks)
    ordered_results = order_chunk_results(summary.chunks, results)
    return await refine_results_with_dynamic_context_queue(
        context,
        settings,
        summary,
        summary.chunks,
        ordered_results,
        semaphore,
    )

async def refine_results_with_dynamic_context_queue(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    semaphore: asyncio.Semaphore,
) -> list[ChunkAnalysis]:
    if not getattr(settings, "enable_dynamic_context_queue", True):
        return results

    max_rounds = max(0, int(getattr(settings, "max_dynamic_context_rounds", 1) or 0))
    max_requests = max(0, int(getattr(settings, "max_dynamic_context_requests", 8) or 0))
    if max_rounds <= 0 or max_requests <= 0:
        return results

    ordered_results = order_chunk_results(chunks, results)
    parent_chunks: dict[int, list[DiffChunk]] = {chunk.index: [chunk] for chunk in chunks}
    parent_results: dict[int, list[ChunkAnalysis]] = {
        chunk.index: [result]
        for chunk, result in zip(chunks, ordered_results)
    }
    current_sources: list[tuple[DiffChunk, ChunkAnalysis]] = list(zip(chunks, ordered_results))
    seen_requests: set[tuple[int, str, tuple[str, ...]]] = set()
    next_chunk_index = max((chunk.index for chunk in chunks), default=0) + 1

    initial_uncertainty_count = sum(len(_analysis_uncertainties(result.analysis)) for result in ordered_results)
    initial_request_count = sum(len(_analysis_context_requests(result.analysis)) for result in ordered_results)
    total_enqueued = 0
    total_success = 0
    total_failed = 0
    total_duplicate_skipped = 0
    total_limit_skipped = 0
    total_invalid_skipped = 0
    total_missing_files = 0
    total_auto_generated_requests = 0

    for round_index in range(1, max_rounds + 1):
        round_tasks: list[DynamicContextTask] = []
        round_uncertainty_count = 0
        round_request_count = 0
        duplicate_skipped = 0
        limit_skipped = 0
        invalid_skipped = 0
        missing_files = 0
        auto_generated_requests = 0

        for parent_chunk, source_result in current_sources:
            uncertainties = _analysis_uncertainties(source_result.analysis)
            context_requests = _analysis_context_requests(source_result.analysis)
            auto_request_count = 0
            if uncertainties and not context_requests:
                max_files = max(1, int(getattr(settings, "max_dynamic_files_per_request", 4) or 4))
                context_requests = _auto_context_requests_from_uncertainties(
                    summary,
                    parent_chunk,
                    uncertainties,
                    max_files=max_files,
                )
                auto_request_count = len(context_requests)
                auto_generated_requests += auto_request_count
            round_uncertainty_count += len(uncertainties)
            round_request_count += len(context_requests)
            if uncertainties or context_requests:
                _record_task_log(
                    settings,
                    "动态补充扫描",
                    {
                        "轮次": round_index,
                        "来源分片": f"{parent_chunk.index}/{parent_chunk.total}",
                        "来源文件": _chunk_file_names(parent_chunk),
                        "不确定点数量": len(uncertainties),
                        "不确定点": uncertainties,
                        "补充请求数量": len(context_requests),
                        "自动生成补充请求数量": auto_request_count,
                        "补充请求": context_requests,
                    },
                )

            for request_index, request in enumerate(context_requests, start=1):
                max_files = max(1, int(getattr(settings, "max_dynamic_files_per_request", 4) or 4))
                request = _limit_context_request_files(request, max_files)
                key = _context_request_key(parent_chunk, request)
                if key in seen_requests:
                    duplicate_skipped += 1
                    continue
                if total_enqueued + len(round_tasks) >= max_requests:
                    limit_skipped += 1
                    continue

                task, skipped_reason, file_statuses = await _prepare_dynamic_context_task(
                    settings,
                    summary,
                    parent_chunk,
                    source_result.analysis,
                    request,
                    round_index=round_index,
                    request_index=request_index,
                    chunk_index=next_chunk_index,
                )
                if task is None:
                    invalid_skipped += 1
                    missing_files += _count_missing_file_statuses(file_statuses)
                    _record_task_log(
                        settings,
                        "动态补充请求跳过",
                        {
                            "轮次": round_index,
                            "来源分片": f"{parent_chunk.index}/{parent_chunk.total}",
                            "原因": skipped_reason,
                            "请求": request,
                            "文件处理": file_statuses,
                        },
                    )
                    continue

                seen_requests.add(key)
                round_tasks.append(task)
                next_chunk_index += 1
                missing_files += _count_missing_file_statuses(file_statuses)

        if round_uncertainty_count or round_request_count or round_tasks or duplicate_skipped or limit_skipped or invalid_skipped:
            _record_task_log(
                settings,
                "动态补充入队",
                {
                    "轮次": round_index,
                    "本轮不确定点数量": round_uncertainty_count,
                    "本轮补充请求数量": round_request_count,
                    "实际入队": len(round_tasks),
                    "去重跳过": duplicate_skipped,
                    "超限跳过": limit_skipped,
                    "无效跳过": invalid_skipped,
                    "自动生成补充请求": auto_generated_requests,
                    "缺失或拉取失败文件数": missing_files,
                    "累计已入队": total_enqueued + len(round_tasks),
                    "总请求上限": max_requests,
                },
            )

        total_duplicate_skipped += duplicate_skipped
        total_limit_skipped += limit_skipped
        total_invalid_skipped += invalid_skipped
        total_missing_files += missing_files
        total_auto_generated_requests += auto_generated_requests
        if not round_tasks:
            break

        round_results = await asyncio.gather(
            *[
                analyze_dynamic_context_task(context, settings, summary, task, semaphore)
                for task in round_tasks
            ]
        )
        total_enqueued += len(round_tasks)
        round_success = sum(1 for result in round_results if not result.error)
        round_failed = len(round_results) - round_success
        total_success += round_success
        total_failed += round_failed

        for task, result in zip(round_tasks, round_results):
            parent_index = task.parent_chunk.index
            parent_chunks.setdefault(parent_index, [task.parent_chunk]).append(task.chunk)
            parent_results.setdefault(parent_index, []).append(result)

        new_uncertainties = sum(len(_analysis_uncertainties(result.analysis)) for result in round_results)
        new_requests = sum(len(_analysis_context_requests(result.analysis)) for result in round_results)
        resolved_uncertainties = sum(len(_analysis_resolved_uncertainties(result.analysis)) for result in round_results)
        _record_task_log(
            settings,
            "动态补充完成",
            {
                "轮次": round_index,
                "完成请求数": len(round_results),
                "补充成功": round_success,
                "补充失败": round_failed,
                "新增不确定点数量": new_uncertainties,
                "新增补充请求数量": new_requests,
                "已解决不确定点数量": resolved_uncertainties,
            },
        )

        current_sources = [
            (task.parent_chunk, result)
            for task, result in zip(round_tasks, round_results)
        ]

    pending_request_count = sum(len(_analysis_context_requests(result.analysis)) for _chunk, result in current_sources)
    final_results = _merge_dynamic_parent_results(summary, chunks, ordered_results, parent_chunks, parent_results)
    final_uncertainty_count = sum(len(_analysis_uncertainties(result.analysis)) for result in final_results)
    if initial_uncertainty_count or initial_request_count or total_enqueued:
        _record_task_log(
            settings,
            "动态补充汇总",
            {
                "初始不确定点": initial_uncertainty_count,
                "初始补充请求": initial_request_count,
                "实际补充请求": total_enqueued,
                "补充成功": total_success,
                "补充失败": total_failed,
                "去重跳过": total_duplicate_skipped,
                "超限跳过": total_limit_skipped,
                "无效跳过": total_invalid_skipped,
                "自动生成补充请求": total_auto_generated_requests,
                "缺失或拉取失败文件数": total_missing_files,
                "最终剩余不确定点": final_uncertainty_count,
                "最终剩余补充请求": pending_request_count,
            },
        )
    return order_chunk_results(chunks, final_results)

async def analyze_chunk_with_retry(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    semaphore: asyncio.Semaphore,
) -> ChunkAnalysis:
    provider_ids = analysis_request_provider_ids(settings)
    provider_errors: list[str] = []
    last_result: ChunkAnalysis | None = None

    for provider_index, provider_id in enumerate(provider_ids):
        result = await analyze_chunk_with_retry_attempt(
            context,
            settings,
            summary,
            chunk,
            semaphore,
            attempt=0,
            provider_id=provider_id,
        )
        if not chunk_result_needs_provider_fallback(result):
            return result

        last_result = result
        provider_errors.append(f"{provider_label(provider_id)}: {chunk_result_failure_text(result)}")
        if provider_index + 1 < len(provider_ids):
            logger.warning(
                "[%s] Provider %s 分析 chunk %d/%d 完成拆分重试后仍失败，尝试备用模型 %s: %s",
                PLUGIN_NAME,
                provider_label(provider_id),
                chunk.index,
                chunk.total,
                provider_label(provider_ids[provider_index + 1]),
                chunk_result_failure_text(result),
            )

    if last_result is None:
            return ChunkAnalysis(
                chunk.index,
                chunk.total,
                enforce_change_coverage(
                    summary,
                    [chunk],
                    fallback_analysis("没有可用模型 Provider，相关文件需要结合 GitHub 原始 diff 复核。"),
                ),
                error="没有可用模型 Provider",
            )

    if len(provider_errors) <= 1:
        return last_result
    return ChunkAnalysis(
        last_result.chunk_index,
        last_result.chunk_total,
        last_result.analysis,
        error="; ".join(provider_errors),
        raw_text=last_result.raw_text,
        token_usage=last_result.token_usage,
    )

async def analyze_chunk_with_retry_attempt(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    semaphore: asyncio.Semaphore,
    *,
    attempt: int,
    provider_id: str | None = None,
) -> ChunkAnalysis:
    try:
        return await analyze_chunk_once(context, settings, summary, chunk, semaphore, provider_id=provider_id)
    except Exception as exc:
        max_retry_count = max(0, int(getattr(settings, "max_retry_count", 2) or 0))
        if len(chunk.files) <= 1 or attempt >= max_retry_count:
            record_model_error(
                settings,
                "chunk_analysis_failed",
                exc,
                summary=summary,
                chunk=chunk,
                extra={
                    "attempt": attempt,
                    "max_retry_count": max_retry_count,
                    "provider_id": provider_label(provider_id),
                },
            )
            logger.warning(
                "[%s] Provider %s 模型分析失败 chunk %d/%d，已重试 %d/%d，无法继续拆分: %s",
                PLUGIN_NAME,
                provider_label(provider_id),
                chunk.index,
                chunk.total,
                attempt,
                max_retry_count,
                exc,
            )
            return ChunkAnalysis(
                chunk.index,
                chunk.total,
                enforce_change_coverage(
                    summary,
                    [chunk],
                    fallback_analysis("模型分析失败，相关文件需要结合 GitHub 原始 diff 复核。"),
                ),
                error=str(exc),
            )

        retry_chunks = split_chunk_for_retry(chunk)
        record_model_error(
            settings,
            "chunk_analysis_retry_split",
            exc,
            summary=summary,
            chunk=chunk,
            extra={
                "attempt": attempt + 1,
                "max_retry_count": max_retry_count,
                "retry_chunks": len(retry_chunks),
                "provider_id": provider_label(provider_id),
            },
        )
        logger.warning(
            "[%s] Provider %s 模型分析失败 chunk %d/%d，拆分为 %d 个更小请求后重试 (第 %d/%d 次): %s",
            PLUGIN_NAME,
            provider_label(provider_id),
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
                    provider_id=provider_id,
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
                enforce_change_coverage(
                    summary,
                    [chunk],
                    fallback_analysis("模型拆分重试已完成，但结果合并失败，需要结合 GitHub 原始 diff 复核。"),
                ),
                error=f"{exc}; retry merge failed: {merge_exc}",
            )

        retry_errors = "; ".join(result.error for result in retry_results if result.error)
        retry_raw_text = "\n\n".join(result.raw_text for result in retry_results if result.raw_text)
        retry_usage = sum_token_usage(result.token_usage for result in retry_results)
        return ChunkAnalysis(chunk.index, chunk.total, merged, error=retry_errors, raw_text=retry_raw_text, token_usage=retry_usage)

async def analyze_chunk_without_retry(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    semaphore: asyncio.Semaphore,
    *,
    provider_id: str | None = None,
) -> ChunkAnalysis:
    try:
        return await analyze_chunk_once(context, settings, summary, chunk, semaphore, provider_id=provider_id)
    except Exception as exc:
        record_model_error(
            settings,
            "chunk_retry_failed",
            exc,
            summary=summary,
            chunk=chunk,
            extra={"provider_id": provider_label(provider_id)},
        )
        logger.warning(
            "[%s] Provider %s 拆分重试仍失败 chunk %d/%d: %s",
            PLUGIN_NAME,
            provider_label(provider_id),
            chunk.index,
            chunk.total,
            exc,
        )
        return ChunkAnalysis(
            chunk.index,
            chunk.total,
            enforce_change_coverage(
                summary,
                [chunk],
                fallback_analysis("模型拆分重试仍失败，相关文件需要结合 GitHub 原始 diff 复核。"),
            ),
            error=str(exc),
        )

async def analyze_chunk_once(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    semaphore: asyncio.Semaphore,
    *,
    provider_id: str | None = None,
) -> ChunkAnalysis:
    prompt = build_prompt(settings, summary, chunk)
    return await analyze_chunk_prompt_once(
        context,
        settings,
        summary,
        chunk,
        prompt,
        semaphore,
        provider_id=provider_id,
        purpose="分片分析",
    )

async def analyze_dynamic_context_task(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    task: DynamicContextTask,
    semaphore: asyncio.Semaphore,
) -> ChunkAnalysis:
    provider_ids = analysis_request_provider_ids(settings)
    provider_errors: list[str] = []
    last_result: ChunkAnalysis | None = None

    for provider_index, provider_id in enumerate(provider_ids):
        try:
            result = await analyze_dynamic_context_task_once(
                context,
                settings,
                summary,
                task,
                semaphore,
                provider_id=provider_id,
            )
        except Exception as exc:
            record_model_error(
                settings,
                "dynamic_context_analysis_failed",
                exc,
                summary=summary,
                chunk=task.chunk,
                extra={
                    "provider_id": provider_label(provider_id),
                    "round_index": task.round_index,
                    "context_request": task.request,
                },
            )
            result = ChunkAnalysis(
                task.chunk.index,
                task.chunk.total,
                enforce_change_coverage(
                    summary,
                    [task.chunk],
                    fallback_analysis("动态补充上下文分析失败，相关文件需要结合 GitHub 原始 diff 复核。"),
                ),
                error=str(exc),
            )

        if not chunk_result_needs_provider_fallback(result):
            return result

        last_result = result
        provider_errors.append(f"{provider_label(provider_id)}: {chunk_result_failure_text(result)}")
        if provider_index + 1 < len(provider_ids):
            logger.warning(
                "[%s] Provider %s 动态补充分析失败 chunk %d/%d，尝试备用模型 %s: %s",
                PLUGIN_NAME,
                provider_label(provider_id),
                task.chunk.index,
                task.chunk.total,
                provider_label(provider_ids[provider_index + 1]),
                chunk_result_failure_text(result),
            )

    if last_result is None:
        return ChunkAnalysis(
            task.chunk.index,
            task.chunk.total,
            enforce_change_coverage(
                summary,
                [task.chunk],
                fallback_analysis("没有可用模型 Provider，动态补充上下文需要结合 GitHub 原始 diff 复核。"),
            ),
            error="没有可用模型 Provider",
        )
    if len(provider_errors) <= 1:
        return last_result
    return ChunkAnalysis(
        last_result.chunk_index,
        last_result.chunk_total,
        last_result.analysis,
        error="; ".join(provider_errors),
        raw_text=last_result.raw_text,
        token_usage=last_result.token_usage,
    )

async def analyze_dynamic_context_task_once(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    task: DynamicContextTask,
    semaphore: asyncio.Semaphore,
    *,
    provider_id: str | None = None,
) -> ChunkAnalysis:
    prompt = build_dynamic_context_prompt(
        settings,
        summary,
        task.chunk,
        task.previous_analysis,
        task.request,
        round_index=task.round_index,
        remaining_rounds=max(0, int(getattr(settings, "max_dynamic_context_rounds", 1) or 0) - task.round_index),
    )
    return await analyze_chunk_prompt_once(
        context,
        settings,
        summary,
        task.chunk,
        prompt,
        semaphore,
        provider_id=provider_id,
        purpose="动态补充分析",
        precovered_source_ids=collect_source_ids(task.previous_analysis),
    )

async def analyze_chunk_prompt_once(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    prompt: str,
    semaphore: asyncio.Semaphore,
    *,
    provider_id: str | None = None,
    purpose: str = "分片分析",
    precovered_source_ids: set[str] | None = None,
) -> ChunkAnalysis:
    async with semaphore:
        response = await request_llm(
            context,
            settings,
            prompt,
            provider_id=provider_id,
            allow_fallback=False,
            summary=summary,
            chunk=chunk,
            purpose=purpose,
        )
    token_usage = extract_token_usage(response)
    ensure_usable_llm_response(response)
    raw_text = extract_response_text(response)
    analysis, repair_usage = await parse_or_repair_analysis_with_usage(
        context,
        settings,
        summary,
        chunk,
        raw_text,
        semaphore=semaphore,
        purpose=f"{purpose} JSON 修复",
    )
    analysis, tool_usage, tool_raw_texts = await refine_analysis_with_tool_calls(
        context,
        settings,
        summary,
        chunk,
        analysis,
        semaphore=semaphore,
        provider_id=provider_id,
    )
    analysis = enforce_change_coverage(
        summary,
        [chunk],
        analysis,
        precovered_source_ids=precovered_source_ids,
    )
    combined_raw_text = "\n\n".join([raw_text, *tool_raw_texts]).strip()
    return ChunkAnalysis(
        chunk.index,
        chunk.total,
        analysis,
        raw_text=combined_raw_text,
        token_usage=token_usage + repair_usage + tool_usage,
    )

async def _prepare_dynamic_context_task(
    settings: PluginConfig,
    summary: DiffSummary,
    parent_chunk: DiffChunk,
    previous_analysis: dict[str, Any],
    request: dict[str, Any],
    *,
    round_index: int,
    request_index: int,
    chunk_index: int,
) -> tuple[DynamicContextTask | None, str, list[dict[str, Any]]]:
    source_file = _clean_dynamic_path(request.get("source_file"))
    missing_files = [_clean_dynamic_path(path) for path in request.get("missing_files", []) if _clean_dynamic_path(path)]
    if not source_file:
        return None, "source_file 为空或不安全", []
    if not missing_files:
        return None, "missing_files 为空", []

    file_statuses: list[dict[str, Any]] = []
    source_info, source_status = await _dynamic_file_info_for_path(
        settings,
        summary,
        parent_chunk,
        source_file,
        role="source_file",
        round_index=round_index,
        reason=str(request.get("reason") or ""),
    )
    file_statuses.append(source_status)
    if source_info is None:
        return None, "source_file 无法读取", file_statuses

    files = [source_info]
    seen_paths = {_clean_dynamic_path(source_info.get("filename"))}
    valid_missing_count = 0
    for path in missing_files:
        if path in seen_paths:
            file_statuses.append({"role": "missing_file", "path": path, "status": "duplicate", "source": ""})
            continue
        file_info, status = await _dynamic_file_info_for_path(
            settings,
            summary,
            parent_chunk,
            path,
            role="missing_file",
            round_index=round_index,
            reason=str(request.get("reason") or ""),
        )
        file_statuses.append(status)
        if file_info is None:
            continue
        files.append(file_info)
        seen_paths.add(path)
        valid_missing_count += 1

    if valid_missing_count <= 0:
        return None, "missing_files 均无法读取", file_statuses

    chunk = DiffChunk(
        index=chunk_index,
        total=chunk_index,
        files=files,
        patch_chars=sum(_file_patch_chars(file_info) for file_info in files),
    )
    task = DynamicContextTask(
        parent_chunk=parent_chunk,
        chunk=chunk,
        request=request,
        previous_analysis=previous_analysis,
        round_index=round_index,
        request_index=request_index,
        file_statuses=file_statuses,
    )
    _record_task_log(
        settings,
        "动态补充请求入队",
        {
            "轮次": round_index,
            "请求序号": request_index,
            "来源分片": f"{parent_chunk.index}/{parent_chunk.total}",
            "动态分片": f"{chunk.index}/{chunk.total}",
            "源文件": source_file,
            "缺少文件": missing_files,
            "实际文件": _chunk_file_names(chunk),
            "原因": request.get("reason"),
            "优先级": request.get("priority"),
            "文件处理": file_statuses,
        },
    )
    return task, "", file_statuses

async def _dynamic_file_info_for_path(
    settings: PluginConfig,
    summary: DiffSummary,
    parent_chunk: DiffChunk,
    path: str,
    *,
    role: str,
    round_index: int,
    reason: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path = _clean_dynamic_path(path)
    if not _is_safe_relative_path(path):
        return None, {"role": role, "path": path, "status": "invalid_path", "source": "", "reason": "path 不是安全的相对路径"}

    file_info = _file_by_path(summary, path)
    if file_info is not None:
        return file_info, {"role": role, "path": path, "status": "ok", "source": "diff"}

    tool_results = await execute_tool_calls(
        settings,
        summary,
        parent_chunk,
        [
            {
                "tool": "read_changed_file",
                "path": path,
                "query": "",
                "reason": reason or "动态补充请求需要读取缺少文件全文",
            }
        ],
        round_index=round_index,
    )
    result = tool_results[0] if tool_results else {}
    status = str(result.get("status") or "failed")
    content = str(result.get("content") or "")
    source = str(result.get("source") or "")
    if status in {"ok", "partial"} and content:
        synthetic = {
            "filename": path,
            "status": "context",
            "additions": 0,
            "deletions": 0,
            "changes": 0,
            "patch": f"(动态补充上下文，来源: {source or status})\n{content}",
            "blob_url": "",
            "raw_url": "",
        }
        return synthetic, {
            "role": role,
            "path": path,
            "status": status,
            "source": source,
            "content_chars": result.get("content_chars"),
            "truncated": "是" if result.get("truncated") else "否",
        }
    return None, {
        "role": role,
        "path": path,
        "status": status,
        "source": source,
        "reason": content[:300],
    }

def _merge_dynamic_parent_results(
    summary: DiffSummary,
    chunks: list[DiffChunk],
    ordered_results: list[ChunkAnalysis],
    parent_chunks: dict[int, list[DiffChunk]],
    parent_results: dict[int, list[ChunkAnalysis]],
) -> list[ChunkAnalysis]:
    final_results: list[ChunkAnalysis] = []
    for chunk, original_result in zip(chunks, ordered_results):
        related_chunks = parent_chunks.get(chunk.index, [chunk])
        related_results = parent_results.get(chunk.index, [original_result])
        if len(related_results) <= 1:
            final_results.append(original_result)
            continue

        merge_results = [
            result if index == 0 or not result.error else _dynamic_failure_as_uncertainty(result)
            for index, result in enumerate(related_results)
        ]
        try:
            merged = merge_chunk_analyses(summary, related_chunks, merge_results)
            merged = _remove_resolved_uncertainties(merged, _collect_resolved_uncertainties(related_results))
        except Exception as exc:
            logger.warning("[%s] 动态补充结果合并失败 chunk %d/%d: %s", PLUGIN_NAME, chunk.index, chunk.total, exc)
            final_results.append(
                ChunkAnalysis(
                    chunk.index,
                    chunk.total,
                    original_result.analysis,
                    error=original_result.error,
                    raw_text=original_result.raw_text,
                    token_usage=sum_token_usage(result.token_usage for result in related_results),
                )
            )
            continue

        original_errors = [result.error for index, result in enumerate(related_results) if index == 0 and result.error]
        raw_text = "\n\n".join(result.raw_text for result in related_results if result.raw_text)
        final_results.append(
            ChunkAnalysis(
                chunk.index,
                chunk.total,
                merged,
                error="; ".join(original_errors),
                raw_text=raw_text,
                token_usage=sum_token_usage(result.token_usage for result in related_results),
            )
        )
    return final_results

def _dynamic_failure_as_uncertainty(result: ChunkAnalysis) -> ChunkAnalysis:
    reason = str(result.error or result.analysis.get("summary") or "动态补充上下文分析失败").strip()
    analysis = normalize_analysis(
        {
            "report_title": "",
            "summary": "动态补充上下文分析未完成。",
            "importance": "中",
            "update_sections": [],
            "ai_analysis": {
                "changed_content": [],
                "player_impact": [],
                "uncertainties": [f"动态补充上下文分析失败：{reason}"],
                "recommendation": "",
            },
            "tags": ["动态补充"],
        }
    )
    return ChunkAnalysis(
        result.chunk_index,
        result.chunk_total,
        analysis,
        raw_text=result.raw_text,
        token_usage=result.token_usage,
    )

def _remove_resolved_uncertainties(analysis: dict[str, Any], resolved_uncertainties: list[str]) -> dict[str, Any]:
    if not resolved_uncertainties:
        return normalize_analysis(analysis)
    resolved = {str(item or "").strip() for item in resolved_uncertainties if str(item or "").strip()}
    updated = dict(analysis)
    ai_analysis = updated.get("ai_analysis") if isinstance(updated.get("ai_analysis"), dict) else {}
    updated_ai = dict(ai_analysis)
    uncertainties = [
        item
        for item in updated_ai.get("uncertainties", [])
        if str(item or "").strip() not in resolved
    ]
    updated_ai["uncertainties"] = uncertainties
    updated["ai_analysis"] = updated_ai
    updated["risks"] = uncertainties
    updated["resolved_uncertainties"] = resolved_uncertainties
    return normalize_analysis(updated)

def _collect_resolved_uncertainties(results: list[ChunkAnalysis]) -> list[str]:
    seen: set[str] = set()
    resolved: list[str] = []
    for result in results:
        for item in _analysis_resolved_uncertainties(result.analysis):
            if item in seen:
                continue
            seen.add(item)
            resolved.append(item)
    return resolved

def _analysis_uncertainties(analysis: dict[str, Any]) -> list[str]:
    if not isinstance(analysis, dict):
        return []
    ai_analysis = analysis.get("ai_analysis") if isinstance(analysis.get("ai_analysis"), dict) else {}
    values = ai_analysis.get("uncertainties") if isinstance(ai_analysis, dict) else []
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item or "").strip()]

def _analysis_context_requests(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(analysis, dict):
        return []
    requests = analysis.get("context_requests")
    if not isinstance(requests, list):
        return []
    return [request for request in requests if isinstance(request, dict)]


def _auto_context_requests_from_uncertainties(
    summary: DiffSummary,
    parent_chunk: DiffChunk,
    uncertainties: list[str],
    *,
    max_files: int,
) -> list[dict[str, Any]]:
    if not _uncertainties_need_context(uncertainties):
        return []

    parent_paths = {_clean_dynamic_path(file_info.get("filename")) for file_info in parent_chunk.files}
    parent_paths.discard("")
    if not parent_paths:
        return []

    candidates = _mentioned_changed_files(summary, uncertainties, parent_paths)
    if not candidates:
        candidates = _related_changed_files(summary, parent_paths)
    if not candidates:
        return []

    source_file = next(
        (
            _clean_dynamic_path(file_info.get("filename"))
            for file_info in parent_chunk.files
            if _clean_dynamic_path(file_info.get("filename")) in parent_paths
        ),
        "",
    )
    if not source_file:
        return []
    reason_text = "；".join(uncertainties[:3])[:500]
    return [
        {
            "source_file": source_file,
            "missing_files": candidates[:max_files],
            "reason": f"模型写入不确定点但未输出 context_requests，插件按相关文件自动补充。{reason_text}",
            "priority": "中",
            "auto_generated": "是",
        }
    ]


def _uncertainties_need_context(uncertainties: list[str]) -> bool:
    text = "\n".join(str(item or "") for item in uncertainties)
    if not text.strip():
        return False
    if "模型未主动覆盖" in text and "插件已按点名册补入" in text:
        return False
    indicators = (
        "缺少",
        "缺失",
        "缺乏",
        "信息不足",
        "上下文不足",
        "无法确认",
        "无法确定",
        "不能确认",
        "不能确定",
        "需要对比",
        "需要结合",
        "需要关联",
        "需要更多上下文",
        "需要完整文件",
        "同目录",
        "同组",
        "关联文件",
    )
    return any(indicator in text for indicator in indicators)


def _mentioned_changed_files(summary: DiffSummary, uncertainties: list[str], parent_paths: set[str]) -> list[str]:
    text = "\n".join(uncertainties)
    result: list[str] = []
    seen: set[str] = set()
    for file_info in summary.files:
        path = _clean_dynamic_path(file_info.get("filename"))
        if not path or path in parent_paths:
            continue
        basename = path.rsplit("/", 1)[-1]
        if path not in text and basename not in text:
            continue
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _related_changed_files(summary: DiffSummary, parent_paths: set[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in group_related_files(summary.files):
        group_paths = [_clean_dynamic_path(file_info.get("filename")) for file_info in group]
        if not any(path in parent_paths for path in group_paths):
            continue
        for path in group_paths:
            if not path or path in parent_paths or path in seen:
                continue
            seen.add(path)
            result.append(path)
    parent_directory_suffixes = {_dynamic_directory_suffix(path) for path in parent_paths}
    parent_directory_suffixes.discard(("", ""))
    for file_info in summary.files:
        path = _clean_dynamic_path(file_info.get("filename"))
        if not path or path in parent_paths or path in seen:
            continue
        if _dynamic_directory_suffix(path) not in parent_directory_suffixes:
            continue
        seen.add(path)
        result.append(path)
    return result


def _dynamic_directory_suffix(path: str) -> tuple[str, str]:
    directory, _, basename = path.rpartition("/")
    _stem, dot, suffix = basename.rpartition(".")
    return directory.lower(), suffix.lower() if dot else ""


def _analysis_resolved_uncertainties(analysis: dict[str, Any]) -> list[str]:
    if not isinstance(analysis, dict):
        return []
    values = analysis.get("resolved_uncertainties")
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item or "").strip()]

def _limit_context_request_files(request: dict[str, Any], max_files: int) -> dict[str, Any]:
    updated = dict(request)
    missing_files = updated.get("missing_files") if isinstance(updated.get("missing_files"), list) else []
    updated["missing_files"] = missing_files[:max_files]
    return updated

def _context_request_key(parent_chunk: DiffChunk, request: dict[str, Any]) -> tuple[int, str, tuple[str, ...]]:
    source_file = _clean_dynamic_path(request.get("source_file"))
    missing_files = tuple(_clean_dynamic_path(path) for path in request.get("missing_files", []) if _clean_dynamic_path(path))
    return parent_chunk.index, source_file, missing_files

def _count_missing_file_statuses(file_statuses: list[dict[str, Any]]) -> int:
    return sum(
        1
        for status in file_statuses
        if str(status.get("role") or "") == "missing_file"
        and str(status.get("status") or "") not in {"ok", "partial", "duplicate"}
    )

def _file_by_path(summary: DiffSummary, path: str) -> dict[str, Any] | None:
    normalized = _clean_dynamic_path(path)
    for file_info in summary.files:
        if _clean_dynamic_path(file_info.get("filename")) == normalized:
            return file_info
    return None

def _clean_dynamic_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if path.startswith("/") or ".." in path.split("/"):
        return ""
    return path.lstrip("./")

def _is_safe_relative_path(path: str) -> bool:
    return bool(path) and not path.startswith("/") and ".." not in path.split("/")

def _file_patch_chars(file_info: dict[str, Any]) -> int:
    return len(str(file_info.get("patch") or "")) + len(str(file_info.get("filename") or ""))

def _chunk_file_names(chunk: DiffChunk) -> list[str]:
    return [str(file_info.get("filename") or "") for file_info in chunk.files]

def _record_task_log(settings: PluginConfig, event: str, metadata: dict[str, Any]) -> None:
    recorder = getattr(settings, "task_log_recorder", None)
    if not callable(recorder):
        return
    try:
        recorder(event, metadata)
    except Exception as exc:
        logger.warning("[%s] 写入动态补充任务日志失败: %s", PLUGIN_NAME, exc)

async def refine_analysis_with_tool_calls(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    analysis: dict[str, Any],
    *,
    semaphore: asyncio.Semaphore | None = None,
    provider_id: str | None = None,
) -> tuple[dict[str, Any], TokenUsage, list[str]]:
    if not getattr(settings, "enable_model_tool_calls", False):
        return analysis, TokenUsage(), []

    max_rounds = max(0, int(getattr(settings, "max_tool_call_rounds", 0) or 0))
    if max_rounds <= 0:
        return analysis, TokenUsage(), []

    usage = TokenUsage()
    raw_texts: list[str] = []
    github_file_cache: dict[str, str] = {}
    current_analysis = analysis

    for round_index in range(1, max_rounds + 1):
        tool_calls = current_analysis.get("tool_calls") if isinstance(current_analysis, dict) else []
        if not isinstance(tool_calls, list) or not tool_calls:
            break

        tool_results = await execute_tool_calls(
            settings,
            summary,
            chunk,
            tool_calls,
            round_index=round_index,
            github_file_cache=github_file_cache,
        )
        prompt = build_tool_refinement_prompt(
            settings,
            summary,
            chunk,
            current_analysis,
            tool_results,
            round_index=round_index,
            remaining_rounds=max_rounds - round_index,
        )

        if semaphore is None:
            response = await request_llm(
                context,
                settings,
                prompt,
                provider_id=provider_id,
                allow_fallback=False if provider_id is not None else True,
                summary=summary,
                chunk=chunk,
                purpose="补充上下文分析",
            )
        else:
            async with semaphore:
                response = await request_llm(
                    context,
                    settings,
                    prompt,
                    provider_id=provider_id,
                    allow_fallback=False if provider_id is not None else True,
                    summary=summary,
                    chunk=chunk,
                    purpose="补充上下文分析",
                )
        usage += extract_token_usage(response)
        ensure_usable_llm_response(response)
        raw_text = extract_response_text(response)
        raw_texts.append(raw_text)
        parsed, repair_usage = await parse_or_repair_analysis_with_usage(
            context,
            settings,
            summary,
            chunk,
            raw_text,
            semaphore=semaphore,
            purpose="补充上下文分析 JSON 修复",
        )
        usage += repair_usage
        current_analysis = parsed

    if _has_tool_calls(current_analysis):
        current_analysis = _append_tool_limit_uncertainty(current_analysis)

    return current_analysis, usage, raw_texts

def _has_tool_calls(analysis: dict[str, Any]) -> bool:
    tool_calls = analysis.get("tool_calls") if isinstance(analysis, dict) else []
    return isinstance(tool_calls, list) and bool(tool_calls)

def _append_tool_limit_uncertainty(analysis: dict[str, Any]) -> dict[str, Any]:
    updated = dict(analysis)
    ai_analysis = updated.get("ai_analysis") if isinstance(updated.get("ai_analysis"), dict) else {}
    updated_ai = dict(ai_analysis)
    uncertainties = list(updated_ai.get("uncertainties") or [])
    message = "模型工具调用轮数已达上限，仍缺少的补充上下文需要人工复核。"
    if message not in uncertainties:
        uncertainties.append(message)
    updated_ai["uncertainties"] = uncertainties
    updated["ai_analysis"] = updated_ai
    updated["tool_calls"] = []
    return normalize_analysis(updated)

def split_chunk_for_retry(chunk: DiffChunk) -> list[DiffChunk]:
    target_size = max(1, (len(chunk.files) + 1) // 2)
    groups = [chunk.files[index : index + target_size] for index in range(0, len(chunk.files), target_size)]
    total = len(groups)
    return [
        DiffChunk(index=index + 1, total=total, files=files, patch_chars=sum(len(str(item.get("patch") or "")) + len(str(item.get("filename") or "")) for item in files))
        for index, files in enumerate(groups)
    ]

def analysis_request_provider_ids(settings: PluginConfig) -> list[str | None]:
    provider_ids: list[str | None] = [str(settings.provider_id or "").strip() or None]
    seen = {provider_ids[0] or ""}
    for provider_id in settings.backup_provider_ids:
        normalized = str(provider_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        provider_ids.append(normalized)
    return provider_ids

def provider_label(provider_id: str | None) -> str:
    return str(provider_id or "").strip() or "默认模型"

def chunk_result_needs_provider_fallback(result: ChunkAnalysis) -> bool:
    if str(result.error or "").strip():
        return True
    tags = result.analysis.get("tags") if isinstance(result.analysis, dict) else []
    if isinstance(tags, list):
        return any(str(tag).strip() == "需复核" for tag in tags)
    return False

def chunk_result_failure_text(result: ChunkAnalysis) -> str:
    error = str(result.error or "").strip()
    if error:
        return error
    if isinstance(result.analysis, dict):
        return str(result.analysis.get("summary") or "模型分析结果需要复核").strip()
    return "模型分析结果需要复核"

async def refine_chunk_analyses(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    *,
    merge_error: str = "",
) -> dict[str, Any]:
    analysis, _ = await refine_chunk_analyses_with_usage(
        context,
        settings,
        summary,
        chunks,
        results,
        merge_error=merge_error,
    )
    return analysis

async def refine_chunk_analyses_with_usage(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    *,
    merge_error: str = "",
) -> tuple[dict[str, Any], TokenUsage]:
    prompt = build_chunk_refinement_prompt(settings, summary, chunks, results, merge_error=merge_error)
    token_usage = TokenUsage()
    try:
        response = await request_llm(
            context,
            settings,
            prompt,
            provider_id=settings.effective_summary_provider_id,
            summary=summary,
            purpose="分片结果总结",
        )
        token_usage = extract_token_usage(response)
        text = extract_response_text(response)
        parsed = parse_analysis_json(text)
        if parsed is None:
            raise ValueError("总结模型输出不是有效 JSON")
        return enforce_change_coverage(summary, chunks, parsed), token_usage
    except Exception as exc:
        record_model_error(
            settings,
            "summary_from_chunks_failed",
            exc,
            summary=summary,
            extra={"merge_error": merge_error},
        )
        logger.warning("[%s] 分片总结模型分析失败，使用兜底报告: %s", PLUGIN_NAME, exc)
        return (
            enforce_change_coverage(
                summary,
                chunks,
                fallback_analysis("模型分片分析已完成，但程序合并和总结模型分析均失败，需要结合 GitHub 原始 diff 复核。"),
            ),
            token_usage,
        )

def sum_token_usage(usages: Any) -> TokenUsage:
    total = TokenUsage()
    for usage in usages:
        if isinstance(usage, TokenUsage):
            total += usage
    return total
