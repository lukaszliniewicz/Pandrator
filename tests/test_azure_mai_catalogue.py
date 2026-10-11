from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from pandrator.logic import tts_handler
from pandrator.logic.tts_provider_profiles import (
    AZURE_SPEECH_LEGACY_MODEL_ALIASES,
    AZURE_SPEECH_MODELS,
    list_tts_provider_profiles,
)

CATALOGUE_PATH = (
    Path(__file__).resolve().parents[1]
    / "pandrator"
    / "logic"
    / "azure_mai_voice_catalogue.json"
)


def test_source_catalogue_preserves_the_documented_voice_matrix() -> None:
    catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    rows = catalogue["rows"]
    models = ["MAI-Voice-2.1", "MAI-Voice-2.1-Flash"]

    assert catalogue["source_url"] == (
        "https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-voices"
    )
    assert catalogue["checked_at"] == "2026-10-09"
    assert catalogue["models"] == models
    assert len(rows) == 97
    stems = [row["voice_id"] for row in rows]
    assert len(set(stems)) == 97
    assert all(row["supported_models"] == models for row in rows)

    suffix_ids = [
        f"{row['voice_id']}:{model}"
        for row in rows
        for model in row["supported_models"]
    ]
    assert len(suffix_ids) == 194
    assert len(set(suffix_ids)) == 194
    assert all(row["gender"] in {"Female", "Male"} for row in rows)
    assert all(row["locale"] and row["language"] for row in rows)
    assert all(
        row["supported_styles"]
        and all(style and style == style.strip() for style in row["supported_styles"])
        and len(row["supported_styles"]) == len(set(row["supported_styles"]))
        for row in rows
    )

    source_rows = {row["voice_id"]: row for row in rows}
    assert source_rows["en-US-Grant"]["supported_styles"] == [
        "agent",
        "audiobook",
        "customer_call_center",
        "educational",
        "narrator",
        "neutral",
    ]
    assert source_rows["es-ES-Marta"]["supported_styles"] == [
        "adventurous",
        "caringempathy",
        "curious",
        "encouraging",
        "excited",
        "friendlycheerful",
        "neutral",
        "nostalgic",
        "reflective",
        "saddisappointed",
        "serious",
    ]


def test_profile_projects_current_model_suffixes_and_source_metadata() -> None:
    catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    rows = catalogue["rows"]
    profile = next(
        profile
        for profile in list_tts_provider_profiles()
        if profile["id"] == "azure-speech-mai-voice-2"
    )

    assert AZURE_SPEECH_MODELS == catalogue["models"]
    assert AZURE_SPEECH_LEGACY_MODEL_ALIASES == {
        "MAI-Voice-2": "MAI-Voice-2.1",
        "MAI-Voice-2-Flash": "MAI-Voice-2.1-Flash",
    }
    assert profile["profile_id"] == "azure-speech-mai-voice-2"
    assert profile["name"] == "Azure Speech · MAI Voice 2.1"
    assert profile["default_model"] == "MAI-Voice-2.1"
    assert profile["default_voice"] == "en-US-Ethan:MAI-Voice-2.1"
    assert profile["default_voices"] == {
        "MAI-Voice-2.1": "en-US-Ethan:MAI-Voice-2.1",
        "MAI-Voice-2.1-Flash": "en-US-Ethan:MAI-Voice-2.1-Flash",
    }
    assert profile["models"] == catalogue["models"]
    assert [item["id"] for item in profile["model_catalog"]] == catalogue["models"]
    assert [item["name"] for item in profile["model_catalog"]] == [
        "MAI Voice 2.1",
        "MAI Voice 2.1 Flash",
    ]
    assert "MAI-Voice-2 resolves to MAI-Voice-2.1" in profile["notes"]
    assert "MAI-Voice-2-Flash resolves to MAI-Voice-2.1-Flash" in profile["notes"]
    assert "at request time" in profile["notes"]

    source_rows = {row["voice_id"]: row for row in rows}
    projected_suffix_ids = {
        voice_id
        for voices in profile["voice_catalogues"].values()
        for voice_id in voices
    }
    assert set(profile["voice_catalogues"]) == set(catalogue["models"])
    assert len(projected_suffix_ids) == 194
    assert len(profile["voices"]) == 194
    assert set(profile["voices"]) == projected_suffix_ids
    assert len(profile["voice_metadata"]) == 194
    assert not any(
        voice_id.endswith(f":{legacy_model}")
        for voice_id in projected_suffix_ids
        for legacy_model in AZURE_SPEECH_LEGACY_MODEL_ALIASES
    )

    for row in rows:
        for model in row["supported_models"]:
            voice_id = f"{row['voice_id']}:{model}"
            metadata = profile["voice_metadata"][f"{model}:{voice_id}"]
            assert metadata["id"] == voice_id
            assert metadata["voice_id"] == voice_id
            assert metadata["model"] == model
            assert metadata["locale"] == row["locale"]
            assert metadata["language"] == row["language"]
            assert metadata["gender"] == row["gender"]
            assert metadata["styles"] == source_rows[row["voice_id"]]["supported_styles"]
            assert metadata["source"] == catalogue["source_url"]
            assert metadata["checked_at"] == catalogue["checked_at"]


