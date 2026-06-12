from __future__ import annotations

import re
from datetime import datetime


VERSION_TITLE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*->\s*(\d+(?:\.\d+)*)\s*$")
INVALID_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def build_report_log_filename(report_title: str, *, now: datetime | None = None) -> str:
    title = str(report_title or "").strip()
    match = VERSION_TITLE_RE.match(title)
    if match:
        return f"{match.group(1)}_{match.group(2)}.log"

    current = now or datetime.now()
    return (
        f"{current.year}年{current.month}月{current.day}日"
        f"{current.hour:02d}：{current.minute:02d}：{current.second:02d}.log"
    )


def add_report_log_suffix(filename: str, suffix: str) -> str:
    clean_suffix = sanitize_filename(suffix, fallback="").strip(" ._")
    if not clean_suffix:
        return filename

    path = str(filename or "").strip()
    if path.lower().endswith(".log"):
        return f"{path[:-4]}_{clean_suffix}.log"
    return f"{path}_{clean_suffix}.log"


def sanitize_filename(filename: str, *, fallback: str = "report.log") -> str:
    normalized = INVALID_FILENAME_CHARS_RE.sub("_", str(filename or "")).strip(" ._")
    return normalized or fallback
