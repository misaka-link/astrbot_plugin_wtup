from __future__ import annotations

import io
import sys
import unittest
from unittest.mock import patch

from wtup_standalone.cli import build_arg_parser


class TestCli(unittest.TestCase):
    def test_arg_parser_file(self):
        parser = build_arg_parser()
        args = parser.parse_args(["-f", "sample.diff", "--format", "json", "-m", "deepseek-chat"])
        self.assertEqual(args.file_path, "sample.diff")
        self.assertEqual(args.format, "json")
        self.assertEqual(args.model, "deepseek-chat")

    def test_arg_parser_compare(self):
        parser = build_arg_parser()
        args = parser.parse_args(["-c", "v1.0...v2.0", "--review-mode", "energy"])
        self.assertEqual(args.compare_range, "v1.0...v2.0")
        self.assertEqual(args.review_mode, "energy")


if __name__ == "__main__":
    unittest.main()
