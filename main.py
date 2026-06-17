from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .wtup.config import (
        BRANCH_NAME,
        DEFAULT_ANALYSIS_PROMPT,
        DEFAULT_REVIEW_PROMPT,
        DEFAULT_SUMMARY_PROMPT,
        DEFAULT_TOOL_CALL_PROMPT,
        PLUGIN_NAME,
        PLUGIN_VERSION,
        REPO_FULL_NAME,
        PluginConfig,
        load_config,
    )
    from .wtup.diff_collector import short_sha
    from .wtup.permissions import admin_target_allows_sender, normalize_user_id
    from .wtup.runtime import RuntimeState
    from .wtup.service import UpdateCheckService
    from .wtup.state_store import StateStore
    from .wtup.termination import TaskTerminatedError, reset_task_termination
except ImportError:
    from wtup.config import (
        BRANCH_NAME,
        DEFAULT_ANALYSIS_PROMPT,
        DEFAULT_REVIEW_PROMPT,
        DEFAULT_SUMMARY_PROMPT,
        DEFAULT_TOOL_CALL_PROMPT,
        PLUGIN_NAME,
        PLUGIN_VERSION,
        REPO_FULL_NAME,
        PluginConfig,
        load_config,
    )
    from wtup.diff_collector import short_sha
    from wtup.permissions import admin_target_allows_sender, normalize_user_id
    from wtup.runtime import RuntimeState
    from wtup.service import UpdateCheckService
    from wtup.state_store import StateStore
    from wtup.termination import TaskTerminatedError, reset_task_termination


COMMAND_RECEIVED_EMOJI_ID = "289"
COMMAND_DONE_EMOJI_ID = "124"
CLEAR_CACHE_FILES_CONFIG_KEY = "clear_cache_files"
TERMINATE_RUNNING_TASK_CONFIG_KEY = "terminate_running_task"
RESTORE_DEFAULT_PROMPTS_CONFIG_KEY = "restore_default_prompts"


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


def _normalize_group_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def _get_event_user_id(event: AstrMessageEvent) -> str:
    for method_name in ("get_sender_id", "get_user_id"):
        method = getattr(event, method_name, None)
        if not callable(method):
            continue
        try:
            user_id = normalize_user_id(method())
        except Exception as exc:
            logger.warning("[%s] 获取发送者 QQ 失败: %s", PLUGIN_NAME, exc)
            user_id = ""
        if user_id:
            return user_id

    message_obj = getattr(event, "message_obj", None)
    candidates = [
        getattr(event, "sender_id", None),
        getattr(event, "user_id", None),
        getattr(message_obj, "sender_id", None),
        getattr(message_obj, "user_id", None),
    ]
    for candidate in candidates:
        user_id = normalize_user_id(candidate)
        if user_id:
            return user_id

    for user in (
        getattr(event, "sender", None),
        getattr(event, "user", None),
        getattr(message_obj, "sender", None),
        getattr(message_obj, "user", None),
    ):
        if isinstance(user, dict):
            for key in ("user_id", "id"):
                user_id = normalize_user_id(user.get(key))
                if user_id:
                    return user_id
            continue
        for key in ("user_id", "id"):
            user_id = normalize_user_id(getattr(user, key, None))
            if user_id:
                return user_id

    raw_message = getattr(message_obj, "raw_message", None)
    if isinstance(raw_message, dict):
        for key in ("user_id", "sender_id"):
            user_id = normalize_user_id(raw_message.get(key))
            if user_id:
                return user_id
        sender = raw_message.get("sender")
        if isinstance(sender, dict):
            for key in ("user_id", "id"):
                user_id = normalize_user_id(sender.get(key))
                if user_id:
                    return user_id
    return ""


def _event_is_configured_admin(event: AstrMessageEvent, admin_targets: list[str]) -> bool:
    return admin_target_allows_sender(
        admin_targets,
        sender_id=_get_event_user_id(event),
        event_origin=getattr(event, "unified_msg_origin", ""),
    )


