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


def build_prompt(settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> str:
    return f"""
{settings.analysis_prompt}

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


def build_refinement_prompt(settings: PluginConfig, summary: DiffSummary, merged_analysis: dict[str, Any]) -> str:
    merged_json = json.dumps(normalize_analysis(merged_analysis), ensure_ascii=False, indent=2)
    return f"""
{settings.analysis_prompt}

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


async def generate_analysis_from_prompt(context: Any, settings: PluginConfig, prompt: str) -> dict[str, Any]:
    response = await request_llm(context, settings, prompt)
    text = extract_response_text(response)
    return safe_normalize_analysis(text)


async def request_llm(context: Any, settings: PluginConfig, prompt: str) -> Any:
    llm_kwargs: dict[str, Any] = {"prompt": prompt}

    if settings.provider_id:
        try:
            provider = context.get_provider_by_id(provider_id=settings.provider_id)
        except Exception as exc:
            provider = None
            logger.warning("[%s] 获取 Provider %s 失败: %s", PLUGIN_NAME, settings.provider_id, exc)
        if provider:
            llm_kwargs["chat_provider_id"] = settings.provider_id
        else:
            logger.warning("[%s] Provider %s 不存在，改用默认模型", PLUGIN_NAME, settings.provider_id)

    return await asyncio.wait_for(context.llm_generate(**llm_kwargs), timeout=settings.timeout_seconds)


async def analyze_chunk(context: Any, settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> dict[str, Any]:
    prompt = build_prompt(settings, summary, chunk)
    return await generate_analysis_from_prompt(context, settings, prompt)


async def refine_merged_analysis(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    merged_analysis: dict[str, Any],
) -> dict[str, Any]:
    prompt = build_refinement_prompt(settings, summary, merged_analysis)
    try:
        response = await request_llm(context, settings, prompt)
        text = extract_response_text(response)
        parsed = parse_analysis_json(text)
        if parsed is None:
            raise ValueError("二次分析模型输出不是有效 JSON")
        return parsed
    except Exception as exc:
        logger.warning("[%s] 二次分析失败，使用程序合并结果: %s", PLUGIN_NAME, exc)
        return normalize_analysis(merged_analysis)


async def analyze_chunks(context: Any, settings: PluginConfig, summary: DiffSummary) -> list[ChunkAnalysis]:
    results: list[ChunkAnalysis] = []
    for chunk in summary.chunks:
        try:
            analysis = await analyze_chunk(context, settings, summary, chunk)
            results.append(ChunkAnalysis(chunk.index, chunk.total, analysis))
        except Exception as exc:
            logger.warning("[%s] 模型分析失败 chunk %d/%d: %s", PLUGIN_NAME, chunk.index, chunk.total, exc)
            results.append(
                ChunkAnalysis(
                    chunk.index,
                    chunk.total,
                    fallback_analysis("模型分析失败，相关文件需要结合 GitHub 原始 diff 复核。"),
                    error=str(exc),
                )
            )
    return results


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
    if isinstance(response, str):
        return response.strip()
    return str(response).strip()


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
