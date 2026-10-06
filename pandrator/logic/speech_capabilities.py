"""Pure model and route performance capability profiles.

This module has no provider, compiler, catalogue, or application imports.
"""

from __future__ import annotations

import re
from typing import Any

CAPABILITY_VERSION = "2026-09-23.1"

_FISH_EVENTS = {
    "pause": "pause",
    "laugh": "laughing",
    "chuckle": "chuckle",
    "sigh": "sigh",
    "inhale": "inhale",
    "exhale": "exhale",
    "clear_throat": "clearing throat",
}
_GEMINI_EVENTS = {
    "pause": "pause",
    "laugh": "laughs",
    "chuckle": "chuckles",
    "sigh": "sighs",
    "cough": "cough",
    "gasp": "gasp",
}
_GEMINI38_EVENTS = {
    "pause": "short pause",
    "laugh": "laugh",
    "chuckle": "chuckle",
    "sigh": "sigh",
    "inhale": "breath",
    "exhale": "exhales",
    "cough": "cough",
    "gasp": "gasp",
    "clear_throat": "throat-clearing",
}
_TURBO_EVENTS = {"laugh": "laugh", "chuckle": "chuckle", "cough": "cough"}
_BREEZE_EVENTS = {"laugh": "laugh", "cough": "cough", "clear_throat": "clears throat", "sigh": "sigh"}
_BREEZE_ZH_EVENTS = {"laugh": "笑", "cough": "咳嗽", "clear_throat": "清嗓子", "sigh": "叹气"}
_ELEVEN_V3_EVENTS = {"laugh": "laughs", "sigh": "sighs", "clear_throat": "clears throat"}
_ELEVEN_STITCHING_MODELS = {
    "eleven_multilingual_v2",
    "eleven_flash_v2",
    "eleven_flash_v2_5",
    "eleven_turbo_v2",
    "eleven_turbo_v2_5",
}


