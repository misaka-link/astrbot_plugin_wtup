from __future__ import annotations

import json
from typing import Any

from ..config import PluginConfig
from ..diff_collector import DiffChunk, DiffSummary, render_chunk_input
from .merge import order_chunk_results
from .models import ChunkAnalysis
from .normalize import normalize_analysis


def _analysis_prompt_text(settings: PluginConfig) -> str:
    return str(settings.analysis_prompt or "").strip()

def _summary_prompt_text(settings: PluginConfig) -> str:
    return str(settings.effective_summary_prompt or "").strip()

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
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结这些变更中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {{
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {{
          "text": "条目内容",
          "children": [
            {{"text": "子条目内容", "children": []}}
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
2. 输出内容格式参考 War Thunder Datamine 更新日志：先按条目列出更新内容，再在下面列出 AI 分析的改动内容。
3. update_sections 使用中文标题，例如“新增载具”“新增文本”“武器调整”“经济调整”“其他变化”。
4. update_sections.items 支持多级 children；要像更新日志一样保留层级，不要把所有内容压成一段。
5. 如果出现载具名，且能从 diff 或文本中判断英文名和中文名，必须写成 英文名(中文名)，例如 JH-7A(飞豹)。无法判断中文名时只写原名，不要编造。
6. 优先解释数据变化可能代表什么，不要只复述文件名。
7. 如果信息不足，要明确写“不确定”，不要编造。
8. changed_content、player_impact、uncertainties 每个数组最多 5 条，每条尽量短。
9. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加 Part、分片、说明文字或其他内容。
10. summary 会显示在标题下面的小字行，可以写描述性标题；不要把描述性标题写进 report_title。
11. 不要在 report_title、summary、update_sections.title 或正文条目中写 Part、分片、第几批等分页信息。
12. 当前是内部模型请求第 {chunk.index}/{chunk.total} 批；该信息只用于你理解输入范围，最终报告会由程序合并，不要输出批次信息。

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
{{
  "report_title": "更新标题，必须是 版本号->版本号，例如 2.56.0.38->2.56.0.39；无法判断版本号时留空",
  "summary": "一句话总结这些变更中最重要的变化",
  "importance": "低/中/高",
  "update_sections": [
    {{
      "title": "新增载具/新增文本/参数调整/经济调整/其他变化",
      "items": [
        {{
          "text": "条目内容",
          "children": [
            {{"text": "子条目内容", "children": []}}
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
2. 只能使用上次模型原始输出和文件列表中已有的信息，不要新增未经输入支持的内容。
3. 如果上次输出无法判断实际改动，生成需复核条目，并把原因写入 uncertainties。
4. 不要在 report_title、summary、update_sections.title 或正文条目中写 Part、分片、第几批等分页信息。

提交范围: {summary.base_sha[:7] or "unknown"}...{summary.head_sha[:7] or "unknown"}
当前分片: {chunk.index}/{chunk.total}
当前分片文件:
{files}

上次模型原始输出:
{str(raw_text or "").strip()[:8000]}
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
          "children": [
            {{"text": "子条目内容", "children": []}}
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
6. 不要在 report_title、summary、update_sections.title 或正文条目中写 Part、分片、第几批等分页信息。
7. changed_content、player_impact、uncertainties 每个数组最多 5 条，每条尽量短。
8. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加其他说明文字。
9. 如果初步合并 JSON 中有分析失败或信息不足的内容，要保留到 uncertainties。

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
          "children": [
            {{"text": "子条目内容", "children": []}}
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
6. 不要在 report_title、summary、update_sections.title 或正文条目中写 Part、分片、第几批等分页信息。
7. changed_content、player_impact、uncertainties 每个数组最多 5 条，每条尽量短。
8. report_title 只能写版本号到版本号，例如 2.56.0.38->2.56.0.39，不要添加其他说明文字。
9. 程序合并失败原因和分片分析失败信息必须保留到 uncertainties。

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
