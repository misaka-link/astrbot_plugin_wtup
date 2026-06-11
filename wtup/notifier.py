from __future__ import annotations

import base64
import inspect
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
    from astrbot.api.event import MessageChain
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)
    MessageChain = None

from .config import PLUGIN_NAME


def _is_group_id(target: str) -> bool:
    return target.isdigit()


def _onebot_image_file(path: Path) -> str:
    return f"base64://{base64.b64encode(path.read_bytes()).decode('utf-8')}"


def _build_onebot_message(*, image_path: Path | None, fallback_text: str) -> list[dict[str, Any]]:
    if image_path and image_path.exists():
        return [{"type": "image", "data": {"file": _onebot_image_file(image_path)}}]
    return [{"type": "text", "data": {"text": fallback_text}}]


async def _collect_call_targets(context: Any) -> list[Any]:
    seen: set[int] = set()
    call_targets: list[Any] = []

    def add(candidate: Any) -> None:
        if candidate is None:
            return
        call_target = candidate if hasattr(candidate, "call_action") else getattr(candidate, "api", None)
        if call_target is None or not hasattr(call_target, "call_action"):
            return
        marker = id(call_target)
        if marker in seen:
            return
        seen.add(marker)
        call_targets.append(call_target)

    add(context)
    for attr in ("bot", "client", "api"):
        candidate = getattr(context, attr, None)
        add(candidate)
        add(getattr(candidate, "api", None))

    get_bot = getattr(context, "get_bot", None)
    if callable(get_bot):
        try:
            bot = get_bot()
            if inspect.isawaitable(bot):
                bot = await bot
            add(bot)
        except Exception as exc:
            logger.debug("[%s] 获取 context bot 失败: %s", PLUGIN_NAME, exc)

    platform_managers = [
        getattr(context, "platform_manager", None),
        getattr(context, "_platform_manager", None),
    ]
    for platform_manager in platform_managers:
        get_insts = getattr(platform_manager, "get_insts", None)
        if not callable(get_insts):
            continue

        try:
            platforms = get_insts()
            if inspect.isawaitable(platforms):
                platforms = await platforms
        except Exception as exc:
            logger.debug("[%s] 获取平台实例失败: %s", PLUGIN_NAME, exc)
            continue

        if not isinstance(platforms, (list, tuple)):
            continue

        for platform in platforms:
            add(platform)
            get_client = getattr(platform, "get_client", None)
            if callable(get_client):
                try:
                    client = get_client()
                    if inspect.isawaitable(client):
                        client = await client
                    add(client)
                    add(getattr(client, "api", None))
                except Exception as exc:
                    logger.debug("[%s] 获取平台客户端失败: %s", PLUGIN_NAME, exc)
            for attr in ("bot", "client", "api"):
                candidate = getattr(platform, attr, None)
                add(candidate)
                add(getattr(candidate, "api", None))

    return call_targets


async def _collect_event_call_targets(event: Any) -> list[Any]:
    seen: set[int] = set()
    call_targets: list[Any] = []

    def add(candidate: Any) -> None:
        if candidate is None:
            return
        call_target = candidate if hasattr(candidate, "call_action") else getattr(candidate, "api", None)
        if call_target is None or not hasattr(call_target, "call_action"):
            return
        marker = id(call_target)
        if marker in seen:
            return
        seen.add(marker)
        call_targets.append(call_target)

    for attr in ("bot", "client", "api"):
        candidate = getattr(event, attr, None)
        add(candidate)
        add(getattr(candidate, "api", None))

    return call_targets


async def _iter_call_targets(context: Any, extra_call_targets: list[Any] | None) -> list[Any]:
    seen: set[int] = set()
    call_targets: list[Any] = []

    def add(call_target: Any) -> None:
        if call_target is None:
            return
        if not hasattr(call_target, "call_action"):
            return
        marker = id(call_target)
        if marker in seen:
            return
        seen.add(marker)
        call_targets.append(call_target)

    for call_target in extra_call_targets or []:
        add(call_target)
    for call_target in await _collect_call_targets(context):
        add(call_target)
    return call_targets


async def _call_action(call_target: Any, action: str, **params: Any) -> Any:
    result = call_target.call_action(action, **params)
    if inspect.isawaitable(result):
        return await result
    return result


async def _send_group_message_by_id(
    context: Any,
    group_id: str,
    *,
    image_path: Path | None,
    fallback_text: str,
    extra_call_targets: list[Any] | None,
) -> None:
    message = _build_onebot_message(image_path=image_path, fallback_text=fallback_text)
    errors: list[str] = []

    for call_target in await _iter_call_targets(context, extra_call_targets):
        try:
            await _call_action(call_target, "send_group_msg", group_id=int(group_id), message=message)
            return
        except Exception as exc:
            errors.append(str(exc))

    if errors:
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("未找到可用的 OneBot call_action 客户端")


async def push_report(
    context: Any,
    targets: list[str],
    *,
    image_path: Path | None,
    fallback_text: str,
    event: Any | None = None,
) -> tuple[int, int]:
    success = 0
    failed = 0
    extra_call_targets = await _collect_event_call_targets(event) if event is not None else None
    for target in targets:
        target = str(target).strip()
        if not target:
            continue
        try:
            if _is_group_id(target):
                await _send_group_message_by_id(
                    context,
                    target,
                    image_path=image_path,
                    fallback_text=fallback_text,
                    extra_call_targets=extra_call_targets,
                )
            else:
                if MessageChain is None:
                    raise RuntimeError("AstrBot MessageChain is unavailable")
                chain = MessageChain()
                if image_path and image_path.exists():
                    chain = chain.file_image(str(image_path))
                else:
                    chain = chain.message(fallback_text)
                await context.send_message(target, chain)
            success += 1
        except Exception as exc:
            failed += 1
            logger.warning("[%s] 推送到 %s 失败: %s", PLUGIN_NAME, target, exc)
    return success, failed
