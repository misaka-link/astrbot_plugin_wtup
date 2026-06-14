from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .analysis import TokenUsage
from .config import PLUGIN_NAME, PluginConfig
from .diff_collector import DiffSummary, short_sha
from .report_log import build_task_artifact_dirname


_logger = logging.getLogger(PLUGIN_NAME)
CACHE_SCHEMA_VERSION = 1
CACHE_FILE_NAME = "analysis.json"


@dataclass(frozen=True)
class AnalysisCacheEntry:
    key: str
    directory: Path
    analysis: dict[str, Any]
    merged_analysis: dict[str, Any] | None
    token_usage: TokenUsage
    pre_summary_token_usage: TokenUsage


class AnalysisResultCache:
    def __init__(
        self,
        root: Path,
        *,
        task_log_recorder: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.root = Path(root)
        self.task_log_recorder = task_log_recorder

    def build_key(self, *, settings: PluginConfig, repo: str, summary: DiffSummary) -> str:
        payload = self._fingerprint_payload(settings=settings, repo=repo, summary=summary)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]

    def directory_for(self, *, settings: PluginConfig, repo: str, summary: DiffSummary) -> Path:
        key = self.build_key(settings=settings, repo=repo, summary=summary)
        return self.root / build_task_artifact_dirname(summary, key)

    def read(self, *, settings: PluginConfig, repo: str, summary: DiffSummary) -> AnalysisCacheEntry | None:
        key = self.build_key(settings=settings, repo=repo, summary=summary)
        directory = self.directory_for(settings=settings, repo=repo, summary=summary)
        path = directory / CACHE_FILE_NAME
        if not path.exists():
            self._record("分析结果缓存未命中", repo=repo, summary=summary, key=key, path=directory)
            return None

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _logger.warning("[%s] 分析结果缓存读取失败: %s (%s)", PLUGIN_NAME, path, exc)
            self._record("分析结果缓存读取失败", repo=repo, summary=summary, key=key, path=directory)
            return None

        if not isinstance(payload, dict) or payload.get("schema_version") != CACHE_SCHEMA_VERSION:
            self._record("分析结果缓存版本不兼容", repo=repo, summary=summary, key=key, path=directory)
            return None
        if payload.get("cache_key") != key:
            self._record("分析结果缓存特征不匹配", repo=repo, summary=summary, key=key, path=directory)
            return None
        if payload.get("base_sha") != summary.base_sha or payload.get("head_sha") != summary.head_sha:
            self._record("分析结果缓存提交范围不匹配", repo=repo, summary=summary, key=key, path=directory)
            return None

        analysis = payload.get("analysis")
        if not isinstance(analysis, dict):
            self._record("分析结果缓存内容无效", repo=repo, summary=summary, key=key, path=directory)
            return None

        merged_analysis = payload.get("merged_analysis")
        if not isinstance(merged_analysis, dict):
            merged_analysis = None

        self._record("分析结果缓存命中", repo=repo, summary=summary, key=key, path=directory)
        return AnalysisCacheEntry(
            key=key,
            directory=directory,
            analysis=analysis,
            merged_analysis=merged_analysis,
            token_usage=_token_usage_from_payload(payload.get("token_usage")),
            pre_summary_token_usage=_token_usage_from_payload(payload.get("pre_summary_token_usage")),
        )

    def write(
        self,
        *,
        settings: PluginConfig,
        repo: str,
        summary: DiffSummary,
        analysis: dict[str, Any],
        merged_analysis: dict[str, Any] | None,
        token_usage: TokenUsage,
        pre_summary_token_usage: TokenUsage,
    ) -> Path | None:
        key = self.build_key(settings=settings, repo=repo, summary=summary)
        directory = self.directory_for(settings=settings, repo=repo, summary=summary)
        path = directory / CACHE_FILE_NAME
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "cache_key": key,
            "repo": repo,
            "base_sha": summary.base_sha,
            "head_sha": summary.head_sha,
            "range": f"{summary.base_sha}...{summary.head_sha}",
            "config_digest": _config_digest(settings),
            "created_at": time.time(),
            "analysis": analysis,
            "merged_analysis": merged_analysis,
            "token_usage": token_usage.to_dict(),
            "pre_summary_token_usage": pre_summary_token_usage.to_dict(),
        }
        try:
            directory.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)
        except Exception as exc:
            _logger.warning("[%s] 分析结果缓存写入失败: %s (%s)", PLUGIN_NAME, path, exc)
            self._record("分析结果缓存写入失败", repo=repo, summary=summary, key=key, path=directory)
            return None

        self._record("分析结果缓存写入", repo=repo, summary=summary, key=key, path=directory)
        return directory

    def _fingerprint_payload(self, *, settings: PluginConfig, repo: str, summary: DiffSummary) -> dict[str, Any]:
        return {
            "schema_version": CACHE_SCHEMA_VERSION,
            "repo": repo,
            "base_sha": summary.base_sha,
            "head_sha": summary.head_sha,
            "config": _config_payload(settings),
        }

    def _record(self, event: str, *, repo: str, summary: DiffSummary, key: str, path: Path) -> None:
        _logger.warning(
            "[%s] %s: range=%s...%s key=%s path=%s",
            PLUGIN_NAME,
            event,
            short_sha(summary.base_sha),
            short_sha(summary.head_sha),
            key,
            path,
        )
        recorder = self.task_log_recorder
        if not callable(recorder):
            return
        try:
            recorder(
                event,
                {
                    "仓库": repo,
                    "提交范围": f"{short_sha(summary.base_sha)}...{short_sha(summary.head_sha)}",
                    "缓存特征": key,
                    "缓存目录": str(path),
                },
            )
        except Exception as exc:
            _logger.warning("[%s] 写入分析结果缓存任务日志失败: %s", PLUGIN_NAME, exc)


def _config_digest(settings: PluginConfig) -> str:
    text = json.dumps(_config_payload(settings), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _config_payload(settings: PluginConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in fields(settings):
        if not field.compare:
            continue
        payload[field.name] = _jsonable_value(getattr(settings, field.name))
    return payload


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_jsonable_value(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _jsonable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return str(value)


def _token_usage_from_payload(value: Any) -> TokenUsage:
    if not isinstance(value, dict):
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=_as_int(value.get("prompt_tokens")),
        completion_tokens=_as_int(value.get("completion_tokens")),
        total_tokens=_as_int(value.get("total_tokens")),
    )


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
