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
    base_version: str = ""
    head_version: str = ""


VERSION_RE = re.compile(r"\b\d+(?:\.\d+){1,4}\b")
REPORT_TITLE_RE = re.compile(r"^\s*(?P<base>\d+(?:\.\d+)*)?\s*->\s*(?P<head>\d+(?:\.\d+)*)?\s*$")


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
    raw_commits = [
        commit_payload
        for commit_payload in compare_payload.get("commits", [])
        if isinstance(commit_payload, dict)
    ]
    commits = [
        _normalize_commit(commit_payload)
        for commit_payload in raw_commits
    ]
    chunks = split_files(files, max_files=max_files, max_chars=max_chars)
    base_commit = compare_payload.get("base_commit") if isinstance(compare_payload.get("base_commit"), dict) else {}
    return DiffSummary(
        base_sha=str(base_commit.get("sha") or ""),
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
        base_version=extract_version_from_commit(base_commit),
        head_version=extract_version_from_commit(raw_commits[-1] if raw_commits else {}),
    )


def normalize_report_title(summary: DiffSummary, title: Any) -> str:
    text = str(title or "").strip().replace("→", "->")
    inferred_base = str(getattr(summary, "base_version", "") or "").strip()
    inferred_head = str(getattr(summary, "head_version", "") or "").strip()

    match = REPORT_TITLE_RE.match(text)
    if match:
        base_version = match.group("base") or inferred_base
        head_version = match.group("head") or inferred_head
        if base_version and head_version:
            return f"{base_version}->{head_version}"
        inferred = inferred_version_range(summary)
        return inferred or ""

    versions = VERSION_RE.findall(text)
    if len(versions) == 1:
        version = versions[0]
        if inferred_base and version == inferred_head:
            return f"{inferred_base}->{version}"
        if inferred_head and version == inferred_base:
            return f"{version}->{inferred_head}"
        inferred = inferred_version_range(summary)
        if inferred:
            return inferred

    inferred = inferred_version_range(summary)
    if inferred and (not text or "->" in text or versions):
        return inferred
    return text


def inferred_version_range(summary: DiffSummary) -> str:
    base_version = str(getattr(summary, "base_version", "") or "").strip()
    head_version = str(getattr(summary, "head_version", "") or "").strip()
    if base_version and head_version:
        return f"{base_version}->{head_version}"
    return ""


def split_files(files: list[dict[str, Any]], *, max_files: int = 0, max_chars: int = 0) -> list[DiffChunk]:
    if not files:
        return [DiffChunk(index=1, total=1, files=[], patch_chars=0)]

    related_groups = group_related_files(files)
    group_chunks = _split_file_groups_by_file_limit(related_groups, max_files)
    if max_chars > 0:
        group_chunks = [
            chunk
            for group_chunk in group_chunks
            for chunk in _split_file_groups_by_char_limit(group_chunk, max_chars)
        ]

    chunks = [_flatten_file_groups(group_chunk) for group_chunk in group_chunks]
    total = len(chunks)
    return [
        DiffChunk(index=index + 1, total=total, files=chunk_files, patch_chars=sum(_file_patch_chars(item) for item in chunk_files))
        for index, chunk_files in enumerate(chunks)
    ]


