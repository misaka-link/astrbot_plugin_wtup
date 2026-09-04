from __future__ import annotations

import csv
import io
import re
from collections import Counter
from typing import Any



try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger("wtup_standalone")


# 多语言 localization CSV：表头第一列是 key（通常带 `|readonly|noverify`），
# 后面是若干语言列 + 注释/长度列。逐行为一条 key + 多语言译文。
DEFAULT_MIN_PATCH_CHARS = 20_000
DEFAULT_MAX_GROUPS = 12
DEFAULT_SAMPLE_PER_GROUP = 3
DEFAULT_SAMPLE_CHARS = 90

_ASCII = re.compile(r"[\x00-\x7f]")


def is_localization_csv(filename: str) -> bool:
    """判断是否为 localization 文本 CSV。"""
    return str(filename or "").strip().lower().endswith(".csv")


def should_subdivide_csv(file_info: dict[str, Any], *, min_patch_chars: int) -> bool:
    if not is_localization_csv(str(file_info.get("filename") or "")):
        return False
    patch = str(file_info.get("patch") or "").strip()
    return len(patch) >= max(0, int(min_patch_chars))


def subdivide_csv_file(
    file_info: dict[str, Any],
    *,
    max_groups: int = DEFAULT_MAX_GROUPS,
    sample_per_group: int = DEFAULT_SAMPLE_PER_GROUP,
    sample_chars: int = DEFAULT_SAMPLE_CHARS,
) -> dict[str, Any]:
    """把一条超大 localization CSV 的 patch 分成“行级子节点”结构，返回紧凑描述。

    返回 dict 含：
      - text: 替换进 prompt 的紧凑文本。
      - groups: [{name, count, sample_keys:[...]}], 供覆盖点名册生成 source_id。
      - row_count / removed_count / column_count: 统计信息。
      - bulk_count: 未逐条列出的行数。
    """
    patch = str(file_info.get("patch") or "")
    added_lines, removed_lines = _extract_patch_change_lines(patch)
    added_rows = _parse_csv_rows(added_lines)
    removed_rows = _parse_csv_rows(removed_lines)

    if added_rows and _is_header_row(added_rows[0]):
        header = added_rows[0]
        body = added_rows[1:]
    else:
        # 修改类文件通常不会把表头放进 diff，此时所有新增行都是数据行。
        header = []
        body = added_rows
    column_count = len(header) or (len(body[0]) if body else 0)

    groups = _group_rows(body, max_groups=max_groups, sample_per_group=sample_per_group, sample_chars=sample_chars)

    filename = str(file_info.get("filename") or "")
    status = str(file_info.get("status") or "modified")
    removed_count = len(removed_rows)
    group_meta = [
        {
            "name": group["name"],
            "count": group["count"],
            "sample_keys": [item["key"] for item in group["sample"]],
        }
        for group in groups
    ]

    text = _render_compact(
        filename,
        status,
        header,
        groups,
        row_count=len(body),
        removed_count=removed_count,
        column_count=column_count,
    )

    bulk_count = len(body) - sum(group["count"] for group in groups)
    return {
        "text": text,
        "groups": group_meta,
        "row_count": len(body),
        "removed_count": removed_count,
        "column_count": column_count,
        "bulk_count": max(0, bulk_count),
    }


