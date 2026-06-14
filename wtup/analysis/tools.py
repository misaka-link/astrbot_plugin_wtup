from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME, REPO_FULL_NAME, PluginConfig
from ..diff_collector import DiffChunk, DiffSummary
from ..github_cache import GitHubCache
from ..github_client import GitHubClient, GitHubRequestError


SUPPORTED_TOOLS = {
    "read_changed_patch",
    "read_changed_file",
    "search_changed_files",
    "list_related_files",
}


async def execute_tool_calls(
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    tool_calls: list[dict[str, Any]],
    *,
    round_index: int,
    github_file_cache: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not tool_calls:
        return []

    max_calls = max(1, int(settings.max_tool_calls_per_round or 1))
    selected_calls = tool_calls[:max_calls]
    results = [
        await _execute_single_tool_call(
            settings,
            summary,
            chunk,
            call,
            round_index=round_index,
            github_file_cache=github_file_cache,
        )
        for call in selected_calls
    ]
    for call in tool_calls[max_calls:]:
        result = _tool_result(
            call,
            status="rejected",
            content=f"超过每轮工具调用上限 {max_calls}，该请求未执行。",
            round_index=round_index,
        )
        _record_tool_call(settings, result)
        results.append(result)
    return results


async def _execute_single_tool_call(
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    call: dict[str, Any],
    *,
    round_index: int,
    github_file_cache: dict[str, str] | None,
) -> dict[str, Any]:
    tool = str(call.get("tool") or "").strip()
    if tool not in SUPPORTED_TOOLS:
        result = _tool_result(
            call,
            status="rejected",
            content=f"不支持的工具: {tool or '空'}。",
            round_index=round_index,
        )
        _record_tool_call(settings, result)
        return result

    if tool == "read_changed_patch":
        result = _read_changed_patch(settings, summary, call, round_index=round_index)
    elif tool == "read_changed_file":
        result = await _read_changed_file(
            settings,
            summary,
            call,
            round_index=round_index,
            github_file_cache=github_file_cache,
        )
    elif tool == "search_changed_files":
        result = _search_changed_files(settings, summary, call, round_index=round_index)
    else:
        result = _list_related_files(settings, summary, chunk, call, round_index=round_index)

    _record_tool_call(settings, result)
    return result


def _read_changed_patch(
    settings: PluginConfig,
    summary: DiffSummary,
    call: dict[str, Any],
    *,
    round_index: int,
) -> dict[str, Any]:
    path = _clean_path(call.get("path"))
    if not path:
        return _tool_result(call, status="rejected", content="读取文件需要提供 path。", round_index=round_index)
    if not _is_safe_relative_path(path):
        return _tool_result(call, status="rejected", content="path 不是安全的相对路径。", round_index=round_index)

    file_info = _file_by_path(summary, path)
    if file_info is None:
        return _tool_result(
            call,
            status="not_found",
            content="本次 diff 中没有找到该文件；工具不会额外请求 GitHub。",
            round_index=round_index,
        )

    patch = str(file_info.get("patch") or "").strip()
    if not patch:
        content = "本次 diff 未提供该文件 patch，可能是二进制文件、过大文件或重命名。"
    else:
        content = patch
    return _tool_result(
        call,
        status="ok",
        content=_truncate(settings, content),
        round_index=round_index,
        truncated=len(content) > _max_result_chars(settings),
        filename=str(file_info.get("filename") or path),
    )


async def _read_changed_file(
    settings: PluginConfig,
    summary: DiffSummary,
    call: dict[str, Any],
    *,
    round_index: int,
    github_file_cache: dict[str, str] | None,
) -> dict[str, Any]:
    path = _clean_path(call.get("path"))
    if not path:
        return _tool_result(call, status="rejected", content="读取文件需要提供 path。", round_index=round_index)
    if not _is_safe_relative_path(path):
        return _tool_result(call, status="rejected", content="path 不是安全的相对路径。", round_index=round_index)

    file_info = _file_by_path(summary, path)
    filename = str((file_info or {}).get("filename") or path)
    local_content = _local_file_content(file_info)
    if local_content:
        return _tool_result(
            call,
            status="ok",
            content=_truncate(settings, local_content),
            round_index=round_index,
            truncated=len(local_content) > _max_result_chars(settings),
            filename=filename,
            source="local",
        )

    if not summary.head_sha:
        return _tool_result(
            call,
            status="failed",
            content="本地没有该文件完整内容，且本次任务缺少 head sha，无法从 GitHub 拉取。",
            round_index=round_index,
            filename=filename,
            source="github",
        )

    cache = github_file_cache if github_file_cache is not None else {}
    cache_key = f"{summary.head_sha}:{path}"
    try:
        if cache_key in cache:
            content = cache[cache_key]
            source = "github_cache"
        else:
            client = GitHubClient(token=settings.github_token, timeout=settings.timeout_seconds)
            disk_cache = _github_cache_from_settings(settings)
            if disk_cache is None:
                content = await _fetch_github_file_text(client, summary.head_sha, path)
                source = "github"
            else:
                result = await asyncio.to_thread(
                    disk_cache.get_file_text,
                    REPO_FULL_NAME,
                    summary.head_sha,
                    path,
                    lambda: client.get_file_text(REPO_FULL_NAME, summary.head_sha, path),
                )
                content = result.value
                source = "github_cache" if result.source == "cache" else "github"
            cache[cache_key] = content
    except GitHubRequestError as exc:
        patch = str((file_info or {}).get("patch") or "").strip()
        if patch:
            content = f"本地没有该文件完整内容，GitHub 拉取失败: {exc}。\n以下仅为本次 diff patch:\n{patch}"
            return _tool_result(
                call,
                status="partial",
                content=_truncate(settings, content),
                round_index=round_index,
                truncated=len(content) > _max_result_chars(settings),
                filename=filename,
                source="patch_fallback",
            )
        return _tool_result(
            call,
            status="failed",
            content=f"本地没有该文件完整内容，GitHub 拉取失败: {exc}。",
            round_index=round_index,
            filename=filename,
            source="github",
        )

    return _tool_result(
        call,
        status="ok",
        content=_truncate(settings, content),
        round_index=round_index,
        truncated=len(content) > _max_result_chars(settings),
        filename=filename,
        source=source,
    )


def _search_changed_files(
    settings: PluginConfig,
    summary: DiffSummary,
    call: dict[str, Any],
    *,
    round_index: int,
) -> dict[str, Any]:
    query = str(call.get("query") or call.get("path") or "").strip()
    if not query:
        return _tool_result(call, status="rejected", content="搜索需要提供 query。", round_index=round_index)

    query_lower = query.lower()
    matches: list[dict[str, Any]] = []
    for file_info in summary.files:
        filename = str(file_info.get("filename") or "")
        patch = str(file_info.get("patch") or "")
        if query_lower not in filename.lower() and query_lower not in patch.lower():
            continue
        snippets = _matching_snippets(patch, query_lower)
        matches.append(
            {
                "filename": filename,
                "status": file_info.get("status") or "",
                "additions": file_info.get("additions") or 0,
                "deletions": file_info.get("deletions") or 0,
                "snippets": snippets[:3],
            }
        )
        if len(matches) >= 20:
            break

    content = json.dumps({"query": query, "matches": matches}, ensure_ascii=False, indent=2)
    return _tool_result(
        call,
        status="ok" if matches else "not_found",
        content=_truncate(settings, content),
        round_index=round_index,
        truncated=len(content) > _max_result_chars(settings),
    )


def _list_related_files(
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    call: dict[str, Any],
    *,
    round_index: int,
) -> dict[str, Any]:
    path = _clean_path(call.get("path"))
    query = str(call.get("query") or "").strip()
    needle = _relation_needle(path or query)
    if not needle:
        return _tool_result(call, status="rejected", content="列出关联文件需要提供 path 或 query。", round_index=round_index)

    current_paths = {str(file_info.get("filename") or "") for file_info in chunk.files}
    related: list[dict[str, Any]] = []
    for file_info in summary.files:
        filename = str(file_info.get("filename") or "")
        relation_score = _relation_score(filename, needle, path)
        if relation_score <= 0:
            continue
        related.append(
            {
                "filename": filename,
                "status": file_info.get("status") or "",
                "additions": file_info.get("additions") or 0,
                "deletions": file_info.get("deletions") or 0,
                "in_current_chunk": filename in current_paths,
                "relation_score": relation_score,
            }
        )
    related.sort(key=lambda item: (-int(item["relation_score"]), str(item["filename"])))
    content = json.dumps({"needle": needle, "related_files": related[:50]}, ensure_ascii=False, indent=2)
    return _tool_result(
        call,
        status="ok" if related else "not_found",
        content=_truncate(settings, content),
        round_index=round_index,
        truncated=len(content) > _max_result_chars(settings),
    )


def _tool_result(
    call: dict[str, Any],
    *,
    status: str,
    content: str,
    round_index: int,
    truncated: bool = False,
    filename: str = "",
    source: str = "",
) -> dict[str, Any]:
    return {
        "round": round_index,
        "tool": str(call.get("tool") or "").strip(),
        "path": _clean_path(call.get("path")),
        "query": str(call.get("query") or "").strip(),
        "reason": str(call.get("reason") or "").strip(),
        "status": status,
        "filename": filename,
        "source": source,
        "truncated": truncated,
        "content_chars": len(content),
        "content": content,
    }


def _record_tool_call(settings: PluginConfig, result: dict[str, Any]) -> None:
    recorder = getattr(settings, "task_log_recorder", None)
    if not callable(recorder):
        return
    try:
        recorder(
            "模型工具调用",
            {
                "轮次": result.get("round"),
                "工具": result.get("tool"),
                "路径": result.get("path"),
                "查询": result.get("query"),
                "原因": result.get("reason"),
                "结果": result.get("status"),
                "来源": result.get("source"),
                "返回字符数": result.get("content_chars"),
                "已截断": "是" if result.get("truncated") else "否",
            },
        )
    except Exception as exc:
        logger.warning("[%s] 写入工具调用日志失败: %s", PLUGIN_NAME, exc)


def _file_by_path(summary: DiffSummary, path: str) -> dict[str, Any] | None:
    normalized = _clean_path(path)
    for file_info in summary.files:
        if _clean_path(file_info.get("filename")) == normalized:
            return file_info
    return None


def _clean_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")


def _local_file_content(file_info: dict[str, Any] | None) -> str:
    if not isinstance(file_info, dict):
        return ""
    for key in ("content", "full_content", "raw_content", "file_content"):
        content = str(file_info.get(key) or "")
        if content:
            return content
    return ""


def _is_safe_relative_path(path: str) -> bool:
    return bool(path) and not path.startswith("/") and ".." not in path.split("/")


def _matching_snippets(patch: str, query_lower: str) -> list[str]:
    snippets: list[str] = []
    lines = patch.splitlines()
    for index, line in enumerate(lines):
        if query_lower not in line.lower():
            continue
        start = max(0, index - 1)
        end = min(len(lines), index + 2)
        snippets.append("\n".join(lines[start:end])[:1000])
        if len(snippets) >= 3:
            break
    return snippets


def _relation_needle(value: str) -> str:
    text = _clean_path(value)
    if not text:
        return ""
    basename = text.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    stem = re.sub(r"\d+", "#", stem.lower())
    return re.sub(r"[_\-.]+", "_", stem).strip("_")


def _relation_score(filename: str, needle: str, requested_path: str) -> int:
    normalized_filename = filename.replace("\\", "/")
    filename_needle = _relation_needle(normalized_filename)
    if requested_path and _clean_path(requested_path) == _clean_path(filename):
        return 100
    if needle and needle == filename_needle:
        return 80
    if needle and needle in filename_needle:
        return 50
    if needle and needle in normalized_filename.lower().replace("-", "_"):
        return 30
    return 0


def _truncate(settings: PluginConfig, content: str) -> str:
    limit = _max_result_chars(settings)
    if len(content) <= limit:
        return content
    return content[:limit] + "\n...（工具结果已按 max_tool_result_chars 截断）"


def _max_result_chars(settings: PluginConfig) -> int:
    return max(1000, int(getattr(settings, "max_tool_result_chars", 12000) or 12000))


def _github_cache_from_settings(settings: PluginConfig) -> GitHubCache | None:
    cache_dir = getattr(settings, "github_cache_dir", None)
    if not cache_dir:
        return None
    return GitHubCache(Path(cache_dir), task_log_recorder=getattr(settings, "task_log_recorder", None))


async def _fetch_github_file_text(client: GitHubClient, ref: str, path: str) -> str:
    try:
        return await asyncio.to_thread(client.get_file_text, REPO_FULL_NAME, ref, path)
    except GitHubRequestError:
        raise
    except Exception as exc:
        raise GitHubRequestError(message=str(exc)) from exc
