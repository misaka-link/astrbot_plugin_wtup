from __future__ import annotations

import json
import re
from typing import Any

from .fallback import fallback_analysis


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
        "analysis_coverage": normalize_analysis_coverage(
            payload.get("analysis_coverage") or payload.get("coverage")
        ),
        "tool_calls": normalize_tool_calls(payload.get("tool_calls")),
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
    changed_content = normalize_list(data.get("changed_content") or payload.get("highlights"))
    player_impact = normalize_list(data.get("player_impact") or payload.get("player_impact"))
    uncertainties = normalize_list(data.get("uncertainties") or payload.get("risks"))
    recommendation = clean_pagination_text(data.get("recommendation") or payload.get("recommendation"))
    return {
        "changed_content": changed_content,
        "player_impact": player_impact,
        "uncertainties": uncertainties,
        "recommendation": recommendation,
    }

def normalize_analysis_coverage(value: Any, *, limit: int = 1000) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        items = value.values()
    elif isinstance(value, list):
        items = value
    else:
        return []

    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = normalize_coverage_path(item.get("path") or item.get("filename") or item.get("file"))
        if not path:
            continue
        status = normalize_coverage_status(item.get("status"))
        covered_changes = normalize_list(
            item.get("covered_changes") or item.get("changes") or item.get("analyzed_changes"),
            limit=20,
        )
        evidence = normalize_list(
            item.get("evidence") or item.get("markers") or item.get("references"),
            limit=20,
        )
        notes = clean_pagination_text(item.get("notes") or item.get("note") or item.get("reason"))
        result.append(
            {
                "path": path,
                "status": status,
                "covered_changes": covered_changes,
                "evidence": evidence,
                "notes": notes,
            }
        )
        if len(result) >= limit:
            break
    return result

def normalize_coverage_path(value: Any) -> str:
    return re.sub(r"/+", "/", str(value or "").strip().replace("\\", "/"))

def normalize_coverage_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "已分析": "analyzed",
        "完整分析": "analyzed",
        "已覆盖": "analyzed",
        "covered": "analyzed",
        "done": "analyzed",
        "待复核": "uncertain",
        "不确定": "uncertain",
        "需复核": "uncertain",
        "partial": "uncertain",
        "partially_analyzed": "uncertain",
        "skipped": "skipped",
        "跳过": "skipped",
        "未分析": "skipped",
    }
    if text in {"analyzed", "uncertain", "skipped"}:
        return text
    return mapping.get(text, "uncertain")

def normalize_update_sections(value: Any, *, section_limit: int = 8, item_limit: int = 20) -> list[dict[str, Any]]:
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
        else:
            text = clean_pagination_text(item)
            children = []
        if text:
            result.append({"text": text, "children": children})
        if len(result) >= limit:
            break
    return result

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
