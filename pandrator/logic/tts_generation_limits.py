"""Static speech limits with provider caps separated from application policies."""

from __future__ import annotations

from typing import Any

from .audio_cpp_segmentation import (
    NATIVE_SPLIT_ONLY_FAMILIES,
    REVIEWED_RUNTIME,
    native_character_chunk_default,
)

_CHECKED_AT = "2026-10-09"
_OPENAI_SPEECH = "https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create"
_OPENAI_MINI = "https://developers.openai.com/api/docs/models/gpt-4o-mini-tts"
_AZURE_QUOTAS = "https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits"
_GEMINI_MODELS = {
    "gemini-3.8-flash-tts": "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts": "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-lite-tts",
    "gemini-3.1-flash-tts-preview": "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts": "https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts": "https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro-preview-tts",
    "gemini-2.5-flash-tts": "https://docs.cloud.google.com/text-to-speech/docs/gemini-tts",
    "gemini-2.5-flash-lite-preview-tts": "https://docs.cloud.google.com/text-to-speech/docs/gemini-tts",
    "gemini-2.5-pro-tts": "https://docs.cloud.google.com/text-to-speech/docs/gemini-tts",
}
_RUNTIME_OUTPUT_BUDGETS = {
    "breeze_tts": (1500, "acoustic_frames"),
    "chatterbox": (384, "speech_tokens"),
    "fish_audio": (1024, "semantic_tokens_per_chunk"),
    "fish_audio_s2": (1024, "semantic_tokens_per_chunk"),
    "voxcpm2": (4096, "audio_tokens"),
    "voxtral_tts": (2048, "audio_frames"),
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
        sources.append(
            "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/"
            + model_id.removeprefix("gemini-").replace(".", "-")
            if _normalised(service) == "vertex_ai" and model_id.startswith("gemini-3.8-")
            else _GEMINI_MODELS[model_id]
        )
    if profile == "azure_speech":
        result["output_seconds"] = 600
        sources.append(_AZURE_QUOTAS)
    if model_id in {"eleven_v4", "eleven_v4_turbo"}:
        # Pandrator's one-turn dialogue requests follow the provider's reliable
        # request recommendation, including compiled inline tags.
        result["input_characters"] = 2000
        result["model_input_characters"] = 10000 if model_id == "eleven_v4" else None
        result["input_limit_note"] = "Dialogue requests are limited to 2000 characters including inline directions."
        sources.extend([
            "https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert",
            "https://elevenlabs.io/docs/overview/models",
        ])
    elif model_id == "eleven_v3":
        result["input_characters"] = 5000
        sources.append("https://elevenlabs.io/docs/overview/models")
    elif model_id == "eleven_multilingual_v2":
        result["input_characters"] = 10000
        sources.append("https://elevenlabs.io/docs/overview/models")
    elif model_id in {"eleven_flash_v2_5", "eleven_turbo_v2_5"}:
        result["input_characters"] = 40000
        sources.append("https://elevenlabs.io/docs/overview/models")

    audio_cpp = _normalised(service) in {"audio_cpp", "audio.cpp", "audiocpp"} or _normalised(adapter) == "audio_cpp"
    runtime_family = family.strip().casefold() or profile
    if audio_cpp:
        result["checked_at"] = "2026-10-16"
        result["segmentation_owner"] = "pandrator"
        result["single_segment_supported"] = runtime_family not in NATIVE_SPLIT_ONLY_FAMILIES
        native_chunk = native_character_chunk_default(runtime_family)
        if native_chunk is not None:
            result["runtime_chunk_characters"] = native_chunk
        if "target_words" in budget:
            result["default_segment_words"] = budget["target_words"]
        sources.append(
            f"https://github.com/0xShug0/audio.cpp/blob/v{REVIEWED_RUNTIME}/"
            f"model_specs/{'fish_audio' if runtime_family == 'fish_audio_s2' else runtime_family}.json"
        )
        if runtime_family in _RUNTIME_OUTPUT_BUDGETS:
            value, unit = _RUNTIME_OUTPUT_BUDGETS[runtime_family]
            result["runtime_output_budget"] = {"value": value, "unit": unit}
        if runtime_family == "qwen3_tts":
            result["runtime_output_budget"] = {"fallback_value": 2048, "unit": "codec_frames", "source": "checkpoint_generation_config"}
        if runtime_family == "chatterbox":
            result["runtime_output_budget"]["multilingual_token_duration_ms"] = 40
        if runtime_family == "voxtral_tts":
            result["runtime_output_budget"]["frame_duration_ms"] = 80
            sources.append("https://docs.mistral.ai/studio/audio/text_to_speech/speech")
        if runtime_family == "pocket_tts":
            result["runtime_chunk_text_tokens"] = 50
        if runtime_family == "confucius4_tts":
            result["runtime_chunk_text_tokens"] = 80
        if runtime_family == "irodori_tts":
            result["input_tokens"] = 256
            result["input_limit_note"] = "The normalized text and BOS must fit the 256-token checkpoint window. Pandrator applies a conservative UTF-8 byte bound to prevent native truncation."
        if runtime_family in {"fish_audio", "fish_audio_s2"}:
            result["application_runtime_chunk_characters"] = 300
            result["runtime_chunk_note"] = "Pandrator supplies at least 300 when unset; audio.cpp's native default is 200. The request budget expands to keep the compiled Pandrator segment whole."
    result["source_urls"] = sources
    return result


__all__ = ["generation_limit_metadata"]
