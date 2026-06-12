from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wtup.notifier import push_log_file


class SendMessageContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, target, chain):
        self.sent.append((target, chain))


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


if __name__ == "__main__":
    unittest.main()
