from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

from .analysis_cache import AnalysisResultCache
from .config import AnalyzerConfig
from .coverage import enforce_change_coverage
from .csv_subdivide import subdivide_summary_csv_files
from .diff_collector import (
    DiffChunk,
    DiffSummary,
    build_diff_summary,
    parse_unified_diff,
    render_chunk_input,
    short_sha,
)
from .diff_enricher import enrich_summary_with_struct_compare
from .github_cache import GitHubCache
from .github_client import GitHubClient
from .merge import merge_chunk_analyses
from .models import AnalysisResult, ChunkAnalysis, TokenUsage
from .retry import analyze_chunks, refine_merged_analysis_with_usage
from .review import review_analysis_with_usage
from .termination import check_task_termination
from .tokens import split_chunks_by_token_limit

_logger = logging.getLogger("wtup_standalone")


class DatamineAnalyzer:
    """War Thunder Datamine 独立分析器。

    支持通过本地 unified diff 文本/文件、或通过 GitHub 仓库比较进行自动化更新解析。
    内置分片切分、token 预估、多模型降级容灾、结构化 diff 提取、多轮 JSON 修复、
    覆盖率校验和监督复核流水线。
    """

    def __init__(
        self,
        config: AnalyzerConfig | None = None,
        *,
        llm_context: Any = None,
    ) -> None:
        self.config = config or AnalyzerConfig.from_env()
        self.llm_context = llm_context
        self.github_client = GitHubClient(
            token=self.config.github_token or "",
            timeout=int(self.config.timeout_seconds),
        )
        self.github_cache = GitHubCache(self.config.cache_dir / "github_cache")
        self.analysis_cache = AnalysisResultCache(self.config.cache_dir / "analysis_cache")

    async def analyze_diff_file(
        self,
        file_path: str | Path,
        *,
        base_sha: str = "local_base",
        head_sha: str = "local_head",
    ) -> AnalysisResult:
        """读取并分析本地 unified diff 文件。"""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Diff 文件不存在: {path}")
        diff_text = path.read_text(encoding="utf-8", errors="replace")
        return await self.analyze_diff_text(diff_text, base_sha=base_sha, head_sha=head_sha)

    async def analyze_diff_text(
        self,
        diff_text: str,
        *,
        base_sha: str = "local_base",
        head_sha: str = "local_head",
        compare_url: str = "",
    ) -> AnalysisResult:
        """分析给定的 unified diff 文本。"""
        started_at = time.monotonic()

        compare_payload = {
            "base_commit": {"sha": base_sha},
            "sha": head_sha,
            "html_url": compare_url,
            "commits": [{"sha": head_sha, "message": f"update to {head_sha}"}],
            "files": [],
        }

        summary = build_diff_summary(
            compare_payload,
            raw_diff_text=diff_text,
            max_files=self.config.max_files_per_chunk,
            max_chars=self.config.max_chars_per_chunk,
        )

        return await self._run_pipeline(summary, started_at=started_at)

    async def analyze_github_compare(
        self,
        base_sha: str,
        head_sha: str,
        *,
        repo: str = "",
    ) -> AnalysisResult:
        """拉取 GitHub 仓库的两个 commit 比较并执行更新分析。"""
        started_at = time.monotonic()
        target_repo = repo or self.config.repo_full_name

        # 检查分析缓存
        cache_entry = None
        probe_summary = DiffSummary(
            base_sha=base_sha,
            head_sha=head_sha,
            compare_url=f"https://github.com/{target_repo}/compare/{base_sha}...{head_sha}",
            total_commits=0,
            total_files=0,
            additions=0,
            deletions=0,
            changed_files=0,
            commits=[],
            files=[],
            chunks=[],
        )
        cached = self.analysis_cache.read(settings=self.config, repo=target_repo, summary=probe_summary)
        if cached is not None:
            _logger.info("分析结果命中本地缓存: %s...%s", short_sha(base_sha), short_sha(head_sha))
            analysis = cached.merged_analysis or cached.analysis
            return self._build_result(analysis, cached.token_usage, started_at=started_at)

        # 获取 compare 数据
        compare_res = self.github_cache.get_compare(
            target_repo,
            base_sha,
            head_sha,
            lambda: self.github_client.get_compare(target_repo, base_sha, head_sha),
        )
        compare_payload = compare_res.value

        # 获取 diff 文本
        diff_res = self.github_cache.get_diff(
            target_repo,
            base_sha,
            head_sha,
            lambda: self.github_client.get_compare_diff(target_repo, base_sha, head_sha),
        )
        raw_diff_text = diff_res.value

        summary = build_diff_summary(
            compare_payload,
            raw_diff_text=raw_diff_text,
            max_files=self.config.max_files_per_chunk,
            max_chars=self.config.max_chars_per_chunk,
        )

        # 结构化对比丰富化 (如果启用)
        if self.config.enable_struct_diff:
            enrich_summary_with_struct_compare(
                self.config,
                self.github_client,
                self.github_cache,
                summary,
                binary_cache_dir=self.config.cache_dir / "binaries",
            )

        result = await self._run_pipeline(summary, started_at=started_at)

        # 写入缓存
        self.analysis_cache.write(
            settings=self.config,
            repo=target_repo,
            summary=summary,
            analysis=result.raw_analysis,
            merged_analysis=result.raw_analysis,
            token_usage=result.token_usage,
            pre_summary_token_usage=result.token_usage,
            analysis_model_route=[self.config.model],
            summary_model_route=[self.config.summary_model or self.config.model],
        )

        return result

    async def _run_pipeline(self, summary: DiffSummary, *, started_at: float) -> AnalysisResult:
        """核心分析执行链路。"""
        check_task_termination(self.config, "分析流水线启动")

        # 如果开启了 datamine 纯净树状模式，优先进行高精度确定性特征提取与图片嗅探
        datamine_text = ""
        resolved_images = []
        raw_diff = "\n".join(
            f"diff --git a/{f.get('filename')} b/{f.get('filename')}\n{f.get('patch') or ''}"
            for f in summary.files
        )
        if getattr(self.config, "report_style", "datamine") == "datamine":
            from .wt_extractor import DatamineFactExtractor
            from .datamine_formatter import format_datamine_report
            from .image_resolver import WarThunderImageResolver
            extractor = DatamineFactExtractor()
            facts = extractor.extract_facts(raw_diff, summary)
            datamine_text = format_datamine_report(facts, bilingual=True)
            resolved_images = WarThunderImageResolver.find_images_in_facts(facts, raw_diff)

            if facts.is_nothingburger:
                analysis = {
                    "report_title": facts.version_range or "War Thunder Datamine",
                    "summary": ":nothingburger: (无实质游戏内容改动)",
                    "importance": "低",
                    "update_sections": [],
                    "tags": ["无实质改动"],
                    "datamine_text": datamine_text,
                    "images": resolved_images,
                }
                return self._build_result(analysis, TokenUsage(), started_at=started_at)

        # 1. 细分大 CSV
        subdivide_summary_csv_files(self.config, summary)

        # 2. 按 token 上限智能切片
        split_summary = split_chunks_by_token_limit(self.config, summary)

        # 3. 分片调用大模型分析
        chunk_results = await analyze_chunks(
            self.llm_context,
            self.config,
            split_summary,
        )

        total_usage = TokenUsage()
        for c in chunk_results:
            total_usage = total_usage + c.token_usage

        # 4. 合并分片分析
        merged_analysis = merge_chunk_analyses(split_summary, split_summary.chunks, chunk_results)

        # 5. 可选：使用总结模型重构精炼
        final_analysis = merged_analysis
        if len(split_summary.chunks) > 1 and self.config.summary_model:
            refined, refine_usage = await refine_merged_analysis_with_usage(
                self.llm_context,
                self.config,
                split_summary,
                merged_analysis,
            )
            final_analysis = refined
            total_usage = total_usage + refine_usage

        # 6. 覆盖率强化与监督模型复核
        if getattr(self.config, "review_mode", "auto") != "off":
            review_res = await review_analysis_with_usage(
                self.llm_context,
                self.config,
                split_summary,
                final_analysis,
            )
            final_analysis = review_res.analysis
            total_usage = total_usage + review_res.token_usage
        else:
            final_analysis = enforce_change_coverage(split_summary, split_summary.chunks, final_analysis)

        if datamine_text:
            final_analysis["datamine_text"] = datamine_text
        if resolved_images:
            final_analysis["images"] = resolved_images

        return self._build_result(final_analysis, total_usage, started_at=started_at)

    def _build_result(self, analysis: dict[str, Any], token_usage: TokenUsage, *, started_at: float) -> AnalysisResult:
        ai_analysis = analysis.get("ai_analysis") or {}
        elapsed = time.monotonic() - started_at

        return AnalysisResult(
            report_title=str(analysis.get("report_title") or ""),
            summary=str(analysis.get("summary") or ""),
            importance=str(analysis.get("importance") or "中"),
            update_sections=analysis.get("update_sections") or [],
            bulk_repeat_content=analysis.get("bulk_repeat_content") or {},
            suspected_hallucinations=analysis.get("suspected_hallucinations") or [],
            ai_analysis=ai_analysis,
            highlights=analysis.get("highlights") or [],
            player_impact=analysis.get("player_impact") or ai_analysis.get("player_impact") or [],
            risks=analysis.get("risks") or ai_analysis.get("uncertainties") or [],
            recommendation=str(analysis.get("recommendation") or ai_analysis.get("recommendation") or ""),
            tags=analysis.get("tags") or [],
            token_usage=token_usage,
            coverage=analysis.get("coverage") or {},
            raw_analysis=analysis,
            images=analysis.get("images") or [],
            model=str(analysis.get("model") or self.config.model or "默认模型"),
            elapsed_seconds=elapsed,
        )
