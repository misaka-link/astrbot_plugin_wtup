from __future__ import annotations

import json
import unittest

from wtup_standalone.fallback import fallback_analysis
from wtup_standalone.normalize import (
    clean_pagination_text,
    normalize_analysis,
    normalize_importance,
    parse_analysis_json,
    safe_normalize_analysis,
)


class TestNormalize(unittest.TestCase):
    def test_parse_json_from_markdown_fences(self):
        raw = """
这里是一些前置文本
```json
{
  "report_title": "2.56.0.38->2.56.0.39",
  "summary": "本次更新摘要",
  "importance": "高",
  "update_sections": [],
  "tags": ["空战"]
}
```
后置废话
"""
        parsed = parse_analysis_json(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["report_title"], "2.56.0.38->2.56.0.39")
        self.assertEqual(parsed["importance"], "高")
        self.assertIn("空战", parsed["tags"])

    def test_safe_normalize_fallback_on_broken_json(self):
        bad = "这不是 JSON 格式的内容"
        result = safe_normalize_analysis(bad)
        self.assertEqual(result["importance"], "中")
        self.assertIn("需复核", result["tags"])
        self.assertIn("模型输出格式未按 JSON 返回", result["summary"])

    def test_clean_pagination_text(self):
        text = "第 1/3 批包含当前分片内容"
        self.assertEqual(clean_pagination_text(text), "本次 diff 包含本次 diff 内容")

    def test_normalize_importance(self):
        self.assertEqual(normalize_importance("高"), "高")
        self.assertEqual(normalize_importance("低"), "低")
        self.assertEqual(normalize_importance("中"), "中")
        self.assertEqual(normalize_importance("极高"), "中")


if __name__ == "__main__":
    unittest.main()
