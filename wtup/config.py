from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PLUGIN_NAME = "astrbot_plugin_wtup"
PLUGIN_VERSION = "0.1.0"
REPO_OWNER = "gszabi99"
REPO_NAME = "War-Thunder-Datamine"
REPO_FULL_NAME = f"{REPO_OWNER}/{REPO_NAME}"
BRANCH_NAME = "master"
DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_ANALYSIS_PROMPT = (
    "请分析 War Thunder Datamine 的 GitHub commit 更新内容，提炼游戏数据变化、"
    "可能影响、重要程度和玩家需要关注的内容。"
)


@dataclass(frozen=True)
class PluginConfig:
    provider_id: str
    timeout_seconds: int
    analysis_prompt: str
    target_groups: list[str]
    monitor_interval_minutes: int
    github_token: str
    max_files_per_report: int
    max_patch_chars: int


def config_get(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:
                return default
        except Exception:
            return default

    try:
        return config[key]
    except Exception:
        return default


def as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def split_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        candidates = value
    else:
        text = str(value or "")
        candidates = text.replace(",", "\n").splitlines()
    return [str(item).strip() for item in candidates if str(item or "").strip()]


def load_config(config: Any) -> PluginConfig:
    return PluginConfig(
        provider_id=str(config_get(config, "provider_id", "") or "").strip(),
        timeout_seconds=as_int(config_get(config, "timeout_seconds", 120), 120, minimum=1),
        analysis_prompt=str(config_get(config, "analysis_prompt", DEFAULT_ANALYSIS_PROMPT) or DEFAULT_ANALYSIS_PROMPT),
        target_groups=split_lines(config_get(config, "target_groups", "")),
        monitor_interval_minutes=as_int(
            config_get(config, "monitor_interval_minutes", DEFAULT_INTERVAL_MINUTES),
            DEFAULT_INTERVAL_MINUTES,
            minimum=1,
        ),
        github_token=str(config_get(config, "github_token", "") or "").strip(),
        max_files_per_report=as_int(config_get(config, "max_files_per_report", 0), 0, minimum=0),
        max_patch_chars=as_int(config_get(config, "max_patch_chars", 0), 0, minimum=0),
    )
