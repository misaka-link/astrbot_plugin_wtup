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

    def test_task_log_records_model_request_details(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            runtime = RuntimeState(
                settings=load_config({}),
                state_store=StateStore(base / "state.json"),
                log_dir=base / "logs",
                error_dir=base / "errors",
                task_log_dir=base / "task_logs",
            )

            task_log_path = runtime.start_task_log(manual=True, force_latest=True, send_to_groups=True)
            request_no = runtime.record_task_log(
                "模型请求开始",
                {
                    "Provider": "default",
                    "输入token": 12,
                },
            )
            runtime.finish_task_log(status="完成", message="ok", elapsed_seconds=1.2)

            content = task_log_path.read_text(encoding="utf-8")
            self.assertEqual(request_no, 1)
            self.assertIn("任务开始", content)
            self.assertIn("模型请求开始", content)
            self.assertIn("第几次模型请求: 1", content)
            self.assertIn("输入token: 12", content)
            self.assertNotIn("请求内容", content)
            self.assertNotIn("请分析这个 diff", content)
            self.assertIn("任务结束", content)


if __name__ == "__main__":
    unittest.main()
