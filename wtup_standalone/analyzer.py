from __future__ import annotations

import asyncio
import json
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
    normalize_report_title,
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

        summary = await asyncio.to_thread(
            build_diff_summary,
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

        # 获取 compare 数据 (异步线程拉取，杜绝主事件循环阻塞)
        compare_res = await asyncio.to_thread(
            self.github_cache.get_compare,
            target_repo,
            base_sha,
            head_sha,
            lambda: self.github_client.get_compare(target_repo, base_sha, head_sha),
        )
        compare_payload = compare_res.value

        # 获取 diff 文本 (异步线程拉取，杜绝大文本读取卡死)
        diff_res = await asyncio.to_thread(
            self.github_cache.get_diff,
            target_repo,
            base_sha,
            head_sha,
            lambda: self.github_client.get_compare_diff(target_repo, base_sha, head_sha),
        )
        raw_diff_text = diff_res.value

        summary = await asyncio.to_thread(
            build_diff_summary,
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

        datamine_text = ""
        resolved_images = []

        # 如果开启了 datamine 纯净树状模式，优先进行高精度确定性特征提取与图片嗅探 (放入后台线程执行，避免阻塞主循环)
        if getattr(self.config, "report_style", "datamine") == "datamine":
            def _extract_datamine_sync():
                raw_diff = "\n".join(
                    f"diff --git a/{f.get('filename')} b/{f.get('filename')}\n{f.get('patch') or ''}"
                    for f in summary.files
                )
                from .wt_extractor import DatamineFactExtractor
                from .datamine_formatter import format_datamine_report
                from .image_resolver import WarThunderImageResolver
                extractor = DatamineFactExtractor()
                facts = extractor.extract_facts(raw_diff, summary)
                datamine_text = format_datamine_report(facts, bilingual=True)
                resolved_images = WarThunderImageResolver.find_images_in_facts(facts, raw_diff)
                return facts, datamine_text, resolved_images

            facts, datamine_text, resolved_images = await asyncio.to_thread(_extract_datamine_sync)

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

            enable_ai = bool(getattr(self.config, "enable_ai_analysis", False))
            has_api_key = bool(getattr(self.config, "api_key", ""))

            # 纯净模式：若未开启 AI 战术研判或无 API Key，直接由高精特征抽取生成完整报告（零 LLM、秒级完成、绝不卡死 CPU）
            if not enable_ai or not has_api_key:
                vehicle_count = len(facts.new_vehicles)
                tags = ["拆包更新", "数据挖掘"]
                if vehicle_count > 0:
                    tags.append("新增载具")
                if facts.loadout_changes:
                    tags.append("挂载调整")
                if facts.mechanics_changes:
                    tags.append("机制改动")

                title = facts.version_range or normalize_report_title(summary, "") or "War Thunder Datamine"
                summary_text = f"版本更新 {title}"
                if vehicle_count > 0:
                    summary_text += f"（包含 {vehicle_count} 项新载具数据）"

                analysis = {
                    "report_title": title,
                    "summary": summary_text,
                    "importance": "高" if vehicle_count > 0 else "中",
                    "update_sections": [],
                    "tags": tags,
                    "datamine_text": datamine_text,
                    "images": resolved_images,
                }
                return self._build_result(analysis, TokenUsage(), started_at=started_at)

            # 若开启 AI 战术研判且已配置有效模型 Key，基于已提取的技术树摘要生成单次专业研判 (避免千次切片重载)
            ai_analysis, ai_summary, usage = await self._generate_ai_analysis_from_datamine(facts, datamine_text)
            vehicle_count = len(facts.new_vehicles)
            tags = ["拆包更新", "数据挖掘"]
            if vehicle_count > 0:
                tags.append("新增载具")
            if facts.loadout_changes:
                tags.append("挂载调整")
            if facts.mechanics_changes:
                tags.append("机制改动")

            title = facts.version_range or normalize_report_title(summary, "") or "War Thunder Datamine"
            summary_text = ai_summary or f"版本更新 {title}"
            if vehicle_count > 0 and not ai_summary:
                summary_text += f"（包含 {vehicle_count} 项新载具数据）"

            analysis = {
                "report_title": title,
                "summary": summary_text,
                "importance": "高" if vehicle_count > 0 else "中",
                "update_sections": [],
                "tags": tags,
                "ai_analysis": ai_analysis,
                "datamine_text": datamine_text,
                "images": resolved_images,
            }
            return self._build_result(analysis, usage, started_at=started_at)

        # 1. 细分大 CSV (异步线程执行，避免阻塞主循环)
        await asyncio.to_thread(subdivide_summary_csv_files, self.config, summary)

        # 2. 按 token 上限智能切片 (异步线程执行)
        split_summary = await asyncio.to_thread(split_chunks_by_token_limit, self.config, summary)

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

    async def _generate_ai_analysis_from_datamine(
        self, facts: Any, datamine_text: str
    ) -> tuple[dict[str, Any], str, TokenUsage]:
        """针对 datamine 模式，基于高密度技术树事实生成单次 AI 战术研判与深度推演。"""
        excerpt = datamine_text[:40000] if len(datamine_text) > 40000 else datamine_text
        prompt = (
            "你是一名专业的《战争雷霆》资深拆包分析专家。\n"
            "请根据以下战争雷霆版本更新提取的技术树/载具/武器改动数据，进行客观且专业的玩家战术研判与深度推演。\n\n"
            "输出要求：\n"
            "1. 只能输出严格合法的单个 JSON 对象，禁止 Markdown 代码块包裹，禁止任何前言或后记。\n"
            "2. JSON 字段结构如下：\n"
            "{\n"
            '  "summary": "一两句话概括本次主要改动",\n'
            '  "changed_content": ["主要改动点1", "主要改动点2"],\n'
            '  "player_impact": ["对玩家研发、经济收益、分房权重(BR)或对局战术的具体影响1"],\n'
            '  "uncertainties": ["暂不明确或需要游戏内实测的内容（若无则为空数组）"],\n'
            '  "recommendation": "给战争雷霆玩家的核心战术或研发建议"\n'
            "}\n\n"
            f"以下是拆包技术树改动数据：\n{excerpt}"
        )
        try:
            from .retry import request_chat_completion_with_retry
            sem = asyncio.Semaphore(1)
            raw_text, usage = await request_chat_completion_with_retry(
                self.llm_context,
                self.config,
                prompt,
                sem,
                provider_id=self.config.model,
                purpose="datamine_ai_summary",
            )
            clean_text = str(raw_text or "").strip()
            if clean_text.startswith("```"):
                lines = clean_text.splitlines()
                clean_text = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])
            data = json.loads(clean_text)
            ai_analysis = {
                "changed_content": data.get("changed_content") if isinstance(data.get("changed_content"), list) else [],
                "player_impact": data.get("player_impact") if isinstance(data.get("player_impact"), list) else [],
                "uncertainties": data.get("uncertainties") if isinstance(data.get("uncertainties"), list) else [],
                "recommendation": str(data.get("recommendation") or ""),
            }
            summary_desc = str(data.get("summary") or "")
            return ai_analysis, summary_desc, usage
        except Exception as exc:
            _logger.warning("基于 datamine 文本生成 AI 战术研判失败: %s", exc)
            return {}, "", TokenUsage()

    def _build_result(self, analysis: dict[str, Any], token_usage: TokenUsage | None, *, started_at: float) -> AnalysisResult:
        ai_analysis = analysis.get("ai_analysis") or {}
        elapsed = time.monotonic() - started_at
        usage = token_usage if token_usage is not None else TokenUsage()

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
            token_usage=usage,
            coverage=analysis.get("coverage") or {},
            raw_analysis=analysis,
            images=analysis.get("images") or [],
            model=str(analysis.get("model") or self.config.model or "默认模型"),
            elapsed_seconds=elapsed,
        )
