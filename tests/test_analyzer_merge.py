from __future__ import annotations

import asyncio
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from wtup.analyzer import (
    ChunkAnalysis,
    TokenUsage,
    analyze_chunks,
    build_chunk_refinement_payload,
    estimate_chunk_input_tokens,
    extract_token_usage,
    llm_failure_reason,
    merge_chunk_analyses,
    refine_chunk_analyses,
    refine_merged_analysis,
    safe_normalize_analysis,
    split_chunks_by_token_limit,
    split_chunk_for_retry,
)
from wtup.config import PluginConfig, load_config
from wtup.diff_collector import DiffChunk, DiffSummary, split_files
from wtup.renderer import report_update_subtitle


def make_summary() -> DiffSummary:
    files = [
        {"filename": "a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
        {"filename": "b.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+b"},
        {"filename": "c.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+c"},
    ]
    chunks = [
        DiffChunk(index=1, total=3, files=[files[0]], patch_chars=2),
        DiffChunk(index=2, total=3, files=[files[1]], patch_chars=2),
        DiffChunk(index=3, total=3, files=[files[2]], patch_chars=2),
    ]
    return DiffSummary(
        base_sha="base123",
        head_sha="head456",
        compare_url="https://example.invalid/compare",
        total_commits=1,
        total_files=len(files),
        additions=3,
        deletions=0,
        changed_files=len(files),
        commits=[],
        files=files,
        chunks=chunks,
    )


def analysis(section_title: str, item_text: str, *, importance: str = "中") -> dict:
    return {
        "report_title": "2.56.0.38->2.56.0.39",
        "summary": item_text,
        "importance": importance,
        "update_sections": [{"title": section_title, "items": [{"text": item_text, "children": []}]}],
        "ai_analysis": {
            "changed_content": [item_text],
            "player_impact": [f"影响 {item_text}"],
            "uncertainties": [],
            "recommendation": f"建议关注 {item_text}",
        },
        "tags": [section_title],
    }


class AnalyzerMergeTest(unittest.TestCase):
    def test_merge_preserves_chunk_order_even_when_results_arrive_out_of_order(self) -> None:
        summary = make_summary()
        results = [
            ChunkAnalysis(3, 3, analysis("武器调整", "第三项")),
            ChunkAnalysis(1, 3, analysis("武器调整", "第一项")),
            ChunkAnalysis(2, 3, analysis("武器调整", "第二项", importance="高")),
        ]

        merged = merge_chunk_analyses(summary, summary.chunks, results)

        self.assertEqual(merged["importance"], "高")
        items = merged["update_sections"][0]["items"]
        self.assertEqual([item["text"] for item in items], ["第一项", "第二项", "第三项"])

    def test_invalid_json_is_normalized_to_safe_analysis(self) -> None:
        parsed = safe_normalize_analysis("这不是 JSON")

        self.assertEqual(parsed["importance"], "中")
        self.assertEqual(parsed["update_sections"][0]["title"], "其他变化")
        self.assertIn("模型输出格式未按 JSON 返回", parsed["ai_analysis"]["uncertainties"][0])

    def test_merge_cleans_pagination_section_titles(self) -> None:
        summary = make_summary()
        results = [
            ChunkAnalysis(1, 3, analysis("Part 1", "第一项")),
            ChunkAnalysis(2, 3, analysis("分片 2", "第二项")),
            ChunkAnalysis(3, 3, analysis("经济调整", "第三项")),
        ]

        merged = merge_chunk_analyses(summary, summary.chunks, results)

        titles = [section["title"] for section in merged["update_sections"]]
        self.assertEqual(titles, ["其他变化", "经济调整"])

    def test_final_report_subtitle_has_no_part_label(self) -> None:
        chunk = DiffChunk(index=1, total=1, files=[], patch_chars=0)
        subtitle = report_update_subtitle(chunk, {"summary": "本次更新包含 1 个提交、3 个文件。"})

        self.assertEqual(subtitle, "本次更新包含 1 个提交、3 个文件。")


class TokenUsageTest(unittest.TestCase):
    def test_extract_token_usage_from_astrbot_usage_object(self) -> None:
        usage = SimpleNamespace(input=12, output=8, total=20)
        response = SimpleNamespace(usage=usage)

        self.assertEqual(extract_token_usage(response), TokenUsage(prompt_tokens=12, completion_tokens=8, total_tokens=20))

    def test_extract_token_usage_from_openai_usage_dict(self) -> None:
        response = {
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 5,
                "total_tokens": 12,
            }
        }

        self.assertEqual(extract_token_usage(response), TokenUsage(prompt_tokens=7, completion_tokens=5, total_tokens=12))

    def test_extract_token_usage_fills_total_when_missing(self) -> None:
        response = {"usage": {"input_tokens": 7, "output_tokens": 5}}

        self.assertEqual(extract_token_usage(response), TokenUsage(prompt_tokens=7, completion_tokens=5, total_tokens=12))