def _extract_patch_change_lines(patch: str) -> tuple[list[str], list[str]]:
    added: list[str] = []
    removed: list[str] = []
    for line in str(patch or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def _is_header_row(row: list[str]) -> bool:
    first = str(row[0] if row else "").strip().lower()
    return bool(first) and (first.startswith("<") or "readonly" in first or "noverify" in first or first == "id")


def _parse_csv_rows(lines: list[str]) -> list[list[str]]:
    if not lines:
        return []
    try:
        return list(csv.reader(io.StringIO("\n".join(lines)), delimiter=";"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] 解析 localization CSV 行失败: %s", "wtup_standalone", exc)
        return []


def _group_rows(
    rows: list[list[str]],
    *,
    max_groups: int,
    sample_per_group: int,
    sample_chars: int,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    grouped: dict[str, list[list[str]]] = {}
    for row in rows:
        if not row:
            continue
        key = str(row[0] or "").strip()
        if not key:
            continue
        grouped.setdefault(_key_prefix(key), []).append(row)

    counts = Counter({name: len(rows) for name, rows in grouped.items()})
    # 保留 top (max_groups - 1) 个类别，其余并入“其他”，避免类别过多。
    top_names = [name for name, _count in counts.most_common(max(1, max_groups - 1))]
    other_name = "其他"
    ordered_groups: list[dict[str, Any]] = []
    other_rows: list[list[str]] = []

    for name in top_names:
        ordered_groups.append(
            _make_group(name, grouped[name], sample_per_group=sample_per_group, sample_chars=sample_chars)
        )
    for name, group_rows in grouped.items():
        if name not in top_names:
            other_rows.extend(group_rows)
    if other_rows:
        ordered_groups.append(
            _make_group(other_name, other_rows, sample_per_group=sample_per_group, sample_chars=sample_chars)
        )
    return ordered_groups


def _make_group(name: str, rows: list[list[str]], *, sample_per_group: int, sample_chars: int) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row[0] or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        en = _column_value(row, 1)
        zh = _column_value(row, 10)
        samples.append({"key": key, "en": _clip(en, sample_chars), "zh": _clip(zh, sample_chars)})
        if len(samples) >= sample_per_group:
            break
    return {"name": name, "count": len(rows), "sample": samples}


def _key_prefix(key: str) -> str:
    text = str(key or "").strip()
    segment = text.split("/", 1)[0]
    return segment or text


def _column_value(row: list[str], index: int) -> str:
    if index < len(row):
        return str(row[index] or "").strip()
    return ""


def _clip(value: str, limit: int) -> str:
    text = str(value or "").strip()
    # 拆包数据里中文常以字面 \t 分隔，去掉以提升可读性。
    text = text.replace("\t", "").replace("\\t", "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}…"


def subdivide_summary_csv_files(settings: Any, summary: Any) -> dict[str, Any]:
    """为 summary.files 中符合条件的大 volume localization CSV 生成结构化子节点。

    会就地替换 file_info["patch"] 为紧凑描述，并写入：
      - file_info["csv_subdivided"] = True
      - file_info["csv_groups"] = [{"name","count","sample_keys"}] 供覆盖点名册生成 source_id。
      - file_info["csv_row_count"] / csv_bulk_count 等统计。
    返回统计 dict（给任务日志用）。
    """
    if not getattr(settings, "enable_csv_subdivide", True):
        return {"启用": "否", "subdivided": 0}
    min_patch_chars = int(getattr(settings, "csv_subdivide_min_chars", DEFAULT_MIN_PATCH_CHARS) or 0)
    max_groups = int(getattr(settings, "csv_subdivide_max_groups", DEFAULT_MAX_GROUPS) or DEFAULT_MAX_GROUPS)
    sample_per_group = int(
        getattr(settings, "csv_subdivide_sample_per_group", DEFAULT_SAMPLE_PER_GROUP) or DEFAULT_SAMPLE_PER_GROUP
    )

    files = list(getattr(summary, "files", None) or [])
    subdivided = 0
    total_rows = 0
    skipped_unchanged = 0
    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        if not should_subdivide_csv(file_info, min_patch_chars=min_patch_chars):
            continue
        result = subdivide_csv_file(
            file_info,
            max_groups=max_groups,
            sample_per_group=sample_per_group,
        )
        file_info["patch"] = result["text"]
        file_info["csv_subdivided"] = True
        file_info["csv_groups"] = result["groups"]
        file_info["csv_row_count"] = result["row_count"]
        file_info["csv_removed_count"] = result["removed_count"]
        file_info["csv_column_count"] = result["column_count"]
        file_info["csv_bulk_count"] = result["bulk_count"]
        subdivided += 1
        total_rows += result["row_count"]
    return {
        "启用": "是",
        "subdivided": subdivided,
        "总行数": total_rows,
        "最大分组数": max_groups,
        "每类样例数": sample_per_group,
    }


def _render_compact(
    filename: str,
    status: str,
    header: list[str],
    groups: list[dict[str, Any]],
    *,
    row_count: int,
    removed_count: int,
    column_count: int,
) -> str:
    lines: list[str] = []
    lines.append(f"# {filename}")
    lines.append(
        f"# 多语言 localization CSV（{column_count} 列: key + 语言列 + 注释/长度列）。"
        f"状态: {status}；新增 {row_count} 行，删除 {removed_count} 行。"
    )
    lines.append("# 程序已按 key 前缀归类，给出每类的代表 key；其余大量重复多语言行构成批量内容，"
                 "请归入 bulk_repeat_content 的 batch/repeated，不要逐条展开。")
    for group in groups:
        name = group["name"]
        count = group["count"]
        lines.append(f"## {name}（{count} 行）")
        for item in group["sample"]:
            lines.append(f"- {item['key'][:80]}")
            if item["en"]:
                lines.append(f"    EN: {item['en']}")
            if item["zh"]:
                lines.append(f"    ZH: {item['zh']}")
        if count > len(group["sample"]):
            lines.append(f"  …（该类别其余 {count - len(group['sample'])} 条 key 未逐条列出）")
    return "\n".join(lines)