from __future__ import annotations

import json
from typing import Any

from ..config import PluginConfig
from ..diff_collector import DiffChunk, DiffSummary, render_chunk_input
from .coverage import render_change_manifest
from .merge import order_chunk_results
from .models import ChunkAnalysis
from .normalize import normalize_analysis


def _analysis_prompt_text(settings: PluginConfig) -> str:
    return str(settings.analysis_prompt or "").strip()

def _summary_prompt_text(settings: PluginConfig) -> str:
    return str(settings.effective_summary_prompt or "").strip()

def _tool_protocol_text(settings: PluginConfig, *, remaining_rounds: int | None = None) -> str:
    if not getattr(settings, "enable_model_tool_calls", False):
        return ""

    remaining_text = ""
    if remaining_rounds is not None:
        remaining_text = f"\n本分片剩余工具调用轮数: {max(0, remaining_rounds)}"
    custom_prompt = str(getattr(settings, "tool_call_prompt", "") or "").strip()
    return f"""

模型工具调用协议：
{custom_prompt}
如果当前上下文不足以完成判断，可以在最终 JSON 顶层额外输出 tool_calls 数组；插件会校验后代为执行，并把结果放入下一轮补充上下文分析。
支持的工具：
- read_changed_patch: 读取本次 diff 中某个文件的 patch。需要 path。
- read_changed_file: 读取目标提交下某个文件的完整文本；如果本地 diff 索引没有完整内容，插件允许额外从 GitHub 拉取。需要 path。
- search_changed_files: 在本次变更文件名和 patch 中搜索关键词。需要 query。
- list_related_files: 按 path 或 query 列出本次 diff 中可能相关的变更文件。
tool_calls 示例：
[
  {{
    "tool": "read_changed_file",
    "path": "aces.vromfs.bin_u/gamedata/flightmodels/f_14d.blkx",
    "query": "",
    "reason": "需要确认挂载配置的完整上下文"
  }}
]
如果不需要补充上下文，请省略 tool_calls 或输出空数组。不要为了普通概括请求工具。{remaining_text}
""".rstrip()

def _dynamic_context_protocol_text(settings: PluginConfig, *, remaining_rounds: int | None = None) -> str:
    if not getattr(settings, "enable_dynamic_context_queue", True):
        return ""

    remaining_text = ""
    if remaining_rounds is not None:
        remaining_text = f"\n本次分析剩余动态补充轮数: {max(0, remaining_rounds)}"
    return f"""

动态补充请求协议：
如果你因为缺少其他文件而无法确定某项改动，不要只写 uncertainties。
请同时在 JSON 顶层输出 context_requests，明确哪个文件的判断缺少哪些文件支持；插件会校验后把 source_file 和 missing_files 放入下一轮模型请求继续分析。
context_requests 示例：
[
  {{
    "source_file": "aces.vromfs.bin_u/gamedata/flightmodels/f_14d.blkx",
    "missing_files": ["aces.vromfs.bin_u/gamedata/flightmodels/f_14d_late.blkx"],
    "reason": "需要对比同系列飞行模型才能确认参数差异影响",
    "priority": "高"
  }}
]
要求：
- source_file 必须是当前判断信息不足的文件路径。
- missing_files 只填写需要一起分析的强相关文件路径，不要填写目录、通配符或模糊描述。
- 只有能被补充文件实际解决的问题才写 context_requests；需要实测、官方确认、经济公式或游戏内显示验证的问题只写入 uncertainties。
- reason 写清楚缺这些文件会影响哪个判断。
- priority 只能写 高/中/低。
- 如果不需要动态补充，请输出空数组或省略 context_requests。{remaining_text}
""".rstrip()

def _analysis_json_schema_text() -> str:
    return """
{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结这些变更中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {
          "text": "条目内容",
          "source_ids": ["覆盖的变更编号，例如 C001-001"],
          "children": [
            {"text": "子条目内容", "source_ids": ["C001-002"], "children": []}
          ]
        }
      ]
    }
  ],
  "ai_analysis": {
    "changed_content": ["AI 分析出的实际改动内容"],
    "player_impact": ["对玩家、载具、经济、任务、地图或战斗体验的可能影响"],
    "uncertainties": ["不确定点、需要继续观察的地方"],
    "recommendation": "是否建议玩家关注/更新，以及原因"
  },
  "tags": ["标签1", "标签2"],
  "tool_calls": [],
  "context_requests": [
    {
      "source_file": "信息不足的源文件路径",
      "missing_files": ["需要补充分析的文件路径"],
      "reason": "缺少这些文件导致哪个判断无法确定",
      "priority": "高/中/低"
    }
  ],
  "resolved_uncertainties": ["本轮已经解决的不确定点原文"]
}
""".strip()

