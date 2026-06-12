from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiffChunk:
    index: int
    total: int
    files: list[dict[str, Any]]
    patch_chars: int


@dataclass(frozen=True)
class DiffSummary:
    base_sha: str
    head_sha: str
    compare_url: str
    total_commits: int
    total_files: int
    additions: int
    deletions: int
    changed_files: int
    commits: list[dict[str, Any]]
    files: list[dict[str, Any]]
    chunks: list[DiffChunk]


def build_diff_summary(
    compare_payload: dict[str, Any],
    *,
    raw_diff_text: str = "",
    max_files: int = 0,
    max_chars: int = 0,
) -> DiffSummary:
    raw_files = parse_unified_diff(raw_diff_text)
    files = raw_files or [
        _normalize_file(file_payload)
        for file_payload in compare_payload.get("files", [])
        if isinstance(file_payload, dict)
    ]
    commits = [
        _normalize_commit(commit_payload)
        for commit_payload in compare_payload.get("commits", [])
        if isinstance(commit_payload, dict)
    ]
    chunks = split_files(files, max_files=max_files, max_chars=max_chars)
    return DiffSummary(
        base_sha=str((compare_payload.get("base_commit") or {}).get("sha") or ""),
        head_sha=str(commits[-1].get("sha") if commits else compare_payload.get("sha") or ""),
        compare_url=str(compare_payload.get("html_url") or compare_payload.get("permalink_url") or ""),
        total_commits=len(commits),
        total_files=len(files),
        additions=sum(int(file.get("additions") or 0) for file in files),
        deletions=sum(int(file.get("deletions") or 0) for file in files),
        changed_files=int(compare_payload.get("files_changed") or len(files)),
        commits=commits,
        files=files,
        chunks=chunks,
    )


def split_files(files: list[dict[str, Any]], *, max_files: int = 0, max_chars: int = 0) -> list[DiffChunk]:
    if not files:
        return [DiffChunk(index=1, total=1, files=[], patch_chars=0)]

    files = order_files_by_similarity(files)
    chunk_count = _target_chunk_count(files, max_files=max_files, max_chars=max_chars)
    chunks = _split_files_by_balanced_chars(files, chunk_count)

    total = len(chunks)
    return [
        DiffChunk(index=index + 1, total=total, files=chunk_files, patch_chars=sum(_file_patch_chars(item) for item in chunk_files))
        for index, chunk_files in enumerate(chunks)
    ]


def _target_chunk_count(files: list[dict[str, Any]], *, max_files: int = 0, max_chars: int = 0) -> int:
    file_count = len(files)
    total_chars = sum(_file_patch_chars(file_info) for file_info in files)
    by_files = (file_count + max_files - 1) // max_files if max_files > 0 else 1
    by_chars = (total_chars + max_chars - 1) // max_chars if max_chars > 0 else 1
    return max(1, min(file_count, max(by_files, by_chars)))


def _split_files_by_balanced_chars(files: list[dict[str, Any]], chunk_count: int) -> list[list[dict[str, Any]]]:
    if chunk_count <= 1:
        return [files]

    chunks: list[list[dict[str, Any]]] = []
    remaining_files = files
    remaining_chars = sum(_file_patch_chars(file_info) for file_info in remaining_files)
    remaining_chunks = chunk_count

    while remaining_chunks > 1:
        target_chars = remaining_chars / remaining_chunks
        split_index = _best_split_index(remaining_files, target_chars)
        chunk_files = remaining_files[:split_index]
        chunks.append(chunk_files)
        remaining_files = remaining_files[split_index:]
        remaining_chars -= sum(_file_patch_chars(file_info) for file_info in chunk_files)
        remaining_chunks -= 1

    chunks.append(remaining_files)
    return chunks


def _best_split_index(files: list[dict[str, Any]], target_chars: float) -> int:
    max_index = len(files) - 1
    best_index = 1
    best_distance = float("inf")
    running_chars = 0

    for index in range(1, max_index + 1):
        running_chars += _file_patch_chars(files[index - 1])
        distance = abs(running_chars - target_chars)
        if distance < best_distance:
            best_index = index
            best_distance = distance

    return best_index


