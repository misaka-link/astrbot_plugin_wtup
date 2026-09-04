from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    """Token 消耗统计。"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: object) -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    @property
    def is_empty(self) -> bool:
        return self.prompt_tokens <= 0 and self.completion_tokens <= 0 and self.total_tokens <= 0

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class ChunkAnalysis:
    """单个 diff 分片的模型分析结果。"""
    chunk_index: int
    chunk_total: int
    analysis: dict[str, Any]
    error: str = ""
    raw_text: str = ""
    token_usage: TokenUsage = TokenUsage()


@dataclass(frozen=True)
class DiffChunk:
    """单个 diff 分片信息。"""
    index: int
    total: int
    files: list[dict[str, Any]]
    patch_chars: int


@dataclass(frozen=True)
class DiffSummary:
    """一次更新所有文件的 diff 汇总信息。"""
    base_sha: str
    head_sha: str
    compare_url: str
    total_commits: int
    total_files: int
    additions: int
    deletions: int
    changed_files: int
    commits: list[dict[str, Any]]
    files: list[dict[str, Any]]
    chunks: list[DiffChunk]
    base_version: str = ""
    head_version: str = ""


@dataclass
class AnalysisResult:
    """最终分析输出结果。"""
    report_title: str
    summary: str
    importance: str
    update_sections: list[dict[str, Any]]
    bulk_repeat_content: dict[str, list[str]]
    suspected_hallucinations: list[str]
    ai_analysis: dict[str, Any]
    highlights: list[str]
    player_impact: list[str]
    risks: list[str]
    recommendation: str
    tags: list[str]
    token_usage: TokenUsage
    coverage: dict[str, Any] = field(default_factory=dict)
    raw_analysis: dict[str, Any] = field(default_factory=dict)
    images: list[dict[str, str]] = field(default_factory=list)
    model: str = ""
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_title": self.report_title,
            "summary": self.summary,
            "importance": self.importance,
            "update_sections": self.update_sections,
            "bulk_repeat_content": self.bulk_repeat_content,
            "suspected_hallucinations": self.suspected_hallucinations,
            "ai_analysis": self.ai_analysis,
            "highlights": self.highlights,
            "player_impact": self.player_impact,
            "risks": self.risks,
            "recommendation": self.recommendation,
            "tags": self.tags,
            "token_usage": self.token_usage.to_dict(),
            "coverage": self.coverage,
            "images": self.images,
            "model": self.model,
            "elapsed_seconds": self.elapsed_seconds,
        }

    def to_markdown(self) -> str:
        """生成排版良好的 Markdown 文本报告。"""
        # 如果包含纯净的 gszabi99 datamine 树状文本，拼接居中底部
        datamine_text = self.raw_analysis.get("datamine_text")
        usage = self.token_usage
        model_str = self.model or "默认模型"
        centered_footer = f"""

<div align="center">

---
**使用模型**：`{model_str}`  
**Token 消耗**：总计 {usage.total_tokens} · 输入 {usage.prompt_tokens} · 输出 {usage.completion_tokens}

</div>"""

        if datamine_text:
            res_text = datamine_text
            if self.images:
                img_lines = ["\n\n### 关联图片与贴花资产 / Associated Images:"]
                for img in self.images:
                    name = img.get("name") or "Image"
                    url = img.get("url") or ""
                    img_lines.append(f"- ![{name}]({url})")
                res_text += "\n".join(img_lines)
            return res_text + centered_footer

        lines: list[str] = []
        title = self.report_title or "War Thunder Datamine 更新分析报告"
        lines.append(f"# {title}")
        lines.append("")
        if self.tags:
            lines.append(f"**标签**: {' · '.join(self.tags)}")
            lines.append("")
        lines.append(f"**重要程度**: {self.importance}")
        lines.append("")
        lines.append(f"**更新概述**:\n{self.summary}")
        lines.append("")

        if self.highlights:
            lines.append("## 核心亮点")
            for item in self.highlights:
                lines.append(f"- {item}")
            lines.append("")

        if self.update_sections:
            lines.append("## 更新详情")
            for sec in self.update_sections:
                sec_title = sec.get("title") or "其他更新"
                items = sec.get("items") or []
                lines.append(f"### {sec_title}")
                for it in items:
                    if isinstance(it, dict):
                        text = it.get("text")
                        if text:
                            lines.append(f"- {text}")
                            for child in it.get("children") or []:
                                child_text = child.get("text") if isinstance(child, dict) else str(child)
                                if child_text:
                                    lines.append(f"  * {child_text}")
                        else:
                            name = it.get("name") or it.get("title") or ""
                            detail = it.get("detail") or it.get("description") or ""
                            imp = it.get("importance")
                            imp_str = f" [{imp}]" if imp else ""
                            lines.append(f"- **{name}**{imp_str}: {detail}")
                    else:
                        lines.append(f"- {it}")
                lines.append("")

        bulk = self.bulk_repeat_content or {}
        batch = bulk.get("batch") or []
        repeated = bulk.get("repeated") or []
        needs_ver = bulk.get("needs_verification") or []
        if batch or repeated or needs_ver:
            lines.append("## 批量与高频改动")
            def _render_bulk_item(item: Any) -> str:
                if isinstance(item, dict):
                    return str(item.get("text") or item.get("title") or "")
                return str(item or "")

            if batch:
                lines.append("### 批量修改")
                for it in batch:
                    text = _render_bulk_item(it)
                    if text:
                        lines.append(f"- {text}")
            if repeated:
                lines.append("### 重复内容")
                for it in repeated:
                    text = _render_bulk_item(it)
                    if text:
                        lines.append(f"- {text}")
            if needs_ver:
                lines.append("### 需验证内容")
                for it in needs_ver:
                    text = _render_bulk_item(it)
                    if text:
                        lines.append(f"- {text}")
            lines.append("")

        ai = self.ai_analysis or {}
        player_impact = ai.get("player_impact") or self.player_impact
        uncertainties = ai.get("uncertainties") or self.risks
        rec = ai.get("recommendation") or self.recommendation

        if player_impact or uncertainties or rec:
            lines.append("## AI 分析与战术建议")
            if player_impact:
                lines.append("### 玩家影响")
                for it in player_impact:
                    lines.append(f"- {it}")
            if uncertainties:
                lines.append("### 不确定性与潜在风险")
                for it in uncertainties:
                    lines.append(f"- {it}")
            if rec:
                lines.append(f"### 综合评价与建议\n{rec}")
            lines.append("")

        lines.append(centered_footer)
        return "\n".join(lines)