class ConfigTest(unittest.TestCase):
    def test_second_pass_config_defaults_to_disabled(self) -> None:
        settings = load_config({})

        self.assertFalse(settings.enable_summary_model)

    def test_second_pass_config_accepts_bool_like_values(self) -> None:
        settings = load_config({"enable_summary_model": "开启"})

        self.assertTrue(settings.enable_summary_model)

    def test_legacy_second_pass_config_still_works(self) -> None:
        settings = load_config({"enable_second_pass_analysis": "开启"})

        self.assertTrue(settings.enable_summary_model)

    def test_summary_provider_defaults_to_analysis_provider(self) -> None:
        settings = load_config({"provider_id": "main-model"})

        self.assertEqual(settings.effective_summary_provider_id, "main-model")

    def test_token_limit_unit_uses_k_or_m(self) -> None:
        self.assertEqual(load_config({"max_input_tokens": 32, "max_input_token_unit": "K"}).max_input_token_limit, 32000)
        self.assertEqual(load_config({"max_input_tokens": 1, "max_input_token_unit": "M"}).max_input_token_limit, 1000000)

    def test_token_limit_accepts_two_decimal_places(self) -> None:
        k_settings = load_config({"max_input_tokens": "0.25", "max_input_token_unit": "K"})
        m_settings = load_config({"max_input_tokens": 1.5, "max_input_token_unit": "M"})

        self.assertEqual(k_settings.max_input_tokens, Decimal("0.25"))
        self.assertEqual(k_settings.max_input_token_limit, 250)
        self.assertEqual(m_settings.max_input_tokens, Decimal("1.50"))
        self.assertEqual(m_settings.max_input_token_limit, 1500000)

    def test_token_limit_rounds_to_two_decimal_places(self) -> None:
        settings = load_config({"max_input_tokens": "1.235", "max_input_token_unit": "K"})

        self.assertEqual(settings.max_input_tokens, Decimal("1.24"))
        self.assertEqual(settings.max_input_token_limit, 1240)

    def test_legacy_char_limit_is_converted_to_k_tokens(self) -> None:
        settings = load_config({"max_patch_chars": 8000})

        self.assertEqual(settings.max_input_tokens, Decimal("8.00"))
        self.assertEqual(settings.max_input_token_limit, 8000)

    def test_model_concurrency_defaults_to_one(self) -> None:
        settings = load_config({})

        self.assertEqual(settings.model_concurrency, 1)

    def test_model_concurrency_has_minimum_one(self) -> None:
        settings = load_config({"model_concurrency": 0})

        self.assertEqual(settings.model_concurrency, 1)


