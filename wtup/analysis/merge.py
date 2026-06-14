from __future__ import annotations

import re
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME
from ..diff_collector import DiffChunk, DiffSummary
from .fallback import fallback_analysis
from .models import ChunkAnalysis
from .normalize import (
    clean_pagination_text,
    normalize_analysis,
    normalize_analysis_coverage,
    normalize_importance,
    normalize_update_items,
)


def merge_chunk_analyses(
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
) -> dict[str, Any]:
    ordered_results = order_chunk_results(chunks, results)
    ordered_chunks = sorted(chunks, key=lambda item: item.index)
    analyses = [coerce_analysis(result.analysis) for result in ordered_results]

    report_title = first_text(analysis.get("report_title") for analysis in analyses)
    importance = max_importance(analysis.get("importance") for analysis in analyses)
    tags = unique_preserve_order(tag for analysis in analyses for tag in analysis.get("tags", []))
    update_sections = merge_update_sections(analyses)

    changed_content = unique_preserve_order(
        clean_pagination_text(item)
        for analysis in analyses
        for item in get_ai_analysis(analysis).get("changed_content", [])
    )[:10]
    player_impact = unique_preserve_order(
        clean_pagination_text(item)
        for analysis in analyses
        for item in get_ai_analysis(analysis).get("player_impact", [])
    )[:10]
    uncertainties = unique_preserve_order(
        clean_pagination_text(item)
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
            "analysis_coverage": merge_analysis_coverage(ordered_chunks, ordered_results, analyses),
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
        text = clean_pagination_text(item.get("text")) if isinstance(item, dict) else ""
        if not text or text in seen:
            continue
        seen.add(text)
        children = item.get("children") if isinstance(item.get("children"), list) else []
        result.append({"text": text, "children": dedupe_update_items(children)})
    return result

def get_ai_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    value = analysis.get("ai_analysis")
    return value if isinstance(value, dict) else {}

def merge_analysis_coverage(
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    analyses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index_by_path: dict[str, int] = {}

    for chunk, result, analysis in zip(chunks, results, analyses):
        coverage_by_path = {
            item["path"]: item
            for item in normalize_analysis_coverage(analysis.get("analysis_coverage"))
            if item.get("path")
        }
        expected_paths = [str(file_info.get("filename") or "").strip().replace("\\", "/") for file_info in chunk.files]
        expected_paths = [path for path in expected_paths if path]
        expected_path_set = set(expected_paths)

        for path in expected_paths:
            item = coverage_by_path.get(path)
            if item is None:
                item = missing_coverage_item(path, error=result.error)
            add_coverage_item(merged, index_by_path, item)

        for path, item in coverage_by_path.items():
            if path not in expected_path_set:
                add_coverage_item(merged, index_by_path, item)

    return merged

def missing_coverage_item(path: str, *, error: str = "") -> dict[str, Any]:
    if str(error or "").strip():
        return {
            "path": path,
            "status": "skipped",
            "covered_changes": [],
            "evidence": [],
            "notes": "该文件所在分片分析失败，未获得模型覆盖标记，需要结合原始 diff 复核。",
        }
    return {
        "path": path,
        "status": "uncertain",
        "covered_changes": [],
        "evidence": [],
        "notes": "模型未返回该文件的分析覆盖标记，需要结合原始 diff 复核。",
    }

def add_coverage_item(
    merged: list[dict[str, Any]],
    index_by_path: dict[str, int],
    item: dict[str, Any],
) -> None:
    normalized_items = normalize_analysis_coverage([item], limit=1)
    if not normalized_items:
        return
    normalized = normalized_items[0]
    path = normalized["path"]
    if path not in index_by_path:
        index_by_path[path] = len(merged)
        merged.append(normalized)
        return

    current = merged[index_by_path[path]]
    current["status"] = stronger_coverage_status(current.get("status"), normalized.get("status"))
    current["covered_changes"] = unique_preserve_order(
        [*list(current.get("covered_changes") or []), *list(normalized.get("covered_changes") or [])]
    )[:20]
    current["evidence"] = unique_preserve_order(
        [*list(current.get("evidence") or []), *list(normalized.get("evidence") or [])]
    )[:20]
    current["notes"] = "; ".join(
        unique_preserve_order([current.get("notes"), normalized.get("notes")])
    )

def stronger_coverage_status(left: Any, right: Any) -> str:
    rank = {"skipped": 0, "uncertain": 1, "analyzed": 2}
    left_text = str(left or "uncertain")
    right_text = str(right or "uncertain")
    return left_text if rank.get(left_text, 1) >= rank.get(right_text, 1) else right_text

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
