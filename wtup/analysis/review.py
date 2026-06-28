from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
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

HIGH_RISK_TERMS = (
    "需复核",
    "待复核",
    "不确定",
    "无法判断",
    "部分",
    "某些",
    "若干",
    "武器",
    "弹药",
    "导弹",
    "雷达",
    "告警",
    "经济",
    "科技树",
    "研发",
    "战斗部",
    "穿深",
    "破片",
    "引信",
    "装甲",
    "TNT",
    "HARM",
)


@dataclass(frozen=True)
class ReviewResult:
    analysis: dict[str, Any]
    token_usage: TokenUsage
    mode_used: str
    issues: list[str]
    applied_revision: bool = False
    revision_stats: dict[str, Any] = field(default_factory=dict)


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
    revision_stats = _empty_revision_stats()

    for round_index in range(1, rounds + 1):
        round_changed = False
        round_rejected = False
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
            revised_items = payload.get("item_revisions") if isinstance(payload.get("item_revisions"), list) else []
            if revised_items:
                current, item_stats = _apply_item_revisions(current, revised_items)
                revision_stats = _merge_revision_stats(revision_stats, item_stats)
                if item_stats["applied"]:
                    applied_revision = True
                    round_changed = True
                if item_stats["rejected"]:
                    round_rejected = True
                issues = _unique([*issues, *item_stats["rejected_reasons"]])

            revised = payload.get("revised_analysis") if isinstance(payload.get("revised_analysis"), dict) else None
            if revised is not None and not revised_items:
                candidate = enforce_change_coverage(summary, summary.chunks, revised)
                if _revision_keeps_required_ids(current, candidate):
                    current = candidate
                    applied_revision = True
                    round_changed = True
                    revision_stats["legacy_revision_applied"] = True
                else:
                    issues = _unique([*issues, "监督模型修正版丢失 source_ids，已拒绝采用。"])
                    revision_stats["legacy_revision_rejected"] = True
                    round_rejected = True

        if mode == REVIEW_MODE_ENERGY:
            break
        if not review_issues and not round_changed and not round_rejected:
            break

    current = _attach_review_metadata(
        current,
        mode=mode,
        issues=issues,
        applied_revision=applied_revision,
        revision_stats=revision_stats,
    )
    return ReviewResult(current, token_usage, mode, issues, applied_revision=applied_revision, revision_stats=revision_stats)


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
        "质量档不要返回整篇 revised_analysis；如需修正文案，只在 item_revisions 中按 item_id 返回 new_text。程序只会替换原条目的 text，source_ids、children、分类和顺序一律由程序保留。"
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
6. 不确定如何修正时，只写入 issues，不要硬改。

JSON 字段：
{{
  "passed": true,
  "issues": ["发现的问题，没有问题则为空数组"],
  "severity": "低/中/高",
  "item_revisions": [
    {{"item_id": "I001", "new_text": "仅替换该条目的新文本", "reason": "修正原因"}}
  ]
}}

检查重点：
- expected_source_ids 中的编号都应出现在 covered_source_ids 中；如果缺失，必须指出。
- update_sections 中已有 source_ids 不得丢失。
- 检查是否出现“部分载具”“某些装备”“若干武器”等模糊表述。
- 每条报告条目应有清晰含义；质量档还要检查分类、参数解释、影响说明是否明显空泛。
- 水面舰艇相关分类应位于所有分类最后。
- 质量档只能修正复核输入 items 中已有 item_id 对应的条目，不要新增、删除、合并或重排条目。

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
        if any(needle in text for needle in ("动态补充", "补充失败", "无法读取", "需复核的未覆盖变更", "需验证内容", "needs_verification")):
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
    all_items: list[dict[str, Any]] = []
    if not isinstance(sections, list):
        return result
    counter = 0
    for section in sections:
        if not isinstance(section, dict):
            continue
        counter = _append_items(
            all_items,
            section.get("items"),
            section_title=str(section.get("title") or ""),
            counter=counter,
        )
    prioritized = sorted(all_items, key=lambda item: (-int(item.get("risk_score") or 0), int(item.get("order") or 0)))
    for item in prioritized[:limit]:
        result.append({key: value for key, value in item.items() if key not in {"order", "risk_score"}})
    return result


def _append_items(
    result: list[dict[str, Any]],
    items: Any,
    *,
    section_title: str,
    counter: int,
) -> int:
    if not isinstance(items, list):
        return counter
    for item in items:
        if not isinstance(item, dict):
            continue
        counter += 1
        item_id = f"I{counter:03d}"
        text = str(item.get("text") or "")
        source_ids = [str(value) for value in item.get("source_ids", []) if str(value or "").strip()]
        risk_score, risk_reasons = _item_risk(section_title, text, source_ids)
        result.append(
            {
                "item_id": item_id,
                "section": section_title,
                "text": text,
                "source_ids": source_ids,
                "risk_reasons": risk_reasons,
                "order": counter,
                "risk_score": risk_score,
            }
        )
        counter = _append_items(
            result,
            item.get("children"),
            section_title=section_title,
            counter=counter,
        )
    return counter


