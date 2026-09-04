from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

REPO_OWNER = "gszabi99"
REPO_NAME = "War-Thunder-Datamine"
REPO_FULL_NAME = f"{REPO_OWNER}/{REPO_NAME}"
BRANCH_NAME = "master"

DEFAULT_ANALYSIS_PROMPT = (
    "请分析 War Thunder Datamine 的 GitHub commit 更新内容，参考 War Thunder Datamine 更新日志格式，"
    "先整理本次更新条目，再给出 AI 分析。全程使用中文；载具若同时有英文名和中文名，"
    "写作 英文名(中文名)，如载具名称有特殊字符也要保留。请注意改动可能并非全部游戏模式，"
    "此为游戏《战争雷霆》的拆包文件，请你语言风格符合战争雷霆玩家。"
    "遇到大规模同类机械修改、重复或高度相似内容、批量但含义不明确需要验证的内容时，"
    "不要塞进主要更新条目，必须剔出到 bulk_repeat_content 的 batch、repeated 或 needs_verification 中，"
    "并保留对应 source_ids。多个文件或对象修改同一内容时，合并成一条“对象 A、对象 B 修改了同一内容”的摘要，"
    "不要逐条铺开，并保留全部 source_ids。"
    "必须遵守后续系统给出的 JSON 输出格式要求。"
)

DEFAULT_SUMMARY_PROMPT = (
    "请分析 War Thunder Datamine 的 GitHub commit 更新内容，参考 War Thunder Datamine 更新日志格式，"
    "先整理本次更新条目，再给出 AI 分析。全程使用中文；载具若同时有英文名和中文名，"
    "写作 英文名(中文名) 如载具名称有特殊字符要保留。请注意改动可能并非全部游戏模式，"
    "此为游戏《战争雷霆》的拆包文件，请你语言风格符合战争雷霆玩家。"
    "整理最终报告时要将批量重复内容从主要更新条目中剔出，分别放入 bulk_repeat_content 的 "
    "batch、repeated 或 needs_verification，并保留对应 source_ids。多个文件或对象修改同一内容时，"
    "合并成一条“对象 A、对象 B 修改了同一内容”的摘要，不要逐条铺开，并保留全部 source_ids。"
    "必须遵守后续系统给出的 JSON 输出格式要求。"
)

DEFAULT_REVIEW_PROMPT = (
    "你是 War Thunder Datamine 更新报告的监督/质检模型。请基于程序提供的点名册覆盖状态和结构化报告做复核，"
    "不要重新创作整篇报告。节能档只检查是否漏编号、JSON 结构、source_ids 保留、是否存在“部分载具/若干装备”等模糊表述、"
    "每条是否有必要的影响说明；质量档还需要检查分类是否合理、参数说明是否清楚、条目是否和对应变更说明匹配。"
    "如需修正文案，质量档只返回按 item_id 定位的 item_revisions，程序会保留原 source_ids 和结构；"
    "如发现缺少 source_ids 或来源编号不在 expected_source_ids 中的疑似幻觉条目，质量档可通过 filtered_items 按 item_id 标记过滤。"
)

DEFAULT_TOOL_CALL_PROMPT = (
    "当当前分片不足以判断关联挂载、完整参数或同名配置时，可以通过 tool_calls 申请补充上下文。"
    "只请求本次 diff 涉及或强相关的文件；优先使用 read_changed_patch、read_changed_file、search_changed_files、list_related_files。"
    "不要请求无关路径，不要为了普通概括请求工具。"
)

REVIEW_MODE_OFF = "off"
REVIEW_MODE_ENERGY = "energy"
REVIEW_MODE_QUALITY = "quality"
REVIEW_MODE_AUTO = "auto"
REVIEW_MODES = {REVIEW_MODE_OFF, REVIEW_MODE_ENERGY, REVIEW_MODE_QUALITY, REVIEW_MODE_AUTO}

STRUCT_DIFF_SCOPE_MISSING = "missing"
STRUCT_DIFF_SCOPE_ALL = "all"
STRUCT_DIFF_SCOPES = {STRUCT_DIFF_SCOPE_MISSING, STRUCT_DIFF_SCOPE_ALL}


