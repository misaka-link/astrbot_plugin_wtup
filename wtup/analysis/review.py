from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME, PluginConfig, REVIEW_MODE_AUTO, REVIEW_MODE_ENERGY, REVIEW_MODE_QUALITY
from ..diff_collector import DiffSummary
from .client import request_llm
from .coverage import collect_source_ids, enforce_change_coverage
from .models import TokenUsage
from .normalize import normalize_analysis
from .responses import ensure_usable_llm_response, extract_response_text, extract_token_usage


FUZZY_TERMS = (
    "部分载具",
    "某些载具",
    "若干载具",
    "部分装备",
    "某些装备",
    "若干装备",
    "部分武器",
    "某些武器",
    "若干武器",
)


@dataclass(frozen=True)
class ReviewResult:
    analysis: dict[str, Any]
    token_usage: TokenUsage
    mode_used: str
    issues: list[str]
    applied_revision: bool = False


async def review_analysis_with_usage(
    context: Any,
    settings: PluginConfig,
    summary: DiffSummary,
    analysis: dict[str, Any],
) -> ReviewResult:
    normalized = enforce_change_coverage(summary, summary.chunks, analysis)
    if not getattr(settings, "enable_review_model", False):
        return ReviewResult(normalized, TokenUsage(), "off", [])

    mode = _select_review_mode(settings, normalized)
    if mode == "off":
        return ReviewResult(normalized, TokenUsage(), "off", [])

    issues = _programmatic_review_issues(normalized)
    rounds = max(1, int(getattr(settings, "review_rounds", 1) or 1))
    token_usage = TokenUsage()
    current = normalized
    applied_revision = False

    for round_index in range(1, rounds + 1):
        prompt = build_review_prompt(settings, summary, current, mode=mode, round_index=round_index, issues=issues)
        provider_id = _review_provider_id(settings, mode)
        try:
            response = await request_llm(
                context,
                settings,
                prompt,
                provider_id=provider_id,
                summary=summary,
                purpose=f"监督模型复核-{_mode_label(mode)}",
            )
            token_usage += extract_token_usage(response)
            ensure_usable_llm_response(response)
            payload = _parse_review_json(extract_response_text(response))
            if payload is None:
                raise ValueError("监督模型输出不是有效 JSON")
        except Exception as exc:
            logger.warning("[%s] 监督模型复核失败，保留当前报告: %s", PLUGIN_NAME, exc)
            issues = [*issues, f"监督模型复核失败：{exc}"]
            break

        review_issues = _review_payload_issues(payload)
        issues = _unique([*issues, *review_issues])
        if mode == REVIEW_MODE_QUALITY:
            revised = payload.get("revised_analysis") if isinstance(payload.get("revised_analysis"), dict) else None
            if revised is not None:
                candidate = enforce_change_coverage(summary, summary.chunks, revised)
                if _revision_keeps_required_ids(current, candidate):
                    current = candidate
                    applied_revision = True
                else:
                    issues = _unique([*issues, "监督模型修正版丢失 source_ids，已拒绝采用。"])

        if not review_issues or mode == REVIEW_MODE_ENERGY:
            break

    current = _attach_review_metadata(current, mode=mode, issues=issues, applied_revision=applied_revision)
    return ReviewResult(current, token_usage, mode, issues, applied_revision=applied_revision)


