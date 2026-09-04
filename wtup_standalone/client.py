from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any, Callable

from .config import AnalyzerConfig
from .errors import record_model_error
from .normalize import safe_normalize_analysis
from .responses import extract_response_text, extract_token_usage
from .termination import TaskTerminatedError, check_task_termination
from .tokens import estimate_input_tokens

_logger = logging.getLogger("wtup_standalone.client")


class StandaloneLLMResponse:
    """标准模型响应封装。"""
    def __init__(self, text: str, usage: dict[str, int] | None = None, raw: Any = None):
        self.completion_text = text
        self.text = text
        self.usage = usage or {}
        self.raw_completion = raw

    def __repr__(self) -> str:
        return f"StandaloneLLMResponse(chars={len(self.completion_text)}, usage={self.usage})"


async def generate_analysis_from_prompt(context: Any, settings: AnalyzerConfig, prompt: str) -> dict[str, Any]:
    response = await request_llm(context, settings, prompt, purpose="单次提示词分析")
    text = extract_response_text(response)
    return safe_normalize_analysis(text)


async def request_llm(
    context: Any,
    settings: AnalyzerConfig,
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
            _logger.warning(
                "Provider %s 请求失败，%s: %s",
                provider_label(requested_provider_id),
                "尝试备用模型" if has_next else "没有可用备用模型",
                exc,
            )

    if last_error is not None:
        raise RuntimeError("所有已配置模型请求失败") from last_error

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
    settings: AnalyzerConfig,
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
    normalized_provider_id = str(provider_id or settings.model or "").strip()
    est_tokens = estimate_input_tokens(prompt)
    prompt_chars = len(prompt)
    p_label = provider_label(normalized_provider_id)
    seq_info = f" ({provider_index}/{provider_total})" if provider_total > 1 else ""

    _logger.info(
        "发起模型请求: 模型=%s%s | 用途=%s | 预估输入token=%d (字符数=%d) | 流式=%s",
        p_label,
        seq_info,
        purpose,
        est_tokens,
        prompt_chars,
        "是" if settings.enable_streaming_llm_call else "否",
    )

    request_no = _record_task_log(
        settings,
        "模型请求开始",
        {
            "用途": purpose,
            "Provider": p_label,
            "Provider序号": f"{provider_index}/{provider_total}",
            "允许备用模型": "是" if allow_fallback else "否",
            "流式请求": "是" if settings.enable_streaming_llm_call else "否",
            "输入token": est_tokens,
            "输入字符数": prompt_chars,
            "分片": _chunk_label(chunk),
            "提交范围": _summary_label(summary),
        },
    )
    started_at = time.monotonic()

    try:
        response = await _request_llm_once(context, settings, prompt, provider_id=normalized_provider_id)
        check_task_termination(settings, f"{purpose} Provider 请求后")
        usage = extract_token_usage(response)
        response_text = extract_response_text(response)
        elapsed = time.monotonic() - started_at

        _logger.info(
            "模型请求完成: 模型=%s | 用途=%s | 耗时=%.2fs | 响应字符数=%d | Token用量: 输入=%s, 输出=%s, 总计=%s",
            p_label,
            purpose,
            elapsed,
            len(response_text),
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
        )

        _record_task_log(
            settings,
            "模型请求完成",
            {
                "第几次模型请求": request_no,
                "用途": purpose,
                "Provider": p_label,
                "耗时秒": f"{elapsed:.2f}",
                "响应字符数": len(response_text),
                "返回总token": usage.total_tokens,
                "返回输入token": usage.prompt_tokens,
                "返回输出token": usage.completion_tokens,
            },
        )
        return response
    except TaskTerminatedError:
        elapsed = time.monotonic() - started_at
        _logger.warning("模型请求已终止: 模型=%s | 用途=%s | 耗时=%.2fs", p_label, purpose, elapsed)
        raise
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        _logger.warning(
            "模型请求失败: 模型=%s | 用途=%s | 耗时=%.2fs | 错误=%s",
            p_label,
            purpose,
            elapsed,
            exc,
        )
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
                "Provider": p_label,
                "耗时秒": f"{elapsed:.2f}",
                "错误": str(exc),
            },
        )
        raise