class FakeContext:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0
        self.kwargs: list[dict] = []

    async def llm_generate(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        return self.response

    def get_provider_by_id(self, provider_id: str):
        return object()


def make_settings(*, model_concurrency: int = 2) -> PluginConfig:
    return PluginConfig(
        provider_id="",
        summary_provider_id="",
        timeout_seconds=5,
        model_concurrency=model_concurrency,
        analysis_prompt="请分析更新。",
        summary_prompt="请总结更新。",
        enable_second_pass_analysis=True,
        target_groups=[],
        analysis_file_groups=[],
        monitor_interval_minutes=30,
        github_token="",
        max_files_per_report=1,
        max_input_tokens=0,
        max_input_token_unit="K",
        max_retry_count=2,
        enable_push_append_text=False,
        push_append_text_template="",
    )


class AnalyzerSecondPassTest(unittest.IsolatedAsyncioTestCase):
    async def test_refine_merged_analysis_uses_valid_model_json(self) -> None:
        summary = make_summary()
        merged = merge_chunk_analyses(
            summary,
            summary.chunks,
            [ChunkAnalysis(1, 3, analysis("武器调整", "第一项"))],
        )
        context = FakeContext(
            """
            {
              "report_title": "2.56.0.38->2.56.0.39",
              "summary": "二次整理摘要",
              "importance": "高",
              "update_sections": [{"title": "武器调整", "items": [{"text": "二次整理条目", "children": []}]}],
              "ai_analysis": {
                "changed_content": ["二次整理条目"],
                "player_impact": ["需要关注"],
                "uncertainties": [],
                "recommendation": "建议关注"
              },
              "tags": ["武器调整"]
            }
            """
        )

        refined = await refine_merged_analysis(context, make_settings(), summary, merged)

        self.assertEqual(context.calls, 1)
        self.assertEqual(refined["summary"], "二次整理摘要")
        self.assertEqual(refined["importance"], "高")
        self.assertIn("请总结更新。", context.kwargs[0]["prompt"])

    async def test_refine_merged_analysis_uses_summary_provider(self) -> None:
        summary = make_summary()
        merged = merge_chunk_analyses(summary, summary.chunks, [ChunkAnalysis(1, 3, analysis("武器调整", "第一项"))])
        context = FakeContext(json_response(analysis("武器调整", "总结")))
        settings = make_settings()
        settings = PluginConfig(
            provider_id="analysis-provider",
            summary_provider_id="summary-provider",
            timeout_seconds=settings.timeout_seconds,
            model_concurrency=settings.model_concurrency,
            analysis_prompt=settings.analysis_prompt,
            summary_prompt=settings.summary_prompt,
            enable_second_pass_analysis=True,
            target_groups=[],
            analysis_file_groups=[],
            monitor_interval_minutes=settings.monitor_interval_minutes,
            github_token="",
            max_files_per_report=1,
            max_input_tokens=0,
            max_input_token_unit="K",
            max_retry_count=2,
            enable_push_append_text=False,
            push_append_text_template="",
        )

        await refine_merged_analysis(context, settings, summary, merged)

        self.assertEqual(context.kwargs[0]["chat_provider_id"], "summary-provider")

    async def test_refine_merged_analysis_falls_back_to_merged_result_on_invalid_json(self) -> None:
        summary = make_summary()
        merged = merge_chunk_analyses(
            summary,
            summary.chunks,
            [ChunkAnalysis(1, 3, analysis("武器调整", "第一项"))],
        )
        context = FakeContext("这不是 JSON")

        refined = await refine_merged_analysis(context, make_settings(), summary, merged)

        self.assertEqual(context.calls, 1)
        self.assertEqual(refined["summary"], merged["summary"])
        self.assertEqual(refined["update_sections"], merged["update_sections"])

    async def test_refine_chunk_analyses_uses_raw_chunk_json_when_merge_failed(self) -> None:
        summary = make_summary()
        results = [
            ChunkAnalysis(1, 3, analysis("武器调整", "第一项"), raw_text='{"summary":"第一项"}'),
            ChunkAnalysis(2, 3, analysis("经济调整", "第二项"), error="分片提醒"),
        ]
        context = FakeContext(
            """
            {
              "report_title": "2.56.0.38->2.56.0.39",
              "summary": "从分片 JSON 二次整理",
              "importance": "中",
              "update_sections": [{"title": "武器调整", "items": [{"text": "第一项", "children": []}]}],
              "ai_analysis": {
                "changed_content": ["第一项"],
                "player_impact": [],
                "uncertainties": ["程序合并失败"],
                "recommendation": "建议复核"
              },
              "tags": ["武器调整"]
            }
            """
        )

        refined = await refine_chunk_analyses(
            context,
            make_settings(),
            summary,
            summary.chunks,
            results,
            merge_error="程序合并失败",
        )

        self.assertEqual(context.calls, 1)
        self.assertEqual(refined["summary"], "从分片 JSON 二次整理")

    async def test_refine_chunk_analyses_falls_back_when_second_pass_fails(self) -> None:
        summary = make_summary()
        context = FakeContext("这不是 JSON")

        refined = await refine_chunk_analyses(
            context,
            make_settings(),
            summary,
            summary.chunks,
            [ChunkAnalysis(1, 3, analysis("武器调整", "第一项"))],
            merge_error="程序合并失败",
        )

        self.assertEqual(context.calls, 1)
        self.assertIn("程序合并和总结模型分析均失败", refined["summary"])

    def test_chunk_refinement_payload_contains_raw_json_context(self) -> None:
        summary = make_summary()
        payload = build_chunk_refinement_payload(
            summary,
            summary.chunks,
            [ChunkAnalysis(1, 3, analysis("武器调整", "第一项"), error="提醒", raw_text='{"summary":"第一项"}')],
            merge_error="程序合并失败",
        )

        self.assertEqual(payload["merge_error"], "程序合并失败")
        self.assertEqual(payload["chunks"][0]["analysis"]["summary"], "第一项")
        self.assertEqual(payload["chunks"][0]["raw_text"], '{"summary":"第一项"}')
        self.assertEqual(payload["chunks"][0]["files"], ["a.blkx"])


class SequenceContext:
    def __init__(self, responses, *, delay: float = 0):
        self.responses = list(responses)
        self.delay = delay
        self.calls = 0
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []
        self.active = 0
        self.max_active = 0

    async def llm_generate(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        self.prompts.append(str(kwargs.get("prompt") or ""))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            if self.delay:
                await asyncio.sleep(self.delay)
            return response
        finally:
            self.active -= 1


class AnalyzerRequestFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_chunks_preserves_chunk_order_with_concurrency(self) -> None:
        summary = make_summary()
        context = SequenceContext(
            [
                json_response(analysis("武器调整", "第一项")),
                json_response(analysis("武器调整", "第二项")),
                json_response(analysis("武器调整", "第三项")),
            ],
            delay=0.01,
        )

        results = await analyze_chunks(context, make_settings(model_concurrency=2), summary)

        self.assertEqual([result.chunk_index for result in results], [1, 2, 3])
        self.assertEqual(context.max_active, 2)

    async def test_analyze_chunks_uses_analysis_provider(self) -> None:
        summary = make_summary()
        context = SequenceContext([json_response(analysis("武器调整", "第一项"))])
        context.get_provider_by_id = lambda provider_id: object()
        settings = PluginConfig(
            provider_id="analysis-provider",
            summary_provider_id="summary-provider",
            timeout_seconds=5,
            model_concurrency=1,
            analysis_prompt="请分析更新。",
            summary_prompt="请总结更新。",
            enable_second_pass_analysis=True,
            target_groups=[],
            analysis_file_groups=[],
            monitor_interval_minutes=30,
            github_token="",
            max_files_per_report=1,
            max_input_tokens=0,
            max_input_token_unit="K",
            max_retry_count=2,
            enable_push_append_text=False,
            push_append_text_template="",
        )
        single_summary = DiffSummary(
            base_sha=summary.base_sha,
            head_sha=summary.head_sha,
            compare_url=summary.compare_url,
            total_commits=summary.total_commits,
            total_files=summary.total_files,
            additions=summary.additions,
            deletions=summary.deletions,
            changed_files=summary.changed_files,
            commits=summary.commits,
            files=summary.files,
            chunks=[summary.chunks[0]],
        )

        await analyze_chunks(context, settings, single_summary)

        self.assertEqual(context.prompts[0].find("请分析更新。") >= 0, True)
        self.assertEqual(context.kwargs[0]["chat_provider_id"], "analysis-provider")

    async def test_invalid_json_triggers_repair_request(self) -> None:
        summary = make_summary()
        context = SequenceContext(
            [
                "不是 JSON",
                json_response(analysis("武器调整", "第二项")),
                json_response(analysis("武器调整", "第三项")),
                json_response(analysis("武器调整", "修复后的条目")),
            ]
        )

        results = await analyze_chunks(context, make_settings(model_concurrency=1), summary)

        self.assertEqual(context.calls, 4)
        self.assertEqual(results[0].analysis["summary"], "修复后的条目")
        self.assertIn("上一次模型分析返回的内容不是有效 JSON", context.prompts[3])

    async def test_llm_empty_output_failure_splits_chunk_and_retries_once(self) -> None:
        files = [
            {"filename": "a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {"filename": "b.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+b"},
            {"filename": "c.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+c"},
            {"filename": "d.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+d"},
        ]
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="https://example.invalid/compare",
            total_commits=1,
            total_files=len(files),
            additions=4,
            deletions=0,
            changed_files=len(files),
            commits=[],
            files=files,
            chunks=[DiffChunk(index=1, total=1, files=files, patch_chars=8)],
        )
        context = SequenceContext(
            [
                empty_choice_response("model_context_window_exceeded"),
                json_response(analysis("武器调整", "前半")),
                json_response(analysis("经济调整", "后半")),
            ]
        )

        results = await analyze_chunks(context, make_settings(), summary)

        self.assertEqual(context.calls, 3)
        self.assertEqual(len(results), 1)
        items = [item["text"] for section in results[0].analysis["update_sections"] for item in section["items"]]
        self.assertEqual(items, ["前半", "后半"])

    async def test_llm_failure_respects_configured_retry_count(self) -> None:
        files = [
            {"filename": "a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {"filename": "b.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+b"},
            {"filename": "c.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+c"},
            {"filename": "d.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+d"},
        ]
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="https://example.invalid/compare",
            total_commits=1,
            total_files=len(files),
            additions=4,
            deletions=0,
            changed_files=len(files),
            commits=[],
            files=files,
            chunks=[DiffChunk(index=1, total=1, files=files, patch_chars=8)],
        )
        context = SequenceContext(
            [
                empty_choice_response("model_context_window_exceeded"),
                empty_choice_response("model_context_window_exceeded"),
                json_response(analysis("武器调整", "B")),
                json_response(analysis("武器调整", "A")),
                json_response(analysis("经济调整", "后半")),
            ]
        )
        settings = make_settings()

        results = await analyze_chunks(context, settings, summary)

        self.assertEqual(context.calls, 5)
        items = [item["text"] for section in results[0].analysis["update_sections"] for item in section["items"]]
        self.assertEqual(items, ["A", "B", "后半"])


class AnalyzerUtilityTest(unittest.TestCase):
    def test_llm_failure_reason_detects_empty_context_window_output(self) -> None:
        response = empty_choice_response("model_context_window_exceeded")

        self.assertIn("model_context_window_exceeded", llm_failure_reason(response))

    def test_split_chunk_for_retry_halves_file_count(self) -> None:
        files = [{"filename": f"f{index}.blkx", "patch": "+x"} for index in range(5)]
        chunks = split_chunk_for_retry(DiffChunk(index=1, total=1, files=files, patch_chars=10))

        self.assertEqual([len(chunk.files) for chunk in chunks], [3, 2])

    def test_split_files_groups_similar_names_before_chunking(self) -> None:
        files = [
            {"filename": "units/tank_1.blkx", "patch": "+a"},
            {"filename": "weapons/gun.blkx", "patch": "+b"},
            {"filename": "units/tank_2.blkx", "patch": "+c"},
        ]
        chunks = split_files(files, max_files=2)
        ordered_filenames = [file["filename"] for chunk in chunks for file in chunk.files]

        self.assertEqual(ordered_filenames, ["units/tank_1.blkx", "units/tank_2.blkx", "weapons/gun.blkx"])

    def test_split_files_applies_file_count_as_hard_limit(self) -> None:
        files = [
            {"filename": "a", "patch": "a" * 100},
            {"filename": "b", "patch": "b" * 100},
            {"filename": "c", "patch": "c"},
            {"filename": "d", "patch": "d"},
            {"filename": "e", "patch": "e"},
            {"filename": "f", "patch": "f"},
        ]
        chunks = split_files(files, max_files=3)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(
            [[file["filename"] for file in chunk.files] for chunk in chunks],
            [["a", "b", "c"], ["d", "e", "f"]],
        )
        self.assertTrue(all(len(chunk.files) <= 3 for chunk in chunks))

    def test_split_files_uses_char_limit_for_quantity_without_splitting_files(self) -> None:
        files = [
            {"filename": "a", "patch": "a" * 50},
            {"filename": "b", "patch": "b" * 50},
            {"filename": "c", "patch": "c" * 50},
            {"filename": "d", "patch": "d" * 50},
        ]
        chunks = split_files(files, max_chars=80)
        chunk_filenames = [[file["filename"] for file in chunk.files] for chunk in chunks]

        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunk_filenames, [["a"], ["b"], ["c", "d"]])

    def test_split_chunks_by_token_limit_preserves_file_integrity(self) -> None:
        files = [
            {"filename": "a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+" + "a" * 200},
            {"filename": "b.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+" + "b" * 200},
            {"filename": "c.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+" + "c" * 200},
        ]
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="",
            total_commits=1,
            total_files=len(files),
            additions=3,
            deletions=0,
            changed_files=len(files),
            commits=[],
            files=files,
            chunks=[DiffChunk(index=1, total=1, files=files, patch_chars=600)],
        )
        settings = load_config({"analysis_prompt": "短", "max_input_tokens": 1, "max_input_token_unit": "K"})
        split_summary = split_chunks_by_token_limit(settings, summary)
        filenames = [file["filename"] for chunk in split_summary.chunks for file in chunk.files]

        self.assertEqual(filenames, ["a.blkx", "b.blkx", "c.blkx"])
        self.assertTrue(all(len(chunk.files) >= 1 for chunk in split_summary.chunks))
        self.assertGreater(sum(estimate_chunk_input_tokens(settings, split_summary, chunk) for chunk in split_summary.chunks), 0)

    def test_token_limit_splits_after_file_count_limit(self) -> None:
        files = [
            {"filename": "a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {"filename": "b.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+b"},
            {"filename": "c.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+c"},
            {"filename": "d.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+d"},
        ]
        base_chunks = split_files(files, max_files=3)
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="",
            total_commits=1,
            total_files=len(files),
            additions=4,
            deletions=0,
            changed_files=len(files),
            commits=[],
            files=files,
            chunks=base_chunks,
        )
        settings = SimpleNamespace(max_input_token_limit=250)

        with patch(
            "wtup.analysis.tokens.estimate_chunk_input_tokens",
            side_effect=lambda _settings, _summary, chunk: len(chunk.files) * 100,
        ):
            split_summary = split_chunks_by_token_limit(settings, summary)

        self.assertEqual(
            [[file["filename"] for file in chunk.files] for chunk in base_chunks],
            [["a.blkx", "b.blkx", "c.blkx"], ["d.blkx"]],
        )
        self.assertEqual(
            [[file["filename"] for file in chunk.files] for chunk in split_summary.chunks],
            [["a.blkx", "b.blkx"], ["c.blkx"], ["d.blkx"]],
        )
        self.assertTrue(all(len(chunk.files) <= 3 for chunk in split_summary.chunks))


def json_response(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def empty_choice_response(finish_reason: str):
    message = SimpleNamespace(content="")
    choice = SimpleNamespace(finish_reason=finish_reason, message=message)
    return SimpleNamespace(choices=[choice])


if __name__ == "__main__":
    unittest.main()
