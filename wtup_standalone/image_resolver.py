from __future__ import annotations

import hashlib
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_logger = logging.getLogger("wtup_standalone.image_resolver")

DATAMINE_RAW_BASE = "https://raw.githubusercontent.com/gszabi99/War-Thunder-Datamine/master"
IMGCHEST_RE = re.compile(r"https?://(?:cdn\.)?imgchest\.com/files/[a-zA-Z0-9_\-]+\.(?:png|jpg|jpeg|webp)", re.I)


class WarThunderImageResolver:
    """战争雷霆拆包资产图片解析器与本地缓存器。

    支持类型：
    1. 外部图床链接: 如 gszabi99 在公告中附带的 imgchest.com 渲染截图 (真实存在)
    2. Diff 中真实入库的图片文件: 如新增的 .png / .svg / .webp 等
    (注: 贴花仓已停更，拆包主仓无贴图与奖杯资源，坚决不盲猜拼接虚假 404 图片链接)
    """

    @staticmethod
    def resolve_decal_url(decal_name: str) -> str | None:
        """解析贴花图片的 GitHub 原始直链 (已废弃停用: 原 War-Thunder-Decals 仓库长期未维护)。"""
        return None

    @staticmethod
    def resolve_trophy_icon_url(icon_name: str) -> str | None:
        """解析奖杯/宝箱图标直链 (已废弃: 拆包主仓不包含事件奖杯图片，避免 404)。"""
        return None

    @staticmethod
    def resolve_loading_screen_url(bg_name: str) -> str | None:
        """解析加载背景图直链 (已废弃: 拆包主仓不包含加载背景图，避免 404)。"""
        return None

    @classmethod
    def find_images_in_facts(cls, facts: Any, raw_diff_or_text: str = "") -> list[dict[str, str]]:
        """从抽取的事实对象或原始 Diff 文本中自动提取真实可访问的原图信息。"""
        found: list[dict[str, str]] = []
        seen_urls: set[str] = set()

        def _add(img_type: str, name: str, url: str | None):
            if not url or url in seen_urls:
                return
            seen_urls.add(url)
            found.append({
                "type": img_type,
                "name": name,
                "url": url,
            })

        if raw_diff_or_text:
            # 1. 嗅探真实的 imgchest.com 外链渲染截图
            for match in IMGCHEST_RE.finditer(raw_diff_or_text):
                img_url = match.group(0)
                _add("screenshot", "实装截图/预览图", img_url)

            # 2. 从原始 diff 中识别实际新增或修改的真实图片文件
            for line in raw_diff_or_text.splitlines():
                line_str = line.strip()
                if line_str.startswith("diff --git a/") and (
                    line_str.endswith(".png") or line_str.endswith(".svg") or line_str.endswith(".webp")
                ):
                    parts = line_str.split()
                    if len(parts) >= 4:
                        file_path = parts[3].lstrip("b/").strip()
                        file_name = Path(file_path).name
                        raw_url = f"{DATAMINE_RAW_BASE}/{file_path}"
                        _add("asset", file_name, raw_url)

        return found
        return found

    @staticmethod
    def download_and_cache_image(url: str, cache_dir: Path, timeout: float = 10.0) -> Path | None:
        """下载图片并持久化缓存在本地 cache_dir，返回本地绝对路径。"""
        if not url:
            return None
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        suffix = ".png"
        if ".jpg" in url.lower() or ".jpeg" in url.lower():
            suffix = ".jpg"
        elif ".webp" in url.lower():
            suffix = ".webp"
        
        target_path = cache_dir / f"{url_hash}{suffix}"
        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 wtup_standalone"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if data:
                    target_path.write_bytes(data)
                    return target_path
        except Exception as exc:
            _logger.debug("下载图片缓存失败: %s (%s)", url, exc)
            return None
        return None
