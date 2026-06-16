from __future__ import annotations

import json
import re
from typing import Any

from .fallback import fallback_analysis


AI_ANALYSIS_ITEM_LIMIT = 50
UPDATE_SECTION_LIMIT = 100
UPDATE_ITEM_LIMIT = 500


def safe_normalize_analysis(text: str) -> dict[str, Any]:
    parsed = parse_analysis_json(text)
    if parsed is not None:
        return parsed
    return fallback_analysis(
        "模型输出格式未按 JSON 返回，相关内容需要结合 GitHub 原始 diff 复核。",
        raw_text=text,
    )

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
        "report_title": clean_pagination_text(payload.get("report_title")),
        "summary": clean_pagination_text(payload.get("summary")) or "本次更新未给出摘要。",
        "importance": normalize_importance(payload.get("importance")),
        "update_sections": normalize_update_sections(payload.get("update_sections")),
        "ai_analysis": ai_analysis,
        "highlights": normalize_list(payload.get("highlights")),
        "player_impact": ai_analysis["player_impact"],
        "risks": ai_analysis["uncertainties"],
        "recommendation": clean_pagination_text(ai_analysis["recommendation"]),
        "tags": normalize_list(payload.get("tags"), limit=8),
        "tool_calls": normalize_tool_calls(payload.get("tool_calls")),
        "context_requests": normalize_context_requests(payload.get("context_requests")),
        "resolved_uncertainties": normalize_list(payload.get("resolved_uncertainties"), limit=20),
        "coverage": normalize_coverage(payload.get("coverage")),
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
    result = [clean_pagination_text(item) for item in items if clean_pagination_text(item)]
    return result[:limit]

def normalize_ai_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("ai_analysis")
    data = raw if isinstance(raw, dict) else {}
    changed_content = normalize_list(
        data.get("changed_content") or payload.get("highlights"),
        limit=AI_ANALYSIS_ITEM_LIMIT,
    )
    player_impact = normalize_list(
        data.get("player_impact") or payload.get("player_impact"),
        limit=AI_ANALYSIS_ITEM_LIMIT,
    )
    uncertainties = normalize_list(
        data.get("uncertainties") or payload.get("risks"),
        limit=AI_ANALYSIS_ITEM_LIMIT,
    )
    recommendation = clean_pagination_text(data.get("recommendation") or payload.get("recommendation"))
    return {
        "changed_content": changed_content,
        "player_impact": player_impact,
        "uncertainties": uncertainties,
        "recommendation": recommendation,
    }

def normalize_update_sections(
    value: Any,
    *,
    section_limit: int = UPDATE_SECTION_LIMIT,
    item_limit: int = UPDATE_ITEM_LIMIT,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    sections: list[dict[str, Any]] = []
    for section in value:
        if isinstance(section, dict):
            title = clean_pagination_text(section.get("title"))
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
            text = clean_pagination_text(item.get("text") or item.get("title"))
            children = normalize_update_items(item.get("children"), limit=limit, depth=depth + 1)
            source_ids = normalize_list(item.get("source_ids"), limit=1000)
        else:
            text = clean_pagination_text(item)
            children = []
            source_ids = []
        if text:
            normalized_item = {"text": text, "children": children}
            if source_ids:
                normalized_item["source_ids"] = source_ids
            result.append(normalized_item)
        if len(result) >= limit:
            break
    return result

def normalize_coverage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    expected = normalize_int(value.get("expected"))
    covered = normalize_int(value.get("covered"))
    missing = normalize_int(value.get("missing"))
    source_ids = normalize_list(value.get("missing_source_ids"), limit=1000)
    result: dict[str, Any] = {
        "expected": expected,
        "covered": covered,
        "missing": missing,
        "missing_source_ids": source_ids,
    }
    return result

def normalize_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

def normalize_tool_calls(value: Any, *, limit: int = 8) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        path = str(item.get("path") or "").strip().replace("\\", "/")
        query = str(item.get("query") or "").strip()
        reason = clean_pagination_text(item.get("reason"))
        if not tool:
            continue
        result.append(
            {
                "tool": tool,
                "path": path,
                "query": query,
                "reason": reason,
            }
        )
        if len(result) >= limit:
            break
    return result

def normalize_context_requests(value: Any, *, limit: int = 8, file_limit: int = 6) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source_file = normalize_context_path(item.get("source_file") or item.get("file") or item.get("path"))
        raw_missing = item.get("missing_files") or item.get("files") or item.get("missing_file")
        missing_files = normalize_context_paths(raw_missing, limit=file_limit)
        reason = clean_pagination_text(item.get("reason"))
        priority = str(item.get("priority") or "中").strip()
        if priority not in {"高", "中", "低"}:
            priority = "中"
        if not source_file or not missing_files:
            continue
        result.append(
            {
                "source_file": source_file,
                "missing_files": missing_files,
                "reason": reason,
                "priority": priority,
            }
        )
        if len(result) >= limit:
            break
    return result

def normalize_context_paths(value: Any, *, limit: int = 6) -> list[str]:
    if isinstance(value, list):
        items = value
    elif value:
        items = [value]
    else:
        items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        path = normalize_context_path(item)
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
        if len(result) >= limit:
            break
    return result

def normalize_context_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    path = re.sub(r"\s+", " ", path).strip()
    if not path or path.startswith("/") or ".." in path.split("/"):
        return ""
    path = path.lstrip("./")
    return path

def clean_pagination_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    replacements = [
        (r"本批\s*diff", "本次 diff"),
        (r"当前\s*diff\s*分片", "本次 diff"),
        (r"当前\s*分片", "本次 diff"),
        (r"本\s*分片", "本次 diff"),
        (r"该\s*分片", "本次 diff"),
        (r"此\s*分片", "本次 diff"),
        (r"本批次?", "本次 diff"),
        (r"当前批次", "本次 diff"),
        (r"第\s*\d+\s*/\s*\d+\s*批", "本次 diff"),
        (r"第\s*\d+\s*批", "本次 diff"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    text = re.sub(r"本次 diff(?=[\u4e00-\u9fff])", "本次 diff ", text)
    return re.sub(r"\s+", " ", text).strip()

def first_non_empty_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""
