from __future__ import annotations

import difflib
import json
from dataclasses import dataclass


DIFF_KIND_SEMANTIC = "semantic"
DIFF_KIND_TEXT = "text"
DIFF_KIND_EMPTY = "empty"
DIFF_KIND_ERROR = "error"

DEFAULT_MAX_CHARS = 6000
DEFAULT_VALUE_LIMIT = 120
DEFAULT_MAX_CHANGES = 400
DEFAULT_TEXT_CONTEXT_LINES = 6

TRUNCATED_SUFFIX = "\n(对比内容过长已截断)"


@dataclass(frozen=True)
class StructCompareResult:
    kind: str
    text: str
    change_count: int
    truncated: bool = False
    detail: str = ""


def build_struct_compare(
    old_text: str,
    new_text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    value_limit: int = DEFAULT_VALUE_LIMIT,
    max_changes: int = DEFAULT_MAX_CHANGES,
) -> StructCompareResult:
    """对同一文件的新旧文本做结构化对比。

    优先尝试 JSON 字段级语义对比；任一侧不是合法 JSON 时退回统一文本 diff。
    返回的 text 不含文件名等头部信息，由调用方包装。
    """
    old_text = str(old_text or "")
    new_text = str(new_text or "")
    if old_text == new_text:
        return StructCompareResult(kind=DIFF_KIND_EMPTY, text="(新旧版本内容一致)", change_count=0)

    semantic = _try_semantic_compare(old_text, new_text, max_chars=max_chars, value_limit=value_limit, max_changes=max_changes)
    if semantic is not None:
        return semantic
    return _text_compare(old_text, new_text, max_chars=max_chars)


def _try_semantic_compare(
    old_text: str,
    new_text: str,
    *,
    max_chars: int,
    value_limit: int,
    max_changes: int,
) -> StructCompareResult | None:
    try:
        old_obj = json.loads(old_text)
        new_obj = json.loads(new_text)
    except (ValueError, TypeError):
        return None
    try:
        changes = collect_json_changes(old_obj, new_obj, value_limit=value_limit, max_changes=max_changes)
    except RecursionError:
        return None
    lines: list[str] = []
    truncated = False
    used = 0
    for line in render_change_lines(changes, value_limit=value_limit):
        extra = len(line) + (1 if lines else 0)
        if max_chars > 0 and used + extra > max_chars:
            truncated = True
            break
        lines.append(line)
        used += extra
    text = "\n".join(lines) if lines else "(未检测到字段级差异，但整体内容不同)"
    if truncated:
        shown = len(lines)
        total = len(changes)
        text += f"\n(字段级变化共 {total} 处，已按字符上限截断，仅显示前 {shown} 处)"
    kind = DIFF_KIND_SEMANTIC if changes else DIFF_KIND_EMPTY
    detail = "" if changes else "两侧均可解析为 JSON 但无字段级差异（可能是格式化差异）"
    return StructCompareResult(kind=kind, text=text, change_count=len(changes), truncated=truncated, detail=detail)


def _text_compare(old_text: str, new_text: str, *, max_chars: int) -> StructCompareResult:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="old",
            tofile="new",
            n=DEFAULT_TEXT_CONTEXT_LINES,
            lineterm="",
        )
    )
    body = [line for line in diff_lines if not line.startswith(("--- ", "+++ ")) and line not in {"--- old", "+++ new"}]
    lines: list[str] = ["(非 JSON 文件，以下为程序生成的统一文本对比)"]
    used = len(lines[0])
    truncated = False
    for line in body:
        extra = len(line) + 1
        if max_chars > 0 and used + extra > max_chars:
            truncated = True
            break
        lines.append(line)
        used += extra
    if truncated:
        lines.append(f"(文本对比已按字符上限截断，原始差异 {len(body)} 行)")
    return StructCompareResult(kind=DIFF_KIND_TEXT, text="\n".join(lines), change_count=len(body), truncated=truncated)


