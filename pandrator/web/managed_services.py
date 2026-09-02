"""Stable bindings between Pandrator provider profiles and manager services."""

from __future__ import annotations

import json
from collections.abc import Mapping
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
        ManagedTtsBinding(
            "audio_cpp",
            "audio_cpp",
            "tts.audio_cpp",
            "audio_cpp_base_url",
        ),
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
        "audio.cpp": "audio_cpp",
        "audiocpp": "audio_cpp",
        "fish_s2": "fishs2",
        "fish_speech": "fishs2",
        "fishs2_cpp": "fishs2",
        "fishs2cpp": "fishs2",
        "voxcpm2": "voxcpm",
        "qwen": "kobold_qwen",
        "qwen3": "kobold_qwen",
        "qwen3_tts": "kobold_qwen",
        "kobold_qwen3": "kobold_qwen",
    }.get(normalized, normalized)


def binding_for_provider(value: object) -> ManagedTtsBinding | None:
    return MANAGED_TTS_BINDINGS.get(normalize_tts_provider_id(value))


def configured_tts_provider_ids(
    *settings_values: Mapping[str, object] | None,
) -> frozenset[str]:
    """Return providers for which the user has saved an endpoint policy.

    A stored provider record is an explicit choice even when it predates the
    ``connection_mode`` field.  Treating those legacy records as external
    prevents a Manager installation from silently replacing a user endpoint.
    """

    configured: set[str] = set()

    def remember(record: object) -> None:
        if not isinstance(record, Mapping):
            return
        raw_id = record.get("id") or record.get("name") or record.get("provider")
        provider_id = normalize_tts_provider_id(raw_id)
        if provider_id:
            configured.add(provider_id)

    for settings in settings_values:
        if not isinstance(settings, Mapping):
            continue
        for key in ("provider_configs", "service_configs"):
            records = settings.get(key)
            if isinstance(records, list):
                for record in records:
                    remember(record)

        legacy = str(settings.get("openai_audio_endpoints_json") or "").strip()
        if not legacy:
            continue
        try:
            records = json.loads(legacy)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(records, list):
            for record in records:
                remember(record)

    return frozenset(configured)


def effective_tts_connection_mode(
    service: Mapping[str, object],
    *,
    configured_provider_ids: frozenset[str],
    manager_configured: bool,
) -> str:
    """Resolve the endpoint owner without overriding explicit user intent."""

    requested = str(service.get("connection_mode") or "").strip().lower()
    if requested in {"external", "managed_local"}:
        return requested

    provider_id = normalize_tts_provider_id(
        service.get("id") or service.get("name")
    )
    if (
        manager_configured
        and provider_id not in configured_provider_ids
        and binding_for_provider(provider_id) is not None
    ):
        return "managed_local"
    return "external"
