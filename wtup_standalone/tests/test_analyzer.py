from __future__ import annotations

import asyncio
import json
import unittest

from wtup_standalone.analyzer import DatamineAnalyzer
from wtup_standalone.config import AnalyzerConfig
from wtup_standalone.models import AnalysisResult

SAMPLE_DIFF = """diff --git a/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx b/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx
@@ -0,0 +1,5 @@
+new_bomb_data: 500kg
"""

SAMPLE_DATAMINE_DIFF = """diff --git a/char.vromfs.bin_u/config/wpcost.blkx b/char.vromfs.bin_u/config/wpcost.blkx
index 1111111..2222222 100644
--- a/char.vromfs.bin_u/config/wpcost.blkx
+++ b/char.vromfs.bin_u/config/wpcost.blkx
@@ -10,3 +10,12 @@
+  "su_17m4_killstreak": {
+    "rank": 6,
+    "economicRankArcade": 26,
+    "economicRankHistorical": 27,
+    "economicRankSimulation": 27,
+  }
diff --git a/version b/version
--- a/version
+++ b/version
@@ -1 +1 @@
-2.57.1.127
+2.57.1.128
"""


class TestDatamineAnalyzer(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_summary_mode_with_mock_llm(self):
        async def mock_llm_callable(prompt: str, **kwargs):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({
                                "report_title": "2.56.0.38->2.56.0.39",
                                "summary": "新增 500kg 炸弹配置",
                                "importance": "中",
                                "update_sections": [
                                    {
                                        "title": "武器调整",
                                        "items": [
                                            {"text": "新增 500kg 航弹", "source_ids": ["C001-001"]}
                                        ]
                                    }
                                ],
                                "bulk_repeat_content": {
                                    "batch": [],
                                    "repeated": [],
                                    "needs_verification": [],
                                },
                                "ai_analysis": {
                                    "changed_content": ["500kg 航弹实装"],
                                    "player_impact": ["对地轰炸能力提升"],
                                    "uncertainties": [],
                                    "recommendation": "可重点关注挂载更新。",
                                },
                                "highlights": ["新增 500kg 航弹"],
                                "tags": ["武器更新", "空战"],
                            })
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 60,
                    "total_tokens": 180,
                }
            }

        config = AnalyzerConfig(
            model="mock-model",
            review_mode="off",
            enable_struct_diff=False,
            report_style="summary",
        )
        analyzer = DatamineAnalyzer(config, llm_context=mock_llm_callable)

        result = await analyzer.analyze_diff_text(SAMPLE_DIFF)

        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.report_title, "2.56.0.38->2.56.0.39")
        self.assertIn("本次更新包含 1 个提交", result.summary)
        self.assertEqual(result.importance, "中")
        self.assertIn("武器更新", result.tags)
        self.assertIn("空战", result.tags)
        self.assertEqual(result.token_usage.total_tokens, 180)

        # 验证 Markdown 格式化
        md = result.to_markdown()
        self.assertIn("# 2.56.0.38->2.56.0.39", md)
        self.assertIn("新增 500kg 航弹", md)
        self.assertIn("对地轰炸能力提升", md)

    async def test_end_to_end_datamine_tree_mode(self):
        config = AnalyzerConfig(
            model="mock-model",
            review_mode="off",
            enable_struct_diff=False,
            report_style="datamine",
        )
        analyzer = DatamineAnalyzer(config)
        result = await analyzer.analyze_diff_text(SAMPLE_DATAMINE_DIFF)

        md = result.to_markdown()
        self.assertIn("Su-17M4 (nuke) [USSR (苏系)]", md)
        self.assertIn("tier VI", md)
        self.assertIn("AB: 9.7", md)
        self.assertIn("Air RB: 10.0", md)
        self.assertIn("SB: 10.0", md)
        self.assertIn("Current dev version: 2.57.1.128", md)


if __name__ == "__main__":
    unittest.main()
