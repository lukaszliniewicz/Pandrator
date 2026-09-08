"""Reset incompatible overrides only on an explicit compatibility-provider switch."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .tts_provider_policy import COMPATIBILITY_SERVICE_IDS

# These are stable service labels accepted by the existing settings boundary.
_ALIASES = {"audio.cpp": "audio_cpp", "qwen3_tts": "kobold_qwen"}
_PROVIDER_PREFIXES = ("voxcpm_", "fishs2_", "chatterbox_", "kobold_qwen_", "magpie_")
_STALE_SELECTION_KEYS = (
    "xtts_model",
    "speaker",
    "reference_audio",
    "reference_text",
    "options",
    "audio_cpp_voice_ref",
    "audio_cpp_reference_text",
)


def _service(values: dict[str, Any]) -> str:
    value = str(values.get("service") or values.get("tts_service") or "").strip()
    key = value.lower().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(key, key)


def prepare_tts_provider_switch(
    previous: dict[str, Any], value: dict[str, Any]
) -> dict[str, Any]:
    """Keep old selections intact, reset stale state when opting into audio.cpp.

    A provider switch must explicitly supply its target model and voice. Empty
    values clear an inherited selection and leave generation to require a valid
    choice. No model equivalence or uploaded-voice equivalence is assumed.
    """
    result = deepcopy(value)
    reviewed = result.pop("provider_switch_reviewed", False) is True
    if (
        _service(previous) not in COMPATIBILITY_SERVICE_IDS
        or _service(value) != "audio_cpp"
    ):
        return result
    for key in list(result):
        if key.startswith(_PROVIDER_PREFIXES) or key in _STALE_SELECTION_KEYS:
            result.pop(key, None)
    result["service"] = "audio_cpp"
    result["tts_service"] = "audio_cpp"
    # Explicit empty overrides prevent old global aliases leaking into sessions.
    model = str(result.get("model") or "").strip()
    if (
        not reviewed
        and model
        and model
        in {str(previous.get("model") or ""), str(previous.get("xtts_model") or "")}
    ):
        model = ""
    voice = str(result.get("voice") or "").strip()
    if (
        not reviewed
        and voice
        and voice
        in {str(previous.get("voice") or ""), str(previous.get("speaker") or "")}
    ):
        voice = ""
    result.update(model=model, xtts_model=model, voice=voice, speaker=voice)
    result["audio_cpp_voice_ref"] = {}
    result["audio_cpp_reference_text"] = ""
    result["options"] = {}
    return result
