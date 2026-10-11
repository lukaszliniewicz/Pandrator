"""Public segment fields; parity is checked without an application import.

The standalone MCP distribution must work without Pandrator/SQLAlchemy. Keep
this schema mirror equal to the authoritative application projection contract.
"""

from typing import Literal

GenerationSegmentField = Literal[
    "id", "ordinal", "revision", "text", "source_segment_ids", "status", "removed",
    "optimization_status", "take_count", "active_take_id", "has_usable_take",
    "audio_reuse_reason", "has_reusable_take", "history_take_id", "node_kind",
    "paragraph_break_after", "speaker", "speech_block_provenance", "alignment_group",
    "optimized_text", "speech_plan", "optimization_reviewed", "optimization_model",
    "voice_id", "voice", "language", "silence_after_ms", "marked", "takes",
    "search_matches", "passage_structure", "take_history_loaded", "effective_take",
]
