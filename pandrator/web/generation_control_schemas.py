"""Validated character and voice-casting contracts for generation controls."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from pandrator.logic.speech_performance import StrictModel

VoiceCategory = Literal["male", "female", "androgynous", "unspecified"]
CharacterStatus = Literal["proposed", "accepted"]


def _trim_text(value: str | None) -> str | None:
    return None if value is None else value.strip() if isinstance(value, str) else value


class CharacterEntry(StrictModel):
    """One stable character identity in a session's character dictionary."""

    id: str = ""
    display_name: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=64)
    voice_category: VoiceCategory = "unspecified"
    notes: str = Field(default="", max_length=2000)
    locked: bool = False
    status: CharacterStatus = "accepted"
    origin: str = Field(default="manual", max_length=160)

    @field_validator("id", "display_name", "notes", "origin", mode="before")
    @classmethod
    def trim_text_fields(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @field_validator("aliases", mode="before")
    @classmethod
    def trim_aliases(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list):
            return value
        aliases: list[str] = []
        for alias in value:
            if not isinstance(alias, str):
                return value
            normalized = alias.strip()
            if not normalized:
                raise ValueError("Character aliases may not be blank.")
            if len(normalized) > 255:
                raise ValueError("Character aliases must be at most 255 characters.")
            aliases.append(normalized)
        return aliases


class VoiceBinding(StrictModel):
    """A provider-neutral voice choice, optionally referencing a managed voice."""

    voice: str = Field(default="", max_length=255)
    voice_id: str | None = Field(default=None, max_length=80)
    service: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=255)
    voice_description: str = Field(default="", max_length=4000)

    @field_validator("voice", "voice_description", mode="before")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @field_validator("voice_id", "service", "model", mode="before")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        normalized = _trim_text(value)
        return normalized or None

    @model_validator(mode="after")
    def has_voice_reference(self) -> VoiceBinding:
        if not (self.voice or self.voice_id or self.voice_description):
            raise ValueError(
                "A voice binding needs a voice, voice_id, or voice_description."
            )
        return self


_CATEGORIES = {"male", "female", "androgynous", "unspecified"}


class CastSettings(StrictModel):
    """Narrator, category, character, and source-speaker voice bindings."""

    narrator: VoiceBinding | None = None
    categories: dict[str, VoiceBinding] = Field(default_factory=dict)
    characters: dict[str, VoiceBinding] = Field(default_factory=dict)
    source_speakers: dict[str, VoiceBinding] = Field(
        default_factory=dict, max_length=500
    )

    @model_validator(mode="after")
    def validate_keys(self) -> CastSettings:
        invalid_categories = set(self.categories) - _CATEGORIES
        if invalid_categories:
            raise ValueError(
                "Unsupported voice categories: " + ", ".join(sorted(invalid_categories))
            )

        normalized_characters: dict[str, VoiceBinding] = {}
        for character_id, binding in self.characters.items():
            normalized = character_id.strip()
            if not normalized:
                raise ValueError("Cast character IDs must be nonblank.")
            if normalized in normalized_characters:
                raise ValueError(f"Duplicate cast character ID: {normalized}")
            normalized_characters[normalized] = binding
        self.characters = normalized_characters

        normalized_speakers: dict[str, VoiceBinding] = {}
        for speaker, binding in self.source_speakers.items():
            normalized = speaker.strip()
            if not normalized or len(normalized) > 160:
                raise ValueError(
                    "Source speaker IDs must be nonblank and at most 160 characters."
                )
            if normalized in normalized_speakers:
                raise ValueError(f"Duplicate source speaker ID: {normalized}")
            normalized_speakers[normalized] = binding
        self.source_speakers = normalized_speakers
        return self


class GenerationControlsUpdateRequest(StrictModel):
    """Revision-checked replacement of one or both generation-control sections."""

    expected_revision: int = Field(ge=0)
    characters: list[CharacterEntry] | None = Field(default=None, max_length=2000)
    cast: CastSettings | None = None
    unlock_ids: list[str] = Field(default_factory=list, max_length=2000)

    @field_validator("unlock_ids", mode="before")
    @classmethod
    def trim_unlock_ids(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return value
            item = item.strip()
            if not item:
                raise ValueError("unlock_ids may not contain blank IDs.")
            normalized.append(item)
        return normalized


__all__ = [
    "CastSettings",
    "CharacterEntry",
    "CharacterStatus",
    "GenerationControlsUpdateRequest",
    "VoiceBinding",
    "VoiceCategory",
]
