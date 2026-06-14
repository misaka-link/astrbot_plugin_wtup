from __future__ import annotations

import base64
import html
import os
import re
import time
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from .config import BRANCH_NAME, PLUGIN_NAME, REPO_FULL_NAME
from .analysis.normalize import normalize_analysis_coverage
from .diff_collector import DiffChunk, DiffSummary, short_sha
from .token_usage import format_token_usage_text


MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")

RENDER_OPTIONS = {
    "width": 1280,
    "height": 920,
    "full_page": True,
    "type": "png",
}


def build_report_html(
    template_path: Path,
    summary: DiffSummary,
    chunk: DiffChunk,
    analysis: dict[str, Any],
    *,
    footer_note: str = "",
    report_label: str = "",
    token_usage: Any | None = None,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    style_css = load_template_css(template_path)
    importance = str(analysis.get("importance") or "中")
    report_title = report_display_title(summary, chunk, analysis)
    update_subtitle = report_update_subtitle(chunk, analysis)

    kicker_parts = ["GitHub Commit Monitor"]
    if report_label:
        kicker_parts.append(report_label)
    kicker_parts.append(f"重要度 {importance}")
    replacements = {
        "{{ style_css }}": style_css,
        "{{ hero_image_src }}": "",
        "{{ report_kicker }}": " · ".join(kicker_parts),
        "{{ report_title }}": "War Thunder Datamine 更新",
        "{{ report_subtitle }}": html.escape(update_subtitle),
        "{{ repo_name }}": REPO_FULL_NAME,
        "{{ branch_name }}": BRANCH_NAME,
        "{{ commit_range }}": f"{short_sha(summary.base_sha)}...{short_sha(summary.head_sha)}",
        "{{ change_stats }}": render_change_stats(summary, chunk),
        "{{ summary_html }}": render_update_content(report_title, update_subtitle, analysis),
        "{{ badges_html }}": render_badges([f"重要度 {importance}", *list(analysis.get("tags") or [])]),
        "{{ article_html }}": render_ai_analysis(analysis),
        "{{ table_html }}": "",
        "{{ sections_html }}": "",
        "{{ footer_note }}": render_footer_note(footer_note),
        "{{ token_usage }}": html.escape(format_token_usage_text(token_usage)),
        "{{ footer_badge }}": html.escape(time.strftime("%Y-%m-%d %H:%M:%S")),
    }
    for needle, value in replacements.items():
        template = template.replace(needle, value)
    return template


def load_template_css(template_path: Path) -> str:
    css_path = template_path.with_suffix(".css")
    try:
        return css_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("[%s] 模板样式文件不存在: %s", PLUGIN_NAME, css_path)
        return ""


async def render_report_image(plugin: Any, html_text: str, output_dir: Path) -> Path | None:
    render_func = getattr(plugin, "html_render", None)
    if not callable(render_func):
        logger.warning("[%s] html_render 不可用，跳过图片渲染", PLUGIN_NAME)
        return None

    try:
        image_data = await render_func(html_text, {}, False, dict(RENDER_OPTIONS))
    except Exception as exc:
        logger.warning("[%s] 报告图片渲染失败: %s", PLUGIN_NAME, exc)
        return None

    image_bytes = image_data_to_bytes(image_data)
    if not is_valid_image_bytes(image_bytes):
        logger.warning("[%s] html_render 返回了无效图片", PLUGIN_NAME)
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"wtup_report_{time.time_ns()}.png"
    output_path.write_bytes(image_bytes)
    return output_path


def render_plain_text(
    summary: DiffSummary,
    chunk: DiffChunk,
    analysis: dict[str, Any],
    *,
    report_label: str = "",
) -> str:
    lines = []
    if report_label:
        lines.append(f"【{report_label}】")
    lines.append(report_display_title(summary, chunk, analysis))
    update_subtitle = report_update_subtitle(chunk, analysis)
    if update_subtitle:
        lines.append(update_subtitle)
    lines.append("")
    update_sections = normalized_update_sections_for_render(analysis)
    for section in update_sections:
        lines.append(f"{section['title']}:")
        lines.extend(render_plain_update_items(section["items"], indent=0))
        lines.append("")

    ai_analysis = analysis.get("ai_analysis") if isinstance(analysis.get("ai_analysis"), dict) else {}
    lines.append("AI 分析:")
    for title, values in (
        ("改动内容", ai_analysis.get("changed_content") or analysis.get("highlights") or []),
        ("可能影响", ai_analysis.get("player_impact") or analysis.get("player_impact") or []),
        ("风险/不确定", ai_analysis.get("uncertainties") or analysis.get("risks") or []),
    ):
        normalized = [str(item).strip() for item in values if str(item or "").strip()]
        if not normalized:
            continue
        lines.append(f"{title}:")
        lines.extend(f"- {item}" for item in normalized)
    recommendation = str(ai_analysis.get("recommendation") or analysis.get("recommendation") or "").strip()
    if recommendation:
        lines.append(f"建议: {recommendation}")
    coverage_lines = render_plain_analysis_coverage(analysis)
    if coverage_lines:
        lines.append("")
        lines.extend(coverage_lines)
    return "\n".join(lines)


def render_change_stats(summary: DiffSummary, chunk: DiffChunk) -> str:
    if len(chunk.files) >= summary.total_files:
        return f"{summary.total_files} 文件 · {chunk.patch_chars} 字符"
    return f"{len(chunk.files)}/{summary.total_files} 文件 · {chunk.patch_chars} 字符"


def report_display_title(summary: DiffSummary, chunk: DiffChunk, analysis: dict[str, Any]) -> str:
    title = str(analysis.get("report_title") or "").strip()
    if not title:
        title = f"{short_sha(summary.base_sha)}->{short_sha(summary.head_sha)}"
    return title


def report_update_subtitle(chunk: DiffChunk, analysis: dict[str, Any]) -> str:
    parts = []
    summary = str(analysis.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    return " · ".join(parts)


def render_update_content(title: str, subtitle: str, analysis: dict[str, Any]) -> str:
    sections = normalized_update_sections_for_render(analysis)
    parts = [
        '<div class="update-report">',
        f'<h2 class="update-title">{html.escape(title)}</h2>',
    ]
    if subtitle:
        parts.append(f'<div class="update-subtitle">{html.escape(subtitle)}</div>')
    for section in sections:
        parts.append('<section class="update-section">')
        parts.append(f'<h3>{html.escape(section["title"])}</h3>')
        parts.append(render_update_items(section["items"]))
        parts.append("</section>")
    parts.append("</div>")
    return "".join(parts)


def normalized_update_sections_for_render(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    sections = analysis.get("update_sections") if isinstance(analysis.get("update_sections"), list) else []
    normalized = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()
        items = section.get("items")
        if title and isinstance(items, list) and items:
            normalized.append({"title": title, "items": items})
    if normalized:
        return normalized

    highlights = [
        {"text": str(item).strip(), "children": []}
        for item in analysis.get("highlights", [])
        if str(item or "").strip()
    ]
    if highlights:
        return [{"title": "更新内容", "items": highlights}]
    return [{"title": "更新内容", "items": [{"text": "本次更新没有可展示的更新条目。", "children": []}]}]


def render_update_items(items: list[dict[str, Any]]) -> str:
    children = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        nested = item.get("children") if isinstance(item.get("children"), list) else []
        if not text:
            continue
        child_html = render_update_items(nested) if nested else ""
        children.append(f"<li><span>{html.escape(text)}</span>{child_html}</li>")
    return "<ul>" + "".join(children) + "</ul>" if children else ""


def render_plain_update_items(items: list[dict[str, Any]], *, indent: int) -> list[str]:
    lines = []
    prefix = "  " * indent + "- "
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{prefix}{text}")
        children = item.get("children") if isinstance(item.get("children"), list) else []
        lines.extend(render_plain_update_items(children, indent=indent + 1))
    return lines

def render_plain_analysis_coverage(analysis: dict[str, Any]) -> list[str]:
    coverage = normalize_analysis_coverage(analysis.get("analysis_coverage"))
    if not coverage:
        return []

    lines = ["分析覆盖清单:"]
    for item in coverage:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        status = render_coverage_status(item.get("status"))
        lines.append(f"- [{status}] {path}")
        changes = [str(value).strip() for value in item.get("covered_changes") or [] if str(value or "").strip()]
        evidence = [str(value).strip() for value in item.get("evidence") or [] if str(value or "").strip()]
        notes = str(item.get("notes") or "").strip()
        if changes:
            lines.append(f"  改动: {'；'.join(changes)}")
        if evidence:
            lines.append(f"  证据: {'；'.join(evidence)}")
        if notes:
            lines.append(f"  备注: {notes}")
    return lines if len(lines) > 1 else []

def render_coverage_status(value: Any) -> str:
    status = str(value or "").strip()
    return {
        "analyzed": "已分析",
        "uncertain": "待复核",
        "skipped": "未分析",
    }.get(status, "待复核")


def render_ai_analysis(analysis: dict[str, Any]) -> str:
    ai_analysis = analysis.get("ai_analysis") if isinstance(analysis.get("ai_analysis"), dict) else {}
    specs = [
        ("改动内容", ai_analysis.get("changed_content") or analysis.get("highlights") or []),
        ("可能影响", ai_analysis.get("player_impact") or analysis.get("player_impact") or []),
        ("风险与不确定", ai_analysis.get("uncertainties") or analysis.get("risks") or []),
    ]
    parts = ['<div class="ai-analysis"><h2>AI 分析</h2>']
    has_content = False
    for title, values in specs:
        normalized = [str(item).strip() for item in values if str(item or "").strip()]
        if not normalized:
            continue
        has_content = True
        list_items = "".join(f"<li>{html.escape(item)}</li>" for item in normalized)
        parts.append(f"<h3>{html.escape(title)}</h3><ul>{list_items}</ul>")
    recommendation = str(ai_analysis.get("recommendation") or analysis.get("recommendation") or "").strip()
    if recommendation:
        has_content = True
        parts.append(f"<h3>建议</h3><p>{html.escape(recommendation)}</p>")
    parts.append("</div>")
    return "".join(parts) if has_content else ""


def render_sections(analysis: dict[str, Any]) -> str:
    specs = [
        ("重点变化", analysis.get("highlights") or []),
        ("可能影响", analysis.get("player_impact") or []),
        ("风险与不确定", analysis.get("risks") or []),
    ]
    sections = []
    for title, items in specs:
        normalized = [str(item).strip() for item in items if str(item or "").strip()]
        if not normalized:
            continue
        list_items = "".join(f"<li>{html.escape(item)}</li>" for item in normalized)
        sections.append(f'<section class="report-section"><h2>{html.escape(title)}</h2><ul>{list_items}</ul></section>')
    return "\n".join(sections)


def render_file_table(files: list[dict[str, Any]]) -> str:
    if not files:
        return ""
    rows = []
    for file_info in files[:18]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(file_info.get('filename') or ''))}</td>"
            f"<td>{html.escape(str(file_info.get('status') or 'modified'))}</td>"
            f"<td>+{int(file_info.get('additions') or 0)} / -{int(file_info.get('deletions') or 0)}</td>"
            "</tr>"
        )
    if len(files) > 18:
        rows.append(f'<tr><td colspan="3">还有 {len(files) - 18} 个文件未在表格中显示。</td></tr>')
    return "<table><thead><tr><th>文件</th><th>状态</th><th>增删</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def render_badges(items: list[str]) -> str:
    normalized = [str(item).strip() for item in items if str(item or "").strip()]
    return "".join(f'<span class="badge">{html.escape(item)}</span>' for item in normalized[:10])


def render_footer_note(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    parts = []
    pos = 0
    for match in MARKDOWN_LINK_RE.finditer(raw):
        parts.append(html.escape(raw[pos : match.start()]))
        label = html.escape(match.group(1).strip())
        url = html.escape(match.group(2).strip(), quote=True)
        parts.append(f'<a href="{url}">{label}</a>')
        pos = match.end()
    parts.append(html.escape(raw[pos:]))
    return "".join(parts).replace("\n", "<br>")


def render_paragraphs(items: list[str]) -> str:
    normalized = [str(item).strip() for item in items if str(item or "").strip()]
    return "".join(f"<p>{html.escape(item)}</p>" for item in normalized)


def image_data_to_bytes(image_data: object) -> bytes | None:
    if isinstance(image_data, bytes):
        return image_data
    if isinstance(image_data, str):
        if image_data.startswith("data:image"):
            try:
                return base64.b64decode(image_data.split(",", 1)[1])
            except Exception:
                return None
        if os.path.exists(image_data):
            try:
                return Path(image_data).read_bytes()
            except Exception:
                return None
    return None


def is_valid_image_bytes(data: bytes | None) -> bool:
    return bool(data and (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")))
