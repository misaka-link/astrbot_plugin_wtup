from __future__ import annotations

import asyncio
import json
import re
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from .config import PLUGIN_NAME, PluginConfig
from .diff_collector import DiffChunk, DiffSummary, render_chunk_input


def build_prompt(settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> str:
    return f"""
{settings.analysis_prompt}

你正在分析固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新。

请只输出 JSON，不要使用 Markdown 代码块。JSON 字段如下：
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结本分片最重要的变化",
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
10. summary 会显示在标题下面的小字行，可以写描述性标题或本分片摘要；不要把描述性标题写进 report_title。
11. 当前是第 {chunk.index}/{chunk.total} 个分片；分片信息会由程序显示在标题下方，不要写进 report_title。

以下是 GitHub 变更数据：

{render_chunk_input(summary, chunk)}
""".strip()


async def analyze_chunk(context: Any, settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> dict[str, Any]:
    prompt = build_prompt(settings, summary, chunk)
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

    response = await asyncio.wait_for(context.llm_generate(**llm_kwargs), timeout=settings.timeout_seconds)
    text = extract_response_text(response)
    parsed = parse_analysis_json(text)
    if parsed is not None:
        return parsed
    return {
        "summary": first_non_empty_line(text) or "模型返回了非 JSON 分析结果。",
        "report_title": "",
        "importance": "中",
        "update_sections": [
            {
                "title": "模型原始输出",
                "items": [{"text": text[:1200] if text else "模型返回为空。", "children": []}],
            }
        ],
        "highlights": [text[:1200]] if text else ["模型返回为空。"],
        "player_impact": [],
        "risks": ["模型输出格式未按 JSON 返回。"],
        "recommendation": "请结合 GitHub 原始 diff 复核。",
        "ai_analysis": {
            "changed_content": [text[:1200]] if text else ["模型返回为空。"],
            "player_impact": [],
            "uncertainties": ["模型输出格式未按 JSON 返回。"],
            "recommendation": "请结合 GitHub 原始 diff 复核。",
        },
        "tags": ["格式异常"],
        "raw_text": text,
    }


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