def test_request_uses_current_suffix_and_validates_styles_for_each_voice() -> None:
    legacy = tts_handler._azure_speech_ssml(
        "Hello & goodbye.", "MAI-Voice-2", "en-US-Ethan:MAI-Voice-2",
        {"azure_speech_style": "whispering"},
    )
    assert 'name="en-US-Ethan:MAI-Voice-2.1"' in legacy
    assert '<mstts:express-as style="whispering">' in legacy
    assert "Hello &amp; goodbye." in legacy

    # Grant supports role styles, while Ethan supports a different emotion set.
    role = tts_handler._azure_speech_ssml(
        "Hello.", "MAI-Voice-2.1-Flash", "en-US-Grant:MAI-Voice-2.1-Flash",
        {"azure_speech_style": "educational"},
    )
    assert 'style="educational"' in role
    with pytest.raises(ValueError, match="style 'educational' is unavailable"):
        tts_handler._azure_speech_ssml(
            "Hello.", "MAI-Voice-2.1", "en-US-Ethan:MAI-Voice-2.1",
            {"azure_speech_style": "educational"},
        )
    with pytest.raises(ValueError, match="published prebuilt voice"):
        tts_handler._azure_speech_ssml(
            "Hello.", "MAI-Voice-2.1", "en-US-Ethan:MAI-Voice-2.1-Flash", {},
        )


def test_saved_legacy_profile_exposes_current_catalogue_and_preserves_connection() -> None:
    from pandrator.logic.tts_service_catalogue import get_provider_configs

    configured = {
        "id": "azure-speech-mai-voice-2", "profile_id": "azure-speech-mai-voice-2",
        "name": "My speech resource", "adapter": "azure_speech", "provider": "azure",
        "api_base": "https://eastus.tts.speech.microsoft.com", "api_key_env": "TEST_SPEECH_KEY",
        "models": ["MAI-Voice-2", "MAI-Voice-2-Flash"],
        "default_model": "MAI-Voice-2-Flash", "default_voice": "en-US-Olivia:MAI-Voice-2-Flash",
        "default_voices": {"MAI-Voice-2": "en-US-Grant:MAI-Voice-2"},
        "voices": ["en-US-Olivia:MAI-Voice-2-Flash"],
    }
    before = deepcopy(configured)
    row = get_provider_configs({"provider_configs": [configured]})[0]
    assert row["models"] == ["MAI-Voice-2.1", "MAI-Voice-2.1-Flash"]
    voices = row["voices"]
    assert isinstance(voices, list) and len(voices) == 194
    assert row["default_model"] == "MAI-Voice-2.1-Flash"
    assert row["default_voice"] == "en-US-Olivia:MAI-Voice-2.1-Flash"
    defaults = row["default_voices"]
    assert isinstance(defaults, dict)
    assert defaults["MAI-Voice-2.1"] == "en-US-Grant:MAI-Voice-2.1"
    for key in ("api_base", "api_key_env", "name"):
        assert row[key] == configured[key]
    assert configured == before
