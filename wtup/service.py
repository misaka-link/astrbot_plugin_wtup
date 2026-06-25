from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from .analyzer import (
    analyze_chunks,
    enforce_change_coverage,
    fallback_analysis,
    estimate_chunk_input_tokens,
    merge_chunk_analyses,
    refine_chunk_analyses_with_usage,
    refine_merged_analysis_with_usage,
    review_analysis_with_usage,
    split_chunks_by_token_limit,
    sum_token_usage,
)
from .analysis_cache import AnalysisResultCache
from .config import BRANCH_NAME, PLUGIN_NAME, REPO_FULL_NAME, PluginConfig
from .diff_collector import DiffChunk, build_diff_summary, short_sha
from .github_cache import GitHubCache
from .github_client import GitHubClient, GitHubRequestError
from .notifier import push_admin_notification, push_log_file, push_report, push_text
from .renderer import build_report_html, render_plain_text, render_report_image
from .runtime import RuntimeState, ceil_minutes, format_elapsed_duration, warning_log
from .state_store import StateStore
from .analysis import TokenUsage
from .termination import check_task_termination, task_should_terminate


MAX_FORCE_COMMIT_COUNT = 5


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
    append_text: str = ""


def normalize_targets(targets: list[str] | None) -> list[str]:
    if not targets:
        return []
    return [str(target).strip() for target in targets if str(target or "").strip()]


def analysis_needs_review(analysis: dict[str, Any]) -> bool:
    tags = analysis.get("tags") if isinstance(analysis.get("tags"), list) else []
    if any(str(tag).strip() == "需复核" for tag in tags):
        return True
    summary = str(analysis.get("summary") or "")
    recommendation = str(analysis.get("recommendation") or "")
    return "需要结合 GitHub 原始 diff 复核" in f"{summary}\n{recommendation}"


def chunk_failure_reasons(results: list[ChunkAnalysis]) -> list[str]:
    reasons: list[str] = []
    for result in results:
        error = str(result.error or "").strip()
        if error:
            reasons.append(f"分片 {result.chunk_index}/{result.chunk_total}: {error}")
        elif analysis_needs_review(result.analysis):
            reason = str(result.analysis.get("summary") or "分片分析结果需要复核").strip()
            reasons.append(f"分片 {result.chunk_index}/{result.chunk_total}: {reason}")
    return reasons


