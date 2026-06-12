from __future__ import annotations

from typing import Any


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
