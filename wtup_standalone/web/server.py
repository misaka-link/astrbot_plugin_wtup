from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# 强制 Python 运行时采用中国标准北京时间 (Asia/Shanghai, UTC+8)
os.environ["TZ"] = "Asia/Shanghai"
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

from .renderer import build_html_report
from .scheduler import UpdateScheduler
from .store import DataStore

_logger = logging.getLogger("wtup_standalone.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"


class WebApp:
    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.store = DataStore(data_dir)
        self.scheduler = UpdateScheduler(self.store)
        self.server = None

    async def handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                writer.close()
                return

            req_line = line.decode("utf-8", errors="replace").strip()
            parts = req_line.split()
            if len(parts) < 2:
                writer.close()
                return

            method, raw_path = parts[0].upper(), parts[1]
            parsed_url = urllib.parse.urlparse(raw_path)
            path = parsed_url.path
            query_params = urllib.parse.parse_qs(parsed_url.query)

            # 读取请求头
            headers = {}
            while True:
                header_line = await reader.readline()
                if not header_line or header_line.strip() == b"":
                    break
                h_str = header_line.decode("utf-8", errors="replace").strip()
                if ":" in h_str:
                    k, v = h_str.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            # 读取请求体
            content_length = int(headers.get("content-length", 0))
            body_bytes = b""
            if content_length > 0:
                body_bytes = await reader.readexactly(content_length)

            # 处理 OPTIONS 预检请求 (CORS)
            if method == "OPTIONS":
                self._send_response(writer, 204, b"", content_type="text/plain", extra_headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                })
                return

            # 路由派发
            await self._dispatch(method, path, query_params, headers, body_bytes, writer)

        except Exception as exc:
            _logger.error("处理 HTTP 请求异常: %s", exc, exc_info=True)
            try:
                self._send_json(writer, 500, {"error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass

    async def _dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        # 1. API 路由
        if path.startswith("/api/"):
            await self._handle_api(method, path, query, headers, body, writer)
            return

        # 2. 静态文件 (支持 GET 与 HEAD)
        if method in ("GET", "HEAD"):
            if path == "/" or path == "/index.html":
                filepath = STATIC_DIR / "index.html"
            else:
                rel = path.lstrip("/")
                if rel.startswith("static/"):
                    rel = rel[7:]
                filepath = STATIC_DIR / rel

            if filepath.exists() and filepath.is_file() and filepath.resolve().is_relative_to(STATIC_DIR.resolve()):
                ctype, _ = mimetypes.guess_type(str(filepath))
                content = filepath.read_bytes()
                self._send_response(writer, 200, content, content_type=ctype or "application/octet-stream")
                return

        # 默认 404
        self._send_json(writer, 404, {"error": "Not Found"})

    async def _handle_api(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        # 解析 JSON body
        payload = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                pass

        # GET /api/status - 查询系统与调度状态 (支持 target=pc 或 target=mobile)
        if method == "GET" and path == "/api/status":
            target = query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            cfg = self.store.get_masked_config()
            state = self.store.load_state(target=target_norm)
            git_mgr = self.scheduler.git_manager_mobile if target_norm == "mobile" else self.scheduler.git_manager
            task_info = self.scheduler.get_task_info(target=target_norm)

            if target_norm == "mobile":
                repo = cfg.get("github_mobile_repo", "gszabi99/War-Thunder-Mobile-Datamine")
                branch = cfg.get("github_mobile_branch", "master")
                sch_enabled = cfg.get("mobile_schedule_enabled", True)
                sch_interval = cfg.get("mobile_schedule_interval_minutes", 15)
                next_check = self.scheduler.next_mobile_check_at
            else:
                repo = cfg.get("github_repo", "gszabi99/War-Thunder-Datamine")
                branch = cfg.get("github_branch", "master")
                sch_enabled = cfg.get("schedule_enabled", True)
                sch_interval = cfg.get("schedule_interval_minutes", 15)
                next_check = self.scheduler.next_check_at

            self._send_json(writer, 200, {
                "target": target_norm,
                "scheduler_running": self.scheduler._running,
                "schedule_enabled": sch_enabled,
                "schedule_interval_minutes": sch_interval,
                "next_check_at": next_check,
                "current_task": task_info,
                "state": state,
                "git_available": git_mgr.is_git_available(),
                "git_ready": git_mgr.is_local_repo_ready(),
                "config_summary": {
                    "target": target_norm,
                    "model": cfg.get("model"),
                    "github_repo": repo,
                    "github_branch": branch,
                    "thinking_mode": cfg.get("thinking_mode"),
                    "has_api_key": cfg.get("has_api_key"),
                    "render_scale": cfg.get("render_scale", "1.5x"),
                },
                "repos": {
                    "pc": {
                        "repo": cfg.get("github_repo", "gszabi99/War-Thunder-Datamine"),
                        "branch": cfg.get("github_branch", "master"),
                    },
                    "mobile": {
                        "repo": cfg.get("github_mobile_repo", "gszabi99/War-Thunder-Mobile-Datamine"),
                        "branch": cfg.get("github_mobile_branch", "master"),
                    }
                }
            })
            return

        # GET /api/latest - 查询最新一次成功生成的分析报告 (支持 target=pc 或 target=mobile)
        if method == "GET" and path == "/api/latest":
            target = query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            latest = self.store.get_latest_report(target=target_norm)
            if not latest or str(latest.get("target") or "pc").lower() != target_norm:
                self._send_json(writer, 200, {"report": None, "target": target_norm, "message": f"暂无{ '手游' if target_norm == 'mobile' else '端游' }分析报告"})
                return
            report_id = latest["id"]
            has_image = bool(latest.get("has_image"))
            self._send_json(writer, 200, {
                "target": target_norm,
                "report": latest,
                "has_image": has_image,
                "image_url": f"/api/report-image/{report_id}" if has_image else None,
                "html_url": f"/api/report-html/{report_id}",
            })
            return

        # GET /api/history - 历史分析报告列表 (支持 target=pc 或 target=mobile)
        if method == "GET" and path == "/api/history":
            target = query.get("target", [None])[0]
            history = self.store.list_history_index(target=target)
            self._send_json(writer, 200, {"target": target or "all", "items": history, "total": len(history)})
            return

        # GET /api/history/{id} - 单份完整分析报告详情
        if method == "GET" and path.startswith("/api/history/"):
            report_id = path[len("/api/history/"):]
            detail = self.store.get_report(report_id)
            if not detail:
                self._send_json(writer, 404, {"error": "报告不存在"})
                return
            self._send_json(writer, 200, {
                "report": detail,
                "image_url": f"/api/report-image/{report_id}" if detail.get("has_image") else None,
                "html_url": f"/api/report-html/{report_id}",
            })
            return

        # DELETE /api/history/{id} 或 POST /api/history/{id}/delete - 删除单份历史报告
        if (method == "DELETE" and path.startswith("/api/history/")) or (method == "POST" and path.startswith("/api/history/") and path.endswith("/delete")):
            report_id = path[len("/api/history/"):]
            if report_id.endswith("/delete"):
                report_id = report_id[:-len("/delete")]
            deleted = self.store.delete_report(report_id)
            if not deleted:
                self._send_json(writer, 404, {"error": "报告不存在或已被删除"})
                return
            self._send_json(writer, 200, {"success": True, "id": report_id, "message": "报告已成功删除"})
            return

        # GET /api/report-image/{id} - 获取报告图片
        if method == "GET" and path.startswith("/api/report-image/"):
            report_id = path[len("/api/report-image/"):]
            report = self.store.get_report(report_id)
            if not report:
                self._send_json(writer, 404, {"error": "报告不存在"})
                return
            img_name = report.get("image_filename") or f"{report_id}.png"
            img_path = self.store.get_image_path(img_name)
            if not img_path:
                self._send_json(writer, 404, {"error": "图片未生成或已删除"})
                return
            img_bytes = img_path.read_bytes()
            self._send_response(writer, 200, img_bytes, content_type="image/png")
            return

        # GET /api/report-html/{id} - 在线浏览 HTML 格式报告
        if method == "GET" and path.startswith("/api/report-html/"):
            report_id = path[len("/api/report-html/"):]
            report = self.store.get_report(report_id)
            if not report:
                self._send_json(writer, 404, {"error": "报告不存在"})
                return
            cfg = self.store.load_config()
            tpl = query.get("template", [None])[0] or cfg.get("render_template", "discord")
            enable_ai = bool(cfg.get("enable_ai_analysis", False))

            # 水印配置：优先使用 query 参数实时预览，其次使用系统配置
            wm_param = query.get("watermark", [None])[0]
            if wm_param is not None:
                wm_enabled = wm_param in ("1", "true", "yes")
            else:
                wm_enabled = bool(cfg.get("watermark_enabled", False))

            wm_config = {
                "watermark_enabled": wm_enabled,
                "watermark_text": query.get("wm_text", [None])[0] or cfg.get("watermark_text", "War Thunder Datamine"),
                "watermark_opacity": float(query.get("wm_opacity", [None])[0] or cfg.get("watermark_opacity", 0.12)),
                "watermark_size": int(query.get("wm_size", [None])[0] or cfg.get("watermark_size", 18)),
                "watermark_density": query.get("wm_density", [None])[0] or cfg.get("watermark_density", "medium"),
            }
            html_text = build_html_report(
                report.get("data") or report,
                template=tpl,
                enable_ai_analysis=enable_ai,
                watermark_config=wm_config,
            )
            self._send_response(writer, 200, html_text.encode("utf-8"), content_type="text/html; charset=utf-8")
            return

        # POST /api/reports/{id}/rerender - 重新渲染报告图片 (支持实时按最新模板、分辨率档位与水印配置重新生成)
        if method == "POST" and path.startswith("/api/reports/") and path.endswith("/rerender"):
            report_id = path.split("/")[3]
            report = self.store.get_report(report_id)
            if not report:
                self._send_json(writer, 404, {"error": "报告不存在"})
                return
            cfg = self.store.load_config()
            tpl = payload.get("template") or cfg.get("render_template", "discord")
            enable_ai = bool(cfg.get("enable_ai_analysis", False))
            target_norm = report.get("target") or "pc"
            is_mobile = (target_norm == "mobile")
            wm_enabled_default = cfg.get("mobile_watermark_enabled" if is_mobile else "watermark_enabled", False)
            wm_text_default = cfg.get("mobile_watermark_text" if is_mobile else "watermark_text", "War Thunder Mobile Datamine" if is_mobile else "War Thunder Datamine")
            wm_opacity_default = cfg.get("mobile_watermark_opacity" if is_mobile else "watermark_opacity", 0.12)
            wm_size_default = cfg.get("mobile_watermark_size" if is_mobile else "watermark_size", 18)
            wm_density_default = cfg.get("mobile_watermark_density" if is_mobile else "watermark_density", "medium")

            wm_config = {
                "watermark_enabled": bool(payload.get("watermark_enabled", wm_enabled_default)),
                "watermark_text": str(payload.get("watermark_text", wm_text_default)),
                "watermark_opacity": float(payload.get("watermark_opacity", wm_opacity_default)),
                "watermark_size": int(payload.get("watermark_size", wm_size_default)),
                "watermark_density": str(payload.get("watermark_density", wm_density_default)),
            }
            render_scale = payload.get("render_scale") or cfg.get("render_scale", "1.5x")
            img_name = report.get("image_filename") or f"{report_id}.png"
            img_path = self.store.get_image_path(img_name)
            from .renderer import render_report_to_image
            img_bytes = await render_report_to_image(
                report.get("data") or report,
                output_path=img_path,
                template=tpl,
                enable_ai_analysis=enable_ai,
                watermark_config=wm_config,
                render_scale=render_scale,
            )
            self._send_json(writer, 200, {
                "success": bool(img_bytes),
                "image_url": f"/api/report-image/{report_id}?t={int(time.time())}"
            })
            return

        # POST /api/git/sync - 同步本地 Git 仓库 (支持 target=pc 或 target=mobile)
        if method == "POST" and path == "/api/git/sync":
            target = payload.get("target") or query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            cfg = self.store.load_config()
            if target_norm == "mobile":
                repo = cfg.get("github_mobile_repo", "gszabi99/War-Thunder-Mobile-Datamine")
                branch = cfg.get("github_mobile_branch", "master")
                git_mgr = self.scheduler.git_manager_mobile
            else:
                repo = cfg.get("github_repo", "gszabi99/War-Thunder-Datamine")
                branch = cfg.get("github_branch", "master")
                git_mgr = self.scheduler.git_manager

            success = await asyncio.to_thread(git_mgr.sync_repo, repo, branch)
            self._send_json(writer, 200, {
                "target": target_norm,
                "repo": repo,
                "branch": branch,
                "success": success,
                "is_ready": git_mgr.is_local_repo_ready(),
            })
            return

        # GET /api/github/commits - 获取 GitHub 最新 commits (支持 target=pc 或 target=mobile)
        if method == "GET" and path == "/api/github/commits":
            target = query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            cfg = self.store.load_config()
            if target_norm == "mobile":
                repo = cfg.get("github_mobile_repo", "gszabi99/War-Thunder-Mobile-Datamine")
                branch = cfg.get("github_mobile_branch", "master")
                git_mgr = self.scheduler.git_manager_mobile
            else:
                repo = cfg.get("github_repo", "gszabi99/War-Thunder-Datamine")
                branch = cfg.get("github_branch", "master")
                git_mgr = self.scheduler.git_manager

            token = cfg.get("github_token", "")
            limit = int(query.get("limit", [15])[0])
            refresh = query.get("refresh", ["0"])[0] in ("1", "true")

            # 优先从本地已同步的 Git 仓库读取 (毫秒级，不受 API 限流)
            if git_mgr.is_local_repo_ready():
                if refresh:
                    await asyncio.to_thread(git_mgr.sync_repo, repo, branch)
                commits = git_mgr.get_commits(branch=branch, limit=limit)
                if not commits:
                    await asyncio.to_thread(git_mgr.sync_repo, repo, branch)
                    commits = git_mgr.get_commits(branch=branch, limit=limit)
                state = self.store.load_state(target=target_norm)
                last_commit = state.get("last_checked_commit")
                self._send_json(writer, 200, {
                    "target": target_norm,
                    "repo": repo,
                    "branch": branch,
                    "source": "local_git",
                    "last_checked_commit": last_commit,
                    "commits": commits,
                })
                return

            try:
                from ..github_client import GitHubClient
                client = GitHubClient(token=token, timeout=15)
                commits = await asyncio.to_thread(client.get_commits, repo, branch=branch, per_page=limit)
                state = self.store.load_state()
                last_commit = state.get("last_checked_commit")
                commit_list = [c.to_dict() if hasattr(c, "to_dict") else c for c in commits]
                self._send_json(writer, 200, {
                    "repo": repo,
                    "branch": branch,
                    "source": "github_api",
                    "last_checked_commit": last_commit,
                    "commits": commit_list,
                })
            except Exception as exc:
                self._send_json(writer, 500, {"error": f"获取 GitHub 提交失败: {exc}"})
            return

        # GET /api/config - 获取配置 (API Key 脱敏)
        if method == "GET" and path == "/api/config":
            self._send_json(writer, 200, self.store.get_masked_config())
            return

        # POST /api/config - 更新配置
        # POST /api/config - 更新配置
        if method == "POST" and path == "/api/config":
            allowed_keys = {
                "openai_base_url", "openai_api_key", "model", "backup_models",
                "summary_model", "review_model", "review_mode",
                "thinking_mode", "thinking_budget_tokens", "temperature",
                "github_repo", "github_branch", "github_token",
                "schedule_interval_minutes", "schedule_enabled",
                "enable_struct_diff", "max_files_per_chunk", "max_chars_per_chunk",
                "enable_model_tool_calls", "max_tool_call_rounds", "enable_dynamic_context_queue",
                "render_template", "max_history_reports", "enable_ai_analysis",
                "watermark_enabled", "watermark_text", "watermark_opacity", "watermark_size", "watermark_density",
                "render_scale",
                "github_mobile_repo", "github_mobile_branch",
                "mobile_schedule_interval_minutes", "mobile_schedule_enabled",
                "mobile_watermark_enabled", "mobile_watermark_text",
                "mobile_watermark_opacity", "mobile_watermark_size", "mobile_watermark_density",
            }
            update_data = {}
            for k, v in payload.items():
                if k in allowed_keys:
                    # 如果 api_key 是空字符串且之前有，则不覆盖；只有传新非空 key 才更新
                    if k == "openai_api_key" and not v:
                        continue
                    update_data[k] = v
            saved = self.store.save_config(update_data)
            self._send_json(writer, 200, {"success": True, "config": self.store.get_masked_config()})
            return

        # POST /api/models/fetch - 从 OpenAI 兼容服务动态获取模型列表
        if method == "POST" and path == "/api/models/fetch":
            cfg = self.store.load_config()
            base_url = (payload.get("base_url") or cfg.get("openai_base_url") or "").rstrip("/")
            api_key = payload.get("api_key") or cfg.get("openai_api_key") or ""

            if not base_url:
                self._send_json(writer, 400, {"error": "未提供 base_url"})
                return

            models = await self._fetch_remote_models(base_url, api_key)
            self._send_json(writer, 200, {"models": models, "count": len(models)})
            return

        # POST /api/analyze/trigger - 手动触发更新检查与分析 (支持 target=pc 或 target=mobile)
        if method == "POST" and path == "/api/analyze/trigger":
            target = payload.get("target") or query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            task_info = self.scheduler.get_task_info(target=target_norm)

            if task_info.get("status") == "running":
                self._send_json(writer, 409, {"error": f"当前已有{'手游' if target_norm == 'mobile' else '端游'}分析任务正在执行中", "task": task_info})
                return

            mode = payload.get("mode", "latest")
            compare_range = payload.get("compare_range")
            diff_text = payload.get("diff_text")
            custom_base = payload.get("custom_base", "")
            custom_head = payload.get("custom_head", "")

            self.scheduler.prepare_task_info(
                mode=mode,
                compare_range=compare_range if mode == "compare" else None,
                is_diff=(mode == "diff"),
                target=target_norm,
            )

            # 在后台异步启动分析
            async def _bg_run():
                try:
                    await self.scheduler.trigger_check(
                        trigger_mode="manual",
                        compare_range=compare_range if mode == "compare" else None,
                        diff_text=diff_text if mode == "diff" else None,
                        custom_base=custom_base,
                        custom_head=custom_head,
                        target=target_norm,
                    )
                except Exception as e:
                    _logger.error(f"手动触发{'手游' if target_norm == 'mobile' else '端游'}分析失败: {e}")

            asyncio.create_task(_bg_run())
            self._send_json(writer, 202, {
                "message": f"{'手游' if target_norm == 'mobile' else '端游'}分析任务已启动",
                "target": target_norm,
                "task": self.scheduler.get_task_info(target=target_norm),
            })
            return

        # POST /api/analyze/retry 或 /api/task/retry - 重试上次执行的任务 (支持 target=pc 或 target=mobile)
        if method == "POST" and path in ("/api/analyze/retry", "/api/task/retry"):
            target = payload.get("target") or query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            task_info = self.scheduler.get_task_info(target=target_norm)

            if task_info.get("status") == "running":
                self._send_json(writer, 409, {"error": f"当前已有{'手游' if target_norm == 'mobile' else '端游'}分析任务正在执行中", "task": task_info})
                return

            self.scheduler.prepare_task_info(mode="retry", target=target_norm)

            async def _bg_retry():
                try:
                    await self.scheduler.retry_last_task(target=target_norm)
                except Exception as e:
                    _logger.error(f"重试{'手游' if target_norm == 'mobile' else '端游'}任务失败: {e}")

            asyncio.create_task(_bg_retry())
            self._send_json(writer, 202, {
                "message": f"{'手游' if target_norm == 'mobile' else '端游'}重试任务已启动",
                "target": target_norm,
                "task": self.scheduler.get_task_info(target=target_norm),
            })
            return

        # POST /api/task/clear-logs - 清空当前任务日志 (支持 target=pc 或 target=mobile)
        if method == "POST" and path == "/api/task/clear-logs":
            target = payload.get("target") or query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            self.scheduler.clear_logs(target=target_norm)
            self._send_json(writer, 200, {"success": True, "target": target_norm, "message": "任务日志已清空"})
            return

        # GET /api/task/status 或 /api/task/current - 获取当前任务详情与实时日志 (支持 target=pc 或 target=mobile)
        if method == "GET" and path in ("/api/task/status", "/api/task/current"):
            target = query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            task_info = self.scheduler.get_task_info(target=target_norm)
            self._send_json(writer, 200, {
                "target": target_norm,
                "task": task_info,
                "can_retry": task_info.get("can_retry", True),
            })
            return

        # POST /api/scheduler/toggle - 开启/暂停定时调度器 (支持 target=pc 或 target=mobile)
        if method == "POST" and path == "/api/scheduler/toggle":
            target = payload.get("target") or query.get("target", ["pc"])[0]
            target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
            enabled = bool(payload.get("enabled", True))
            if target_norm == "mobile":
                self.store.save_config({"mobile_schedule_enabled": enabled})
                self._send_json(writer, 200, {"target": "mobile", "mobile_schedule_enabled": enabled})
            else:
                self.store.save_config({"schedule_enabled": enabled})
                self._send_json(writer, 200, {"target": "pc", "schedule_enabled": enabled})
            return
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        def _sync_fetch() -> list[str]:
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    model_list = []
                    # OpenAI 标准格式: {"data": [{"id": "model_name"}, ...]}
                    for item in data.get("data") or []:
                        m_id = item.get("id") if isinstance(item, dict) else str(item)
                        if m_id:
                            model_list.append(str(m_id))
                    # 或者部分自定义网关格式: {"models": [...]}
                    for item in data.get("models") or []:
                        m_id = item.get("id") or item.get("name") if isinstance(item, dict) else str(item)
                        if m_id:
                            model_list.append(str(m_id))
                    return sorted(list(set(model_list)))
            except Exception as e:
                _logger.warning("获取模型列表异常: %s", e)
                raise RuntimeError(f"获取模型列表失败: {e}")

        return await asyncio.to_thread(_sync_fetch)

    def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status_code: int,
        body: bytes,
        content_type: str = "text/plain",
        extra_headers: dict[str, str] | None = None,
        is_head: bool = False,
    ) -> None:
        status_text = {
            200: "OK",
            202: "Accepted",
            204: "No Content",
            400: "Bad Request",
            404: "Not Found",
            409: "Conflict",
            500: "Internal Server Error",
        }.get(status_code, "OK")

        lines = [
            f"HTTP/1.1 {status_code} {status_text}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Headers: Content-Type, Authorization",
            "Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                lines.append(f"{k}: {v}")
        header_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        writer.write(header_bytes if is_head else (header_bytes + body))

    def _send_json(self, writer: asyncio.StreamWriter, status_code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._send_response(writer, status_code, body, content_type="application/json; charset=utf-8")

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """启动 Web 服务与后台调度器。"""
        self.scheduler.start()
        self.server = await asyncio.start_server(self.handle_request, host, port)
        _logger.info(f"wtup WebUI 已在 http://{host}:{port} 启动")

    async def run_forever(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        await self.start(host, port)
        async with self.server:
            await self.server.serve_forever()


def run_server(host: str = "0.0.0.0", port: int = 8080, data_dir: str = "data") -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )
    app = WebApp(data_dir)
    try:
        asyncio.run(app.run_forever(host, port))
    except KeyboardInterrupt:
        _logger.info("服务收到终止信号，正在退出...")


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8080))
    run_server(host, port)
