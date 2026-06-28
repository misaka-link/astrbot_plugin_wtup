from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from wtup.diff_collector import DiffChunk, DiffSummary
from wtup.analysis import TokenUsage
from wtup.renderer import (
    build_report_html,
    render_footer_note,
    render_plain_text,
    render_report_output,
    render_watermark,
    report_display_title,
)


class RendererFooterNoteTest(unittest.TestCase):
    def test_report_display_title_completes_partial_version_range(self) -> None:
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="",
            total_commits=1,
            total_files=0,
            additions=0,
            deletions=0,
            changed_files=0,
            commits=[],
            files=[],
            chunks=[],
            base_version="2.56.0.42",
            head_version="2.56.0.43",
        )
        chunk = DiffChunk(index=1, total=1, files=[], patch_chars=0)

        self.assertEqual(
            report_display_title(summary, chunk, {"report_title": "-> 2.56.0.43"}),
            "2.56.0.42->2.56.0.43",
        )

    def test_footer_note_renders_markdown_link_and_lines(self) -> None:
        self.assertEqual(
            render_footer_note("[repo](https://github.com/example/repo)\n第二行"),
            '<a href="https://github.com/example/repo">repo</a><br>第二行',
        )

    def test_footer_note_escapes_plain_text(self) -> None:
        self.assertEqual(render_footer_note("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")

    def test_render_watermark_skips_empty_text(self) -> None:
        self.assertEqual(render_watermark(""), "")

    def test_render_watermark_escapes_text_and_applies_options(self) -> None:
        watermark_html = render_watermark("<测试>", opacity_percent=35, density="high")

        self.assertIn("&lt;测试&gt;", watermark_html)
        self.assertIn("opacity: 0.35", watermark_html)
        self.assertIn("watermark-density-high", watermark_html)
        self.assertNotIn("<测试>", watermark_html)

    def test_build_report_html_injects_watermark(self) -> None:
        with TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "report.html"
            template_path.write_text(
                "<html><head><style>{{ style_css }}</style></head><body>{{ watermark_html }}</body></html>",
                encoding="utf-8",
            )
            template_path.with_suffix(".css").write_text("", encoding="utf-8")
            summary = DiffSummary(
                base_sha="base123",
                head_sha="head456",
                compare_url="",
                total_commits=1,
                total_files=0,
                additions=0,
                deletions=0,
                changed_files=0,
                commits=[],
                files=[],
                chunks=[],
            )
            chunk = DiffChunk(index=1, total=1, files=[], patch_chars=0)

            html = build_report_html(
                template_path,
                summary,
                chunk,
                {"summary": "摘要"},
                watermark_text="测试水印",
                watermark_opacity_percent=20,
                watermark_density="low",
            )

            self.assertIn("测试水印", html)
            self.assertIn("opacity: 0.20", html)
            self.assertIn("watermark-density-low", html)

    def test_build_report_html_injects_split_css_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "report.html"
            template_path.write_text(
                "<html><head><style>{{ style_css }}</style></head><body>{{ report_title }}</body></html>",
                encoding="utf-8",
            )
            template_path.with_suffix(".css").write_text(".card { color: red; }\n", encoding="utf-8")
            summary = DiffSummary(
                base_sha="base123",
                head_sha="head456",
                compare_url="",
                total_commits=1,
                total_files=0,
                additions=0,
                deletions=0,
                changed_files=0,
                commits=[],
                files=[],
                chunks=[],
            )
            chunk = DiffChunk(index=1, total=1, files=[], patch_chars=0)

            html = build_report_html(template_path, summary, chunk, {"summary": "摘要"})

            self.assertIn(".card { color: red; }", html)
            self.assertNotIn("{{ style_css }}", html)

    def test_build_report_html_injects_token_usage(self) -> None:
        with TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "report.html"
            template_path.write_text(
                "<footer>{{ token_usage }}</footer>",
                encoding="utf-8",
            )
            template_path.with_suffix(".css").write_text("", encoding="utf-8")
            summary = DiffSummary(
                base_sha="base123",
                head_sha="head456",
                compare_url="",
                total_commits=1,
                total_files=0,
                additions=0,
                deletions=0,
                changed_files=0,
                commits=[],
                files=[],
                chunks=[],
            )
            chunk = DiffChunk(index=1, total=1, files=[], patch_chars=0)

            html = build_report_html(
                template_path,
                summary,
                chunk,
                {"summary": "摘要"},
                token_usage=TokenUsage(prompt_tokens=12, completion_tokens=8, total_tokens=20),
            )

            self.assertIn("Token 消耗：总 20 · 输入 12 · 输出 8", html)

    def test_render_plain_text_does_not_include_source_url(self) -> None:
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="https://github.com/example/repo/compare/base...head",
            total_commits=1,
            total_files=0,
            additions=0,
            deletions=0,
            changed_files=0,
            commits=[],
            files=[],
            chunks=[],
        )
        chunk = DiffChunk(index=1, total=1, files=[], patch_chars=0)

        text = render_plain_text(summary, chunk, {"summary": "摘要", "update_sections": []})

        self.assertNotIn("Source:", text)
        self.assertNotIn("https://github.com/example/repo/compare/base...head", text)

    def test_bulk_repeat_content_renders_before_ai_analysis(self) -> None:
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="",
            total_commits=1,
            total_files=0,
            additions=0,
            deletions=0,
            changed_files=0,
            commits=[],
            files=[],
            chunks=[],
        )
        chunk = DiffChunk(index=1, total=1, files=[], patch_chars=0)
        analysis = {
            "summary": "摘要",
            "update_sections": [{"title": "参数调整", "items": [{"text": "主条目", "children": []}]}],
            "bulk_repeat_content": {
                "batch": [{"text": "批量修改", "children": []}],
                "repeated": [],
                "needs_verification": [{"text": "需验证内容", "children": []}],
            },
            "ai_analysis": {
                "changed_content": ["AI 改动"],
                "player_impact": [],
                "uncertainties": [],
                "recommendation": "",
            },
        }

        text = render_plain_text(summary, chunk, analysis)

        self.assertLess(text.index("批量重复内容:"), text.index("AI 分析:"))
        self.assertIn("批量修改", text)

        with TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "report.html"
            template_path.write_text(
                "<main>{{ summary_html }}{{ article_html }}</main>",
                encoding="utf-8",
            )
            template_path.with_suffix(".css").write_text("", encoding="utf-8")

            html = build_report_html(template_path, summary, chunk, analysis)

        self.assertLess(html.index("批量重复内容"), html.index("AI 分析"))
        self.assertIn("需验证内容", html)


