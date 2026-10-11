"""Pure model and route performance capability profiles.

This module has no provider, compiler, catalogue, or application imports.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

CAPABILITY_VERSION = "2026-10-10.2"

_FISH_EVENTS = {
    "pause": "pause",
    "laugh": "laughing",
    "chuckle": "chuckle",
    "sigh": "sigh",
    "inhale": "inhale",
    "exhale": "exhale",
    "clear_throat": "clearing throat",
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
_TURBO_EVENTS = {
    "laugh": "laugh",
    "chuckle": "chuckle",
    "cough": "cough",
    "sigh": "sigh",
    "gasp": "gasp",
    "clear_throat": "clear throat",
}
_BREEZE_EVENTS = {"laugh": "laugh", "cough": "cough", "clear_throat": "clears throat", "sigh": "sigh"}
_BREEZE_ZH_EVENTS = {"laugh": "笑", "cough": "咳嗽", "clear_throat": "清嗓子", "sigh": "叹气"}
_ELEVEN_V3_EVENTS = {
    "laugh": "laughs",
    "sigh": "sighs",
    "exhale": "exhales",
    "clear_throat": "clears throat",
}
_ELEVEN_V4_TURBO_EVENTS = {
    "laugh": "laughs",
    "sigh": "sighs",
    "clear_throat": "clears throat",
}
_ELEVEN_STITCHING_MODELS = {
    "eleven_multilingual_v2",
    "eleven_flash_v2",
    "eleven_flash_v2_5",
    "eleven_turbo_v2",
    "eleven_turbo_v2_5",
}
_ELEVEN_BREAK_MODELS = {
    "eleven_multilingual_v2",
    "eleven_flash_v2_5",
    "eleven_flash_v2",
}
_GEMINI_LEGACY_MODELS = {
    prefix + model
    for prefix in ("", "models_", "gemini_", "vertex_ai_")
    for model in (
        "gemini_3_1_flash_tts_preview",
        "gemini_2_5_flash_preview_tts",
        "gemini_2_5_flash_lite_preview_tts",
        "gemini_2_5_pro_preview_tts",
        "gemini_2_5_flash_tts",
        "gemini_2_5_pro_tts",
    )
}
_GEMINI_31_MODELS = {
    prefix + "gemini_3_1_flash_tts_preview"
    for prefix in ("", "models_", "gemini_", "vertex_ai_")
}
_MAYA_EVENTS = {"laugh": "laugh", "sigh": "sigh", "chuckle": "chuckle", "gasp": "gasp", "exhale": "exhale"}
_MAYA_MODELS = {"maya1_orig", "maya1_q8_0"}
_AUK_MODELS = {
    "auk_base_f16",
    "auk_base_f32",
    "auk_base_q8_0",
    "auk_flash_f16",
    "auk_flash_f32",
    "auk_flash_q8_0",
}
_MOSS_TTSD_MODELS = {
    "moss_ttsd_bf16_codec_f16",
    "moss_ttsd_q4_k_codec_f16",
    "moss_ttsd_q8_0_codec_f16",
}
_MOSS_TTS_MODELS = {
    "moss_tts_v15_bf16_codec_f16",
    "moss_tts_v15_q4_k_codec_f16",
    "moss_tts_v15_q8_0_codec_f16",
}
_IRODORI_DESIGN_MODELS = {
    "irodori_tts_600m_v3_voicedesign_f16",
    "irodori_tts_600m_v3_voicedesign_q8_0",
    "irodori_tts_v4_small_f16",
    "irodori_tts_v4_small_q8_0",
}
_IRODORI_500_MODELS = {"irodori_tts_500m_v3_f16", "irodori_tts_500m_v3_q8_0"}


def capability_backend(endpoint: Mapping[str, Any], fallback: str = "") -> str:
    """Resolve a native provider route without trusting a compatibility label."""
    adapter = str(endpoint.get("adapter") or "").strip().casefold()
    if adapter in {"audio_cpp", "elevenlabs_native", "azure_speech"}:
        return adapter

    provider = str(endpoint.get("provider") or "").strip().casefold()
    endpoint_id = str(endpoint.get("id") or "").strip().casefold()
    name = str(endpoint.get("name") or "").strip().casefold()
    profile_id = str(endpoint.get("profile_id") or "").strip().casefold()
    if (
        adapter == "azure_openai"
        or provider == "azure_openai"
        or endpoint_id == "azure-openai-v1"
        or profile_id == "azure-openai-v1"
    ):
        return "azure_openai"

    # Preserve Google's model-specific profiles when they are exposed through
    # an OpenAI-compatible transport.
    if provider in {"gemini", "google_gemini", "vertex_ai", "google_vertex_ai"}:
        return provider

    configured_urls = [
        str(endpoint.get(key) or "").strip()
        for key in ("base_url", "api_base")
        if str(endpoint.get(key) or "").strip()
    ]
    hosts: list[str] = []
    for value in configured_urls:
        try:
            parsed = urlparse(value if "://" in value else f"//{value}")
            hosts.append((parsed.hostname or "").casefold())
        except ValueError:
            hosts.append("")

    canonical_identity = endpoint_id == "openai" or name == "openai"
    official_url = bool(hosts) and all(host == "api.openai.com" for host in hosts)
    fixture_identity = not configured_urls and canonical_identity
    openai_identity = provider == "openai" or canonical_identity
    if openai_identity:
        if not profile_id and (official_url or fixture_identity):
            return "openai"
        return "openai_compatible"
    if adapter == "openai_compatible" and provider == "azure":
        return "azure_openai"
    return str(endpoint.get("provider") or endpoint.get("id") or fallback)


def capabilities_for_model(
    model: str,
    *,
    backend: str = "",
    family: str = "",
    voice_mode: str = "",
    backend_version: str = "",
    reference_audio: bool = False,
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
        "instruction_kind": "none",
        "instruction_transport": "",
        "requires_instruction": False,
        "uses_reference": bool(reference_audio),
        "instruction_scope": [],
        "native_instruction_scope": [],
        "phrase_instruction_transport": "none",
        "phrase_instruction_reliability": "none",
        "recommended_instruction_scope": "none",
        "voice_design": False,
        "voice_design_workflow": "none",
        "emotion": {"mode": "none", "tags": []},
        "event_tags": {},
        "semantic_context": "none",
        "semantic_context_transport": "none",
        "acoustic_context": "not_enabled",
        "timing": "not_guaranteed",
        "multi_voice": {"pandrator": "sequential_parts", "native": "not_implemented"},
        "notes": [],
    }
    audio_cpp = route in {"audio_cpp", "audiocpp", "audio_cpp_server"}
    gemini = route in {"gemini", "google_gemini", "vertex_ai", "google_vertex_ai"}
    elevenlabs = route in {"elevenlabs", "elevenlabs_native"}
    if elevenlabs and model == "eleven_v4":
        profile.update(
            status="documented",
            dialect="eleven_v4",
            instructions="inline",
            instruction_scope=["request", "span"],
            emotion={"mode": "open_description", "tags": []},
            event_tags=dict(_ELEVEN_V3_EVENTS),
            semantic_context="field",
        )
        profile["notes"].extend(
            [
                "Eleven v4 uses inline delivery tags; scope and timing remain model-interpreted.",
                "The dialogue route supports stability, similarity, and up to 100 characters of unspoken text context on either side.",
            ]
        )
        profile["event_tags"]["pause"] = "pause"
        profile["event_format"] = "inline"
        profile["event_pause"] = {
            "format": "inline",
            "short_tag": "pause",
            "long_tag": "long pause",
            "threshold_ms": 1000,
            "max_duration_ms": None,
        }
    elif elevenlabs and model == "eleven_v4_turbo":
        profile.update(
            status="documented",
            dialect="eleven_v4",
            instructions="inline",
            instruction_scope=["request", "span"],
            emotion={"mode": "open_description", "tags": []},
            event_tags=dict(_ELEVEN_V4_TURBO_EVENTS),
        )
        profile["notes"].extend(
            [
                "Eleven v4 uses inline delivery tags; scope and timing remain model-interpreted.",
                "The realtime route supports stability; hidden before/after text context is not documented.",
            ]
        )
    elif elevenlabs and model == "eleven_v3":
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
        if model in _ELEVEN_BREAK_MODELS:
            profile.update(
                event_tags={"pause": "break"},
                event_format="ssml_break",
                event_pause={"format": "ssml_break", "max_duration_ms": 3000},
            )
        profile["notes"].append(
            "Previous and next text are unspoken stitching context, subject to model interpretation."
        )
    elif gemini and normalized in {
        "gemini_3_8_flash_tts",
        "gemini_gemini_3_8_flash_tts",
        "vertex_ai_gemini_3_8_flash_tts",
        "models_gemini_3_8_flash_tts",
        "gemini_3_8_flash_lite_tts",
        "gemini_gemini_3_8_flash_lite_tts",
        "vertex_ai_gemini_3_8_flash_lite_tts",
        "models_gemini_3_8_flash_lite_tts",
    }:
        profile.update(
            status="documented",
            dialect="gemini38",
            instructions="field",
            instruction_scope=["request", "span"],
            native_instruction_scope=["request"],
            phrase_instruction_transport="quoted_hint",
            instruction_transport=(
                "speechMetadata.style" if route in {"vertex_ai", "google_vertex_ai"}
                else "speech_metadata.style"
            ),
            semantic_context="prompt",
            emotion={"mode": "open_description", "tags": []},
            event_tags=dict(_GEMINI38_EVENTS),
            event_format="angle_brackets",
            voice_design_workflow="resource_creation",
        )
        profile["notes"].append(
            "Directions and optional context use unspoken speech metadata; phrase direction and pause duration remain soft hints."
        )
    elif gemini and normalized in _GEMINI_LEGACY_MODELS:
        vertex_events = {"sigh": "sigh"} if route in {"vertex_ai", "google_vertex_ai"} else {}
        if route in {"vertex_ai", "google_vertex_ai"} and normalized in _GEMINI_31_MODELS:
            vertex_events["laugh"] = "laughs"
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
            # The Vertex migration guide documents [sigh] for earlier models.
            # Other exact legacy event spellings are not verified per route.
            event_tags=vertex_events,
        )
        profile["notes"].append(
            "Context separation and inline scope are prompt-mediated, not hard guarantees."
        )
        profile["notes"].append(
            "Only route-verified legacy vocal tags are compiled; other events remain saved."
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
                instruction_kind="voice_description" if "voicedesign" in normalized else "delivery",
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
    elif route == "kobold_qwen" and normalized == "voice_cloning":
        profile["status"] = "documented"
        profile["notes"].append("The Qwen cloning path does not receive delivery instructions.")
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
    elif route in {"openai", "openai_audio"} and normalized in {"tts_1", "tts_1_hd"}:
        profile["status"] = "documented"
        profile["notes"].append("Classic OpenAI TTS models do not receive delivery instructions.")
    elif (route == "chatterbox" and normalized in {"chatterbox_turbo", "turbo"}) or (audio_cpp and family == "chatterbox_turbo"):
        profile.update(
            status="documented",
            dialect="chatterbox_tags",
            event_tags=(
                {
                    "laugh": "laugh",
                    "chuckle": "chuckle",
                    "sigh": "sigh",
                    "cough": "cough",
                    "gasp": "gasp",
                    "clear_throat": "clear throat",
                }
                if audio_cpp
                else dict(_TURBO_EVENTS)
            ),
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
            instruction_kind="voice_and_delivery",
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
            instruction_kind="voice_and_delivery",
        )
    elif audio_cpp and family == "fireredtts3" and "instruct" in normalized:
        profile.update(
            status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], voice_design=True,
            emotion={"mode": "open_description", "tags": []},
            instruction_kind="voice_description",
        )
        if reference_audio:
            profile.update(instructions="none", instruction_kind="none", instruction_scope=[],
                voice_design=False, emotion={"mode": "none", "tags": []})
            profile["notes"].append("FireRed cloning uses the instruction slot for the reference transcript; directions are unavailable in this mode.")
        else:
            profile["notes"].append("The description changes voice identity; phrase delivery is not controlled.")
    elif audio_cpp and normalized in _AUK_MODELS:
        profile.update(
            status="documented",
            dialect="instructions",
            instructions="field",
            instruction_scope=["request"],
            instruction_kind="voice_description",
            requires_instruction=True,
            instruction_transport="options.instruct",
        )
    elif audio_cpp and normalized in _MOSS_TTSD_MODELS:
        profile.update(
            status="documented",
            dialect="instructions",
            instructions="field",
            instruction_scope=["request"],
            instruction_kind="delivery",
            instruction_transport="options.instruct",
        )
    elif audio_cpp and normalized in _MAYA_MODELS:
        profile.update(status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], voice_design=True, requires_instruction=True,
            instruction_kind="voice_and_delivery", instruction_transport="options.instruct",
            emotion={"mode": "open_description", "tags": []},
            event_tags=dict(_MAYA_EVENTS), event_format="angle_brackets")
    elif audio_cpp and normalized in _MOSS_TTS_MODELS:
        profile.update(status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], instruction_kind="voice_description",
            instruction_transport="options.instruct",
            emotion={"mode": "open_description", "tags": []})
        profile["notes"].append("Voice descriptions and speaker attributes are loosely followed; phrase delivery is not controlled.")
    elif audio_cpp and normalized == "moss_voicegen_bf16_codec_f16_decode":
        profile.update(status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], voice_design=True,
            instruction_kind="voice_description", instruction_transport="options.instruct",
            emotion={"mode": "open_description", "tags": []})
        profile["notes"].append("Description and seed do not ensure the same stable voice across different text; phrase delivery is not controlled.")
    elif audio_cpp and normalized in _IRODORI_DESIGN_MODELS:
        profile.update(status="documented", dialect="instructions", instructions="field",
            instruction_scope=["request"], voice_design=True,
            instruction_kind="voice_description", instruction_transport="options.instruction",
            emotion={"mode": "open_description", "tags": []})
        profile["notes"].append("The description changes voice identity; linked reference remains available; phrase delivery is not controlled.")
    elif audio_cpp and normalized in _IRODORI_500_MODELS:
        profile["status"] = "documented"
    elif audio_cpp and family == "neutts":
        profile.update(status="documented", dialect="neutts",
            emotion={"mode": "enum", "tags": ["angry", "disgusted", "sad", "happy", "fearful", "neutral", "surprised"]})
    elif audio_cpp and family == "index_tts2":
        profile.update(
            status="documented",
            emotion={
                "mode": "text_field",
                "transport": "options.emotion_text",
                "tags": [],
                "intensity": {
                    "min": 0.0,
                    "max": 1.0,
                    "default": 1.0,
                    "transport": "options.emotion_alpha",
                    "scope": "request",
                    "label": "Emotion intensity",
                    "semantics": (
                        "Scales IndexTTS2 emotion guidance; 0.0 disables its contribution "
                        "and 1.0 uses full weight."
                    ),
                },
            },
        )
        profile["notes"].append("Emotion descriptions use a whole-request native option; general instructions and phrase emotion scope are unavailable.")
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
            instruction_kind="voice_and_delivery",
        )
        profile["notes"].append("Voice/style descriptions are parenthesized input prefixes; phrase scope is not established.")
    if profile["instructions"] != "none" and profile["instruction_kind"] == "none":
        profile["instruction_kind"] = "delivery"
    if profile["instructions"] != "none":
        profile["native_instruction_scope"] = ["request"]
    if profile["instructions"] in {"field", "prompt"} and profile["instruction_kind"] in {
        "delivery",
        "voice_and_delivery",
    }:
        profile.update(
            native_instruction_scope=["request"],
            phrase_instruction_transport="quoted_hint",
            phrase_instruction_reliability="best_effort",
        )
    if profile["dialect"] in {"fish_s2", "eleven_v3", "eleven_v4"}:
        profile.update(
            phrase_instruction_transport="inline_hint",
            phrase_instruction_reliability="best_effort",
        )
    if profile["dialect"] == "fish_s2":
        profile["recommended_instruction_scope"] = "request"
        profile["notes"].append("Whole-segment directions are recommended. Phrase cues are best effort and can be ignored or continue beyond the marked words; reset cues are not closing tags.")
    if profile["dialect"] == "gemini":
        profile.update(
            phrase_instruction_transport="inline_hint",
            phrase_instruction_reliability="best_effort",
            semantic_context_transport="guided_prompt",
        )
    elif profile["dialect"] == "gemini38":
        profile["semantic_context_transport"] = "instruction_hint"
    if profile["semantic_context"] == "field":
        profile["semantic_context_transport"] = "native_fields"
    if profile["voice_design"]:
        profile["voice_design_workflow"] = "request_description"
    return profile


def capabilities_for_endpoint_model(
    model: str,
    endpoint: Mapping[str, Any],
    *,
    family: str = "",
    voice_mode: str = "",
    backend_version: str = "",
    reference_audio: bool = False,
    voice: str = "",
) -> dict[str, Any]:
    """Resolve a public model selector through only explicit upstream mapping.

    Azure OpenAI deployments use deployment names as public selectors. An
    exact ``model_catalog`` row may identify the upstream OpenAI model whose
    native capabilities apply; the returned profile still names the public
    selector and Azure route. Without that explicit mapping the profile stays
    unknown.
    """
    route = capability_backend(endpoint)
    profile_model = model
    profile_route = route
    if route == "azure_openai":
        catalog = endpoint.get("model_catalog") or []
        for row in catalog:
            if not isinstance(row, Mapping) or row.get("id") != model:
                continue
            upstream_model = str(row.get("upstream_model_id") or "").strip()
            if upstream_model:
                profile_model = upstream_model
                profile_route = "openai"
            break

    profile = capabilities_for_model(
        profile_model,
        backend=profile_route,
        family=family,
        voice_mode=voice_mode,
        backend_version=backend_version,
        reference_audio=reference_audio,
    )
    profile["model"] = model
    profile["backend"] = route
    if profile_model != model:
        profile["upstream_model_id"] = profile_model
    if route == "azure_speech":
        from .azure_speech_emotions import azure_speech_emotion_profile

        emotion = azure_speech_emotion_profile(model, voice)
        if emotion is not None:
            if voice and emotion["tags"]:
                emotion["intensity"] = {
                    "min": 0.01,
                    "max": 2.0,
                    "default": 1.0,
                    "transport": "options.azure_speech_style_degree",
                    "scope": "request",
                    "label": "Style degree",
                    "semantics": (
                        "Scales this selected voice's documented speaking style; 1.0 is "
                        "the predefined degree, with lower or higher values softening or strengthening it."
                    ),
                }
            profile.update(
                status="documented",
                dialect="azure_style",
                instructions="none",
                instruction_scope=[],
                native_instruction_scope=[],
                phrase_instruction_transport="none",
                phrase_instruction_reliability="none",
                emotion=emotion,
            )
    return profile


def decorate_service_capabilities(service: dict[str, Any]) -> None:
    """Expose one backend-authoritative capability view to UI and MCP clients."""
    catalog = service.get("model_catalog") or []
    metadata = {
        str(item["id"]): item
        for item in catalog
        if isinstance(item, dict) and item.get("id")
    }
    model_ids = list(dict.fromkeys([*service.get("models", []), *metadata]))
    profiles = {
        model: capabilities_for_endpoint_model(
            model,
            service,
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