def _item_risk(section_title: str, text: str, source_ids: list[str]) -> tuple[int, list[str]]:
    haystack = f"{section_title}\n{text}"
    score = 0
    reasons: list[str] = []
    fuzzy_hits = [term for term in FUZZY_TERMS if term in haystack]
    if fuzzy_hits:
        score += 100
        reasons.append("模糊表述：" + "、".join(fuzzy_hits[:3]))
    uncertainty_hits = [term for term in ("需复核", "待复核", "不确定", "无法判断") if term in haystack]
    if uncertainty_hits:
        score += 90
        reasons.append("含不确定/复核提示")
    high_risk_hits = [term for term in HIGH_RISK_TERMS if term in haystack]
    if high_risk_hits:
        score += 35
        reasons.append("高影响关键词：" + "、".join(_unique(high_risk_hits)[:4]))
    if len(source_ids) >= 5 and len(text.strip()) < 120:
        score += 60
        reasons.append("短文本覆盖多个 source_ids")
    if not source_ids:
        score += 15
        reasons.append("缺少 source_ids")
    return score, reasons


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


def _apply_item_revisions(analysis: dict[str, Any], revisions: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = normalize_analysis(analysis)
    id_map = _item_id_map(updated)
    stats = _empty_revision_stats()
    stats["attempted"] = len([item for item in revisions if isinstance(item, dict)])
    rejected_reasons: list[str] = []

    for revision in revisions:
        if not isinstance(revision, dict):
            rejected_reasons.append("局部修正不是 JSON object，已拒绝。")
            continue
        item_id = str(revision.get("item_id") or "").strip().upper()
        if not item_id:
            rejected_reasons.append("局部修正缺少 item_id，已拒绝。")
            continue
        target = id_map.get(item_id)
        if target is None:
            rejected_reasons.append(f"局部修正 {item_id} 不存在，已拒绝。")
            continue
        new_text = str(revision.get("new_text") or "").strip()
        if not new_text:
            rejected_reasons.append(f"局部修正 {item_id} 的 new_text 为空，已拒绝。")
            continue
        returned_ids = revision.get("source_ids")
        if isinstance(returned_ids, list):
            normalized_returned = [str(value).strip() for value in returned_ids if str(value or "").strip()]
            if normalized_returned and set(normalized_returned) != set(target["source_ids"]):
                rejected_reasons.append(f"局部修正 {item_id} 返回的 source_ids 与原条目不一致，已拒绝。")
                continue
        item = target["item"]
        old_text = str(item.get("text") or "").strip()
        if new_text == old_text:
            continue
        item["text"] = new_text
        stats["applied"] += 1

    stats["rejected_reasons"] = _unique(rejected_reasons)
    stats["rejected"] = len(stats["rejected_reasons"])
    return updated, stats


def _item_id_map(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sections = analysis.get("update_sections") if isinstance(analysis.get("update_sections"), list) else []
    result: dict[str, dict[str, Any]] = {}
    counter = 0
    for section in sections:
        if not isinstance(section, dict):
            continue
        counter = _index_items(result, section.get("items"), counter=counter)
    return result


def _index_items(result: dict[str, dict[str, Any]], items: Any, *, counter: int) -> int:
    if not isinstance(items, list):
        return counter
    for item in items:
        if not isinstance(item, dict):
            continue
        counter += 1
        item_id = f"I{counter:03d}"
        result[item_id] = {
            "item": item,
            "source_ids": [str(value) for value in item.get("source_ids", []) if str(value or "").strip()],
        }
        counter = _index_items(result, item.get("children"), counter=counter)
    return counter


def _empty_revision_stats() -> dict[str, Any]:
    return {
        "attempted": 0,
        "applied": 0,
        "rejected": 0,
        "rejected_reasons": [],
        "legacy_revision_applied": False,
        "legacy_revision_rejected": False,
    }


def _merge_revision_stats(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or _empty_revision_stats())
    merged["attempted"] = int(merged.get("attempted") or 0) + int(extra.get("attempted") or 0)
    merged["applied"] = int(merged.get("applied") or 0) + int(extra.get("applied") or 0)
    merged["rejected"] = int(merged.get("rejected") or 0) + int(extra.get("rejected") or 0)
    merged["rejected_reasons"] = _unique(
        [*list(merged.get("rejected_reasons") or []), *list(extra.get("rejected_reasons") or [])]
    )
    merged["legacy_revision_applied"] = bool(merged.get("legacy_revision_applied")) or bool(
        extra.get("legacy_revision_applied")
    )
    merged["legacy_revision_rejected"] = bool(merged.get("legacy_revision_rejected")) or bool(
        extra.get("legacy_revision_rejected")
    )
    return merged


def _attach_review_metadata(
    analysis: dict[str, Any],
    *,
    mode: str,
    issues: list[str],
    applied_revision: bool,
    revision_stats: dict[str, Any],
) -> dict[str, Any]:
    updated = normalize_analysis(analysis)
    updated["review"] = {
        "mode": mode,
        "issues": issues,
        "applied_revision": applied_revision,
        "revision_stats": revision_stats,
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
