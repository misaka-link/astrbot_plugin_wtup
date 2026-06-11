from __future__ import annotations

import unittest
from datetime import datetime

from wtup.report_log import build_report_log_filename, sanitize_filename


class ReportLogFilenameTest(unittest.TestCase):
    def test_version_title_uses_underscore(self) -> None:
        self.assertEqual(
            build_report_log_filename("2.56.0.38->2.56.0.39"),
            "2.56.0.38_2.56.0.39.log",
        )

    def test_non_version_title_uses_local_time_format(self) -> None:
        self.assertEqual(
            build_report_log_filename("更新报告", now=datetime(2026, 6, 12, 3, 0, 18)),
            "2026年6月12日03：00：18.log",
        )

    def test_sanitize_filename_removes_windows_invalid_chars(self) -> None:
        self.assertEqual(sanitize_filename('a:b*c?"d<e>f|.log'), "a_b_c_d_e_f_.log")


if __name__ == "__main__":
    unittest.main()
