from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .wtup.analyzer import (
        analyze_chunks,
        fallback_analysis,
        merge_chunk_analyses,
        refine_chunk_analyses,
        refine_merged_analysis,
    )
    from .wtup.config import BRANCH_NAME, PLUGIN_NAME, PLUGIN_VERSION, REPO_FULL_NAME, PluginConfig, load_config
    from .wtup.diff_collector import DiffChunk, build_diff_summary, short_sha
    from .wtup.github_client import GitHubClient, GitHubRequestError
    from .wtup.notifier import push_report
    from .wtup.report_log import build_report_log_filename, sanitize_filename
    from .wtup.renderer import build_report_html, render_plain_text, render_report_image
    from .wtup.state_store import StateStore
except ImportError:
    from wtup.analyzer import (
        analyze_chunks,
        fallback_analysis,
        merge_chunk_analyses,
        refine_chunk_analyses,
        refine_merged_analysis,
    )
    from wtup.config import BRANCH_NAME, PLUGIN_NAME, PLUGIN_VERSION, REPO_FULL_NAME, PluginConfig, load_config
    from wtup.diff_collector import DiffChunk, build_diff_summary, short_sha
    from wtup.github_client import GitHubClient, GitHubRequestError
    from wtup.notifier import push_report
    from wtup.report_log import build_report_log_filename, sanitize_filename
    from wtup.renderer import build_report_html, render_plain_text, render_report_image
    from wtup.state_store import StateStore


COMMAND_RECEIVED_EMOJI_ID = "289"
COMMAND_DONE_EMOJI_ID = "124"


