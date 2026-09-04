from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import REPO_FULL_NAME, AnalyzerConfig as PluginConfig
from .ext_cli import ExtCliBinaryManager, ExtCliError, ExtCliRunner
from .github_client import GitHubClient, GitHubRequestError
from .github_cache import GitHubCache
from .struct_diff import (
    DIFF_KIND_EMPTY,
    DIFF_KIND_ERROR,
    DIFF_KIND_SEMANTIC,
    DIFF_KIND_TEXT,
    StructCompareResult,
    build_struct_compare,
)
from .termination import TaskTerminatedError


_logger = logging.getLogger("wtup_standalone")

PACKED_UNPACKABLE_SUFFIXES = (".blk", ".dxp", ".grp")
VROMFS_SUFFIX = ".vromfs.bin"
MAX_NOTES = 20

ScopeMissing = "missing"
ScopeAll = "all"


@dataclass
class EnrichStats:
    scanned: int = 0
    processed: int = 0
    backfilled_missing: int = 0
    rewritten_from_patch: int = 0
    kept_original_patch: int = 0
    unchanged_marked: int = 0
    skipped_removed: int = 0
    failures: int = 0
    budget_exhausted: bool = False
    ext_cli_used: bool = False
    notes: list[str] = field(default_factory=list)

    def add_note(self, message: str) -> None:
        if len(self.notes) >= MAX_NOTES:
            return
        self.notes.append(str(message)[:300])

    def summary(self) -> str:
        parts = [
            f"扫描 {self.scanned}",
            f"处理 {self.processed}",
            f"回填无patch {self.backfilled_missing}",
            f"替换patch {self.rewritten_from_patch}",
            f"保留原patch {self.kept_original_patch}",
            f"标注一致 {self.unchanged_marked}",
            f"跳过已删除 {self.skipped_removed}",
            f"失败 {self.failures}",
        ]
        if self.budget_exhausted:
            parts.append("已达文件数预算上限")
        if self.ext_cli_used:
            parts.append("使用wt_ext_cli解包")
        return ", ".join(parts)


def looks_like_packed(filename: str) -> bool:
    name = str(filename or "").strip().lower()
    if name.endswith(VROMFS_SUFFIX):
        return True
    return any(name.endswith(suffix) for suffix in PACKED_UNPACKABLE_SUFFIXES)


def enrich_summary_with_struct_compare(
    settings: PluginConfig,
    client: GitHubClient,
    cache: GitHubCache,
    summary: Any,
    *,
    should_terminate: Callable[[], bool] | None = None,
    binary_cache_dir: Path | None = None,
) -> EnrichStats:
    """为 summary.files 补充/替换“新旧版本结构化对比”内容。

    - scope=missing：只为 GitHub 未返回 patch 的文件生成对比（默认）。
    - scope=all：所有文件都尝试用语义对比替换行级 patch（JSON 解析成功时）。
    任一环节失败都会保留原状并记录原因，不影响后续分析流程。
    """
    stats = EnrichStats()
    base_ref = str(getattr(summary, "base_sha", "") or "")
    head_ref = str(getattr(summary, "head_sha", "") or "")
    files = list(getattr(summary, "files", None) or [])
    if not base_ref or not head_ref or not files:
        return stats

    scope_all = _scope_all(settings)
    budget = _max_files(settings)
    max_chars = _max_chars_per_file(settings)

    runner: ExtCliRunner | None = None
    manager = ExtCliBinaryManager(_resolve_binary_cache_dir(settings, binary_cache_dir))

    for file_info in files:
        if should_terminate is not None and should_terminate():
            raise TaskTerminatedError("新旧版本结构化对比")
        if not isinstance(file_info, dict):
            continue
        stats.scanned += 1
        filename = str(file_info.get("filename") or "").strip()
        status = str(file_info.get("status") or "modified")
        if not filename:
            continue
        if status == "removed":
            stats.skipped_removed += 1
            continue
        has_patch = bool(str(file_info.get("patch") or "").strip())
        if has_patch and not scope_all:
            continue
        if budget > 0 and stats.processed >= budget:
            stats.budget_exhausted = True
            break

        stats.processed += 1
        try:
            old_bytes = b"" if status == "added" else _fetch_blob(settings, client, cache, base_ref, filename)
            new_bytes = _fetch_blob(settings, client, cache, head_ref, filename)
        except GitHubRequestError as exc:
            stats.failures += 1
            stats.add_note(f"{filename}: 获取文件内容失败 ({exc})")
            continue

        old_text = _decode_strict(old_bytes)
        new_text = _decode_strict(new_bytes)
        used_ext_cli = False
        if old_text is None or new_text is None:
            if not looks_like_packed(filename):
                stats.failures += 1
                stats.add_note(f"{filename}: 内容不是文本且非可识别的打包格式")
                continue
            if filename.lower().endswith(VROMFS_SUFFIX):
                stats.failures += 1
                stats.add_note(f"{filename}: 完整 vromfs 归档暂不支持逐文件下载对比")
                continue
            if runner is None:
                try:
                    resolved = manager.resolve(
                        getattr(settings, "ext_cli_binary_path", "") or "",
                        auto_download=_auto_download(settings),
                    )
                    runner = ExtCliRunner(resolved, timeout_seconds=_ext_cli_timeout(settings))
                except ExtCliError as exc:
                    stats.failures += 1
                    stats.add_note(f"{filename}: {exc}")
                    continue
            try:
                old_text = "" if status == "added" else _unpack_packed(runner, old_bytes, settings)
                new_text = _unpack_packed(runner, new_bytes, settings)
                used_ext_cli = True
                stats.ext_cli_used = True
            except ExtCliError as exc:
                stats.failures += 1
                stats.add_note(f"{filename}: wt_ext_cli 解包失败 ({exc})")
                continue

        result = build_struct_compare(old_text or "", new_text or "", max_chars=max_chars)
        _write_back(file_info, result, has_patch=has_patch, scope_all=scope_all, used_ext_cli=used_ext_cli, stats=stats)
    return stats


