from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .wtup.config import BRANCH_NAME, PLUGIN_NAME, PLUGIN_VERSION, REPO_FULL_NAME, PluginConfig, load_config
    from .wtup.diff_collector import short_sha
    from .wtup.runtime import RuntimeState
    from .wtup.service import UpdateCheckService
    from .wtup.state_store import StateStore
except ImportError:
    from wtup.config import BRANCH_NAME, PLUGIN_NAME, PLUGIN_VERSION, REPO_FULL_NAME, PluginConfig, load_config
    from wtup.diff_collector import short_sha
    from wtup.runtime import RuntimeState
    from wtup.service import UpdateCheckService
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
        logger.warning("[%s] 检查管理员权限失败: %s", PLUGIN_NAME, exc)
        return False


def _normalize_group_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def _get_event_group_id(event: AstrMessageEvent) -> str:
    for method_name in ("get_group_id",):
        method = getattr(event, method_name, None)
        if not callable(method):
            continue
        try:
            group_id = _normalize_group_id(method())
        except Exception as exc:
            logger.warning("[%s] 获取当前群号失败: %s", PLUGIN_NAME, exc)
            group_id = ""
        if group_id:
            return group_id

    message_obj = getattr(event, "message_obj", None)
    candidates = [
        getattr(event, "group_id", None),
        getattr(event, "group", None),
        getattr(message_obj, "group_id", None),
        getattr(message_obj, "group", None),
    ]
    for candidate in candidates:
        group_id = _normalize_group_id(candidate)
        if group_id:
            return group_id

    raw_message = getattr(message_obj, "raw_message", None)
    if isinstance(raw_message, dict):
        for key in ("group_id", "group", "gid"):
            group_id = _normalize_group_id(raw_message.get(key))
            if group_id:
                return group_id

    origin = str(getattr(event, "unified_msg_origin", "") or "")
    if "group" in origin.lower():
        match = re.search(r"(\d+)(?!.*\d)", origin)
        if match:
            return match.group(1)
    return ""


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
        self.error_dir = self.data_dir / "errors"
        self.template_path = Path(__file__).resolve().parent / "templates" / "help_miku.html"
        self._task: asyncio.Task | None = None
        self.service = UpdateCheckService(
            context=self.context,
            settings=self.settings,
            state_store=self.state_store,
            image_dir=self.image_dir,
            log_dir=self.log_dir,
            error_dir=self.error_dir,
            template_path=self.template_path,
            render_host=self,
        )
        self.settings = self.service.with_runtime_hooks(self.settings)
        self.service.settings = self.settings

    async def initialize(self):
        self._task = asyncio.create_task(self._monitor_loop())
        logger.warning(
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
            f"管理员通知目标: {len(self.settings.admin_targets)} 个",
            f"单次模型请求文件限制: {self.settings.max_files_per_report or '不限制'}",
            f"单次模型请求 token 输入限制: {self.settings.max_input_token_limit or '不限制'}",
            f"模型请求并发数: {self.settings.model_concurrency}",
            f"总结模型: {'启动' if self.settings.enable_summary_model else '关闭'}",
            f"备用模型: {len(self.settings.backup_provider_ids)} 个",
            f"分析前报告: {'生成' if self.settings.enable_pre_summary_report else '关闭'}",
            f"最大重试次数: {self.settings.max_retry_count}",
            f"文件保留数量: {self.settings.max_saved_artifacts or '不限制'}",
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
        force_current_group = force_latest and not force_all
        if force_all and not _event_is_admin(event):
            yield event.plain_result("权限不足：/wtup_check 强制全部 只能由 AstrBot 管理员执行。")
            return
        target_groups = None
        analysis_file_groups = None
        send_to_groups = force_all
        if force_current_group:
            current_group_id = _get_event_group_id(event)
            if not current_group_id:
                yield event.plain_result("当前会话不是可识别的群聊，无法把强制分析报告上传到当前群。")
                return
            target_groups = [current_group_id]
            analysis_file_groups = [current_group_id]
            send_to_groups = True
        try:
            result = await self.check_once(
                manual=True,
                force_latest=force_latest,
                send_to_groups=send_to_groups,
                event=event,
                target_groups=target_groups,
                analysis_file_groups=analysis_file_groups,
            )
        except Exception as exc:
            logger.warning("[%s] 手动检查失败: %s", PLUGIN_NAME, exc, exc_info=True)
            yield event.plain_result(f"检查失败：{exc}")
            return

        await self._react_to_command_done(event)
        if result.get("analysis_failed") and _get_event_group_id(event):
            return
        if not send_to_groups:
            reports = result.get("reports") if isinstance(result.get("reports"), list) else []
            if reports:
                sent_report_append_text = False
                for report in reports:
                    if not isinstance(report, dict):
                        continue
                    image_path = report.get("image_path")
                    if image_path:
                        yield event.image_result(str(image_path))
                    else:
                        yield event.plain_result(str(report.get("fallback_text") or ""))
                    report_append_text = str(report.get("append_text") or "").strip()
                    if report_append_text:
                        sent_report_append_text = True
                        yield event.plain_result(report_append_text)
                append_text = str(result.get("append_text") or "").strip()
                if append_text and not sent_report_append_text:
                    yield event.plain_result(append_text)
                return
            if result.get("image_path"):
                yield event.image_result(str(result["image_path"]))
                append_text = str(result.get("append_text") or "").strip()
                if append_text:
                    yield event.plain_result(append_text)
                return
            append_text = str(result.get("append_text") or "").strip()
            if append_text:
                yield event.plain_result(append_text)
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
            logger.warning("[%s] 更新表情失败，可能当前平台或协议端不支持: %s", PLUGIN_NAME, exc)
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
                logger.warning("[%s] 定时检查失败: %s", PLUGIN_NAME, exc, exc_info=True)
            await asyncio.sleep(self.settings.monitor_interval_minutes * 60)

    async def check_once(
        self,
        *,
        manual: bool,
        force_latest: bool,
        send_to_groups: bool,
        event: AstrMessageEvent | None = None,
        target_groups: list[str] | None = None,
        analysis_file_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.service.check_once(
            manual=manual,
            force_latest=force_latest,
            send_to_groups=send_to_groups,
            event=event,
            target_groups=target_groups,
            analysis_file_groups=analysis_file_groups,
        )

    def _resolve_data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return Path(__file__).resolve().parent / ".data"

    def _record_model_error(self, stage: str, error: BaseException | str, metadata: dict[str, Any]) -> None:
        service = getattr(self, "service", None)
        runtime = getattr(service, "runtime", None)
        if runtime is not None:
            runtime.record_model_error(stage, error, metadata)
            return
        RuntimeState(
            settings=getattr(self, "settings", load_config({})),
            state_store=getattr(self, "state_store", StateStore(Path("/tmp/wtup_state.json"))),
            log_dir=getattr(self, "log_dir", Path("/tmp")),
            error_dir=getattr(self, "error_dir"),
        ).record_model_error(stage, error, metadata)

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.warning("[%s] 插件已卸载", PLUGIN_NAME)