def _set_config_value(config: Any, key: str, value: Any) -> bool:
    changed = False
    setter = getattr(config, "set", None)
    if callable(setter):
        try:
            setter(key, value)
            changed = True
        except Exception as exc:
            logger.warning("[%s] 更新配置项 %s 失败: %s", PLUGIN_NAME, key, exc)

    try:
        config[key] = value
        changed = True
    except Exception:
        pass

    try:
        setattr(config, key, value)
        changed = True
    except Exception:
        pass

    return changed


def _save_config(config: Any) -> bool:
    for method_name in ("save_config", "save", "save_plugin_config"):
        method = getattr(config, method_name, None)
        if not callable(method):
            continue
        try:
            method()
            return True
        except TypeError:
            try:
                method(config)
                return True
            except Exception as exc:
                logger.warning("[%s] 保存配置失败: %s", PLUGIN_NAME, exc)
                return False
        except Exception as exc:
            logger.warning("[%s] 保存配置失败: %s", PLUGIN_NAME, exc)
            return False
    return False


def _schema_prompt_defaults() -> dict[str, str]:
    fallback = {
        "analysis_prompt": DEFAULT_ANALYSIS_PROMPT,
        "summary_prompt": DEFAULT_SUMMARY_PROMPT,
        "tool_call_prompt": DEFAULT_TOOL_CALL_PROMPT,
        "review_prompt": DEFAULT_REVIEW_PROMPT,
    }
    schema_path = Path(__file__).resolve().parent / "_conf_schema.json"
    try:
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[%s] 读取后台默认提示词失败，使用代码内置兜底: %s", PLUGIN_NAME, exc)
        return fallback
    if not isinstance(payload, dict):
        return fallback
    result = dict(fallback)
    for key in result:
        field = payload.get(key)
        if isinstance(field, dict):
            default = str(field.get("default") or "").strip()
            if default:
                result[key] = default
    return result


