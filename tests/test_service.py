from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from wtup.config import load_config
from wtup.analysis import ChunkAnalysis, TokenUsage
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

    async def test_check_once_uses_real_token_usage_for_append_text(self) -> None:
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
            service = UpdateCheckService(
                context=object(),
                settings=load_config(
                    {
                        "target_groups": ["123"],
                        "provider_id": "NewAPI-OpenAI/glm-5.1",
                        "summary_provider_id": "NewApi-OpenAI/gemini-3.5-flash-preview",
                        "enable_push_append_text": True,
                        "enable_summary_model": True,
                        "push_append_text_template": "消耗token:{token_count}\n分析模型:{analysis_model_name}\n总结模型:{summary_model_name}",
                    }
                ),
                state_store=state_store,
                image_dir=base / "images",
                log_dir=base / "logs",
                error_dir=base / "errors",
                template_path=template_path,
            )

            latest = type("Commit", (), {"sha": "head", "parents": ["base"]})()
            compare_payload = {
                "base_commit": {"sha": "base"},
                "merge_base_commit": {"sha": "base"},
                "status": "ahead",
                "ahead_by": 1,
                "total_commits": 1,
                "commits": [],
                "files": [
                    {
                        "filename": "a.blkx",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "patch": "+a",
                    }
                ],
                "html_url": "",
            }
            chunk_result = ChunkAnalysis(
                1,
                1,
                {
                    "report_title": "2.56.0.38->2.56.0.39",
                    "summary": "摘要",
                    "update_sections": [],
                },
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

            with (
                patch("wtup.service.GitHubClient") as client_class,
                patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[chunk_result])),
                patch("wtup.service.merge_chunk_analyses", return_value=chunk_result.analysis),
                patch("wtup.service.refine_merged_analysis_with_usage", new=AsyncMock(return_value=(chunk_result.analysis, TokenUsage()))),
                patch("wtup.service.render_report_image", new=AsyncMock(return_value=None)),
                patch("wtup.service.push_report", new=AsyncMock(return_value=(1, 0))),
                patch("wtup.service.push_text", new=AsyncMock(return_value=(1, 0))) as push_text,
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                await service.check_once(manual=True, force_latest=False, send_to_groups=True)

            self.assertEqual(
                push_text.await_args.kwargs["text"],
                "消耗token:150\n分析模型:glm-5.1\n总结模型:gemini-3.5-flash-preview",
            )
            task = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")["last_generated_task"]
            self.assertEqual(task["token_usage"]["total_tokens"], 150)


if __name__ == "__main__":
    unittest.main()
