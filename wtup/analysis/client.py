from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from ..config import PLUGIN_NAME, PluginConfig
from ..termination import TaskTerminatedError, check_task_termination
from .errors import record_model_error
from .normalize import safe_normalize_analysis
from .responses import extract_response_text, extract_token_usage
from .tokens import estimate_input_tokens


async def generate_analysis_from_prompt(context: Any, settings: PluginConfig, prompt: str) -> dict[str, Any]:
    response = await request_llm(context, settings, prompt, purpose="单次提示词分析")
    text = extract_response_text(response)
    return safe_normalize_analysis(text)


async def request_llm(
    context: Any,
    settings: PluginConfig,
    prompt: str,
    *,
    provider_id: str | None = None,
    allow_fallback: bool = True,
    summary: Any | None = None,
    chunk: Any | None = None,
    purpose: str = "模型请求",
) -> Any:
    check_task_termination(settings, f"{purpose} 准备请求前")
    if not allow_fallback:
        return await _request_llm_with_provider(
            context,
            settings,
            prompt,
            provider_id=provider_id,
            provider_index=1,
            provider_total=1,
            allow_fallback=False,
            summary=summary,
            chunk=chunk,
            purpose=purpose,
        )

    provider_ids = request_provider_ids(settings, provider_id)
    last_error: BaseException | None = None

    for index, requested_provider_id in enumerate(provider_ids):
        try:
            return await _request_llm_with_provider(
                context,
                settings,
                prompt,
                provider_id=requested_provider_id,
                provider_index=index + 1,
                provider_total=len(provider_ids),
                allow_fallback=True,
                summary=summary,
                chunk=chunk,
                purpose=purpose,
            )
        except TaskTerminatedError:
            raise
        except Exception as exc:
            last_error = exc
            has_next = index + 1 < len(provider_ids)
            logger.warning(
                "[%s] Provider %s 请求失败，%s: %s",
                PLUGIN_NAME,
                provider_label(requested_provider_id),
                "尝试备用模型" if has_next else "没有可用备用模型",
                exc,
            )

    if last_error is not None:
        raise RuntimeError("所有已配置模型请求失败") from last_error

    # request_provider_ids always returns at least the default model.
    return await _request_llm_with_provider(
        context,
        settings,
        prompt,
        provider_id=None,
        provider_index=1,
        provider_total=1,
        allow_fallback=False,
        summary=summary,
        chunk=chunk,
        purpose=purpose,
    )


async def _request_llm_with_provider(
    context: Any,
    settings: PluginConfig,
    prompt: str,
    *,
    provider_id: str | None,
    provider_index: int,
    provider_total: int,
    allow_fallback: bool,
    summary: Any | None,
    chunk: Any | None,
    purpose: str,
) -> Any:
    check_task_termination(settings, f"{purpose} Provider 请求前")
    normalized_provider_id = str(provider_id or "").strip()
    request_no = _record_task_log(
        settings,
        "模型请求开始",
        {
            "用途": purpose,
            "Provider": provider_label(normalized_provider_id),
            "Provider序号": f"{provider_index}/{provider_total}",
            "允许备用模型": "是" if allow_fallback else "否",
            "流式请求": "是" if settings.enable_streaming_llm_call else "否",
            "输入token": estimate_input_tokens(prompt),
            "分片": _chunk_label(chunk),
            "提交范围": _summary_label(summary),
        },
    )
    started_at = time.monotonic()
    if normalized_provider_id:
        try:
            provider = context.get_provider_by_id(provider_id=normalized_provider_id)
        except Exception as exc:
            wrapped = RuntimeError(f"获取 Provider {normalized_provider_id} 失败: {exc}")
            _record_provider_request_error(
                settings,
                wrapped,
                prompt=prompt,
                provider_id=normalized_provider_id,
                provider_index=provider_index,
                provider_total=provider_total,
                allow_fallback=allow_fallback,
                summary=summary,
                chunk=chunk,
                purpose=purpose,
            )
            _record_task_log(
                settings,
                "模型请求失败",
                {
                    "第几次模型请求": request_no,
                    "用途": purpose,
                    "Provider": provider_label(normalized_provider_id),
                    "耗时秒": f"{time.monotonic() - started_at:.2f}",
                    "错误": str(wrapped),
                },
            )
            raise wrapped from exc
        if not provider:
            exc = RuntimeError(f"Provider {normalized_provider_id} 不存在")
            _record_provider_request_error(
                settings,
                exc,
                prompt=prompt,
                provider_id=normalized_provider_id,
                provider_index=provider_index,
                provider_total=provider_total,
                allow_fallback=allow_fallback,
                summary=summary,
                chunk=chunk,
                purpose=purpose,
            )
            _record_task_log(
                settings,
                "模型请求失败",
                {
                    "第几次模型请求": request_no,
                    "用途": purpose,
                    "Provider": provider_label(normalized_provider_id),
                    "耗时秒": f"{time.monotonic() - started_at:.2f}",
                    "错误": str(exc),
                },
            )
            raise exc

    try:
        response = await _request_llm_once(context, settings, prompt, provider_id=normalized_provider_id)
        check_task_termination(settings, f"{purpose} Provider 请求后")
        usage = extract_token_usage(response)
        response_text = extract_response_text(response)
        _record_task_log(
            settings,
            "模型请求完成",
            {
                "第几次模型请求": request_no,
                "用途": purpose,
                "Provider": provider_label(normalized_provider_id),
                "耗时秒": f"{time.monotonic() - started_at:.2f}",
                "响应字符数": len(response_text),
                "返回总token": usage.total_tokens,
                "返回输入token": usage.prompt_tokens,
                "返回输出token": usage.completion_tokens,
            },
        )
        return response
    except TaskTerminatedError:
        raise
    except Exception as exc:
        _record_provider_request_error(
            settings,
            exc,
            prompt=prompt,
            provider_id=normalized_provider_id,
            provider_index=provider_index,
            provider_total=provider_total,
            allow_fallback=allow_fallback,
            summary=summary,
            chunk=chunk,
            purpose=purpose,
        )
        _record_task_log(
            settings,
            "模型请求失败",
            {
                "第几次模型请求": request_no,
                "用途": purpose,
                "Provider": provider_label(normalized_provider_id),
                "耗时秒": f"{time.monotonic() - started_at:.2f}",
                "错误": str(exc),
            },
        )
        raise


