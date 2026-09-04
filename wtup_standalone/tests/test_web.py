from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, patch

from wtup_standalone.web.server import WebApp
from wtup_standalone.web.store import DataStore


class TestWebApp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)
        self.app = WebApp(self.data_dir)
        # 测试环境下禁用定时后台网络轮询，避免消耗 lock 与触发实际请求
        self.app.store.save_config({"schedule_enabled": False})
        # 绑定到本地可用高端口 (如 18088)
        self.port = 18088
        await self.app.start(host="127.0.0.1", port=self.port)

    async def asyncTearDown(self):
        self.app.scheduler.stop()
        if self.app.server:
            self.app.server.close()
            await self.app.server.wait_closed()
        self.tmp_dir.cleanup()

    async def test_api_status(self):
        url = f"http://127.0.0.1:{self.port}/api/status"
        def _fetch():
            with urllib.request.urlopen(url, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        status_code, data = await asyncio.to_thread(_fetch)
        self.assertEqual(status_code, 200)
        self.assertTrue(data["scheduler_running"])
        self.assertIn("schedule_interval_minutes", data)
        self.assertIn("current_task", data)
        self.assertIn("logs", data["current_task"])
        self.assertIn("status", data["current_task"])
        self.assertIn("can_retry", data["current_task"])

    async def test_api_config_get_and_post(self):
        # 1. GET
        url = f"http://127.0.0.1:{self.port}/api/config"
        def _get():
            with urllib.request.urlopen(url, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        status_code, cfg = await asyncio.to_thread(_get)
        self.assertEqual(status_code, 200)
        self.assertIn("openai_base_url", cfg)

        # 2. POST
        update_data = {
            "model": "test-gpt-5",
            "thinking_mode": "high",
            "thinking_budget_tokens": 8192,
            "schedule_interval_minutes": 15,
        }
        def _post():
            req = urllib.request.Request(
                url,
                data=json.dumps(update_data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        status_code, post_res = await asyncio.to_thread(_post)
        self.assertEqual(status_code, 200)
        self.assertTrue(post_res["success"])
        self.assertEqual(post_res["config"]["model"], "test-gpt-5")
        self.assertEqual(post_res["config"]["thinking_mode"], "high")
        self.assertEqual(post_res["config"]["thinking_budget_tokens"], 8192)

    async def test_api_history_and_save_report(self):
        # 预先在 store 中插入一条报告
        report_id = self.app.store.save_report(
            {
                "report_title": "2.56.0.40->2.56.0.41",
                "summary": "测试报告",
                "importance": "高",
                "tags": ["测试"],
                "token_usage": {"total_tokens": 100},
            },
            trigger_mode="manual",
            commit_base="base111",
            commit_head="head222",
        )

        # 1. GET /api/history
        url_hist = f"http://127.0.0.1:{self.port}/api/history"
        def _get_hist():
            with urllib.request.urlopen(url_hist, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))

        hist_data = await asyncio.to_thread(_get_hist)
        self.assertGreaterEqual(hist_data["total"], 1)
        self.assertEqual(hist_data["items"][0]["id"], report_id)

        # 2. GET /api/latest
        url_latest = f"http://127.0.0.1:{self.port}/api/latest"
        def _get_latest():
            with urllib.request.urlopen(url_latest, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))

        latest_data = await asyncio.to_thread(_get_latest)
        self.assertIsNotNone(latest_data["report"])
        self.assertEqual(latest_data["report"]["id"], report_id)

        # 3. GET /api/report-html/{id}
        url_html = f"http://127.0.0.1:{self.port}/api/report-html/{report_id}"
        def _get_html():
            with urllib.request.urlopen(url_html, timeout=3) as resp:
                return resp.status, resp.read().decode("utf-8")

        html_code, html_text = await asyncio.to_thread(_get_html)
        self.assertEqual(html_code, 200)
        self.assertTrue("2.56.0.40" in html_text and "2.56.0.41" in html_text)

    async def test_task_status_and_clear_logs(self):
        # 模拟产生一些日志
        self.app.scheduler.log("测试日志第一条", level="INFO")
        self.app.scheduler.log("测试警告日志", level="WARNING")
        self.app.scheduler.log("测试错误日志", level="ERROR")

        # GET /api/task/status
        url_task = f"http://127.0.0.1:{self.port}/api/task/status"
        def _get_task():
            with urllib.request.urlopen(url_task, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        code, task_data = await asyncio.to_thread(_get_task)
        self.assertEqual(code, 200)
        logs = task_data["task"]["logs"]
        self.assertGreaterEqual(len(logs), 3)
        self.assertTrue(any("测试日志第一条" in l for l in logs))
        self.assertTrue(any("测试错误日志" in l for l in logs))

        # POST /api/task/clear-logs
        url_clear = f"http://127.0.0.1:{self.port}/api/task/clear-logs"
        def _clear():
            req = urllib.request.Request(url_clear, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        clear_code, clear_res = await asyncio.to_thread(_clear)
        self.assertEqual(clear_code, 200)
        self.assertTrue(clear_res["success"])
        self.assertEqual(len(self.app.scheduler.current_task_info["logs"]), 0)

    async def test_task_retry_api(self):
        # 测试 POST /api/analyze/retry
        url_retry = f"http://127.0.0.1:{self.port}/api/analyze/retry"
        def _retry():
            req = urllib.request.Request(url_retry, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        # mock trigger_check 避免网络请求
        with patch.object(self.app.scheduler, "trigger_check", new_callable=AsyncMock) as mock_trigger:
            mock_trigger.return_value = "report_mock_123"
            code, res = await asyncio.to_thread(_retry)
            self.assertEqual(code, 202)
            self.assertIn("重试任务已启动", res["message"])
            await asyncio.sleep(0.1)
            mock_trigger.assert_called_once()

    async def test_analysis_failure_does_not_update_commit_or_latest(self):
        """核心验证：分析任务失败时，绝不更新给 API，也不标记该版本已分析完成！"""
        # 初始 state 无 last_checked_commit
        initial_commit = "initial_base_commit_111"
        self.app.store.update_state(last_checked_commit=initial_commit, last_check_status="success")

        # 模拟 trigger_check 内部执行抛出异常（例如大模型调用全部失败或网络中断）
        with patch.object(self.app.scheduler, "git_manager") as mock_git:
            mock_git.is_local_repo_ready.return_value = False
            mock_git.get_remote_head.return_value = "failed_new_commit_999"

            with patch("wtup_standalone.web.scheduler.DatamineAnalyzer.analyze_github_compare", side_effect=RuntimeError("OpenAI API 限流超时 429")):
                with self.assertRaises(RuntimeError):
                    await self.app.scheduler.trigger_check(
                        trigger_mode="manual",
                        compare_range="initial_base_commit_111...failed_new_commit_999"
                    )

        # 检查 1: 任务状态标记为 failed，并记录错误信息
        task_info = self.app.scheduler.current_task_info
        self.assertEqual(task_info["status"], "failed")
        self.assertIn("OpenAI API 限流超时", task_info["error"])
        self.assertIsNone(task_info["result_report_id"])
        self.assertTrue(task_info["can_retry"])

        # 检查 2: state 中的 last_checked_commit 绝对不能被推进为新 commit！
        state = self.app.store.load_state()
        self.assertEqual(state.get("last_checked_commit"), initial_commit)
        self.assertIn("error", state.get("last_check_status", ""))

        # 检查 3: GET /api/latest 绝不包含失败的临时版本，也不会返回任何未完成报告
        url_latest = f"http://127.0.0.1:{self.port}/api/latest"
        def _get_latest():
            with urllib.request.urlopen(url_latest, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))

        latest_data = await asyncio.to_thread(_get_latest)
        self.assertIsNone(latest_data.get("report"))


    async def test_delete_history_report(self):
        """测试删除单份历史分析报告。"""
        id1 = self.app.store.save_report({"report_title": "报告 1", "summary": "s1"}, image_bytes=b"fake_img_1")
        id2 = self.app.store.save_report({"report_title": "报告 2", "summary": "s2"}, image_bytes=b"fake_img_2")

        self.assertTrue((self.app.store.reports_dir / f"{id1}.json").exists())
        self.assertTrue((self.app.store.images_dir / f"{id1}.png").exists())

        url_del = f"http://127.0.0.1:{self.port}/api/history/{id1}"
        def _delete():
            req = urllib.request.Request(url_del, method="DELETE")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        code, data = await asyncio.to_thread(_delete)
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])

        # 验证文件和索引已删除
        self.assertFalse((self.app.store.reports_dir / f"{id1}.json").exists())
        self.assertFalse((self.app.store.images_dir / f"{id1}.png").exists())
        index = self.app.store.list_history_index()
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["id"], id2)

    async def test_max_history_reports_limit_and_pruning(self):
        """测试最多保存历史报告数量限制 (默认 15 份，支持配置)。"""
        # 设置上限为 3 份
        self.app.store.save_config({"max_history_reports": 3})

        ids = []
        for i in range(5):
            r_id = self.app.store.save_report(
                {"report_title": f"报告 {i}", "summary": f"测试 {i}"},
                image_bytes=f"fake_img_{i}".encode("utf-8")
            )
            ids.append(r_id)

        # 索引只应保留最新 3 份 (即 ids[4], ids[3], ids[2])
        index = self.app.store.list_history_index()
        self.assertEqual(len(index), 3)
        self.assertEqual(index[0]["id"], ids[4])
        self.assertEqual(index[1]["id"], ids[3])
        self.assertEqual(index[2]["id"], ids[2])

        # 超出上限的旧文件应该已被物理清理以释放磁盘
        self.assertFalse((self.app.store.reports_dir / f"{ids[0]}.json").exists())
        self.assertFalse((self.app.store.images_dir / f"{ids[0]}.png").exists())
        self.assertFalse((self.app.store.reports_dir / f"{ids[1]}.json").exists())
        self.assertFalse((self.app.store.images_dir / f"{ids[1]}.png").exists())
        # 保留的文件依然存在
        self.assertTrue((self.app.store.reports_dir / f"{ids[4]}.json").exists())
        self.assertTrue((self.app.store.images_dir / f"{ids[4]}.png").exists())

    async def test_task_status_text_explicit_names(self):
        """测试任务执行状态规范化名称：分析中、分析完成、分析失败、等待分析。"""
        task_info = self.app.scheduler.current_task_info
        self.assertEqual(task_info["status_text"], "等待分析")

        self.app.scheduler.prepare_task_info(mode="manual")
        self.assertEqual(self.app.scheduler.current_task_info["status"], "running")
        self.assertEqual(self.app.scheduler.current_task_info["status_text"], "分析中")

    def test_watermark_html_rendering_and_configuration(self):
        """测试平铺防盗水印的 HTML 渲染与开关配置。"""
        from wtup_standalone.web.renderer import build_html_report

        report_data = {
            "report_title": "2.57.1.130->2.57.1.131",
            "summary": "测试水印",
            "importance": "低",
        }

        # 1. 默认关闭水印
        html_default = build_html_report(report_data, template="discord")
        self.assertNotIn("wt-watermark-overlay", html_default)

        # 2. 开启水印 (Discord 风格)
        wm_cfg = {
            "watermark_enabled": True,
            "watermark_text": "MyCustomChannel",
            "watermark_opacity": 0.15,
            "watermark_size": 22,
            "watermark_density": "high",
        }
        html_discord_wm = build_html_report(report_data, template="discord", watermark_config=wm_cfg)
        self.assertIn("wt-watermark-overlay", html_discord_wm)
        self.assertIn("MyCustomChannel", html_discord_wm)
        self.assertIn("position: relative", html_discord_wm)
        self.assertIn("overflow: hidden", html_discord_wm)

        # 3. 开启水印 (Miku 风格)
        html_miku_wm = build_html_report(report_data, template="miku", watermark_config=wm_cfg)
        self.assertIn("wt-watermark-overlay", html_miku_wm)
        self.assertIn("MyCustomChannel", html_miku_wm)

        # 4. 配置持久化验证
        self.app.store.save_config(wm_cfg)
        loaded = self.app.store.load_config()
        self.assertTrue(loaded["watermark_enabled"])
        self.assertEqual(loaded["watermark_text"], "MyCustomChannel")
        self.assertEqual(loaded["watermark_opacity"], 0.15)
        self.assertEqual(loaded["watermark_size"], 22)
        self.assertEqual(loaded["watermark_density"], "high")

    async def test_mobile_api_and_resolution_scale(self):
        """测试手游独立 API、状态隔离及图片渲染清晰度档位。"""
        from wtup_standalone.web.renderer import parse_render_scale

        # 1. 档位测试
        self.assertEqual(parse_render_scale("1x"), 1.0)
        self.assertEqual(parse_render_scale("1.5x"), 1.5)
        self.assertEqual(parse_render_scale("2x"), 2.0)
        self.assertEqual(parse_render_scale("3x"), 3.0)

        # 2. 状态查询 target=mobile
        url_mobile_status = f"http://127.0.0.1:{self.port}/api/status?target=mobile"
        def _get_mobile_status():
            with urllib.request.urlopen(url_mobile_status, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        code, status_data = await asyncio.to_thread(_get_mobile_status)
        self.assertEqual(code, 200)
        self.assertEqual(status_data["target"], "mobile")
        self.assertIn("War-Thunder-Mobile-Datamine", status_data["config_summary"]["github_repo"])
        self.assertIn("mobile", status_data["repos"])

        # 3. 手游独立报告保存与获取
        r_mobile_id = self.app.store.save_report(
            {"report_title": "1.26.0.56->1.26.0.62", "summary": "手游更新测试"},
            target="mobile",
        )
        r_pc_id = self.app.store.save_report(
            {"report_title": "2.57.1.130->2.57.1.131", "summary": "端游更新测试"},
            target="pc",
        )
        latest_mobile = self.app.store.get_latest_report(target="mobile")
        latest_pc = self.app.store.get_latest_report(target="pc")
        self.assertIsNotNone(latest_mobile)
        self.assertIsNotNone(latest_pc)
        self.assertEqual(latest_mobile["id"], r_mobile_id)
        self.assertEqual(latest_pc["id"], r_pc_id)

        # 4. 手游专属独立 HTML 模板测试
        from wtup_standalone.web.renderer import build_html_report
        html_mobile = build_html_report({"report_title": "1.26.0.56->1.26.0.62", "target": "mobile", "summary": "手游更新测试"})
        self.assertIn("战争雷霆手游", html_mobile)
        self.assertIn("手游专刊", html_mobile)
        self.assertIn("mobile-canvas", html_mobile)
        self.assertIn("War-Thunder-Mobile-Datamine", html_mobile)


if __name__ == "__main__":
    unittest.main()
