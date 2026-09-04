from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_logger = logging.getLogger("wtup_standalone.renderer")

DEFAULT_RENDER_TIMEOUT_SECONDS = 30.0
TEMPLATE_DISCORD = "discord"
TEMPLATE_MIKU = "miku"
TEMPLATE_MOBILE = "mobile"
SUPPORTED_TEMPLATES = {TEMPLATE_DISCORD, TEMPLATE_MIKU, TEMPLATE_MOBILE, "1", "2", "3"}

STATIC_FONTS_DIR = Path(__file__).resolve().parent / "static" / "fonts"
_FONT_CACHE: dict[str, str] = {}


def get_font_base64(name: str) -> str:
    """读取本地 Discord 官方 gg sans 字体并转为 base64 嵌入，确保完全离线可用且零跨域。"""
    if name not in _FONT_CACHE:
        font_path = STATIC_FONTS_DIR / name
        if font_path.exists():
            try:
                _FONT_CACHE[name] = base64.b64encode(font_path.read_bytes()).decode("ascii")
            except Exception:
                _FONT_CACHE[name] = ""
        else:
            _FONT_CACHE[name] = ""
    return _FONT_CACHE[name]


# 内置防乱码 SVG 矢量图标（完全内联，不依赖系统 Emoji 字体，绝无 □ 豆腐块）
SVG_FOLDER = '<svg viewBox="0 0 24 24" width="14" height="14" fill="#949ba4" style="vertical-align:-2px;"><path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>'
SVG_SPARKLE = '<svg viewBox="0 0 24 24" width="15" height="15" fill="#5865F2" style="vertical-align:-2px;"><path d="M12 2L14.4 7.6L20 10L14.4 12.4L12 18L9.6 12.4L4 10L9.6 7.6L12 2Z"/></svg>'
SVG_SWORDS = '<svg viewBox="0 0 24 24" width="14" height="14" fill="#5865F2" style="vertical-align:-2px;"><path d="M6.92 5L5 6.92l4.95 4.95l1.92-1.92L6.92 5zm12.08 14l-4.95-4.95l-1.92 1.92l4.95 4.95l1.92-1.92zM19 5l-1.92-1.92l-4.95 4.95l1.92 1.92L19 5zM5 19l4.95-4.95l-1.92-1.92L3.08 17.08L5 19z"/></svg>'
SVG_WARNING = '<svg viewBox="0 0 24 24" width="14" height="14" fill="#fee75c" style="vertical-align:-2px;"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>'
SVG_BULB = '<svg viewBox="0 0 24 24" width="14" height="14" fill="#fee75c" style="vertical-align:-2px;"><path d="M12 2C7.58 2 4 5.58 4 10c0 2.76 1.4 5.2 3.54 6.63V19c0 .55.45 1 1 1h6.92c.55 0 1-.45 1-1v-2.37C18.6 15.2 20 12.76 20 10c0-4.42-3.58-8-8-8zm-2 19h4v1h-4v-1zm-1-3v-1.1l-.64-.43C6.86 15.42 6 13.82 6 10c0-3.31 2.69-6 6-6s6 2.69 6 6c0 3.82-.86 5.42-2.36 6.47l-.64.43V18H9z"/></svg>'
SVG_BOT = '<svg viewBox="0 0 24 24" width="14" height="14" fill="#949ba4" style="vertical-align:-2px;"><path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 0 1 7 7h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-1H2a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1a7 7 0 0 1 7-7h1V5.73c-.6-.34-1-.99-1-1.73a2 2 0 0 1 2-2M7.5 13A1.5 1.5 0 0 0 6 14.5 1.5 1.5 0 0 0 7.5 16 1.5 1.5 0 0 0 9 14.5 1.5 1.5 0 0 0 7.5 13m9 0a1.5 1.5 0 0 0-1.5 1.5 1.5 1.5 0 0 0 1.5 1.5 1.5 1.5 0 0 0 1.5-1.5 1.5 1.5 0 0 0-1.5-1.5"/></svg>'
SVG_WRENCH = '<svg viewBox="0 0 24 24" width="14" height="14" fill="#5865F2" style="vertical-align:-2px;"><path d="M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6 6 9 1.6 4.7C.4 7.1.9 10.1 2.9 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.3-2.3c.5-.4.5-1.1.1-1.4z"/></svg>'
SVG_SUB_ARROW = '<svg viewBox="0 0 16 16" width="11" height="11" fill="#5865F2" style="vertical-align:-1px; margin-right:4px;"><path d="M2 2v6h8V5l4 4-4 4v-3H1V2h1z"/></svg>'

SVG_AVATAR = '''<svg viewBox="0 0 44 44" width="42" height="42" style="border-radius: 50%; box-shadow: 0 4px 10px rgba(0,0,0,0.35);">
  <defs>
    <linearGradient id="avGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#5865F2" />
      <stop offset="100%" stop-color="#3C45A5" />
    </linearGradient>
  </defs>
  <rect width="44" height="44" rx="22" fill="url(#avGrad)" />
  <path d="M12 14h20l-3 12h-14z" fill="rgba(255,255,255,0.2)" />
  <text x="22" y="27" font-family="'gg sans', Arial, sans-serif" font-weight="900" font-size="16" fill="#ffffff" text-anchor="middle" letter-spacing="0.5">WT</text>
  <circle cx="34" cy="34" r="4.5" fill="#23A55A" stroke="#313338" stroke-width="2" />
</svg>'''

SVG_AVATAR_MOBILE = '''<svg viewBox="0 0 44 44" width="42" height="42" style="border-radius: 50%; box-shadow: 0 4px 10px rgba(0,0,0,0.35);">
  <defs>
    <linearGradient id="avGradMobile" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#f59e0b" />
      <stop offset="100%" stop-color="#b45309" />
    </linearGradient>
  </defs>
  <rect width="44" height="44" rx="22" fill="url(#avGradMobile)" />
  <path d="M12 14h20l-3 12h-14z" fill="rgba(255,255,255,0.2)" />
  <text x="22" y="27" font-family="'gg sans', Arial, sans-serif" font-weight="900" font-size="13" fill="#ffffff" text-anchor="middle" letter-spacing="0.5">WTM</text>
  <circle cx="34" cy="34" r="4.5" fill="#10B981" stroke="#1e293b" stroke-width="2" />
</svg>'''


def normalize_template_name(template: str | None, is_mobile: bool = False) -> str:
    t = str(template or "").strip().lower()
    if is_mobile or t in ("mobile", "3", "wtm", "wt_mobile", "shouyou", "mobile_style", "mobile_discord"):
        return TEMPLATE_MOBILE
    if t in ("miku", "2", "miku_style", "help_miku"):
        return TEMPLATE_MIKU
    return TEMPLATE_DISCORD


def generate_watermark_css_and_overlay(
    watermark_config: dict[str, Any] | None,
    theme: str = "discord",
) -> tuple[str, str]:
    """根据水印配置生成 CSS 样式与 HTML 覆盖层。
    
    返回: (css_code, html_overlay_code)
    若未开启水印或配置为空，返回 ("", "")。
    """
    if not watermark_config:
        return "", ""
    
    enabled = bool(watermark_config.get("watermark_enabled", False))
    if not enabled:
        return "", ""

    text = str(watermark_config.get("watermark_text") or "War Thunder Datamine").strip()
    if not text:
        return "", ""

    try:
        opacity = float(watermark_config.get("watermark_opacity", 0.12))
        opacity = max(0.01, min(0.9, opacity))
    except (TypeError, ValueError):
        opacity = 0.12

    try:
        size = int(watermark_config.get("watermark_size", 18))
        size = max(10, min(60, size))
    except (TypeError, ValueError):
        size = 18

    density = str(watermark_config.get("watermark_density", "medium")).lower().strip()
    density_map = {
        "high": (180, 120),
        "medium": (260, 180),
        "low": (380, 260),
    }
    w, h = density_map.get(density, (260, 180))

    # 配适主题颜色
    if theme == "miku":
        color = f"rgba(13, 148, 136, {opacity})"
    elif theme == "mobile":
        color = f"rgba(245, 158, 11, {opacity})"
    else:
        color = f"rgba(255, 255, 255, {opacity})"

    import urllib.parse
    escaped_text = html.escape(text)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">
