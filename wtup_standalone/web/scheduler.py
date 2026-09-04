from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# 强制调度器与日志格式化采用中国标准北京时间 (Asia/Shanghai, UTC+8)
os.environ["TZ"] = "Asia/Shanghai"
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

BEIJING_TZ = timezone(timedelta(hours=8))

def beijing_now() -> datetime:
    """获取当前北京时间 datetime 对象。"""
    return datetime.now(BEIJING_TZ)

def beijing_now_iso() -> str:
    """获取当前北京时间 ISO 8601 字符串 (含 +08:00 时区信息)。"""
    return beijing_now().isoformat()

from ..analyzer import DatamineAnalyzer
from ..config import AnalyzerConfig
from ..git_manager import GitRepoManager
from .renderer import render_report_to_image
from .store import DataStore

_logger = logging.getLogger("wtup_standalone.scheduler")

STATUS_TEXT_MAP = {
    "idle": "等待分析",
    "running": "分析中",
    "completed": "分析完成",
    "failed": "分析失败",
}


def is_analysis_failed(result: Any) -> tuple[bool, str]:
    """检查分析结果是否实质上失败（例如全部模型调用失败转入兜底）。"""
    if result is None:
        return True, "分析结果为空"
    summary = str(getattr(result, "summary", "") or "").strip()
    tags = getattr(result, "tags", []) or []
    # 检查是否为模型全链路失败的兜底分析
    if "需复核" in tags and ("模型分析失败" in summary or "没有可用模型" in summary or "不可用" in summary):
        return True, summary
    if summary.startswith("模型分析失败") or summary.startswith("没有可用模型 Provider"):
        return True, summary
    return False, ""


class TaskLogHandler(logging.Handler):
    """把 wtup_standalone 各模块的标准 logging 自动接入当前任务日志流。"""

    def __init__(self, scheduler: UpdateScheduler) -> None:
        super().__init__()
        self.scheduler = scheduler

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # 避免重复记录 UpdateScheduler 自身产生的内部日志
            if record.name == "wtup_standalone.scheduler":
                return
            msg = record.getMessage()
            timestamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            module_name = record.name.split(".")[-1]
            entry = f"[{timestamp}] [{record.levelname}] [{module_name}] {msg}"
            
            target = getattr(self.scheduler, "_active_target", "pc")
            info = self.scheduler.current_mobile_task_info if target == "mobile" else self.scheduler.current_task_info
            logs = info.setdefault("logs", [])
            logs.append(entry)
            if len(logs) > 500:
                info["logs"] = logs[-500:]
        except Exception:
            pass


