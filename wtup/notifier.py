from __future__ import annotations

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


async def push_report(context: Any, targets: list[str], *, image_path: Path | None, fallback_text: str) -> tuple[int, int]:
    success = 0
    failed = 0
    for target in targets:
        try:
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