async def _request_llm_once(
    context: Any,
    settings: AnalyzerConfig,
    prompt: str,
    *,
    provider_id: str = "",
) -> Any:
    # 1. 如果 context 是自定义的可调用函数 (如 mock 或外部包装器)
    if callable(context):
        if asyncio.iscoroutinefunction(context):
            return await asyncio.wait_for(context(prompt, model=provider_id, settings=settings), timeout=settings.timeout_seconds)
        return await asyncio.wait_for(asyncio.to_thread(context, prompt, model=provider_id, settings=settings), timeout=settings.timeout_seconds)

    # 2. 如果 context 具有 AstrBot 原生接口 (llm_generate)
    if hasattr(context, "llm_generate"):
        llm_kwargs: dict[str, Any] = {"prompt": prompt}
        if provider_id:
            llm_kwargs["chat_provider_id"] = provider_id
        return await asyncio.wait_for(context.llm_generate(**llm_kwargs), timeout=settings.timeout_seconds)

    # 3. 独立模式：直接通过 HTTP 调用 OpenAI 兼容接口
    return await _call_openai_compatible_api(settings, prompt, model=provider_id or settings.model)


async def _call_openai_compatible_api(settings: AnalyzerConfig, prompt: str, model: str) -> StandaloneLLMResponse:
    """使用标准库发起 OpenAI 兼容格式的 Chat Completion 请求。"""
    base_url = settings.base_url.rstrip("/")
    url = f"{base_url}/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "wtup_standalone/0.1.0",
    }
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": getattr(settings, "temperature", 0.2),
        "stream": settings.enable_streaming_llm_call,
    }

    thinking_mode = getattr(settings, "thinking_mode", "off")
    thinking_budget = int(getattr(settings, "thinking_budget_tokens", 0) or 0)
    if thinking_mode in {"low", "medium", "high"}:
        payload["reasoning_effort"] = thinking_mode
        budget_map = {"low": 1024, "medium": 4096, "high": 8192}
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget_map[thinking_mode]}
    elif thinking_mode == "custom" and thinking_budget > 0:
        payload["reasoning_effort"] = "medium"
        payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    req_data = json.dumps(payload).encode("utf-8")
    timeout = settings.timeout_seconds

    def _sync_post() -> StandaloneLLMResponse:
        req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if settings.enable_streaming_llm_call:
                    return _parse_streaming_sse_response(resp)
                else:
                    body = resp.read().decode("utf-8")
                    data = json.loads(body)
                    text = ""
                    choices = data.get("choices") or []
                    if choices:
                        msg = choices[0].get("message") or {}
                        text = msg.get("content") or ""
                    usage = data.get("usage") or {}
                    return StandaloneLLMResponse(text=text, usage=usage, raw=data)
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {err.code} from {url}: {err_body}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"Connection failed to {url}: {err.reason}") from err

    return await asyncio.wait_for(asyncio.to_thread(_sync_post), timeout=timeout + 5.0)


def _parse_streaming_sse_response(resp: Any) -> StandaloneLLMResponse:
    """解析 Server-Sent Events 流式返回并聚合成完整文本。"""
    content_parts: list[str] = []
    final_usage: dict[str, int] = {}
    last_chunk: dict[str, Any] = {}

    for line_bytes in resp:
        line = line_bytes.decode("utf-8").strip()
        if not line:
            continue
        if line.startswith("data: "):
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                last_chunk = data
                choices = data.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    text_delta = delta.get("content") or ""
                    if text_delta:
                        content_parts.append(text_delta)
                if data.get("usage"):
                    final_usage = data["usage"]
            except json.JSONDecodeError:
                continue

    full_text = "".join(content_parts)
    return StandaloneLLMResponse(text=full_text, usage=final_usage, raw=last_chunk)


def _record_provider_request_error(
    settings: AnalyzerConfig,
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


def request_provider_ids(settings: AnalyzerConfig, provider_id: str | None = None) -> list[str | None]:
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


def _record_task_log(settings: AnalyzerConfig, event: str, metadata: dict[str, Any]) -> int | None:
    recorder = getattr(settings, "task_log_recorder", None)
    if not callable(recorder):
        return None
    try:
        return recorder(event, metadata)
    except Exception as exc:
        _logger.warning("写入任务日志失败: %s", exc)
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