def group_related_files(files: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    key_to_group_index: dict[str, int] = {}

    for file_info in files:
        relation_keys = _relation_keys(file_info)
        matched_indexes = sorted(
            {key_to_group_index[key] for key in relation_keys if key in key_to_group_index}
        )
        if not matched_indexes:
            key_to_group_index.update({key: len(groups) for key in relation_keys})
            groups.append([file_info])
            continue

        target_index = matched_indexes[0]
        for source_index in reversed(matched_indexes[1:]):
            groups[target_index].extend(groups[source_index])
            del groups[source_index]
            for key, group_index in list(key_to_group_index.items()):
                if group_index == source_index:
                    key_to_group_index[key] = target_index
                elif group_index > source_index:
                    key_to_group_index[key] = group_index - 1

        groups[target_index].append(file_info)
        for key in relation_keys:
            key_to_group_index[key] = target_index

    return _order_file_groups_by_directory(groups)


def _split_files_by_file_limit(files: list[dict[str, Any]], max_files: int = 0) -> list[list[dict[str, Any]]]:
    if max_files <= 0:
        return [files]
    return [files[index : index + max_files] for index in range(0, len(files), max_files)]


def _split_file_groups_by_file_limit(
    groups: list[list[dict[str, Any]]],
    max_files: int = 0,
) -> list[list[list[dict[str, Any]]]]:
    if max_files <= 0:
        return [groups]

    chunks: list[list[list[dict[str, Any]]]] = []
    current: list[list[dict[str, Any]]] = []
    current_count = 0

    for group in groups:
        for part in _split_files_by_file_limit(group, max_files):
            part_count = len(part)
            if current and current_count + part_count > max_files:
                chunks.append(current)
                current = []
                current_count = 0
            current.append(part)
            current_count += part_count

    if current:
        chunks.append(current)
    return chunks


def _split_files_by_char_limit(files: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    chunk_count = _target_chunk_count(files, max_chars=max_chars)
    return _split_files_by_balanced_chars(files, chunk_count)


def _split_file_groups_by_char_limit(
    groups: list[list[dict[str, Any]]],
    max_chars: int,
) -> list[list[list[dict[str, Any]]]]:
    chunk_count = _target_group_chunk_count(groups, max_chars=max_chars)
    return _split_file_groups_by_balanced_chars(groups, chunk_count)


def _target_chunk_count(files: list[dict[str, Any]], *, max_chars: int = 0) -> int:
    file_count = len(files)
    total_chars = sum(_file_patch_chars(file_info) for file_info in files)
    by_chars = (total_chars + max_chars - 1) // max_chars if max_chars > 0 else 1
    return max(1, min(file_count, by_chars))


def _target_group_chunk_count(groups: list[list[dict[str, Any]]], *, max_chars: int = 0) -> int:
    group_count = len(groups)
    total_chars = sum(_file_group_patch_chars(group) for group in groups)
    by_chars = (total_chars + max_chars - 1) // max_chars if max_chars > 0 else 1
    return max(1, min(group_count, by_chars))


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


def _split_file_groups_by_balanced_chars(
    groups: list[list[dict[str, Any]]],
    chunk_count: int,
) -> list[list[list[dict[str, Any]]]]:
    if chunk_count <= 1:
        return [groups]

    chunks: list[list[list[dict[str, Any]]]] = []
    remaining_groups = groups
    remaining_chars = sum(_file_group_patch_chars(group) for group in remaining_groups)
    remaining_chunks = chunk_count

    while remaining_chunks > 1:
        target_chars = remaining_chars / remaining_chunks
        split_index = _best_group_split_index(remaining_groups, target_chars)
        chunk_groups = remaining_groups[:split_index]
        chunks.append(chunk_groups)
        remaining_groups = remaining_groups[split_index:]
        remaining_chars -= sum(_file_group_patch_chars(group) for group in chunk_groups)
        remaining_chunks -= 1

    chunks.append(remaining_groups)
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


def _best_group_split_index(groups: list[list[dict[str, Any]]], target_chars: float) -> int:
    max_index = len(groups) - 1
    best_index = 1
    best_distance = float("inf")
    running_chars = 0

    for index in range(1, max_index + 1):
        running_chars += _file_group_patch_chars(groups[index - 1])
        distance = abs(running_chars - target_chars)
        if distance < best_distance:
            best_index = index
            best_distance = distance

    return best_index


def order_files_by_similarity(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _flatten_file_groups(group_related_files(files))


def _flatten_file_groups(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [file_info for group in groups for file_info in group]


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


def extract_version_from_commit(commit_payload: dict[str, Any]) -> str:
    message = _commit_message(commit_payload)
    versions = VERSION_RE.findall(message)
    return versions[-1] if versions else ""


def _commit_message(commit_payload: dict[str, Any]) -> str:
    commit = commit_payload.get("commit") if isinstance(commit_payload.get("commit"), dict) else {}
    return str(commit.get("message") or commit_payload.get("message") or "")


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
        "message": _commit_message(commit_payload),
        "author_name": str(author.get("name") or ""),
        "authored_at": str(author.get("date") or ""),
    }


def _file_patch_chars(file_info: dict[str, Any]) -> int:
    return len(str(file_info.get("patch") or "")) + len(str(file_info.get("filename") or ""))


def _file_group_patch_chars(group: list[dict[str, Any]]) -> int:
    return sum(_file_patch_chars(file_info) for file_info in group)


def _similarity_key(file_info: dict[str, Any]) -> str:
    directory, normalized_stem, suffix = _path_identity(file_info)
    return f"{directory}|{normalized_stem}|{suffix}"


def _relation_keys(file_info: dict[str, Any]) -> list[str]:
    directory, normalized_stem, suffix = _path_identity(file_info)
    keys = [f"path:{directory}|{normalized_stem}|{suffix}"]
    if _is_specific_relation_stem(normalized_stem):
        keys.append(f"name:{normalized_stem}|{suffix}")
        keys.append(f"stem:{normalized_stem}")
    return keys


def _path_identity(file_info: dict[str, Any]) -> tuple[str, str, str]:
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
    return directory.lower(), normalized_stem, suffix.lower()


def _is_specific_relation_stem(stem: str) -> bool:
    if len(stem) < 3 or not re.search(r"[a-z]", stem):
        return False
    return stem not in {
        "readme",
        "index",
        "config",
        "common",
        "shared",
        "strings",
        "localization",
        "lang",
        "unit",
        "units",
        "weapon",
        "weapons",
        "vehicle",
        "vehicles",
    }


def _order_file_groups_by_directory(groups: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    directory_buckets: dict[str, list[list[dict[str, Any]]]] = {}
    for group in groups:
        first_file = group[0] if group else {}
        directory = _path_identity(first_file)[0]
        directory_buckets.setdefault(directory, []).append(group)
    return [group for bucket in directory_buckets.values() for group in bucket]


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