@dataclass
class AnalyzerConfig:
    """独立分析器配置。"""
    # 模型 API 配置
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    backup_models: list[str] = field(default_factory=list)
    timeout_seconds: float = 120.0
    enable_streaming_llm_call: bool = True

    # 提示词与分析流程
    report_style: str = "datamine"  # "datamine" (gszabi99 纯净技术树模式) 或 "summary" (传统概括模式)
    render_template: str = "discord"  # "discord" (1. Discord黑暗风格，默认) 或 "miku" (2. 初音未来风格)
    render_scale: str = "1.5x"  # "1x" (标准), "1.5x" (高清推荐), "2x" (超清 2K), "3x" (极致 4K)
    target_game: str = "pc"  # "pc" 或 "mobile"
    analysis_prompt: str = DEFAULT_ANALYSIS_PROMPT
    summary_prompt: str = DEFAULT_SUMMARY_PROMPT
    review_prompt: str = DEFAULT_REVIEW_PROMPT
    tool_call_prompt: str = DEFAULT_TOOL_CALL_PROMPT

    # 监督模型与复核
    review_mode: str = REVIEW_MODE_AUTO
    review_model: str = ""  # 留空则使用 model
    review_energy_batch_size: int = 80
    review_quality_batch_size: int = 25

    # 思考链推理与温度
    thinking_mode: str = "off"  # off, low, medium, high, custom
    thinking_budget_tokens: int = 0
    temperature: float = 0.2

    # 总结模型
    summary_model: str = ""  # 留空则使用 model

    # Token 与分片预算
    max_input_tokens: int = 100_000
    max_files_per_chunk: int = 50
    max_chars_per_chunk: int = 60_000

    # 上下文与工具调用
    enable_model_tool_calls: bool = True
    enable_dynamic_context_queue: bool = True
    max_tool_call_rounds: int = 5
    max_tool_calls_per_round: int = 5

    # 是否启用 AI 战术研判与深度推演 (默认关闭，节省 Token 并加快分析)
    enable_ai_analysis: bool = False

    # 水印设置 (默认关闭，可自定义文本、透明度、字号、密度)
    watermark_enabled: bool = False
    watermark_text: str = "War Thunder Datamine"
    watermark_opacity: float = 0.12
    watermark_size: int = 18
    watermark_density: str = "medium"

    # 结构化对比
    enable_struct_diff: bool = True
    struct_diff_scope: str = STRUCT_DIFF_SCOPE_MISSING
    struct_diff_max_files: int = 60
    struct_diff_max_chars_per_file: int = 6000
    wt_ext_cli_path: Path | None = None

    # GitHub API 配置
    github_token: str | None = None
    repo_full_name: str = REPO_FULL_NAME
    branch: str = BRANCH_NAME

    # 缓存与日志
    cache_dir: Path = field(default_factory=lambda: Path(os.environ.get("WTUP_CACHE_DIR", ".cache/wtup_standalone")))
    task_log_recorder: Callable[[str, dict[str, Any]], Any] | None = None

    # 终止信号 (可选)
    terminate_checker: Callable[[], bool] | None = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> "AnalyzerConfig":
        """从环境变量加载默认配置。"""
        api_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("LLM_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1") or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("OPENAI_MODEL", "") or os.environ.get("MODEL", "gpt-4o-mini")
        github_token = os.environ.get("GITHUB_TOKEN") or None

        backup_models_str = os.environ.get("BACKUP_MODELS", "")
        backup_models = [m.strip() for m in backup_models_str.split(",") if m.strip()]

        config_kwargs = {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "backup_models": backup_models,
            "github_token": github_token,
        }
        config_kwargs.update(kwargs)
        return cls(**config_kwargs)

    @property
    def provider_id(self) -> str:
        return self.model

    @property
    def backup_provider_ids(self) -> list[str]:
        return self.backup_models

    @property
    def effective_summary_prompt(self) -> str:
        return self.summary_prompt or self.analysis_prompt

    @property
    def summary_provider_id(self) -> str:
        return self.summary_model or self.model

    @property
    def effective_summary_provider_id(self) -> str:
        return self.summary_provider_id or self.provider_id

    @property
    def review_provider_id(self) -> str:
        return self.review_model or self.model

    @property
    def effective_review_provider_id(self) -> str:
        return self.review_provider_id or self.provider_id

    @property
    def effective_review_quality_provider_id(self) -> str:
        return self.review_provider_id or self.provider_id

    @property
    def enable_review_model(self) -> bool:
        return self.review_mode != REVIEW_MODE_OFF

    @property
    def max_input_token_count(self) -> int:
        return self.max_input_tokens
