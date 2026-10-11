"""Compatibility for historical persisted audio.cpp voice maps."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..constants import magpie_voice_catalog

_LEGACY_VOICE_CATALOGUES: dict[str, list[str]] = {
    "qwen3_tts_1_7b_customvoice_q8_0": [
        "Aiden",
        "Dylan",
        "Eric",
        "Ono_Anna",
        "Ryan",
        "Serena",
        "Sohee",
        "Uncle_Fu",
        "Vivian",
    ],
    "magpie_tts_q8_0": magpie_voice_catalog(include_legacy_emotions=True),
    "pocket_tts_english_q8_0": ["alba"],
}

_LEGACY_DEFAULT_VOICES = {
    "qwen3_tts_1_7b_customvoice_q8_0": "Aiden",
    "magpie_tts_q8_0": "Magpie-Multilingual.EN-US.Sofia",
    "pocket_tts_english_q8_0": "alba",
}


def merge_audio_cpp_voice_catalogues(
    builtin_voice_catalogues: Mapping[str, Any],
    builtin_default_voices: Mapping[str, Any],
    raw_voice_catalogues: Mapping[str, Any],
    raw_default_voices: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Overlay saved maps by model, refreshing only the exact old generated maps."""
    raw_catalogues = dict(raw_voice_catalogues)
    raw_defaults = dict(raw_default_voices)
    if (
        raw_catalogues == _LEGACY_VOICE_CATALOGUES
        and raw_defaults == _LEGACY_DEFAULT_VOICES
    ):
        return deepcopy(dict(builtin_voice_catalogues)), deepcopy(
            dict(builtin_default_voices)
        )

    catalogues = deepcopy(dict(builtin_voice_catalogues))
    defaults = deepcopy(dict(builtin_default_voices))
    catalogues.update(deepcopy(raw_catalogues))
    defaults.update(deepcopy(raw_defaults))

    for model_id, voices in raw_catalogues.items():
        if isinstance(voices, list) and not voices and model_id not in raw_defaults:
            defaults.pop(model_id, None)

    return catalogues, defaults
