from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from wtup.analyzer import (
    ChunkAnalysis,
    analyze_chunks,
    build_chunk_refinement_payload,
    llm_failure_reason,
    merge_chunk_analyses,
    refine_chunk_analyses,
    refine_merged_analysis,
    safe_normalize_analysis,
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


class ConfigTest(unittest.TestCase):
    def test_second_pass_config_defaults_to_disabled(self) -> None:
        settings = load_config({})

        self.assertFalse(settings.enable_second_pass_analysis)

    def test_second_pass_config_accepts_bool_like_values(self) -> None:
        settings = load_config({"enable_second_pass_analysis": "开启"})

        self.assertTrue(settings.enable_second_pass_analysis)

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

    async def llm_generate(self, **kwargs):
        self.calls += 1
        return self.response


def make_settings(*, model_concurrency: int = 2) -> PluginConfig:
    return PluginConfig(
        provider_id="",
        timeout_seconds=5,
        model_concurrency=model_concurrency,
        analysis_prompt="请分析更新。",
        enable_second_pass_analysis=True,
        target_groups=[],
        monitor_interval_minutes=30,
        github_token="",
        max_files_per_report=1,
        max_patch_chars=0,
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
        self.assertIn("程序合并和二次分析均失败", refined["summary"])

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
        self.active = 0
        self.max_active = 0

    async def llm_generate(self, **kwargs):
        self.calls += 1
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

        self.assertEqual([file["filename"] for file in chunks[0].files], ["units/tank_1.blkx", "units/tank_2.blkx"])


def json_response(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def empty_choice_response(finish_reason: str):
    message = SimpleNamespace(content="")
    choice = SimpleNamespace(finish_reason=finish_reason, message=message)
    return SimpleNamespace(choices=[choice])


if __name__ == "__main__":
    unittest.main()
