from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChunkAnalysis:
    chunk_index: int
    chunk_total: int
    analysis: dict[str, Any]
    error: str = ""
    raw_text: str = ""
