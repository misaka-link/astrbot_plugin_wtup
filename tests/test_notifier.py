from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wtup.notifier import push_admin_notification, push_log_file


class SendMessageContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, target, chain):
        self.sent.append((target, chain))


class CallActionContext:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))


class NotifierLogFileTest(unittest.IsolatedAsyncioTestCase):
    async def test_push_log_file_does_not_fallback_to_text_for_non_group_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "report.log"
            log_path.write_text("log text", encoding="utf-8")
            context = SendMessageContext()

            success, failed = await push_log_file(context, ["platform:group"], log_path=log_path)

            self.assertEqual(success, 0)
            self.assertEqual(failed, 1)
            self.assertEqual(context.sent, [])

    async def test_push_admin_notification_sends_private_message_for_numeric_target(self) -> None:
        context = CallActionContext()

        success, failed = await push_admin_notification(context, ["123456"], text="分析失败")

        self.assertEqual(success, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(context.calls[0][0], "send_private_msg")
        self.assertEqual(context.calls[0][1]["user_id"], 123456)
        self.assertEqual(context.calls[0][1]["message"][0]["data"]["text"], "分析失败")


if __name__ == "__main__":
    unittest.main()