def build_admin_failure_notice(
    *,
    summary: Any,
    analysis_failure_reasons: list[str],
    log_path: Path,
) -> str:
    reason_lines = "\n".join(f"- {reason}" for reason in analysis_failure_reasons[:8])
    if not reason_lines:
        reason_lines = "- 模型分析失败，未生成可推送报告。"
    return (
        "WT 更新分析失败，已跳过群推送。\n"
        f"仓库: {REPO_FULL_NAME}\n"
        f"提交范围: {short_sha(summary.base_sha)}...{short_sha(summary.head_sha)}\n"
        f"文件数: {summary.total_files}\n"
        "失败原因:\n"
        f"{reason_lines}\n"
        f"日志: {log_path}"
    )


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
        task_log_dir: Path | None = None,
        github_cache_dir: Path | None = None,
        analysis_cache_dir: Path | None = None,
        render_host: Any | None = None,
    ) -> None:
        self.context = context
        self.render_host = render_host if render_host is not None else self
        self.state_store = state_store
        self.image_dir = image_dir
        self.log_dir = log_dir
        self.error_dir = error_dir
        self.task_log_dir = task_log_dir if task_log_dir is not None else log_dir.parent / "task_logs"
        self.github_cache_dir = github_cache_dir if github_cache_dir is not None else log_dir.parent / "github_cache"
        self.analysis_cache_dir = (
            analysis_cache_dir if analysis_cache_dir is not None else log_dir.parent / "analysis_cache"
        )
        self.template_path = template_path
        self.runtime = RuntimeState(
            settings=settings,
            state_store=state_store,
            log_dir=log_dir,
            error_dir=error_dir,
            task_log_dir=self.task_log_dir,
        )
        self.github_cache = GitHubCache(self.github_cache_dir, task_log_recorder=self.runtime.record_task_log)
        self.analysis_cache = AnalysisResultCache(
            self.analysis_cache_dir,
            task_log_recorder=self.runtime.record_task_log,
        )
        self.settings = self.with_runtime_hooks(settings)
        self._check_lock = asyncio.Lock()

    def with_runtime_hooks(self, settings: PluginConfig) -> PluginConfig:
        settings = self.runtime.with_runtime_hooks(settings)
        settings = replace(settings, github_cache_dir=self.github_cache_dir)
        self.runtime.settings = settings
        return settings

    async def check_once(
        self,
        *,
        manual: bool,
        force_latest: bool,
        send_to_groups: bool,
        event: Any | None = None,
        target_groups: list[str] | None = None,
        analysis_file_groups: list[str] | None = None,
        force_commit_count: int = 1,
    ) -> dict[str, Any]:
        async with self._check_lock:
            started_at = time.monotonic()
            force_commit_count = max(1, min(MAX_FORCE_COMMIT_COUNT, int(force_commit_count or 1)))
            task_log_path = self.runtime.start_task_log(
                manual=manual,
                force_latest=force_latest,
                send_to_groups=send_to_groups,
            )
            warning_log("[%s] 开始执行检查%s", PLUGIN_NAME, "（手动）" if manual else "（定时）")
            push_targets = normalize_targets(target_groups) if target_groups is not None else list(self.settings.target_groups)
            file_targets = (
                normalize_targets(analysis_file_groups)
                if analysis_file_groups is not None
                else list(self.settings.analysis_file_groups)
            )
            self.runtime.record_task_log(
                "任务目标",
                {
                    "报告推送目标数": len(push_targets),
                    "日志文件推送目标数": len(file_targets),
                    "强制提交数量": force_commit_count if force_latest else 0,
                },
            )
            check_task_termination(self.settings, "任务开始")

            client = GitHubClient(
                token=self.settings.github_token,
                max_retries=self.settings.github_max_retry_count,
                should_terminate=lambda: task_should_terminate(self.settings),
            )
            logger.warning("[%s] 步骤 1/5: 获取最新 commit...", PLUGIN_NAME)
            self.runtime.record_task_log("步骤 1/5 开始", {"内容": "获取最新 commit"})
            check_task_termination(self.settings, "获取最新 commit 前")
            latest = await asyncio.to_thread(client.get_latest_commit, REPO_FULL_NAME, BRANCH_NAME)
            check_task_termination(self.settings, "获取最新 commit 后")
            logger.warning("[%s] 已完成获取最新 commit: %s", PLUGIN_NAME, short_sha(latest.sha))
            self.runtime.record_task_log("步骤 1/5 完成", {"最新提交": short_sha(latest.sha)})
            if not latest.sha:
                message = "未获取到最新 commit。"
                self.runtime.finish_task_log(
                    status="失败",
                    message=message,
                    elapsed_seconds=time.monotonic() - started_at,
                )
                return {"message": message, "task_log_path": task_log_path}

            repo_state = self.state_store.get_repo_state(REPO_FULL_NAME)
            previous_sha = str(repo_state.get("last_commit_sha") or "").strip()

            if not previous_sha:
                if force_latest and latest.parents:
                    previous_sha = latest.parents[0]
                else:
                    self.runtime.save_seen_commit(latest.sha)
                    logger.warning("[%s] 首次检查，已建立基线: %s", PLUGIN_NAME, short_sha(latest.sha))
                    message = (
                        f"首次检查已建立基线：{short_sha(latest.sha)}。\n"
                        "定时任务不会推送历史更新；如需测试最新 commit，请使用 /wtup_check 强制。"
                    )
                    self.runtime.record_task_log("建立基线", {"提交": short_sha(latest.sha)})
                    self.runtime.finish_task_log(
                        status="跳过",
                        message=message,
                        elapsed_seconds=time.monotonic() - started_at,
                    )
                    return {"message": message, "task_log_path": task_log_path}

            if previous_sha == latest.sha and not force_latest:
                self.runtime.save_seen_commit(latest.sha)
                logger.warning("[%s] 没有新 commit，跳过", PLUGIN_NAME)
                message = f"没有新 commit，当前为 {short_sha(latest.sha)}。"
                self.runtime.record_task_log("跳过分析", {"原因": "没有新 commit", "当前提交": short_sha(latest.sha)})
                self.runtime.finish_task_log(
                    status="跳过",
                    message=message,
                    elapsed_seconds=time.monotonic() - started_at,
                )
                return {"message": message, "task_log_path": task_log_path}

            if force_latest and latest.parents:
                if force_commit_count <= 1:
                    previous_sha = latest.parents[0]
                else:
                    logger.warning("[%s] 强制分析最新 %d 个 commit，获取提交列表...", PLUGIN_NAME, force_commit_count)
                    self.runtime.record_task_log(
                        "获取强制提交范围",
                        {"提交数量": force_commit_count, "最新提交": short_sha(latest.sha)},
                    )
                    check_task_termination(self.settings, "获取强制提交范围前")
                    recent_commits = await asyncio.to_thread(
                        client.get_recent_commits,
                        REPO_FULL_NAME,
                        BRANCH_NAME,
                        force_commit_count,
                    )
                    check_task_termination(self.settings, "获取强制提交范围后")
                    if len(recent_commits) < force_commit_count:
                        message = f"只获取到 {len(recent_commits)} 个最新 commit，无法强制分析最新 {force_commit_count} 个 commit。"
                        self.runtime.finish_task_log(
                            status="失败",
                            message=message,
                            elapsed_seconds=time.monotonic() - started_at,
                        )
                        return {"message": message, "task_log_path": task_log_path}
                    oldest_forced = recent_commits[force_commit_count - 1]
                    if oldest_forced.parents:
                        previous_sha = oldest_forced.parents[0]
                    else:
                        message = f"最新第 {force_commit_count} 个 commit 没有父提交，无法确定对比起点。"
                        self.runtime.finish_task_log(
                            status="失败",
                            message=message,
                            elapsed_seconds=time.monotonic() - started_at,
                        )
                        return {"message": message, "task_log_path": task_log_path}
                    self.runtime.record_task_log(
                        "强制提交范围",
                        {
                            "提交数量": force_commit_count,
                            "起点": short_sha(previous_sha),
                            "终点": short_sha(latest.sha),
                        },
                    )

            if send_to_groups and not push_targets:
                message = "发现新 commit，但未配置推送群聊列表，已跳过模型分析和推送。"
                self.runtime.record_task_log("跳过分析", {"原因": "未配置推送群聊列表"})
                self.runtime.finish_task_log(
                    status="跳过",
                    message=message,
                    elapsed_seconds=time.monotonic() - started_at,
                )
                return {"message": message, "task_log_path": task_log_path}

            logger.warning("[%s] 步骤 2/5: 对比 commits (%s...%s)...", PLUGIN_NAME, short_sha(previous_sha), short_sha(latest.sha))
            self.runtime.record_task_log(
                "步骤 2/5 开始",
                {
                    "内容": "对比 commits",
                    "提交范围": f"{short_sha(previous_sha)}...{short_sha(latest.sha)}",
                },
            )
            check_task_termination(self.settings, "对比 commits 前")
            compare_result = await asyncio.to_thread(
                self.github_cache.get_compare,
                REPO_FULL_NAME,
                previous_sha,
                latest.sha,
                lambda: client.compare_commits(REPO_FULL_NAME, previous_sha, latest.sha),
            )
            check_task_termination(self.settings, "对比 commits 后")
            compare_payload = compare_result.value
            logger.warning("[%s] 已完成对比 commits，共 %d 个文件变更", PLUGIN_NAME, len(compare_payload.get("files", [])))
            self.runtime.record_task_log(
                "步骤 2/5 完成",
                {"文件变更数": len(compare_payload.get("files", [])), "来源": compare_result.source},
            )

            logger.warning("[%s] 步骤 3/5: 获取原始 diff...", PLUGIN_NAME)
            self.runtime.record_task_log("步骤 3/5 开始", {"内容": "获取原始 diff"})
            try:
                check_task_termination(self.settings, "获取原始 diff 前")
                diff_result = await asyncio.to_thread(
                    self.github_cache.get_diff,
                    REPO_FULL_NAME,
                    previous_sha,
                    latest.sha,
                    lambda: client.compare_diff_text(REPO_FULL_NAME, previous_sha, latest.sha),
                )
                check_task_termination(self.settings, "获取原始 diff 后")
                raw_diff_text = diff_result.value
                logger.warning("[%s] 已完成获取原始 diff", PLUGIN_NAME)
                self.runtime.record_task_log(
                    "步骤 3/5 完成",
                    {"原始 diff 字符数": len(raw_diff_text), "来源": diff_result.source},
                )
            except GitHubRequestError as exc:
                raw_diff_text = ""
                logger.warning("[%s] 获取原始 diff 失败，使用 compare API 文件列表兜底: %s", PLUGIN_NAME, exc)
                self.runtime.record_task_log(
                    "步骤 3/5 失败后兜底",
                    {"原因": str(exc), "兜底": "使用 compare API 文件列表"},
                )

            logger.warning("[%s] 步骤 4/5: 构建 diff 摘要...", PLUGIN_NAME)
            self.runtime.record_task_log("步骤 4/5 开始", {"内容": "构建 diff 摘要"})
            check_task_termination(self.settings, "构建 diff 摘要前")
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
            check_task_termination(self.settings, "构建 diff 摘要后")
            input_token_count = sum(estimate_chunk_input_tokens(self.settings, summary, chunk) for chunk in summary.chunks)
            logger.warning(
                "[%s] 已完成构建 diff 摘要，%d 个文件，拆分为 %d 次模型请求，预计输入 %d token",
                PLUGIN_NAME,
                summary.total_files,
                len(summary.chunks),
                input_token_count,
            )
            self.runtime.record_task_log(
                "步骤 4/5 完成",
                {
                    "文件数": summary.total_files,
                    "模型请求分片数": len(summary.chunks),
                    "预计输入token": input_token_count,
                },
            )

            sent_count = 0
            failed_count = 0
            report_sent_count = 0
            report_failed_count = 0
            file_sent_count = 0
            file_failed_count = 0
            admin_sent_count = 0
            admin_failed_count = 0

            logger.warning("[%s] 步骤 5/5: 分析并生成报告...", PLUGIN_NAME)
            self.runtime.record_task_log("步骤 5/5 开始", {"内容": "分析并生成报告"})
            check_task_termination(self.settings, "模型分析前")
            summary_model_enabled = self.settings.enable_summary_model
            analysis_cache_entry = self.analysis_cache.read(settings=self.settings, repo=REPO_FULL_NAME, summary=summary)
            analysis_source = "cache" if analysis_cache_entry is not None else "model"
            if analysis_cache_entry is not None:
                analysis = analysis_cache_entry.analysis
                merged_analysis = analysis_cache_entry.merged_analysis
                token_usage = analysis_cache_entry.token_usage
                pre_summary_token_usage = analysis_cache_entry.pre_summary_token_usage
                analysis_model_route = analysis_cache_entry.analysis_model_route
                summary_model_route = analysis_cache_entry.summary_model_route
                analysis_failure_reasons: list[str] = []
                logger.warning(
                    "[%s] 分析结果缓存命中，跳过大模型请求: %s",
                    PLUGIN_NAME,
                    analysis_cache_entry.directory,
                )
                self.runtime.record_task_log(
                    "模型分析跳过",
                    {
                        "原因": "分析结果缓存命中",
                        "缓存目录": str(analysis_cache_entry.directory),
                        "缓存特征": analysis_cache_entry.key,
                    },
                )
            else:
                chunk_results = await analyze_chunks(self.context, self.settings, summary)
                check_task_termination(self.settings, "模型分片分析后")
                token_usage = sum_token_usage(result.token_usage for result in chunk_results)
                analysis_failure_reasons = chunk_failure_reasons(chunk_results)
                pre_summary_token_usage = token_usage
                analysis_model_route = self.runtime.current_model_provider_route("analysis")
                summary_model_route: list[str] = []
                merged_analysis: dict[str, Any] | None = None
                try:
                    analysis = merge_chunk_analyses(summary, summary.chunks, chunk_results)
                    merged_analysis = analysis
                    if summary_model_enabled:
                        logger.warning("[%s] 已启动总结模型，正在整理合并报告...", PLUGIN_NAME)
                        self.runtime.record_task_log("总结模型开始", {"内容": "整理合并报告"})
                        analysis, summary_usage = await refine_merged_analysis_with_usage(
                            self.context,
                            self.settings,
                            summary,
                            analysis,
                        )
                        check_task_termination(self.settings, "总结模型分析后")
                        token_usage += summary_usage
                        summary_model_route = self.runtime.current_model_provider_route("summary")
                except Exception as exc:
                    logger.warning("[%s] 合并分片分析结果失败: %s", PLUGIN_NAME, exc)
                    analysis_failure_reasons.append(f"合并分片分析结果失败: {exc}")
                    if summary_model_enabled:
                        logger.warning("[%s] 已启动总结模型，改用分片原始分析 JSON 生成报告...", PLUGIN_NAME)
                        self.runtime.record_task_log(
                            "总结模型开始",
                            {
                                "内容": "合并失败后改用分片原始分析 JSON 生成报告",
                                "合并错误": str(exc),
                            },
                        )
                        analysis, summary_usage = await refine_chunk_analyses_with_usage(
                            self.context,
                            self.settings,
                            summary,
                            summary.chunks,
                            chunk_results,
                            merge_error=str(exc),
                        )
                        check_task_termination(self.settings, "总结模型兜底分析后")
                        token_usage += summary_usage
                        summary_model_route = self.runtime.current_model_provider_route("summary")
                    else:
                        analysis = fallback_analysis("程序合并分片分析结果失败，需要结合 GitHub 原始 diff 复核。")
                if self.settings.enable_review_model:
                    logger.warning("[%s] 已启动监督模型，正在复核最终报告...", PLUGIN_NAME)
                    self.runtime.record_task_log(
                        "监督模型开始",
                        {
                            "模式": self.settings.review_mode,
                            "节能模型": self.settings.effective_review_provider_id or "默认模型",
                            "质量模型": self.settings.effective_review_quality_provider_id or "默认模型",
                        },
                    )
                    review_result = await review_analysis_with_usage(
                        self.context,
                        self.settings,
                        summary,
                        analysis,
                    )
                    check_task_termination(self.settings, "监督模型复核后")
                    analysis = review_result.analysis
                    token_usage += review_result.token_usage
                    self.runtime.record_task_log(
                        "监督模型完成",
                        {
                            "实际模式": review_result.mode_used,
                            "发现问题数": len(review_result.issues),
                            "发现问题": review_result.issues,
                            "采用修正版": "是" if review_result.applied_revision else "否",
                            "局部修正尝试": review_result.revision_stats.get("attempted", 0),
                            "局部修正成功": review_result.revision_stats.get("applied", 0),
                            "局部修正拒绝": review_result.revision_stats.get("rejected", 0),
                            "局部修正拒绝原因": review_result.revision_stats.get("rejected_reasons", []),
                            "真实总token": review_result.token_usage.total_tokens,
                        },
                    )
            if analysis_needs_review(analysis) and not analysis_failure_reasons:
                analysis_failure_reasons.append(str(analysis.get("summary") or "模型分析结果需要复核").strip())
            analysis_failed = bool(analysis_failure_reasons)
            analysis = enforce_change_coverage(summary, summary.chunks, analysis)
            if merged_analysis is not None:
                merged_analysis = enforce_change_coverage(summary, summary.chunks, merged_analysis)
            coverage = analysis.get("coverage") if isinstance(analysis.get("coverage"), dict) else {}
            if coverage:
                self.runtime.record_task_log(
                    "变更覆盖检查",
                    {
                        "应覆盖变更点": coverage.get("expected", 0),
                        "已覆盖变更点": coverage.get("covered", 0),
                        "未覆盖变更点": coverage.get("missing", 0),
                        "未覆盖编号": coverage.get("missing_source_ids", []),
                    },
                )
            if not analysis_failed and analysis_source == "model":
                self.analysis_cache.write(
                    settings=self.settings,
                    repo=REPO_FULL_NAME,
                    summary=summary,
                    analysis=analysis,
                    merged_analysis=merged_analysis,
                    token_usage=token_usage,
                    pre_summary_token_usage=pre_summary_token_usage,
                    analysis_model_route=analysis_model_route,
                    summary_model_route=summary_model_route,
                )
            logger.warning(
                "[%s] 模型真实 token 消耗: total=%d, prompt=%d, completion=%d",
                PLUGIN_NAME,
                token_usage.total_tokens,
                token_usage.prompt_tokens,
                token_usage.completion_tokens,
            )
            self.runtime.record_task_log(
                "模型分析完成",
                {
                    "来源": "缓存" if analysis_source == "cache" else "模型请求",
                    "失败原因数": len(analysis_failure_reasons),
                    "真实总token": token_usage.total_tokens,
                    "真实输入token": token_usage.prompt_tokens,
                    "真实输出token": token_usage.completion_tokens,
                },
            )
            report_chunk = DiffChunk(
                index=1,
                total=1,
                files=summary.files,
                patch_chars=sum(chunk.patch_chars for chunk in summary.chunks),
            )
            report_artifact_dirname = self.analysis_cache.directory_for(
                settings=self.settings,
                repo=REPO_FULL_NAME,
                summary=summary,
            ).name
            report_specs: list[tuple[str, str, dict[str, Any], str, TokenUsage]] = []
            if self.settings.enable_pre_summary_report and summary_model_enabled and merged_analysis is not None:
                report_specs.append(
                    ("pre_summary", "总分析模型分析前", merged_analysis, "总分析前", pre_summary_token_usage)
                )
            post_label = "总分析模型分析后" if report_specs else ""
            post_suffix = "总分析后" if report_specs else ""
            report_specs.append(("final", post_label, analysis, post_suffix, token_usage))
            report_artifacts: list[ReportArtifact] = []
            log_cleanup_keep = None if self.settings.max_saved_artifacts <= 0 else self.settings.max_saved_artifacts
            file_cleanup_keep = (
                None
                if self.settings.max_saved_artifacts <= 0
                else max(self.settings.max_saved_artifacts, len(report_specs))
            )
            for key, display_name, report_analysis, filename_suffix, report_token_usage in report_specs:
                check_task_termination(self.settings, f"生成报告文件前: {display_name or '合并报告'}")
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
                    artifact_dirname=report_artifact_dirname,
                    filename_suffix=filename_suffix,
                    display_name=display_name,
                    cleanup_keep=log_cleanup_keep,
                    token_usage=report_token_usage,
                )
                self.runtime.record_task_log(
                    "报告文件生成",
                    {
                        "报告类型": display_name or "合并报告",
                        "图片路径": str(image_path) if image_path else "",
                        "报告日志路径": str(log_path),
                        "报告总token": report_token_usage.total_tokens,
                    },
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
                check_task_termination(self.settings, f"生成报告文件后: {display_name or '合并报告'}")
            self.runtime.cleanup_saved_artifacts(self.image_dir, keep=file_cleanup_keep)
            final_report = report_artifacts[-1]
            image_path = final_report.image_path
            fallback_text = final_report.fallback_text
            log_path = final_report.log_path
            elapsed_seconds = time.monotonic() - started_at
            elapsed_minutes = ceil_minutes(elapsed_seconds)
            elapsed_duration = format_elapsed_duration(elapsed_seconds)
            append_text = ""
            if self.settings.enable_push_append_text:
                for report in report_artifacts:
                    report.append_text = self.runtime.build_push_append_text(
                        analysis=report.analysis,
                        token_count=report.token_usage.total_tokens,
                        elapsed_minutes=elapsed_minutes,
                        elapsed_duration=elapsed_duration,
                        summary_model_enabled=summary_model_enabled and report.key == "final",
                        analysis_model_route=analysis_model_route,
                        summary_model_route=summary_model_route,
                    )
                append_text = final_report.append_text

            if analysis_failed:
                notice_text = build_admin_failure_notice(
                    summary=summary,
                    analysis_failure_reasons=analysis_failure_reasons,
                    log_path=log_path,
                )
                self.runtime.record_task_log(
                    "分析失败",
                    {
                        "失败原因": analysis_failure_reasons[:8],
                        "跳过群推送": "是",
                    },
                )
                if self.settings.admin_targets:
                    logger.warning(
                        "[%s] 分析失败，跳过群推送，私发通知 %d 个管理员...",
                        PLUGIN_NAME,
                        len(self.settings.admin_targets),
                    )
                    admin_sent_count, admin_failed_count = await push_admin_notification(
                        self.context,
                        self.settings.admin_targets,
                        text=notice_text,
                        event=event,
                        should_terminate=lambda: task_should_terminate(self.settings),
                    )
                    logger.warning(
                        "[%s] 管理员通知完成: 成功 %d，失败 %d",
                        PLUGIN_NAME,
                        admin_sent_count,
                        admin_failed_count,
                    )
                    self.runtime.record_task_log(
                        "管理员通知完成",
                        {"成功": admin_sent_count, "失败": admin_failed_count},
                    )
                else:
                    logger.warning("[%s] 分析失败，已跳过群推送，但未配置管理员列表，无法私发通知", PLUGIN_NAME)
                    self.runtime.record_task_log("管理员通知跳过", {"原因": "未配置管理员列表"})

            if not analysis_failed and send_to_groups and push_targets:
                logger.warning(
                    "[%s] 推送 %d 份报告到 %d 个群聊...",
                    PLUGIN_NAME,
                    len(report_artifacts),
                    len(push_targets),
                )
                self.runtime.record_task_log(
                    "报告推送开始",
                    {"报告份数": len(report_artifacts), "目标群数": len(push_targets)},
                )
                for report in report_artifacts:
                    check_task_termination(self.settings, f"报告推送前: {report.display_name or '合并报告'}")
                    ok, failed = await push_report(
                        self.context,
                        push_targets,
                        image_path=report.image_path,
                        fallback_text=report.fallback_text,
                        event=event,
                        should_terminate=lambda: task_should_terminate(self.settings),
                    )
                    sent_count += ok
                    failed_count += failed
                    report_sent_count += ok
                    report_failed_count += failed
                    logger.warning(
                        "[%s] 推送完成%s: 成功 %d，失败 %d",
                        PLUGIN_NAME,
                        report.display_name or "合并报告",
                        ok,
                        failed,
                    )
                    self.runtime.record_task_log(
                        "报告推送完成",
                        {
                            "报告类型": report.display_name or "合并报告",
                            "成功": ok,
                            "失败": failed,
                        },
                    )
                    if report.append_text:
                        check_task_termination(self.settings, f"追加文字推送前: {report.display_name or '合并报告'}")
                        text_ok, text_failed = await push_text(
                            self.context,
                            push_targets,
                            text=report.append_text,
                            event=event,
                            should_terminate=lambda: task_should_terminate(self.settings),
                        )
                        sent_count += text_ok
                        failed_count += text_failed
                        logger.warning(
                            "[%s] %s追加文字推送完成: 成功 %d，失败 %d",
                            PLUGIN_NAME,
                            report.display_name or "合并报告",
                            text_ok,
                            text_failed,
                        )
                        self.runtime.record_task_log(
                            "追加文字推送完成",
                            {
                                "报告类型": report.display_name or "合并报告",
                                "成功": text_ok,
                                "失败": text_failed,
                            },
                        )

            if not analysis_failed and send_to_groups and file_targets:
                logger.warning(
                    "[%s] 推送 %d 份分析日志文件到 %d 个群聊...",
                    PLUGIN_NAME,
                    len(report_artifacts),
                    len(file_targets),
                )
                self.runtime.record_task_log(
                    "分析日志文件推送开始",
                    {"报告份数": len(report_artifacts), "目标群数": len(file_targets)},
                )
                for report in report_artifacts:
                    check_task_termination(self.settings, f"分析日志文件推送前: {report.display_name or '合并报告'}")
                    file_ok, file_failed = await push_log_file(
                        self.context,
                        file_targets,
                        log_path=report.log_path,
                        event=event,
                        should_terminate=lambda: task_should_terminate(self.settings),
                    )
                    sent_count += file_ok
                    failed_count += file_failed
                    file_sent_count += file_ok
                    file_failed_count += file_failed
                    logger.warning(
                        "[%s] %s分析日志文件推送完成: 成功 %d，失败 %d",
                        PLUGIN_NAME,
                        report.display_name or "",
                        file_ok,
                        file_failed,
                    )
                    self.runtime.record_task_log(
                        "分析日志文件推送完成",
                        {
                            "报告类型": report.display_name or "合并报告",
                            "成功": file_ok,
                            "失败": file_failed,
                        },
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
                sent_to_groups=send_to_groups and not analysis_failed and bool(report_artifacts),
                target_groups=push_targets,
                sent_count=sent_count,
                failed_count=failed_count,
                token_usage=token_usage,
                task_log_path=task_log_path,
            )

            reports_generated = not analysis_failed and bool(report_artifacts)
            should_mark_seen = (not manual and not send_to_groups) or (send_to_groups and reports_generated)
            if should_mark_seen:
                self.runtime.save_seen_commit(latest.sha)
            elif analysis_failed:
                logger.warning(
                    "[%s] 本次分析失败，保留 commit 进度在 %s，下次循环继续重试 %s",
                    PLUGIN_NAME,
                    short_sha(previous_sha),
                    short_sha(latest.sha),
                )
            elif send_to_groups and push_targets:
                logger.warning(
                    "[%s] 本次报告未成功发送到任何目标，保留 commit 进度在 %s，下次循环继续重试 %s",
                    PLUGIN_NAME,
                    short_sha(previous_sha),
                    short_sha(latest.sha),
                )

            model_request_count = (
                0 if analysis_source == "cache" else len(summary.chunks) + (1 if summary_model_enabled else 0)
            )
            source_text = "分析缓存命中，未调用大模型" if analysis_source == "cache" else "已调用大模型"
            message = (
                f"发现更新：{short_sha(previous_sha)}...{short_sha(latest.sha)}，"
                f"共 {summary.total_files} 个文件，模型请求 {model_request_count} 次，"
                f"并发 {self.settings.model_concurrency}，{source_text}，已生成 {len(report_artifacts)} 份报告。"
            )
            if send_to_groups:
                if analysis_failed:
                    message += (
                        f" 分析失败，已跳过群推送，管理员通知成功 {admin_sent_count}，失败 {admin_failed_count}。"
                    )
                    if not self.settings.admin_targets:
                        message += " 未配置管理员列表。"
                else:
                    message += f" 推送成功 {sent_count}，失败 {failed_count}。"
            warning_log("[%s] 检查完成: %s", PLUGIN_NAME, message)
            result = {
                "message": message,
                "image_path": image_path,
                "log_path": log_path,
                "task_log_path": task_log_path,
                "append_text": append_text,
                "analysis_failed": analysis_failed,
                "admin_sent_count": admin_sent_count,
                "admin_failed_count": admin_failed_count,
                "commit_marked_complete": should_mark_seen,
                "report_sent_count": report_sent_count,
                "report_failed_count": report_failed_count,
                "file_sent_count": file_sent_count,
                "file_failed_count": file_failed_count,
                "analysis_source": analysis_source,
                "reports": [
                    {
                        "key": report.key,
                        "display_name": report.display_name,
                        "image_path": report.image_path,
                        "log_path": report.log_path,
                        "fallback_text": report.fallback_text,
                        "append_text": report.append_text,
                    }
                    for report in report_artifacts
                ],
            }
            self.runtime.finish_task_log(
                status="完成（分析失败）" if analysis_failed else "完成",
                message=message,
                elapsed_seconds=time.monotonic() - started_at,
            )
            if image_path:
                return result
            if send_to_groups:
                return result
            result["message"] = fallback_text or message
            return result
