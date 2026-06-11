from __future__ import annotations

import unittest

from wtup.renderer import render_footer_note


class RendererFooterNoteTest(unittest.TestCase):
    def test_footer_note_renders_markdown_link_and_lines(self) -> None:
        self.assertEqual(
            render_footer_note("[repo](https://github.com/example/repo)\n第二行"),
            '<a href="https://github.com/example/repo">repo</a><br>第二行',
        )

    def test_footer_note_escapes_plain_text(self) -> None:
        self.assertEqual(render_footer_note("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")


if __name__ == "__main__":
    unittest.main()
