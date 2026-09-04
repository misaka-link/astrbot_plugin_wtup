from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    "model": os.environ.get("MODEL", os.environ.get("OPENAI_MODEL", "deepseek-chat")),
    "backup_models": [m.strip() for m in os.environ.get("BACKUP_MODELS", "").split(",") if m.strip()],
    "summary_model": "",
    "review_model": "",
    "review_mode": "auto",
    "thinking_mode": "off",  # off, low, medium, high, custom
    "thinking_budget_tokens": 0,
    "temperature": 0.2,
    "github_repo": os.environ.get("GITHUB_REPO", "gszabi99/War-Thunder-Datamine"),
    "github_branch": os.environ.get("GITHUB_BRANCH", "master"),
    "github_token": os.environ.get("GITHUB_TOKEN", ""),
    "schedule_interval_minutes": int(os.environ.get("CHECK_INTERVAL_MINUTES", 15)),
    "schedule_enabled": True,
    "render_template": os.environ.get("RENDER_TEMPLATE", "discord"),  # discord (默认) 或 miku
    "enable_struct_diff": True,
    "enable_model_tool_calls": True,
    "max_tool_call_rounds": int(os.environ.get("MAX_TOOL_CALL_ROUNDS", 5)),
    "max_files_per_chunk": 50,
    "max_chars_per_chunk": 60000,
    "max_history_reports": int(os.environ.get("MAX_HISTORY_REPORTS", 15)),
    "enable_ai_analysis": False,  # 默认关闭 AI 战术研判与深度推演
    # 报告水印设置 (默认关，支持密度/透明度/大小/文字)
    "watermark_enabled": False,
    "watermark_text": "War Thunder Datamine",
    "watermark_opacity": 0.12,
    "watermark_size": 18,
    "watermark_density": "medium",

    # 图片渲染分辨率清晰度档位 (1x, 1.5x, 2x, 3x)
    "render_scale": "1.5x",

    # 手游独立配置 (gszabi99/War-Thunder-Mobile-Datamine)
    "github_mobile_repo": os.environ.get("GITHUB_MOBILE_REPO", "gszabi99/War-Thunder-Mobile-Datamine"),
    "github_mobile_branch": os.environ.get("GITHUB_MOBILE_BRANCH", "master"),
    "mobile_schedule_interval_minutes": int(os.environ.get("MOBILE_CHECK_INTERVAL_MINUTES", 15)),
    "mobile_schedule_enabled": False,
    "mobile_watermark_enabled": False,
    "mobile_watermark_text": "War Thunder Mobile Datamine",
    "mobile_watermark_opacity": 0.12,
    "mobile_watermark_size": 18,
    "mobile_watermark_density": "medium",
}


