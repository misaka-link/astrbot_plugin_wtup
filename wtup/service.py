from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from .analyzer import (
    analyze_chunks,
    fallback_analysis,
    estimate_chunk_input_tokens,
    merge_chunk_analyses,
    refine_chunk_analyses,
    refine_merged_analysis,
    split_chunks_by_token_limit,
)
from .config import BRANCH_NAME, PLUGIN_NAME, REPO_FULL_NAME, PluginConfig
from .diff_collector import DiffChunk, build_diff_summary, short_sha
from .github_client import GitHubClient, GitHubRequestError
from .notifier import push_log_file, push_report, push_text
from .renderer import build_report_html, render_plain_text, render_report_image
from .runtime import RuntimeState, ceil_minutes, warning_log
from .state_store import StateStore


class UpdateCheckService:
    def __init__(
        self,
        *,
        context: Any,
        settings: PluginConfig,
        state_store: StateStore,
        image_dir: Path,
        log_dir: Path,
        error_dir: Path,
        template_path: Path,
        render_host: Any | None = None,
    ) -> None:
        self.context = context
        self.settings = settings
        self.render_host = render_host if render_host is not None else self
        self.state_store = state_store
        self.image_dir = image_dir
        self.log_dir = log_dir
        self.error_dir = error_dir
        self.template_path = template_path
        self.runtime = RuntimeState(
            settings=settings,
            state_store=state_store,
            log_dir=log_dir,
            error_dir=error_dir,
        )
        self._check_lock = asyncio.Lock()

    def with_runtime_hooks(self, settings: PluginConfig) -> PluginConfig:
        self.runtime.settings = settings
        return self.runtime.with_runtime_hooks(settings)

    async def check_once(
        self,
        *,
        manual: bool,
        force_latest: bool,
        send_to_groups: bool,
        event: Any | None = None,
    ) -> dict[str, Any]:
        async with self._check_lock:
            started_at = time.monotonic()
            warning_log("[%s] 开始执行检查%s", PLUGIN_NAME, "（手动）" if manual else "（定时）")

            client = GitHubClient(token=self.settings.github_token)
            logger.warning("[%s] 步骤 1/5: 获取最新 commit...", PLUGIN_NAME)
            latest = await asyncio.to_thread(client.get_latest_commit, REPO_FULL_NAME, BRANCH_NAME)
            logger.warning("[%s] 已完成获取最新 commit: %s", PLUGIN_NAME, short_sha(latest.sha))
            if not latest.sha:
                return {"message": "未获取到最新 commit。"}

            repo_state = self.state_store.get_repo_state(REPO_FULL_NAME)
            previous_sha = str(repo_state.get("last_commit_sha") or "").strip()

            if not previous_sha:
                if force_latest and latest.parents:
                    previous_sha = latest.parents[0]
                else:
                    self.runtime.save_seen_commit(latest.sha)
                    logger.warning("[%s] 首次检查，已建立基线: %s", PLUGIN_NAME, short_sha(latest.sha))
                    return {
                        "message": (
                            f"首次检查已建立基线：{short_sha(latest.sha)}。\n"
                            "定时任务不会推送历史更新；如需测试最新 commit，请使用 /wtup_check 强制。"
                        )
                    }

            if previous_sha == latest.sha and not force_latest:
                self.runtime.save_seen_commit(latest.sha)
                logger.warning("[%s] 没有新 commit，跳过", PLUGIN_NAME)
                return {"message": f"没有新 commit，当前为 {short_sha(latest.sha)}。"}

            if force_latest and latest.parents:
                previous_sha = latest.parents[0]

            if send_to_groups and not self.settings.target_groups:
                return {"message": "发现新 commit，但未配置推送群聊列表，已跳过模型分析和推送。"}

            logger.warning("[%s] 步骤 2/5: 对比 commits (%s...%s)...", PLUGIN_NAME, short_sha(previous_sha), short_sha(latest.sha))
            compare_payload = await asyncio.to_thread(client.compare_commits, REPO_FULL_NAME, previous_sha, latest.sha)
            logger.warning("[%s] 已完成对比 commits，共 %d 个文件变更", PLUGIN_NAME, len(compare_payload.get("files", [])))

            logger.warning("[%s] 步骤 3/5: 获取原始 diff...", PLUGIN_NAME)
            try:
                raw_diff_text = await asyncio.to_thread(client.compare_diff_text, REPO_FULL_NAME, previous_sha, latest.sha)
                logger.warning("[%s] 已完成获取原始 diff", PLUGIN_NAME)
            except GitHubRequestError as exc:
                raw_diff_text = ""
                logger.warning("[%s] 获取原始 diff 失败，使用 compare API 文件列表兜底: %s", PLUGIN_NAME, exc)

            logger.warning("[%s] 步骤 4/5: 构建 diff 摘要...", PLUGIN_NAME)
            summary = build_diff_summary(
                compare_payload,
                raw_diff_text=raw_diff_text,
                max_files=self.settings.max_files_per_report,
                max_chars=0,
            )
            if not summary.head_sha:
                summary = build_diff_summary(
                    {**compare_payload, "sha": latest.sha},
                    raw_diff_text=raw_diff_text,
                    max_files=self.settings.max_files_per_report,
                    max_chars=0,
                )
            summary = split_chunks_by_token_limit(self.settings, summary)
            input_token_count = sum(estimate_chunk_input_tokens(self.settings, summary, chunk) for chunk in summary.chunks)
            logger.warning(
                "[%s] 已完成构建 diff 摘要，%d 个文件，拆分为 %d 次模型请求，预计输入 %d token",
                PLUGIN_NAME,
                summary.total_files,
                len(summary.chunks),
                input_token_count,
            )

            sent_count = 0
            failed_count = 0

            logger.warning("[%s] 步骤 5/5: 分析并生成单份报告...", PLUGIN_NAME)
            chunk_results = await analyze_chunks(self.context, self.settings, summary)
            summary_model_enabled = self.settings.enable_summary_model
            try:
                analysis = merge_chunk_analyses(summary, summary.chunks, chunk_results)
                if summary_model_enabled:
                    logger.warning("[%s] 已启动总结模型，正在整理合并报告...", PLUGIN_NAME)
                    analysis = await refine_merged_analysis(self.context, self.settings, summary, analysis)
            except Exception as exc:
                logger.warning("[%s] 合并分片分析结果失败: %s", PLUGIN_NAME, exc)
                if summary_model_enabled:
                    logger.warning("[%s] 已启动总结模型，改用分片原始分析 JSON 生成报告...", PLUGIN_NAME)
                    analysis = await refine_chunk_analyses(
                        self.context,
                        self.settings,
                        summary,
                        summary.chunks,
                        chunk_results,
                        merge_error=str(exc),
                    )
                else:
                    analysis = fallback_analysis("程序合并分片分析结果失败，需要结合 GitHub 原始 diff 复核。")
            report_chunk = DiffChunk(
                index=1,
                total=1,
                files=summary.files,
                patch_chars=sum(chunk.patch_chars for chunk in summary.chunks),
            )
            html_text = build_report_html(
                self.template_path,
                summary,
                report_chunk,
                analysis,
                footer_note=self.settings.footer_note,
            )
            image_path = await render_report_image(self.render_host, html_text, self.image_dir)
            fallback_text = render_plain_text(summary, report_chunk, analysis)
            log_path = self.runtime.save_report_log(summary, analysis, fallback_text)
            elapsed_minutes = ceil_minutes(time.monotonic() - started_at)

            if send_to_groups and self.settings.target_groups:
                logger.warning("[%s] 推送合并报告到 %d 个群聊...", PLUGIN_NAME, len(self.settings.target_groups))
                ok, failed = await push_report(
                    self.context,
                    self.settings.target_groups,
                    image_path=image_path,
                    fallback_text=fallback_text,
                    event=event,
                )
                sent_count += ok
                failed_count += failed
                logger.warning("[%s] 推送完成合并报告: 成功 %d，失败 %d", PLUGIN_NAME, ok, failed)
                if self.settings.enable_push_append_text:
                    append_text = self.runtime.build_push_append_text(
                        analysis=analysis,
                        token_count=input_token_count,
                        elapsed_minutes=elapsed_minutes,
                        summary_model_enabled=summary_model_enabled,
                    )
                    text_ok, text_failed = await push_text(
                        self.context,
                        self.settings.target_groups,
                        text=append_text,
                        event=event,
                    )
                    sent_count += text_ok
                    failed_count += text_failed
                    logger.warning("[%s] 追加文字推送完成: 成功 %d，失败 %d", PLUGIN_NAME, text_ok, text_failed)

            if send_to_groups and self.settings.analysis_file_groups:
                logger.warning("[%s] 推送分析日志文件到 %d 个群聊...", PLUGIN_NAME, len(self.settings.analysis_file_groups))
                file_ok, file_failed = await push_log_file(
                    self.context,
                    self.settings.analysis_file_groups,
                    log_path=log_path,
                    event=event,
                )
                logger.warning("[%s] 分析日志文件推送完成: 成功 %d，失败 %d", PLUGIN_NAME, file_ok, file_failed)

            self.runtime.save_task_state(
                summary=summary,
                analysis=analysis,
                log_path=log_path,
                image_path=image_path,
                manual=manual,
                sent_to_groups=bool(send_to_groups and self.settings.target_groups),
                sent_count=sent_count,
                failed_count=failed_count,
            )

            if not manual or send_to_groups:
                self.runtime.save_seen_commit(latest.sha)

            message = (
                f"发现更新：{short_sha(previous_sha)}...{short_sha(latest.sha)}，"
                f"共 {summary.total_files} 个文件，模型请求 {len(summary.chunks) + (1 if summary_model_enabled else 0)} 次，"
                f"并发 {self.settings.model_concurrency}，已合并为 1 份报告。"
            )
            if send_to_groups:
                message += f" 推送成功 {sent_count}，失败 {failed_count}。"
            warning_log("[%s] 检查完成: %s", PLUGIN_NAME, message)
            if image_path:
                return {"message": message, "image_path": image_path}
            return {"message": fallback_text or message}
