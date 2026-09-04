from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

# 动态加载纯净版插件模块
spec = importlib.util.spec_from_file_location("astrbot_plugin_wtup.main", "astrbot_plugin_wtup/main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
WTUpdateClient = mod.WTUpdateClient


class MockContext:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, target, chain):
        self.sent_messages.append((target, chain))


class TestClientPlugin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.context = MockContext()
        self.client = WTUpdateClient(context=self.context, config={
            "interval_minutes": 5,
            "push_targets": ["123456"],
            "api_base_url": "http://mock-api.local",
        })
        self.client.data_dir = Path(self.tmp_dir.name)
        self.client.state_file = self.client.data_dir / "client_state.json"
        if self.client._task:
            self.client._task.cancel()

    async def asyncTearDown(self):
        if self.client._task and not self.client._task.done():
            self.client._task.cancel()
        self.tmp_dir.cleanup()

    def test_default_config_values(self):
        self.assertEqual(self.client._get_config_int("interval_minutes", 5), 5)
        self.assertEqual(self.client._get_config_str("render_template", "discord"), "discord")
        self.assertEqual(self.client._get_config_str("api_base_url", "http://127.0.0.1:8080"), "http://mock-api.local")

    async def test_skip_push_when_already_sent(self):
        # 预设已经发送过的 report_id
        self.client._save_last_sent_report_id("report_20260902_001")
        self.assertEqual(self.client.last_sent_report_id, "report_20260902_001")

        # Mock API 返回相同的 report_id
        mock_api_response = json.dumps({
            "report": {
                "id": "report_20260902_001",
                "report_title": "2.57.1.127 -> 2.57.1.128",
            }
        }).encode("utf-8")

        self.client._sync_fetch = lambda req, timeout: mock_api_response

        # 触发定时检查 (manual=False)
        pushed, msg = await self.client.check_and_push(manual=False)
        # 必须不推送！静默跳过！
        self.assertFalse(pushed)
        self.assertEqual(msg, "无新更新")
        self.assertEqual(len(self.context.sent_messages), 0)

    async def test_push_and_persist_when_new_report_detected(self):
        # 预设旧的 report_id
        self.client._save_last_sent_report_id("report_20260901_old")

        # Mock API 返回全新的 report_id
        mock_api_response = json.dumps({
            "report": {
                "id": "report_20260902_new",
                "report_title": "2.57.1.127 -> 2.57.1.128",
                "importance": "高",
                "tags": ["新增载具"],
                "summary": "Su-17M4 核弹版",
            }
        }).encode("utf-8")

        mock_image = b"test_mock_png_image_bytes"
        self.client._sync_fetch = lambda req, timeout: mock_image if "report-image" in req.full_url else mock_api_response

        # 触发定时检查 (manual=False)
        pushed, msg = await self.client.check_and_push(manual=False)

        # 必须成功推送！
        self.assertTrue(pushed)
        self.assertIn("成功推送报告", msg)
        self.assertEqual(len(self.context.sent_messages), 1)
        self.assertEqual(self.context.sent_messages[0][0], "123456")

        # 必须持久化新的 report_id，防止下次重复推送
        self.assertEqual(self.client.last_sent_report_id, "report_20260902_new")
        saved_from_file = self.client._load_last_sent_report_id()
        self.assertEqual(saved_from_file, "report_20260902_new")


if __name__ == "__main__":
    unittest.main()