def capabilities_for_model(
    model: str,
    *,
    backend: str = "",
    family: str = "",
    voice_mode: str = "",
    backend_version: str = "",
) -> dict[str, Any]:
    """Conservative known model/route intersection, not a family-wide promise.

    The profiles are documented, not acoustically certified. Unrecognized
    endpoints/variants stay unknown. Exact tag spellings are model-specific.
    """
    normalized = re.sub(r"[^a-z0-9]+", "_", model.casefold()).strip("_")
    route = re.sub(r"[^a-z0-9]+", "_", backend.casefold()).strip("_")
    family = family.casefold()
    profile: dict[str, Any] = {
        "schema_version": 1,
        "profile_version": CAPABILITY_VERSION,
        "model": model,
        "backend": backend,
        "backend_version": backend_version,
        "voice_mode": voice_mode,
        "status": "unknown",
        "dialect": "none",
        "instructions": "none",
        "instruction_scope": [],
        "voice_design": False,
        "emotion": {"mode": "none", "tags": []},
        "event_tags": {},
        "semantic_context": "none",
        "acoustic_context": "not_enabled",
        "timing": "not_guaranteed",
        "notes": [],
    }
    audio_cpp = route in {"audio_cpp", "audiocpp", "audio_cpp_server"}
    gemini = route in {"gemini", "google_gemini", "vertex_ai", "google_vertex_ai"}
    elevenlabs = route in {"elevenlabs", "elevenlabs_native"}
    if elevenlabs and model == "eleven_v3":
        profile.update(
            status="documented",
            dialect="eleven_v3",
            instructions="inline",
            instruction_scope=["request", "span"],
            emotion={"mode": "open_description", "tags": []},
            event_tags=dict(_ELEVEN_V3_EVENTS),
        )
        profile["notes"].append(
            "Inline directions and restoration are model-interpreted; no hard scope, reset, or timing guarantee."
        )
        profile["notes"].append(
            "Provider documentation differs on non-stability voice settings for Eleven v3; these request options may be ignored by the selected model."
        )
    elif elevenlabs and model in _ELEVEN_STITCHING_MODELS:
        profile.update(status="documented", semantic_context="field")
        profile["notes"].append(
            "Previous and next text are unspoken stitching context, subject to model interpretation."
        )
    elif gemini and normalized in {
        "gemini_3_8_flash_tts",
        "gemini_gemini_3_8_flash_tts",
        "vertex_ai_gemini_3_8_flash_tts",
        "models_gemini_3_8_flash_tts",
    }:
        profile.update(
            status="documented",
            dialect="gemini38",
            instructions="field",
            instruction_scope=["request", "span"],
            semantic_context="prompt",
            emotion={"mode": "open_description", "tags": []},
            event_tags=dict(_GEMINI38_EVENTS),
            event_format="angle_brackets",
        )
        profile["notes"].append(
            "Directions and optional context use unspoken speech metadata; phrase direction and pause duration remain soft hints."
        )
    elif gemini and "tts" in normalized:
        profile.update(
            status="documented",
            dialect="gemini",
            instructions="prompt",
            instruction_scope=["request", "span"],
            semantic_context="prompt",
            emotion={
                "mode": "open_description",
                "tags": ["excited", "serious", "whispers", "curious"],
            },
            event_tags=dict(_GEMINI_EVENTS),
        )
        profile["notes"].append(
            "Context separation and inline scope are prompt-mediated, not hard guarantees."
        )
    elif (
        audio_cpp
        and (family in {"fish_audio", "fish_audio_s2"} or "fish_audio_s2" in normalized)
    ) or route == "fishs2":
        profile.update(
            status="documented",
            dialect="fish_s2",
            instructions="inline",
            instruction_scope=["request", "span"],
            emotion={
                "mode": "open_description",
                "tags": ["excited", "angry", "sad", "whisper"],
            },
            event_tags=dict(_FISH_EVENTS),
        )
        profile["notes"].append(
            "Free-form inline tags; scope, resets and pause duration are soft hints."
        )
    elif audio_cpp and (family == "qwen3_tts" or "qwen3" in normalized):
        profile["status"] = "documented"
        is_large = "1_7b" in normalized
        if is_large and ("customvoice" in normalized or "voicedesign" in normalized):
            profile.update(
                dialect="qwen",
                instructions="field",
                instruction_scope=["request"],
                voice_design="voicedesign" in normalized,
                emotion={"mode": "open_description", "tags": []},
            )
        else:
            profile["notes"].append(
                "Qwen Base/cloning and 0.6B CustomVoice do not support this instruction path."
            )
    elif route == "kobold_qwen" and normalized in {
        "prebuilt_voices",
        "qwen3_tts_customvoice",
    }:
        profile.update(
            status="documented",
            dialect="qwen",
            instructions="field",
            instruction_scope=["request"],
            emotion={"mode": "open_description", "tags": []},
        )
        profile["notes"].append(
            "Requires the backend's instruction-capable CustomVoice model; not the cloning path."
        )
    elif route in {"openai", "openai_audio"} and (
        normalized == "gpt_4o_mini_tts" or normalized.startswith("gpt_4o_mini_tts_")
    ):
        profile.update(
            status="documented",
            dialect="instructions",
            instructions="field",
            instruction_scope=["request"],
            emotion={"mode": "open_description", "tags": []},
        )
    elif (route == "chatterbox" and normalized in {"chatterbox_turbo", "turbo"}) or (audio_cpp and family == "chatterbox_turbo"):
        profile.update(
            status="documented",
            dialect="chatterbox_tags",
            event_tags={"laugh": "laugh", "sigh": "sigh"} if audio_cpp else dict(_TURBO_EVENTS),
        )
        profile["notes"].append(
            "Only the listed vocal-event tags are compiled; this is not free-form instruction support."
        )
    elif audio_cpp and family == "breeze_tts":
        # Existing Pandrator reference-free/design route accepts instructions.
        profile.update(
            status="documented",
            dialect="instructions",
            instructions="field",
            instruction_scope=["request"],
            voice_design=True,
            emotion={"mode": "open_description", "tags": []},
            event_tags=dict(_BREEZE_EVENTS),
            event_format="parentheses",
        )
    elif audio_cpp and family == "supertonic":
        profile.update(
            status="documented", dialect="none",
        )
    elif audio_cpp and family == "omnivoice":
        profile.update(
            status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], voice_design=True,
            emotion={"mode": "open_description", "tags": []},
            event_tags={"laugh": "laughter", "sigh": "sigh"},
        )
    elif audio_cpp and family == "fireredtts3" and "instruct" in normalized:
        profile.update(
            status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], voice_design=True,
            emotion={"mode": "open_description", "tags": []},
        )
    elif audio_cpp and family == "moss_voicegen":
        profile.update(status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], voice_design=True,
            emotion={"mode": "open_description", "tags": []})
    elif audio_cpp and family == "irodori_tts" and "voicedesign" in normalized:
        profile.update(status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], voice_design=True,
            emotion={"mode": "open_description", "tags": []})
    elif audio_cpp and family == "neutts":
        profile.update(status="documented", dialect="neutts",
            emotion={"mode": "enum", "tags": ["angry", "disgusted", "sad", "happy", "fearful", "neutral", "surprised"]})
    elif audio_cpp and family == "cosyvoice3":
        profile.update(
            status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"],
            emotion={"mode": "open_description", "tags": []},
        )
    elif audio_cpp and family == "voxcpm2":
        profile.update(
            status="documented", dialect="voxcpm2", instructions="inline",
            instruction_scope=["request"], voice_design=True,
            emotion={"mode": "open_description", "tags": []},
        )
        profile["notes"].append("Voice/style descriptions are parenthesized input prefixes; phrase scope is not established.")
    return profile


def decorate_service_capabilities(service: dict[str, Any]) -> None:
    """Expose one backend-authoritative capability view to UI and MCP clients."""
    route = str(service.get("adapter") or "")
    if route not in {"audio_cpp", "elevenlabs_native"}:
        route = str(service.get("provider") or service.get("id") or "")
    catalog = service.get("model_catalog") or []
    metadata = {
        str(item["id"]): item
        for item in catalog
        if isinstance(item, dict) and item.get("id")
    }
    model_ids = list(dict.fromkeys([*service.get("models", []), *metadata]))
    profiles = {
        model: capabilities_for_model(
            model,
            backend=route,
            family=str(metadata.get(model, {}).get("family") or ""),
            voice_mode=str(metadata.get(model, {}).get("voice_mode") or ""),
            backend_version=str(service.get("backend_version") or ""),
        )
        for model in model_ids
        if isinstance(model, str)
    }
    service["expressive_capabilities"] = profiles
    for item in catalog:
        if isinstance(item, dict) and item.get("id") in profiles:
            item["expressive_capabilities"] = profiles[item["id"]]
    # Keep the legacy UI field as a projection, not a competing capability model.
    service["generation_prompt_models"] = [
        model
        for model, profile in profiles.items()
        if profile["instructions"] != "none"
    ]
