from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import PLUGIN_NAME, PLUGIN_VERSION


_logger = logging.getLogger(PLUGIN_NAME)


GITHUB_API_BASE = "https://api.github.com"


RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class GitHubRequestError(RuntimeError):
    def __init__(self, status_code: int | None = None, message: str = ""):
        self.status_code = status_code
        self.message = str(message or "").strip()
        super().__init__(f"GitHub request failed: {status_code or 'unknown'} {self.message}")


@dataclass(frozen=True)
class CommitRef:
    sha: str
    html_url: str
    message: str
    author_name: str
    authored_at: str
    parents: list[str]


class GitHubClient:
    def __init__(self, token: str = "", timeout: int = 30, max_retries: int = 3):
        self.token = str(token or "").strip()
        self.timeout = timeout
        self.max_retries = max(0, max_retries)

    def get_latest_commit(self, repo: str, branch: str) -> CommitRef:
        payload = self._request_json(f"/repos/{repo}/commits/{urllib.parse.quote(branch, safe='')}")
        return self._commit_ref_from_payload(payload)

    def get_commit(self, repo: str, sha: str) -> dict[str, Any]:
        return self._request_json(f"/repos/{repo}/commits/{urllib.parse.quote(sha, safe='')}")

    def compare_commits(self, repo: str, base_sha: str, head_sha: str) -> dict[str, Any]:
        base = urllib.parse.quote(base_sha, safe="")
        head = urllib.parse.quote(head_sha, safe="")
        return self._request_json(f"/repos/{repo}/compare/{base}...{head}")

    def compare_diff_text(self, repo: str, base_sha: str, head_sha: str) -> str:
        base = urllib.parse.quote(base_sha, safe="")
        head = urllib.parse.quote(head_sha, safe="")
        url = f"https://github.com/{repo}/compare/{base}...{head}.diff"
        return self._request_text(url, accept="text/plain")

    def get_file_text(self, repo: str, ref: str, path: str) -> str:
        safe_path = urllib.parse.quote(str(path or "").strip().lstrip("/"), safe="/")
        safe_ref = urllib.parse.quote(str(ref or "").strip(), safe="")
        url = f"{GITHUB_API_BASE}/repos/{repo}/contents/{safe_path}?ref={safe_ref}"
        _logger.warning("[%s] API 请求: /repos/%s/contents/%s?ref=%s", PLUGIN_NAME, repo, safe_path, safe_ref)
        text = self._request_text(url, accept="application/vnd.github.raw")
        _logger.warning("[%s] API 请求完成: /repos/%s/contents/%s?ref=%s", PLUGIN_NAME, repo, safe_path, safe_ref)
        return text

    def _request_json(self, path: str) -> dict[str, Any]:
        url = f"{GITHUB_API_BASE}{path}"
        _logger.warning("[%s] API 请求: %s", PLUGIN_NAME, path)
        result = self._request_json_with_retry(url, self._api_headers())
        _logger.warning("[%s] API 请求完成: %s", PLUGIN_NAME, path)
        return result

    def _request_json_with_retry(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        last_error: GitHubRequestError | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                wait = 2 ** (attempt - 1)
                _logger.warning(
                    "[%s] 请求失败 (status=%s)，%d 秒后重试 (第 %d/%d 次)...",
                    PLUGIN_NAME, last_error.status_code if last_error else "network", wait, attempt, self.max_retries,
                )
                time.sleep(wait)
            try:
                return self._do_request_json(url, headers)
            except GitHubRequestError as exc:
                if exc.status_code is not None and exc.status_code not in RETRYABLE_STATUSES:
                    raise
                last_error = exc
        raise last_error or GitHubRequestError(message="retry exhausted")

    def _do_request_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                text = response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GitHubRequestError(exc.code, self._extract_error_message(body)) from exc
        except urllib.error.URLError as exc:
            raise GitHubRequestError(message=str(exc.reason or exc)) from exc

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitHubRequestError(message="GitHub returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubRequestError(message="GitHub returned unexpected payload")
        return payload

    def _request_text(self, raw_url: str, *, accept: str) -> str:
        headers = {
            "Accept": accept,
            "User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return self._request_text_with_retry(raw_url, headers)

    def _request_text_with_retry(self, url: str, headers: dict[str, str]) -> str:
        last_error: GitHubRequestError | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                wait = 2 ** (attempt - 1)
                _logger.warning(
                    "[%s] 请求失败 (status=%s)，%d 秒后重试 (第 %d/%d 次)...",
                    PLUGIN_NAME, last_error.status_code if last_error else "network", wait, attempt, self.max_retries,
                )
                time.sleep(wait)
            try:
                return self._do_request_text(url, headers)
            except GitHubRequestError as exc:
                if exc.status_code is not None and exc.status_code not in RETRYABLE_STATUSES:
                    raise
                last_error = exc
        raise last_error or GitHubRequestError(message="retry exhausted")

    def _do_request_text(self, url: str, headers: dict[str, str]) -> str:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GitHubRequestError(exc.code, self._extract_error_message(body)) from exc
        except urllib.error.URLError as exc:
            raise GitHubRequestError(message=str(exc.reason or exc)) from exc

    def _api_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _extract_error_message(body: str) -> str:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body[:300]
        if isinstance(payload, dict):
            return str(payload.get("message") or payload)[:300]
        return str(payload)[:300]

    @staticmethod
    def _commit_ref_from_payload(payload: dict[str, Any]) -> CommitRef:
        commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        parents = payload.get("parents") if isinstance(payload.get("parents"), list) else []
        return CommitRef(
            sha=str(payload.get("sha") or ""),
            html_url=str(payload.get("html_url") or ""),
            message=str(commit.get("message") or ""),
            author_name=str(author.get("name") or ""),
            authored_at=str(author.get("date") or ""),
            parents=[str(item.get("sha")) for item in parents if isinstance(item, dict) and item.get("sha")],
        )
