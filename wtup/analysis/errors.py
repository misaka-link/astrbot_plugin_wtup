from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME, PluginConfig
from ..diff_collector import DiffChunk, DiffSummary


def record_model_error(
    settings: PluginConfig,
    stage: str,
    error: BaseException | str,
    *,
    summary: DiffSummary | None = None,
    chunk: DiffChunk | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    recorder = getattr(settings, "model_error_recorder", None)
    if not callable(recorder):
        return
    metadata: dict[str, Any] = dict(extra or {})
    if summary is not None:
        metadata.update(
            {
                "base_sha": summary.base_sha,
                "head_sha": summary.head_sha,
                "compare_url": summary.compare_url,
                "total_files": summary.total_files,
            }
        )
    if chunk is not None:
        metadata.update(
            {
                "chunk_index": chunk.index,
                "chunk_total": chunk.total,
                "chunk_files": [str(file_info.get("filename") or "") for file_info in chunk.files],
                "chunk_patch_chars": chunk.patch_chars,
            }
        )
    try:
        recorder(stage, error, metadata)
    except Exception as exc:
        logger.warning("[%s] 保存模型错误日志失败: %s", PLUGIN_NAME, exc)