<text x="{w/2}" y="{h/2}" text-anchor="middle" dominant-baseline="middle"
      transform="rotate(-25 {w/2} {h/2})"
      fill="{color}" font-size="{size}px" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="1px">
{escaped_text}
</text>
</svg>'''
    encoded_svg = urllib.parse.quote(svg)
    data_uri = f"data:image/svg+xml;utf8,{encoded_svg}"

    css = f'''
    .wt-watermark-overlay {{
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
        z-index: 999;
        background-repeat: repeat;
        background-image: url('{data_uri}');
    }}
    '''
    overlay_html = '<div class="wt-watermark-overlay"></div>'
    return css, overlay_html


def _build_mobile_html_report(
    report_data: dict[str, Any],
    enable_ai_analysis: bool = False,
    watermark_config: dict[str, Any] | None = None,
) -> str:
    """模板 3：战争雷霆手游专属风格 (War Thunder Mobile Theme)。
    黑曜石底色搭配琥珀金高光，醒目标注『战争雷霆手游』、『手游专刊』与专属仓库路径。
    """
    if watermark_config is None:
        watermark_config = report_data.get("watermark_config")
    wm_css, wm_overlay = generate_watermark_css_and_overlay(watermark_config, theme="mobile")
    raw_title = str(report_data.get("report_title") or "War Thunder Mobile 更新").strip()
    if "->" in raw_title:
        title = html.escape(raw_title.replace("->", " → "))
    else:
        title = html.escape(raw_title)

    summary = html.escape(str(report_data.get("summary") or ""))
    importance = str(report_data.get("importance") or "中").strip()

    if importance == "高":
        accent_color = "#f43f5e"
        imp_bg = "rgba(244, 63, 94, 0.15)"
        imp_border = "#f43f5e"
        imp_text = "#fda4af"
    elif importance == "低":
        accent_color = "#10b981"
        imp_bg = "rgba(16, 185, 129, 0.12)"
        imp_border = "#10b981"
        imp_text = "#6ee7b7"
    else:
        accent_color = "#f59e0b"
        imp_bg = "rgba(245, 158, 11, 0.15)"
        imp_border = "#f59e0b"
        imp_text = "#fbbf24"

    tags = [html.escape(str(t)) for t in report_data.get("tags") or []]
    tag_badges = "".join(f'<span class="mobile-badge">{t}</span>' for t in tags)

    version_range = html.escape(str(report_data.get("version_range") or ""))
    range_badge = f'<span class="mobile-badge badge-range">{version_range}</span>' if version_range else ""

    reg_b64 = get_font_base64("ggsans.woff2")
    semi_b64 = get_font_base64("ggsanssemibold.woff2")
    bold_b64 = get_font_base64("ggsansbold.woff2")
    mono_b64 = get_font_base64("ggsansmono.woff2")

    font_face_css = f'''
    @font-face {{
        font-family: "gg sans";
        font-weight: 400;
        font-style: normal;
        src: {f'url("data:font/woff2;base64,{reg_b64}") format("woff2"),' if reg_b64 else ''}
             url("/static/fonts/ggsans.woff2") format("woff2"),
             url("https://cdn.jsdelivr.net/gh/damnxav/gg-sans-font@master/ggsans.woff2") format("woff2");
    }}
    @font-face {{
        font-family: "gg sans";
        font-weight: 600;
        font-style: normal;
        src: {f'url("data:font/woff2;base64,{semi_b64}") format("woff2"),' if semi_b64 else ''}
             url("/static/fonts/ggsanssemibold.woff2") format("woff2"),
             url("https://cdn.jsdelivr.net/gh/damnxav/gg-sans-font@master/ggsanssemibold.woff2") format("woff2");
    }}
    @font-face {{
        font-family: "gg sans";
        font-weight: 700;
        font-style: normal;
        src: {f'url("data:font/woff2;base64,{bold_b64}") format("woff2"),' if bold_b64 else ''}
             url("/static/fonts/ggsansbold.woff2") format("woff2"),
             url("https://cdn.jsdelivr.net/gh/damnxav/gg-sans-font@master/ggsansbold.woff2") format("woff2");
    }}
    @font-face {{
        font-family: "gg sans mono";
        font-weight: 400;
        font-style: normal;
        src: {f'url("data:font/woff2;base64,{mono_b64}") format("woff2"),' if mono_b64 else ''}
             url("/static/fonts/ggsansmono.woff2") format("woff2"),
             url("https://cdn.jsdelivr.net/gh/damnxav/gg-sans-font@master/ggsansmono.woff2") format("woff2");
    }}
    '''

    datamine_text = str(report_data.get("datamine_text") or "").strip()
    datamine_code_html = ""
    if datamine_text:
        datamine_code_html = f'''
        <div class="code-wrapper">
            <div class="code-bar">
                <span class="code-lang">📱 WT MOBILE CHANGELOG SPEC</span>
            </div>
            <pre class="code-box"><code>{html.escape(datamine_text)}</code></pre>
        </div>
        '''

    sections_html = []
    if not datamine_text:
        for sec in report_data.get("update_sections") or []:
            sec_title = html.escape(str(sec.get("title") or "更新内容"))
            items = sec.get("items") or []
            if not items:
                continue
            item_nodes = []
            for it in items:
                if isinstance(it, dict):
                    t_val = html.escape(str(it.get("text") or ""))
                    children = it.get("children") or []
                    c_html = ""
                    if children:
                        c_nodes = []
                        for c in children:
                            c_txt = html.escape(str(c.get("text") if isinstance(c, dict) else c))
                            c_nodes.append(f'<div class="sub-item">{SVG_SUB_ARROW}{c_txt}</div>')
                        c_html = f'<div class="sub-items">{"".join(c_nodes)}</div>'
                    item_nodes.append(f'<div class="update-item"><div class="item-title"><span class="bullet" style="color:#f59e0b">•</span> {t_val}</div>{c_html}</div>')
                else:
                    item_nodes.append(f'<div class="update-item"><div class="item-title"><span class="bullet" style="color:#f59e0b">•</span> {html.escape(str(it))}</div></div>')

            sections_html.append(f'''
            <div class="embed-field">
                <div class="field-title" style="color:#fbbf24">{SVG_WRENCH}<span>{sec_title}</span></div>
                <div class="field-content">{"".join(item_nodes)}</div>
            </div>
            ''')

    ai_html = ""
    if enable_ai_analysis:
        ai = report_data.get("ai_analysis") or {}
        if ai and (ai.get("player_impact") or ai.get("recommendation") or ai.get("uncertainties")):
            blocks = []
            if ai.get("player_impact"):
                impact_lines = "".join(f'<li>{html.escape(str(p))}</li>' for p in ai["player_impact"])
                blocks.append(f'''
                <div class="ai-sub-block">
                    <div class="ai-label impact-label">{SVG_SWORDS}<span>手游战术与实战影响</span></div>
                    <ul class="ai-list">{impact_lines}</ul>
                </div>
                ''')
            if ai.get("uncertainties"):
                unc_lines = "".join(f'<li>{html.escape(str(u))}</li>' for u in ai["uncertainties"])
                blocks.append(f'''
                <div class="ai-sub-block">
                    <div class="ai-label warn-label">{SVG_WARNING}<span>机制细节与不确定项</span></div>
                    <ul class="ai-list">{unc_lines}</ul>
                </div>
                ''')
            if ai.get("recommendation"):
                rec_text = html.escape(str(ai["recommendation"]))
                blocks.append(f'''
                <div class="ai-recommendation">
                    <span class="rec-title">{SVG_BULB}综合研判：</span>{rec_text}
                </div>
                ''')
            ai_html = f'''
            <div class="ai-analysis-card" style="border-left: 4px solid #f59e0b;">
                <div class="ai-header" style="color:#fbbf24">
                    {SVG_SPARKLE}
                    <span>AI 智能战术研判与机制推演 (手游专栏)</span>
                </div>
                {"".join(blocks)}
            </div>
            '''

    images = report_data.get("images") or []
    images_html = ""
    if images:
        cards = []
        for img in images:
            i_url = html.escape(str(img.get("url") or ""))
            i_name = html.escape(str(img.get("name") or "Asset"))
            i_type = html.escape(str(img.get("type") or "美术资产"))
            cards.append(f'''
            <div class="asset-card">
                <div class="asset-img-box"><img src="{i_url}" alt="{i_name}" onerror="this.style.opacity=0.4" /></div>
                <div class="asset-name" title="{i_name}">{i_name}</div>
                <span class="asset-type">{i_type}</span>
            </div>
            ''')
        images_html = f'''
        <div class="embed-field">
            <div class="field-title" style="justify-content: center; text-align: center; color:#fbbf24">🎨 新增美术与贴花资产 ({len(images)})</div>
            <div class="asset-container">
                <div class="asset-grid">{"".join(cards)}</div>
            </div>
        </div>
        '''

    usage = report_data.get("token_usage") or {}
    total_tokens = usage.get("total_tokens", 0)
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    elapsed = report_data.get("elapsed_seconds", 0.0)
    model_name = html.escape(str(report_data.get("model") or "默认模型"))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} (War Thunder Mobile Theme)</title>
<style>
    {font_face_css}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
        background-color: #1e1f22;
        font-family: "gg sans", "Noto Sans SC", "Helvetica Neue", Helvetica, Arial, "Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji", sans-serif;
        color: #dbdee1;
        -webkit-font-smoothing: antialiased;
        padding: 0;
        margin: 0;
        display: block;
        width: 100%;
    }}
    body {{
        padding: 16px;
    }}
    #mobile-canvas {{
        width: 840px;
        margin: 0 auto;
        box-sizing: border-box;
        height: fit-content;
        background: linear-gradient(180deg, #1e1f22 0%, #17181a 100%);
        padding: 20px 24px;
        display: flex;
        gap: 16px;
        align-items: flex-start;
        border: 1px solid rgba(245, 158, 11, 0.25);
        border-radius: 12px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.4);
        position: relative;
        overflow: hidden;
    }}
    {wm_css}
    .avatar {{
        flex-shrink: 0;
        display: flex;
        align-items: center;
        justify-content: center;
    }}
    .message-body {{
        flex: 1;
        min-width: 0;
    }}
    .message-header {{
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
        line-height: 1.3;
    }}
    .author-name {{
        color: #fbbf24;
        font-size: 15px;
        font-weight: 700;
        letter-spacing: 0.2px;
    }}
    .mobile-bot-pill {{
        background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
        color: #ffffff;
        font-size: 10px;
        font-weight: 800;
        padding: 1px 6px;
        border-radius: 4px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        box-shadow: 0 2px 6px rgba(245, 158, 11, 0.35);
    }}
    .channel-source {{
        color: #f59e0b;
        font-size: 11px;
        opacity: 0.85;
    }}
    .timestamp {{
        color: #949ba4;
        font-size: 11px;
    }}
    .discord-embed {{
        background-color: #2b2d31;
        border-left: 4px solid #f59e0b;
        border-radius: 0 8px 8px 0;
        padding: 14px 18px;
        display: flex;
        flex-direction: column;
        gap: 12px;
    }}
    .embed-repo-author {{
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: #fbbf24;
        font-weight: 600;
    }}
    .embed-title {{
        font-size: 20px;
        font-weight: 800;
        color: #ffffff;
        line-height: 1.35;
        letter-spacing: -0.2px;
    }}
    .meta-pills {{
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        align-items: center;
    }}
    .mobile-badge {{
        font-size: 11px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 4px;
        background-color: #383a40;
        color: #e0e1e5;
        border: 1px solid rgba(255,255,255,0.06);
    }}
    .badge-mobile-tag {{
        background: linear-gradient(135deg, rgba(245, 158, 11, 0.25) 0%, rgba(217, 119, 6, 0.3) 100%);
        border: 1px solid #f59e0b;
        color: #fbbf24;
        font-weight: 800;
    }}
    .badge-importance {{
        background-color: {imp_bg};
        border: 1px solid {imp_border};
        color: {imp_text};
    }}
    .badge-range {{
        font-family: "gg sans mono", monospace;
        color: #949ba4;
    }}
    .embed-description {{
        font-size: 13.5px;
        line-height: 1.5;
        color: #dbdee1;
        background-color: rgba(0,0,0,0.18);
        padding: 10px 14px;
        border-radius: 6px;
        border-left: 2px solid #f59e0b;
    }}
    .code-wrapper {{
        background-color: #1e1f22;
        border-radius: 6px;
        border: 1px solid #383a40;
        overflow: hidden;
    }}
    .code-bar {{
        background-color: #232428;
        padding: 5px 12px;
        border-bottom: 1px solid #383a40;
        display: flex;
        align-items: center;
        justify-content: space-between;
    }}
    .code-lang {{
        font-size: 11px;
        font-weight: 700;
        color: #fbbf24;
        letter-spacing: 0.5px;
    }}
    .code-box {{
        padding: 12px;
        font-family: "gg sans mono", "Consolas", monospace;
        font-size: 12px;
        line-height: 1.45;
        color: #dbdee1;
        white-space: pre-wrap;
        word-break: break-all;
    }}
    .embed-field {{
        display: flex;
        flex-direction: column;
        gap: 6px;
    }}
    .field-title {{
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        font-weight: 700;
        color: #f2f3f5;
        text-transform: uppercase;
        letter-spacing: 0.3px;
    }}
    .field-content {{
        display: flex;
        flex-direction: column;
        gap: 6px;
    }}
    .update-item {{
        background-color: #232428;
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 12.5px;
        line-height: 1.45;
        border: 1px solid rgba(255,255,255,0.04);
    }}
    .item-title {{
        font-weight: 600;
        color: #e0e1e5;
    }}
    .bullet {{
        font-weight: 900;
        margin-right: 4px;
    }}
    .sub-items {{
        margin-top: 5px;
        margin-left: 12px;
        display: flex;
        flex-direction: column;
        gap: 4px;
    }}
    .sub-item {{
        font-size: 12px;
        color: #c4c9ce;
        line-height: 1.4;
    }}
    .ai-analysis-card {{
        background-color: #232428;
        border-radius: 6px;
        padding: 12px 16px;
        display: flex;
        flex-direction: column;
        gap: 10px;
    }}
    .ai-header {{
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        font-weight: 800;
        letter-spacing: 0.3px;
    }}
    .ai-sub-block {{
        display: flex;
        flex-direction: column;
        gap: 4px;
    }}
    .ai-label {{
        display: flex;
        align-items: center;
        gap: 5px;
        font-size: 12px;
        font-weight: 700;
    }}
    .impact-label {{ color: #fbbf24; }}
    .warn-label {{ color: #fee75c; }}
    .ai-list {{
        margin-left: 20px;
        font-size: 12px;
        color: #dbdee1;
        line-height: 1.45;
    }}
    .ai-recommendation {{
        background-color: rgba(245, 158, 11, 0.12);
        border: 1px solid rgba(245, 158, 11, 0.35);
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 12px;
        color: #fef3c7;
        line-height: 1.45;
    }}
    .rec-title {{
        font-weight: 700;
        color: #fbbf24;
    }}
    .embed-footer {{
        border-top: 1px solid rgba(255,255,255,0.06);
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        font-size: 11px;
        color: #949ba4;
        padding-top: 8px;
        margin-top: 2px;
        flex-wrap: wrap;
    }}
    .footer-left {{
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    .footer-text {{ font-family: "gg sans", "Consolas", "Noto Sans SC", sans-serif; }}
    .footer-dot {{ color: #4e5058; }}
    .footer-brand {{ color: #fbbf24; font-size: 10.5px; font-weight: 700; }}
</style>
</head>
<body>
<div id="mobile-canvas">
    {wm_overlay}
    <div class="avatar">{SVG_AVATAR_MOBILE}</div>
    <div class="message-body">
        <div class="message-header">
            <span class="author-name">War Thunder Mobile · 战争雷霆手游</span>
            <span class="mobile-bot-pill">📱 手游专刊</span>
            <span class="channel-source">@WTM Mobile Dispatch</span>
            <span class="timestamp">Today at Mobile Center</span>
        </div>

        <div class="discord-embed">
            <div class="embed-repo-author">
                {SVG_FOLDER}
                <span>gszabi99/War-Thunder-Mobile-Datamine (master)</span>
            </div>

            <div class="embed-title">📱 战雷手游更新: {title}</div>

            <div class="meta-pills">
                <span class="mobile-badge badge-mobile-tag">📱 战雷手游</span>
                <span class="mobile-badge badge-importance">重要度: {importance}</span>
                {range_badge}
                {tag_badges}
            </div>

            <div class="embed-description">{summary}</div>

            {datamine_code_html}
            {"".join(sections_html)}
            {ai_html}
            {images_html}

            <div class="embed-footer">
                <div class="footer-left">
                    {SVG_BOT}
                    <span class="footer-text">{model_name} <span class="footer-dot">•</span> 消耗: {total_tokens:,} Tokens ({prompt_tokens:,} / {completion_tokens:,}) <span class="footer-dot">•</span> 耗时 {elapsed:.2f}s</span>
                </div>
                <div class="footer-brand">War Thunder Mobile Style · 手游更新简报</div>
            </div>
        </div>
    </div>
</div>
</body>
</html>"""


def build_html_report(
    report_data: dict[str, Any],
    template: str = TEMPLATE_DISCORD,
    enable_ai_analysis: bool | None = None,
    watermark_config: dict[str, Any] | None = None,
) -> str:
    """生成 HTML 报告文档，支持选择模板风格与平铺水印：
    1. 'discord' (默认) - Discord 官方深色质感与官方 gg sans 字体风格 (端游)
    2. 'miku' - 初音未来清爽青绿微渐变风格
    3. 'mobile' - 战争雷霆手游专属风格 (金珀黑曜质感，醒目标注战争雷霆手游与手游专刊)
    支持是否包含 AI 战术研判开关与报告水印渲染配置。
    """
    is_mobile = str(report_data.get("target") or "").lower() == "mobile" or str(template or "").lower() in ("mobile", "wtm", "shouyou", "3")
    selected = normalize_template_name(template, is_mobile=is_mobile)
    if enable_ai_analysis is None:
        enable_ai_analysis = bool(report_data.get("enable_ai_analysis", False))
    if watermark_config is None:
        watermark_config = report_data.get("watermark_config")
    if selected == TEMPLATE_MOBILE:
        return _build_mobile_html_report(report_data, enable_ai_analysis=enable_ai_analysis, watermark_config=watermark_config)
    if selected == TEMPLATE_MIKU:
        return _build_miku_html_report(report_data, enable_ai_analysis=enable_ai_analysis, watermark_config=watermark_config)
    return _build_discord_html_report(report_data, enable_ai_analysis=enable_ai_analysis, watermark_config=watermark_config)


def _build_discord_html_report(
    report_data: dict[str, Any],
    enable_ai_analysis: bool = False,
    watermark_config: dict[str, Any] | None = None,
) -> str:
    """模板 1：重构版 Discord 官方客户端暗黑风格 (Discord Dark Theme)，全链路采用官方 gg sans 字体。"""
    if watermark_config is None:
        watermark_config = report_data.get("watermark_config")
    wm_css, wm_overlay = generate_watermark_css_and_overlay(watermark_config, theme="discord")
    raw_title = str(report_data.get("report_title") or "War Thunder Datamine 更新").strip()
    if "->" in raw_title:
        title = html.escape(raw_title.replace("->", " → "))
    else:
        title = html.escape(raw_title)

    summary = html.escape(str(report_data.get("summary") or ""))
    importance = str(report_data.get("importance") or "中").strip()

    if importance == "高":
        accent_color = "#ed4245"  # Discord Red
        imp_bg = "rgba(237, 66, 69, 0.15)"
        imp_border = "#ed4245"
        imp_text = "#ff7b7d"
    elif importance == "低":
        accent_color = "#57f287"  # Discord Green
        imp_bg = "rgba(87, 242, 135, 0.12)"
        imp_border = "#57f287"
        imp_text = "#57f287"
    else:
        accent_color = "#fee75c"  # Discord Yellow
        imp_bg = "rgba(254, 231, 92, 0.12)"
        imp_border = "#fee75c"
        imp_text = "#fee75c"

    tags = [html.escape(str(t)) for t in report_data.get("tags") or []]
    tag_badges = "".join(f'<span class="discord-badge">{t}</span>' for t in tags)

    version_range = html.escape(str(report_data.get("version_range") or ""))
    range_badge = f'<span class="discord-badge badge-range">{version_range}</span>' if version_range else ""

    # 读取内置 Discord gg sans 字体数据，支持数据级完全嵌入
    reg_b64 = get_font_base64("ggsans.woff2")
    semi_b64 = get_font_base64("ggsanssemibold.woff2")
    bold_b64 = get_font_base64("ggsansbold.woff2")
    mono_b64 = get_font_base64("ggsansmono.woff2")

    font_face_css = f'''
    @font-face {{
        font-family: "gg sans";
        font-weight: 400;
        font-style: normal;
        src: {f'url("data:font/woff2;base64,{reg_b64}") format("woff2"),' if reg_b64 else ''}
             url("/static/fonts/ggsans.woff2") format("woff2"),
             url("https://cdn.jsdelivr.net/gh/damnxav/gg-sans-font@master/ggsans.woff2") format("woff2");
    }}
    @font-face {{
        font-family: "gg sans";
        font-weight: 600;
        font-style: normal;
        src: {f'url("data:font/woff2;base64,{semi_b64}") format("woff2"),' if semi_b64 else ''}
             url("/static/fonts/ggsanssemibold.woff2") format("woff2"),
             url("https://cdn.jsdelivr.net/gh/damnxav/gg-sans-font@master/ggsanssemibold.woff2") format("woff2");
    }}
    @font-face {{
        font-family: "gg sans";
        font-weight: 700;
        font-style: normal;
        src: {f'url("data:font/woff2;base64,{bold_b64}") format("woff2"),' if bold_b64 else ''}
             url("/static/fonts/ggsansbold.woff2") format("woff2"),
             url("https://cdn.jsdelivr.net/gh/damnxav/gg-sans-font@master/ggsansbold.woff2") format("woff2");
    }}
    @font-face {{
        font-family: "gg sans mono";
        font-weight: 400;
        font-style: normal;
        src: {f'url("data:font/woff2;base64,{mono_b64}") format("woff2"),' if mono_b64 else ''}
             url("/static/fonts/ggsansmono.woff2") format("woff2"),
             url("https://cdn.jsdelivr.net/gh/damnxav/gg-sans-font@master/ggsansmono.woff2") format("woff2");
    }}
    '''

    # 1. 纯净 datamine 代码块呈现
    datamine_text = str(report_data.get("datamine_text") or "").strip()
    datamine_code_html = ""
    if datamine_text:
        datamine_code_html = f'''
        <div class="code-wrapper">
            <div class="code-bar">
                <span class="code-lang">DATAMINE CHANGELOG SPEC</span>
            </div>
            <pre class="code-box"><code>{html.escape(datamine_text)}</code></pre>
        </div>
        '''

    # 2. 更新分区条目
    sections_html = []
    if not datamine_text:
        for sec in report_data.get("update_sections") or []:
            sec_title = html.escape(str(sec.get("title") or "更新内容"))
            items = sec.get("items") or []
            if not items:
                continue
            item_nodes = []
            for it in items:
                if isinstance(it, dict):
                    t_val = html.escape(str(it.get("text") or ""))
                    children = it.get("children") or []
                    c_html = ""
                    if children:
                        c_nodes = []
                        for c in children:
                            c_txt = html.escape(str(c.get("text") if isinstance(c, dict) else c))
                            c_nodes.append(f'<div class="sub-item">{SVG_SUB_ARROW}{c_txt}</div>')
                        c_html = f'<div class="sub-items">{"".join(c_nodes)}</div>'
                    item_nodes.append(f'<div class="update-item"><div class="item-title"><span class="bullet">•</span> {t_val}</div>{c_html}</div>')
                else:
                    item_nodes.append(f'<div class="update-item"><div class="item-title"><span class="bullet">•</span> {html.escape(str(it))}</div></div>')

            sections_html.append(f'''
            <div class="embed-field">
                <div class="field-title">{SVG_WRENCH}<span>{sec_title}</span></div>
                <div class="field-content">{"".join(item_nodes)}</div>
            </div>
            ''')

    # 3. AI 智能战术研判卡片 (受开关严格控制，默认关闭不渲染)
    ai_html = ""
    if enable_ai_analysis:
        ai = report_data.get("ai_analysis") or {}
        if ai and (ai.get("player_impact") or ai.get("recommendation") or ai.get("uncertainties")):
            blocks = []
            if ai.get("player_impact"):
                impact_lines = "".join(f'<li>{html.escape(str(p))}</li>' for p in ai["player_impact"])
                blocks.append(f'''
                <div class="ai-sub-block">
                    <div class="ai-label impact-label">{SVG_SWORDS}<span>战术与实战影响</span></div>
                    <ul class="ai-list">{impact_lines}</ul>
                </div>
                ''')
            if ai.get("uncertainties"):
                unc_lines = "".join(f'<li>{html.escape(str(u))}</li>' for u in ai["uncertainties"])
                blocks.append(f'''
                <div class="ai-sub-block">
                    <div class="ai-label warn-label">{SVG_WARNING}<span>机制细节与不确定项</span></div>
                    <ul class="ai-list">{unc_lines}</ul>
                </div>
                ''')
            if ai.get("recommendation"):
                rec_text = html.escape(str(ai["recommendation"]))
                blocks.append(f'''
                <div class="ai-recommendation">
                    <span class="rec-title">{SVG_BULB}综合研判：</span>{rec_text}
                </div>
                ''')

            ai_html = f'''
            <div class="embed-field ai-field">
                <div class="field-title ai-field-title">
                    {SVG_SPARKLE}<span>AI 智能战术研判与深度推演</span>
                </div>
                <div class="ai-card-content">{"".join(blocks)}</div>
            </div>
            '''

    # 4. 美术资产画廊（居中展示，自适应图片比例）
    images = report_data.get("images") or []
    images_html = ""
    if images:
        cards = []
        for img in images:
            i_url = html.escape(str(img.get("url") or ""))
            i_name = html.escape(str(img.get("name") or "Asset"))
            i_type = html.escape(str(img.get("type") or "贴花"))
            cards.append(f'''
            <div class="asset-card">
                <div class="asset-img-box"><img src="{i_url}" alt="{i_name}" onerror="this.style.opacity=0.4" /></div>
                <div class="asset-name" title="{i_name}">{i_name}</div>
                <span class="asset-type">{i_type}</span>
            </div>
            ''')
        images_html = f'''
        <div class="embed-field">
            <div class="field-title" style="justify-content: center; text-align: center;">🎨 新增美术与贴花资产 ({len(images)})</div>
            <div class="asset-container">
                <div class="asset-grid">{"".join(cards)}</div>
            </div>
        </div>
        '''

    usage = report_data.get("token_usage") or {}
    total_tokens = usage.get("total_tokens", 0)
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    elapsed = report_data.get("elapsed_seconds", 0.0)
    model_name = html.escape(str(report_data.get("model") or "默认模型"))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} (Discord Dark Theme)</title>
<style>
    {font_face_css}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
        background-color: #313338;
        font-family: "gg sans", "Noto Sans SC", "Helvetica Neue", Helvetica, Arial, "Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji", sans-serif;
        color: #dbdee1;
        -webkit-font-smoothing: antialiased;
        padding: 0;
        margin: 0;
        display: block;
        width: 100%;
    }}
    body {{
        padding: 16px;
    }}
    #discord-canvas {{
        width: 840px;
        margin: 0 auto;
        box-sizing: border-box;
        height: fit-content;
        background-color: #313338;
        padding: 20px 24px;
        display: flex;
        gap: 16px;
        align-items: flex-start;
        position: relative;
        overflow: hidden;
    }}
    {wm_css}
    .avatar {{
        flex-shrink: 0;
        display: flex;
        align-items: center;
        justify-content: center;
    }}
    .message-body {{
        flex: 1;
        min-width: 0;
    }}
    .message-header {{
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 8px;
        line-height: 1.3;
    }}
    .author-name {{
        color: #f2f3f5;
        font-size: 15px;
        font-weight: 600;
    }}
    .bot-pill {{
        background: #5865f2;
        color: #ffffff;
        font-size: 10px;
        font-weight: 700;
        padding: 1px 5px;
        border-radius: 3px;
        line-height: 1.2;
        letter-spacing: 0.3px;
        text-transform: uppercase;
        display: inline-flex;
        align-items: center;
    }}
    .channel-source {{
        color: #949ba4;
        font-size: 12px;
        margin-left: 2px;
    }}
    .timestamp {{
        color: #949ba4;
        font-size: 12px;
        margin-left: 4px;
    }}

    /* Discord Embed 卡片核心容器 */
    .discord-embed {{
        background: #2b2d31;
        border-left: 4px solid {accent_color};
        border-radius: 4px 8px 8px 4px;
        padding: 16px 20px 14px 20px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.32);
        display: flex;
        flex-direction: column;
        gap: 12px;
    }}
    .embed-repo-author {{
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        font-weight: 600;
        color: #949ba4;
    }}
    .embed-title {{
        font-size: 19px;
        font-weight: 700;
        color: #00a8fc;
        line-height: 1.3;
        display: flex;
        align-items: center;
        gap: 8px;
    }}
    .meta-pills {{
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
        align-items: center;
    }}
    .discord-badge {{
        font-size: 11px;
        font-weight: 600;
        padding: 2px 7px;
        border-radius: 4px;
        background: #1e1f22;
        border: 1px solid #383a40;
        color: #dbdee1;
    }}
    .badge-importance {{
        background: {imp_bg};
        border-color: {imp_border};
        color: {imp_text};
        font-weight: 700;
    }}
    .badge-range {{
        font-family: "gg sans mono", "Consolas", monospace;
        color: #5865f2;
    }}
    .embed-description {{
        font-size: 13.5px;
        line-height: 1.5;
        color: #dbdee1;
        padding: 9px 12px;
        background: #1e1f22;
        border-radius: 4px;
        border-left: 3px solid #5865f2;
    }}

    /* Embed Fields 分区 */
    .embed-field {{
        display: flex;
        flex-direction: column;
        gap: 6px;
    }}
    .field-title {{
        font-size: 13.5px;
        font-weight: 700;
        color: #f2f3f5;
        letter-spacing: 0.2px;
        border-bottom: 1px solid rgba(255,255,255,0.06);
        padding-bottom: 5px;
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    .field-content {{
        display: flex;
        flex-direction: column;
        gap: 6px;
    }}
    .update-item {{
        font-size: 13.5px;
        line-height: 1.45;
        color: #dbdee1;
    }}
    .item-title {{
        color: #f2f3f5;
    }}
    .bullet {{ color: #5865f2; font-weight: bold; margin-right: 4px; }}
    .sub-items {{
        margin-left: 18px;
        margin-top: 3px;
        display: flex;
        flex-direction: column;
        gap: 3px;
    }}
    .sub-item {{
        font-size: 12.5px;
        color: #949ba4;
        display: flex;
        align-items: center;
    }}

    /* AI 战术研判模块 */
    .ai-field {{
        background: rgba(88, 101, 242, 0.05);
        border: 1px solid rgba(88, 101, 242, 0.22);
        border-radius: 6px;
        padding: 12px 14px;
        margin-top: 4px;
    }}
    .ai-field-title {{
        color: #8599ff;
        border-bottom: 1px solid rgba(88, 101, 242, 0.2);
    }}
    .ai-card-content {{
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-top: 4px;
    }}
    .ai-sub-block {{
        display: flex;
        flex-direction: column;
        gap: 4px;
    }}
    .ai-label {{
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.3px;
        display: flex;
        align-items: center;
        gap: 5px;
    }}
    .impact-label {{ color: #5865f2; }}
    .warn-label {{ color: #fee75c; }}
    .ai-list {{
        list-style: disc;
        margin-left: 18px;
        font-size: 12.5px;
        color: #dbdee1;
        line-height: 1.45;
    }}
    .ai-recommendation {{
        background: #1e1f22;
        border-left: 3px solid #5865f2;
        padding: 8px 12px;
        border-radius: 0 4px 4px 0;
        font-size: 12.5px;
        line-height: 1.45;
        color: #c9cdfb;
    }}
    .rec-title {{ font-weight: 700; color: #ffffff; display: inline-flex; align-items: center; gap: 4px; }}

    /* 美术资产画廊：居中展示，自适应图片比例 */
    .asset-container {{
        display: flex;
        justify-content: center;
        align-items: center;
        width: 100%;
    }}
    .asset-grid {{
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 12px;
        width: 100%;
    }}
    .asset-card {{
        background: #1e1f22;
        border: 1px solid #383a40;
        border-radius: 8px;
        padding: 10px;
        text-align: center;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        max-width: 240px;
        flex: 0 1 auto;
    }}
    .asset-img-box {{
        width: 100%;
        display: flex;
        align-items: center;
        justify-content: center;
        margin-bottom: 6px;
    }}
    .asset-img-box img {{
        max-height: 140px;
        max-width: 100%;
        width: auto;
        height: auto;
        object-fit: contain;
        border-radius: 4px;
        margin: 0 auto;
        display: block;
    }}
    .asset-name {{
        font-size: 11px;
        font-weight: 600;
        color: #dbdee1;
        width: 100%;
        text-align: center;
        word-break: break-word;
        line-height: 1.3;
    }}
    .asset-type {{
        font-size: 9.5px;
        font-weight: 600;
        color: #949ba4;
        background: #2b2d31;
        padding: 1px 6px;
        border-radius: 3px;
        margin-top: 4px;
        display: inline-block;
    }}

    /* 代码块 */
    .code-wrapper {{
        background: #1e1f22;
        border: 1px solid #232428;
        border-radius: 4px;
        overflow: hidden;
    }}
    .code-bar {{
        background: #111214;
        padding: 4px 10px;
        font-size: 10.5px;
        color: #80848e;
        font-family: monospace;
    }}
    .code-box {{
        padding: 10px;
        font-family: "gg sans mono", "Consolas", monospace;
        font-size: 12px;
        color: #dbdee1;
        white-space: pre-wrap;
        line-height: 1.45;
    }}

    /* Embed Footer */
    .embed-footer {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        font-size: 11px;
        color: #949ba4;
        padding-top: 8px;
        margin-top: 2px;
        flex-wrap: wrap;
    }}
    .footer-left {{
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    .footer-text {{ font-family: "gg sans", "Consolas", "Noto Sans SC", sans-serif; }}
    .footer-dot {{ color: #4e5058; }}
    .footer-brand {{ color: #80848e; font-size: 10.5px; }}
</style>
</head>
<body>
<div id="discord-canvas">
    {wm_overlay}
    <div class="avatar">{SVG_AVATAR}</div>
    <div class="message-body">
        <div class="message-header">
            <span class="author-name">wasabi (gszabi99)</span>
            <span class="bot-pill">BOT</span>
            <span class="channel-source">@Datamine Dispatch</span>
            <span class="timestamp">Today at Datamine Center</span>
        </div>

        <div class="discord-embed">
            <div class="embed-repo-author">
                {SVG_FOLDER}
                <span>gszabi99/War-Thunder-Datamine (master)</span>
            </div>

            <div class="embed-title">{title}</div>

            <div class="meta-pills">
                <span class="discord-badge badge-importance">重要度: {importance}</span>
                {range_badge}
                {tag_badges}
            </div>

            <div class="embed-description">{summary}</div>

            {datamine_code_html}
            {"".join(sections_html)}
            {ai_html}
            {images_html}

            <div class="embed-footer">
                <div class="footer-left">
                    {SVG_BOT}
                    <span class="footer-text">{model_name} <span class="footer-dot">•</span> 消耗: {total_tokens:,} Tokens ({prompt_tokens:,} / {completion_tokens:,}) <span class="footer-dot">•</span> 耗时 {elapsed:.2f}s</span>
                </div>
                <div class="footer-brand">Discord Dark Style</div>
            </div>
        </div>
    </div>
</div>
</body>
</html>"""


def _build_miku_html_report(
    report_data: dict[str, Any],
    enable_ai_analysis: bool = False,
    watermark_config: dict[str, Any] | None = None,
) -> str:
    """模板 2：初音未来 Miku 风格 (清新蓝绿微渐变风格)。"""
    if watermark_config is None:
        watermark_config = report_data.get("watermark_config")
    wm_css, wm_overlay = generate_watermark_css_and_overlay(watermark_config, theme="miku")
    title = html.escape(str(report_data.get("report_title") or "War Thunder Datamine 更新报告"))
    summary = html.escape(str(report_data.get("summary") or ""))
    importance = html.escape(str(report_data.get("importance") or "中"))
    tags = [html.escape(str(t)) for t in report_data.get("tags") or []]
    tag_badges = "".join(f'<span class="miku-badge miku-tag">{t}</span>' for t in tags)

    datamine_text = str(report_data.get("datamine_text") or "").strip()
    datamine_box_html = ""
    if datamine_text:
        datamine_box_html = f'''
        <div class="miku-spec-box">
            <div class="spec-header">
                <span class="dot"></span>
                <span>DATAMINE TREE SPECIFICATION</span>
            </div>
            <pre class="spec-content"><code>{html.escape(datamine_text)}</code></pre>
        </div>
        '''

    sections_html = []
    if not datamine_text:
        for sec in report_data.get("update_sections") or []:
            sec_title = html.escape(str(sec.get("title") or "更新内容"))
            items = sec.get("items") or []
            if not items:
                continue
            item_nodes = []
            for it in items:
                if isinstance(it, dict):
                    text = html.escape(str(it.get("text") or ""))
                    children = it.get("children") or []
                    children_html = ""
                    if children:
                        c_list = "".join(f'<li>{html.escape(str(c.get("text") if isinstance(c, dict) else c))}</li>' for c in children)
                        children_html = f'<ul class="sub-list">{c_list}</ul>'
                    item_nodes.append(f'<li class="update-item"><div class="item-text">{text}</div>{children_html}</li>')
                else:
                    item_nodes.append(f'<li class="update-item"><div class="item-text">{html.escape(str(it))}</div></li>')
            if item_nodes:
                sections_html.append(f'''
                <div class="miku-card-section">
                    <h3 class="section-title">{sec_title}</h3>
                    <ul class="update-list">{"".join(item_nodes)}</ul>
                </div>
                ''')

    # AI 战术研判模块 (受开关控制，默认关闭)
    ai_html = ""
    if enable_ai_analysis:
        ai = report_data.get("ai_analysis") or {}
        if ai and (ai.get("player_impact") or ai.get("recommendation") or ai.get("uncertainties")):
            blocks = []
            if ai.get("player_impact"):
                impact_lines = "".join(f'<li>{html.escape(str(p))}</li>' for p in ai["player_impact"])
                blocks.append(f'''
                <div style="margin-bottom: 10px;">
                    <div style="font-size: 12.5px; font-weight: 700; color: #0d9488; margin-bottom: 4px;">⚔️ 战术与对局影响</div>
                    <ul style="list-style: disc; margin-left: 18px; font-size: 13px; color: #334155; line-height: 1.45;">{impact_lines}</ul>
                </div>
                ''')
            if ai.get("uncertainties"):
                unc_lines = "".join(f'<li>{html.escape(str(u))}</li>' for u in ai["uncertainties"])
                blocks.append(f'''
                <div style="margin-bottom: 10px;">
                    <div style="font-size: 12.5px; font-weight: 700; color: #d97706; margin-bottom: 4px;">⚠️ 机制细节与不确定项</div>
                    <ul style="list-style: disc; margin-left: 18px; font-size: 13px; color: #334155; line-height: 1.45;">{unc_lines}</ul>
                </div>
                ''')
            if ai.get("recommendation"):
                rec_text = html.escape(str(ai["recommendation"]))
                blocks.append(f'''
                <div style="background: rgba(57, 197, 187, 0.12); border-left: 3px solid #0d9488; padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 13px; color: #0f766e;">
                    <strong>💡 综合研判：</strong>{rec_text}
                </div>
                ''')

            ai_html = f'''
            <div class="miku-card-section" style="border: 1px solid rgba(13, 148, 136, 0.3); background: #fafefe;">
                <h3 class="section-title" style="color: #0d9488;">✦ AI 智能战术研判与深度推演</h3>
                {"".join(blocks)}
            </div>
            '''

    images = report_data.get("images") or []
    images_html = ""
    if images:
        cards = []
        for img in images:
            img_url = html.escape(str(img.get("url") or ""))
            img_name = html.escape(str(img.get("name") or "Asset"))
            img_type = html.escape(str(img.get("type") or "贴花/图标"))
            cards.append(f'''
            <div class="miku-image-card">
                <div class="miku-img-box"><img src="{img_url}" alt="{img_name}" onerror="this.style.opacity=0.4" /></div>
                <div class="image-caption">{img_name}</div>
                <span class="miku-badge miku-pill">{img_type}</span>
            </div>
            ''')
        images_html = f'''
        <div class="miku-card-section" style="margin-top: 18px;">
            <h3 class="section-title" style="text-align: center;">关联贴花与美术资产 ({len(images)})</h3>
            <div class="image-grid" style="display: flex; flex-wrap: wrap; justify-content: center; gap: 12px;">{"".join(cards)}</div>
        </div>
        '''

    usage = report_data.get("token_usage") or {}
    total_tokens = usage.get("total_tokens", 0)
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    elapsed = report_data.get("elapsed_seconds", 0.0)
    model_name = html.escape(str(report_data.get("model") or "默认模型"))

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} (Miku Theme)</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
        background-color: #f1fbfb;
        color: #263844;
        font-family: "Noto Sans SC", "Microsoft YaHei", "PingFang SC", "Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji", sans-serif;
        padding: 0;
        margin: 0;
        display: block;
        width: 100%;
    }}
    body {{
        padding: 16px;
    }}
    #miku-canvas {{
        width: 840px;
        margin: 0 auto;
        box-sizing: border-box;
        height: fit-content;
        background-color: #f1fbfb;
        display: flex;
        flex-direction: column;
        position: relative;
        overflow: hidden;
    }}
    {wm_css}
    .header {{
        background: linear-gradient(135deg, rgba(57, 197, 187, 0.2) 0%, rgba(255, 255, 255, 0.9) 100%);
        border: 1px solid rgba(57, 197, 187, 0.35);
        border-radius: 16px;
        padding: 24px 28px;
        margin-bottom: 20px;
        box-shadow: 0 8px 24px rgba(57, 197, 187, 0.08);
    }}
    .kicker {{
        font-size: 11px;
        font-weight: 800;
        letter-spacing: 2px;
        text-transform: uppercase;
        color: #0d9488;
        margin-bottom: 6px;
    }}
    .title {{
        font-size: 24px;
        font-weight: 800;
        color: #164e63;
        margin-bottom: 12px;
    }}
    .meta-row {{
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
        margin-bottom: 14px;
    }}
    .miku-badge {{
        font-size: 11px;
        font-weight: 700;
        padding: 3px 10px;
        border-radius: 9999px;
    }}
    .miku-tag {{ background: rgba(57, 197, 187, 0.15); color: #0d9488; border: 1px solid rgba(57, 197, 187, 0.3); }}
    .miku-imp {{ background: #f43f5e; color: #ffffff; }}
    .miku-pill {{ background: rgba(57, 197, 187, 0.12); color: #0f766e; }}

    .summary-box {{
        background: #ffffff;
        border-left: 4px solid #39c5bb;
        padding: 14px 18px;
        border-radius: 0 10px 10px 0;
        font-size: 14px;
        color: #334155;
        box-shadow: 0 4px 14px rgba(57, 197, 187, 0.08);
        line-height: 1.5;
    }}

    .miku-spec-box {{
        background: #ffffff;
        border: 1px solid rgba(57, 197, 187, 0.28);
        border-radius: 12px;
        margin: 18px 0;
        overflow: hidden;
        box-shadow: 0 8px 24px rgba(42, 157, 149, 0.06);
    }}
    .spec-header {{
        background: linear-gradient(90deg, #ecfdf5 0%, #f0fdfa 100%);
        padding: 8px 16px;
        font-size: 11px;
        font-weight: 800;
        color: #0f766e;
        letter-spacing: 1.5px;
        border-bottom: 1px solid rgba(57, 197, 187, 0.2);
        display: flex;
        align-items: center;
        gap: 8px;
    }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #39c5bb; display: inline-block; }}
    .spec-content {{
        padding: 16px;
        font-family: "Consolas", "Courier New", Courier, monospace;
        font-size: 13px;
        color: #1e293b;
        white-space: pre-wrap;
        line-height: 1.55;
        background: #fafefe;
    }}

    .miku-card-section {{
        background: #ffffff;
        border: 1px solid rgba(57, 197, 187, 0.22);
        border-radius: 12px;
        padding: 18px 22px;
        margin-bottom: 16px;
        box-shadow: 0 4px 16px rgba(57, 197, 187, 0.06);
    }}
    .section-title {{
        font-size: 15px;
        font-weight: 800;
        color: #0f766e;
        margin-bottom: 12px;
        border-bottom: 1px solid #f0fdfa;
        padding-bottom: 6px;
    }}
    .update-list {{ list-style: none; }}
    .update-item {{ margin-bottom: 8px; position: relative; padding-left: 16px; font-size: 13.5px; color: #334155; }}
    .update-item::before {{ content: "✿"; position: absolute; left: 0; color: #39c5bb; font-size: 11px; top: 1px; }}
    .sub-list {{ list-style: circle; margin-left: 18px; font-size: 12.5px; color: #64748b; }}

    .miku-image-card {{
        background: #f8fafc;
        border: 1px solid rgba(57, 197, 187, 0.24);
        border-radius: 10px;
        padding: 10px;
        text-align: center;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        max-width: 220px;
    }}
    .miku-img-box {{
        width: 100%;
        display: flex;
        align-items: center;
        justify-content: center;
        margin-bottom: 6px;
    }}
    .miku-img-box img {{
        max-height: 120px;
        max-width: 100%;
        width: auto;
        height: auto;
        object-fit: contain;
        margin: 0 auto;
        display: block;
    }}
    .image-caption {{ font-size: 11px; font-weight: 600; color: #1e293b; word-break: break-all; }}

    .miku-footer-centered {{
        border-top: 1px solid rgba(57, 197, 187, 0.28);
        padding-top: 18px;
        margin-top: 20px;
        text-align: center;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
    }}
    .miku-footer-line {{
        font-size: 12px;
        color: #334155;
        margin-bottom: 4px;
        text-align: center;
    }}
    .miku-footer-line strong {{
        color: #0d9488;
        font-family: "Consolas", monospace;
    }}
    .miku-sub-line {{
        font-size: 11px;
        color: #94a3b8;
        margin-top: 2px;
    }}
</style>
</head>
<body>
<div id="miku-canvas">
    {wm_overlay}
    <div class="header">
        <div class="kicker">Hatsune Miku Theme · War Thunder Datamine</div>
        <div class="title">{title}</div>
        <div class="meta-row">
            <span class="miku-badge miku-imp">重要度: {importance}</span>
            {tag_badges}
        </div>
        <div class="summary-box">
            {summary}
        </div>
    </div>

    {datamine_box_html}
    {"".join(sections_html)}
    {ai_html}
    {images_html}

    <div class="miku-footer-centered">
        <div class="miku-footer-line">使用模型：<strong>{model_name}</strong></div>
        <div class="miku-footer-line">Token 消耗：总计 <strong>{total_tokens:,}</strong> · 输入 <strong>{prompt_tokens:,}</strong> · 输出 <strong>{completion_tokens:,}</strong></div>
        <div class="miku-footer-line miku-sub-line">耗时：{elapsed:.2f}s · Miku Style 39</div>
    </div>
</div>
</body>
</html>'''