class UpdateScheduler:
    """定时检查与后台任务调度器 (支持端游与手游双独立通道)。"""

    def __init__(self, store: DataStore) -> None:
        self.store = store
        self.git_manager = GitRepoManager(self.store.data_dir / "repo", default_repo="gszabi99/War-Thunder-Datamine")
        self.git_manager_mobile = GitRepoManager(self.store.data_dir / "repo_mobile", default_repo="gszabi99/War-Thunder-Mobile-Datamine")
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._wake_event = asyncio.Event()

        self._active_target = "pc"

        # 端游与手游各自的任务参数记录 (用于重试)
        self.last_task_params: dict[str, Any] = {}
        self.last_mobile_task_params: dict[str, Any] = {}

        # 下次检查时间
        self._next_check_at: str | None = None
        self._next_mobile_check_at: str | None = None

        # 端游任务执行状态
        self.current_task_info: dict[str, Any] = {
            "id": None,
            "target": "pc",
            "status": "idle",  # idle, running, completed, failed
            "status_text": "等待分析",
            "stage": "等待分析",
            "progress_percent": 0,
            "logs": [],
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result_report_id": None,
            "params": {},
            "trigger_mode": None,
            "can_retry": True,
        }

        # 手游任务执行状态
        self.current_mobile_task_info: dict[str, Any] = {
            "id": None,
            "target": "mobile",
            "status": "idle",
            "status_text": "等待分析",
            "stage": "等待分析",
            "progress_percent": 0,
            "logs": [],
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result_report_id": None,
            "params": {},
            "trigger_mode": None,
            "can_retry": True,
        }

        # 挂载日志捕获器
        self._log_handler = TaskLogHandler(self)
        logging.getLogger("wtup_standalone").addHandler(self._log_handler)
    @property
    def next_check_at(self) -> str | None:
        """获取端游下一次预计执行检查的时间 (ISO 8601)。"""
        config = self.store.load_config()
        if not config.get("schedule_enabled", True):
            return None
        if self._next_check_at:
            return self._next_check_at
        state = self.store.load_state(target="pc")
        last_checked = state.get("last_checked_at")
        if last_checked:
            try:
                last_dt = datetime.fromisoformat(last_checked)
                interval = max(1, int(config.get("schedule_interval_minutes", 15)))
                return (last_dt + timedelta(minutes=interval)).isoformat()
            except Exception:
                pass
        return None

    @property
    def next_mobile_check_at(self) -> str | None:
        """获取手游下一次预计执行检查的时间 (ISO 8601)。"""
        config = self.store.load_config()
        if not config.get("mobile_schedule_enabled", True):
            return None
        if self._next_mobile_check_at:
            return self._next_mobile_check_at
        state = self.store.load_state(target="mobile")
        last_checked = state.get("last_checked_at")
        if last_checked:
            try:
                last_dt = datetime.fromisoformat(last_checked)
                interval = max(1, int(config.get("mobile_schedule_interval_minutes", 15)))
                return (last_dt + timedelta(minutes=interval)).isoformat()
            except Exception:
                pass
        return None

    def get_task_info(self, target: str = "pc") -> dict[str, Any]:
        """获取指定目标的任务状态对象 (pc 或 mobile)。"""
        return self.current_mobile_task_info if str(target).lower() == "mobile" else self.current_task_info

    def start(self) -> None:
        """启动后台调度循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        _logger.info("UpdateScheduler 已启动 (支持端游与手游双独立通道)")

    def stop(self) -> None:
        """停止调度器。"""
        self._running = False
        self._wake_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
        try:
            logging.getLogger("wtup_standalone").removeHandler(self._log_handler)
        except Exception:
            pass
        _logger.info("UpdateScheduler 已停止")

    def log(self, message: str, level: str = "INFO", target: str | None = None) -> None:
        """向当前任务状态追加一条实时日志。"""
        t = target or getattr(self, "_active_target", "pc")
        info = self.current_mobile_task_info if str(t).lower() == "mobile" else self.current_task_info
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] [{level}] {message}"
        logs = info.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > 500:
            info["logs"] = logs[-500:]
        if level == "ERROR":
            _logger.error(message)
        elif level == "WARNING":
            _logger.warning(message)
        else:
            _logger.info(message)

    def clear_logs(self, target: str = "pc") -> None:
        """清空当前任务状态的日志。"""
        info = self.current_mobile_task_info if str(target).lower() == "mobile" else self.current_task_info
        info["logs"] = []

    def prepare_task_info(self, *, mode: str = "manual", compare_range: str | None = None, is_diff: bool = False, target: str = "pc") -> None:
        """在后台异步任务创建前预置 running 状态，供前端立即感知。"""
        info = self.current_mobile_task_info if str(target).lower() == "mobile" else self.current_task_info
        info["status"] = "running"
        info["status_text"] = "分析中"
        info["can_retry"] = False
        info["stage"] = "准备启动分析任务..."
        info["progress_percent"] = 3
        info["started_at"] = beijing_now_iso()
        info["finished_at"] = None
        info["error"] = None
        info["result_report_id"] = None

    def _set_stage(self, stage: str, progress: int, target: str | None = None) -> None:
        t = target or getattr(self, "_active_target", "pc")
        info = self.current_mobile_task_info if str(t).lower() == "mobile" else self.current_task_info
        info["stage"] = stage
        info["progress_percent"] = progress
        self.log(stage, level="INFO", target=t)

    async def _loop(self) -> None:
        """后台定时轮询循环 (同时独立支持端游与手游监控)。"""
        last_pc_check = 0.0
        last_mobile_check = 0.0

        while self._running:
            now_ts = time.time()
            config = self.store.load_config()

            pc_enabled = bool(config.get("schedule_enabled", True))
            pc_interval = max(1, int(config.get("schedule_interval_minutes", 15))) * 60

            mobile_enabled = bool(config.get("mobile_schedule_enabled", True))
            mobile_interval = max(1, int(config.get("mobile_schedule_interval_minutes", 15))) * 60

            # 1. 端游定时检查
            if pc_enabled and (now_ts - last_pc_check >= pc_interval):
                try:
                    last_pc_check = now_ts
                    self._next_check_at = (beijing_now() + timedelta(seconds=pc_interval)).isoformat()
                    await self.trigger_check(trigger_mode="scheduled", target="pc")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    _logger.error("端游定时更新检查异常: %s", exc)

            # 2. 手游定时检查
            if mobile_enabled and (now_ts - last_mobile_check >= mobile_interval):
                try:
                    last_mobile_check = now_ts
                    self._next_mobile_check_at = (beijing_now() + timedelta(seconds=mobile_interval)).isoformat()
                    await self.trigger_check(trigger_mode="scheduled", target="mobile")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    _logger.error("手游定时更新检查异常: %s", exc)

            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def retry_last_task(self, target: str = "pc") -> str:
        """重试上一次执行的任务。"""
        target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
        params_dict = self.last_mobile_task_params if target_norm == "mobile" else self.last_task_params
        info = self.current_mobile_task_info if target_norm == "mobile" else self.current_task_info

        if self._lock.locked():
            raise RuntimeError("当前已有分析任务正在执行中，无法重试")

        params = dict(params_dict or info.get("params") or {})
        trigger_mode = "manual"
        compare_range = params.get("compare_range")
        diff_text = params.get("diff_text")
        custom_base = params.get("custom_base", "")
        custom_head = params.get("custom_head", "")

        label = "手游" if target_norm == "mobile" else "端游"
        self.log(f"收到重试指令，正在重新发起{label}分析任务 (比对范围={compare_range or '无'}, diff={'有' if diff_text else '无'})", target=target_norm)
        return await self.trigger_check(
            trigger_mode=trigger_mode,
            compare_range=compare_range,
            diff_text=diff_text,
            custom_base=custom_base,
            custom_head=custom_head,
            target=target_norm,
        )

    async def trigger_check(
        self,
        *,
        trigger_mode: str = "manual",
        compare_range: str | None = None,
        diff_text: str | None = None,
        custom_base: str = "",
        custom_head: str = "",
        target: str = "pc",
    ) -> str:
        """触发更新检查（定时或手动，支持端游 pc 与手游 mobile）。"""
        target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
        self._active_target = target_norm
        task_info = self.current_mobile_task_info if target_norm == "mobile" else self.current_task_info
        git_mgr = self.git_manager_mobile if target_norm == "mobile" else self.git_manager

        async with self._lock:
            task_id = f"task_{int(time.time() * 1000)}"
            task_params = {
                "trigger_mode": trigger_mode,
                "compare_range": compare_range,
                "diff_text": diff_text,
                "custom_base": custom_base,
                "custom_head": custom_head,
                "target": target_norm,
            }
            if target_norm == "mobile":
                self.last_mobile_task_params = dict(task_params)
            else:
                self.last_task_params = dict(task_params)

            existing_logs = task_info.get("logs", [])
            new_logs = existing_logs[-30:] if existing_logs else []
            prefix_label = "【手游 Mobile】" if target_norm == "mobile" else "【端游 PC】"
            new_logs.append(f"[{time.strftime('%H:%M:%S')}] [INFO] === 任务 {task_id} 启动 ({prefix_label} 模式: {trigger_mode}) ===")

            task_info.update({
                "id": task_id,
                "target": target_norm,
                "status": "running",
                "status_text": "分析中",
                "stage": "初始化分析任务",
                "progress_percent": 5,
                "logs": new_logs,
                "started_at": beijing_now_iso(),
                "finished_at": None,
                "error": None,
                "result_report_id": None,
                "params": task_params,
                "trigger_mode": trigger_mode,
                "can_retry": False,
            })

            config_dict = self.store.load_config()

            if target_norm == "mobile":
                repo_full_name = config_dict.get("github_mobile_repo", "gszabi99/War-Thunder-Mobile-Datamine")
                branch = config_dict.get("github_mobile_branch", "master")
                wm_config = {
                    "watermark_enabled": bool(config_dict.get("mobile_watermark_enabled", False)),
                    "watermark_text": str(config_dict.get("mobile_watermark_text", "War Thunder Mobile Datamine")),
                    "watermark_opacity": float(config_dict.get("mobile_watermark_opacity", 0.12)),
                    "watermark_size": int(config_dict.get("mobile_watermark_size", 18)),
                    "watermark_density": str(config_dict.get("mobile_watermark_density", "medium")),
                }
            else:
                repo_full_name = config_dict.get("github_repo", "gszabi99/War-Thunder-Datamine")
                branch = config_dict.get("github_branch", "master")
                wm_config = {
                    "watermark_enabled": bool(config_dict.get("watermark_enabled", False)),
                    "watermark_text": str(config_dict.get("watermark_text", "War Thunder Datamine")),
                    "watermark_opacity": float(config_dict.get("watermark_opacity", 0.12)),
                    "watermark_size": int(config_dict.get("watermark_size", 18)),
                    "watermark_density": str(config_dict.get("watermark_density", "medium")),
                }

            render_scale = config_dict.get("render_scale", "1.5x")

            cfg = AnalyzerConfig.from_env(
                api_key=config_dict.get("openai_api_key", ""),
                base_url=config_dict.get("openai_base_url", ""),
                model=config_dict.get("model", "deepseek-chat"),
                backup_models=config_dict.get("backup_models", []),
                summary_model=config_dict.get("summary_model", ""),
                review_model=config_dict.get("review_model", ""),
                review_mode=config_dict.get("review_mode", "auto"),
                enable_struct_diff=config_dict.get("enable_struct_diff", True),
                github_token=config_dict.get("github_token", ""),
                repo_full_name=repo_full_name,
                branch=branch,
                render_scale=render_scale,
                target_game=target_norm,
            )
            analyzer = DatamineAnalyzer(cfg)
            self.log(f"目标仓库: {cfg.repo_full_name} ({cfg.branch}) · 使用模型: {cfg.model} · 清晰度档位: {render_scale}", target=target_norm)

            try:
                # 场景 1: 直接传入 diff 文本
                if diff_text:
                    self._set_stage("正在分析用户上传的 diff 补丁", 30, target=target_norm)
                    self.log(f"补丁文本大小: {len(diff_text)} 字符", target=target_norm)
                    result = await analyzer.analyze_diff_text(
                        diff_text,
                        base_sha=custom_base or "upload_base",
                        head_sha=custom_head or "upload_head",
                    )
                    base_sha = custom_base or "upload_base"
                    head_sha = custom_head or "upload_head"

                # 场景 2: 指定比较范围
                elif compare_range:
                    if "..." in compare_range:
                        base_sha, head_sha = compare_range.split("...", 1)
                    elif ".." in compare_range:
                        base_sha, head_sha = compare_range.split("..", 1)
                    else:
                        raise ValueError(f"compare 格式不正确，需为 base...head，收到: {compare_range}")
                    base_sha = base_sha.strip()
                    head_sha = head_sha.strip()
                    self._set_stage(f"正在拉取比对数据 {base_sha[:7]}...{head_sha[:7]}", 20, target=target_norm)
                    result = await analyzer.analyze_github_compare(base_sha, head_sha)

                # 场景 3: 默认拉取 GitHub 最新 commits
                else:
                    self._set_stage(f"探测 {cfg.repo_full_name} ({cfg.branch}) 最新提交 (Git / API)", 10, target=target_norm)
                    state = self.store.load_state(target=target_norm)
                    last_commit = state.get("last_checked_commit")

                    # 1. 优先使用 Git 本地命令 git ls-remote 毫秒级探测，免受 API 限流
                    remote_sha = await asyncio.to_thread(
                        git_mgr.get_remote_head,
                        cfg.repo_full_name,
                        cfg.branch,
                    )
                    if remote_sha and last_commit and remote_sha == last_commit and trigger_mode == "scheduled":
                        self._set_stage(f"当前已是最新提交 {remote_sha[:7]} (Git ls-remote 探测)，无需更新", 100, target=target_norm)
                        task_info["status"] = "completed"
                        task_info["status_text"] = "分析完成"
                        task_info["can_retry"] = True
                        task_info["finished_at"] = beijing_now_iso()
                        self.store.update_state(
                            target=target_norm,
                            last_checked_at=beijing_now_iso(),
                            last_check_status="up_to_date",
                        )
                        return ""

                    # 2. 如果本地 Git 仓库可用，检查是否需要同步增量提交
                    if git_mgr.is_local_repo_ready():
                        commits = git_mgr.get_commits(cfg.branch, limit=10)
                        local_head = commits[0]["sha"] if commits else ""
                        need_sync = (not commits) or (remote_sha and local_head != remote_sha) or (trigger_mode != "scheduled")
                        if need_sync:
                            self.log(f"检测到远程新提交或本地需同步 (remote={remote_sha[:7] if remote_sha else '未知'}, local={local_head[:7] if local_head else '空'})，正在拉取增量提交...", target=target_norm)
                            sync_ok = await asyncio.to_thread(
                                git_mgr.sync_repo,
                                cfg.repo_full_name,
                                cfg.branch,
                            )
                            if sync_ok:
                                commits = git_mgr.get_commits(cfg.branch, limit=10)
                            else:
                                self.log("本地 Git fetch 失败，尝试继续使用现有数据或 API 回退", level="WARNING", target=target_norm)
                        if commits:
                            self.log(f"使用本地 Git 仓库提交历史 (最新: {commits[0]['sha'][:7]})", target=target_norm)
                        else:
                            commits = await asyncio.to_thread(
                                analyzer.github_client.get_commits,
                                cfg.repo_full_name,
                                branch=cfg.branch,
                                per_page=10,
                            )
                    else:
                        # 本地仓库未克隆，尝试自动同步
                        self.log("本地 Git 仓库未就绪，尝试克隆增量仓库...", target=target_norm)
                        sync_ok = await asyncio.to_thread(
                            git_mgr.sync_repo,
                            cfg.repo_full_name,
                            cfg.branch,
                        )
                        if sync_ok and git_mgr.is_local_repo_ready():
                            commits = git_mgr.get_commits(cfg.branch, limit=10)
                        else:
                            commits = await asyncio.to_thread(
                                analyzer.github_client.get_commits,
                                cfg.repo_full_name,
                                branch=cfg.branch,
                                per_page=10,
                            )

                    if not commits and remote_sha:
                        latest_commit = remote_sha
                    elif commits:
                        latest_commit = commits[0]["sha"]
                    else:
                        raise RuntimeError("未获取到任何 commit 记录")
                    if not last_commit:
                        # 首次运行：取最新提交的父提交作为比对基线
                        parents = commits[0].get("parents", [])
                        last_commit = parents[0] if parents else commits[-1]["sha"]
                        self.log(f"首次初始化提交基线: {last_commit[:7]}", target=target_norm)

                    if latest_commit == last_commit and trigger_mode == "scheduled":
                        self._set_stage(f"当前已是最新提交 {latest_commit[:7]}，无需更新", 100, target=target_norm)
                        task_info["status"] = "completed"
                        task_info["status_text"] = "分析完成"
                        task_info["can_retry"] = True
                        task_info["finished_at"] = beijing_now_iso()
                        self.store.update_state(
                            target=target_norm,
                            last_checked_at=beijing_now_iso(),
                            last_check_status="up_to_date",
                        )
                        return ""

                    base_sha = last_commit
                    head_sha = latest_commit

                    # 核心防护：避免相同 commit 对比产生 0 改动而跳过模型分析！
                    # 如果基线等于最新提交（例如手动触发检查或重试最新版本），自动回退比对到其父提交
                    if base_sha == head_sha or (latest_commit == last_commit and trigger_mode != "scheduled"):
                        parents = commits[0].get("parents", []) if commits else []
                        parent_sha = parents[0] if parents else (commits[1]["sha"] if len(commits) > 1 else "")
                        if parent_sha and parent_sha != head_sha:
                            base_sha = parent_sha
                            self.log(f"当前已是最新提交 {head_sha[:7]}，重试/分析将重跑该版本与父提交的改动: {base_sha[:7]}...{head_sha[:7]}", target=target_norm)

                    self._set_stage(f"开始执行版本比对分析 {base_sha[:7]}...{head_sha[:7]}", 25, target=target_norm)
                    result = await analyzer.analyze_github_compare(base_sha, head_sha)

                # 严格校验分析结果是否实质失败（如全链路模型报错回退到兜底）
                failed, fail_reason = is_analysis_failed(result)
                if failed:
                    raise RuntimeError(f"模型分析失败，未能生成有效分析报告: {fail_reason}")

                # 分析完成，按配置模板生成图片 (1. Discord 黑暗风格, 2. Miku 风格) 与水印
                template_style = config_dict.get("render_template", "discord")
                enable_ai = bool(config_dict.get("enable_ai_analysis", False))
                self._set_stage(f"模型分析完成，正在按 {template_style} 模板生成渲染报告图片 (清晰度档位: {render_scale})", 80, target=target_norm)
                report_dict = result.to_dict()
                report_dict["enable_ai_analysis"] = enable_ai
                report_dict["watermark_config"] = wm_config
                report_dict["render_scale"] = render_scale
                report_dict["target"] = target_norm
                image_bytes = await render_report_to_image(
                    report_dict,
                    template=template_style,
                    enable_ai_analysis=enable_ai,
                    watermark_config=wm_config,
                    render_scale=render_scale,
                )

                self._set_stage("正在保存报告与更新状态", 95, target=target_norm)
                report_id = self.store.save_report(
                    report_dict,
                    trigger_mode=trigger_mode,
                    commit_base=base_sha,
                    commit_head=head_sha,
                    image_bytes=image_bytes,
                    target=target_norm,
                )

                # 只有分析与保存完全成功，才标记该版本分析完成，更新给 API
                self.store.update_state(
                    target=target_norm,
                    last_checked_commit=head_sha,
                    last_checked_at=beijing_now_iso(),
                    last_check_status="success",
                )

                self._set_stage(f"分析完成！报告 ID: {report_id}", 100, target=target_norm)
                task_info["status"] = "completed"
                task_info["status_text"] = "分析完成"
                task_info["can_retry"] = True
                task_info["result_report_id"] = report_id
                task_info["finished_at"] = beijing_now_iso()
                return report_id

            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {str(exc)}"
                self.log(f"任务执行失败: {error_msg}", level="ERROR", target=target_norm)
                tb_lines = traceback.format_exc().strip().split("\n")
                for tb in tb_lines[-3:]:
                    self.log(f"堆栈异常: {tb.strip()}", level="ERROR", target=target_norm)

                task_info["status"] = "failed"
                task_info["status_text"] = "分析失败"
                task_info["error"] = error_msg
                task_info["can_retry"] = True
                task_info["result_report_id"] = None
                task_info["finished_at"] = beijing_now_iso()

                # 注意：任务失败绝不标记这个版本分析完成 (不更新 last_checked_commit)，也不写入任何有效报告给 API
                self.store.update_state(
                    target=target_norm,
                    last_checked_at=beijing_now_iso(),
                    last_check_status=f"error: {error_msg}",
                )
                raise
            finally:
                try:
                    cfg = self.store.load_config()
                    if target_norm == "mobile":
                        if cfg.get("mobile_schedule_enabled", True):
                            interval = max(1, int(cfg.get("mobile_schedule_interval_minutes", 15)))
                            self._next_mobile_check_at = (beijing_now() + timedelta(minutes=interval)).isoformat()
                    else:
                        if cfg.get("schedule_enabled", True):
                            interval = max(1, int(cfg.get("schedule_interval_minutes", 15)))
                            self._next_check_at = (beijing_now() + timedelta(minutes=interval)).isoformat()
                except Exception:
                    pass
