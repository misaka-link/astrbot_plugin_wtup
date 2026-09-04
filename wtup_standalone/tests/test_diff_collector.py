from __future__ import annotations

import unittest

from wtup_standalone.diff_collector import (
    build_diff_summary,
    extract_version_from_commit,
    normalize_report_title,
    parse_unified_diff,
    render_chunk_input,
    short_sha,
)


SAMPLE_DIFF = """diff --git a/char.vromfs.bin_u/config/game_modes.blkx b/char.vromfs.bin_u/config/game_modes.blkx
index 1111111..2222222 100644
--- a/char.vromfs.bin_u/config/game_modes.blkx
+++ b/char.vromfs.bin_u/config/game_modes.blkx
@@ -10,3 +10,4 @@
 line1
+line2_added
 line3
diff --git a/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx b/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx
@@ -0,0 +1,5 @@
+new_bomb_data
"""


class TestDiffCollector(unittest.TestCase):
    def test_parse_unified_diff(self):
        files = parse_unified_diff(SAMPLE_DIFF)
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["filename"], "char.vromfs.bin_u/config/game_modes.blkx")
        self.assertEqual(files[0]["status"], "modified")
        self.assertEqual(files[0]["additions"], 1)
        self.assertEqual(files[0]["deletions"], 0)

        self.assertEqual(files[1]["filename"], "aces.vromfs.bin_u/gamedata/weapons/bomb.blkx")
        self.assertEqual(files[1]["status"], "added")
        self.assertEqual(files[1]["additions"], 1)

    def test_build_diff_summary(self):
        compare_payload = {
            "base_commit": {"sha": "abcdef1234567890", "commit": {"message": "2.56.0.38"}},
            "sha": "123456abcdef7890",
            "commits": [{"sha": "123456abcdef7890", "commit": {"message": "2.56.0.39 update"}}],
            "files": [],
        }
        summary = build_diff_summary(compare_payload, raw_diff_text=SAMPLE_DIFF)
        self.assertEqual(summary.total_files, 2)
        self.assertEqual(summary.base_version, "2.56.0.38")
        self.assertEqual(summary.head_version, "2.56.0.39")
        self.assertEqual(len(summary.chunks), 1)

        title = normalize_report_title(summary, "")
        self.assertEqual(title, "2.56.0.38->2.56.0.39")

    def test_render_chunk_input(self):
        compare_payload = {
            "base_commit": {"sha": "abcdef1234567890"},
            "sha": "123456abcdef7890",
            "commits": [{"sha": "123456abcdef7890", "message": "commit title", "author_name": "tester"}],
            "files": [],
        }
        summary = build_diff_summary(compare_payload, raw_diff_text=SAMPLE_DIFF)
        chunk_text = render_chunk_input(summary, summary.chunks[0])
        self.assertIn("提交范围: abcdef1...123456a", chunk_text)
        self.assertIn("char.vromfs.bin_u/config/game_modes.blkx", chunk_text)
        self.assertIn("+line2_added", chunk_text)


if __name__ == "__main__":
    unittest.main()
