from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wtup.diff_collector import DiffChunk, DiffSummary
from wtup.analysis import TokenUsage
from wtup.renderer import build_report_html, render_footer_note, render_plain_text


class RendererFooterNoteTest(unittest.TestCase):
    def test_footer_note_renders_markdown_link_and_lines(self) -> None:
        self.assertEqual(
            render_footer_note("[repo](https://github.com/example/repo)\n第二行"),
            '<a href="https://github.com/example/repo">repo</a><br>第二行',
        )

    def test_footer_note_escapes_plain_text(self) -> None:
        self.assertEqual(render_footer_note("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")

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


if __name__ == "__main__":
    unittest.main()
