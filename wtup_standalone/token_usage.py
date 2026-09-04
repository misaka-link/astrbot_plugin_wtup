from __future__ import annotations

from typing import Any


def token_usage_numbers(token_usage: Any | None) -> tuple[int, int, int]:
    if token_usage is None:
        return 0, 0, 0

    def read(name: str, fallback: str = "") -> Any:
        if isinstance(token_usage, dict):
            return token_usage.get(name, token_usage.get(fallback, 0))
        value = getattr(token_usage, name, None)
        if value is None and fallback:
            value = getattr(token_usage, fallback, 0)
        return value

    prompt_tokens = as_non_negative_int(read("prompt_tokens", "input_tokens"))
    completion_tokens = as_non_negative_int(read("completion_tokens", "output_tokens"))
    total_tokens = as_non_negative_int(read("total_tokens"))
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return total_tokens, prompt_tokens, completion_tokens


def as_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def format_token_usage_text(token_usage: Any | None) -> str:
    total_tokens, prompt_tokens, completion_tokens = token_usage_numbers(token_usage)
    return f"Token 消耗：总 {total_tokens} · 输入 {prompt_tokens} · 输出 {completion_tokens}"
