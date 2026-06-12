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
