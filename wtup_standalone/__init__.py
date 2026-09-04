from __future__ import annotations

from .analyzer import DatamineAnalyzer
from .config import AnalyzerConfig
from .models import AnalysisResult, ChunkAnalysis, DiffChunk, DiffSummary, TokenUsage
from .diff_collector import build_diff_summary, parse_unified_diff
from .struct_diff import build_struct_compare, StructCompareResult
from .normalize import safe_normalize_analysis, parse_analysis_json
from .fallback import fallback_analysis
from .token_usage import format_token_usage_text

__version__ = "0.1.0"

__all__ = [
    "DatamineAnalyzer",
    "AnalyzerConfig",
    "AnalysisResult",
    "ChunkAnalysis",
    "DiffChunk",
    "DiffSummary",
    "TokenUsage",
    "build_diff_summary",
    "parse_unified_diff",
    "build_struct_compare",
    "StructCompareResult",
    "safe_normalize_analysis",
    "parse_analysis_json",
    "fallback_analysis",
    "format_token_usage_text",
]
