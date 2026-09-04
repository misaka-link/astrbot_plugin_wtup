from __future__ import annotations

import json
import unittest

from wtup_standalone.struct_diff import (
    DIFF_KIND_EMPTY,
    DIFF_KIND_SEMANTIC,
    DIFF_KIND_TEXT,
    build_struct_compare,
    collect_json_changes,
    render_change_lines,
)


class TestStructDiff(unittest.TestCase):
    def test_identical_content_reports_empty(self) -> None:
        result = build_struct_compare('{"a": 1}', '{"a": 1}')
        self.assertEqual(result.kind, DIFF_KIND_EMPTY)
        self.assertEqual(result.change_count, 0)
        self.assertIn("一致", result.text)

    def test_scalar_change_is_reported_with_path(self) -> None:
        old = '{"unit": {"rank": 2, "br": 5.7}}'
        new = '{"unit": {"rank": 3, "br": 5.7}}'
        result = build_struct_compare(old, new)
        self.assertEqual(result.kind, DIFF_KIND_SEMANTIC)
        self.assertEqual(result.change_count, 1)
        self.assertIn("[修改] $.unit.rank: 2 -> 3", result.text)

    def test_added_and_removed_fields(self) -> None:
        old = '{"a": 1, "b": 2}'
        new = '{"a": 1, "c": 3}'
        lines = render_change_lines(collect_json_changes(json.loads(old), json.loads(new)))
        self.assertIn("[新增] $.c = 3", lines)
        self.assertIn("[删除] $.b (旧值: 2)", lines)

    def test_non_json_falls_back_to_text(self) -> None:
        old = "line one\nline two\n"
        new = "line one\nline two changed\n"
        result = build_struct_compare(old, new)
        self.assertEqual(result.kind, DIFF_KIND_TEXT)
        self.assertIn("-line two", result.text)
        self.assertIn("+line two changed", result.text)


if __name__ == "__main__":
    unittest.main()
