from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable


PLUGIN_NAME = "astrbot_plugin_wtup"
PLUGIN_VERSION = "0.1.1"
REPO_OWNER = "gszabi99"
REPO_NAME = "War-Thunder-Datamine"
REPO_FULL_NAME = f"{REPO_OWNER}/{REPO_NAME}"
BRANCH_NAME = "master"
DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_FOOTER_NOTE = "[gszabi99/War-Thunder-Datamine](https://github.com/gszabi99/War-Thunder-Datamine)"
DEFAULT_ANALYSIS_PROMPT = """请分析 War Thunder Datamine 的 GitHub commit 更新内容。你是分片分析模型，只负责当前输入中可见的 diff 内容；最终报告会由程序和总结模型合并。

核心目标：完整、准确、可核对。必须逐文件、逐条分析当前输入中的全部可见改动，不得因为改动很小、看似内部调整、纯文本、本地化、重命名、新增/删除文件、格式变化或数值微调而跳过。

更新条目要求：
1. 按 War Thunder Datamine 更新日志风格整理，先列“更新条目”，再给出 AI 分析。
2. 每条改动必须保留关键细节：对象名称、参数名、旧值/新值、新增/删除项、文本键、文件语义或机制变化。
3. 涉及参数时，参数名后要用圆括号补充中文含义，例如 uncageBeforeLaunch (发射前解除陀螺锁定)。无法可靠判断含义时写“不确定”，不要编造。
4. 涉及载具、武器、弹药、挂载、改装件、乘员组或装备时，必须尽量写出完整名称；能判断中英文时使用 英文原名(中文译名)。禁止用“部分载具”“某些装备”代替明确对象。
5. 每条改动都要说明可能影响：会怎样影响游戏内表现、手感、平衡、经济、任务、UI 或玩家理解。若没有明显直接影响，明确写“对游戏表现无明显直接影响”。

分类要求：
1. 使用清晰中文分类，例如“空中载具”“地面载具”“直升机”“武器/弹药”“经济/科技树”“地图/任务”“文本/UI”“其他变化”“水面舰艇”。
2. 无法明确归类的内容放入“其他变化”。
3. 水面舰艇相关内容放在所有分类最后；没有内容的分类省略。

新覆盖清单要求：
1. 必须配合插件后续 JSON 协议输出 analysis_coverage。
2. analysis_coverage 是防遗漏清单，不是正文；当前输入中的每个文件都必须有独立记录，path 必须与输入文件名完全一致。
3. 只有确认该文件所有可见改动都已经写入 update_sections 或 ai_analysis，才能标记 analyzed。
4. 如果只分析了一部分、上下文不足、影响不明确或工具结果不足，标记 uncertain，并在 notes 写明原因。
5. covered_changes 必须列出该文件已经覆盖的改动点；evidence 必须写能回指 diff 的参数名、实体名、文本键、文件片段或数值变化，不能只写“已检查”。

语言与边界：
1. 全程中文，信息优先，语气可以有《战争雷霆》玩家熟悉的轻微吐槽，但不要牺牲清晰度。
2. 不要输出 Markdown 代码块，不要输出 JSON 之外的解释。
3. 不要在最终内容里写“当前分片”“本批 diff”“第几批”等内部拆分语境；统一说“本次 diff”。
4. 不确定的信息写入 uncertainties，不要猜测、不要补充输入中没有的事实。"""
DEFAULT_SUMMARY_PROMPT = """你是最终整理模型，只负责基于已经给出的分片分析 JSON 或程序初步合并 JSON 做去重、合并和排版整理。不要重新分析原始 diff，不要新增输入中没有的事实，不要扩大解释。

你的任务只有这些：
1. 合并重复条目：删除完全重复内容，合并同一对象、同一参数或同一机制的近似表述。
2. 保留独立改动：不同文件、不同对象、不同参数、不同数值、不同文本键或不同影响的内容不能因为看起来相似就删掉。
3. 整理分类顺序：使用清晰中文分类；无法归类放入“其他变化”；水面舰艇相关内容放在最后；空分类省略。
4. 压缩冗余表达：让正文更适合最终推送，减少重复句、分片语境和过长说明，但必须保留关键细节、数值变化、对象名称和影响判断。
5. 整理 AI 分析：changed_content、player_impact、uncertainties 只做归纳去重，不重新发明结论；已有不确定点和分析失败信息必须保留。

analysis_coverage 硬性要求：
1. 必须完整保留输入中已有的 analysis_coverage，不能删除、合并、改名或压缩任何 path。
2. 不要把多个文件合成一个 coverage 项；每个 path 仍然独立存在。
3. 不要把 uncertain/skipped 擅自改成 analyzed；除非输入中已经明确证明该文件所有可见改动都被覆盖。
4. covered_changes、evidence、notes 中已有的信息要保留；可以去重和轻微润色，但不能抹掉证据或复核原因。
5. 如果发现 coverage 中某个文件的独立改动没有出现在正文，应优先把该改动补回 update_sections，而不是删除 coverage。

输出风格：
1. 全程中文，只输出插件要求的 JSON，不要输出 Markdown 代码块、前言或解释。
2. 最终报告要像一次完整更新，不要出现“分片”“本批”“当前批次”“Part”等内部流程词。
3. 如果输入信息不足，只保留“不确定/需复核”的说明；不要编造载具名称、参数含义、游戏影响或版本结论。"""
DEFAULT_PUSH_APPEND_TEXT_TEMPLATE = (
    "{version_range} 分析完成\n"
    "消耗token:{token_count}\n"
    "耗时{elapsed_duration}\n"
    "分析模型:{analysis_model}\n"
    "总结模型:{summary_model}"
)
DEFAULT_TOOL_CALL_PROMPT = (
    "当当前分片不足以判断关联挂载、完整参数、同名配置、实体归属、参数含义，"
    "或 analysis_coverage 缺少可回指 diff 的证据时，可以通过 tool_calls 申请补充上下文。"
    "只请求本次 diff 涉及或强相关的文件；优先使用 read_changed_patch 查看变更片段，"
    "必要时用 read_changed_file 读取目标提交文件全文，用 search_changed_files 搜索参数名/实体名/文本键，"
    "用 list_related_files 查找同名或强关联文件。不要请求无关路径，不要为了普通概括请求工具；"
    "如果工具结果仍不足，必须在 uncertainties 和 analysis_coverage.notes 中标记待复核。"
)


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
    max_files_per_report: int
    max_input_tokens: Decimal
    max_input_token_unit: str
    max_retry_count: int
    enable_push_append_text: bool
    push_append_text_template: str
    enable_model_tool_calls: bool = False
    max_tool_call_rounds: int = 2
    max_tool_calls_per_round: int = 5
    max_tool_result_chars: int = 12000
    tool_call_prompt: str = DEFAULT_TOOL_CALL_PROMPT
    enable_pre_summary_report: bool = False
    clear_cache_files: bool = False
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
    max_input_tokens = as_decimal(max_input_raw, 0, minimum=Decimal("0"))
    max_input_token_unit = normalize_token_unit(config_get(config, "max_input_token_unit", "K"))
    if max_input_tokens <= 0:
        legacy_max_chars = as_int(config_get(config, "max_patch_chars", 0), 0, minimum=0)
        if legacy_max_chars > 0 and max_input_raw is None:
            max_input_tokens = as_decimal(Decimal(legacy_max_chars) / Decimal(1000), 0, minimum=Decimal("0"))
            max_input_token_unit = "K"
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
        max_files_per_report=as_int(config_get(config, "max_files_per_report", 0), 0, minimum=0),
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
        clear_cache_files=as_bool(config_get(config, "clear_cache_files", False)),
        max_saved_artifacts=as_int(config_get(config, "max_saved_artifacts", 5), 5, minimum=0),
        enable_push_append_text=as_bool(config_get(config, "enable_push_append_text", False)),
        push_append_text_template=str(
            config_get(config, "push_append_text_template", DEFAULT_PUSH_APPEND_TEXT_TEMPLATE)
            or DEFAULT_PUSH_APPEND_TEXT_TEMPLATE
        ),
        backup_provider_ids=unique_provider_ids(
            [
                str(config_get(config, "backup_provider_id_1", "") or "").strip(),
                str(config_get(config, "backup_provider_id_2", "") or "").strip(),
            ]
        ),
    )
