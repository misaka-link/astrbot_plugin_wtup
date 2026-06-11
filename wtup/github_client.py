from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import PLUGIN_NAME, PLUGIN_VERSION


GITHUB_API_BASE = "https://api.github.com"


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
    def __init__(self, token: str = "", timeout: int = 30):
        self.token = str(token or "").strip()
        self.timeout = timeout

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

    def _request_json(self, path: str) -> dict[str, Any]:
        url = f"{GITHUB_API_BASE}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

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

    def _request_text(self, url: str, *, accept: str) -> str:
        headers = {
            "Accept": accept,
            "User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

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
