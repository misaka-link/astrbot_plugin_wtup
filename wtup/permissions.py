from __future__ import annotations

import re
from typing import Any


def normalize_user_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def admin_target_allows_sender(admin_targets: list[str], *, sender_id: Any = "", event_origin: Any = "") -> bool:
    sender = normalize_user_id(sender_id)
    origin = str(event_origin or "").strip()
    if not sender and not origin:
        return False

    for target in admin_targets:
        normalized_target = str(target or "").strip()
        if not normalized_target:
            continue
        if origin and normalized_target == origin and not _is_group_origin(normalized_target):
            return True
        if sender and normalized_target == sender:
            return True
        if sender and _private_origin_user_id(normalized_target) == sender:
            return True
    return False


def _private_origin_user_id(value: str) -> str:
    lower_value = value.lower()
    if "group" in lower_value or ("private" not in lower_value and "friend" not in lower_value):
        return ""
    match = re.search(r"(\d+)(?!.*\d)", value)
    return match.group(1) if match else ""


def _is_group_origin(value: str) -> bool:
    return "group" in value.lower()
