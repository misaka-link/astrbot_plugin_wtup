from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, MessageChain, filter
    from astrbot.api.message_components import Image, Plain
    from astrbot.api.star import Context, Star, register
except ModuleNotFoundError:
    # 允许在独立测试环境下运行
    import logging
    logger = logging.getLogger("astrbot_plugin_wtup")
    Context = Any
    AstrBotConfig = dict
    AstrMessageEvent = Any
    class MessageChain:
        def __init__(self):
            self.elements = []
        def message(self, elem):
            self.elements.append(elem)
            return self
    class Image:
        @staticmethod
        def fromFileSystem(path):
            return path
        @staticmethod
        def fromBase64(b64):
            return b64
    class Plain:
        def __init__(self, text=""):
            self.text = text
    class Star:
        def __init__(self, context=None):
            self.context = context
    def register(*args, **kwargs):
        return lambda cls: cls
    class _Filter:
        @staticmethod
        def command(*args, **kwargs):
            return lambda fn: fn
        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda fn: fn
    filter = _Filter()


PLUGIN_NAME = "astrbot_plugin_wtup"
PLUGIN_VERSION = "v1.0.0"
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_API_BASE_URL = "http://10.10.10.99:1883"


@register(PLUGIN_NAME, "御坂_20001", "War Thunder 更新推送客户端 (API版)", PLUGIN_VERSION)
class WTUpdateClient(Star):
    """War Thunder Datamine 轻量级推送客户端。

    仅负责：
    1. 默认每隔 5 分钟轮询一次 WTUP 后台 API；
    2. 检测到新分析报告后，自动下载渲染好的长图和摘要并推送到群聊；
    3. 支持 /wtup_check 手动检查与 /wtup_status 状态查看。
    完全移除了模型分析、Git 仓库拉取、解包等重型开销，极度轻量与高可用。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = self._resolve_data_dir()
        self.state_file = self.data_dir / "client_state.json"

        # 核心比对基准：持久化记录已经推送过的报告 ID (端游与手游独立)
        self.last_sent_report_id = self._load_last_sent_report_id("pc")
        self.mobile_last_sent_report_id = self._load_last_sent_report_id("mobile")

        self.last_check_time = None
        self.last_check_status = "就绪"

        self.mobile_last_check_time = None
        self.mobile_last_check_status = "未开启" if not self._get_config_bool("enable_mobile_push", False) else "就绪"

        self._task: asyncio.Task[None] | None = None

    async def initialize(self):
        """AstrBot 框架生命周期钩子：在事件循环就绪后启动后台定时任务。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            pc_interval = self._get_config_int("interval_minutes", DEFAULT_INTERVAL_MINUTES)
            mobile_enabled = self._get_config_bool("enable_mobile_push", False)
            mobile_interval = self._get_config_int("mobile_interval_minutes", DEFAULT_INTERVAL_MINUTES)
            logger.info(
                "[%s] 后台轮询服务已启动 (端游周期: %d 分钟 | 手游推送: %s, 手游周期: %d 分钟)",
                PLUGIN_NAME,
                pc_interval,
                "开启" if mobile_enabled else "关闭",
                mobile_interval,
            )

    async def terminate(self):
        """AstrBot 框架生命周期钩子：在插件卸载/重载时平稳取消任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _resolve_data_dir(self) -> Path:
        data_dir = Path("data") / "plugins" / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    def _load_last_sent_report_id(self, target: str = "pc") -> str | None:
        """从本地持久化文件读取已推送过的报告 ID (区分端游与手游)。"""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if target == "mobile":
                        return data.get("mobile_last_sent_report_id")
                    return data.get("last_sent_report_id") or data.get("last_reported_id")
            except Exception:
                pass
        return None

    def _save_last_sent_report_id(self, report_id: str, target: str = "pc") -> None:
        """持久化保存已推送成功的报告 ID，用于下次比对。"""
        if target == "mobile":
            self.mobile_last_sent_report_id = report_id
        else:
            self.last_sent_report_id = report_id

        data = {}
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        if target == "mobile":
            data["mobile_last_sent_report_id"] = report_id
            data["mobile_last_check_time"] = now_str
        else:
            data["last_sent_report_id"] = report_id
            data["last_check_time"] = now_str

        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("[%s] 保存客户端已推送状态失败: %s", PLUGIN_NAME, exc)

    def _get_config_str(self, key: str, default: str) -> str:
        val = self.config.get(key)
        return str(val).strip() if val is not None and str(val).strip() else default

    def _get_config_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (ValueError, TypeError):
            return default

    def _get_config_bool(self, key: str, default: bool) -> bool:
        val = self.config.get(key)
        if val is None:
            return default
        return bool(val)

    def _get_config_list(self, key: str) -> list[str]:
        raw = self.config.get(key)
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str) and raw.strip():
            return [x.strip() for x in raw.replace(",", " ").split() if x.strip()]
        return []

    def _get_push_targets(self, target: str = "pc") -> list[str]:
        """获取推送目标列表，支持端游与手游群聊独立隔离配置。"""
        if target == "mobile":
            return self._get_config_list("mobile_push_targets")
        targets = self._get_config_list("push_targets")
        if not targets:
            targets = self._get_config_list("target_groups")
        return targets

    async def _resolve_api_base(self) -> str:
        """智能解析可用的 API 服务端地址，容器内外自动适配。"""
        configured = self._get_config_str("api_base_url", DEFAULT_API_BASE_URL).rstrip("/")
        candidates = [configured]
        if "127.0.0.1" in configured or "localhost" in configured:
            candidates.extend([
                "http://10.10.10.99:1883",
                "http://wtup-datamine-monitor:8080",
                "http://172.17.0.1:1883",
                "http://172.27.0.1:1883",
                "http://host.docker.internal:1883",
            ])
        for base in candidates:
            try:
                req = urllib.request.Request(f"{base}/api/status", headers={"User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}"})
                await asyncio.to_thread(self._sync_fetch, req, 2)
                return base
            except Exception:
                continue
        return configured

    async def _poll_loop(self) -> None:
        """后台双通道定时轮询循环 (独立管理端游与手游监控)。"""
        await asyncio.sleep(5)
        last_pc_check = 0.0
        last_mobile_check = 0.0

        while True:
            now_ts = time.time()
            pc_interval = max(1, self._get_config_int("interval_minutes", DEFAULT_INTERVAL_MINUTES)) * 60
            enable_mobile = self._get_config_bool("enable_mobile_push", False)
            mobile_interval = max(1, self._get_config_int("mobile_interval_minutes", DEFAULT_INTERVAL_MINUTES)) * 60

            # 1. 端游定时轮询
            if now_ts - last_pc_check >= pc_interval:
                last_pc_check = now_ts
                try:
                    await self.check_and_push(manual=False, target="pc")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self.last_check_status = f"异常: {exc}"
                    logger.error("[%s] 端游轮询 API 发生异常: %s", PLUGIN_NAME, exc)

            # 2. 手游定时轮询
            if enable_mobile and (now_ts - last_mobile_check >= mobile_interval):
                last_mobile_check = now_ts
                try:
                    await self.check_and_push(manual=False, target="mobile")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self.mobile_last_check_status = f"异常: {exc}"
                    logger.error("[%s] 手游轮询 API 发生异常: %s", PLUGIN_NAME, exc)

            await asyncio.sleep(15)

    async def check_and_push(self, manual: bool = False, event: Any = None, target: str = "pc") -> tuple[bool, str]:
        """向后台 API 查询最新结果并执行推送 (支持 target=pc 或 target=mobile)。"""
        target_norm = "mobile" if str(target).lower() in ("mobile", "shouyou", "手游") else "pc"
        is_mobile = (target_norm == "mobile")
        channel_name = "手游" if is_mobile else "端游"

        api_base = await self._resolve_api_base()
        template = self._get_config_str("render_template", "discord")
        push_targets = self._get_push_targets(target=target_norm)
        enable_image = self._get_config_bool("enable_push_image", True)
        enable_text = self._get_config_bool("enable_push_text", True)

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        if is_mobile:
            self.mobile_last_check_time = now_str
        else:
            self.last_check_time = now_str

        latest_url = f"{api_base}/api/latest?target={target_norm}"

        try:
            req = urllib.request.Request(latest_url, headers={"User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}"})
            resp_bytes = await asyncio.to_thread(self._sync_fetch, req, 12)
            data = json.loads(resp_bytes.decode("utf-8"))
        except Exception as exc:
            msg = f"连接 WTUP API ({api_base}) 失败: {exc}"
            if is_mobile:
                self.mobile_last_check_status = msg
            else:
                self.last_check_status = msg
            logger.warning("[%s] [%s] %s", PLUGIN_NAME, channel_name, msg)
            if manual and event:
                await event.send(MessageChain().message(f"❌ {channel_name}: {msg}"))
            return False, msg

        report = data.get("report")
        if not report:
            msg = f"后台 API 尚未生成任何{channel_name}分析报告"
            if is_mobile:
                self.mobile_last_check_status = msg
            else:
                self.last_check_status = msg
            if manual and event:
                await event.send(MessageChain().message(f"ℹ️ {msg}"))
            return False, msg

        # 严格类型校验：双重防呆锁，确保返回报告的 target 与当前通道严格一致，防止端游/手游串线
        report_target = str(report.get("target") or "").lower()
        title_raw = str(report.get("report_title") or "")
        if not report_target:
            if title_raw.startswith("1.") or "mobile" in title_raw.lower() or "手游" in title_raw:
                report_target = "mobile"
            else:
                report_target = "pc"

        if is_mobile and report_target != "mobile":
            err_msg = f"通道数据异常拦截: 当前请求手游更新，但 API 返回了端游报告 ({title_raw})，已安全拦截丢弃！"
            logger.warning("[%s] %s", PLUGIN_NAME, err_msg)
            self.mobile_last_check_status = err_msg
            if manual and event:
                await event.send(MessageChain().message(f"⚠️ {err_msg}"))
            return False, "类型不匹配拦截"

        if not is_mobile and report_target == "mobile":
            err_msg = f"通道数据异常拦截: 当前请求端游更新，但 API 返回了手游报告 ({title_raw})，已安全拦截丢弃！"
            logger.warning("[%s] %s", PLUGIN_NAME, err_msg)
            self.last_check_status = err_msg
            if manual and event:
                await event.send(MessageChain().message(f"⚠️ {err_msg}"))
            return False, "类型不匹配拦截"

        report_id = report.get("id")
        last_sent = self.mobile_last_sent_report_id if is_mobile else self.last_sent_report_id
        if not manual and report_id and report_id == last_sent:
            status_msg = f"已比对无新更新 (最新{channel_name}报告 {report_id} 已发送过)"
            if is_mobile:
                self.mobile_last_check_status = status_msg
            else:
                self.last_check_status = status_msg
            return False, "无新更新"

        # 发现新报告！开始准备消息
        title = report.get("report_title") or (f"War Thunder {channel_name} 更新" if is_mobile else "War Thunder Datamine 更新")
        importance = report.get("importance") or "中"
        tags = " · ".join(report.get("tags") or [])
        summary = report.get("summary") or ""

        header_title = f"📢 【War Thunder 手游更新推送】" if is_mobile else f"📢 【War Thunder 更新推送】"
        caption_lines = [header_title, f"📌 {title}"]
        if tags:
            caption_lines.append(f"🏷️ 标签: {tags}")
        caption_lines.append(f"⚡ 重要度: {importance}")
        if summary:
            caption_lines.append(f"📝 概述: {summary}")

        caption_text = "\n".join(caption_lines)

        # 尝试下载渲染长图
        image_bytes = None
        if enable_image and report_id:
            img_url = f"{api_base}/api/report-image/{report_id}?template={template}&target={target_norm}"
            try:
                img_req = urllib.request.Request(img_url, headers={"User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}"})
                image_bytes = await asyncio.to_thread(self._sync_fetch, img_req, 20)
            except Exception as exc:
                logger.warning("[%s] [%s] 从 API 下载长图失败: %s，回退为纯文字", PLUGIN_NAME, channel_name, exc)

        # 执行发送
        success_count = 0
        if manual and event:
            await self._send_to_event(event, caption_text if enable_text else "", image_bytes)
            success_count += 1
        elif push_targets:
            for target_id in push_targets:
                try:
                    await self._send_to_target(target_id, caption_text if enable_text else "", image_bytes)
                    success_count += 1
                except Exception as exc:
                    logger.warning("[%s] [%s] 推送到目标 %s 失败: %s", PLUGIN_NAME, channel_name, target_id, exc)

        # 持久化已推送的 report_id
        if report_id:
            self._save_last_sent_report_id(report_id, target=target_norm)

        status_msg = f"成功推送报告 {report_id} 到 {success_count} 个目标 ({channel_name})"
        if is_mobile:
            self.mobile_last_check_status = status_msg
        else:
            self.last_check_status = status_msg
        logger.info("[%s] [%s] %s", PLUGIN_NAME, channel_name, status_msg)
        return True, status_msg

    async def _send_to_event(self, event: Any, text: str, image_bytes: bytes | None) -> None:
        chain = MessageChain()
        if text:
            chain.message(text)
        if image_bytes:
            chain.message(self._build_image_component(image_bytes))
        await event.send(chain)

    async def _collect_call_targets(self) -> list[Any]:
        seen: set[int] = set()
        call_targets: list[Any] = []

        def add(candidate: Any) -> None:
            if candidate is None:
                return
            target = candidate if hasattr(candidate, "call_action") else getattr(candidate, "api", None)
            if target is not None and hasattr(target, "call_action"):
                marker = id(target)
                if marker not in seen:
                    seen.add(marker)
                    call_targets.append(target)

        add(self.context)
        for attr in ("bot", "client", "api"):
            add(getattr(self.context, attr, None))

        get_bot = getattr(self.context, "get_bot", None)
        if callable(get_bot):
            try:
                bot = get_bot()
                if inspect.isawaitable(bot):
                    bot = await bot
                add(bot)
            except Exception:
                pass

        platform_manager = getattr(self.context, "platform_manager", None) or getattr(self.context, "_platform_manager", None)
        if platform_manager:
            insts = getattr(platform_manager, "platform_insts", None) or getattr(platform_manager, "get_insts", None)
            if callable(insts):
                try:
                    insts = insts()
                    if inspect.isawaitable(insts):
                        insts = await insts
                except Exception:
                    insts = None
            for p in (insts or []):
                add(p)
                for attr in ("bot", "client", "api"):
                    cand = getattr(p, attr, None)
                    add(cand)
                    if cand:
                        add(getattr(cand, "api", None))
                get_client = getattr(p, "get_client", None)
                if callable(get_client):
                    try:
                        c = get_client()
                        if inspect.isawaitable(c):
                            c = await c
                        add(c)
                        if c:
                            add(getattr(c, "api", None))
                    except Exception:
                        pass
        return call_targets

    async def _send_to_target(self, target: str, text: str, image_bytes: bytes | None) -> None:
        """向目标发送群聊/私聊消息 (支持 AstrBot Star 发送机制与 OneBot call_action)。"""
        if target.isdigit():
            msg_data = []
            if text:
                msg_data.append({"type": "text", "data": {"text": text}})
            if image_bytes:
                b64 = base64.b64encode(image_bytes).decode("utf-8")
                msg_data.append({"type": "image", "data": {"file": f"base64://{b64}"}})

            call_targets = await self._collect_call_targets()
            for ct in call_targets:
                try:
                    res = ct.call_action("send_group_msg", group_id=int(target), message=msg_data)
                    if inspect.isawaitable(res):
                        await res
                    return
                except Exception:
                    continue

        chain = MessageChain()
        if text:
            chain.message(text)
        if image_bytes:
            chain.message(self._build_image_component(image_bytes))

        if hasattr(self.context, "send_message"):
            try:
                await self.context.send_message(target, chain)
                return
            except Exception as exc:
                logger.debug("[%s] context.send_message 异常: %s", PLUGIN_NAME, exc)

        raise RuntimeError(f"无法向目标 {target} 发送消息：未找到适用的发送渠道")

    def _build_image_component(self, image_bytes: bytes) -> Any:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(image_bytes)
        tmp.close()
        try:
            return Image.fromFileSystem(tmp.name)
        except Exception:
            return Image.fromBase64(base64.b64encode(image_bytes).decode("utf-8"))

    @staticmethod
    def _sync_fetch(req: urllib.request.Request, timeout: float) -> bytes:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    @filter.command("wtup_check")
    async def cmd_check(self, event: AstrMessageEvent):
        """手动向 WTUP 后台 API 检查更新并推送。支持: /wtup_check (端游) 或 /wtup_check mobile (手游)。"""
        admin_targets = self._get_config_list("admin_targets")
        if admin_targets:
            sender_id = str(getattr(event, "get_sender_id", lambda: "")() or getattr(event, "sender_id", "")).strip()
            if sender_id not in admin_targets:
                await event.send(MessageChain().message("⛔ 仅管理员可执行 /wtup_check 强制检查。"))
                return

        msg_str = str(getattr(event, "message_str", "") or "").lower()
        target = "mobile" if any(w in msg_str for w in ("mobile", "手游", "手雷")) else "pc"
        label = "手游" if target == "mobile" else "端游"

        await event.send(MessageChain().message(f"🔍 正在向 WTUP 后台 API 查询最新{label}报告..."))
        success, msg = await self.check_and_push(manual=True, event=event, target=target)
        if not success:
            await event.send(MessageChain().message(f"提示: {msg}"))

    @filter.command("wtup_mobile_check")
    async def cmd_mobile_check(self, event: AstrMessageEvent):
        """手动检查 WTUP 手游更新并推送。"""
        admin_targets = self._get_config_list("admin_targets")
        if admin_targets:
            sender_id = str(getattr(event, "get_sender_id", lambda: "")() or getattr(event, "sender_id", "")).strip()
            if sender_id not in admin_targets:
                await event.send(MessageChain().message("⛔ 仅管理员可执行 /wtup_mobile_check 强制检查。"))
                return

        await event.send(MessageChain().message("🔍 正在向 WTUP 后台 API 查询最新手游报告..."))
        success, msg = await self.check_and_push(manual=True, event=event, target="mobile")
        if not success:
            await event.send(MessageChain().message(f"提示: {msg}"))

    @filter.command("wtup_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 WTUP 客户端状态 (包含端游与手游监控)。"""
        api_base = await self._resolve_api_base()
        interval = self._get_config_int("interval_minutes", DEFAULT_INTERVAL_MINUTES)
        targets = self._get_push_targets("pc")
        template = self._get_config_str("render_template", "discord")

        enable_mobile = self._get_config_bool("enable_mobile_push", False)
        mobile_interval = self._get_config_int("mobile_interval_minutes", DEFAULT_INTERVAL_MINUTES)
        mobile_targets = self._get_push_targets("mobile")

        lines = [
            "📊 【WTUP 推送客户端状态】",
            f"🔗 后台 API: {api_base}",
            f"🎨 渲染模板: {template}",
            "",
            "🎮 ── 端游监控 (PC / Console) ──",
            f"⏱️ 轮询周期: {interval} 分钟",
            f"📢 目标群数: {len(targets)} 个",
            f"🕒 上次检查: {self.last_check_time or '尚未检查'}",
            f"📌 已推送报告: {self.last_sent_report_id or '暂无'}",
            f"📝 状态说明: {self.last_check_status}",
            "",
            "📱 ── 手游监控 (WT Mobile) ──",
            f"⚡ 手游推送开关: {'已开启' if enable_mobile else '已关闭'}",
            f"⏱️ 轮询周期: {mobile_interval} 分钟",
            f"📢 目标群数: {len(mobile_targets)} 个",
            f"🕒 上次检查: {self.mobile_last_check_time or '尚未检查'}",
            f"📌 已推送报告: {self.mobile_last_sent_report_id or '暂无'}",
            f"📝 状态说明: {self.mobile_last_check_status}",
        ]
        await event.send(MessageChain().message("\n".join(lines)))
