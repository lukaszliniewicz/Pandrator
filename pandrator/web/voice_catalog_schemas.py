"""Strict schemas for voice descriptions, references, and collections."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _optional_text(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    normalized = value.strip()
    return normalized or None


def _required_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return value.strip()


def _deduplicate(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if not any(value == previous for previous in result):
            result.append(value)
    return result


class TraitEvidence(_StrictModel):
    source: Literal["user", "provider", "design_request", "audition_review"]
    status: Literal["requested", "described", "reviewed"]
    artifact_id: str | None = Field(default=None, min_length=1, max_length=160)
    note: str | None = Field(default=None, max_length=400)

    _trim_text = field_validator("artifact_id", "note", mode="before")(_optional_text)

    @model_validator(mode="after")
    def validate_source_status(self) -> TraitEvidence:
        if self.status == "reviewed" and self.source != "audition_review":
            raise ValueError("Reviewed traits require audition-review evidence.")
        if self.source == "design_request" and self.status != "requested":
            raise ValueError("Design-request evidence must have requested status.")
        if self.source == "audition_review":
            if self.status != "reviewed":
                raise ValueError("Audition-review evidence must have reviewed status.")
            if not self.artifact_id:
                raise ValueError("Audition-review evidence requires artifact_id.")
        return self


class VoiceLanguage(_StrictModel):
    language: str = Field(min_length=1, max_length=40)
    locale: str | None = Field(default=None, min_length=1, max_length=40)
    accent: str | None = Field(default=None, min_length=1, max_length=80)
    detail: str | None = Field(default=None, min_length=1, max_length=200)
    evidence: TraitEvidence | None = None

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip().replace("_", "-").lower()
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", normalized):
            raise ValueError("Language must be a valid normalized language tag.")
        return normalized

    @field_validator("locale", mode="before")
    @classmethod
    def normalize_locale(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip().replace("_", "-").lower()
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", normalized):
            raise ValueError("Locale must be a valid normalized language tag.")
        return normalized

    _trim_text = field_validator("accent", "detail", mode="before")(_optional_text)


class VoiceProfile(_StrictModel):
    """Description and evidence metadata; this is never an audio classifier."""

    schema_version: Literal[1] = 1
    pitch: Literal["low", "mid", "high"] | None = None
    perceived_age: Literal["childlike", "youthful", "adult", "older"] | None = None
    textures: list[
        Literal[
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
        ]
    ] = Field(default_factory=list, max_length=10)
    delivery_presets: list[
        Literal["neutral", "conversational", "formal", "storytelling", "dramatic"]
    ] = Field(default_factory=list, max_length=5)
    use_cases: list[
        Literal[
            "audiobook_narration",
            "character_dialogue",
            "voiceover",
            "documentary",
            "news",
            "advertising",
            "instructional",
        ]
    ] = Field(default_factory=list, max_length=7)
    languages: list[VoiceLanguage] = Field(default_factory=list, max_length=30)
    tags: list[str] = Field(default_factory=list, max_length=24)
    evidence: dict[str, TraitEvidence] = Field(default_factory=dict, max_length=32)

    @field_validator("textures", "delivery_presets", "use_cases")
    @classmethod
    def deduplicate_facets(cls, value: list[str]) -> list[str]:
        return _deduplicate(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, str):
                item = item.strip()
                if not item:
                    raise ValueError("Voice profile tags may not be blank.")
            normalized.append(item)
        return _deduplicate(normalized)

    @field_validator("tags")
    @classmethod
    def validate_tag_lengths(cls, value: list[str]) -> list[str]:
        if any(len(item) > 40 for item in value):
            raise ValueError("Voice profile tags must be at most 40 characters.")
        return value

    @field_validator("evidence", mode="before")
    @classmethod
    def normalize_evidence_keys(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str):
                key = key.strip()
            if not isinstance(key, str) or not key:
                raise ValueError(
                    "Voice profile evidence keys must be nonblank strings."
                )
            if len(key) > 120:
                raise ValueError(
                    "Voice profile evidence keys must be at most 120 characters."
                )
            normalized[key] = item
        return normalized


class VoiceReference(_StrictModel):
    kind: Literal["managed", "provider"]
    voice_id: str | None = Field(default=None, min_length=1, max_length=160)
    service_id: str | None = Field(default=None, min_length=1, max_length=160)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    voice: str | None = Field(default=None, min_length=1, max_length=255)

    _trim_text = field_validator(
        "voice_id", "service_id", "model", "voice", mode="before"
    )(_optional_text)

    @model_validator(mode="after")
    def validate_shape(self) -> VoiceReference:
        if self.kind == "managed":
            if self.voice_id is None:
                raise ValueError("Managed voice references require voice_id.")
            if any(
                value is not None for value in (self.service_id, self.model, self.voice)
            ):
                raise ValueError("Managed voice references may contain voice_id only.")
            return self

        if self.voice_id is not None:
            raise ValueError("Provider voice references may not contain voice_id.")
        if any(value is None for value in (self.service_id, self.model, self.voice)):
            raise ValueError(
                "Provider voice references require service_id, model, and voice."
            )
        return self


def canonical_voice_key(ref: VoiceReference) -> str:
    """Return the stable storage key for a managed or provider voice reference."""

    reference = (
        ref if isinstance(ref, VoiceReference) else VoiceReference.model_validate(ref)
    )
    if reference.kind == "managed":
        assert reference.voice_id is not None
        return f"managed:{reference.voice_id}"
    assert reference.service_id is not None
    assert reference.model is not None
    assert reference.voice is not None
    return ":".join(
        (
            "provider",
            quote(reference.service_id, safe=""),
            quote(reference.model, safe=""),
            quote(reference.voice, safe=""),
        )
    )


class VoiceCollectionCreate(_StrictModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> Any:
        return _required_text(value)


class VoiceCollectionUpdate(_StrictModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    add_members: list[VoiceReference] = Field(default_factory=list, max_length=200)
    remove_members: list[VoiceReference] = Field(default_factory=list, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> Any:
        if value is None:
            return None
        return _required_text(value)

    @model_validator(mode="after")
    def reject_overlapping_members(self) -> VoiceCollectionUpdate:
        additions = {canonical_voice_key(ref) for ref in self.add_members}
        removals = {canonical_voice_key(ref) for ref in self.remove_members}
        overlap = additions & removals
        if overlap:
            raise ValueError(
                "A voice reference cannot be added and removed in the same update."
            )
        return self


__all__ = [
    "TraitEvidence",
    "VoiceCollectionCreate",
    "VoiceCollectionUpdate",
    "VoiceLanguage",
    "VoiceProfile",
    "VoiceReference",
    "canonical_voice_key",
]
