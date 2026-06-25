from __future__ import annotations

import unittest

from wtup.github_client import GitHubClient, GitHubRequestError
from wtup.termination import TaskTerminatedError


class FailingGitHubClient(GitHubClient):
    def __init__(self, errors: list[GitHubRequestError], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.errors = list(errors)
        self.calls = 0

    def _do_request_json(self, url, headers):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"ok": True}


class RecordingGitHubClient(GitHubClient):
    def __init__(self, payload, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.payload = payload
        self.paths: list[str] = []

    def _request_json(self, path):
        self.paths.append(path)
        return self.payload


class GitHubClientRetryTest(unittest.TestCase):
    def test_default_retry_delay_is_minutes_with_four_retries(self) -> None:
        client = GitHubClient()

        self.assertEqual(client.max_retries, 4)
        self.assertEqual([client._retry_delay_seconds(attempt) for attempt in range(1, 5)], [60, 120, 240, 480])

    def test_retryable_errors_are_retried_with_exponential_delay(self) -> None:
        slept: list[float] = []
        client = FailingGitHubClient(
            [GitHubRequestError(500, "server"), GitHubRequestError(message="network")],
            max_retries=4,
            retry_base_delay_seconds=1,
            sleep_func=slept.append,
        )

        payload = client._request_json_with_retry("https://example.invalid", {})

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(client.calls, 3)
        self.assertEqual(slept, [1.0, 1.0, 1.0])

    def test_non_retryable_status_fails_without_retry(self) -> None:
        slept: list[float] = []
        client = FailingGitHubClient(
            [GitHubRequestError(404, "not found")],
            max_retries=4,
            retry_base_delay_seconds=1,
            sleep_func=slept.append,
        )

        with self.assertRaises(GitHubRequestError):
            client._request_json_with_retry("https://example.invalid", {})

        self.assertEqual(client.calls, 1)
        self.assertEqual(slept, [])

    def test_retry_wait_can_be_terminated(self) -> None:
        client = FailingGitHubClient(
            [GitHubRequestError(500, "server")],
            max_retries=4,
            retry_base_delay_seconds=60,
            should_terminate=lambda: True,
            sleep_func=lambda _seconds: None,
        )

        with self.assertRaises(TaskTerminatedError):
            client._request_json_with_retry("https://example.invalid", {})

        self.assertEqual(client.calls, 1)

    def test_get_recent_commits_requests_limited_branch_commits(self) -> None:
        client = RecordingGitHubClient(
            [
                {
                    "sha": "head",
                    "html_url": "https://example.invalid/head",
                    "commit": {"message": "head", "author": {"name": "author", "date": "date"}},
                    "parents": [{"sha": "base"}],
                }
            ]
        )

        commits = client.get_recent_commits("owner/repo", "master", 2)

        self.assertEqual(client.paths, ["/repos/owner/repo/commits?sha=master&per_page=2"])
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].sha, "head")
        self.assertEqual(commits[0].parents, ["base"])
