from __future__ import annotations

import pytest
from pydantic import ValidationError

from pandrator.web.schemas import VoiceCreate, VoiceUpdate
from pandrator.web.voice_catalog_schemas import (
    TraitEvidence,
    VoiceLanguage,
    VoiceProfile,
    VoiceReference,
    canonical_voice_key,
)


def test_profile_defaults_and_normalization() -> None:
    profile = VoiceProfile.model_validate(
        {
            "textures": ["warm", "warm", "bright"],
            "languages": [
                {
                    "language": " EN_us ",
                }
            ],
            "tags": [" narrator ", "narrator"],
            "evidence": {
                "provider language": {
                    "source": "provider",
                    "status": "described",
                }
            },
        }
    )

    assert profile.pitch is None
    assert profile.perceived_age is None
    assert profile.textures == ["warm", "bright"]
    assert profile.languages[0].language == "en-us"
    assert profile.tags == ["narrator"]
    assert VoiceProfile().evidence == {}
    assert profile.evidence["provider language"].status == "described"

    assert VoiceCreate(name="Narrator").profile is None
    assert VoiceUpdate().profile is None


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": True},
        {
            "textures": [
                "warm",
                "bright",
                "dark",
                "airy",
                "breathy",
                "raspy",
                "gravelly",
                "resonant",
                "clear",
                "nasal",
                "warm",
            ]
        },
        {
            "delivery_presets": [
                "neutral",
                "conversational",
                "formal",
                "storytelling",
                "dramatic",
                "neutral",
            ]
        },
        {
            "use_cases": [
                "audiobook_narration",
                "character_dialogue",
                "voiceover",
                "documentary",
                "news",
                "advertising",
                "instructional",
                "news",
            ]
        },
        {"languages": [{"language": "en"}] * 31},
        {"tags": [f"tag-{index}" for index in range(25)]},
        {
            "evidence": {
                str(index): {"source": "user", "status": "described"}
                for index in range(33)
            }
        },
        {"pitch": "unknown"},
        {"languages": [{"language": "en", "extra": True}]},
        {"tags": [""]},
        {"tags": ["x" * 41]},
    ],
)
def test_profile_rejects_extra_oversized_and_bad_values(payload: dict) -> None:
    with pytest.raises(ValidationError):
        VoiceProfile.model_validate(payload)


def test_evidence_bounds_and_enum_values() -> None:
    evidence = TraitEvidence(
        source="audition_review",
        status="reviewed",
        artifact_id="artifact-1",
        note="Reviewed by the editor.",
    )
    assert evidence.artifact_id == "artifact-1"
    with pytest.raises(ValidationError):
        TraitEvidence(source="user", status="described", note="x" * 401)
    with pytest.raises(ValidationError):
        VoiceLanguage(language="en", locale="x" * 41)
    with pytest.raises(ValidationError):
        TraitEvidence(source="design_request", status="described")
    with pytest.raises(ValidationError):
        TraitEvidence(source="audition_review", status="reviewed")


def test_voice_reference_shape_and_canonical_key() -> None:
    managed = VoiceReference(kind="managed", voice_id="voice-1")
    provider = VoiceReference(
        kind="provider",
        service_id="service/one",
        model="model:v1",
        voice="en US",
    )
    assert canonical_voice_key(managed) == "managed:voice-1"
    assert canonical_voice_key(provider) == "provider:service%2Fone:model%3Av1:en%20US"

    with pytest.raises(ValidationError):
        VoiceReference(kind="managed", voice_id="voice-1", voice="provider-voice")
    with pytest.raises(ValidationError):
        VoiceReference(kind="provider", service_id="service", model="model")
    with pytest.raises(ValidationError):
        VoiceReference(
            kind="provider",
            voice_id="managed-id",
            service_id="service",
            model="model",
            voice="voice",
        )
