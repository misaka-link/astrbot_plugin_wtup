from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import AnalyzerConfig
from .models import DiffChunk, DiffSummary

_logger = logging.getLogger("wtup_standalone")


@dataclass(frozen=True)
class ModelErrorInfo:
    category: str
    hint: str
    deterministic: bool


_DETERMINISTIC_HINTS: dict[str, str] = {
    "model_unavailable": (
        "模型在当前渠道不可用（model_not_found / 无可用渠道）。"
        "请检查模型名称，或把该模型从 model / backup_models 移除。"
    ),
    "invalid_request_format": (
        "请求格式不被兼容端点支持。请检查接口地址与模型名称。"
    ),
    "auth": "模型鉴权失败（401 / invalid key / unauthorized），请检查 API Key 与 base_url。",
}


def classify_model_error(error: BaseException | str) -> ModelErrorInfo:
    """识别模型请求错误的类型，返回分类、中文提示和是否确定性失败。"""
    text = str(error or "")
    low = text.lower()

    context_leftover_markers = (
        "exceeds the maximum",
        "maximum number of tokens",
        "input token count exceeds",
        "too many tokens",
        "prompt is too long",
        "context length",
        "context_length",
        "context_window_exceeded",
        "context window",
        "over the context",
        "input length exceeded",
    )
    if any(marker in low for marker in context_leftover_markers):
        return ModelErrorInfo(
            "context_overflow",
            "模型输入超过上下文上限。建议调低 max_input_tokens，或更换更大上下文模型。",
            deterministic=False,
        )
    if "model_not_found" in low or "no available channel for model" in low or "model_not_exist" in low:
        return ModelErrorInfo("model_unavailable", _DETERMINISTIC_HINTS["model_unavailable"], deterministic=True)
    if "field messages is required" in low or ("messages" in low and "required" in low):
        return ModelErrorInfo("invalid_request_format", _DETERMINISTIC_HINTS["invalid_request_format"], deterministic=True)
    if "unauthorized" in low or "invalid api key" in low or "auth" in low or "401" in text or "认证失败" in text:
        return ModelErrorInfo("auth", _DETERMINISTIC_HINTS["auth"], deterministic=True)
    if "rate limit" in low or "429" in text or "quota" in low or "限流" in text or "请求过多" in text:
        return ModelErrorInfo("rate_limit", "触发模型限流或配额限制，可稍后重试。", deterministic=False)
    return ModelErrorInfo("unknown", "", deterministic=False)


def record_model_error(
    settings: AnalyzerConfig,
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
    error_info = classify_model_error(error)
    if error_info.category != "unknown":
        metadata["error_category"] = error_info.category
        metadata["error_hint"] = error_info.hint
        metadata["error_deterministic"] = error_info.deterministic
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
        _logger.warning("保存模型错误日志失败: %s", exc)
