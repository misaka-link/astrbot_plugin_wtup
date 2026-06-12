from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

from wtup.config import PLUGIN_VERSION
from wtup.config import load_config
from wtup.report_log import build_report_log_filename, sanitize_filename
from wtup.runtime import RuntimeState
from wtup.state_store import StateStore


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

    def test_plugin_version_is_011(self) -> None:
        self.assertEqual(PLUGIN_VERSION, "0.1.1")


class ModelErrorLogTest(unittest.TestCase):
    def test_model_error_log_uses_chinese_second_filename_and_json_payload(self) -> None:
        main = import_main_with_astrbot_stubs()

        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(main.WTUpdatePlugin)
            plugin.error_dir = Path(temp_dir) / "errors"

            plugin._record_model_error("chunk_analysis_failed", RuntimeError("模型爆炸"), {"chunk_index": 1})

            files = list(plugin.error_dir.glob("*.log"))
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0].name, r"^\d{4}年\d{1,2}月\d{1,2}日\d{2}时\d{2}分\d{2}秒\.log$")
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["stage"], "chunk_analysis_failed")
            self.assertEqual(payload["error"], "模型爆炸")
            self.assertIn("RuntimeError: 模型爆炸", payload["traceback"])
            self.assertEqual(payload["metadata"]["chunk_index"], 1)

    def test_runtime_cleanup_keeps_latest_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            target = base / "logs"
            target.mkdir()
            for index in range(7):
                path = target / f"{index}.log"
                path.write_text(str(index), encoding="utf-8")
                path.touch()

            runtime = RuntimeState(
                settings=load_config({"max_saved_artifacts": 5}),
                state_store=StateStore(base / "state.json"),
                log_dir=target,
                error_dir=base / "errors",
            )

            runtime.cleanup_saved_artifacts(target)

            self.assertEqual(len(list(target.glob("*.log"))), 5)


def import_main_with_astrbot_stubs():
    if "main" in sys.modules:
        return importlib.import_module("main")

    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.AstrBotConfig = dict
    astrbot_api.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)

    event_module = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class Filter:
        @staticmethod
        def command(_name):
            return lambda func: func

    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.filter = Filter()

    star_module = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(_name):
            return tempfile.mkdtemp()

    def register(*_args, **_kwargs):
        return lambda cls: cls

    star_module.Context = object
    star_module.Star = Star
    star_module.StarTools = StarTools
    star_module.register = register

    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    sys.modules["astrbot.api"] = astrbot_api
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module
    return importlib.import_module("main")


if __name__ == "__main__":
    unittest.main()
