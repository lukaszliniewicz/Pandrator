"""Usage estimates returned by configured commercial TTS providers."""

from typing import TypedDict


class TtsUsageEstimate(TypedDict):
    provider: str
    model: str
    commercial: bool
    estimated: bool
    cost_usd: float | None
    cost_source: str
    input_characters: int
    input_tokens: int
    output_audio_tokens: int
    duration_ms: int