def order_files_by_similarity(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for file_info in files:
        groups.setdefault(_similarity_key(file_info), []).append(file_info)
    return [file_info for group in groups.values() for file_info in group]


def render_chunk_input(summary: DiffSummary, chunk: DiffChunk) -> str:
    lines = [
        f"提交范围: {short_sha(summary.base_sha)}...{short_sha(summary.head_sha)}",
        f"分片: {chunk.index}/{chunk.total}",
        f"提交数: {summary.total_commits}",
        f"文件数: {summary.total_files}",
        f"当前分片文件数: {len(chunk.files)}",
        "",
        "提交列表:",
    ]
    for commit in summary.commits:
        message = first_line(commit.get("message", ""))
        lines.append(f"- {short_sha(commit.get('sha', ''))} {message} ({commit.get('author_name') or 'unknown'})")

    lines.extend(["", "文件变更:"])
    for file_info in chunk.files:
        lines.append(
            f"\n### {file_info['filename']} "
            f"[{file_info['status']}, +{file_info['additions']}/-{file_info['deletions']}]"
        )
        patch = str(file_info.get("patch") or "").strip()
        if patch:
            lines.append(patch)
        else:
            lines.append("(GitHub 未返回该文件 patch，可能是二进制文件、过大文件或重命名。)")
    return "\n".join(lines)


def short_sha(value: object) -> str:
    text = str(value or "")
    return text[:7] if text else "unknown"


def first_line(value: object) -> str:
    return str(value or "").strip().splitlines()[0][:220]


def _normalize_file(file_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": str(file_payload.get("filename") or ""),
        "status": str(file_payload.get("status") or "modified"),
        "additions": int(file_payload.get("additions") or 0),
        "deletions": int(file_payload.get("deletions") or 0),
        "changes": int(file_payload.get("changes") or 0),
        "patch": str(file_payload.get("patch") or ""),
        "blob_url": str(file_payload.get("blob_url") or ""),
        "raw_url": str(file_payload.get("raw_url") or ""),
    }


def _normalize_commit(commit_payload: dict[str, Any]) -> dict[str, Any]:
    commit = commit_payload.get("commit") if isinstance(commit_payload.get("commit"), dict) else {}
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    return {
        "sha": str(commit_payload.get("sha") or ""),
        "html_url": str(commit_payload.get("html_url") or ""),
        "message": str(commit.get("message") or ""),
        "author_name": str(author.get("name") or ""),
        "authored_at": str(author.get("date") or ""),
    }


def _file_patch_chars(file_info: dict[str, Any]) -> int:
    return len(str(file_info.get("patch") or "")) + len(str(file_info.get("filename") or ""))


def _similarity_key(file_info: dict[str, Any]) -> str:
    filename = str(file_info.get("filename") or "").strip().replace("\\", "/")
    directory, _, basename = filename.rpartition("/")
    if not basename:
        basename = directory
        directory = ""
    stem, dot, suffix = basename.rpartition(".")
    if not dot:
        stem = basename
        suffix = ""
    normalized_stem = re.sub(r"\d+", "#", stem.lower())
    normalized_stem = re.sub(r"[_\-.]+", "_", normalized_stem).strip("_")
    return f"{directory.lower()}|{normalized_stem}|{suffix.lower()}"


def parse_unified_diff(raw_diff_text: str) -> list[dict[str, Any]]:
    text = str(raw_diff_text or "")
    if not text.strip():
        return []

    files: list[dict[str, Any]] = []
    current_lines: list[str] = []
    current_filename = ""

    def flush() -> None:
        nonlocal current_lines, current_filename
        if not current_lines:
            return
        patch = "\n".join(current_lines)
        filename = current_filename or _filename_from_patch(patch)
        additions, deletions = _count_patch_changes(current_lines)
        files.append(
            {
                "filename": filename,
                "status": _status_from_patch(current_lines),
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
                "patch": patch,
                "blob_url": "",
                "raw_url": "",
            }
        )
        current_lines = []
        current_filename = ""

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_filename = _filename_from_diff_header(line)
        current_lines.append(line)
    flush()
    return files


def _filename_from_diff_header(line: str) -> str:
    parts = line.split()
    if len(parts) >= 4:
        candidate = parts[3]
        if candidate.startswith("b/"):
            return candidate[2:]
        return candidate
    return ""


def _filename_from_patch(patch: str) -> str:
    for line in patch.splitlines():
        if line.startswith("+++ "):
            candidate = line[4:].strip()
            if candidate != "/dev/null":
                return candidate[2:] if candidate.startswith("b/") else candidate
        if line.startswith("--- "):
            candidate = line[4:].strip()
            if candidate != "/dev/null":
                return candidate[2:] if candidate.startswith("a/") else candidate
    return "unknown"


def _count_patch_changes(lines: list[str]) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in lines:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _status_from_patch(lines: list[str]) -> str:
    text = "\n".join(lines[:20])
    if "new file mode" in text:
        return "added"
    if "deleted file mode" in text:
        return "removed"
    if "rename from " in text and "rename to " in text:
        return "renamed"
    return "modified"
