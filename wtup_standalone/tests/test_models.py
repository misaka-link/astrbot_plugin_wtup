from __future__ import annotations

import unittest

from wtup_standalone.models import (
    AnalysisResult,
    ChunkAnalysis,
    DiffChunk,
    DiffSummary,
    TokenUsage,
)


class TestModels(unittest.TestCase):
    def test_token_usage_arithmetic(self):
        u1 = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        u2 = TokenUsage(prompt_tokens=200, completion_tokens=80, total_tokens=280)
        u3 = u1 + u2
        self.assertEqual(u3.prompt_tokens, 300)
        self.assertEqual(u3.completion_tokens, 130)
        self.assertEqual(u3.total_tokens, 430)
        self.assertFalse(u3.is_empty)

    def test_token_usage_is_empty(self):
        empty = TokenUsage()
        self.assertTrue(empty.is_empty)
        self.assertEqual(empty.to_dict(), {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    def test_analysis_result_markdown(self):
        res = AnalysisResult(
            report_title="2.56.0.38->2.56.0.39",
            summary="本次更新主要针对陆战载具和红外告警。",
            importance="高",
            update_sections=[
                {
                    "title": "新增载具",
                    "items": [
                        {"name": "JH-7A(飞豹)", "detail": "新增引导炸弹挂载", "importance": "高"}
                    ]
                }
            ],
            bulk_repeat_content={
                "batch": ["批量修改 20 个单位维修费"],
                "repeated": [],
                "needs_verification": [],
            },
            suspected_hallucinations=[],
            ai_analysis={
                "player_impact": ["陆战防空环境发生变化"],
                "uncertainties": ["挂载具体数量需进服确认"],
                "recommendation": "建议关注。",
            },
            highlights=["JH-7A 新增挂载"],
            player_impact=["陆战防空环境变化"],
            risks=["挂载数量需实测"],
            recommendation="建议关注。",
            tags=["空战", "载具更新"],
            token_usage=TokenUsage(100, 50, 150),
            elapsed_seconds=1.23,
        )
        md = res.to_markdown()
        self.assertIn("# 2.56.0.38->2.56.0.39", md)
        self.assertIn("**标签**: 空战 · 载具更新", md)
        self.assertIn("**重要程度**: 高", md)
        self.assertIn("## 核心亮点", md)
        self.assertIn("JH-7A(飞豹)", md)
        self.assertIn("## 批量与高频改动", md)
        self.assertIn("## AI 分析与战术建议", md)
        self.assertIn("总计 150 · 输入 100 · 输出 50", md)

        data = res.to_dict()
        self.assertEqual(data["report_title"], "2.56.0.38->2.56.0.39")
        self.assertEqual(data["token_usage"]["total_tokens"], 150)


if __name__ == "__main__":
    unittest.main()
