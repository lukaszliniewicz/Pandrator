"""Stable bindings between Pandrator provider profiles and manager services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ManagedTtsBinding:
    provider_id: str
    component_id: str
    service_id: str
    settings_url_key: str


MANAGED_TTS_BINDINGS: dict[str, ManagedTtsBinding] = {
    binding.provider_id: binding
    for binding in (
        ManagedTtsBinding("xtts", "xtts", "tts.xtts", "xtts_base_url"),
        ManagedTtsBinding("voxcpm", "voxcpm", "tts.voxcpm", "voxcpm_base_url"),
        ManagedTtsBinding(
            "fishs2",
            "fish_speech",
            "tts.fish_speech",
            "fishs2_base_url",
        ),
        ManagedTtsBinding(
            "voxtral",
            "voxtral",
            "tts.voxtral",
            "voxtral_base_url",
        ),
        ManagedTtsBinding("kokoro", "kokoro", "tts.kokoro", "kokoro_base_url"),
        ManagedTtsBinding("silero", "silero", "tts.silero", "silero_base_url"),
        ManagedTtsBinding(
            "chatterbox",
            "chatterbox",
            "tts.chatterbox",
            "chatterbox_base_url",
        ),
        ManagedTtsBinding(
            "kobold_qwen",
            "qwen_tts",
            "tts.qwen",
            "kobold_qwen_base_url",
        ),
        ManagedTtsBinding("magpie", "magpie", "tts.magpie", "magpie_base_url"),
    )
}


def normalize_tts_provider_id(value: object) -> str:
    normalized = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    return {
        "qwen": "kobold_qwen",
        "qwen3": "kobold_qwen",
        "qwen3_tts": "kobold_qwen",
        "kobold_qwen3": "kobold_qwen",
    }.get(normalized, normalized)


def binding_for_provider(value: object) -> ManagedTtsBinding | None:
    return MANAGED_TTS_BINDINGS.get(normalize_tts_provider_id(value))
