from __future__ import annotations

from typing import Any


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
