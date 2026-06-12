from __future__ import annotations

from .client import generate_analysis_from_prompt, request_llm
from .errors import record_model_error
from .fallback import fallback_analysis
from .merge import (
    clean_section_title,
    coerce_analysis,
    dedupe_update_items,
    first_recommendation_by_importance,
    first_text,
    get_ai_analysis,
    max_importance,
    merge_chunk_analyses,
    merge_update_sections,
    order_chunk_results,
    unique_preserve_order,
)
from .models import ChunkAnalysis
from .normalize import (
    first_non_empty_line,
    normalize_ai_analysis,
    normalize_analysis,
    normalize_importance,
    normalize_list,
    normalize_update_items,
    normalize_update_sections,
    parse_analysis_json,
    safe_normalize_analysis,
)
from .prompts import (
    build_chunk_refinement_payload,
    build_chunk_refinement_prompt,
    build_json_repair_prompt,
    build_prompt,
    build_refinement_prompt,
    json_safe,
)
from .repair import parse_or_repair_analysis
from .responses import ensure_usable_llm_response, extract_response_text, llm_failure_reason
from .retry import (
    analyze_chunk,
    analyze_chunk_once,
    analyze_chunk_with_retry,
    analyze_chunk_with_retry_attempt,
    analyze_chunk_without_retry,
    analyze_chunks,
    refine_chunk_analyses,
    refine_merged_analysis,
    split_chunk_for_retry,
)
from .tokens import (
    estimate_chunk_input_tokens,
    estimate_input_tokens,
    file_patch_chars,
    split_chunks_by_token_limit,
)

__all__ = [
    "ChunkAnalysis",
    "analyze_chunk",
    "analyze_chunk_once",
    "analyze_chunk_with_retry",
    "analyze_chunk_with_retry_attempt",
    "analyze_chunk_without_retry",
    "analyze_chunks",
    "build_chunk_refinement_payload",
    "build_chunk_refinement_prompt",
    "build_json_repair_prompt",
    "build_prompt",
    "build_refinement_prompt",
    "clean_section_title",
    "coerce_analysis",
    "dedupe_update_items",
    "ensure_usable_llm_response",
    "estimate_chunk_input_tokens",
    "estimate_input_tokens",
    "extract_response_text",
    "fallback_analysis",
    "file_patch_chars",
    "first_non_empty_line",
    "first_recommendation_by_importance",
    "first_text",
    "generate_analysis_from_prompt",
    "get_ai_analysis",
    "json_safe",
    "llm_failure_reason",
    "max_importance",
    "merge_chunk_analyses",
    "merge_update_sections",
    "normalize_ai_analysis",
    "normalize_analysis",
    "normalize_importance",
    "normalize_list",
    "normalize_update_items",
    "normalize_update_sections",
    "order_chunk_results",
    "parse_analysis_json",
    "parse_or_repair_analysis",
    "record_model_error",
    "refine_chunk_analyses",
    "refine_merged_analysis",
    "request_llm",
    "safe_normalize_analysis",
    "split_chunk_for_retry",
    "split_chunks_by_token_limit",
    "unique_preserve_order",
]
