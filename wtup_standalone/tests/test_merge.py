from __future__ import annotations

import unittest

from wtup_standalone.diff_collector import DiffChunk, DiffSummary
from wtup_standalone.merge import (
    clean_section_title,
    dedupe_update_items,
    merge_chunk_analyses,
    order_chunk_results,
)
from wtup_standalone.models import ChunkAnalysis, TokenUsage


class TestMerge(unittest.TestCase):
    def test_merge_chunk_analyses(self):
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="http://github.com",
            total_commits=1,
            total_files=2,
            additions=10,
            deletions=2,
            changed_files=2,
            commits=[],
            files=[],
            chunks=[],
            base_version="2.56.0.38",
            head_version="2.56.0.39",
        )
        chunk1 = DiffChunk(index=1, total=2, files=[], patch_chars=100)
        chunk2 = DiffChunk(index=2, total=2, files=[], patch_chars=100)

        res1 = ChunkAnalysis(
            chunk_index=1,
            chunk_total=2,
            analysis={
                "report_title": "2.56.0.38->2.56.0.39",
                "importance": "高",
                "update_sections": [
                    {"title": "新增载具", "items": [{"text": "JH-7A", "source_ids": ["C001-001"]}]}
                ],
                "tags": ["空战"],
            },
            token_usage=TokenUsage(100, 50, 150),
        )
        res2 = ChunkAnalysis(
            chunk_index=2,
            chunk_total=2,
            analysis={
                "report_title": "2.56.0.38->2.56.0.39",
                "importance": "中",
                "update_sections": [
                    {"title": "参数调整", "items": [{"text": "雷达告警参数调整", "source_ids": ["C002-001"]}]}
                ],
                "tags": ["雷达"],
            },
            token_usage=TokenUsage(200, 60, 260),
        )

        merged = merge_chunk_analyses(summary, [chunk1, chunk2], [res1, res2])
        self.assertEqual(merged["report_title"], "2.56.0.38->2.56.0.39")
        self.assertEqual(merged["importance"], "高")
        self.assertIn("空战", merged["tags"])
        self.assertIn("雷达", merged["tags"])
        titles = [sec["title"] for sec in merged["update_sections"]]
        self.assertIn("新增载具", titles)
        self.assertIn("参数调整", titles)

    def test_clean_section_title(self):
        self.assertEqual(clean_section_title("part1/2"), "其他变化")
        self.assertEqual(clean_section_title("第1部分"), "其他变化")
        self.assertEqual(clean_section_title("分片2"), "其他变化")
        self.assertEqual(clean_section_title("新增载具"), "新增载具")


if __name__ == "__main__":
    unittest.main()
