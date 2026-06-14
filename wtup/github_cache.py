from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from .config import PLUGIN_NAME


_logger = logging.getLogger(PLUGIN_NAME)
T = TypeVar("T")


@dataclass(frozen=True)
class CacheResult(Generic[T]):
    value: T
    source: str
    path: Path


class GitHubCache:
    def __init__(self, root: Path, *, task_log_recorder: Callable[[str, dict[str, Any]], Any] | None = None):
        self.root = Path(root)
        self.task_log_recorder = task_log_recorder

    def get_compare(
        self,
        repo: str,
        base_sha: str,
        head_sha: str,
        fetcher: Callable[[], dict[str, Any]],
    ) -> CacheResult[dict[str, Any]]:
        cache_path = self.root / "compare" / f"{_safe_key(base_sha)}...{_safe_key(head_sha)}.json"
        hit, payload = self._read_json(cache_path)
        if hit:
            self._record("GitHub 缓存命中", "compare", repo=repo, path=cache_path)
            return CacheResult(payload, "cache", cache_path)

        self._record("GitHub 缓存未命中", "compare", repo=repo, path=cache_path)
        payload = fetcher()
        if self._write_json(cache_path, payload):
            self._record("GitHub 缓存写入", "compare", repo=repo, path=cache_path)
        else:
            self._record("GitHub 缓存写入失败", "compare", repo=repo, path=cache_path)
        return CacheResult(payload, "github", cache_path)

    def get_diff(
        self,
        repo: str,
        base_sha: str,
        head_sha: str,
        fetcher: Callable[[], str],
    ) -> CacheResult[str]:
        cache_path = self.root / "diff" / f"{_safe_key(base_sha)}...{_safe_key(head_sha)}.diff"
        hit, text = self._read_text(cache_path)
        if hit:
            self._record("GitHub 缓存命中", "diff", repo=repo, path=cache_path, chars=len(text))
            return CacheResult(text, "cache", cache_path)

        self._record("GitHub 缓存未命中", "diff", repo=repo, path=cache_path)
        text = fetcher()
        if self._write_text(cache_path, text):
            self._record("GitHub 缓存写入", "diff", repo=repo, path=cache_path, chars=len(text))
        else:
            self._record("GitHub 缓存写入失败", "diff", repo=repo, path=cache_path, chars=len(text))
        return CacheResult(text, "github", cache_path)

    def get_file_text(
        self,
        repo: str,
        ref: str,
        path: str,
        fetcher: Callable[[], str],
    ) -> CacheResult[str]:
        cache_path, metadata_path = self._file_paths(repo, ref, path)
        hit, text = self._read_text(cache_path)
        if hit:
            self._record(
                "GitHub 缓存命中",
                "file",
                repo=repo,
                ref=ref,
                target_path=path,
                path=cache_path,
                chars=len(text),
            )
            return CacheResult(text, "cache", cache_path)

        self._record("GitHub 缓存未命中", "file", repo=repo, ref=ref, target_path=path, path=cache_path)
        text = fetcher()
        content_saved = self._write_text(cache_path, text)
        metadata_saved = self._write_json(
            metadata_path,
            {
                "repo": repo,
                "ref": ref,
                "path": path,
                "content_file": cache_path.name,
                "cached_at": time.time(),
            },
        )
        self._record(
            "GitHub 缓存写入" if content_saved and metadata_saved else "GitHub 缓存写入失败",
            "file",
            repo=repo,
            ref=ref,
            target_path=path,
            path=cache_path,
            chars=len(text),
        )
        return CacheResult(text, "github", cache_path)

    def _file_paths(self, repo: str, ref: str, path: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(f"{repo}\0{ref}\0{path}".encode("utf-8")).hexdigest()
        directory = self.root / "files" / _safe_key(ref)
        return directory / f"{digest}.txt", directory / f"{digest}.json"

    def _read_json(self, path: Path) -> tuple[bool, dict[str, Any]]:
        if not path.exists():
            return False, {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _logger.warning("[%s] GitHub 缓存读取失败: %s (%s)", PLUGIN_NAME, path, exc)
            return False, {}
        if not isinstance(payload, dict):
            _logger.warning("[%s] GitHub 缓存内容不是 JSON object: %s", PLUGIN_NAME, path)
            return False, {}
        return True, payload

    def _read_text(self, path: Path) -> tuple[bool, str]:
        if not path.exists():
            return False, ""
        try:
            return True, path.read_text(encoding="utf-8")
        except Exception as exc:
            _logger.warning("[%s] GitHub 缓存读取失败: %s (%s)", PLUGIN_NAME, path, exc)
            return False, ""

    def _write_json(self, path: Path, payload: dict[str, Any]) -> bool:
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as exc:
            _logger.warning("[%s] GitHub 缓存 JSON 序列化失败: %s (%s)", PLUGIN_NAME, path, exc)
            return False
        return self._write_text(path, text)

    def _write_text(self, path: Path, text: str) -> bool:
        tmp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
            tmp_path.write_text(str(text or ""), encoding="utf-8")
            tmp_path.replace(path)
            return True
        except Exception as exc:
            _logger.warning("[%s] GitHub 缓存写入失败: %s (%s)", PLUGIN_NAME, path, exc)
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            return False

    def _record(
        self,
        event: str,
        cache_type: str,
        *,
        repo: str,
        path: Path,
        ref: str = "",
        target_path: str = "",
        chars: int | None = None,
    ) -> None:
        _logger.warning("[%s] %s: type=%s path=%s", PLUGIN_NAME, event, cache_type, path)
        recorder = self.task_log_recorder
        if not callable(recorder):
            return
        payload: dict[str, Any] = {
            "类型": cache_type,
            "仓库": repo,
            "缓存路径": str(path),
        }
        if ref:
            payload["ref"] = ref
        if target_path:
            payload["目标路径"] = target_path
        if chars is not None:
            payload["字符数"] = chars
        try:
            recorder(event, payload)
        except Exception as exc:
            _logger.warning("[%s] 写入 GitHub 缓存任务日志失败: %s", PLUGIN_NAME, exc)


def _safe_key(value: object) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("._-")
    if safe and len(safe) <= 120:
        return safe
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest
