"""Portable character dictionary and voice-casting contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .common import ToolInput

VoiceCategory = Literal["male", "female", "androgynous", "unspecified"]
CharacterStatus = Literal["proposed", "accepted"]


class _StrictModel(ToolInput):
    """Local strict base without importing the application package."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _trim_text(value: Any) -> Any:
    return value if value is None or not isinstance(value, str) else value.strip()


class CharacterEntry(_StrictModel):
    """One stable character identity in a session's character dictionary."""

    id: str = Field(default="", max_length=80)
    display_name: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=64)
    voice_category: VoiceCategory = "unspecified"
    notes: str = Field(default="", max_length=2000)
    locked: bool = False
    status: CharacterStatus = "accepted"
    origin: str = Field(default="manual", max_length=160)

    @field_validator("id", "display_name", "notes", "origin", mode="before")
    @classmethod
    def trim_text_fields(cls, value: Any) -> Any:
        return value if not isinstance(value, str) else value.strip()

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
            aliases.append(normalized)
        return aliases


class VoiceBinding(_StrictModel):
    """Provider-neutral voice choice, optionally referencing a managed voice."""

    voice: str = Field(default="", max_length=255)
    voice_id: str | None = Field(default=None, max_length=80)
    service: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=255)
    voice_description: str = Field(default="", max_length=4000)

    @field_validator("voice", "voice_description", mode="before")
    @classmethod
    def trim_required_text(cls, value: Any) -> Any:
        return value if not isinstance(value, str) else value.strip()

    @field_validator("voice_id", "service", "model", mode="before")
    @classmethod
    def trim_optional_text(cls, value: Any) -> Any:
        return _trim_text(value)

    @model_validator(mode="after")
    def has_voice_reference(self) -> "VoiceBinding":
        if not (self.voice or self.voice_id or self.voice_description):
            raise ValueError(
                "A voice binding needs a voice, voice_id, or voice_description."
            )
        return self


_CATEGORIES = {"male", "female", "androgynous", "unspecified"}


class CastSettings(_StrictModel):
    """Narrator, category, character, and source-speaker voice bindings."""

    narrator: VoiceBinding | None = None
    categories: dict[str, VoiceBinding] = Field(default_factory=dict)
    characters: dict[str, VoiceBinding] = Field(default_factory=dict)
    source_speakers: dict[str, VoiceBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_keys(self) -> "CastSettings":
        invalid_categories = set(self.categories) - _CATEGORIES
        if invalid_categories:
            raise ValueError(
                "Unsupported voice categories: "
                + ", ".join(sorted(invalid_categories))
            )

        normalized_characters: dict[str, VoiceBinding] = {}
        for character_id, binding in self.characters.items():
            normalized = character_id.strip()
            if not normalized or len(normalized) > 80:
                raise ValueError(
                    "Cast character IDs must be nonblank and at most 80 characters."
                )
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


class GetGenerationControlsInput(_StrictModel):
    session_id: str = Field(min_length=1, max_length=80)


class UpdateGenerationControlsInput(_StrictModel):
    session_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=0)
    characters: list[CharacterEntry] | None = Field(default=None, max_length=2000)
    cast: CastSettings | None = None
    unlock_ids: list[str] = Field(default_factory=list, max_length=2000)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )

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


class GenerationControlsResponse(_StrictModel):
    """Public revisioned response shared by GET and PUT tools."""

    id: str | None = Field(default=None, min_length=1, max_length=80)
    session_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=0)
    characters: list[CharacterEntry] = Field(max_length=2000)
    cast: CastSettings


# Naming aliases keep the public portable contract readable at call sites that
# use the domain terms directly while preserving one model definition.
Character = CharacterEntry
Cast = CastSettings
GenerationControls = GenerationControlsResponse
GenerationControlsUpdateRequest = UpdateGenerationControlsInput


GENERATION_CONTROLS_INPUT_MODELS = (
    GetGenerationControlsInput,
    UpdateGenerationControlsInput,
)


__all__ = [
    "Cast",
    "CastSettings",
    "Character",
    "CharacterEntry",
    "CharacterStatus",
    "GenerationControls",
    "GenerationControlsResponse",
    "GenerationControlsUpdateRequest",
    "GENERATION_CONTROLS_INPUT_MODELS",
    "GetGenerationControlsInput",
    "UpdateGenerationControlsInput",
    "VoiceBinding",
    "VoiceCategory",
]
