from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
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
    refine_chunk_analyses_with_usage,
    refine_merged_analysis_with_usage,
    split_chunks_by_token_limit,
    sum_token_usage,
)
from .config import BRANCH_NAME, PLUGIN_NAME, REPO_FULL_NAME, PluginConfig
from .diff_collector import DiffChunk, build_diff_summary, short_sha
from .github_client import GitHubClient, GitHubRequestError
from .notifier import push_log_file, push_report, push_text
from .renderer import build_report_html, render_plain_text, render_report_image
from .runtime import RuntimeState, ceil_minutes, warning_log
from .state_store import StateStore
from .analysis import TokenUsage


@dataclass
class ReportArtifact:
    key: str
    display_name: str
    analysis: dict[str, Any]
    html_text: str
    fallback_text: str
    image_path: Path | None
    log_path: Path
    token_usage: TokenUsage


def normalize_targets(targets: list[str] | None) -> list[str]:
    if not targets:
        return []
    return [str(target).strip() for target in targets if str(target or "").strip()]


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
        target_groups: list[str] | None = None,
        analysis_file_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._check_lock:
            started_at = time.monotonic()
            warning_log("[%s] 开始执行检查%s", PLUGIN_NAME, "（手动）" if manual else "（定时）")
            push_targets = normalize_targets(target_groups) if target_groups is not None else list(self.settings.target_groups)
            file_targets = (
                normalize_targets(analysis_file_groups)
                if analysis_file_groups is not None
                else list(self.settings.analysis_file_groups)
            )

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

            if send_to_groups and not push_targets:
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

            logger.warning("[%s] 步骤 5/5: 分析并生成报告...", PLUGIN_NAME)
            chunk_results = await analyze_chunks(self.context, self.settings, summary)
            token_usage = sum_token_usage(result.token_usage for result in chunk_results)
            pre_summary_token_usage = token_usage
            summary_model_enabled = self.settings.enable_summary_model
            merged_analysis: dict[str, Any] | None = None
            try:
                analysis = merge_chunk_analyses(summary, summary.chunks, chunk_results)
                merged_analysis = analysis
                if summary_model_enabled:
                    logger.warning("[%s] 已启动总结模型，正在整理合并报告...", PLUGIN_NAME)
                    analysis, summary_usage = await refine_merged_analysis_with_usage(
                        self.context,
                        self.settings,
                        summary,
                        analysis,
                    )
                    token_usage += summary_usage
            except Exception as exc:
                logger.warning("[%s] 合并分片分析结果失败: %s", PLUGIN_NAME, exc)
                if summary_model_enabled:
                    logger.warning("[%s] 已启动总结模型，改用分片原始分析 JSON 生成报告...", PLUGIN_NAME)
                    analysis, summary_usage = await refine_chunk_analyses_with_usage(
                        self.context,
                        self.settings,
                        summary,
                        summary.chunks,
                        chunk_results,
                        merge_error=str(exc),
                    )
                    token_usage += summary_usage
                else:
                    analysis = fallback_analysis("程序合并分片分析结果失败，需要结合 GitHub 原始 diff 复核。")
            token_count = token_usage.total_tokens
            logger.warning(
                "[%s] 模型真实 token 消耗: total=%d, prompt=%d, completion=%d",
                PLUGIN_NAME,
                token_usage.total_tokens,
                token_usage.prompt_tokens,
                token_usage.completion_tokens,
            )
            report_chunk = DiffChunk(
                index=1,
                total=1,
                files=summary.files,
                patch_chars=sum(chunk.patch_chars for chunk in summary.chunks),
            )
            report_specs: list[tuple[str, str, dict[str, Any], str, TokenUsage]] = []
            if self.settings.enable_pre_summary_report and summary_model_enabled and merged_analysis is not None:
                report_specs.append(
                    ("pre_summary", "总分析模型分析前", merged_analysis, "总分析前", pre_summary_token_usage)
                )
            post_label = "总分析模型分析后" if report_specs else ""
            post_suffix = "总分析后" if report_specs else ""
            report_specs.append(("final", post_label, analysis, post_suffix, token_usage))
            report_artifacts: list[ReportArtifact] = []
            cleanup_keep = (
                None
                if self.settings.max_saved_artifacts <= 0
                else max(self.settings.max_saved_artifacts, len(report_specs))
            )
            for key, display_name, report_analysis, filename_suffix, report_token_usage in report_specs:
                html_text = build_report_html(
                    self.template_path,
                    summary,
                    report_chunk,
                    report_analysis,
                    footer_note=self.settings.footer_note,
                    report_label=display_name,
                    token_usage=report_token_usage,
                )
                image_path = await render_report_image(self.render_host, html_text, self.image_dir)
                fallback_text = render_plain_text(summary, report_chunk, report_analysis, report_label=display_name)
                log_path = self.runtime.save_report_log(
                    summary,
                    report_analysis,
                    fallback_text,
                    filename_suffix=filename_suffix,
                    display_name=display_name,
                    cleanup_keep=cleanup_keep,
                    token_usage=report_token_usage,
                )
                report_artifacts.append(
                    ReportArtifact(
                        key=key,
                        display_name=display_name,
                        analysis=report_analysis,
                        html_text=html_text,
                        fallback_text=fallback_text,
                        image_path=image_path,
                        log_path=log_path,
                        token_usage=report_token_usage,
                    )
                )
            self.runtime.cleanup_saved_artifacts(self.image_dir, keep=cleanup_keep)
            final_report = report_artifacts[-1]
            image_path = final_report.image_path
            fallback_text = final_report.fallback_text
            log_path = final_report.log_path
            elapsed_minutes = ceil_minutes(time.monotonic() - started_at)
            append_text = ""
            if self.settings.enable_push_append_text:
                append_text = self.runtime.build_push_append_text(
                    analysis=analysis,
                    token_count=token_count,
                    elapsed_minutes=elapsed_minutes,
                    summary_model_enabled=summary_model_enabled,
                )

            if send_to_groups and push_targets:
                logger.warning(
                    "[%s] 推送 %d 份报告到 %d 个群聊...",
                    PLUGIN_NAME,
                    len(report_artifacts),
                    len(push_targets),
                )
                for report in report_artifacts:
                    ok, failed = await push_report(
                        self.context,
                        push_targets,
                        image_path=report.image_path,
                        fallback_text=report.fallback_text,
                        event=event,
                    )
                    sent_count += ok
                    failed_count += failed
                    logger.warning(
                        "[%s] 推送完成%s: 成功 %d，失败 %d",
                        PLUGIN_NAME,
                        report.display_name or "合并报告",
                        ok,
                        failed,
                    )
                if append_text:
                    text_ok, text_failed = await push_text(
                        self.context,
                        push_targets,
                        text=append_text,
                        event=event,
                    )
                    sent_count += text_ok
                    failed_count += text_failed
                    logger.warning("[%s] 追加文字推送完成: 成功 %d，失败 %d", PLUGIN_NAME, text_ok, text_failed)

            if send_to_groups and file_targets:
                logger.warning(
                    "[%s] 推送 %d 份分析日志文件到 %d 个群聊...",
                    PLUGIN_NAME,
                    len(report_artifacts),
                    len(file_targets),
                )
                for report in report_artifacts:
                    file_ok, file_failed = await push_log_file(
                        self.context,
                        file_targets,
                        log_path=report.log_path,
                        event=event,
                    )
                    logger.warning(
                        "[%s] %s分析日志文件推送完成: 成功 %d，失败 %d",
                        PLUGIN_NAME,
                        report.display_name or "",
                        file_ok,
                        file_failed,
                    )

            self.runtime.save_task_state(
                summary=summary,
                analysis=analysis,
                log_path=log_path,
                image_path=image_path,
                reports=[
                    {
                        "key": report.key,
                        "display_name": report.display_name,
                        "log_path": report.log_path,
                        "image_path": report.image_path,
                    }
                    for report in report_artifacts
                ],
                manual=manual,
                sent_to_groups=bool(send_to_groups and push_targets),
                target_groups=push_targets,
                sent_count=sent_count,
                failed_count=failed_count,
                token_usage=token_usage,
            )

            if not manual or send_to_groups:
                self.runtime.save_seen_commit(latest.sha)

            message = (
                f"发现更新：{short_sha(previous_sha)}...{short_sha(latest.sha)}，"
                f"共 {summary.total_files} 个文件，模型请求 {len(summary.chunks) + (1 if summary_model_enabled else 0)} 次，"
                f"并发 {self.settings.model_concurrency}，已生成 {len(report_artifacts)} 份报告。"
            )
            if send_to_groups:
                message += f" 推送成功 {sent_count}，失败 {failed_count}。"
            warning_log("[%s] 检查完成: %s", PLUGIN_NAME, message)
            result = {
                "message": message,
                "image_path": image_path,
                "log_path": log_path,
                "append_text": append_text,
                "reports": [
                    {
                        "key": report.key,
                        "display_name": report.display_name,
                        "image_path": report.image_path,
                        "log_path": report.log_path,
                        "fallback_text": report.fallback_text,
                    }
                    for report in report_artifacts
                ],
            }
            if image_path:
                return result
            result["message"] = fallback_text or message
            return result
