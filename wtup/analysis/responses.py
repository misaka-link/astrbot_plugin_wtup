from __future__ import annotations

from typing import Any

from .models import TokenUsage


def extract_response_text(response: Any) -> str:
    if response is None:
        return ""
    if hasattr(response, "completion_text"):
        return str(response.completion_text or "").strip()
    if hasattr(response, "text"):
        return str(response.text or "").strip()
    choices = getattr(response, "choices", None)
    if choices:
        first_choice = choices[0]
        message = getattr(first_choice, "message", None)
        content = getattr(message, "content", None)
        if content is not None:
            return str(content or "").strip()
    if isinstance(response, str):
        return response.strip()
    return str(response).strip()

def ensure_usable_llm_response(response: Any) -> None:
    reason = llm_failure_reason(response)
    if reason:
        raise RuntimeError(reason)

def llm_failure_reason(response: Any) -> str:
    if response is None:
        return "模型无可用输出"

    choices = getattr(response, "choices", None)
    if choices:
        first_choice = choices[0]
        finish_reason = str(getattr(first_choice, "finish_reason", "") or "").strip()
        text = extract_response_text(response)
        if text:
            return ""
        if finish_reason and finish_reason != "stop":
            return f"模型无可用输出: {finish_reason}"
        return "模型无可用输出"

    if extract_response_text(response):
        return ""
    return "模型无可用输出"

def extract_token_usage(response: Any) -> TokenUsage:
    usage = _extract_usage_payload(response)
    if not usage:
        return TokenUsage()

    if isinstance(usage, dict):
        prompt_tokens = _first_int(usage, "prompt_tokens", "input_tokens", "input")
        completion_tokens = _first_int(usage, "completion_tokens", "output_tokens", "output")
        total_tokens = _first_int(usage, "total_tokens", "total")
        return _normalize_token_usage(prompt_tokens, completion_tokens, total_tokens)

    if hasattr(usage, "input") or hasattr(usage, "output"):
        return _normalize_token_usage(
            _safe_int(getattr(usage, "input", 0)),
            _safe_int(getattr(usage, "output", 0)),
            _safe_int(getattr(usage, "total", 0)),
        )

    return _normalize_token_usage(
        _safe_int(getattr(usage, "prompt_tokens", 0)),
        _safe_int(getattr(usage, "completion_tokens", 0)),
        _safe_int(getattr(usage, "total_tokens", 0)),
    )

def _extract_usage_payload(response: Any) -> Any:
    if response is None:
        return None

    usage = getattr(response, "usage", None)
    if usage:
        return usage

    raw_completion = getattr(response, "raw_completion", None)
    if raw_completion is not None:
        usage = getattr(raw_completion, "usage", None)
        if usage:
            return usage
        if isinstance(raw_completion, dict):
            usage = raw_completion.get("usage")
            if usage:
                return usage

    if isinstance(response, dict):
        return response.get("usage")
    return None

def _first_int(data: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in data:
            return _safe_int(data.get(key))
    return 0

def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

def _normalize_token_usage(prompt_tokens: int, completion_tokens: int, total_tokens: int) -> TokenUsage:
    if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
        total_tokens = prompt_tokens + completion_tokens
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