class DataStore:
    """管理数据持久化：系统设置、历史分析报告、任务状态。"""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        if data_dir is None:
            data_dir = os.environ.get("DATA_DIR", "data")
        self.data_dir = Path(data_dir).resolve()
        self.reports_dir = self.data_dir / "reports"
        self.images_dir = self.data_dir / "images"
        self.config_path = self.data_dir / "config.json"
        self.state_path = self.data_dir / "state.json"
        self.state_mobile_path = self.data_dir / "state_mobile.json"
        self.history_index_path = self.data_dir / "history_index.json"

        self._init_dirs()

    def _init_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)

    def load_config(self) -> dict[str, Any]:
        """读取系统设置（若不存在则以默认值初始化）。"""
        if not self.config_path.exists():
            self.save_config(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(saved)
            return merged
        except Exception:
            return dict(DEFAULT_CONFIG)

    def save_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """保存系统设置。"""
        current = self.load_config() if self.config_path.exists() else dict(DEFAULT_CONFIG)
        current.update(config)
        tmp_file = self.config_path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        tmp_file.replace(self.config_path)
        # 如果更新了 max_history_reports，立即修剪超出上限的旧历史报告
        if "max_history_reports" in config:
            try:
                self.prune_history(int(config["max_history_reports"]))
            except Exception:
                pass
        return current

    def get_masked_config(self) -> dict[str, Any]:
        """返回带有脱敏 API Key 的配置供前端展示。"""
        cfg = self.load_config()
        key = cfg.get("openai_api_key") or ""
        if key:
            if len(key) > 8:
                cfg["openai_api_key_masked"] = f"{key[:3]}...{key[-4:]}"
            else:
                cfg["openai_api_key_masked"] = "***"
        else:
            cfg["openai_api_key_masked"] = ""
        # 移除明文 key
        cfg["has_api_key"] = bool(key)
        return cfg

    def load_state(self, target: str = "pc") -> dict[str, Any]:
        """读取运行状态 (区分端游 pc 与手游 mobile)。"""
        target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
        path = self.state_mobile_path if target_norm == "mobile" else self.state_path
        if not path.exists():
            return {
                "target": target_norm,
                "last_checked_commit": None,
                "last_checked_at": None,
                "last_check_status": None,
                "latest_report_id": None,
            }
        try:
            with open(path, "r", encoding="utf-8") as f:
                res = json.load(f)
                res.setdefault("target", target_norm)
                return res
        except Exception:
            return {"target": target_norm}

    def update_state(self, target: str = "pc", **kwargs: Any) -> dict[str, Any]:
        """更新运行状态 (区分端游 pc 与手游 mobile)。"""
        target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
        path = self.state_mobile_path if target_norm == "mobile" else self.state_path
        state = self.load_state(target=target_norm)
        state.update(kwargs)
        state["target"] = target_norm
        tmp_file = path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp_file.replace(path)
        return state

    def save_report(
        self,
        report_data: dict[str, Any],
        *,
        trigger_mode: str = "manual",
        commit_base: str = "",
        commit_head: str = "",
        image_bytes: bytes | None = None,
        target: str = "pc",
    ) -> str:
        """保存一份分析报告，同时更新索引。区分端游 pc 与手游 mobile。"""
        now = datetime.now()
        seq = getattr(self, "_report_seq", 0) + 1
        self._report_seq = seq
        target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
        report_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{seq:03d}"

        image_filename = None
        if image_bytes:
            image_filename = f"{report_id}.png"
            image_path = self.images_dir / image_filename
            image_path.write_bytes(image_bytes)

        report_record = {
            "id": report_id,
            "target": target_norm,
            "created_at": now.isoformat(),
            "report_title": report_data.get("report_title") or ("War Thunder Mobile 更新报告" if target_norm == "mobile" else "War Thunder Datamine 更新报告"),
            "version_range": f"{commit_base[:7]}...{commit_head[:7]}" if commit_base or commit_head else "",
            "summary": report_data.get("summary") or "",
            "importance": report_data.get("importance") or "中",
            "tags": report_data.get("tags") or [],
            "token_usage": report_data.get("token_usage") or {},
            "elapsed_seconds": report_data.get("elapsed_seconds") or 0.0,
            "commit_base": commit_base,
            "commit_head": commit_head,
            "trigger_mode": trigger_mode,
            "has_image": image_filename is not None,
            "image_filename": image_filename,
            "data": report_data,
        }

        report_file = self.reports_dir / f"{report_id}.json"
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report_record, f, ensure_ascii=False, indent=2)

        # 更新历史索引
        index = self.list_history_index(target=None)
        summary_entry = {
            "id": report_id,
            "target": target_norm,
            "created_at": report_record["created_at"],
            "report_title": report_record["report_title"],
            "summary": report_record["summary"],
            "importance": report_record["importance"],
            "tags": report_record["tags"],
            "token_usage": report_record["token_usage"],
            "elapsed_seconds": report_record["elapsed_seconds"],
            "commit_base": commit_base,
            "commit_head": commit_head,
            "trigger_mode": trigger_mode,
            "has_image": report_record["has_image"],
        }
        index.insert(0, summary_entry)
        # 依据配置的 max_history_reports (默认 15) 限制历史数量并清理超量文件
        cfg = self.load_config()
        max_history = max(1, int(cfg.get("max_history_reports", 15)))
        if len(index) > max_history:
            to_remove = index[max_history:]
            for old_item in to_remove:
                old_id = old_item.get("id")
                if old_id:
                    self._delete_report_files(old_id)
            index = index[:max_history]

        with open(self.history_index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

        # 更新对应端游/手游的最新报告 ID
        self.update_state(target=target_norm, latest_report_id=report_id)
        return report_id

    def _delete_report_files(self, report_id: str) -> None:
        """从磁盘删除报告的 json 文件及图片文件。"""
        report_file = self.reports_dir / f"{report_id}.json"
        if report_file.exists():
            try:
                report_file.unlink()
            except Exception:
                pass

        img_file = self.images_dir / f"{report_id}.png"
        if img_file.exists():
            try:
                img_file.unlink()
            except Exception:
                pass

    def delete_report(self, report_id: str) -> bool:
        """删除指定 ID 的历史报告（包括 json 报告文件、图片及索引中的条目）。"""
        index = self.list_history_index()
        new_index = [item for item in index if item.get("id") != report_id]
        deleted = len(new_index) < len(index)

        self._delete_report_files(report_id)

        with open(self.history_index_path, "w", encoding="utf-8") as f:
            json.dump(new_index, f, ensure_ascii=False, indent=2)

        state = self.load_state()
        if state.get("latest_report_id") == report_id:
            next_latest_id = new_index[0]["id"] if new_index else None
            self.update_state(latest_report_id=next_latest_id)

        return deleted

    def prune_history(self, max_count: int | None = None) -> int:
        """根据上限修剪超量的历史报告，清理磁盘并返回删除的数量。"""
        if max_count is None:
            cfg = self.load_config()
            max_count = max(1, int(cfg.get("max_history_reports", 15)))
        index = self.list_history_index()
        if len(index) <= max_count:
            return 0
        to_prune = index[max_count:]
        kept = index[:max_count]
        pruned_count = 0
        for item in to_prune:
            r_id = item.get("id")
            if r_id:
                self._delete_report_files(r_id)
                pruned_count += 1
        with open(self.history_index_path, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False, indent=2)

        state = self.load_state()
        curr_latest = state.get("latest_report_id")
        if curr_latest and not any(it.get("id") == curr_latest for it in kept):
            self.update_state(latest_report_id=kept[0]["id"] if kept else None)
        return pruned_count

    def list_history_index(self, target: str | None = None) -> list[dict[str, Any]]:
        """获取所有历史报告列表摘要，支持按端游 pc 与手游 mobile 筛选。"""
        if not self.history_index_path.exists():
            return []
        try:
            with open(self.history_index_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            if target == "mobile":
                return [x for x in items if x.get("target") == "mobile"]
            elif target == "pc":
                return [x for x in items if x.get("target") in ("pc", None, "")]
            return items
        except Exception:
            return []

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        """获取某份完整报告详情。"""
        report_file = self.reports_dir / f"{report_id}.json"
        if not report_file.exists():
            return None
        try:
            with open(report_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def get_latest_report(self, target: str = "pc") -> dict[str, Any] | None:
        """获取最新一次生成的报告 (严格区分端游 pc 与手游 mobile，杜绝交叉串线)。"""
        target_norm = "mobile" if str(target).lower() == "mobile" else "pc"
        state = self.load_state(target=target_norm)
        latest_id = state.get("latest_report_id")
        if latest_id:
            rep = self.get_report(latest_id)
            if rep and str(rep.get("target") or "pc").lower() == target_norm:
                return rep

        # 回退从对应通道的索引中获取匹配报告
        index = self.list_history_index(target=target_norm)
        for item in index:
            r_id = item.get("id")
            if r_id:
                rep = self.get_report(r_id)
                if rep and str(rep.get("target") or "pc").lower() == target_norm:
                    return rep
        return None

    def get_image_path(self, filename: str) -> Path | None:
        """获取报告图片路径。"""
        path = self.images_dir / filename
        if path.exists() and path.is_file():
            return path
        return None
