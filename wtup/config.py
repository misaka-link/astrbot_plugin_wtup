from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable


PLUGIN_NAME = "astrbot_plugin_wtup"
PLUGIN_VERSION = "0.1.5"
REPO_OWNER = "gszabi99"
REPO_NAME = "War-Thunder-Datamine"
REPO_FULL_NAME = f"{REPO_OWNER}/{REPO_NAME}"
BRANCH_NAME = "master"
DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_MAX_FILES_PER_REPORT = 150
DEFAULT_MAX_INPUT_TOKENS = Decimal("0.10")
DEFAULT_MAX_INPUT_TOKEN_UNIT = "M"
DEFAULT_FOOTER_NOTE = "[gszabi99/War-Thunder-Datamine](https://github.com/gszabi99/War-Thunder-Datamine)"
DEFAULT_ANALYSIS_PROMPT = (
    "请分析 War Thunder Datamine 的 GitHub commit 更新内容，参考 War Thunder Datamine 更新日志格式，"
    "先整理本次更新条目，再给出 AI 分析。全程使用中文；载具若同时有英文名和中文名，"
    "写作 英文名(中文名)，如载具名称有特殊字符也要保留。请注意改动可能并非全部游戏模式，"
    "此为游戏《战争雷霆》的拆包文件，请你语言风格符合战争雷霆玩家。"
    "必须遵守后续系统给出的 JSON 输出格式要求。"
)
DEFAULT_SUMMARY_PROMPT = (
    "请分析 War Thunder Datamine 的 GitHub commit 更新内容，参考 War Thunder Datamine 更新日志格式，"
    "先整理本次更新条目，再给出 AI 分析。全程使用中文；载具若同时有英文名和中文名，"
    "写作 英文名(中文名) 如载具名称有特殊字符要保留。请注意改动可能并非全部游戏模式，"
    "此为游戏《战争雷霆》的拆包文件，请你语言风格符合战争雷霆玩家。"
    "必须遵守后续系统给出的 JSON 输出格式要求。"
)
DEFAULT_PUSH_APPEND_TEXT_TEMPLATE = (
    "{version_range} 分析完成\n"
    "消耗token:{token_count}\n"
    "耗时{elapsed_duration}\n"
    "分析模型:{analysis_model}\n"
    "总结模型:{summary_model}"
)
DEFAULT_TOOL_CALL_PROMPT = (
    "当当前分片不足以判断关联挂载、完整参数或同名配置时，可以通过 tool_calls 申请补充上下文。"
    "只请求本次 diff 涉及或强相关的文件；优先使用 read_changed_patch、read_changed_file、search_changed_files、list_related_files。"
    "不要请求无关路径，不要为了普通概括请求工具。"
)
DEFAULT_REVIEW_PROMPT = (
    "你是 War Thunder Datamine 更新报告的监督/质检模型。请基于程序提供的点名册覆盖状态和结构化报告做复核，"
    "不要重新创作整篇报告。节能档只检查是否漏编号、JSON 结构、source_ids 保留、是否存在“部分载具/若干装备”等模糊表述、"
    "每条是否有必要的影响说明；质量档还需要检查分类是否合理、参数说明是否清楚、条目是否和对应变更说明匹配。"
    "如需修正文案，质量档只返回按 item_id 定位的 item_revisions，程序会保留原 source_ids 和结构。"
)
REVIEW_MODE_OFF = "off"
REVIEW_MODE_ENERGY = "energy"
REVIEW_MODE_QUALITY = "quality"
REVIEW_MODE_AUTO = "auto"
REVIEW_MODES = {REVIEW_MODE_OFF, REVIEW_MODE_ENERGY, REVIEW_MODE_QUALITY, REVIEW_MODE_AUTO}