def build_prompt(settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk) -> str:
    return f"""
{_analysis_prompt_text(settings)}

你正在分析固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{_analysis_json_schema_text()}

要求：
1. 用中文。
2. 输出内容格式参考 War Thunder Datamine 更新日志：先按条目列出更新内容，再在下面列出 AI 分析的改动内容。
3. update_sections 使用中文标题，例如“新增载具”“新增文本”“武器调整”“经济调整”“其他变化”。
4. update_sections.items 支持多级 children；要像更新日志一样保留层级，不要把所有内容压成一段。
5. 如果出现载具名，且能从 diff 或文本中判断英文名和中文名，必须写成 英文名(中文名)，例如 JH-7A(飞豹)。无法判断中文名时只写原名，不要编造。
6. 优先解释数据变化可能代表什么，不要只复述文件名。
7. 如果信息不足，要明确写“不确定”，不要编造。
8. update_sections 是完整更新条目，不受 AI 分析摘要数量限制；changed_content、player_impact、uncertainties 每个数组最多 50 条，每条尽量短。
9. 下面的“变更覆盖点名册”是必须覆盖的原始变更编号；每个编号都必须出现在某条 update_sections.items 或 children 的 source_ids 中。
10. 一个报告条目可以覆盖多个 source_ids，但不得编造点名册不存在的编号；如果某个编号无法解释，也要写成需复核条目并带上该 source_ids。
11. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加 Part、分片、说明文字或其他内容。
12. summary 会显示在标题下面的小字行，可以写描述性标题；不要把描述性标题写进 report_title。
13. 不要在 report_title、summary、update_sections.title、正文条目或 uncertainties 中写 Part、分片、第几批、本批 diff、当前分片等分页信息。
14. 如果上下文不足，只能写“本次 diff 未提供足够信息”，不要写“本批 diff 未出现/当前分片未出现”。
15. 当前是内部模型请求第 {chunk.index}/{chunk.total} 批；该信息只用于你理解输入范围，最终报告会由程序合并，不要输出批次信息。
{_tool_protocol_text(settings, remaining_rounds=getattr(settings, "max_tool_call_rounds", 0))}
{_dynamic_context_protocol_text(settings, remaining_rounds=getattr(settings, "max_dynamic_context_rounds", 0))}

变更覆盖点名册：
{render_change_manifest(summary, chunk)}

以下是 GitHub 变更数据：

{render_chunk_input(summary, chunk)}
""".strip()

def build_json_repair_prompt(settings: PluginConfig, summary: DiffSummary, chunk: DiffChunk, raw_text: str) -> str:
    files = "\n".join(f"- {file_info.get('filename') or ''}" for file_info in chunk.files)
    return f"""
{_analysis_prompt_text(settings)}

上一次模型分析返回的内容不是有效 JSON。请基于“上次模型原始输出”和“当前分片文件列表”重新整理为严格 JSON。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{_analysis_json_schema_text()}

要求：
1. 用中文。
2. 只能使用上次模型原始输出和文件列表中已有的信息，不要新增未经输入支持的内容。
3. 如果上次输出无法判断实际改动，生成需复核条目，并把原因写入 uncertainties。
4. 不要在 report_title、summary、update_sections.title、正文条目或 uncertainties 中写 Part、分片、第几批、本批 diff、当前分片等分页信息。
5. 如果上下文不足，只能写“本次 diff 未提供足够信息”，不要写“本批 diff 未出现/当前分片未出现”。

提交范围: {summary.base_sha[:7] or "unknown"}...{summary.head_sha[:7] or "unknown"}
当前分片: {chunk.index}/{chunk.total}
当前分片文件:
{files}

变更覆盖点名册：
{render_change_manifest(summary, chunk)}

上次模型原始输出:
{str(raw_text or "").strip()[:8000]}
""".strip()

