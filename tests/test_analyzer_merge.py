from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from wtup.analyzer import (
    ChunkAnalysis,
    TokenUsage,
    analyze_chunks,
    build_change_manifest,
    build_chunk_refinement_payload,
    collect_source_ids,
    enforce_change_coverage,
    estimate_chunk_input_tokens,
    extract_token_usage,
    llm_failure_reason,
    merge_chunk_analyses,
    refine_chunk_analyses,
    refine_merged_analysis,
    request_llm,
    safe_normalize_analysis,
    split_chunks_by_token_limit,
    split_chunk_for_retry,
)
from wtup.config import PluginConfig, load_config
from wtup.diff_collector import DiffChunk, DiffSummary, build_diff_summary, split_files
from wtup.renderer import report_update_subtitle
from wtup.analysis import review_analysis_with_usage


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
    def test_diff_summary_extracts_versions_from_compare_commits(self) -> None:
        summary = build_diff_summary(
            {
                "base_commit": {
                    "sha": "base123",
                    "commit": {"message": "2.56.0.42"},
                },
                "commits": [
                    {
                        "sha": "head456",
                        "commit": {"message": "2.56.0.43"},
                    }
                ],
                "files": [],
            }
        )

        self.assertEqual(summary.base_version, "2.56.0.42")
        self.assertEqual(summary.head_version, "2.56.0.43")

    def test_merge_completes_partial_report_title_from_summary_versions(self) -> None:
        summary = replace(make_summary(), base_version="2.56.0.42", head_version="2.56.0.43")
        payload = analysis("武器调整", "第一项")
        payload["report_title"] = "-> 2.56.0.43"
        results = [ChunkAnalysis(1, 3, payload)]

        merged = merge_chunk_analyses(summary, summary.chunks, results)

        self.assertEqual(merged["report_title"], "2.56.0.42->2.56.0.43")

    def test_coverage_check_completes_single_version_report_title(self) -> None:
        summary = replace(make_summary(), base_version="2.56.0.42", head_version="2.56.0.43")
        payload = analysis("武器调整", "第一项")
        payload["report_title"] = "2.56.0.43"

        updated = enforce_change_coverage(summary, summary.chunks, payload)

        self.assertEqual(updated["report_title"], "2.56.0.42->2.56.0.43")

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

    def test_model_json_normalization_rewrites_pagination_context(self) -> None:
        parsed = safe_normalize_analysis(
            json_response(
                {
                    "report_title": "2.56.0.38->2.56.0.39",
                    "summary": "本批diff缺少具体参数",
                    "importance": "中",
                    "update_sections": [
                        {
                            "title": "参数调整",
                            "items": [{"text": "当前分片未提供完整参数", "children": []}],
                        }
                    ],
                    "ai_analysis": {
                        "changed_content": ["本分片包含参数调整"],
                        "player_impact": ["该分片显示影响待确认"],
                        "uncertainties": ["当前批次信息不足"],
                        "recommendation": "建议结合本批次后续 diff 复核",
                    },
                    "tags": ["参数调整"],
                }
            )
        )

        self.assertEqual(parsed["summary"], "本次 diff 缺少具体参数")
        self.assertEqual(parsed["update_sections"][0]["items"][0]["text"], "本次 diff 未提供完整参数")
        self.assertEqual(parsed["ai_analysis"]["changed_content"], ["本次 diff 包含参数调整"])
        self.assertEqual(parsed["ai_analysis"]["player_impact"], ["本次 diff 显示影响待确认"])
        self.assertEqual(parsed["ai_analysis"]["uncertainties"], ["本次 diff 信息不足"])
        self.assertEqual(parsed["recommendation"], "建议结合本次 diff 后续 diff 复核")

    def test_model_json_normalization_keeps_context_requests(self) -> None:
        payload = analysis("参数调整", "初次分析")
        payload["context_requests"] = [
            {
                "source_file": "./a.blkx",
                "missing_files": ["b.blkx", "../bad.blkx", "b.blkx"],
                "reason": "当前分片缺少关联文件",
                "priority": "高",
            }
        ]

        parsed = safe_normalize_analysis(json_response(payload))

        self.assertEqual(
            parsed["context_requests"],
            [
                {
                    "source_file": "a.blkx",
                    "missing_files": ["b.blkx"],
                    "reason": "本次 diff 缺少关联文件",
                    "priority": "高",
                }
            ],
        )

    def test_model_json_normalization_keeps_larger_report_limits(self) -> None:
        payload = analysis("参数调整", "初次分析")
        payload["update_sections"] = [
            {
                "title": "参数调整",
                "items": [
                    {"text": f"完整条目 {index}", "children": []}
                    for index in range(30)
                ],
            }
        ]
        payload["ai_analysis"] = {
            "changed_content": [f"改动摘要 {index}" for index in range(25)],
            "player_impact": [f"影响摘要 {index}" for index in range(25)],
            "uncertainties": [f"不确定点 {index}" for index in range(25)],
            "recommendation": "建议关注",
        }

        parsed = safe_normalize_analysis(json_response(payload))

        self.assertEqual(len(parsed["update_sections"][0]["items"]), 30)
        self.assertEqual(len(parsed["ai_analysis"]["changed_content"]), 25)
        self.assertEqual(len(parsed["ai_analysis"]["player_impact"]), 25)
        self.assertEqual(len(parsed["ai_analysis"]["uncertainties"]), 25)

    def test_merge_keeps_more_ai_summary_items(self) -> None:
        summary = make_summary()
        payload = analysis("参数调整", "初次分析")
        payload["ai_analysis"] = {
            "changed_content": [f"改动摘要 {index}" for index in range(20)],
            "player_impact": [f"影响摘要 {index}" for index in range(20)],
            "uncertainties": [f"不确定点 {index}" for index in range(20)],
            "recommendation": "建议关注",
        }
        second_payload = analysis("经济调整", "二次分析")
        second_payload["ai_analysis"] = {
            "changed_content": [f"改动摘要 {index}" for index in range(20, 40)],
            "player_impact": [f"影响摘要 {index}" for index in range(20, 40)],
            "uncertainties": [f"不确定点 {index}" for index in range(20, 40)],
            "recommendation": "建议关注",
        }

        merged = merge_chunk_analyses(
            summary,
            summary.chunks[:2],
            [
                ChunkAnalysis(1, 2, payload),
                ChunkAnalysis(2, 2, second_payload),
            ],
        )

        self.assertEqual(len(merged["ai_analysis"]["changed_content"]), 40)
        self.assertEqual(merged["ai_analysis"]["changed_content"][0], "改动摘要 0")
        self.assertEqual(merged["ai_analysis"]["changed_content"][-1], "改动摘要 39")

    def test_change_manifest_assigns_stable_source_ids(self) -> None:
        summary = make_summary()

        manifest = build_change_manifest(summary, summary.chunks[0])

        self.assertEqual([entry.source_id for entry in manifest], ["C001-001"])
        self.assertEqual(manifest[0].file_path, "a.blkx")
        self.assertIn("新增/新值: a", manifest[0].description)

    def test_normalization_preserves_source_ids_on_update_items(self) -> None:
        payload = analysis("参数调整", "初次分析")
        payload["update_sections"][0]["items"][0]["source_ids"] = ["C001-001"]

        parsed = safe_normalize_analysis(json_response(payload))

        self.assertEqual(collect_source_ids(parsed), {"C001-001"})

    def test_coverage_checker_appends_missing_change_entries(self) -> None:
        summary = make_summary()
        payload = analysis("参数调整", "模型只写了概括")

        covered = enforce_change_coverage(summary, summary.chunks[:1], payload)

        self.assertEqual(covered["coverage"]["expected"], 1)
        self.assertEqual(covered["coverage"]["missing"], 1)
        self.assertIn("C001-001", collect_source_ids(covered))
        titles = [section["title"] for section in covered["update_sections"]]
        self.assertIn("需复核的未覆盖变更", titles)

    def test_coverage_checker_compacts_repeated_uncovered_diff_entries(self) -> None:
        patch = """@@ -42303,7 +42303,7 @@
-        "_template": "rendinst_replicated",
+        "_template": "rendinst",
@@ -42329,7 +42329,7 @@
-        "_template": "rendinst_replicated",
+        "_template": "rendinst",
"""
        files = [
            {
                "filename": "aces.vromfs.bin_u/gamedata/scenes/hangar_field_halloween_objects.blkx",
                "status": "modified",
                "additions": 2,
                "deletions": 2,
                "patch": patch,
            }
        ]
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="",
            total_commits=1,
            total_files=1,
            additions=2,
            deletions=2,
            changed_files=1,
            commits=[],
            files=files,
            chunks=[DiffChunk(index=1, total=1, files=files, patch_chars=len(patch))],
        )
        payload = analysis("参数调整", "模型只写了概括")

        covered = enforce_change_coverage(summary, summary.chunks, payload)

        review_sections = [
            section
            for section in covered["update_sections"]
            if section["title"].startswith("需复核的未覆盖变更")
        ]
        self.assertEqual(len(review_sections), 1)
        items = review_sections[0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_ids"], ["C001-001", "C001-002"])
        self.assertIn("2 处同类未覆盖变更", items[0]["text"])
        self.assertIn('"_template": "rendinst_replicated"', items[0]["text"])
        self.assertIn('"_template": "rendinst"', items[0]["text"])
        self.assertNotIn('", ->', items[0]["text"])
        self.assertNotIn('",，', items[0]["text"])
        self.assertNotIn("@@", items[0]["text"])
        self.assertNotIn("删除/旧值", items[0]["text"])
        self.assertNotIn("新增/新值", items[0]["text"])

    def test_normalization_filters_raw_diff_summary_copied_by_model(self) -> None:
        payload = analysis(
            "需复核的未覆盖变更",
            'aces.vromfs.bin_u/gamedata/scenes/hangar_field_halloween_objects.blkx: @@ -42303,7 +42303,7 @@；删除/旧值: "_template": "rendinst_replicated",；新增/新值: "_template": "rendinst",',
        )
        payload["update_sections"][0]["items"][0]["source_ids"] = ["C001-001"]
        payload["ai_analysis"]["changed_content"] = [
            'aces.vromfs.bin_u/gamedata/scenes/hangar_field_halloween_objects.blkx: @@ -42303,7 +42303,7 @@；删除/旧值: "_template": "rendinst_replicated",；新增/新值: "_template": "rendinst",'
        ]

        parsed = safe_normalize_analysis(json_response(payload))

        item_text = parsed["update_sections"][0]["items"][0]["text"]
        changed_text = parsed["ai_analysis"]["changed_content"][0]
        self.assertIn('"_template": "rendinst_replicated" -> "_template": "rendinst"', item_text)
        self.assertNotIn("@@", item_text)
        self.assertNotIn("删除/旧值", item_text)
        self.assertNotIn("新增/新值", item_text)
        self.assertEqual(item_text, changed_text)
        self.assertEqual(parsed["update_sections"][0]["items"][0]["source_ids"], ["C001-001"])

    def test_normalization_merges_duplicate_raw_diff_summary_items(self) -> None:
        payload = analysis("需复核的未覆盖变更", "占位")
        payload["update_sections"][0]["items"] = [
            {
                "text": 'aces.vromfs.bin_u/gamedata/scenes/hangar_field_halloween_objects.blkx: @@ -42303,7 +42303,7 @@；删除/旧值: "_template": "rendinst_replicated",；新增/新值: "_template": "rendinst",',
                "children": [],
                "source_ids": ["C001-001"],
            },
            {
                "text": 'aces.vromfs.bin_u/gamedata/scenes/hangar_field_halloween_objects.blkx: @@ -42329,7 +42329,7 @@；删除/旧值: "_template": "rendinst_replicated",；新增/新值: "_template": "rendinst",',
                "children": [],
                "source_ids": ["C001-002"],
            },
        ]

        parsed = safe_normalize_analysis(json_response(payload))

        items = parsed["update_sections"][0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_ids"], ["C001-001", "C001-002"])
        self.assertIn('"_template": "rendinst_replicated" -> "_template": "rendinst"', items[0]["text"])
        self.assertNotIn("@@", items[0]["text"])
        self.assertNotIn("删除/旧值", items[0]["text"])
        self.assertNotIn("新增/新值", items[0]["text"])

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

    def test_merge_rewrites_pagination_context_in_items_and_uncertainties(self) -> None:
        summary = make_summary()
        payload = analysis("参数调整", "具体参数未在本批diff中出现")
        payload["ai_analysis"]["uncertainties"] = ["当前分片缺少参数，需要复核"]

        merged = merge_chunk_analyses(summary, summary.chunks[:1], [ChunkAnalysis(1, 1, payload)])

        item_text = merged["update_sections"][0]["items"][0]["text"]
        uncertainties = merged["ai_analysis"]["uncertainties"]
        self.assertEqual(item_text, "具体参数未在本次 diff 中出现")
        self.assertEqual(uncertainties, ["本次 diff 缺少参数，需要复核"])

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

    def test_backup_provider_ids_are_loaded_and_deduplicated(self) -> None:
        settings = load_config(
            {
                "provider_id": "main-model",
                "backup_provider_id_1": " backup-model ",
                "backup_provider_id_2": "backup-model",
            }
        )

        self.assertEqual(settings.backup_provider_ids, ["backup-model"])
        self.assertEqual(settings.analysis_provider_ids, ["main-model", "backup-model"])
        self.assertEqual(settings.provider_fallback_ids("summary-model"), ["summary-model", "backup-model"])

    def test_admin_targets_are_loaded_from_list_or_lines(self) -> None:
        list_settings = load_config({"admin_targets": ["123", "platform:private:user"]})
        line_settings = load_config({"admin_targets": "123\nplatform:private:user"})

        self.assertEqual(list_settings.admin_targets, ["123", "platform:private:user"])
        self.assertEqual(line_settings.admin_targets, ["123", "platform:private:user"])

    def test_model_provider_config_items_are_grouped_first(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(list(schema.keys())[:6], [
            "provider_id",
            "summary_provider_id",
            "review_provider_id",
            "review_quality_provider_id",
            "backup_provider_id_1",
            "backup_provider_id_2",
        ])

    def test_token_limit_unit_uses_k_or_m(self) -> None:
        self.assertEqual(load_config({"max_input_tokens": 32, "max_input_token_unit": "K"}).max_input_token_limit, 32000)
        self.assertEqual(load_config({"max_input_tokens": 1, "max_input_token_unit": "M"}).max_input_token_limit, 1000000)

    def test_long_context_split_defaults_are_loaded_without_schema(self) -> None:
        settings = load_config({})

        self.assertEqual(settings.max_files_per_report, 150)
        self.assertEqual(settings.max_input_tokens, Decimal("0.10"))
        self.assertEqual(settings.max_input_token_unit, "M")
        self.assertEqual(settings.max_input_token_limit, 100000)

    def test_explicit_zero_keeps_unlimited_split_settings(self) -> None:
        settings = load_config({"max_files_per_report": 0, "max_input_tokens": 0})

        self.assertEqual(settings.max_files_per_report, 0)
        self.assertEqual(settings.max_input_tokens, Decimal("0.00"))
        self.assertEqual(settings.max_input_token_limit, 0)

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

    def test_streaming_llm_call_defaults_to_disabled(self) -> None:
        settings = load_config({})

        self.assertFalse(settings.enable_streaming_llm_call)

    def test_streaming_llm_call_accepts_bool_like_values(self) -> None:
        settings = load_config({"enable_streaming_llm_call": "开启"})

        self.assertTrue(settings.enable_streaming_llm_call)

    def test_clear_cache_files_defaults_to_disabled(self) -> None:
        settings = load_config({})

        self.assertFalse(settings.clear_cache_files)

    def test_clear_cache_files_accepts_bool_like_values(self) -> None:
        settings = load_config({"clear_cache_files": "开启"})

        self.assertTrue(settings.clear_cache_files)

    def test_github_retry_and_termination_defaults(self) -> None:
        settings = load_config({})

        self.assertEqual(settings.github_max_retry_count, 4)
        self.assertFalse(settings.terminate_running_task)
        self.assertFalse(settings.enable_task_lock)

    def test_terminate_running_task_accepts_bool_like_values(self) -> None:
        settings = load_config({"terminate_running_task": "开启"})

        self.assertTrue(settings.terminate_running_task)

    def test_enable_task_lock_accepts_bool_like_values(self) -> None:
        settings = load_config({"enable_task_lock": "开启"})

        self.assertTrue(settings.enable_task_lock)

    def test_dynamic_context_queue_defaults_to_enabled_with_limits(self) -> None:
        settings = load_config({})

        self.assertTrue(settings.enable_dynamic_context_queue)
        self.assertEqual(settings.max_dynamic_context_rounds, 1)
        self.assertEqual(settings.max_dynamic_context_requests, 8)
        self.assertEqual(settings.max_dynamic_files_per_request, 4)
        self.assertEqual(settings.max_dynamic_context_chars, 12000)

    def test_dynamic_context_queue_can_be_disabled(self) -> None:
        settings = load_config({"enable_dynamic_context_queue": "关闭", "max_dynamic_context_rounds": 0})

        self.assertFalse(settings.enable_dynamic_context_queue)
        self.assertEqual(settings.max_dynamic_context_rounds, 0)

    def test_dynamic_context_chars_accepts_zero_as_unlimited(self) -> None:
        settings = load_config({"max_dynamic_context_chars": 0})

        self.assertEqual(settings.max_dynamic_context_chars, 0)

    def test_review_config_defaults_and_modes(self) -> None:
        settings = load_config({})

        self.assertFalse(settings.enable_review_model)
        self.assertEqual(settings.review_mode, "quality")
        self.assertEqual(settings.effective_review_provider_id, "")
        self.assertEqual(settings.effective_review_quality_provider_id, "")
        self.assertIn("质检模型", settings.review_prompt)

        energy_settings = load_config({"review_mode": "节能"})
        quality_settings = load_config({"review_mode": "quality"})

        self.assertEqual(energy_settings.review_mode, "energy")
        self.assertEqual(quality_settings.review_mode, "quality")

    def test_review_provider_defaults_to_analysis_model(self) -> None:
        settings = load_config({"provider_id": "analysis-provider", "review_provider_id": "review-provider"})

        self.assertEqual(settings.effective_review_provider_id, "review-provider")
        self.assertEqual(settings.effective_review_quality_provider_id, "review-provider")


class ReviewModelTest(unittest.IsolatedAsyncioTestCase):
    async def test_energy_review_records_issues_without_revision(self) -> None:
        summary = make_summary()
        payload = with_source_ids(analysis("参数调整", "部分载具参数变化"), "C001-001")
        context = FakeContext(json_response({"passed": False, "issues": ["存在模糊表述"], "severity": "中"}))
        settings = load_config(
            {
                "enable_review_model": True,
                "review_mode": "energy",
                "review_provider_id": "review-provider",
            }
        )

        result = await review_analysis_with_usage(context, settings, summary, payload)

        self.assertEqual(result.mode_used, "energy")
        self.assertIn("存在模糊表述", result.issues)
        self.assertFalse(result.applied_revision)
        self.assertIn("review", result.analysis)
        self.assertEqual(context.kwargs[0]["chat_provider_id"], "review-provider")

    async def test_quality_review_applies_item_revision_without_touching_source_ids(self) -> None:
        summary = make_summary()
        payload = with_source_ids(analysis("参数调整", "部分载具参数变化"), "C001-001")
        context = FakeContext(
            json_response(
                {
                    "passed": False,
                    "issues": ["修正模糊表述"],
                    "severity": "中",
                    "item_revisions": [
                        {
                            "item_id": "I001",
                            "new_text": "M1 Abrams(M1 艾布拉姆斯) 参数变化，对游戏表现影响待复核。",
                            "reason": "点名具体载具",
                        }
                    ],
                }
            )
        )
        settings = load_config(
            {
                "enable_review_model": True,
                "review_mode": "quality",
                "review_quality_provider_id": "quality-review-provider",
            }
        )

        result = await review_analysis_with_usage(context, settings, summary, payload)

        item = result.analysis["update_sections"][0]["items"][0]
        self.assertTrue(result.applied_revision)
        self.assertIn("M1 Abrams", item["text"])
        self.assertEqual(item["source_ids"], ["C001-001"])
        self.assertEqual(result.revision_stats["attempted"], 1)
        self.assertEqual(result.revision_stats["applied"], 1)
        self.assertIn('"item_id": "I001"', context.kwargs[0]["prompt"])

    async def test_quality_review_rejects_unknown_item_revision(self) -> None:
        summary = make_summary()
        payload = with_source_ids(analysis("参数调整", "部分载具参数变化"), "C001-001")
        context = FakeContext(
            json_response(
                {
                    "passed": False,
                    "issues": [],
                    "severity": "中",
                    "item_revisions": [{"item_id": "I999", "new_text": "不应采用"}],
                }
            )
        )
        settings = load_config({"enable_review_model": True, "review_mode": "quality"})

        result = await review_analysis_with_usage(context, settings, summary, payload)

        self.assertFalse(result.applied_revision)
        self.assertEqual(result.analysis["update_sections"][0]["items"][0]["text"], "部分载具参数变化")
        self.assertEqual(result.revision_stats["applied"], 0)
        self.assertEqual(result.revision_stats["rejected"], 1)
        self.assertTrue(any("I999 不存在" in issue for issue in result.issues))

    async def test_quality_review_rejects_item_revision_with_mismatched_source_ids(self) -> None:
        summary = make_summary()
        payload = with_source_ids(analysis("参数调整", "部分载具参数变化"), "C001-001")
        context = FakeContext(
            json_response(
                {
                    "passed": False,
                    "issues": [],
                    "severity": "中",
                    "item_revisions": [
                        {"item_id": "I001", "new_text": "不应采用", "source_ids": ["C999-001"]}
                    ],
                }
            )
        )
        settings = load_config({"enable_review_model": True, "review_mode": "quality"})

        result = await review_analysis_with_usage(context, settings, summary, payload)

        self.assertFalse(result.applied_revision)
        self.assertEqual(result.analysis["update_sections"][0]["items"][0]["text"], "部分载具参数变化")
        self.assertEqual(result.revision_stats["applied"], 0)
        self.assertEqual(result.revision_stats["rejected"], 1)
        self.assertTrue(any("source_ids 与原条目不一致" in issue for issue in result.issues))

    async def test_quality_review_applies_revision_when_source_ids_are_kept(self) -> None:
        summary = make_summary()
        payload = with_source_ids(analysis("参数调整", "部分载具参数变化"), "C001-001")
        revised = with_source_ids(analysis("参数调整", "M1 Abrams(M1 艾布拉姆斯) 参数变化，对游戏表现影响待复核。"), "C001-001")
        context = FakeContext(
            json_response(
                {
                    "passed": False,
                    "issues": ["修正模糊表述"],
                    "severity": "中",
                    "revised_analysis": revised,
                }
            )
        )
        settings = load_config(
            {
                "enable_review_model": True,
                "review_mode": "quality",
                "review_quality_provider_id": "quality-review-provider",
            }
        )

        result = await review_analysis_with_usage(context, settings, summary, payload)

        self.assertEqual(result.mode_used, "quality")
        self.assertTrue(result.applied_revision)
        self.assertIn("M1 Abrams", result.analysis["update_sections"][0]["items"][0]["text"])
        self.assertEqual(context.kwargs[0]["chat_provider_id"], "quality-review-provider")


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
        enable_streaming_llm_call=False,
        analysis_prompt="请分析更新。",
        summary_prompt="请总结更新。",
        enable_second_pass_analysis=True,
        target_groups=[],
        analysis_file_groups=[],
        monitor_interval_minutes=30,
        github_token="",
        github_max_retry_count=4,
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
            enable_streaming_llm_call=False,
            analysis_prompt=settings.analysis_prompt,
            summary_prompt=settings.summary_prompt,
            enable_second_pass_analysis=True,
            target_groups=[],
            analysis_file_groups=[],
            monitor_interval_minutes=settings.monitor_interval_minutes,
            github_token="",
            github_max_retry_count=4,
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

        expected = enforce_change_coverage(summary, summary.chunks, merged)

        refined = await refine_merged_analysis(context, make_settings(), summary, merged)

        self.assertEqual(context.calls, 1)
        self.assertEqual(refined["summary"], expected["summary"])
        self.assertEqual(refined["update_sections"], expected["update_sections"])

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


class StreamProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.kwargs: list[dict] = []

    async def text_chat_stream(self, **kwargs):
        self.kwargs.append(kwargs)
        for response in self.responses:
            await asyncio.sleep(0)
            yield response


class StreamContext(SequenceContext):
    def __init__(self, provider: StreamProvider | None):
        super().__init__([json_response(analysis("武器调整", "非流式结果"))])
        self.provider = provider

    def get_provider_by_id(self, provider_id: str):
        return self.provider

    def get_all_providers(self):
        return [self.provider] if self.provider is not None else []


class AnalyzerRequestFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_llm_records_task_log_events(self) -> None:
        events: list[tuple[str, dict]] = []

        def recorder(event: str, metadata: dict) -> int | None:
            events.append((event, metadata))
            return 1 if event == "模型请求开始" else None

        context = SequenceContext([json_response(analysis("武器调整", "第一项"))])
        settings = replace(load_config({}), task_log_recorder=recorder)

        await request_llm(context, settings, "请分析这个 diff")

        self.assertEqual([event for event, _metadata in events], ["模型请求开始", "模型请求完成"])
        self.assertGreater(events[0][1]["输入token"], 0)
        self.assertNotIn("请求内容", events[0][1])
        self.assertEqual(events[1][1]["第几次模型请求"], 1)

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
            enable_streaming_llm_call=False,
            analysis_prompt="请分析更新。",
            summary_prompt="请总结更新。",
            enable_second_pass_analysis=True,
            target_groups=[],
            analysis_file_groups=[],
            monitor_interval_minutes=30,
            github_token="",
            github_max_retry_count=4,
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

    async def test_dynamic_context_queue_enqueues_missing_diff_file_and_logs_counts(self) -> None:
        summary = make_summary()
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
        first = with_source_ids(analysis("参数调整", "初次分析"), "C001-001")
        first["ai_analysis"]["uncertainties"] = ["缺少 b.blkx 无法确认影响"]
        first["context_requests"] = [
            {
                "source_file": "a.blkx",
                "missing_files": ["b.blkx"],
                "reason": "需要 b 文件确认关联参数",
                "priority": "高",
            }
        ]
        second = with_source_ids(analysis("参数调整", "补充后分析"), "C002-001")
        second["resolved_uncertainties"] = ["缺少 b.blkx 无法确认影响"]
        context = SequenceContext([json_response(first), json_response(second)])
        events: list[tuple[str, dict]] = []
        settings = replace(
            load_config(
                {
                    "enable_dynamic_context_queue": True,
                    "max_dynamic_context_rounds": 1,
                    "max_dynamic_context_requests": 4,
                }
            ),
            task_log_recorder=lambda event, metadata: events.append((event, metadata)) or len(events),
        )

        results = await analyze_chunks(context, settings, single_summary)

        self.assertEqual(context.calls, 2)
        self.assertIn("当前动态补充请求", context.prompts[1])
        items = [item["text"] for section in results[0].analysis["update_sections"] for item in section["items"]]
        self.assertEqual(items, ["初次分析", "补充后分析"])
        self.assertNotIn("缺少 b.blkx 无法确认影响", results[0].analysis["ai_analysis"]["uncertainties"])
        event_names = [event for event, _metadata in events]
        self.assertIn("动态补充扫描", event_names)
        self.assertIn("动态补充入队", event_names)
        self.assertIn("动态补充汇总", event_names)
        enqueue_metadata = next(metadata for event, metadata in events if event == "动态补充入队")
        self.assertEqual(enqueue_metadata["本轮不确定点数量"], 1)
        self.assertEqual(enqueue_metadata["本轮补充请求数量"], 1)
        self.assertEqual(enqueue_metadata["实际入队"], 1)

    async def test_dynamic_context_queue_auto_generates_requests_from_mentioned_changed_files(self) -> None:
        files = [
            {"filename": "units/a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {"filename": "units/b.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+b"},
        ]
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="https://example.invalid/compare",
            total_commits=1,
            total_files=len(files),
            additions=2,
            deletions=0,
            changed_files=len(files),
            commits=[],
            files=files,
            chunks=[DiffChunk(index=1, total=1, files=[files[0]], patch_chars=2)],
        )
        first = with_source_ids(analysis("参数调整", "初次分析"), "C001-001")
        first["ai_analysis"]["uncertainties"] = ["缺少 units/b.blkx 无法确认同组参数影响"]
        first["context_requests"] = []
        second = with_source_ids(analysis("参数调整", "自动补充后分析"), "C002-001")
        second["resolved_uncertainties"] = ["缺少 units/b.blkx 无法确认同组参数影响"]
        context = SequenceContext([json_response(first), json_response(second)])
        events: list[tuple[str, dict]] = []
        settings = replace(
            load_config(
                {
                    "enable_dynamic_context_queue": True,
                    "max_dynamic_context_rounds": 1,
                    "max_dynamic_context_requests": 4,
                }
            ),
            task_log_recorder=lambda event, metadata: events.append((event, metadata)) or len(events),
        )

        results = await analyze_chunks(context, settings, summary)

        self.assertEqual(context.calls, 2)
        self.assertIn("units/b.blkx", context.prompts[1])
        items = [item["text"] for section in results[0].analysis["update_sections"] for item in section["items"]]
        self.assertEqual(items, ["初次分析", "自动补充后分析"])
        scan_metadata = next(metadata for event, metadata in events if event == "动态补充扫描")
        enqueue_metadata = next(metadata for event, metadata in events if event == "动态补充入队")
        summary_metadata = next(metadata for event, metadata in events if event == "动态补充汇总")
        self.assertEqual(scan_metadata["补充请求数量"], 1)
        self.assertEqual(scan_metadata["自动生成补充请求数量"], 1)
        self.assertEqual(enqueue_metadata["实际入队"], 1)
        self.assertEqual(enqueue_metadata["自动生成补充请求"], 1)
        self.assertEqual(summary_metadata["自动生成补充请求"], 1)

    async def test_dynamic_context_queue_does_not_auto_generate_from_same_directory_hint(self) -> None:
        files = [
            {"filename": "units/a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {"filename": "units/b.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+b"},
        ]
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="https://example.invalid/compare",
            total_commits=1,
            total_files=len(files),
            additions=2,
            deletions=0,
            changed_files=len(files),
            commits=[],
            files=files,
            chunks=[DiffChunk(index=1, total=1, files=[files[0]], patch_chars=2)],
        )
        first = with_source_ids(analysis("参数调整", "初次分析"), "C001-001")
        first["ai_analysis"]["uncertainties"] = ["缺少同目录配置无法确认同组参数影响"]
        first["context_requests"] = []
        context = SequenceContext([json_response(first)])
        events: list[tuple[str, dict]] = []
        settings = replace(
            load_config(
                {
                    "enable_dynamic_context_queue": True,
                    "max_dynamic_context_rounds": 1,
                    "max_dynamic_context_requests": 4,
                }
            ),
            task_log_recorder=lambda event, metadata: events.append((event, metadata)) or len(events),
        )

        results = await analyze_chunks(context, settings, summary)

        self.assertEqual(context.calls, 1)
        self.assertEqual(results[0].analysis["summary"], "初次分析")
        scan_metadata = next(metadata for event, metadata in events if event == "动态补充扫描")
        enqueue_metadata = next(metadata for event, metadata in events if event == "动态补充入队")
        self.assertEqual(scan_metadata["补充请求数量"], 0)
        self.assertEqual(scan_metadata["自动生成补充请求数量"], 0)
        self.assertEqual(enqueue_metadata["实际入队"], 0)
        self.assertEqual(enqueue_metadata["自动生成补充请求"], 0)

    async def test_dynamic_context_queue_skips_oversized_context_request(self) -> None:
        files = [
            {"filename": "units/a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {
                "filename": "units/b.blkx",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "patch": "+" + ("b" * 200),
            },
        ]
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="https://example.invalid/compare",
            total_commits=1,
            total_files=len(files),
            additions=2,
            deletions=0,
            changed_files=len(files),
            commits=[],
            files=files,
            chunks=[DiffChunk(index=1, total=1, files=[files[0]], patch_chars=2)],
        )
        first = with_source_ids(analysis("参数调整", "初次分析"), "C001-001")
        first["ai_analysis"]["uncertainties"] = ["缺少 units/b.blkx 无法确认同组参数影响"]
        first["context_requests"] = [
            {
                "source_file": "units/a.blkx",
                "missing_files": ["units/b.blkx"],
                "reason": "需要 b 文件确认关联参数",
                "priority": "高",
            }
        ]
        context = SequenceContext([json_response(first)])
        events: list[tuple[str, dict]] = []
        settings = replace(
            load_config(
                {
                    "enable_dynamic_context_queue": True,
                    "max_dynamic_context_rounds": 1,
                    "max_dynamic_context_requests": 4,
                    "max_dynamic_context_chars": 80,
                }
            ),
            task_log_recorder=lambda event, metadata: events.append((event, metadata)) or len(events),
        )

        results = await analyze_chunks(context, settings, summary)

        self.assertEqual(context.calls, 1)
        self.assertEqual(results[0].analysis["summary"], "初次分析")
        skip_metadata = next(metadata for event, metadata in events if event == "动态补充请求跳过")
        enqueue_metadata = next(metadata for event, metadata in events if event == "动态补充入队")
        self.assertEqual(skip_metadata["原因"], "动态补充上下文超过字符上限")
        self.assertEqual(skip_metadata["文件处理"][1]["status"], "too_large")
        self.assertEqual(enqueue_metadata["实际入队"], 0)
        self.assertEqual(enqueue_metadata["无效跳过"], 1)

    async def test_dynamic_context_queue_can_be_disabled_for_analysis(self) -> None:
        summary = make_summary()
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
        first = analysis("参数调整", "初次分析")
        first["context_requests"] = [
            {
                "source_file": "a.blkx",
                "missing_files": ["b.blkx"],
                "reason": "需要 b 文件确认关联参数",
                "priority": "高",
            }
        ]
        context = SequenceContext([json_response(first)])

        results = await analyze_chunks(
            context,
            load_config({"enable_dynamic_context_queue": False}),
            single_summary,
        )

        self.assertEqual(context.calls, 1)
        self.assertEqual(results[0].analysis["summary"], "初次分析")

    async def test_analyze_chunks_falls_back_to_backup_provider_on_request_error(self) -> None:
        summary = make_summary()
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
        context = SequenceContext(
            [
                RuntimeError("primary provider failed"),
                json_response(analysis("武器调整", "备用模型结果")),
            ]
        )
        context.get_provider_by_id = lambda provider_id: object()
        settings = load_config(
            {
                "provider_id": "primary-provider",
                "backup_provider_id_1": "backup-provider",
                "max_retry_count": 0,
            }
        )

        results = await analyze_chunks(context, settings, single_summary)

        self.assertEqual(context.calls, 2)
        self.assertEqual(
            [kwargs["chat_provider_id"] for kwargs in context.kwargs],
            ["primary-provider", "backup-provider"],
        )
        self.assertEqual(results[0].analysis["summary"], "备用模型结果")

    async def test_analysis_provider_failure_splits_before_backup_provider(self) -> None:
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
                RuntimeError("primary provider failed"),
                json_response(with_source_ids(analysis("武器调整", "前半"), "C001-001", "C002-001")),
                json_response(with_source_ids(analysis("经济调整", "后半"), "C003-001", "C004-001")),
            ]
        )
        context.get_provider_by_id = lambda provider_id: object()
        settings = load_config(
            {
                "provider_id": "primary-provider",
                "backup_provider_id_1": "backup-provider",
                "max_retry_count": 1,
            }
        )

        results = await analyze_chunks(context, settings, summary)

        self.assertEqual(context.calls, 3)
        self.assertEqual(
            [kwargs["chat_provider_id"] for kwargs in context.kwargs],
            ["primary-provider", "primary-provider", "primary-provider"],
        )
        items = [item["text"] for section in results[0].analysis["update_sections"] for item in section["items"]]
        self.assertEqual(items, ["前半", "后半"])

    async def test_analysis_uses_backup_after_primary_split_retries_are_exhausted(self) -> None:
        files = [
            {"filename": "a.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {"filename": "b.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+b"},
        ]
        summary = DiffSummary(
            base_sha="base123",
            head_sha="head456",
            compare_url="https://example.invalid/compare",
            total_commits=1,
            total_files=len(files),
            additions=2,
            deletions=0,
            changed_files=len(files),
            commits=[],
            files=files,
            chunks=[DiffChunk(index=1, total=1, files=files, patch_chars=4)],
        )
        context = SequenceContext(
            [
                RuntimeError("primary original failed"),
                RuntimeError("primary first split failed"),
                RuntimeError("primary second split failed"),
                json_response(analysis("武器调整", "备用模型结果")),
            ]
        )
        context.get_provider_by_id = lambda provider_id: object()
        settings = load_config(
            {
                "provider_id": "primary-provider",
                "backup_provider_id_1": "backup-provider",
                "max_retry_count": 1,
            }
        )

        results = await analyze_chunks(context, settings, summary)

        self.assertEqual(context.calls, 4)
        self.assertEqual(
            [kwargs["chat_provider_id"] for kwargs in context.kwargs],
            ["primary-provider", "primary-provider", "primary-provider", "backup-provider"],
        )
        self.assertEqual(results[0].analysis["summary"], "备用模型结果")

    async def test_request_llm_records_provider_failure_before_fallback(self) -> None:
        records = []
        context = SequenceContext(
            [
                RuntimeError("primary provider failed"),
                json_response(analysis("武器调整", "备用模型结果")),
            ]
        )
        context.get_provider_by_id = lambda provider_id: object()
        settings = replace(
            load_config(
                {
                    "provider_id": "primary-provider",
                    "backup_provider_id_1": "backup-provider",
                }
            ),
            model_error_recorder=lambda stage, error, metadata: records.append((stage, str(error), metadata)),
        )

        response = await request_llm(context, settings, "请分析")

        self.assertEqual(json.loads(response)["summary"], "备用模型结果")
        self.assertEqual(records[0][0], "provider_request_failed")
        self.assertEqual(records[0][1], "primary provider failed")
        self.assertEqual(records[0][2]["provider_id"], "primary-provider")
        self.assertEqual(records[0][2]["provider_index"], 1)
        self.assertEqual(records[0][2]["provider_total"], 2)

    async def test_request_llm_uses_default_model_before_backup_when_primary_is_empty(self) -> None:
        context = SequenceContext(
            [
                RuntimeError("default provider failed"),
                json_response(analysis("武器调整", "备用模型结果")),
            ]
        )
        context.get_provider_by_id = lambda provider_id: object()
        settings = load_config({"backup_provider_id_1": "backup-provider"})

        response = await request_llm(context, settings, "请分析")

        self.assertEqual(json.loads(response)["summary"], "备用模型结果")
        self.assertEqual(context.calls, 2)
        self.assertNotIn("chat_provider_id", context.kwargs[0])
        self.assertEqual(context.kwargs[1]["chat_provider_id"], "backup-provider")

    async def test_request_llm_uses_non_streaming_call_by_default(self) -> None:
        context = SequenceContext([json_response(analysis("武器调整", "非流式结果"))])
        context.get_provider_by_id = lambda provider_id: object()
        settings = load_config({"provider_id": "analysis-provider"})

        response = await request_llm(context, settings, "请分析")

        self.assertEqual(context.calls, 1)
        self.assertEqual(context.kwargs[0]["chat_provider_id"], "analysis-provider")
        self.assertNotIn("stream", context.kwargs[0])
        self.assertEqual(json.loads(response)["summary"], "非流式结果")

    async def test_request_llm_uses_streaming_provider_when_enabled(self) -> None:
        provider = StreamProvider(
            [
                SimpleNamespace(is_chunk=True, completion_text='{"summary":"'),
                SimpleNamespace(is_chunk=True, completion_text='流式结果"}', usage=SimpleNamespace(input=4, output=5, total=9)),
            ]
        )
        context = StreamContext(provider)
        settings = load_config({"provider_id": "analysis-provider", "enable_streaming_llm_call": True})

        response = await request_llm(context, settings, "请分析")

        self.assertEqual(context.calls, 0)
        self.assertEqual(provider.kwargs[0], {"prompt": "请分析"})
        self.assertEqual(response.completion_text, '{"summary":"流式结果"}')
        self.assertEqual(extract_token_usage(response).total_tokens, 9)

    async def test_invalid_json_triggers_repair_request(self) -> None:
        summary = make_summary()
        summary = DiffSummary(
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
        context = SequenceContext(
            [
                "不是 JSON",
                json_response(analysis("武器调整", "修复后的条目")),
            ]
        )

        results = await analyze_chunks(context, make_settings(model_concurrency=1), summary)

        self.assertEqual(context.calls, 2)
        self.assertEqual(results[0].analysis["summary"], "修复后的条目")
        self.assertIn("上一次模型分析返回的内容不是有效 JSON", context.prompts[1])

    async def test_tool_calls_are_ignored_when_disabled(self) -> None:
        summary = make_summary()
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
        payload = analysis("武器调整", "初次分析")
        payload["tool_calls"] = [{"tool": "read_changed_patch", "path": "b.blkx", "reason": "需要更多上下文"}]
        context = SequenceContext([json_response(payload)])

        results = await analyze_chunks(context, load_config({}), single_summary)

        self.assertEqual(context.calls, 1)
        self.assertEqual(results[0].analysis["summary"], "初次分析")

    async def test_tool_calls_refine_chunk_with_local_patch(self) -> None:
        summary = make_summary()
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
        first = analysis("武器调整", "初次分析")
        first["tool_calls"] = [{"tool": "read_changed_patch", "path": "b.blkx", "reason": "查看 b 文件 patch"}]
        context = SequenceContext(
            [
                json_response(first),
                json_response(analysis("武器调整", "补充后分析")),
            ]
        )
        events: list[tuple[str, dict]] = []
        settings = replace(
            load_config(
                {
                    "enable_model_tool_calls": True,
                    "max_tool_call_rounds": 1,
                    "max_tool_calls_per_round": 2,
                }
            ),
            task_log_recorder=lambda event, metadata: events.append((event, metadata)) or len(events),
        )

        results = await analyze_chunks(context, settings, single_summary)

        self.assertEqual(context.calls, 2)
        self.assertEqual(results[0].analysis["summary"], "补充后分析")
        self.assertIn("模型工具调用协议", context.prompts[0])
        self.assertIn("工具结果", context.prompts[1])
        self.assertIn("b.blkx", context.prompts[1])
        self.assertIn("+b", context.prompts[1])
        self.assertIn("补充上下文分析", [metadata.get("用途") for event, metadata in events if event == "模型请求开始"])
        self.assertIn("模型工具调用", [event for event, _metadata in events])

    async def test_read_changed_file_fetches_github_when_local_content_missing(self) -> None:
        summary = make_summary()
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
        first = analysis("武器调整", "初次分析")
        first["tool_calls"] = [
            {"tool": "read_changed_file", "path": "extra/foo.blkx", "reason": "需要完整文件"}
        ]
        context = SequenceContext(
            [
                json_response(first),
                json_response(analysis("武器调整", "GitHub 补充后分析")),
            ]
        )
        settings = load_config({"enable_model_tool_calls": True, "max_tool_call_rounds": 1})

        with patch("wtup.analysis.tools.GitHubClient.get_file_text", return_value="FULL FILE CONTENT") as fetch:
            results = await analyze_chunks(context, settings, single_summary)

        self.assertEqual(results[0].analysis["summary"], "GitHub 补充后分析")
        fetch.assert_called_once_with("gszabi99/War-Thunder-Datamine", "head456", "extra/foo.blkx")
        self.assertIn("FULL FILE CONTENT", context.prompts[1])

    async def test_read_changed_file_reuses_disk_cache_between_runs(self) -> None:
        summary = make_summary()
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
        first = analysis("武器调整", "初次分析")
        first["tool_calls"] = [
            {"tool": "read_changed_file", "path": "extra/foo.blkx", "reason": "需要完整文件"}
        ]

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            settings = replace(
                load_config({"enable_model_tool_calls": True, "max_tool_call_rounds": 1}),
                github_cache_dir=base / "github_cache",
            )
            first_context = SequenceContext(
                [
                    json_response(first),
                    json_response(analysis("武器调整", "GitHub 补充后分析")),
                ]
            )
            with patch("wtup.analysis.tools.GitHubClient.get_file_text", return_value="FULL FILE CONTENT") as fetch:
                results = await analyze_chunks(first_context, settings, single_summary)

            self.assertEqual(results[0].analysis["summary"], "GitHub 补充后分析")
            fetch.assert_called_once()
            self.assertEqual(len(list((base / "github_cache" / "files" / "head456").glob("*.txt"))), 1)

            second_context = SequenceContext(
                [
                    json_response(first),
                    json_response(analysis("武器调整", "缓存补充后分析")),
                ]
            )
            with patch(
                "wtup.analysis.tools.GitHubClient.get_file_text",
                side_effect=AssertionError("should use disk cache"),
            ) as fetch_again:
                results = await analyze_chunks(second_context, settings, single_summary)

            self.assertEqual(results[0].analysis["summary"], "缓存补充后分析")
            fetch_again.assert_not_called()
            self.assertIn("FULL FILE CONTENT", second_context.prompts[1])

    async def test_tool_call_round_limit_adds_uncertainty(self) -> None:
        summary = make_summary()
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
        first = analysis("武器调整", "初次分析")
        first["tool_calls"] = [{"tool": "read_changed_patch", "path": "b.blkx", "reason": "第一次补充"}]
        second = analysis("武器调整", "仍需补充")
        second["tool_calls"] = [{"tool": "read_changed_patch", "path": "c.blkx", "reason": "还需要补充"}]
        context = SequenceContext([json_response(first), json_response(second)])
        settings = load_config({"enable_model_tool_calls": True, "max_tool_call_rounds": 1})

        results = await analyze_chunks(context, settings, single_summary)

        self.assertEqual(context.calls, 2)
        self.assertEqual(results[0].analysis["tool_calls"], [])
        self.assertIn("模型工具调用轮数已达上限", results[0].analysis["ai_analysis"]["uncertainties"][0])

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
                json_response(with_source_ids(analysis("武器调整", "前半"), "C001-001", "C002-001")),
                json_response(with_source_ids(analysis("经济调整", "后半"), "C003-001", "C004-001")),
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
                json_response(with_source_ids(analysis("经济调整", "后半"), "C003-001", "C004-001")),
                json_response(with_source_ids(analysis("武器调整", "A"), "C001-001")),
                json_response(with_source_ids(analysis("武器调整", "B"), "C002-001")),
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

    def test_split_files_packs_related_directory_groups_before_other_files(self) -> None:
        files = [
            {"filename": "units/tank_1.blkx", "patch": "+a"},
            {"filename": "weapons/gun.blkx", "patch": "+b"},
            {"filename": "units/tank_2.blkx", "patch": "+c"},
            {"filename": "units/plane.blkx", "patch": "+d"},
        ]
        chunks = split_files(files, max_files=2)

        self.assertEqual(
            [[file["filename"] for file in chunk.files] for chunk in chunks],
            [["units/tank_1.blkx", "units/tank_2.blkx"], ["units/plane.blkx", "weapons/gun.blkx"]],
        )

    def test_split_files_groups_same_entity_across_directories(self) -> None:
        files = [
            {"filename": "data/entity_alpha.blkx", "patch": "+a"},
            {"filename": "units/plane.blkx", "patch": "+b"},
            {"filename": "lang/entity_alpha.csv", "patch": "+c"},
        ]
        chunks = split_files(files, max_files=2)

        self.assertEqual(
            [[file["filename"] for file in chunk.files] for chunk in chunks],
            [["data/entity_alpha.blkx", "lang/entity_alpha.csv"], ["units/plane.blkx"]],
        )

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

    def test_token_limit_splits_on_related_group_boundaries_first(self) -> None:
        files = [
            {"filename": "units/tank_1.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {"filename": "weapons/gun.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+b"},
            {"filename": "units/tank_2.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+c"},
            {"filename": "units/plane.blkx", "status": "modified", "additions": 1, "deletions": 0, "patch": "+d"},
        ]
        base_chunks = split_files(files)
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
            [[file["filename"] for file in chunk.files] for chunk in split_summary.chunks],
            [
                ["units/tank_1.blkx", "units/tank_2.blkx"],
                ["units/plane.blkx", "weapons/gun.blkx"],
            ],
        )


def json_response(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def with_source_ids(payload: dict, *source_ids: str) -> dict:
    sections = payload.get("update_sections") if isinstance(payload.get("update_sections"), list) else []
    if sections and isinstance(sections[0], dict):
        items = sections[0].get("items") if isinstance(sections[0].get("items"), list) else []
        if items and isinstance(items[0], dict):
            items[0]["source_ids"] = list(source_ids)
    return payload


def empty_choice_response(finish_reason: str):
    message = SimpleNamespace(content="")
    choice = SimpleNamespace(finish_reason=finish_reason, message=message)
    return SimpleNamespace(choices=[choice])


if __name__ == "__main__":
    unittest.main()
