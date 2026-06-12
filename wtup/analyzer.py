from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from .config import PLUGIN_NAME, PluginConfig
from .diff_collector import DiffChunk, DiffSummary, render_chunk_input


@dataclass(frozen=True)
class ChunkAnalysis:
    chunk_index: int
    chunk_total: int
    analysis: dict[str, Any]
    error: str = ""
    raw_text: str = ""


def _analysis_prompt_text(settings: PluginConfig) -> str:
    return str(settings.analysis_prompt or "").strip()


def _summary_prompt_text(settings: PluginConfig) -> str:
    return str(settings.effective_summary_prompt or "").strip()


def build_prompt(settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> str:
    return f"""
{_analysis_prompt_text(settings)}

你正在分析固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结这些变更中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {{
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {{
          "text": "条目内容",
          "children": [
            {{"text": "子条目内容", "children": []}}
          ]
        }}
      ]
    }}
  ],
  "ai_analysis": {{
    "changed_content": ["AI 分析出的实际改动内容"],
    "player_impact": ["对玩家、载具、经济、任务、地图或战斗体验的可能影响"],
    "uncertainties": ["不确定点、需要继续观察的地方"],
    "recommendation": "是否建议玩家关注/更新，以及原因"
  }},
  "tags": ["标签1", "标签2"]
}}

要求：
1. 用中文。
2. 输出内容格式参考 War Thunder Datamine 更新日志：先按条目列出更新内容，再在下面列出 AI 分析的改动内容。
3. update_sections 使用中文标题，例如“新增载具”“新增文本”“武器调整”“经济调整”“其他变化”。
4. update_sections.items 支持多级 children；要像更新日志一样保留层级，不要把所有内容压成一段。
5. 如果出现载具名，且能从 diff 或文本中判断英文名和中文名，必须写成 英文名(中文名)，例如 JH-7A(飞豹)。无法判断中文名时只写原名，不要编造。
6. 优先解释数据变化可能代表什么，不要只复述文件名。
7. 如果信息不足，要明确写“不确定”，不要编造。
8. changed_content、player_impact、uncertainties 每个数组最多 5 条，每条尽量短。
9. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加 Part、分片、说明文字或其他内容。
10. summary 会显示在标题下面的小字行，可以写描述性标题；不要把描述性标题写进 report_title。
11. 不要在 report_title、summary、update_sections.title 或正文条目中写 Part、分片、第几批等分页信息。
12. 当前是内部模型请求第 {chunk.index}/{chunk.total} 批；该信息只用于你理解输入范围，最终报告会由程序合并，不要输出批次信息。

以下是 GitHub 变更数据：

{render_chunk_input(summary, chunk)}
""".strip()


def build_json_repair_prompt(settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk, raw_text: str) -> str:
    files = "\n".join(f"- {file_info.get('filename') or ''}" for file_info in chunk.files)
    return f"""
{_analysis_prompt_text(settings)}

上一次模型分析返回的内容不是有效 JSON。请基于“上次模型原始输出”和“当前分片文件列表”重新整理为严格 JSON。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结这些变更中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {{
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {{
          "text": "条目内容",
          "children": [
            {{"text": "子条目内容", "children": []}}
          ]
        }}
      ]
    }}
  ],
  "ai_analysis": {{
    "changed_content": ["AI 分析出的实际改动内容"],
    "player_impact": ["对玩家、载具、经济、任务、地图或战斗体验的可能影响"],
    "uncertainties": ["不确定点、需要继续观察的地方"],
    "recommendation": "是否建议玩家关注/更新，以及原因"
  }},
  "tags": ["标签1", "标签2"]
}}

要求：
1. 用中文。
2. 只能使用上次模型原始输出和文件列表中已有的信息，不要新增未经输入支持的内容。
3. 如果上次输出无法判断实际改动，生成需复核条目，并把原因写入 uncertainties。
4. 不要在 report_title、summary、update_sections.title 或正文条目中写 Part、分片、第几批等分页信息。

提交范围: {summary.base_sha[:7] or "unknown"}...{summary.head_sha[:7] or "unknown"}
当前分片: {chunk.index}/{chunk.total}
当前分片文件:
{files}

上次模型原始输出:
{str(raw_text or "").strip()[:8000]}
""".strip()


def build_refinement_prompt(settings: PluginConfig, summary: DiffSummary, merged_analysis: dict[str, Any]) -> str:
    merged_json = json.dumps(normalize_analysis(merged_analysis), ensure_ascii=False, indent=2)
    return f"""
{_summary_prompt_text(settings)}

你正在整理固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新最终报告。

前面已经按 diff 分片完成多次模型分析，程序也已经把分片分析结果初步合并为 JSON。
你的任务不是重新分析原始 diff，而是基于这个初步合并 JSON 做二次整理，生成更适合最终推送的报告。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结本次更新中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {{
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {{
          "text": "条目内容",
          "children": [
            {{"text": "子条目内容", "children": []}}
          ]
        }}
      ]
    }}
  ],
  "ai_analysis": {{
    "changed_content": ["AI 分析出的实际改动内容"],
    "player_impact": ["对玩家、载具、经济、任务、地图或战斗体验的可能影响"],
    "uncertainties": ["不确定点、需要继续观察的地方"],
    "recommendation": "是否建议玩家关注/更新，以及原因"
  }},
  "tags": ["标签1", "标签2"]
}}

要求：
1. 用中文。
2. 只能使用初步合并 JSON 中已有的信息，不要新增未经输入支持的内容。
3. 去重重复条目，合并含义相近的条目。
4. 保留重要的载具、武器、经济、任务、地图、文本等改动。
5. update_sections 使用中文标题，并保留条目层级。
6. 不要在 report_title、summary、update_sections.title 或正文条目中写 Part、分片、第几批等分页信息。
7. changed_content、player_impact、uncertainties 每个数组最多 5 条，每条尽量短。
8. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加其他说明文字。
9. 如果初步合并 JSON 中有分析失败或信息不足的内容，要保留到 uncertainties。

提交范围: {summary.base_sha[:7] or "unknown"}...{summary.head_sha[:7] or "unknown"}
提交数: {summary.total_commits}
文件数: {summary.total_files}
初步合并 JSON:
{merged_json}
""".strip()


def build_chunk_refinement_prompt(
    settings: PluginConfig,
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    *,
    merge_error: str = "",
) -> str:
    chunk_json = json.dumps(
        build_chunk_refinement_payload(summary, chunks, results, merge_error=merge_error),
        ensure_ascii=False,
        indent=2,
    )
    return f"""
{_summary_prompt_text(settings)}

你正在整理固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新最终报告。

前面已经按 diff 分片完成多次模型分析，但程序合并分片结果时失败了。
你的任务是直接基于每个分片的原始分析 JSON 做二次整理，生成最终报告。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结本次更新中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {{
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {{
          "text": "条目内容",
          "children": [
            {{"text": "子条目内容", "children": []}}
          ]
        }}
      ]
    }}
  ],
  "ai_analysis": {{
    "changed_content": ["AI 分析出的实际改动内容"],
    "player_impact": ["对玩家、载具、经济、任务、地图或战斗体验的可能影响"],
    "uncertainties": ["不确定点、需要继续观察的地方"],
    "recommendation": "是否建议玩家关注/更新，以及原因"
  }},
  "tags": ["标签1", "标签2"]
}}

要求：
1. 用中文。
2. 只能使用分片分析 JSON 和 raw_text 中已有的信息，不要新增未经输入支持的内容。
3. 去重重复条目，合并含义相近的条目。
4. 保留重要的载具、武器、经济、任务、地图、文本等改动。
5. update_sections 使用中文标题，并保留条目层级。
6. 不要在 report_title、summary、update_sections.title 或正文条目中写 Part、分片、第几批等分页信息。
7. changed_content、player_impact、uncertainties 每个数组最多 5 条，每条尽量短。
8. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加其他说明文字。
9. 程序合并失败原因和分片分析失败信息必须保留到 uncertainties。

分片分析数据:
{chunk_json}
""".strip()


def build_chunk_refinement_payload(
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    *,
    merge_error: str = "",
) -> dict[str, Any]:
    chunk_map = {chunk.index: chunk for chunk in chunks}
    ordered_results = order_chunk_results(chunks, results)
    empty_chunk = DiffChunk(index=0, total=0, files=[], patch_chars=0)
    return {
        "commit_range": f"{summary.base_sha[:7] or 'unknown'}...{summary.head_sha[:7] or 'unknown'}",
        "total_commits": summary.total_commits,
        "total_files": summary.total_files,
        "merge_error": str(merge_error or "").strip(),
        "chunks": [
            {
                "chunk_index": result.chunk_index,
                "chunk_total": result.chunk_total,
                "error": result.error,
                "files": [
                    str(file_info.get("filename") or "")
                    for file_info in chunk_map.get(result.chunk_index, empty_chunk).files
                ],
                "analysis": json_safe(result.analysis),
                "raw_text": result.raw_text[:4000],
            }
            for result in ordered_results
        ],
    }


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def estimate_input_tokens(text: str) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    ascii_chars = sum(1 for char in raw if ord(char) < 128)
    non_ascii_chars = len(raw) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + non_ascii_chars)


