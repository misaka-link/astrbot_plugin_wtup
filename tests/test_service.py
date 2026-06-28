from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from wtup.config import load_config
from wtup.analysis import ChunkAnalysis, TokenUsage
from wtup.renderer import ReportRenderResult
from wtup.service import UpdateCheckService
from wtup.state_store import StateStore


def render_output_result(image_path=None, message_text="", backend="text", requested_mode="t2i", notices=None, errors=None):
    return ReportRenderResult(
        image_path=image_path,
        message_text=message_text,
        backend=backend,
        requested_mode=requested_mode,
        notices=list(notices or []),
        errors=dict(errors or {}),
    )


async def render_output_text_result(plugin, html_text, output_dir, *, render_mode, fallback_text, enable_fallback_text=False):
    return render_output_result(message_text=fallback_text, backend="text", requested_mode=render_mode)


async def render_output_failed_without_text(plugin, html_text, output_dir, *, render_mode, fallback_text, enable_fallback_text=False):
    return render_output_result(
        message_text="t2i 渲染失败，正在尝试 Playwright...\n图片渲染失败，未发送文字报告。",
        backend="",
        requested_mode=render_mode,
        notices=["t2i 渲染失败，正在尝试 Playwright...", "图片渲染失败，未发送文字报告。"],
    )


class UpdateCheckServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_check_once_rejects_new_task_when_task_lock_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            service = UpdateCheckService(
                context=object(),
                settings=load_config({"enable_task_lock": "开启"}),
                state_store=StateStore(base / "state.json"),
                image_dir=base / "images",
                log_dir=base / "logs",
                error_dir=base / "errors",
                template_path=base / "help_miku.html",
            )

            await service._check_lock.acquire()
            try:
                result = await service.check_once(manual=True, force_latest=False, send_to_groups=False)
            finally:
                service._check_lock.release()

            self.assertTrue(result["locked"])
            self.assertIn("任务锁已开启", result["message"])

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
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)) as render_report_output,
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                await service.check_once(manual=True, force_latest=False, send_to_groups=False)

            self.assertIs(render_report_output.await_args.args[0], render_host)

    async def test_check_once_reuses_cached_compare_and_diff(self) -> None:
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
                settings=load_config({}),
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

            with (
                patch("wtup.service.GitHubClient") as client_class,
                patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[])),
                patch("wtup.service.merge_chunk_analyses", return_value={"summary": "摘要", "update_sections": []}),
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                await service.check_once(manual=True, force_latest=False, send_to_groups=False)
                await service.check_once(manual=True, force_latest=False, send_to_groups=False)

            self.assertEqual(client.get_latest_commit.call_count, 2)
            self.assertEqual(client.compare_commits.call_count, 1)
            self.assertEqual(client.compare_diff_text.call_count, 1)
            self.assertTrue((base / "github_cache" / "compare" / "base...head.json").exists())
            self.assertTrue((base / "github_cache" / "diff" / "base...head.diff").exists())

    async def test_check_once_reuses_analysis_cache_for_same_config_and_range(self) -> None:
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
                settings=load_config({"provider_id": "main-model"}),
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
                {"summary": "摘要", "update_sections": []},
                token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

            with (
                patch("wtup.service.GitHubClient") as client_class,
                patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[chunk_result])) as analyze_chunks,
                patch("wtup.service.merge_chunk_analyses", return_value=chunk_result.analysis) as merge_chunk_analyses,
                patch("wtup.service.render_report_output", new=AsyncMock(return_value=render_output_result(image_path=base / "images" / "report.png", backend="t2i"))),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                first = await service.check_once(manual=True, force_latest=False, send_to_groups=False)
                second = await service.check_once(manual=True, force_latest=False, send_to_groups=False)

            self.assertEqual(first["analysis_source"], "model")
            self.assertEqual(second["analysis_source"], "cache")
            self.assertIn("模型请求 0 次", second["message"])
            self.assertEqual(analyze_chunks.await_count, 1)
            self.assertEqual(merge_chunk_analyses.call_count, 1)
            self.assertEqual(len(list((base / "analysis_cache").glob("base...head_*"))), 1)
            self.assertEqual(len(list((base / "logs").glob("base...head_*/*.log"))), 2)

    async def test_check_once_analysis_cache_key_changes_with_config(self) -> None:
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
                {"summary": "摘要", "update_sections": []},
                token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

            with (
                patch("wtup.service.GitHubClient") as client_class,
                patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[chunk_result])) as analyze_chunks,
                patch("wtup.service.merge_chunk_analyses", return_value=chunk_result.analysis),
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                first_service = UpdateCheckService(
                    context=object(),
                    settings=load_config({"provider_id": "main-model"}),
                    state_store=state_store,
                    image_dir=base / "images",
                    log_dir=base / "logs",
                    error_dir=base / "errors",
                    template_path=template_path,
                )
                second_service = UpdateCheckService(
                    context=object(),
                    settings=load_config({"provider_id": "other-model"}),
                    state_store=state_store,
                    image_dir=base / "images",
                    log_dir=base / "logs",
                    error_dir=base / "errors",
                    template_path=template_path,
                )

                first = await first_service.check_once(manual=True, force_latest=False, send_to_groups=False)
                second = await second_service.check_once(manual=True, force_latest=False, send_to_groups=False)

            self.assertEqual(first["analysis_source"], "model")
            self.assertEqual(second["analysis_source"], "model")
            self.assertEqual(analyze_chunks.await_count, 2)
            self.assertEqual(len(list((base / "analysis_cache").glob("base...head_*"))), 2)

    async def test_force_commit_count_uses_oldest_forced_parent_as_base(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            template_path = base / "help_miku.html"
            template_path.write_text("<style>{{ style_css }}</style>{{ summary_html }}", encoding="utf-8")
            template_path.with_suffix(".css").write_text(".card {}", encoding="utf-8")
            state_store = StateStore(base / "state.json")
            state_store.update_repo_state(
                "gszabi99/War-Thunder-Datamine",
                {"last_commit_sha": "old-state"},
            )
            service = UpdateCheckService(
                context=object(),
                settings=load_config({}),
                state_store=state_store,
                image_dir=base / "images",
                log_dir=base / "logs",
                error_dir=base / "errors",
                template_path=template_path,
            )

            latest = type("Commit", (), {"sha": "head", "parents": ["mid"]})()
            second = type("Commit", (), {"sha": "mid", "parents": ["base"]})()
            compare_payload = {
                "base_commit": {"sha": "base"},
                "merge_base_commit": {"sha": "base"},
                "status": "ahead",
                "ahead_by": 2,
                "total_commits": 2,
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

            with (
                patch("wtup.service.GitHubClient") as client_class,
                patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[])),
                patch("wtup.service.merge_chunk_analyses", return_value={"summary": "摘要", "update_sections": []}),
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.get_recent_commits.return_value = [latest, second]
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                await service.check_once(
                    manual=True,
                    force_latest=True,
                    send_to_groups=False,
                    force_commit_count=2,
                )

            client.get_recent_commits.assert_called_once_with("gszabi99/War-Thunder-Datamine", "master", 2)
            client.compare_commits.assert_called_once_with("gszabi99/War-Thunder-Datamine", "base", "head")
            client.compare_diff_text.assert_called_once_with("gszabi99/War-Thunder-Datamine", "base", "head")

    async def test_list_pending_commits_outputs_short_sha_and_version(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            state_store = StateStore(base / "state.json")
            state_store.update_repo_state(
                "gszabi99/War-Thunder-Datamine",
                {"last_commit_sha": "base"},
            )
            service = UpdateCheckService(
                context=object(),
                settings=load_config({}),
                state_store=state_store,
                image_dir=base / "images",
                log_dir=base / "logs",
                error_dir=base / "errors",
                template_path=base / "help_miku.html",
            )

            latest = type("Commit", (), {"sha": "0e0e9bc1234567890", "parents": ["base"]})()
            compare_payload = {
                "commits": [
                    {
                        "sha": "0e0e9bc1234567890",
                        "commit": {"message": "2.56.0.43"},
                    }
                ]
            }

            with patch("wtup.service.GitHubClient") as client_class:
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload

                result = await service.list_pending_commits()

            self.assertEqual(result["pending_commits"][0]["short_sha"], "0e0e9bc")
            self.assertEqual(result["pending_commits"][0]["version"], "2.56.0.43")
            self.assertEqual(result["message"], "未推送 commit 列表：\n0e0e9bc 2.56.0.43")

    async def test_mark_commit_pushed_updates_seen_commit_when_pending(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            state_store = StateStore(base / "state.json")
            state_store.update_repo_state(
                "gszabi99/War-Thunder-Datamine",
                {"last_commit_sha": "base"},
            )
            service = UpdateCheckService(
                context=object(),
                settings=load_config({}),
                state_store=state_store,
                image_dir=base / "images",
                log_dir=base / "logs",
                error_dir=base / "errors",
                template_path=base / "help_miku.html",
            )

            target_sha = "0e0e9bc1234567890"
            latest = type("Commit", (), {"sha": target_sha, "parents": ["base"]})()
            commit_payload = {"sha": target_sha, "commit": {"message": "2.56.0.43"}}
            compare_payload = {"commits": [commit_payload]}

            with patch("wtup.service.GitHubClient") as client_class:
                client = client_class.return_value
                client.get_commit.return_value = commit_payload
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload

                result = await service.mark_commit_pushed("0e0e9bc")

            repo_state = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")
            self.assertTrue(result["marked"])
            self.assertEqual(repo_state["last_commit_sha"], target_sha)
            self.assertIn("已标记为推送完成: 0e0e9bc 2.56.0.43", result["message"])

    async def test_mark_commit_pushed_rejects_commit_outside_pending_list(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            state_store = StateStore(base / "state.json")
            state_store.update_repo_state(
                "gszabi99/War-Thunder-Datamine",
                {"last_commit_sha": "base"},
            )
            service = UpdateCheckService(
                context=object(),
                settings=load_config({}),
                state_store=state_store,
                image_dir=base / "images",
                log_dir=base / "logs",
                error_dir=base / "errors",
                template_path=base / "help_miku.html",
            )

            target_sha = "0e0e9bc1234567890"
            latest = type("Commit", (), {"sha": "head", "parents": ["base"]})()
            commit_payload = {"sha": target_sha, "commit": {"message": "2.56.0.43"}}

            with patch("wtup.service.GitHubClient") as client_class:
                client = client_class.return_value
                client.get_commit.return_value = commit_payload
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = {"commits": []}

                result = await service.mark_commit_pushed("0e0e9bc")

            repo_state = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")
            self.assertFalse(result["marked"])
            self.assertEqual(repo_state["last_commit_sha"], "base")
            self.assertIn("不在当前未推送列表", result["message"])

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
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
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

    async def test_check_once_generates_and_pushes_pre_summary_report_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            template_path = base / "help_miku.html"
            template_path.write_text(
                "<div>{{ report_kicker }}</div><div>{{ summary_html }}</div>",
                encoding="utf-8",
            )
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
                        "analysis_file_groups": ["123"],
                        "provider_id": "NewAPI-OpenAI/glm-5.1",
                        "summary_provider_id": "NewApi-OpenAI/gemini-3.5-flash-preview",
                        "enable_summary_model": True,
                        "enable_pre_summary_report": True,
                        "enable_push_append_text": True,
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
                    "summary": "分析前摘要",
                    "update_sections": [],
                },
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )
            final_analysis = {
                "report_title": "2.56.0.38->2.56.0.39",
                "summary": "分析后摘要",
                "update_sections": [],
            }

            with (
                patch("wtup.service.GitHubClient") as client_class,
                patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[chunk_result])),
                patch("wtup.service.merge_chunk_analyses", return_value=chunk_result.analysis),
                patch(
                    "wtup.service.refine_merged_analysis_with_usage",
                    new=AsyncMock(
                        return_value=(
                            final_analysis,
                            TokenUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
                        )
                    ),
                ),
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
                patch("wtup.service.push_report", new=AsyncMock(return_value=(1, 0))) as push_report,
                patch("wtup.service.push_text", new=AsyncMock(return_value=(1, 0))) as push_text,
                patch("wtup.service.push_log_file", new=AsyncMock(return_value=(1, 0))) as push_log_file,
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                result = await service.check_once(manual=True, force_latest=False, send_to_groups=True)

            self.assertEqual(push_report.await_count, 2)
            self.assertEqual([report["key"] for report in result["reports"]], ["pre_summary", "final"])
            self.assertIn("分析前摘要", result["reports"][0]["fallback_text"])
            self.assertIn("分析后摘要", result["reports"][1]["fallback_text"])
            pushed_texts = [call.kwargs["fallback_text"] for call in push_report.await_args_list]
            self.assertIn("【总分析模型分析前】", pushed_texts[0])
            self.assertIn("分析前摘要", pushed_texts[0])
            self.assertIn("【总分析模型分析后】", pushed_texts[1])
            self.assertIn("分析后摘要", pushed_texts[1])
            self.assertEqual(push_text.await_count, 2)
            append_texts = [call.kwargs["text"] for call in push_text.await_args_list]
            self.assertEqual(
                append_texts,
                [
                    "消耗token:150\n分析模型:glm-5.1\n总结模型:未启动",
                    "消耗token:200\n分析模型:glm-5.1\n总结模型:gemini-3.5-flash-preview",
                ],
            )
            self.assertEqual(result["reports"][0]["append_text"], append_texts[0])
            self.assertEqual(result["reports"][1]["append_text"], append_texts[1])
            self.assertEqual(result["append_text"], append_texts[1])
            self.assertEqual(push_log_file.await_count, 2)
            log_names = [call.kwargs["log_path"].name for call in push_log_file.await_args_list]
            self.assertEqual(
                log_names,
                [
                    "2.56.0.38_2.56.0.39_总分析前.log",
                    "2.56.0.38_2.56.0.39_总分析后.log",
                ],
            )
            task = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")["last_generated_task"]
            task_log_path = Path(task["log_path"])
            self.assertEqual(task_log_path.name, "2.56.0.38_2.56.0.39_总分析后.log")
            self.assertEqual(task_log_path.parent.parent, base / "logs")
            self.assertTrue(task_log_path.parent.name.startswith("base...head_"))
            self.assertEqual([report["key"] for report in task["reports"]], ["pre_summary", "final"])
            pre_log = Path(task["reports"][0]["log_path"]).read_text(encoding="utf-8")
            post_log = Path(task["reports"][1]["log_path"]).read_text(encoding="utf-8")
            self.assertIn("Token 消耗：总 150 · 输入 100 · 输出 50", pre_log)
            self.assertIn("Token 消耗：总 200 · 输入 130 · 输出 70", post_log)

    async def test_log_cleanup_counts_task_directories_not_dual_report_files(self) -> None:
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
                    "summary": "分析前摘要",
                    "update_sections": [],
                },
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )
            final_analysis = {
                "report_title": "2.56.0.38->2.56.0.39",
                "summary": "分析后摘要",
                "update_sections": [],
            }

            async def run_once(provider_id: str) -> None:
                service = UpdateCheckService(
                    context=object(),
                    settings=load_config(
                        {
                            "provider_id": provider_id,
                            "enable_summary_model": True,
                            "enable_pre_summary_report": True,
                            "max_saved_artifacts": 1,
                        }
                    ),
                    state_store=state_store,
                    image_dir=base / "images",
                    log_dir=base / "logs",
                    error_dir=base / "errors",
                    template_path=template_path,
                )
                with (
                    patch("wtup.service.GitHubClient") as client_class,
                    patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[chunk_result])),
                    patch("wtup.service.merge_chunk_analyses", return_value=chunk_result.analysis),
                    patch(
                        "wtup.service.refine_merged_analysis_with_usage",
                        new=AsyncMock(
                            return_value=(
                                final_analysis,
                                TokenUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
                            )
                        ),
                    ),
                    patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
                ):
                    client = client_class.return_value
                    client.get_latest_commit.return_value = latest
                    client.compare_commits.return_value = compare_payload
                    client.compare_diff_text.return_value = ""

                    await service.check_once(manual=True, force_latest=False, send_to_groups=False)

            await run_once("model-a")
            await run_once("model-b")

            task_dirs = [path for path in (base / "logs").iterdir() if path.is_dir()]
            self.assertEqual(len(task_dirs), 1)
            self.assertEqual(len(list(task_dirs[0].glob("*.log"))), 2)

    async def test_check_once_returns_append_text_for_manual_current_session(self) -> None:
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
                        "provider_id": "NewAPI-OpenAI/glm-5.1",
                        "enable_push_append_text": True,
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
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                result = await service.check_once(manual=True, force_latest=False, send_to_groups=False)

            self.assertEqual(
                result["append_text"],
                "消耗token:150\n分析模型:glm-5.1\n总结模型:未启动",
            )

    async def test_check_once_uses_override_targets_for_current_group_force(self) -> None:
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
                        "target_groups": ["111"],
                        "analysis_file_groups": ["222"],
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
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
                patch("wtup.service.push_report", new=AsyncMock(return_value=(1, 0))) as push_report,
                patch("wtup.service.push_log_file", new=AsyncMock(return_value=(1, 0))) as push_log_file,
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                await service.check_once(
                    manual=True,
                    force_latest=True,
                    send_to_groups=True,
                    target_groups=["999"],
                    analysis_file_groups=["999"],
                )

            self.assertEqual(push_report.await_args.args[1], ["999"])
            self.assertEqual(push_log_file.await_args.args[1], ["999"])
            task = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")["last_generated_task"]
            self.assertEqual(task["target_groups"], ["999"])

    async def test_group_report_success_marks_commit_seen(self) -> None:
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
                settings=load_config({"target_groups": ["123"]}),
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
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
                patch("wtup.service.push_report", new=AsyncMock(return_value=(1, 0))),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                result = await service.check_once(manual=False, force_latest=False, send_to_groups=True)

            repo_state = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")
            self.assertTrue(result["commit_marked_complete"])
            self.assertEqual(result["report_sent_count"], 1)
            self.assertEqual(repo_state["last_commit_sha"], "head")
            self.assertIn("last_pushed_task", repo_state)

    async def test_image_failure_without_text_fallback_does_not_mark_commit_seen(self) -> None:
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
                settings=load_config({"target_groups": ["123"]}),
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
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_failed_without_text)),
                patch("wtup.service.push_report", new=AsyncMock(return_value=(1, 0))),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                result = await service.check_once(manual=False, force_latest=False, send_to_groups=True)

            repo_state = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")
            self.assertFalse(result["commit_marked_complete"])
            self.assertTrue(result["render_failed"])
            self.assertEqual(repo_state["last_commit_sha"], "base")

    async def test_group_report_failure_still_marks_commit_seen_after_report_generated(self) -> None:
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
                        "enable_push_append_text": True,
                        "push_append_text_template": "消耗token:{token_count}",
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
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
                patch("wtup.service.push_report", new=AsyncMock(return_value=(0, 1))),
                patch("wtup.service.push_text", new=AsyncMock(return_value=(1, 0))),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                result = await service.check_once(manual=False, force_latest=False, send_to_groups=True)

            repo_state = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")
            self.assertTrue(result["commit_marked_complete"])
            self.assertEqual(result["report_sent_count"], 0)
            self.assertEqual(result["report_failed_count"], 1)
            self.assertEqual(repo_state["last_commit_sha"], "head")
            self.assertIn("last_pushed_task", repo_state)
            self.assertNotIn("未标记本次更新完成", result["message"])

    async def test_group_log_file_success_marks_commit_seen_when_report_image_fails(self) -> None:
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
                        "analysis_file_groups": ["123"],
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
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
                patch("wtup.service.push_report", new=AsyncMock(return_value=(0, 1))),
                patch("wtup.service.push_log_file", new=AsyncMock(return_value=(1, 0))),
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                result = await service.check_once(manual=False, force_latest=False, send_to_groups=True)

            repo_state = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")
            self.assertTrue(result["commit_marked_complete"])
            self.assertEqual(result["report_sent_count"], 0)
            self.assertEqual(result["report_failed_count"], 1)
            self.assertEqual(result["file_sent_count"], 1)
            self.assertEqual(result["file_failed_count"], 0)
            self.assertEqual(repo_state["last_commit_sha"], "head")
            self.assertIn("last_pushed_task", repo_state)
            self.assertNotIn("未标记本次更新完成", result["message"])

    async def test_analysis_failure_skips_group_push_and_notifies_admins(self) -> None:
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
                        "analysis_file_groups": ["123"],
                        "admin_targets": ["456"],
                        "enable_push_append_text": True,
                        "push_append_text_template": "消耗token:{token_count}",
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
                    "summary": "模型分析失败，相关文件需要结合 GitHub 原始 diff 复核。",
                    "update_sections": [],
                    "tags": ["需复核"],
                },
                error="模型请求失败",
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

            with (
                patch("wtup.service.GitHubClient") as client_class,
                patch("wtup.service.analyze_chunks", new=AsyncMock(return_value=[chunk_result])),
                patch("wtup.service.merge_chunk_analyses", return_value=chunk_result.analysis),
                patch("wtup.service.render_report_output", new=AsyncMock(side_effect=render_output_text_result)),
                patch("wtup.service.push_report", new=AsyncMock(return_value=(1, 0))) as push_report,
                patch("wtup.service.push_text", new=AsyncMock(return_value=(1, 0))) as push_text,
                patch("wtup.service.push_log_file", new=AsyncMock(return_value=(1, 0))) as push_log_file,
                patch("wtup.service.push_admin_notification", new=AsyncMock(return_value=(1, 0))) as push_admin_notification,
            ):
                client = client_class.return_value
                client.get_latest_commit.return_value = latest
                client.compare_commits.return_value = compare_payload
                client.compare_diff_text.return_value = ""

                result = await service.check_once(manual=False, force_latest=False, send_to_groups=True)

            repo_state = state_store.get_repo_state("gszabi99/War-Thunder-Datamine")
            self.assertTrue(result["analysis_failed"])
            self.assertEqual(result["admin_sent_count"], 1)
            self.assertEqual(result["report_sent_count"], 0)
            self.assertEqual(repo_state["last_commit_sha"], "base")
            self.assertNotIn("last_pushed_task", repo_state)
            push_report.assert_not_awaited()
            push_text.assert_not_awaited()
            push_log_file.assert_not_awaited()
            push_admin_notification.assert_awaited_once()
            self.assertEqual(push_admin_notification.await_args.args[1], ["456"])
            self.assertIn("WT 更新分析失败", push_admin_notification.await_args.kwargs["text"])
            self.assertIn("模型请求失败", push_admin_notification.await_args.kwargs["text"])
            self.assertIn("分析失败，已跳过群推送", result["message"])


if __name__ == "__main__":
    unittest.main()
