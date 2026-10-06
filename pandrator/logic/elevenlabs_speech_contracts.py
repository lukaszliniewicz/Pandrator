"""Validated ElevenLabs speech request settings."""

import math
from typing import Any


def _validated_elevenlabs_voice_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("ElevenLabs voice settings must be a dictionary.")
    numeric_ranges = {
        "stability": (0.0, 1.0),
        "similarity_boost": (0.0, 1.0),
        "style": (0.0, 1.0),
        "speed": (0.25, 4.0),
    }
    allowed = {*numeric_ranges, "use_speaker_boost"}
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