def _write_back(
    file_info: dict,
    result: StructCompareResult,
    *,
    has_patch: bool,
    scope_all: bool,
    used_ext_cli: bool,
    stats: EnrichStats,
) -> None:
    provenance = f"struct:{result.kind}" + ("+ext_cli" if used_ext_cli else "")
    file_info["diff_source"] = provenance
    if result.kind == DIFF_KIND_EMPTY:
        if has_patch:
            # 行级 patch 有差异但语义内容一致（多为格式化改动），保留原 patch。
            stats.kept_original_patch += 1
            return
        file_info["patch"] = "(程序对比：新旧版本内容一致)"
        stats.unchanged_marked += 1
        return
    if not has_patch:
        file_info["patch"] = _render_block(result)
        stats.backfilled_missing += 1
        return
    if scope_all and result.kind == DIFF_KIND_SEMANTIC:
        file_info["patch"] = _render_block(result)
        stats.rewritten_from_patch += 1
        return
    stats.kept_original_patch += 1


def _render_block(result: StructCompareResult) -> str:
    label = {
        DIFF_KIND_SEMANTIC: "JSON 字段级",
        DIFF_KIND_TEXT: "统一文本",
    }.get(result.kind, result.kind)
    header = f"(程序生成的{label}新旧版本结构化对比，共 {result.change_count} 处变化)"
    return f"{header}\n{result.text}"


def _fetch_blob(
    settings: PluginConfig,
    client: GitHubClient,
    cache: GitHubCache,
    ref: str,
    filename: str,
) -> bytes:
    def fetch() -> bytes:
        return client.get_blob(REPO_FULL_NAME, ref, filename)

    return cache.get_file_bytes(REPO_FULL_NAME, ref, filename, fetch).value


def _decode_strict(data: bytes) -> str | None:
    try:
        return bytes(data or b"").decode("utf-8")
    except UnicodeDecodeError:
        return None


def _unpack_packed(runner: ExtCliRunner, data: bytes, settings: PluginConfig) -> str:
    if not data:
        return ""
    return runner.unpack_blk_bytes(
        data,
        nm_path=str(getattr(settings, "ext_cli_nm_path", "") or ""),
        dict_path=str(getattr(settings, "ext_cli_dict_path", "") or ""),
    )


def _scope_all(settings: PluginConfig) -> bool:
    value = str(getattr(settings, "struct_diff_scope", ScopeMissing) or ScopeMissing).strip().lower()
    return value == ScopeAll


def _max_files(settings: PluginConfig) -> int:
    value = getattr(settings, "struct_diff_max_files", 60)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 60
    return max(0, parsed)


def _max_chars_per_file(settings: PluginConfig) -> int:
    value = getattr(settings, "struct_diff_max_chars_per_file", 6000)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 6000
    return max(500, parsed)


def _auto_download(settings: PluginConfig) -> bool:
    return bool(getattr(settings, "ext_cli_auto_download", True))


def _ext_cli_timeout(settings: PluginConfig) -> float:
    value = getattr(settings, "ext_cli_timeout_seconds", 60)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 60.0
    return max(5.0, parsed)


def _resolve_binary_cache_dir(settings: PluginConfig, override: Path | None) -> Path:
    if override is not None:
        return Path(override)
    cache_dir = getattr(settings, "github_cache_dir", None)
    if cache_dir is not None:
        return Path(cache_dir).parent / "ext_cli_bin"
    return Path.cwd() / "ext_cli_bin"