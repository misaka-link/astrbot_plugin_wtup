from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wtup.config import load_config
from wtup.runtime import RuntimeState, format_elapsed_duration
from wtup.state_store import StateStore


class RuntimeAppendTextTest(unittest.TestCase):
    def test_format_elapsed_duration_outputs_minutes_and_seconds(self) -> None:
        self.assertEqual(format_elapsed_duration(125.9), "2分5秒")
        self.assertEqual(format_elapsed_duration(0), "0分0秒")
        self.assertEqual(format_elapsed_duration(-1), "0分0秒")

    def test_build_push_append_text_supports_elapsed_duration_aliases(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            runtime = RuntimeState(
                settings=load_config(
                    {
                        "push_append_text_template": "耗时:{elapsed_duration}\n别名:{耗时}\n分钟:{elapsed_minutes}",
                    }
                ),
                state_store=StateStore(base / "state.json"),
                log_dir=base / "logs",
                error_dir=base / "errors",
            )

            text = runtime.build_push_append_text(
                analysis={"report_title": "2.56.0.38->2.56.0.39"},
                token_count=150,
                elapsed_minutes=3,
                elapsed_duration="2分5秒",
                summary_model_enabled=False,
            )

            self.assertEqual(text, "耗时:2分5秒\n别名:2分5秒\n分钟:3")


if __name__ == "__main__":
    unittest.main()