@dataclass(frozen=True)
class PluginConfig:
    provider_id: str
    summary_provider_id: str
    timeout_seconds: int
    model_concurrency: int
    enable_streaming_llm_call: bool
    analysis_prompt: str
    summary_prompt: str
    enable_second_pass_analysis: bool
    target_groups: list[str]
    analysis_file_groups: list[str]
    monitor_interval_minutes: int
    github_token: str
    github_max_retry_count: int
    max_files_per_report: int
    max_input_tokens: Decimal
    max_input_token_unit: str
    max_retry_count: int
    enable_push_append_text: bool
    push_append_text_template: str
    restore_default_prompts: bool = False
    enable_review_model: bool = False
    review_mode: str = REVIEW_MODE_QUALITY
    review_provider_id: str = ""
    review_quality_provider_id: str = ""
    review_prompt: str = DEFAULT_REVIEW_PROMPT
    review_rounds: int = 1
    review_energy_batch_size: int = 80
    review_quality_batch_size: int = 25
    review_upgrade_missing_id_threshold: int = 10
    review_upgrade_on_missing_ids: bool = True
    review_upgrade_on_context_failure: bool = True
    enable_model_tool_calls: bool = False
    max_tool_call_rounds: int = 2
    max_tool_calls_per_round: int = 5
    max_tool_result_chars: int = 12000
    tool_call_prompt: str = DEFAULT_TOOL_CALL_PROMPT
    enable_dynamic_context_queue: bool = True
    max_dynamic_context_rounds: int = 1
    max_dynamic_context_requests: int = 8
    max_dynamic_files_per_request: int = 4
    max_dynamic_context_chars: int = 12000
    enable_pre_summary_report: bool = False
    clear_cache_files: bool = False
    terminate_running_task: bool = False
    enable_task_lock: bool = field(default=False, compare=False)
    max_saved_artifacts: int = 5
    footer_note: str = DEFAULT_FOOTER_NOTE
    backup_provider_ids: list[str] = field(default_factory=list)
    admin_targets: list[str] = field(default_factory=list)
    model_error_recorder: Callable[[str, BaseException | str, dict[str, Any]], None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    task_log_recorder: Callable[[str, dict[str, Any]], Any] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    task_termination_checker: Callable[[], bool] | None = field(default=None, compare=False, repr=False)
    task_termination_resetter: Callable[[], None] | None = field(default=None, compare=False, repr=False)
    task_lock_checker: Callable[[], bool] | None = field(default=None, compare=False, repr=False)
    github_cache_dir: Path | None = field(default=None, compare=False, repr=False)

    @property
    def enable_summary_model(self) -> bool:
        return self.enable_second_pass_analysis

    @property
    def max_input_token_limit(self) -> int:
        if self.max_input_tokens <= 0:
            return 0
        multiplier = 1_000_000 if self.max_input_token_unit.upper() == "M" else 1_000
        return int(self.max_input_tokens * multiplier)

    @property
    def max_patch_chars(self) -> int:
        return self.max_input_token_limit

    @property
    def effective_summary_provider_id(self) -> str:
        return self.summary_provider_id or self.provider_id

    @property
    def effective_summary_prompt(self) -> str:
        return self.summary_prompt or self.analysis_prompt

    @property
    def analysis_provider_ids(self) -> list[str]:
        return unique_provider_ids([self.provider_id, *self.backup_provider_ids])

    @property
    def effective_review_provider_id(self) -> str:
        return self.review_provider_id or self.provider_id

    @property
    def effective_review_quality_provider_id(self) -> str:
        return self.review_quality_provider_id or self.review_provider_id or self.provider_id

    def provider_fallback_ids(self, provider_id: str | None = None) -> list[str]:
        primary_provider_id = self.provider_id if provider_id is None else str(provider_id or "").strip()
        return unique_provider_ids([primary_provider_id, *self.backup_provider_ids])


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
        return getattr(config, key, default)


def as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def as_decimal(value: Any, default: str | int | Decimal, *, minimum: Decimal | None = None) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (AttributeError, InvalidOperation, ValueError):
        parsed = Decimal(str(default))
    parsed = parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enable", "enabled", "开启", "是"}:
        return True
    if text in {"0", "false", "no", "off", "disable", "disabled", "关闭", "否"}:
        return False
    return default


def normalize_token_unit(value: Any) -> str:
    text = str(value or "").strip().upper()
    return "M" if text == "M" else "K"


def normalize_review_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "关闭": REVIEW_MODE_OFF,
        "关": REVIEW_MODE_OFF,
        "off": REVIEW_MODE_OFF,
        "节能": REVIEW_MODE_ENERGY,
        "节能档": REVIEW_MODE_ENERGY,
        "energy": REVIEW_MODE_ENERGY,
        "质量": REVIEW_MODE_QUALITY,
        "质量档": REVIEW_MODE_QUALITY,
        "quality": REVIEW_MODE_QUALITY,
        "自动": REVIEW_MODE_AUTO,
        "自动档": REVIEW_MODE_AUTO,
        "auto": REVIEW_MODE_AUTO,
    }
    normalized = aliases.get(text, text)
    return normalized if normalized in REVIEW_MODES else REVIEW_MODE_AUTO