def parse_render_scale(scale_val: Any) -> float:
    """解析渲染清晰度档位，转换为 deviceScaleFactor。
    1x -> 1.0 (标准，约 840px 宽，轻量快速)
    1.5x -> 1.5 (高清，约 1260px 宽，默认推荐)
    2x -> 2.0 (超清 Retina，约 1680px 宽，高分锐利)
    3x -> 3.0 (极致 4K，约 2520px 宽，最高画质)
    """
    if isinstance(scale_val, (int, float)):
        return max(1.0, min(4.0, float(scale_val)))
    s = str(scale_val or "").strip().lower()
    mapping = {
        "1x": 1.0, "1.0x": 1.0, "1": 1.0,
        "1.5x": 1.5, "1.5": 1.5,
        "2x": 2.0, "2.0x": 2.0, "2": 2.0,
        "3x": 3.0, "3.0x": 3.0, "3": 3.0,
    }
    return mapping.get(s, 1.5)


async def render_report_to_image(
    report_data: dict[str, Any],
    output_path: str | Path | None = None,
    template: str = TEMPLATE_DISCORD,
    timeout_seconds: float = DEFAULT_RENDER_TIMEOUT_SECONDS,
    enable_ai_analysis: bool | None = None,
    watermark_config: dict[str, Any] | None = None,
    render_scale: str | float | None = None,
) -> bytes | None:
    """将 HTML 报告渲染为高清 PNG 图像。
    自适应内容真实高度，彻底杜绝下方大片空白残留问题。
    支持选择渲染清晰度档位 (1x / 1.5x / 2x / 3x)。
    """
    if enable_ai_analysis is None:
        enable_ai_analysis = bool(report_data.get("enable_ai_analysis", False))
    if watermark_config is None:
        watermark_config = report_data.get("watermark_config")
    scale_factor = parse_render_scale(render_scale or report_data.get("render_scale") or "1.5x")
    html_content = build_html_report(
        report_data,
        template=template,
        enable_ai_analysis=enable_ai_analysis,
        watermark_config=watermark_config,
    )
    is_mobile = str(report_data.get("target") or "").lower() == "mobile" or str(template or "").lower() in ("mobile", "wtm", "shouyou", "3")
    selected = normalize_template_name(template, is_mobile=is_mobile)
    if selected == TEMPLATE_MOBILE:
        target_canvas_id = "mobile-canvas"
    elif selected == TEMPLATE_MIKU:
        target_canvas_id = "miku-canvas"
    else:
        target_canvas_id = "discord-canvas"
    timeout_ms = int(max(5.0, timeout_seconds) * 1000)

    # 1. 优先采用 Selenium (结合系统已有的 chromium-driver，能够精确截取目标容器尺寸，零下部多余留白)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By

        def _run_selenium() -> bytes:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_html = Path(tmpdir) / "report.html"
                tmp_html.write_text(html_content, encoding="utf-8")
                options = Options()
                options.add_argument("--headless=new")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--allow-file-access-from-files")
                options.add_argument("--disable-web-security")
                options.add_argument("--window-size=1200,1200")
                options.add_argument(f"--force-device-scale-factor={scale_factor}")
                driver = webdriver.Chrome(options=options)
                try:
                    driver.get(f"file://{tmp_html}")
                    try:
                        target = driver.find_element(By.ID, target_canvas_id)
                    except Exception:
                        target = driver.find_element(By.TAG_NAME, "body")

                    # 1. 动态自适应视口尺寸：防止超出 1200px 时被 Chromium 视口硬性裁剪
                    req_w = max(1200, int(target.size.get("width", 840) + target.location.get("x", 0) * 2 + 100))
                    req_h = max(1200, int(target.size.get("height", 1200) + target.location.get("y", 0) + 150))
                    driver.set_window_size(req_w, req_h)

                    # 2. 等待外部网络图片与字体加载完毕 (最长等待 6 秒，完成即提前回调)
                    try:
                        driver.execute_async_script("""
                            var callback = arguments[arguments.length - 1];
                            var imgs = Array.from(document.images);
                            if (imgs.length === 0) { callback(); return; }
                            var count = 0;
                            var finished = false;
                            function doneOne() {
                                if (finished) return;
                                count++;
                                if (count >= imgs.length) {
                                    finished = true;
                                    callback();
                                }
                            }
                            setTimeout(function() {
                                if (!finished) { finished = true; callback(); }
                            }, 6000);
                            imgs.forEach(function(img) {
                                if (img.complete) {
                                    doneOne();
                                } else {
                                    img.addEventListener('load', doneOne);
                                    img.addEventListener('error', doneOne);
                                }
                            });
                        """)
                    except Exception:
                        pass

                    # 3. 再次获取自适应高度（图片全部加载后内容高度可能撑开）
                    final_h = max(req_h, int(target.size.get("height", 1200) + target.location.get("y", 0) + 150))
                    if final_h > req_h:
                        driver.set_window_size(req_w, final_h)

                    return target.screenshot_as_png
                finally:
                    driver.quit()

        image_bytes = await asyncio.to_thread(_run_selenium)
        if image_bytes and len(image_bytes) > 200:
            if output_path:
                Path(output_path).write_bytes(image_bytes)
            return image_bytes
    except Exception as exc:
        _logger.debug("Selenium 渲染尝试失败: %s", exc)

    # 2. 备选：尝试 playwright (如果宿主安装)
    try:
        from playwright.async_api import async_playwright

        async def _run_playwright() -> bytes:
            async with async_playwright() as p:
                browser = await p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
                page = await browser.new_page(viewport={"width": 1000, "height": 800}, device_scale_factor=scale_factor)
                page.set_default_timeout(timeout_ms)
                await page.set_content(html_content, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(200)
                try:
                    target = page.locator(f"#{target_canvas_id}")
                    image_bytes = await target.screenshot(type="png", timeout=timeout_ms)
                except Exception:
                    image_bytes = await page.screenshot(full_page=True, type="png", timeout=timeout_ms)
                await browser.close()
                return image_bytes

        image_bytes = await asyncio.wait_for(_run_playwright(), timeout=timeout_seconds + 3.0)
        if image_bytes and len(image_bytes) > 200:
            if output_path:
                Path(output_path).write_bytes(image_bytes)
            return image_bytes
    except Exception as exc:
        _logger.debug("Playwright 渲染跳过或失败: %s", exc)

    # 3. 兜底无头 Chrome / Chromium CLI
    chrome_bin = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser")
    if chrome_bin:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_html = Path(tmpdir) / "report.html"
                tmp_png = Path(tmpdir) / "report.png"
                tmp_html.write_text(html_content, encoding="utf-8")
                cmd = [
                    chrome_bin,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--hide-scrollbars",
                    "--window-size=840,420",
                    f"--screenshot={tmp_png}",
                    str(tmp_html),
                ]
                subprocess.run(cmd, timeout=timeout_seconds, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                if tmp_png.exists():
                    image_bytes = tmp_png.read_bytes()
                    if output_path:
                        Path(output_path).write_bytes(image_bytes)
                    return image_bytes
        except Exception as exc:
            _logger.debug("无头 Chrome CLI 渲染失败: %s", exc)

    return None
