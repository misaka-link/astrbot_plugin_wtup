from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .wtup.analyzer import analyze_chunk
    from .wtup.config import BRANCH_NAME, PLUGIN_NAME, PLUGIN_VERSION, REPO_FULL_NAME, PluginConfig, load_config
    from .wtup.diff_collector import build_diff_summary, short_sha
    from .wtup.github_client import GitHubClient, GitHubRequestError
    from .wtup.notifier import push_report
    from .wtup.renderer import build_report_html, render_plain_text, render_report_image
    from .wtup.state_store import StateStore
except ImportError:
    from wtup.analyzer import analyze_chunk
    from wtup.config import BRANCH_NAME, PLUGIN_NAME, PLUGIN_VERSION, REPO_FULL_NAME, PluginConfig, load_config
    from wtup.diff_collector import build_diff_summary, short_sha
    from wtup.github_client import GitHubClient, GitHubRequestError
    from wtup.notifier import push_report
    from wtup.renderer import build_report_html, render_plain_text, render_report_image
    from wtup.state_store import StateStore


@register(PLUGIN_NAME, "御坂_20001", "War Thunder Datamine 更新监控插件", PLUGIN_VERSION)
class WTUpdatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.settings: PluginConfig = load_config(self.config)
        self.data_dir = self._resolve_data_dir()
        self.state_store = StateStore(self.data_dir / "state.json")
        self.image_dir = self.data_dir / "images"
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
            f"文件限制: {self.settings.max_files_per_report or '不限制'}",
            f"字符限制: {self.settings.max_patch_chars or '不限制'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("wtup_bind")
    async def wtup_bind(self, event: AstrMessageEvent):
        origin = getattr(event, "unified_msg_origin", "") or ""
        if not origin:
            yield event.plain_result("当前会话没有 unified_msg_origin，无法绑定。")
            return
        yield event.plain_result(
            "当前会话 unified_msg_origin：\n"
            f"{origin}\n\n"
            "把这一行填入后台配置的“推送群聊列表”，每行一个。"
        )

    @filter.command("wtup_check")
    async def wtup_check(self, event: AstrMessageEvent):
        args = str(getattr(event, "message_str", "") or "")
        force_latest = "强制" in args or "force" in args.lower()
        try:
            result = await self.check_once(manual=True, force_latest=force_latest, send_to_groups=False)
        except Exception as exc:
            logger.exception("[%s] 手动检查失败: %s", PLUGIN_NAME, exc)
            yield event.plain_result(f"检查失败：{exc}")
            return

        if result.get("image_path"):
            yield event.image_result(str(result["image_path"]))
            return
        yield event.plain_result(str(result.get("message") or "检查完成。"))

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

    async def check_once(self, *, manual: bool, force_latest: bool, send_to_groups: bool) -> dict[str, Any]:
        async with self._check_lock:
            client = GitHubClient(token=self.settings.github_token)
            latest = await asyncio.to_thread(client.get_latest_commit, REPO_FULL_NAME, BRANCH_NAME)
            if not latest.sha:
                return {"message": "未获取到最新 commit。"}

            repo_state = self.state_store.get_repo_state(REPO_FULL_NAME)
            previous_sha = str(repo_state.get("last_commit_sha") or "").strip()

            if not previous_sha:
                if force_latest and latest.parents:
                    previous_sha = latest.parents[0]
                else:
                    self._save_seen_commit(latest.sha)
                    return {
                        "message": (
                            f"首次检查已建立基线：{short_sha(latest.sha)}。\n"
                            "定时任务不会推送历史更新；如需测试最新 commit，请使用 /wtup_check 强制。"
                        )
                    }

            if previous_sha == latest.sha and not force_latest:
                self._save_seen_commit(latest.sha)
                return {"message": f"没有新 commit，当前为 {short_sha(latest.sha)}。"}

            if force_latest and latest.parents:
                previous_sha = latest.parents[0]

            if send_to_groups and not self.settings.target_groups:
                return {"message": "发现新 commit，但未配置推送群聊列表，已跳过模型分析和推送。"}

            compare_payload = await asyncio.to_thread(client.compare_commits, REPO_FULL_NAME, previous_sha, latest.sha)
            try:
                raw_diff_text = await asyncio.to_thread(client.compare_diff_text, REPO_FULL_NAME, previous_sha, latest.sha)
            except GitHubRequestError as exc:
                raw_diff_text = ""
                logger.warning("[%s] 获取原始 diff 失败，使用 compare API 文件列表兜底: %s", PLUGIN_NAME, exc)
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

            sent_count = 0
            failed_count = 0
            first_image_path: Path | None = None
            first_text = ""

            for chunk in summary.chunks:
                analysis = await analyze_chunk(self.context, self.settings, summary, chunk)
                html_text = build_report_html(self.template_path, summary, chunk, analysis)
                image_path = await render_report_image(self, html_text, self.image_dir)
                fallback_text = render_plain_text(summary, chunk, analysis)
                if first_image_path is None:
                    first_image_path = image_path
                    first_text = fallback_text

                if send_to_groups and self.settings.target_groups:
                    ok, failed = await push_report(
                        self.context,
                        self.settings.target_groups,
                        image_path=image_path,
                        fallback_text=fallback_text,
                    )
                    sent_count += ok
                    failed_count += failed

            if not manual or send_to_groups:
                self._save_seen_commit(latest.sha)

            message = (
                f"发现更新：{short_sha(previous_sha)}...{short_sha(latest.sha)}，"
                f"共 {summary.total_files} 个文件，拆分 {len(summary.chunks)} 份。"
            )
            if send_to_groups:
                message += f" 推送成功 {sent_count}，失败 {failed_count}。"
            if first_image_path:
                return {"message": message, "image_path": first_image_path}
            return {"message": first_text or message}

    def _save_seen_commit(self, sha: str) -> None:
        self.state_store.update_repo_state(
            REPO_FULL_NAME,
            {
                "last_commit_sha": sha,
                "last_checked_at": time.time(),
                "branch": BRANCH_NAME,
            },
        )

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
