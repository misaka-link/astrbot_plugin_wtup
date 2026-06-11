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
  "summary": "一句话总结本分片最重要的变化",
  "importance": "低/中/高",
  "highlights": ["重点变化 1", "重点变化 2"],
  "player_impact": ["对玩家、载具、经济、任务、地图或战斗体验的可能影响"],
  "risks": ["不确定点、需要继续观察的地方"],
  "recommendation": "是否建议玩家关注/更新，以及原因",
  "tags": ["标签1", "标签2"]
}}

要求：
1. 用中文。
2. 优先解释数据变化可能代表什么，不要只复述文件名。
3. 如果信息不足，要明确写“不确定”，不要编造。
4. 每个数组最多 5 条，每条尽量短。
5. 当前是第 {chunk.index}/{chunk.total} 个分片；如果是分片报告，请在 summary 中体现。

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
        "importance": "中",
        "highlights": [text[:1200]] if text else ["模型返回为空。"],
        "player_impact": [],
        "risks": ["模型输出格式未按 JSON 返回。"],
        "recommendation": "请结合 GitHub 原始 diff 复核。",
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
    return {
        "summary": str(payload.get("summary") or "").strip() or "本次更新未给出摘要。",
        "importance": normalize_importance(payload.get("importance")),
        "highlights": normalize_list(payload.get("highlights")),
        "player_impact": normalize_list(payload.get("player_impact")),
        "risks": normalize_list(payload.get("risks")),
        "recommendation": str(payload.get("recommendation") or "").strip(),
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


def first_non_empty_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""
