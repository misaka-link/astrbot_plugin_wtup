from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME, PluginConfig
from ..diff_collector import DiffChunk, DiffSummary, group_related_files
from .prompts import build_prompt


def estimate_input_tokens(text: str) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    ascii_chars = sum(1 for char in raw if ord(char) < 128)
    non_ascii_chars = len(raw) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + non_ascii_chars)

def estimate_chunk_input_tokens(settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> int:
    return estimate_input_tokens(build_prompt(settings, summary, chunk))

def split_chunks_by_token_limit(settings: PluginConfig, summary: DiffSummary) -> DiffSummary:
    limit = int(getattr(settings, "max_input_token_limit", 0) or 0)
    if limit <= 0:
        return summary

    split_groups: list[list[dict[str, Any]]] = []
    for chunk in summary.chunks:
        split_groups.extend(_split_file_group_by_token_limit(settings, summary, chunk.files, limit))

    if len(split_groups) == len(summary.chunks) and all(
        group == chunk.files for group, chunk in zip(split_groups, summary.chunks)
    ):
        return summary

    total = len(split_groups) or 1
    chunks = [
        DiffChunk(index=index + 1, total=total, files=files, patch_chars=sum(file_patch_chars(item) for item in files))
        for index, files in enumerate(split_groups or [[]])
    ]
    return DiffSummary(
        base_sha=summary.base_sha,
        head_sha=summary.head_sha,
        compare_url=summary.compare_url,
        total_commits=summary.total_commits,
        total_files=summary.total_files,
        additions=summary.additions,
        deletions=summary.deletions,
        changed_files=summary.changed_files,
        commits=summary.commits,
        files=summary.files,
        chunks=chunks,
    )

def _split_file_group_by_token_limit(
    settings: PluginConfig,
    summary: DiffSummary,
    files: list[dict[str, Any]],
    limit: int,
) -> list[list[dict[str, Any]]]:
    if len(files) <= 1:
        if files:
            single = DiffChunk(index=1, total=1, files=files, patch_chars=sum(file_patch_chars(item) for item in files))
            tokens = estimate_chunk_input_tokens(settings, summary, single)
            if tokens > limit:
                logger.warning(
                    "[%s] 单文件模型输入约 %d token，超过限制 %d token；为保证文件完整性不拆分: %s",
                    PLUGIN_NAME,
                    tokens,
                    limit,
                    files[0].get("filename") or "",
                )
        return [files]

    probe = DiffChunk(index=1, total=1, files=files, patch_chars=sum(file_patch_chars(item) for item in files))
    if estimate_chunk_input_tokens(settings, summary, probe) <= limit:
        return [files]

    related_groups = group_related_files(files)
    if len(related_groups) > 1:
        midpoint = _best_related_group_split_index(related_groups)
        return [
            *_split_file_group_by_token_limit(settings, summary, _flatten_related_groups(related_groups[:midpoint]), limit),
            *_split_file_group_by_token_limit(settings, summary, _flatten_related_groups(related_groups[midpoint:]), limit),
        ]

    midpoint = (len(files) + 1) // 2
    return [
        *_split_file_group_by_token_limit(settings, summary, files[:midpoint], limit),
        *_split_file_group_by_token_limit(settings, summary, files[midpoint:], limit),
    ]

def file_patch_chars(file_info: dict[str, Any]) -> int:
    return len(str(file_info.get("patch") or "")) + len(str(file_info.get("filename") or ""))

def _best_related_group_split_index(groups: list[list[dict[str, Any]]]) -> int:
    max_index = len(groups) - 1
    best_index = 1
    best_distance = float("inf")
    target_chars = sum(_file_group_patch_chars(group) for group in groups) / 2
    running_chars = 0

    for index in range(1, max_index + 1):
        running_chars += _file_group_patch_chars(groups[index - 1])
        distance = abs(running_chars - target_chars)
        if distance <= best_distance:
            best_index = index
            best_distance = distance
    return best_index

def _file_group_patch_chars(group: list[dict[str, Any]]) -> int:
    return sum(file_patch_chars(file_info) for file_info in group)

def _flatten_related_groups(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [file_info for group in groups for file_info in group]