def _get_event_message_id(event: AstrMessageEvent) -> str:
    msg_obj = getattr(event, "message_obj", None)
    message_id = getattr(msg_obj, "message_id", None)
    if message_id is None:
        raw_message = getattr(msg_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            message_id = raw_message.get("message_id")
    return str(message_id or "").strip()


def _call_target(event: AstrMessageEvent) -> Any | None:
    bot = getattr(event, "bot", None) or getattr(event, "client", None)
    if hasattr(bot, "call_action"):
        return bot
    api = getattr(bot, "api", None)
    if hasattr(api, "call_action"):
        return api
    return None


def _event_is_admin(event: AstrMessageEvent) -> bool:
    is_admin = getattr(event, "is_admin", None)
    if not callable(is_admin):
        return False
    try:
        return bool(is_admin())
    except Exception as exc:
        logger.debug("[%s] 检查管理员权限失败: %s", PLUGIN_NAME, exc)
        return False


@register(PLUGIN_NAME, "御坂_20001", "War Thunder Datamine 更新监控插件", PLUGIN_VERSION)
class WTUpdatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.settings: PluginConfig = load_config(self.config)
        self.data_dir = self._resolve_data_dir()
        self.state_store = StateStore(self.data_dir / "state.json")
        self.image_dir = self.data_dir / "images"
        self.log_dir = self.data_dir / "logs"
        self.template_path = Path(__file__).resolve().parent / "templates" / "help_miku.html"
        self._task: asyncio.Task | None = None
        self._check_lock = asyncio.Lock()

    async def initialize(self):
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "[%s] 已启动，监控 %s@%s，间隔 %s 分钟，推送目标 %s 个",
            PLUGIN_NAME,
            REPO_FULL_NAME,
            BRANCH_NAME,
            self.settings.monitor_interval_minutes,
            len(self.settings.target_groups),
        )

    @filter.command("wtup_status")
    async def wtup_status(self, event: AstrMessageEvent):
        await self._react_to_command_received(event)
        repo_state = self.state_store.get_repo_state(REPO_FULL_NAME)
        last_sha = str(repo_state.get("last_commit_sha") or "")
        last_checked_at = repo_state.get("last_checked_at")
        last_checked_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(last_checked_at))) if last_checked_at else "未检查"
        lines = [
            "WT 更新监控状态",
            f"仓库: {REPO_FULL_NAME}",
            f"分支: {BRANCH_NAME}",
            f"最近提交: {short_sha(last_sha)}",
            f"上次检查: {last_checked_text}",
            f"检查间隔: {self.settings.monitor_interval_minutes} 分钟",
            f"推送目标: {len(self.settings.target_groups)} 个",
            f"单次模型请求文件限制: {self.settings.max_files_per_report or '不限制'}",
            f"单次模型请求字符限制: {self.settings.max_patch_chars or '不限制'}",
            f"模型请求并发数: {self.settings.model_concurrency}",
            f"二次分析: {'开启' if self.settings.enable_second_pass_analysis else '关闭'}",
        ]
        await self._react_to_command_done(event)
        yield event.plain_result("\n".join(lines))

    @filter.command("wtup_bind")
    async def wtup_bind(self, event: AstrMessageEvent):
        await self._react_to_command_received(event)
        origin = getattr(event, "unified_msg_origin", "") or ""
        if not origin:
            yield event.plain_result("当前会话没有 unified_msg_origin，无法绑定。")
            return
        await self._react_to_command_done(event)
        yield event.plain_result(
            "当前会话 unified_msg_origin：\n"
            f"{origin}\n\n"
            "后台配置的“推送群聊列表”支持填写群号或 unified_msg_origin，每行一个。"
        )

    @filter.command("wtup_check")
    async def wtup_check(self, event: AstrMessageEvent):
        await self._react_to_command_received(event)
        args = str(getattr(event, "message_str", "") or "")
        args_lower = args.lower()
        force_all = "强制全部" in args or ("force" in args_lower and "all" in args_lower)
        force_latest = force_all or "强制" in args or "force" in args_lower
        if force_all and not _event_is_admin(event):
            yield event.plain_result("权限不足：/wtup_check 强制全部 只能由 AstrBot 管理员执行。")
            return
        try:
            result = await self.check_once(manual=True, force_latest=force_latest, send_to_groups=force_all, event=event)
        except Exception as exc:
            logger.exception("[%s] 手动检查失败: %s", PLUGIN_NAME, exc)
            yield event.plain_result(f"检查失败：{exc}")
            return

        await self._react_to_command_done(event)
        if result.get("image_path") and not force_all:
            yield event.image_result(str(result["image_path"]))
            return
        yield event.plain_result(str(result.get("message") or "检查完成。"))

    async def _react_to_command(self, event: AstrMessageEvent, emoji_id: str, *, set_reaction: bool = True) -> bool:
        message_id = _get_event_message_id(event)
        if not message_id:
            return False

        try:
            message_id_int = int(message_id)
        except ValueError:
            return False

        call_target = _call_target(event)
        if call_target is None:
            return False

        try:
            await call_target.call_action(
                "set_msg_emoji_like",
                message_id=message_id_int,
                emoji_id=emoji_id,
                emoji_type="1",
                set=set_reaction,
            )
            return True
        except Exception as exc:
            logger.debug("[%s] 更新表情失败，可能当前平台或协议端不支持: %s", PLUGIN_NAME, exc)
            return False

    async def _react_to_command_received(self, event: AstrMessageEvent) -> bool:
        return await self._react_to_command(event, COMMAND_RECEIVED_EMOJI_ID)

    async def _react_to_command_done(self, event: AstrMessageEvent) -> bool:
        return await self._react_to_command(event, COMMAND_DONE_EMOJI_ID)

    async def _monitor_loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self.check_once(manual=False, force_latest=False, send_to_groups=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[%s] 定时检查失败: %s", PLUGIN_NAME, exc)
            await asyncio.sleep(self.settings.monitor_interval_minutes * 60)

    async def check_once(
        self,
        *,
        manual: bool,
        force_latest: bool,
        send_to_groups: bool,
        event: AstrMessageEvent | None = None,
    ) -> dict[str, Any]:
        async with self._check_lock:
            logger.info("[%s] ========== 开始执行检查%s ==========", PLUGIN_NAME, "（手动）" if manual else "（定时）")

            client = GitHubClient(token=self.settings.github_token)
            logger.info("[%s] 步骤 1/5: 获取最新 commit...", PLUGIN_NAME)
            latest = await asyncio.to_thread(client.get_latest_commit, REPO_FULL_NAME, BRANCH_NAME)
            logger.info("[%s] 已完成获取最新 commit: %s", PLUGIN_NAME, short_sha(latest.sha))
            if not latest.sha:
                return {"message": "未获取到最新 commit。"}

            repo_state = self.state_store.get_repo_state(REPO_FULL_NAME)
            previous_sha = str(repo_state.get("last_commit_sha") or "").strip()

            if not previous_sha:
                if force_latest and latest.parents:
                    previous_sha = latest.parents[0]
                else:
                    self._save_seen_commit(latest.sha)
                    logger.info("[%s] 首次检查，已建立基线: %s", PLUGIN_NAME, short_sha(latest.sha))
                    return {
                        "message": (
                            f"首次检查已建立基线：{short_sha(latest.sha)}。\n"
                            "定时任务不会推送历史更新；如需测试最新 commit，请使用 /wtup_check 强制。"
                        )
                    }

            if previous_sha == latest.sha and not force_latest:
                self._save_seen_commit(latest.sha)
                logger.info("[%s] 没有新 commit，跳过", PLUGIN_NAME)
                return {"message": f"没有新 commit，当前为 {short_sha(latest.sha)}。"}

            if force_latest and latest.parents:
                previous_sha = latest.parents[0]

            if send_to_groups and not self.settings.target_groups:
                return {"message": "发现新 commit，但未配置推送群聊列表，已跳过模型分析和推送。"}

            logger.info("[%s] 步骤 2/5: 对比 commits (%s...%s)...", PLUGIN_NAME, short_sha(previous_sha), short_sha(latest.sha))
            compare_payload = await asyncio.to_thread(client.compare_commits, REPO_FULL_NAME, previous_sha, latest.sha)
            logger.info("[%s] 已完成对比 commits，共 %d 个文件变更", PLUGIN_NAME, len(compare_payload.get("files", [])))

            logger.info("[%s] 步骤 3/5: 获取原始 diff...", PLUGIN_NAME)
            try:
                raw_diff_text = await asyncio.to_thread(client.compare_diff_text, REPO_FULL_NAME, previous_sha, latest.sha)
                logger.info("[%s] 已完成获取原始 diff", PLUGIN_NAME)
            except GitHubRequestError as exc:
                raw_diff_text = ""
                logger.warning("[%s] 获取原始 diff 失败，使用 compare API 文件列表兜底: %s", PLUGIN_NAME, exc)

            logger.info("[%s] 步骤 4/5: 构建 diff 摘要...", PLUGIN_NAME)
            summary = build_diff_summary(
                compare_payload,
                raw_diff_text=raw_diff_text,
                max_files=self.settings.max_files_per_report,
                max_chars=self.settings.max_patch_chars,
            )
            if not summary.head_sha:
                summary = build_diff_summary(
                    {**compare_payload, "sha": latest.sha},
                    raw_diff_text=raw_diff_text,
                    max_files=self.settings.max_files_per_report,
                    max_chars=self.settings.max_patch_chars,
                )
            logger.info("[%s] 已完成构建 diff 摘要，%d 个文件，拆分为 %d 次模型请求", PLUGIN_NAME, summary.total_files, len(summary.chunks))

            sent_count = 0
            failed_count = 0

            logger.info("[%s] 步骤 5/5: 分析并生成单份报告...", PLUGIN_NAME)
            chunk_results = await analyze_chunks(self.context, self.settings, summary)
            second_pass_enabled = self.settings.enable_second_pass_analysis and len(summary.chunks) > 1
            try:
                analysis = merge_chunk_analyses(summary, summary.chunks, chunk_results)
                if second_pass_enabled:
                    logger.info("[%s] 已启用二次分析，正在整理合并报告...", PLUGIN_NAME)
                    analysis = await refine_merged_analysis(self.context, self.settings, summary, analysis)
            except Exception as exc:
                logger.warning("[%s] 合并分片分析结果失败: %s", PLUGIN_NAME, exc)
                if second_pass_enabled:
                    logger.info("[%s] 已启用二次分析，改用分片原始分析 JSON 生成报告...", PLUGIN_NAME)
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
            image_path = await render_report_image(self, html_text, self.image_dir)
            fallback_text = render_plain_text(summary, report_chunk, analysis)
            log_path = self._save_report_log(summary, analysis, fallback_text, image_path=image_path)

            if send_to_groups and self.settings.target_groups:
                logger.info("[%s] 推送合并报告到 %d 个群聊...", PLUGIN_NAME, len(self.settings.target_groups))
                ok, failed = await push_report(
                    self.context,
                    self.settings.target_groups,
                    image_path=image_path,
                    fallback_text=fallback_text,
                    event=event,
                )
                sent_count += ok
                failed_count += failed
                logger.info("[%s] 推送完成合并报告: 成功 %d，失败 %d", PLUGIN_NAME, ok, failed)

            self._save_task_state(
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
                self._save_seen_commit(latest.sha)

            message = (
                f"发现更新：{short_sha(previous_sha)}...{short_sha(latest.sha)}，"
                f"共 {summary.total_files} 个文件，模型请求 {len(summary.chunks) + (1 if second_pass_enabled else 0)} 次，"
                f"并发 {self.settings.model_concurrency}，已合并为 1 份报告。"
            )
            if send_to_groups:
                message += f" 推送成功 {sent_count}，失败 {failed_count}。"
            logger.info("[%s] ========== 检查完成: %s ==========", PLUGIN_NAME, message)
            if image_path:
                return {"message": message, "image_path": image_path}
            return {"message": fallback_text or message}

    def _save_seen_commit(self, sha: str) -> None:
        self._update_repo_state(
            {
                "last_commit_sha": sha,
                "last_checked_at": time.time(),
                "branch": BRANCH_NAME,
            }
        )

    def _save_report_log(
        self,
        summary: Any,
        analysis: dict[str, Any],
        fallback_text: str,
        *,
        image_path: Path | None,
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
        if image_path:
            header.append(f"图片: {image_path}")
        header.extend(["", str(fallback_text or "").strip(), ""])

        self.log_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(header), encoding="utf-8")
        logger.info("[%s] 已保存最终报告日志: %s", PLUGIN_NAME, output_path)
        return output_path

    def _save_task_state(
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
        self._update_repo_state(updates)

    def _update_repo_state(self, updates: dict[str, Any]) -> None:
        repo_state = self.state_store.get_repo_state(REPO_FULL_NAME)
        repo_state.update(updates)
        self.state_store.update_repo_state(REPO_FULL_NAME, repo_state)

    def _resolve_data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return Path(__file__).resolve().parent / ".data"

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[%s] 插件已卸载", PLUGIN_NAME)