def build_dynamic_context_prompt(
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    previous_analysis: dict[str, Any],
    context_request: dict[str, Any],
    *,
    round_index: int,
    remaining_rounds: int,
) -> str:
    previous_json = json.dumps(normalize_analysis(previous_analysis), ensure_ascii=False, indent=2)
    request_json = json.dumps(context_request, ensure_ascii=False, indent=2)
    return f"""
{_analysis_prompt_text(settings)}

你正在继续分析固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新。
上一次分析明确说明某个文件缺少关联文件，插件已经把 source_file 和 missing_files 组合成这一次动态补充请求。
请只围绕这次补充请求修正或补充分析，不要重新概括整次更新。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{_analysis_json_schema_text()}

要求：
1. 用中文。
2. 只能使用上一次分析 JSON、当前动态补充请求和下面提供的补充文件内容。
3. 如果补充文件解决了原来的不确定点，请把已解决的不确定点原文写入 resolved_uncertainties。
4. 只输出这次补充直接新增或修正的条目，不要重复上一次分析已经覆盖的内容。
5. 如果仍缺少其他强相关文件，请继续输出 context_requests；否则输出空数组或省略。
6. 不要在 report_title、summary、update_sections.title、正文条目或 uncertainties 中写 Part、分片、第几批、本批 diff、当前分片等分页信息。
7. 如果上下文仍不足，只能写“本次 diff 未提供足够信息”。
{_dynamic_context_protocol_text(settings, remaining_rounds=remaining_rounds)}

提交范围: {summary.base_sha[:7] or "unknown"}...{summary.head_sha[:7] or "unknown"}
动态补充轮次: {round_index}
当前动态补充请求:
{request_json}

上一次分析 JSON:
{previous_json}

变更覆盖点名册：
{render_change_manifest(summary, chunk)}

补充文件数据:
{render_chunk_input(summary, chunk)}
""".strip()

def build_tool_refinement_prompt(
    settings: PluginConfig,
    summary: DiffSummary,
    chunk: DiffChunk,
    previous_analysis: dict[str, Any],
    tool_results: list[dict[str, Any]],
    *,
    round_index: int,
    remaining_rounds: int,
) -> str:
    previous_json = json.dumps(normalize_analysis(previous_analysis), ensure_ascii=False, indent=2)
    results_json = json.dumps(tool_results, ensure_ascii=False, indent=2)
    files = "\n".join(f"- {file_info.get('filename') or ''}" for file_info in chunk.files)
    return f"""
{_analysis_prompt_text(settings)}

你正在继续分析固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新。
上一次模型输出了 tool_calls，插件已经按规则执行工具，并返回补充上下文。
请基于“上次分析 JSON”和“工具结果”输出修订后的严格 JSON。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

要求：
1. 用中文。
2. 只能使用上次分析 JSON、当前分片文件列表和工具结果中已有的信息。
3. 如果工具结果被拒绝、未找到、截断或 GitHub 拉取失败，要把影响写入 uncertainties。
4. 不要在 report_title、summary、update_sections.title、正文条目或 uncertainties 中写 Part、分片、第几批、本批 diff、当前分片等分页信息。
5. 如果上下文仍不足，只能写“本次 diff 未提供足够信息”。
{_tool_protocol_text(settings, remaining_rounds=remaining_rounds)}
{_dynamic_context_protocol_text(settings, remaining_rounds=getattr(settings, "max_dynamic_context_rounds", 0))}

提交范围: {summary.base_sha[:7] or "unknown"}...{summary.head_sha[:7] or "unknown"}
当前分片: {chunk.index}/{chunk.total}
工具调用轮次: {round_index}
当前分片文件:
{files}

变更覆盖点名册：
{render_change_manifest(summary, chunk)}

上次分析 JSON:
{previous_json}

工具结果:
{results_json}
""".strip()

def build_refinement_prompt(settings: PluginConfig, summary: DiffSummary, merged_analysis: dict[str, Any]) -> str:
    merged_json = json.dumps(normalize_analysis(merged_analysis), ensure_ascii=False, indent=2)
    return f"""
{_summary_prompt_text(settings)}

你正在整理固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新最终报告。

前面已经按 diff 分片完成多次模型分析，程序也已经把分片分析结果初步合并为 JSON。
你的任务不是重新分析原始 diff，而是基于这个初步合并 JSON 做二次整理，生成更适合最终推送的报告。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结本次更新中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {{
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {{
          "text": "条目内容",
          "source_ids": ["覆盖的变更编号，例如 C001-001"],
          "children": [
            {{"text": "子条目内容", "source_ids": ["C001-002"], "children": []}}
          ]
        }}
      ]
    }}
  ],
  "ai_analysis": {{
    "changed_content": ["AI 分析出的实际改动内容"],
    "player_impact": ["对玩家、载具、经济、任务、地图或战斗体验的可能影响"],
    "uncertainties": ["不确定点、需要继续观察的地方"],
    "recommendation": "是否建议玩家关注/更新，以及原因"
  }},
  "tags": ["标签1", "标签2"]
}}

要求：
1. 用中文。
2. 只能使用初步合并 JSON 中已有的信息，不要新增未经输入支持的内容。
3. 去重重复条目，合并含义相近的条目。
4. 保留重要的载具、武器、经济、任务、地图、文本等改动。
5. update_sections 使用中文标题，并保留条目层级。
6. 必须保留并合并 update_sections.items 和 children 中已有的 source_ids，不能删除、改写或编造 source_ids。
7. 不要在 report_title、summary、update_sections.title、正文条目或 uncertainties 中写 Part、分片、第几批、本批 diff、当前分片等分页信息。
8. 如果上下文不足，只能写“本次 diff 未提供足够信息”，不要写“本批 diff 未出现/当前分片未出现”。
9. update_sections 是完整更新条目，不受 AI 分析摘要数量限制；changed_content、player_impact、uncertainties 每个数组最多 50 条，每条尽量短。
10. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加其他说明文字。
11. 如果初步合并 JSON 中有分析失败或信息不足的内容，要保留到 uncertainties。

提交范围: {summary.base_sha[:7] or "unknown"}...{summary.head_sha[:7] or "unknown"}
提交数: {summary.total_commits}
文件数: {summary.total_files}
初步合并 JSON:
{merged_json}
""".strip()