def estimate_chunk_input_tokens(settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> int:
    return estimate_input_tokens(build_prompt(settings, summary, chunk))


def split_chunks_by_token_limit(settings: PluginConfig, summary: DiffSummary) -> DiffSummary:
    limit = int(getattr(settings, "max_input_token_limit", 0) or 0)
    if limit <= 0:
        return summary

    split_groups: list[list[dict[str, Any]]] = []
    for chunk in summary.chunks:
        split_groups.extend(_split_file_group_by_token_limit(settings, summary, chunk.files, limit))

    if len(split_groups) == len(summary.chunks) and all(
        group == chunk.files for group, chunk in zip(split_groups, summary.chunks)
    ):
        return summary

    total = len(split_groups) or 1
    chunks = [
        DiffChunk(index=index + 1, total=total, files=files, patch_chars=sum(file_patch_chars(item) for item in files))
        for index, files in enumerate(split_groups or [[]])
    ]
    return DiffSummary(
        base_sha=summary.base_sha,
        head_sha=summary.head_sha,
        compare_url=summary.compare_url,
        total_commits=summary.total_commits,
        total_files=summary.total_files,
        additions=summary.additions,
        deletions=summary.deletions,
        changed_files=summary.changed_files,
        commits=summary.commits,
        files=summary.files,
        chunks=chunks,
    )


def _split_file_group_by_token_limit(
    settings: PluginConfig,
    summary: DiffSummary,
    files: list[dict[str, Any]],
    limit: int,
) -> list[list[dict[str, Any]]]:
    if len(files) <= 1:
        if files:
            single = DiffChunk(index=1, total=1, files=files, patch_chars=sum(file_patch_chars(item) for item in files))
            tokens = estimate_chunk_input_tokens(settings, summary, single)
            if tokens > limit:
                logger.warning(
                    "[%s] 单文件模型输入约 %d token，超过限制 %d token；为保证文件完整性不拆分: %s",
                    PLUGIN_NAME,
                    tokens,
                    limit,
                    files[0].get("filename") or "",
                )
        return [files]

    probe = DiffChunk(index=1, total=1, files=files, patch_chars=sum(file_patch_chars(item) for item in files))
    if estimate_chunk_input_tokens(settings, summary, probe) <= limit:
        return [files]

    midpoint = (len(files) + 1) // 2
    return [
        *_split_file_group_by_token_limit(settings, summary, files[:midpoint], limit),
        *_split_file_group_by_token_limit(settings, summary, files[midpoint:], limit),
    ]


def file_patch_chars(file_info: dict[str, Any]) -> int:
    return len(str(file_info.get("patch") or "")) + len(str(file_info.get("filename") or ""))


def record_model_error(
    settings: PluginConfig,
    stage: str,
    error: BaseException | str,
    *,
    summary: DiffSummary | None = None,
    chunk: DiffChunk | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    recorder = getattr(settings, "model_error_recorder", None)
    if not callable(recorder):
        return
    metadata: dict[str, Any] = dict(extra or {})
    if summary is not None:
        metadata.update(
            {
                "base_sha": summary.base_sha,
                "head_sha": summary.head_sha,
                "compare_url": summary.compare_url,
                "total_files": summary.total_files,
            }
        )
    if chunk is not None:
        metadata.update(
            {
                "chunk_index": chunk.index,
                "chunk_total": chunk.total,
                "chunk_files": [str(file_info.get("filename") or "") for file_info in chunk.files],
                "chunk_patch_chars": chunk.patch_chars,
            }
        )
    try:
        recorder(stage, error, metadata)
    except Exception as exc:
        logger.warning("[%s] 保存模型错误日志失败: %s", PLUGIN_NAME, exc)


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


def safe_normalize_analysis(text: str) -> dict[str, Any]:
    parsed = parse_analysis_json(text)
    if parsed is not None:
        return parsed
    return fallback_analysis(
        "模型输出格式未按 JSON 返回，相关内容需要结合 GitHub 原始 diff 复核。",
        raw_text=text,
    )


def fallback_analysis(reason: str, *, raw_text: str = "") -> dict[str, Any]:
    reason = str(reason or "").strip() or "模型分析结果不可用，需要结合 GitHub 原始 diff 复核。"
    raw_text = str(raw_text or "").strip()
    item_text = raw_text[:1200] if raw_text else reason
    return {
        "summary": reason,
        "report_title": "",
        "importance": "中",
        "update_sections": [
            {
                "title": "其他变化",
                "items": [{"text": item_text, "children": []}],
            }
        ],
        "highlights": [item_text],
        "player_impact": [],
        "risks": [reason],
        "recommendation": "请结合 GitHub 原始 diff 复核。",
        "ai_analysis": {
            "changed_content": [item_text],
            "player_impact": [],
            "uncertainties": [reason],
            "recommendation": "请结合 GitHub 原始 diff 复核。",
        },
        "tags": ["需复核"],
        "raw_text": raw_text,
    }


def merge_chunk_analyses(
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
) -> dict[str, Any]:
    ordered_results = order_chunk_results(chunks, results)
    analyses = [coerce_analysis(result.analysis) for result in ordered_results]

    report_title = first_text(analysis.get("report_title") for analysis in analyses)
    importance = max_importance(analysis.get("importance") for analysis in analyses)
    tags = unique_preserve_order(tag for analysis in analyses for tag in analysis.get("tags", []))
    update_sections = merge_update_sections(analyses)

    changed_content = unique_preserve_order(
        item
        for analysis in analyses
        for item in get_ai_analysis(analysis).get("changed_content", [])
    )[:10]
    player_impact = unique_preserve_order(
        item
        for analysis in analyses
        for item in get_ai_analysis(analysis).get("player_impact", [])
    )[:10]
    uncertainties = unique_preserve_order(
        item
        for analysis in analyses
        for item in get_ai_analysis(analysis).get("uncertainties", [])
    )[:10]
    if any(result.error for result in ordered_results):
        uncertainties = unique_preserve_order(
            [*uncertainties, "部分文件模型分析失败，需要结合 GitHub 原始 diff 复核。"]
        )[:10]

    recommendation = first_recommendation_by_importance(analyses) or "建议关注本次更新，并结合游戏内实装情况复核。"
    summary_text = f"本次更新共 {summary.total_files} 个文件。"
    if summary.total_commits:
        summary_text = f"本次更新包含 {summary.total_commits} 个提交、{summary.total_files} 个文件。"

    return normalize_analysis(
        {
            "report_title": report_title,
            "summary": summary_text,
            "importance": importance,
            "update_sections": update_sections,
            "ai_analysis": {
                "changed_content": changed_content,
                "player_impact": player_impact,
                "uncertainties": uncertainties,
                "recommendation": recommendation,
            },
            "tags": tags,
        }
    )


def order_chunk_results(chunks: list[DiffChunk], results: list[ChunkAnalysis]) -> list[ChunkAnalysis]:
    result_map = {result.chunk_index: result for result in results}
    ordered: list[ChunkAnalysis] = []
    for chunk in sorted(chunks, key=lambda item: item.index):
        result = result_map.get(chunk.index)
        if result is None:
            ordered.append(
                ChunkAnalysis(
                    chunk.index,
                    chunk.total,
                    fallback_analysis("部分文件未获得模型分析结果，需要结合 GitHub 原始 diff 复核。"),
                    error="missing analysis result",
                )
            )
        else:
            ordered.append(result)
    return ordered


def coerce_analysis(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        try:
            return normalize_analysis(value)
        except Exception as exc:
            logger.warning("[%s] 标准化模型结果失败: %s", PLUGIN_NAME, exc)
    return fallback_analysis("模型分析结果结构异常，需要结合 GitHub 原始 diff 复核。")


def merge_update_sections(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index_by_title: dict[str, int] = {}
    for analysis in analyses:
        for section in analysis.get("update_sections", []):
            if not isinstance(section, dict):
                continue
            title = clean_section_title(section.get("title"))
            items = normalize_update_items(section.get("items"), limit=100)
            if not items:
                continue
            if title not in index_by_title:
                index_by_title[title] = len(merged)
                merged.append({"title": title, "items": []})
            merged[index_by_title[title]]["items"].extend(items)

    for section in merged:
        section["items"] = dedupe_update_items(section["items"])
    return merged or [{"title": "更新内容", "items": [{"text": "本次更新没有可展示的更新条目。", "children": []}]}]


def clean_section_title(value: Any) -> str:
    title = str(value or "").strip()
    if not title:
        return "其他变化"
    normalized = re.sub(r"\s+", "", title).lower()
    if re.fullmatch(r"(part\d+(/\d+)?|第?\d+(批|部分|分片)|分片\d+(/\d+)?)", normalized):
        return "其他变化"
    if "分片" in title or re.search(r"\bpart\s*\d+", title, flags=re.I):
        return "其他变化"
    return title


def dedupe_update_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        text = str(item.get("text") or "").strip() if isinstance(item, dict) else ""
        if not text or text in seen:
            continue
        seen.add(text)
        children = item.get("children") if isinstance(item.get("children"), list) else []
        result.append({"text": text, "children": dedupe_update_items(children)})
    return result


def get_ai_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    value = analysis.get("ai_analysis")
    return value if isinstance(value, dict) else {}


def max_importance(values: Any) -> str:
    rank = {"低": 0, "中": 1, "高": 2}
    best = "低"
    for value in values:
        normalized = normalize_importance(value)
        if rank[normalized] > rank[best]:
            best = normalized
    return best


def first_recommendation_by_importance(analyses: list[dict[str, Any]]) -> str:
    rank = {"低": 0, "中": 1, "高": 2}
    ordered = sorted(analyses, key=lambda item: rank.get(normalize_importance(item.get("importance")), 1), reverse=True)
    for analysis in ordered:
        recommendation = str(get_ai_analysis(analysis).get("recommendation") or analysis.get("recommendation") or "").strip()
        if recommendation:
            return recommendation
    return ""


def first_text(values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def unique_preserve_order(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def extract_response_text(response: Any) -> str:
    if response is None:
        return ""
    if hasattr(response, "completion_text"):
        return str(response.completion_text or "").strip()
    if hasattr(response, "text"):
        return str(response.text or "").strip()
    choices = getattr(response, "choices", None)
    if choices:
        first_choice = choices[0]
        message = getattr(first_choice, "message", None)
        content = getattr(message, "content", None)
        if content is not None:
            return str(content or "").strip()
    if isinstance(response, str):
        return response.strip()
    return str(response).strip()


def ensure_usable_llm_response(response: Any) -> None:
    reason = llm_failure_reason(response)
    if reason:
        raise RuntimeError(reason)


def llm_failure_reason(response: Any) -> str:
    if response is None:
        return "模型无可用输出"

    choices = getattr(response, "choices", None)
    if choices:
        first_choice = choices[0]
        finish_reason = str(getattr(first_choice, "finish_reason", "") or "").strip()
        text = extract_response_text(response)
        if text:
            return ""
        if finish_reason and finish_reason != "stop":
            return f"模型无可用输出: {finish_reason}"
        return "模型无可用输出"

    if extract_response_text(response):
        return ""
    return "模型无可用输出"


def parse_analysis_json(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S)
    if match:
        raw = match.group(1).strip()
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return normalize_analysis(payload)


def normalize_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    ai_analysis = normalize_ai_analysis(payload)
    return {
        "report_title": str(payload.get("report_title") or "").strip(),
        "summary": str(payload.get("summary") or "").strip() or "本次更新未给出摘要。",
        "importance": normalize_importance(payload.get("importance")),
        "update_sections": normalize_update_sections(payload.get("update_sections")),
        "ai_analysis": ai_analysis,
        "highlights": normalize_list(payload.get("highlights")),
        "player_impact": ai_analysis["player_impact"],
        "risks": ai_analysis["uncertainties"],
        "recommendation": ai_analysis["recommendation"],
        "tags": normalize_list(payload.get("tags"), limit=8),
    }


def normalize_importance(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in {"低", "中", "高"} else "中"


def normalize_list(value: Any, *, limit: int = 5) -> list[str]:
    if isinstance(value, list):
        items = value
    elif value:
        items = [value]
    else:
        items = []
    result = [str(item).strip() for item in items if str(item or "").strip()]
    return result[:limit]


def normalize_ai_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("ai_analysis")
    data = raw if isinstance(raw, dict) else {}
    changed_content = normalize_list(data.get("changed_content") or payload.get("highlights"))
    player_impact = normalize_list(data.get("player_impact") or payload.get("player_impact"))
    uncertainties = normalize_list(data.get("uncertainties") or payload.get("risks"))
    recommendation = str(data.get("recommendation") or payload.get("recommendation") or "").strip()
    return {
        "changed_content": changed_content,
        "player_impact": player_impact,
        "uncertainties": uncertainties,
        "recommendation": recommendation,
    }


def normalize_update_sections(value: Any, *, section_limit: int = 8, item_limit: int = 20) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    sections: list[dict[str, Any]] = []
    for section in value:
        if isinstance(section, dict):
            title = str(section.get("title") or "").strip()
            raw_items = section.get("items")
        else:
            title = ""
            raw_items = section
        items = normalize_update_items(raw_items, limit=item_limit)
        if title and items:
            sections.append({"title": title, "items": items})
        if len(sections) >= section_limit:
            break
    return sections


def normalize_update_items(value: Any, *, limit: int = 20, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 4:
        return []
    if isinstance(value, list):
        items = value
    elif value:
        items = [value]
    else:
        return []

    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("title") or "").strip()
            children = normalize_update_items(item.get("children"), limit=limit, depth=depth + 1)
        else:
            text = str(item or "").strip()
            children = []
        if text:
            result.append({"text": text, "children": children})
        if len(result) >= limit:
            break
    return result


def first_non_empty_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""