def _record_provider_request_error(
    settings: PluginConfig,
    error: BaseException,
    *,
    prompt: str,
    provider_id: str,
    provider_index: int,
    provider_total: int,
    allow_fallback: bool,
    summary: Any | None,
    chunk: Any | None,
    purpose: str,
) -> None:
    record_model_error(
        settings,
        "provider_request_failed",
        error,
        summary=summary,
        chunk=chunk,
        extra={
            "provider_id": provider_id or "默认模型",
            "provider_index": provider_index,
            "provider_total": provider_total,
            "allow_fallback": allow_fallback,
            "enable_streaming_llm_call": settings.enable_streaming_llm_call,
            "prompt_chars": len(prompt),
            "purpose": purpose,
        },
    )


def request_provider_ids(settings: PluginConfig, provider_id: str | None = None) -> list[str | None]:
    primary_provider_id = settings.provider_id if provider_id is None else str(provider_id or "").strip()
    provider_ids: list[str | None] = [primary_provider_id or None]
    seen = {provider_ids[0] or ""}
    for backup_provider_id in settings.backup_provider_ids:
        normalized = str(backup_provider_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        provider_ids.append(normalized)
    return provider_ids


def provider_label(provider_id: str | None) -> str:
    return str(provider_id or "").strip() or "默认模型"


def _record_task_log(settings: PluginConfig, event: str, metadata: dict[str, Any]) -> int | None:
    recorder = getattr(settings, "task_log_recorder", None)
    if not callable(recorder):
        return None
    try:
        return recorder(event, metadata)
    except Exception as exc:
        logger.warning("[%s] 写入任务日志失败: %s", PLUGIN_NAME, exc)
        return None


def _summary_label(summary: Any | None) -> str:
    if summary is None:
        return ""
    base_sha = str(getattr(summary, "base_sha", "") or "")
    head_sha = str(getattr(summary, "head_sha", "") or "")
    if base_sha or head_sha:
        return f"{base_sha[:7] or 'unknown'}...{head_sha[:7] or 'unknown'}"
    return ""


def _chunk_label(chunk: Any | None) -> str:
    if chunk is None:
        return ""
    index = getattr(chunk, "index", None)
    total = getattr(chunk, "total", None)
    files = getattr(chunk, "files", None)
    file_count = len(files) if isinstance(files, list) else 0
    if index is None or total is None:
        return f"{file_count} 个文件"
    return f"{index}/{total}，{file_count} 个文件"


async def _request_llm_once(
    context: Any,
    settings: PluginConfig,
    prompt: str,
    *,
    provider_id: str = "",
) -> Any:
    llm_kwargs: dict[str, Any] = {"prompt": prompt}
    if provider_id:
        llm_kwargs["chat_provider_id"] = provider_id
    if settings.enable_streaming_llm_call:
        provider = _get_stream_provider(context, provider_id)
        if provider is not None:
            logger.info("[%s] 使用流式 Provider 调用%s", PLUGIN_NAME, f": {provider_id}" if provider_id else "")
            return await asyncio.wait_for(_call_provider_stream(provider, llm_kwargs), timeout=settings.timeout_seconds)
        if provider_id:
            raise RuntimeError(f"Provider {provider_id} 不支持流式调用")
        logger.warning("[%s] 未找到可用的流式 Provider，已回退为非流式模型请求", PLUGIN_NAME)
    return await asyncio.wait_for(context.llm_generate(**llm_kwargs), timeout=settings.timeout_seconds)


def _get_stream_provider(context: Any, provider_id: str) -> Any | None:
    provider = None
    if provider_id:
        try:
            provider = context.get_provider_by_id(provider_id=provider_id)
        except Exception as exc:
            logger.warning("[%s] 获取流式 Provider %s 失败: %s", PLUGIN_NAME, provider_id, exc)
            return None
    if provider is None and not provider_id:
        get_all_providers = getattr(context, "get_all_providers", None)
        if callable(get_all_providers):
            try:
                providers = get_all_providers()
            except Exception as exc:
                logger.warning("[%s] 获取可用 Provider 列表失败: %s", PLUGIN_NAME, exc)
                providers = []
            if providers:
                provider = providers[0]
    if provider is not None and callable(getattr(provider, "text_chat_stream", None)):
        return provider
    return None


async def _call_provider_stream(provider: Any, llm_kwargs: dict[str, Any]) -> Any:
    stream_kwargs = dict(llm_kwargs)
    stream_kwargs.pop("chat_provider_id", None)

    final_response = None
    content_parts: list[str] = []
    async for response in provider.text_chat_stream(**stream_kwargs):
        final_response = response
        text = extract_response_text(response)
        if getattr(response, "is_chunk", False) and text:
            content_parts.append(text)

    if final_response is None:
        raise RuntimeError("流式 LLM 调用未返回任何响应")

    final_text = extract_response_text(final_response)
    if final_text and not getattr(final_response, "is_chunk", False):
        return final_response

    return SimpleNamespace(
        role="assistant",
        completion_text="".join(content_parts),
        usage=getattr(final_response, "usage", None),
        raw_completion=getattr(final_response, "raw_completion", None),
    )