def build_chunk_refinement_prompt(
    settings: PluginConfig,
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    *,
    merge_error: str = "",
) -> str:
    chunk_json = json.dumps(
        build_chunk_refinement_payload(summary, chunks, results, merge_error=merge_error),
        ensure_ascii=False,
        indent=2,
    )
    return f"""
{_summary_prompt_text(settings)}

你正在整理固定仓库 gszabi99/War-Thunder-Datamine 的 commit 更新最终报告。

前面已经按 diff 分片完成多次模型分析，但程序合并分片结果时失败了。
你的任务是直接基于每个分片的原始分析 JSON 做二次整理，生成最终报告。

输出协议：
1. 只能输出一个 JSON object。
2. 不要输出 Markdown 代码块。
3. 不要输出解释、前言、后记。
4. JSON 必须能被 json.loads 直接解析。
5. 所有字符串必须使用双引号。
6. 不允许尾随逗号。
7. 不确定的信息写入 uncertainties，不要编造。

JSON 字段如下：
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结本次更新中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {{
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {{
          "text": "条目内容",
          "source_ids": ["覆盖的变更编号，例如 C001-001"],
          "children": [
            {{"text": "子条目内容", "source_ids": ["C001-002"], "children": []}}
          ]
        }}
      ]
    }}
  ],
  "ai_analysis": {{
    "changed_content": ["AI 分析出的实际改动内容"],
    "player_impact": ["对玩家、载具、经济、任务、地图或战斗体验的可能影响"],
    "uncertainties": ["不确定点、需要继续观察的地方"],
    "recommendation": "是否建议玩家关注/更新，以及原因"
  }},
  "tags": ["标签1", "标签2"]
}}

要求：
1. 用中文。
2. 只能使用分片分析 JSON 和 raw_text 中已有的信息，不要新增未经输入支持的内容。
3. 去重重复条目，合并含义相近的条目。
4. 保留重要的载具、武器、经济、任务、地图、文本等改动。
5. update_sections 使用中文标题，并保留条目层级。
6. 必须保留并合并 update_sections.items 和 children 中已有的 source_ids，不能删除、改写或编造 source_ids。
7. 不要在 report_title、summary、update_sections.title、正文条目或 uncertainties 中写 Part、分片、第几批、本批 diff、当前分片等分页信息。
8. 如果上下文不足，只能写“本次 diff 未提供足够信息”，不要写“本批 diff 未出现/当前分片未出现”。
9. update_sections 是完整更新条目，不受 AI 分析摘要数量限制；changed_content、player_impact、uncertainties 每个数组最多 50 条，每条尽量短。
10. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加其他说明文字。
11. 程序合并失败原因和分片分析失败信息必须保留到 uncertainties。

分片分析数据:
{chunk_json}
""".strip()

def build_chunk_refinement_payload(
    summary: DiffSummary,
    chunks: list[DiffChunk],
    results: list[ChunkAnalysis],
    *,
    merge_error: str = "",
) -> dict[str, Any]:
    chunk_map = {chunk.index: chunk for chunk in chunks}
    ordered_results = order_chunk_results(chunks, results)
    empty_chunk = DiffChunk(index=0, total=0, files=[], patch_chars=0)
    return {
        "commit_range": f"{summary.base_sha[:7] or 'unknown'}...{summary.head_sha[:7] or 'unknown'}",
        "total_commits": summary.total_commits,
        "total_files": summary.total_files,
        "merge_error": str(merge_error or "").strip(),
        "chunks": [
            {
                "chunk_index": result.chunk_index,
                "chunk_total": result.chunk_total,
                "error": result.error,
                "files": [
                    str(file_info.get("filename") or "")
                    for file_info in chunk_map.get(result.chunk_index, empty_chunk).files
                ],
                "analysis": json_safe(result.analysis),
                "raw_text": result.raw_text[:4000],
            }
            for result in ordered_results
        ],
    }

def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