def split_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        candidates = value
    else:
        text = str(value or "")
        candidates = text.replace(",", "\n").splitlines()
    return [str(item).strip() for item in candidates if str(item or "").strip()]


def unique_provider_ids(provider_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for provider_id in provider_ids:
        text = str(provider_id or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def load_config(config: Any) -> PluginConfig:
    max_input_raw = config_get(config, "max_input_tokens", None)
    max_input_token_unit = normalize_token_unit(config_get(config, "max_input_token_unit", DEFAULT_MAX_INPUT_TOKEN_UNIT))
    if max_input_raw is None:
        legacy_max_chars = as_int(config_get(config, "max_patch_chars", 0), 0, minimum=0)
        if legacy_max_chars > 0:
            max_input_tokens = as_decimal(Decimal(legacy_max_chars) / Decimal(1000), 0, minimum=Decimal("0"))
            max_input_token_unit = "K"
        else:
            max_input_tokens = DEFAULT_MAX_INPUT_TOKENS
            max_input_token_unit = DEFAULT_MAX_INPUT_TOKEN_UNIT
    else:
        max_input_tokens = as_decimal(max_input_raw, DEFAULT_MAX_INPUT_TOKENS, minimum=Decimal("0"))
    return PluginConfig(
        provider_id=str(config_get(config, "provider_id", "") or "").strip(),
        summary_provider_id=str(config_get(config, "summary_provider_id", "") or "").strip(),
        timeout_seconds=as_int(config_get(config, "timeout_seconds", 120), 120, minimum=1),
        model_concurrency=as_int(config_get(config, "model_concurrency", 1), 1, minimum=1),
        enable_streaming_llm_call=as_bool(config_get(config, "enable_streaming_llm_call", False)),
        analysis_prompt=str(config_get(config, "analysis_prompt", DEFAULT_ANALYSIS_PROMPT) or DEFAULT_ANALYSIS_PROMPT),
        summary_prompt=str(config_get(config, "summary_prompt", DEFAULT_SUMMARY_PROMPT) or DEFAULT_SUMMARY_PROMPT),
        enable_second_pass_analysis=as_bool(
            config_get(config, "enable_summary_model", config_get(config, "enable_second_pass_analysis", False))
        ),
        enable_pre_summary_report=as_bool(config_get(config, "enable_pre_summary_report", False)),
        footer_note=str(config_get(config, "footer_note", DEFAULT_FOOTER_NOTE) or DEFAULT_FOOTER_NOTE),
        target_groups=split_lines(config_get(config, "target_groups", "")),
        admin_targets=split_lines(config_get(config, "admin_targets", "")),
        analysis_file_groups=split_lines(config_get(config, "analysis_file_groups", "")),
        monitor_interval_minutes=as_int(
            config_get(config, "monitor_interval_minutes", DEFAULT_INTERVAL_MINUTES),
            DEFAULT_INTERVAL_MINUTES,
            minimum=1,
        ),
        github_token=str(config_get(config, "github_token", "") or "").strip(),
        github_max_retry_count=as_int(config_get(config, "github_max_retry_count", 4), 4, minimum=0),
        max_files_per_report=as_int(
            config_get(config, "max_files_per_report", DEFAULT_MAX_FILES_PER_REPORT),
            DEFAULT_MAX_FILES_PER_REPORT,
            minimum=0,
        ),
        max_input_tokens=max_input_tokens,
        max_input_token_unit=max_input_token_unit,
        max_retry_count=as_int(config_get(config, "max_retry_count", 2), 2, minimum=0),
        enable_model_tool_calls=as_bool(config_get(config, "enable_model_tool_calls", False)),
        max_tool_call_rounds=as_int(config_get(config, "max_tool_call_rounds", 2), 2, minimum=0),
        max_tool_calls_per_round=as_int(config_get(config, "max_tool_calls_per_round", 5), 5, minimum=1),
        max_tool_result_chars=as_int(config_get(config, "max_tool_result_chars", 12000), 12000, minimum=1000),
        tool_call_prompt=str(
            config_get(config, "tool_call_prompt", DEFAULT_TOOL_CALL_PROMPT)
            or DEFAULT_TOOL_CALL_PROMPT
        ),
        enable_dynamic_context_queue=as_bool(config_get(config, "enable_dynamic_context_queue", True), True),
        max_dynamic_context_rounds=as_int(config_get(config, "max_dynamic_context_rounds", 1), 1, minimum=0),
        max_dynamic_context_requests=as_int(config_get(config, "max_dynamic_context_requests", 8), 8, minimum=0),
        max_dynamic_files_per_request=as_int(config_get(config, "max_dynamic_files_per_request", 4), 4, minimum=1),
        max_dynamic_context_chars=as_int(config_get(config, "max_dynamic_context_chars", 12000), 12000, minimum=0),
        clear_cache_files=as_bool(config_get(config, "clear_cache_files", False)),
        terminate_running_task=as_bool(config_get(config, "terminate_running_task", False)),
        enable_task_lock=as_bool(config_get(config, "enable_task_lock", False)),
        max_saved_artifacts=as_int(config_get(config, "max_saved_artifacts", 5), 5, minimum=0),
        enable_push_append_text=as_bool(config_get(config, "enable_push_append_text", False)),
        push_append_text_template=str(
            config_get(config, "push_append_text_template", DEFAULT_PUSH_APPEND_TEXT_TEMPLATE)
            or DEFAULT_PUSH_APPEND_TEXT_TEMPLATE
        ),
        restore_default_prompts=as_bool(config_get(config, "restore_default_prompts", False)),
        enable_review_model=as_bool(config_get(config, "enable_review_model", False)),
        review_mode=normalize_review_mode(config_get(config, "review_mode", REVIEW_MODE_QUALITY)),
        review_provider_id=str(config_get(config, "review_provider_id", "") or "").strip(),
        review_quality_provider_id=str(config_get(config, "review_quality_provider_id", "") or "").strip(),
        review_prompt=str(config_get(config, "review_prompt", DEFAULT_REVIEW_PROMPT) or DEFAULT_REVIEW_PROMPT),
        review_rounds=as_int(config_get(config, "review_rounds", 1), 1, minimum=0),
        review_energy_batch_size=as_int(config_get(config, "review_energy_batch_size", 80), 80, minimum=1),
        review_quality_batch_size=as_int(config_get(config, "review_quality_batch_size", 25), 25, minimum=1),
        review_upgrade_missing_id_threshold=as_int(
            config_get(config, "review_upgrade_missing_id_threshold", 10),
            10,
            minimum=0,
        ),
        review_upgrade_on_missing_ids=as_bool(config_get(config, "review_upgrade_on_missing_ids", True), True),
        review_upgrade_on_context_failure=as_bool(
            config_get(config, "review_upgrade_on_context_failure", True),
            True,
        ),
        backup_provider_ids=unique_provider_ids(
            [
                str(config_get(config, "backup_provider_id_1", "") or "").strip(),
                str(config_get(config, "backup_provider_id_2", "") or "").strip(),
            ]
        ),
    )
