"""Static speech limits with provider caps separated from application policies."""

from __future__ import annotations

from typing import Any

_CHECKED_AT = "2026-10-07"
_OPENAI_SPEECH = "https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create"
_OPENAI_MINI = "https://developers.openai.com/api/docs/models/gpt-4o-mini-tts"
_AZURE_QUOTAS = "https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits"
_GEMINI_MODELS = {
    "gemini-3.8-flash-tts": "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-tts",
    "gemini-3.1-flash-tts-preview": "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts": "https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts": "https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro-preview-tts",
    "gemini-2.5-flash-tts": "https://docs.cloud.google.com/text-to-speech/docs/gemini-tts",
    "gemini-2.5-pro-tts": "https://docs.cloud.google.com/text-to-speech/docs/gemini-tts",
}
_RUNTIME_CHUNKS = {"qwen3_tts": 8192, "voxcpm2": 2048, "breeze_tts": 600}
_RUNTIME_OUTPUT_BUDGETS = {
    "qwen3_tts": (2048, "codec_frames"),
    "breeze_tts": (1500, "acoustic_frames"),
}


def _normalised(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def generation_limit_metadata(
    *, service: str, model: str, family: str = "", adapter: str = ""
) -> dict[str, Any]:
    """Return owned metadata without discovering providers or active runtimes.

    Runtime values describe the pinned audio.cpp defaults. Only documented
    provider caps are returned as input/output limits; token budgets are never
    converted to hard transcript character limits. The default segment policy
    is language-neutral; preparation resolves language-specific policies.
    """

    # Catalogue construction also participates in TTS imports. Keep the shared
    # local policy import lazy rather than introducing a module import cycle.
    from .audiobook_chunking import MAX_MANUAL_LENGTH, audiobook_chunk_budget

    policy_model = model or family
    settings: dict[str, Any] = {
        "service": service,
        "model": policy_model,
        "adapter": adapter,
    }
    if family:
        settings["model_catalog"] = [{"id": policy_model, "family": family}]
    budget = audiobook_chunk_budget({"audiobook_chunking": "model"}, settings)
    profile = budget["profile"]
    result: dict[str, Any] = {
        "default_segment_characters": budget["target_chars"],
        "policy_max_segment_characters": MAX_MANUAL_LENGTH,
        "checked_at": _CHECKED_AT,
        "source_urls": [],
    }
    sources: list[str] = []
    model_id = model.strip().casefold()
    mini = model_id == "gpt-4o-mini-tts" or model_id.startswith("gpt-4o-mini-tts-")
    if mini or model_id in {"tts-1", "tts-1-hd"}:
        result["input_characters"] = 4096
        sources.append(_OPENAI_SPEECH)
        if mini:
            result["input_tokens"] = 2000
            sources.append(_OPENAI_MINI)
    if model_id in _GEMINI_MODELS:
        result["input_tokens"] = 8192
        result["output_tokens"] = 16384
        sources.append(_GEMINI_MODELS[model_id])
    if profile == "azure_speech":
        result["output_seconds"] = 600
        sources.append(_AZURE_QUOTAS)

    audio_cpp = _normalised(service) in {"audio_cpp", "audio.cpp", "audiocpp"} or _normalised(adapter) == "audio_cpp"
    runtime_family = family.strip().casefold() or profile
    if audio_cpp and runtime_family in _RUNTIME_CHUNKS:
        result["runtime_chunk_characters"] = _RUNTIME_CHUNKS[runtime_family]
        sources.append(
            "https://github.com/0xShug0/audio.cpp/blob/v0.9.0/"
            f"src/models/{runtime_family}/session.cpp"
        )
        if runtime_family in _RUNTIME_OUTPUT_BUDGETS:
            value, unit = _RUNTIME_OUTPUT_BUDGETS[runtime_family]
            result["runtime_output_budget"] = {"value": value, "unit": unit}
    result["source_urls"] = sources
    return result


__all__ = ["generation_limit_metadata"]
