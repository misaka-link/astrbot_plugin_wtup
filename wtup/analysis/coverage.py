from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..diff_collector import DiffChunk, DiffSummary
from .normalize import normalize_analysis, normalize_list


@dataclass(frozen=True)
class ChangeEntry:
    source_id: str
    file_path: str
    status: str
    description: str


def build_change_manifest(summary: DiffSummary, chunk: DiffChunk) -> list[ChangeEntry]:
    entries_by_path = _summary_entries_by_path(summary)
    entries: list[ChangeEntry] = []
    seen: set[str] = set()
    for file_info in chunk.files:
        path = _file_path(file_info)
        file_entries = entries_by_path.get(path) or _file_entries(file_info, _fallback_file_index(summary, file_info))
        for entry in file_entries:
            if entry.source_id in seen:
                continue
            seen.add(entry.source_id)
            entries.append(entry)
    return entries


def render_change_manifest(summary: DiffSummary, chunk: DiffChunk) -> str:
    entries = build_change_manifest(summary, chunk)
    if not entries:
        return "本分片没有可编号的文本 diff 变更点。"
    lines = []
    for entry in entries:
        lines.append(
            f"- {entry.source_id} | {entry.file_path} | {entry.status} | {entry.description}"
        )
    return "\n".join(lines)


def enforce_change_coverage(
    summary: DiffSummary,
    chunks: list[DiffChunk],
    analysis: dict[str, Any],
    *,
    precovered_source_ids: set[str] | None = None,
) -> dict[str, Any]:
    expected = _expected_entries(summary, chunks)
    if not expected:
        return normalize_analysis(analysis)

    normalized = normalize_analysis(analysis)
    covered = collect_source_ids(normalized)
    precovered = precovered_source_ids or set()
    missing = [entry for entry in expected if entry.source_id not in covered and entry.source_id not in precovered]
    if not missing:
        updated = dict(normalized)
        updated["coverage"] = {
            "expected": len(expected),
            "covered": len(expected),
            "missing": 0,
            "missing_source_ids": [],
        }
        return normalize_analysis(updated)

    sections = list(normalized.get("update_sections") or [])
    for index, batch in enumerate(_batches(missing, 500), start=1):
        title = "需复核的未覆盖变更" if index == 1 else f"需复核的未覆盖变更 {index}"
        sections.append(
            {
                "title": title,
                "items": [
                    {
                        "text": f"{entry.file_path}: {entry.description}",
                        "children": [],
                        "source_ids": [entry.source_id],
                    }
                    for entry in batch
                ],
            }
        )

    ai_analysis = normalized.get("ai_analysis") if isinstance(normalized.get("ai_analysis"), dict) else {}
    updated_ai = dict(ai_analysis)
    uncertainties = list(updated_ai.get("uncertainties") or [])
    missing_ids = ", ".join(entry.source_id for entry in missing[:20])
    suffix = " 等" if len(missing) > 20 else ""
    message = f"模型未主动覆盖 {len(missing)} 个原始变更点，插件已按点名册补入需复核条目：{missing_ids}{suffix}。"
    if message not in uncertainties:
        uncertainties.append(message)
    updated_ai["uncertainties"] = uncertainties

    updated = dict(normalized)
    updated["update_sections"] = sections
    updated["ai_analysis"] = updated_ai
    updated["risks"] = uncertainties
    updated["coverage"] = {
        "expected": len(expected),
        "covered": len(expected) - len(missing),
        "missing": len(missing),
        "missing_source_ids": [entry.source_id for entry in missing],
    }
    return normalize_analysis(updated)


def collect_source_ids(analysis: dict[str, Any]) -> set[str]:
    sections = analysis.get("update_sections") if isinstance(analysis.get("update_sections"), list) else []
    result: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            continue
        result.update(_collect_item_source_ids(section.get("items")))
    return result


def _collect_item_source_ids(items: Any) -> set[str]:
    result: set[str] = set()
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        result.update(normalize_list(item.get("source_ids"), limit=1000))
        result.update(_collect_item_source_ids(item.get("children")))
    return result


def _expected_entries(summary: DiffSummary, chunks: list[DiffChunk]) -> list[ChangeEntry]:
    entries: list[ChangeEntry] = []
    seen: set[str] = set()
    for chunk in chunks:
        for entry in build_change_manifest(summary, chunk):
            if entry.source_id in seen:
                continue
            seen.add(entry.source_id)
            entries.append(entry)
    return entries


def _summary_entries_by_path(summary: DiffSummary) -> dict[str, list[ChangeEntry]]:
    return {
        _file_path(file_info): _file_entries(file_info, file_index)
        for file_index, file_info in enumerate(summary.files, start=1)
    }


def _file_entries(file_info: dict[str, Any], file_index: int) -> list[ChangeEntry]:
    path = _file_path(file_info)
    status = str(file_info.get("status") or "modified").strip() or "modified"
    patch = str(file_info.get("patch") or "").strip()
    if not patch:
        return [
            ChangeEntry(
                source_id=f"C{file_index:03d}-001",
                file_path=path,
                status=status,
                description="GitHub 未返回文本 patch，需按文件级变更复核。",
            )
        ]

    entries: list[ChangeEntry] = []
    hunk_header = ""
    current: list[str] = []
    group_index = 1

    def flush() -> None:
        nonlocal current, group_index
        if not current:
            return
        description = _describe_change_group(hunk_header, current)
        entries.append(
            ChangeEntry(
                source_id=f"C{file_index:03d}-{group_index:03d}",
                file_path=path,
                status=status,
                description=description,
            )
        )
        group_index += 1
        current = []

    for line in patch.splitlines():
        if line.startswith("@@"):
            flush()
            hunk_header = line.strip()
            continue
        if line.startswith(("+++", "---", "diff --git ", "index ", "new file mode", "deleted file mode")):
            continue
        if line.startswith(("+", "-")):
            current.append(line)
            continue
        flush()
    flush()

    if entries:
        return entries
    return [
        ChangeEntry(
            source_id=f"C{file_index:03d}-001",
            file_path=path,
            status=status,
            description="文件 metadata 或重命名信息发生变化，文本内容无可拆分改动。",
        )
    ]


def _describe_change_group(hunk_header: str, lines: list[str]) -> str:
    removed = [_clean_patch_line(line[1:]) for line in lines if line.startswith("-")]
    added = [_clean_patch_line(line[1:]) for line in lines if line.startswith("+")]
    parts = []
    if hunk_header:
        parts.append(hunk_header)
    if removed:
        parts.append("删除/旧值: " + _join_snippets(removed))
    if added:
        parts.append("新增/新值: " + _join_snippets(added))
    return "；".join(parts)[:700]


def _join_snippets(lines: list[str]) -> str:
    compact = [line for line in lines if line]
    text = " | ".join(compact[:4])
    if len(compact) > 4:
        text += " | ..."
    return text[:360]


def _clean_patch_line(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:180]


def _file_path(file_info: dict[str, Any]) -> str:
    return str(file_info.get("filename") or "unknown").strip().replace("\\", "/")


def _fallback_file_index(summary: DiffSummary, file_info: dict[str, Any]) -> int:
    path = _file_path(file_info)
    for index, candidate in enumerate(summary.files, start=1):
        if _file_path(candidate) == path:
            return index
    return 999


def _batches(values: list[ChangeEntry], size: int) -> list[list[ChangeEntry]]:
    return [values[index : index + size] for index in range(0, len(values), size)]
