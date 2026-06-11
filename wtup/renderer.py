from __future__ import annotations

import base64
import html
import os
import time
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    import logging

    logger = logging.getLogger(__name__)

from .config import BRANCH_NAME, PLUGIN_NAME, REPO_FULL_NAME
from .diff_collector import DiffChunk, DiffSummary, short_sha


RENDER_OPTIONS = {
    "width": 1280,
    "height": 920,
    "full_page": True,
    "type": "png",
}


def build_report_html(template_path: Path, summary: DiffSummary, chunk: DiffChunk, analysis: dict[str, Any]) -> str:
    template = template_path.read_text(encoding="utf-8")
    importance = str(analysis.get("importance") or "中")
    title_suffix = f"War Thunder Datamine 更新分析"
    if chunk.total > 1:
        title_suffix += f" ({chunk.index}/{chunk.total})"

    replacements = {
        "{{ hero_image_src }}": "",
        "{{ report_kicker }}": f"GitHub Commit Monitor · 重要度 {importance}",
        "{{ report_title }}": title_suffix,
        "{{ report_subtitle }}": html.escape(str(analysis.get("summary") or "")),
        "{{ repo_name }}": REPO_FULL_NAME,
        "{{ branch_name }}": BRANCH_NAME,
        "{{ commit_range }}": f"{short_sha(summary.base_sha)}...{short_sha(summary.head_sha)}",
        "{{ change_stats }}": f"{len(chunk.files)}/{summary.total_files} 文件 · {chunk.patch_chars} 字符",
        "{{ summary_html }}": render_paragraphs([str(analysis.get("summary") or "")]),
        "{{ badges_html }}": render_badges([f"重要度 {importance}", *list(analysis.get("tags") or [])]),
        "{{ article_html }}": render_article(analysis),
        "{{ table_html }}": render_file_table(chunk.files),
        "{{ sections_html }}": render_sections(analysis),
        "{{ footer_note }}": html.escape(f"Source: {summary.compare_url or 'GitHub compare API'}"),
        "{{ footer_badge }}": html.escape(time.strftime("%Y-%m-%d %H:%M:%S")),
    }
    for needle, value in replacements.items():
        template = template.replace(needle, value)
    return template


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


def render_plain_text(summary: DiffSummary, chunk: DiffChunk, analysis: dict[str, Any]) -> str:
    lines = [
        "War Thunder Datamine 更新分析",
        f"仓库: {REPO_FULL_NAME}",
        f"分支: {BRANCH_NAME}",
        f"范围: {short_sha(summary.base_sha)}...{short_sha(summary.head_sha)}",
    ]
    if chunk.total > 1:
        lines.append(f"分片: {chunk.index}/{chunk.total}")
    lines.extend(
        [
            f"重要度: {analysis.get('importance') or '中'}",
            f"摘要: {analysis.get('summary') or ''}",
        ]
    )
    for title, key in (("重点变化", "highlights"), ("可能影响", "player_impact"), ("风险/不确定", "risks")):
        values = [str(item) for item in analysis.get(key, []) if str(item).strip()]
        if not values:
            continue
        lines.append(f"\n{title}:")
        lines.extend(f"- {item}" for item in values)
    recommendation = str(analysis.get("recommendation") or "").strip()
    if recommendation:
        lines.extend(["", f"建议: {recommendation}"])
    return "\n".join(lines)


def render_article(analysis: dict[str, Any]) -> str:
    recommendation = str(analysis.get("recommendation") or "").strip()
    if not recommendation:
        return ""
    return f"<h2>建议</h2><p>{html.escape(recommendation)}</p>"


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