def _clear_directory_contents(directory: Path) -> tuple[int, int]:
    if not directory.exists():
        return 0, 0

    removed_count = 0
    failed_count = 0
    for path in list(directory.iterdir()):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed_count += 1
        except Exception as exc:
            failed_count += 1
            logger.warning("[%s] 清理缓存文件失败: %s (%s)", PLUGIN_NAME, path, exc)
    return removed_count, failed_count


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
        self.config = config if config is not None else {}
        self.settings: PluginConfig = load_config(self.config)
        self.data_dir = self._resolve_data_dir()
        self._restore_default_prompts_on_startup()
        self._clear_cache_files_on_startup()
        self.state_store = StateStore(self.data_dir / "state.json")
        self.image_dir = self.data_dir / "images"
        self.log_dir = self.data_dir / "logs"
        self.error_dir = self.data_dir / "errors"
        self.template_path = Path(__file__).resolve().parent / "templates" / "help_miku.html"
        self._task: asyncio.Task | None = None
        self.service = UpdateCheckService(
            context=self.context,
            settings=self._with_dynamic_controls(self.settings),
            state_store=self.state_store,
            image_dir=self.image_dir,
            log_dir=self.log_dir,
            error_dir=self.error_dir,
            template_path=self.template_path,
            render_host=self,
        )
        self.settings = self.service.with_runtime_hooks(self._with_dynamic_controls(self.settings))
        self.service.settings = self.settings

    def _with_dynamic_controls(self, settings: PluginConfig) -> PluginConfig:
        return replace(
            settings,
            task_termination_checker=self._should_terminate_running_task,
            task_termination_resetter=self._reset_terminate_running_task,
        )

    def _should_terminate_running_task(self) -> bool:
        return load_config(self.config).terminate_running_task

    def _reset_terminate_running_task(self) -> None:
        if not load_config(self.config).terminate_running_task:
            return
        if _set_config_value(self.config, TERMINATE_RUNNING_TASK_CONFIG_KEY, False):
            _save_config(self.config)
        self.settings = self._with_dynamic_controls(load_config(self.config))
        self.settings = self.service.with_runtime_hooks(self.settings)
        self.service.settings = self.settings

    def _clear_cache_files_on_startup(self) -> None:
        if not self.settings.clear_cache_files:
            return

        removed_count, failed_count = _clear_directory_contents(self.data_dir)
        if _set_config_value(self.config, CLEAR_CACHE_FILES_CONFIG_KEY, False):
            _save_config(self.config)
        self.settings = load_config(self.config)
        logger.warning(
            "[%s] 已执行一次性缓存清理，目录=%s，删除 %s 项，失败 %s 项，已关闭后台开关",
            PLUGIN_NAME,
            self.data_dir,
            removed_count,
            failed_count,
        )

    def _restore_default_prompts_on_startup(self) -> None:
        if not self.settings.restore_default_prompts:
            return

        changed = False
        prompt_defaults = _schema_prompt_defaults()
        for key, value in (
            ("analysis_prompt", prompt_defaults["analysis_prompt"]),
            ("summary_prompt", prompt_defaults["summary_prompt"]),
            ("tool_call_prompt", prompt_defaults["tool_call_prompt"]),
            ("review_prompt", prompt_defaults["review_prompt"]),
            (RESTORE_DEFAULT_PROMPTS_CONFIG_KEY, False),
        ):
            changed = _set_config_value(self.config, key, value) or changed
        if changed:
            _save_config(self.config)
        self.settings = load_config(self.config)
        logger.warning("[%s] 已恢复内置提示词，并自动关闭后台恢复开关", PLUGIN_NAME)

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
            f"管理员列表: {len(self.settings.admin_targets)} 个",
            f"单次模型请求文件限制: {self.settings.max_files_per_report or '不限制'}",
            f"单次模型请求 token 输入限制: {self.settings.max_input_token_limit or '不限制'}",
            f"模型请求并发数: {self.settings.model_concurrency}",
            f"流式模型请求: {'启动' if self.settings.enable_streaming_llm_call else '关闭'}",
            f"总结模型: {'启动' if self.settings.enable_summary_model else '关闭'}",
            f"备用模型: {len(self.settings.backup_provider_ids)} 个",
            f"分析前报告: {'生成' if self.settings.enable_pre_summary_report else '关闭'}",
            f"最大重试次数: {self.settings.max_retry_count}",
            f"GitHub 请求最大重试次数: {self.settings.github_max_retry_count}",
            f"终止当前任务开关: {'开启' if self._should_terminate_running_task() else '关闭'}",
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
        if force_latest and not _event_is_configured_admin(event, self.settings.admin_targets):
            yield event.plain_result("权限不足：/wtup_check 强制 和 /wtup_check 强制全部 只能由插件后台“管理员列表”中的用户执行。")
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
        try:
            return await self.service.check_once(
                manual=manual,
                force_latest=force_latest,
                send_to_groups=send_to_groups,
                event=event,
                target_groups=target_groups,
                analysis_file_groups=analysis_file_groups,
            )
        except TaskTerminatedError as exc:
            runtime = getattr(getattr(self, "service", None), "runtime", None)
            message = f"任务已终止：{exc.stage or '后台开关已开启'}。未标记本次更新完成。"
            task_log_path = runtime.current_task_log_path if runtime is not None else None
            if runtime is not None and runtime.current_task_log_path is not None:
                runtime.record_task_log("任务终止", {"阶段": exc.stage or "未知", "结果": "未标记 commit 完成"})
                runtime.finish_task_log(
                    status="已终止",
                    message=message,
                    elapsed_seconds=runtime.current_task_elapsed_seconds(),
                )
            reset_task_termination(self.settings)
            logger.warning("[%s] %s", PLUGIN_NAME, message)
            return {
                "message": message,
                "task_log_path": task_log_path,
                "terminated": True,
                "commit_marked_complete": False,
            }
        except Exception as exc:
            runtime = getattr(getattr(self, "service", None), "runtime", None)
            if runtime is not None and runtime.current_task_log_path is not None:
                runtime.finish_task_log(
                    status="异常",
                    message=str(exc),
                    elapsed_seconds=runtime.current_task_elapsed_seconds(),
                )
            raise

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