def build_review_prompt(
    settings: PluginConfig,
    summary: DiffSummary,
    analysis: dict[str, Any],
    *,
    mode: str,
    round_index: int,
    issues: list[str],
) -> str:
    normalized = normalize_analysis(analysis)
    batch_size = (
        settings.review_quality_batch_size
        if mode == REVIEW_MODE_QUALITY
        else settings.review_energy_batch_size
    )
    payload = _review_payload(summary, normalized, mode=mode, issues=issues, limit=batch_size)
    mode_text = "质量档" if mode == REVIEW_MODE_QUALITY else "节能档"
    revision_text = (
        "质量档可以在 revised_analysis 中返回修正版；必须完整保留原有 source_ids，不能删减或编造编号。"
        if mode == REVIEW_MODE_QUALITY
        else "节能档不要返回 revised_analysis，只返回是否通过和问题列表。"
    )
    return f"""
{settings.review_prompt}

你正在执行监督模型复核，模式：{mode_text}，轮次：{round_index}。
本次复核最多关注 {batch_size} 个待审条目；程序已经做过 source_ids 硬覆盖检查。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. JSON 必须能被 json.loads 解析。
4. 不要重新分析原始 diff，不要创造新事实。
5. {revision_text}

JSON 字段：
{{
  "passed": true,
  "issues": ["发现的问题，没有问题则为空数组"],
  "severity": "低/中/高",
  "revised_analysis": {{}}
}}

检查重点：
- expected_source_ids 中的编号都应出现在 covered_source_ids 中；如果缺失，必须指出。
- update_sections 中已有 source_ids 不得丢失。
- 检查是否出现“部分载具”“某些装备”“若干武器”等模糊表述。
- 每条报告条目应有清晰含义；质量档还要检查分类、参数解释、影响说明是否明显空泛。
- 水面舰艇相关分类应位于所有分类最后。

复核输入：
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()


def _select_review_mode(settings: PluginConfig, analysis: dict[str, Any]) -> str:
    configured = str(getattr(settings, "review_mode", REVIEW_MODE_AUTO) or REVIEW_MODE_AUTO).strip().lower()
    if configured in {"off", "关闭"}:
        return "off"
    if configured == REVIEW_MODE_QUALITY:
        return REVIEW_MODE_QUALITY
    if configured == REVIEW_MODE_ENERGY:
        return REVIEW_MODE_ENERGY
    return REVIEW_MODE_QUALITY if _should_upgrade_to_quality(settings, analysis) else REVIEW_MODE_ENERGY


def _should_upgrade_to_quality(settings: PluginConfig, analysis: dict[str, Any]) -> bool:
    coverage = analysis.get("coverage") if isinstance(analysis.get("coverage"), dict) else {}
    missing = _as_int(coverage.get("missing"))
    threshold = max(0, int(getattr(settings, "review_upgrade_missing_id_threshold", 10) or 0))
    if getattr(settings, "review_upgrade_on_missing_ids", True) and missing > threshold:
        return True
    if getattr(settings, "review_upgrade_on_context_failure", True):
        text = json.dumps(analysis, ensure_ascii=False)
        if any(needle in text for needle in ("动态补充", "补充失败", "无法读取", "需复核的未覆盖变更")):
            return True
    return False


def _review_provider_id(settings: PluginConfig, mode: str) -> str:
    if mode == REVIEW_MODE_QUALITY:
        return settings.effective_review_quality_provider_id
    return settings.effective_review_provider_id


def _programmatic_review_issues(analysis: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    coverage = analysis.get("coverage") if isinstance(analysis.get("coverage"), dict) else {}
    missing = _as_int(coverage.get("missing"))
    if missing:
        issues.append(f"仍有 {missing} 个变更编号由程序补入需复核条目。")
    fuzzy_hits = _find_fuzzy_terms(analysis)
    if fuzzy_hits:
        issues.append("存在模糊表述：" + "、".join(fuzzy_hits[:8]))
    if _naval_section_not_last(analysis):
        issues.append("水面舰艇相关分类未位于所有分类最后。")
    return issues


def _review_payload(
    summary: DiffSummary,
    analysis: dict[str, Any],
    *,
    mode: str,
    issues: list[str],
    limit: int,
) -> dict[str, Any]:
    expected = sorted(_expected_source_ids(analysis))
    covered = sorted(collect_source_ids(analysis))
    items = _flatten_items(analysis.get("update_sections"), limit=max(1, limit))
    return {
        "commit_range": f"{summary.base_sha[:7] or 'unknown'}...{summary.head_sha[:7] or 'unknown'}",
        "mode": mode,
        "coverage": analysis.get("coverage") if isinstance(analysis.get("coverage"), dict) else {},
        "expected_source_ids": expected,
        "covered_source_ids": covered,
        "known_issues": issues,
        "sections": [
            {
                "title": str(section.get("title") or ""),
                "item_count": len(section.get("items") if isinstance(section.get("items"), list) else []),
            }
            for section in analysis.get("update_sections", [])
            if isinstance(section, dict)
        ],
        "items": items,
        "ai_analysis": analysis.get("ai_analysis") if isinstance(analysis.get("ai_analysis"), dict) else {},
    }


def _expected_source_ids(analysis: dict[str, Any]) -> set[str]:
    coverage = analysis.get("coverage") if isinstance(analysis.get("coverage"), dict) else {}
    missing = set(str(item) for item in coverage.get("missing_source_ids", []) if str(item or "").strip())
    return collect_source_ids(analysis) | missing


def _flatten_items(sections: Any, *, limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(sections, list):
        return result
    for section in sections:
        if not isinstance(section, dict):
            continue
        _append_items(result, section.get("items"), section_title=str(section.get("title") or ""), limit=limit)
        if len(result) >= limit:
            break
    return result


def _append_items(result: list[dict[str, Any]], items: Any, *, section_title: str, limit: int) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "section": section_title,
                "text": str(item.get("text") or ""),
                "source_ids": [str(value) for value in item.get("source_ids", []) if str(value or "").strip()],
            }
        )
        if len(result) >= limit:
            return
        _append_items(result, item.get("children"), section_title=section_title, limit=limit)
        if len(result) >= limit:
            return


def _review_payload_issues(payload: dict[str, Any]) -> list[str]:
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return []
    return _unique(str(item).strip() for item in issues if str(item or "").strip())


def _parse_review_json(text: str) -> dict[str, Any] | None:
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
    return payload if isinstance(payload, dict) else None


def _revision_keeps_required_ids(previous: dict[str, Any], candidate: dict[str, Any]) -> bool:
    previous_ids = collect_source_ids(previous)
    candidate_ids = collect_source_ids(candidate)
    return previous_ids.issubset(candidate_ids)


def _attach_review_metadata(
    analysis: dict[str, Any],
    *,
    mode: str,
    issues: list[str],
    applied_revision: bool,
) -> dict[str, Any]:
    updated = normalize_analysis(analysis)
    updated["review"] = {
        "mode": mode,
        "issues": issues,
        "applied_revision": applied_revision,
    }
    if issues:
        ai_analysis = dict(updated.get("ai_analysis") or {})
        uncertainties = list(ai_analysis.get("uncertainties") or [])
        message = "监督模型复核提示：" + "；".join(issues[:5])
        if message not in uncertainties:
            uncertainties.append(message)
        ai_analysis["uncertainties"] = uncertainties
        updated["ai_analysis"] = ai_analysis
        updated["risks"] = uncertainties
    return updated


def _find_fuzzy_terms(analysis: dict[str, Any]) -> list[str]:
    text = json.dumps(analysis.get("update_sections", []), ensure_ascii=False)
    return _unique(term for term in FUZZY_TERMS if term in text)


def _naval_section_not_last(analysis: dict[str, Any]) -> bool:
    sections = analysis.get("update_sections") if isinstance(analysis.get("update_sections"), list) else []
    titles = [str(section.get("title") or "") for section in sections if isinstance(section, dict)]
    naval_indexes = [index for index, title in enumerate(titles) if re.search(r"水面|舰艇|海军|舰船", title)]
    return bool(naval_indexes) and max(naval_indexes) != len(titles) - 1


def _mode_label(mode: str) -> str:
    return "质量档" if mode == REVIEW_MODE_QUALITY else "节能档"


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _unique(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
