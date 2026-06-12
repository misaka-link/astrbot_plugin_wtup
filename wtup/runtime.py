from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from .config import BRANCH_NAME, PLUGIN_NAME, REPO_FULL_NAME, PluginConfig
from .diff_collector import short_sha
from .report_log import build_report_log_filename, sanitize_filename
from .state_store import StateStore


LOG_SEPARATOR = "=============="


def warning_log(message: str, *args: Any, exc_info: bool = False) -> None:
    logger.warning("%s", LOG_SEPARATOR)
    logger.warning(message, *args, exc_info=exc_info)
    logger.warning("%s", LOG_SEPARATOR)


def ceil_minutes(seconds: float) -> int:
    return max(1, int((max(0.0, seconds) + 59) // 60))


class RuntimeState:
    def __init__(
        self,
        *,
        settings: PluginConfig,
        state_store: StateStore,
        log_dir: Path,
        error_dir: Path,
    ) -> None:
        self.settings = settings
        self.state_store = state_store
        self.log_dir = log_dir
        self.error_dir = error_dir

    def with_runtime_hooks(self, settings: PluginConfig) -> PluginConfig:
        try:
            from dataclasses import replace

            return replace(settings, model_error_recorder=self.record_model_error)
        except Exception:
            settings.model_error_recorder = self.record_model_error  # type: ignore[misc]
            return settings

    def save_seen_commit(self, sha: str) -> None:
        self.update_repo_state(
            {
                "last_commit_sha": sha,
                "last_checked_at": time.time(),
                "branch": BRANCH_NAME,
            }
        )

    def record_model_error(self, stage: str, error: BaseException | str, metadata: dict[str, Any]) -> None:
        self.error_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        base_name = f"{now.year}年{now.month}月{now.day}日{now.hour:02d}时{now.minute:02d}分{now.second:02d}秒"
        output_path = self.error_dir / f"{base_name}.log"
        suffix = 1
        while output_path.exists():
            output_path = self.error_dir / f"{base_name}_{suffix}.log"
            suffix += 1

        error_text = str(error)
        payload = {
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "stage": stage,
            "error_type": type(error).__name__ if isinstance(error, BaseException) else "str",
            "error": error_text,
            "metadata": metadata,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        warning_log("[%s] 模型报错 stage=%s error=%s 错误日志=%s", PLUGIN_NAME, stage, error_text, output_path)

    def save_report_log(
        self,
        summary: Any,
        analysis: dict[str, Any],
        fallback_text: str,
    ) -> Path:
        title = str(analysis.get("report_title") or "").strip()
        filename = sanitize_filename(build_report_log_filename(title))
        output_path = self.log_dir / filename
        generated_at = datetime.now()
        header = [
            f"生成时间: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"仓库: {REPO_FULL_NAME}",
            f"分支: {BRANCH_NAME}",
            f"提交范围: {short_sha(summary.base_sha)}...{short_sha(summary.head_sha)}",
        ]
        if summary.compare_url:
            header.append(f"Compare: {summary.compare_url}")
        header.extend(["", str(fallback_text or "").strip(), ""])

        self.log_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(header), encoding="utf-8")
        logger.warning("[%s] 已保存最终报告日志: %s", PLUGIN_NAME, output_path)
        return output_path

    def build_push_append_text(
        self,
        *,
        analysis: dict[str, Any],
        token_count: int,
        elapsed_minutes: int,
        summary_model_enabled: bool,
    ) -> str:
        version_range = str(analysis.get("report_title") or "").strip() or "版本->版本"
        analysis_model = self.settings.provider_id or "默认模型"
        summary_model = self.settings.effective_summary_provider_id or "默认模型"
        if not summary_model_enabled:
            summary_model = "未启动"
        template = self.settings.push_append_text_template
        try:
            text = template.format(
                version_range=version_range,
                token_count=token_count,
                elapsed_minutes=elapsed_minutes,
                analysis_model=analysis_model,
                summary_model=summary_model,
            )
        except Exception as exc:
            logger.warning("[%s] 追加文字模板格式化失败，使用默认内容: %s", PLUGIN_NAME, exc)
            text = (
                f"{version_range} 分析完成\n"
                f"消耗token:{token_count}\n"
                f"耗时{elapsed_minutes}分钟\n"
                f"分析模型:{analysis_model}\n"
                f"总结模型:{summary_model}"
            )
        return str(text or "").strip()

    def save_task_state(
        self,
        *,
        summary: Any,
        analysis: dict[str, Any],
        log_path: Path,
        image_path: Path | None,
        manual: bool,
        sent_to_groups: bool,
        sent_count: int,
        failed_count: int,
    ) -> None:
        now = time.time()
        task = {
            "repo": REPO_FULL_NAME,
            "branch": BRANCH_NAME,
            "base_sha": summary.base_sha,
            "head_sha": summary.head_sha,
            "report_title": str(analysis.get("report_title") or "").strip(),
            "log_path": str(log_path),
            "image_path": str(image_path) if image_path else "",
            "compare_url": summary.compare_url,
            "manual": manual,
            "sent_to_groups": sent_to_groups,
            "target_groups": list(self.settings.target_groups) if sent_to_groups else [],
            "sent_count": sent_count,
            "failed_count": failed_count,
            "generated_at": now,
        }
        updates: dict[str, Any] = {"last_generated_task": task}
        if sent_to_groups:
            updates["last_pushed_task"] = {**task, "pushed_at": now}
        self.update_repo_state(updates)

    def update_repo_state(self, updates: dict[str, Any]) -> None:
        repo_state = self.state_store.get_repo_state(REPO_FULL_NAME)
        repo_state.update(updates)
        self.state_store.update_repo_state(REPO_FULL_NAME, repo_state)
