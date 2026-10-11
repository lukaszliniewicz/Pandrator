"""Voice-specific literal emotion styles for Azure MAI Speech models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CATALOGUE_PATH = Path(__file__).with_name("azure_mai_voice_catalogue.json")
with _CATALOGUE_PATH.open(encoding="utf-8") as _catalogue_file:
    _CATALOGUE: dict[str, Any] = json.load(_catalogue_file)

_MODELS = tuple(_CATALOGUE["models"])
_ROWS = tuple(_CATALOGUE["rows"])
_ROWS_BY_VOICE = {row["voice_id"].casefold(): row for row in _ROWS}
_LEGACY_MODEL_ALIASES = {
    "mai-voice-2": "MAI-Voice-2.1",
    "mai-voice-2-flash": "MAI-Voice-2.1-Flash",
}
_MODELS_BY_CASEFOLD = {model.casefold(): model for model in _MODELS}


def canonical_azure_speech_model(model: str) -> str:
    """Return a current MAI model ID for an exact current or legacy selector."""
    normalized = str(model or "").strip().casefold()
    return _MODELS_BY_CASEFOLD.get(
        normalized, _LEGACY_MODEL_ALIASES.get(normalized, "")
    )


def canonical_azure_speech_voice(model: str, voice: str) -> str:
    """Resolve a catalogued voice stem and suffix for the selected MAI model."""
    canonical_model = canonical_azure_speech_model(model)
    normalized = str(voice or "").strip()
    stem, separator, suffix = normalized.rpartition(":")
    if not canonical_model or not separator:
        return ""
    if canonical_azure_speech_model(suffix) != canonical_model:
        return ""
    row = _ROWS_BY_VOICE.get(stem.casefold())
    if row is None or canonical_model not in row["supported_models"]:
        return ""
    return f"{row['voice_id']}:{canonical_model}"


def azure_speech_emotion_profile(
    model: str, voice: str = ""
) -> dict[str, Any] | None:
    """Return literal documented style tags, narrowed to a selected voice."""
    if model not in _MODELS:
        return None

    rows = [row for row in _ROWS if model in row["supported_models"]]
    if voice:
        canonical_voice = canonical_azure_speech_voice(model, voice)
        selected = next(
            (
                row
                for row in rows
                if canonical_voice
                and f"{row['voice_id']}:{model}" == canonical_voice
            ),
            None,
        )
        tags = list(selected["supported_styles"]) if selected is not None else []
    else:
        tags = sorted(
            {style for row in rows for style in row["supported_styles"]}
        )

    return {"mode": "enum", "tags": tags, "voice_dependent": True}


def normalize_azure_speech_emotion(
    emotion: str, model: str, voice: str = ""
) -> str | None:
    """Trim and case-fold a literal style; return None when it is unsupported."""
    normalized = str(emotion or "").strip().casefold()
    if not normalized:
        return ""
    profile = azure_speech_emotion_profile(model, voice)
    if profile is None:
        return None
    return next(
        (style for style in profile["tags"] if style.casefold() == normalized),
        None,
    )