class ReportRenderOutputTest(unittest.IsolatedAsyncioTestCase):
    async def test_t2i_mode_falls_back_to_playwright(self) -> None:
        with TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "report.png"
            with (
                patch("wtup.renderer.render_report_image_t2i", new=AsyncMock(return_value=None)) as t2i,
                patch("wtup.renderer.render_report_image_playwright", new=AsyncMock(return_value=image_path)) as playwright,
            ):
                result = await render_report_output(
                    object(),
                    "<html></html>",
                    Path(temp_dir),
                    render_mode="t2i",
                    fallback_text="纯文字报告",
                )

            self.assertEqual(result.image_path, image_path)
            self.assertEqual(result.backend, "playwright")
            self.assertEqual(result.fallback_notice, "t2i 渲染失败，正在尝试 Playwright...")
            self.assertEqual(t2i.await_count, 1)
            self.assertEqual(playwright.await_count, 1)

    async def test_playwright_mode_falls_back_to_t2i(self) -> None:
        with TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "report.png"
            with (
                patch("wtup.renderer.render_report_image_playwright", new=AsyncMock(return_value=None)),
                patch("wtup.renderer.render_report_image_t2i", new=AsyncMock(return_value=image_path)),
            ):
                result = await render_report_output(
                    object(),
                    "<html></html>",
                    Path(temp_dir),
                    render_mode="playwright",
                    fallback_text="纯文字报告",
                )

            self.assertEqual(result.image_path, image_path)
            self.assertEqual(result.backend, "t2i")
            self.assertEqual(result.fallback_notice, "Playwright 渲染失败，正在尝试 t2i...")

    async def test_image_failures_do_not_emit_full_text_by_default(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with (
                patch("wtup.renderer.render_report_image_t2i", new=AsyncMock(return_value=None)),
                patch("wtup.renderer.render_report_image_playwright", new=AsyncMock(return_value=None)),
            ):
                result = await render_report_output(
                    object(),
                    "<html></html>",
                    Path(temp_dir),
                    render_mode="t2i",
                    fallback_text="纯文字报告",
                    enable_fallback_text=False,
                )

            self.assertIsNone(result.image_path)
            self.assertNotIn("纯文字报告", result.message_text)
            self.assertIn("图片渲染失败，未发送文字报告。", result.message_text)

    async def test_image_failures_emit_full_text_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with (
                patch("wtup.renderer.render_report_image_t2i", new=AsyncMock(return_value=None)),
                patch("wtup.renderer.render_report_image_playwright", new=AsyncMock(return_value=None)),
            ):
                result = await render_report_output(
                    object(),
                    "<html></html>",
                    Path(temp_dir),
                    render_mode="t2i",
                    fallback_text="纯文字报告",
                    enable_fallback_text=True,
                )

            self.assertIn("图片渲染失败，已自动切换为文字模式。", result.message_text)
            self.assertIn("纯文字报告", result.message_text)

    async def test_text_mode_skips_image_rendering(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with (
                patch("wtup.renderer.render_report_image_t2i", new=AsyncMock()) as t2i,
                patch("wtup.renderer.render_report_image_playwright", new=AsyncMock()) as playwright,
            ):
                result = await render_report_output(
                    object(),
                    "<html></html>",
                    Path(temp_dir),
                    render_mode="text",
                    fallback_text="纯文字报告",
                )

            self.assertEqual(result.backend, "text")
            self.assertEqual(result.message_text, "纯文字报告")
            t2i.assert_not_awaited()
            playwright.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