def collect_json_changes(
    old_obj: object,
    new_obj: object,
    *,
    value_limit: int = DEFAULT_VALUE_LIMIT,
    max_changes: int = DEFAULT_MAX_CHANGES,
) -> list[tuple[str, str, str, str]]:
    """递归收集两个 JSON 值之间的字段级变化。

    返回元组列表： (op, path, old_display, new_display)
    op ∈ {"changed", "added", "removed", "length"}。
    """
    changes: list[tuple[str, str, str, str]] = []
    _walk(old_obj, new_obj, "$", changes, value_limit=value_limit, max_changes=max_changes)
    return changes


def _walk(
    old_obj: object,
    new_obj: object,
    path: str,
    changes: list[tuple[str, str, str, str]],
    *,
    value_limit: int,
    max_changes: int,
) -> None:
    if len(changes) >= max_changes:
        return
    if isinstance(old_obj, dict) and isinstance(new_obj, dict):
        _walk_dict(old_obj, new_obj, path, changes, value_limit=value_limit, max_changes=max_changes)
        return
    if isinstance(old_obj, list) and isinstance(new_obj, list):
        _walk_list(old_obj, new_obj, path, changes, value_limit=value_limit, max_changes=max_changes)
        return
    if old_obj != new_obj and _display(old_obj, value_limit) != _display(new_obj, value_limit):
        changes.append(("changed", path, _display(old_obj, value_limit), _display(new_obj, value_limit)))


def _walk_dict(
    old_obj: dict,
    new_obj: dict,
    path: str,
    changes: list[tuple[str, str, str, str]],
    *,
    value_limit: int,
    max_changes: int,
) -> None:
    for key in new_obj:
        if len(changes) >= max_changes:
            return
        child_path = f"{path}{_key_suffix(key)}"
        if key not in old_obj:
            changes.append(("added", child_path, "", _display(new_obj[key], value_limit)))
        else:
            _walk(old_obj[key], new_obj[key], child_path, changes, value_limit=value_limit, max_changes=max_changes)
    for key in old_obj:
        if len(changes) >= max_changes:
            return
        if key in new_obj:
            continue
        child_path = f"{path}{_key_suffix(key)}"
        changes.append(("removed", child_path, _display(old_obj[key], value_limit), ""))


def _walk_list(
    old_obj: list,
    new_obj: list,
    path: str,
    changes: list[tuple[str, str, str, str]],
    *,
    value_limit: int,
    max_changes: int,
) -> None:
    if len(old_obj) != len(new_obj):
        changes.append(("length", path, str(len(old_obj)), str(len(new_obj))))
    common = min(len(old_obj), len(new_obj))
    for index in range(common):
        if len(changes) >= max_changes:
            return
        _walk(old_obj[index], new_obj[index], f"{path}[{index}]", changes, value_limit=value_limit, max_changes=max_changes)
    for index in range(common, len(new_obj)):
        if len(changes) >= max_changes:
            return
        changes.append(("added", f"{path}[{index}]", "", _display(new_obj[index], value_limit)))
    for index in range(common, len(old_obj)):
        if len(changes) >= max_changes:
            return
        changes.append(("removed", f"{path}[{index}]", _display(old_obj[index], value_limit), ""))


def render_change_lines(
    changes: list[tuple[str, str, str, str]],
    *,
    value_limit: int = DEFAULT_VALUE_LIMIT,
) -> list[str]:
    lines: list[str] = []
    for op, path, old_value, new_value in changes:
        if op == "changed":
            lines.append(f"[修改] {path}: {old_value} -> {new_value}")
        elif op == "added":
            lines.append(f"[新增] {path} = {_clip(new_value, value_limit)}")
        elif op == "removed":
            lines.append(f"[删除] {path} (旧值: {_clip(old_value, value_limit)})")
        elif op == "length":
            lines.append(f"[数量] {path}: {old_value} 项 -> {new_value} 项")
    return lines


def _key_suffix(key: object) -> str:
    text = str(key)
    if text and re_fullmatch_ident(text):
        return f".{text}"
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'["{escaped}"]'


def re_fullmatch_ident(text: str) -> bool:
    return bool(text) and all(character.isalnum() or character == "_" for character in text)


def _display(value: object, value_limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=False)
    except (TypeError, ValueError):
        text = repr(value)
    return _clip(text, value_limit)


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}…(截断)"