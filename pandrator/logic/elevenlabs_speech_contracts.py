"""Validated ElevenLabs speech request settings."""

import math
from typing import Any

ELEVENLABS_V4_HTTP_MODEL = "eleven_v4"
ELEVENLABS_V4_TURBO_MODEL = "eleven_v4_turbo"
ELEVENLABS_V4_MODELS = frozenset(
    {ELEVENLABS_V4_HTTP_MODEL, ELEVENLABS_V4_TURBO_MODEL}
)
ELEVENLABS_V4_MAX_REQUEST_CHARACTERS = 2_000


def _validated_elevenlabs_voice_settings(
    value: Any,
    *,
    model_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("ElevenLabs voice settings must be a dictionary.")
    numeric_ranges = {
        "stability": (0.0, 1.0),
        "similarity_boost": (0.0, 1.0),
        "style": (0.0, 1.0),
        "speed": (0.25, 4.0),
    }
    allowed = {*numeric_ranges, "use_speaker_boost"}
    selected_model = str(model_id or "").casefold()
    if selected_model == ELEVENLABS_V4_HTTP_MODEL:
        allowed = {"stability", "similarity_boost"}
    elif selected_model == ELEVENLABS_V4_TURBO_MODEL:
        allowed = {"stability"}
    validated: dict[str, Any] = {}
    for key, item in value.items():
        if key not in allowed:
            raise ValueError(f"Unsupported ElevenLabs voice setting: {key}.")
        if key == "use_speaker_boost":
            if not isinstance(item, bool):
                raise ValueError("ElevenLabs use_speaker_boost must be a boolean.")
        elif (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not numeric_ranges[key][0] <= item <= numeric_ranges[key][1]
            or not math.isfinite(item)
        ):
            raise ValueError(f"ElevenLabs {key} is outside its supported numeric range.")
        validated[key] = item
    return validated


__all__ = [
    "ELEVENLABS_V4_HTTP_MODEL",
    "ELEVENLABS_V4_MAX_REQUEST_CHARACTERS",
    "ELEVENLABS_V4_MODELS",
    "ELEVENLABS_V4_TURBO_MODEL",
    "_validated_elevenlabs_voice_settings",
]
