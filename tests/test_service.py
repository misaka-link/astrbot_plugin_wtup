from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from wtup.config import load_config
from wtup.service import UpdateCheckService
from wtup.state_store import StateStore


class UpdateCheckServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_check_once_uses_render_host_for_html_render(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            template_path = base / "help_miku.html"
            template_path.write_text("<style>{{ style_css }}</style>{{ summary_html }}", encoding="utf-8")
            template_path.with_suffix(".css").write_text(".card {}", encoding="utf-8")
            state_store = StateStore(base / "state.json")
            state_store.update_repo_state(
                "gszabi99/War-Thunder-Datamine",
                {"last_commit_sha": "base"},
            )
            render_host = object()
            service = UpdateCheckService(
                context=object(),
                settings=load_config({}),
                state_store=state_store,
                image_dir=base / "images",
                log_dir=base / "logs",
                error_dir=base / "errors",
                template_path=template_path,
                render_host=render_host,
            )

            latest = type("Commit", (), {"sha": "head", "parents": ["base"]})()
            compare_payload = {
                "base_commit": {"sha": "base"},
                "merge_base_commit": {"sha": "base"},
                "status": "ahead",
                "ahead_by": 1,
                "total_commits": 1,
                "commits": [],
                "files": [],
                "html_url": "",
            }

            with (
                patch("wtup.service.GitHubClient") as client_class,
                patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[])),
                patch("wtup.service.merge_chunk_analyses", return_value={"summary": "摘要", "update_sections": []}),
                patch("wtup.service.render_report_image", new=AsyncMock(return_value=None)) as render_report_image,
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                await service.check_once(manual=True, force_latest=False, send_to_groups=False)

            self.assertIs(render_report_image.await_args.args[0], render_host)


if __name__ == "__main__":
    unittest.main()
