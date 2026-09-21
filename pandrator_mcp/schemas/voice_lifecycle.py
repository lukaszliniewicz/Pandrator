"""Typed MCP contracts for voice catalog and lifecycle operations."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .common import ToolInput

VoiceCategory = Literal["male", "female", "androgynous", "unspecified"]
Pitch = Literal["low", "mid", "high"]

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
_PROFILE_MAX_BYTES = 64 * 1024
_SETTINGS_MAX_BYTES = 32 * 1024


def _bounded_json_object(value: Any, *, name: str, maximum: int) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain finite JSON values.") from error
    if len(encoded) > maximum:
        raise ValueError(f"{name} may not exceed {maximum // 1024} KiB.")
    return value


class VoiceReferenceInput(ToolInput):
    """A managed voice or an exact provider service/model/voice tuple."""

    kind: Literal["managed", "provider"]
    voice_id: str | None = Field(default=None, min_length=1, max_length=160)
    service_id: str | None = Field(default=None, min_length=1, max_length=160)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    voice: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("voice_id", "service_id", "model", "voice", mode="before")
    @classmethod
    def trim_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_shape(self) -> "VoiceReferenceInput":
        if self.kind == "managed":
            if self.voice_id is None:
                raise ValueError("Managed voice references require voice_id.")
            if any(value is not None for value in (self.service_id, self.model, self.voice)):
                raise ValueError("Managed voice references may contain voice_id only.")
            return self
        if self.voice_id is not None:
            raise ValueError("Provider voice references may not contain voice_id.")
        if any(value is None for value in (self.service_id, self.model, self.voice)):
            raise ValueError("Provider voice references require service_id, model, and voice.")
        return self


class VoiceCreateInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    language: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=4_000)
    voice_category: VoiceCategory = "unspecified"
    profile: dict[str, Any] | None = Field(default=None)
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=_SAFE_KEY)

    @field_validator("name", "language", mode="before")
    @classmethod
    def trim_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _bounded_json_object(value, name="profile", maximum=_PROFILE_MAX_BYTES)


class VoiceCatalogInput(ToolInput):
    """Flat bounded query for the normalized voice catalog."""

    # language and limit are retained for compatibility with the legacy tool.
    query: str = Field(default="", max_length=300)
    language: str | None = Field(default="", max_length=40)
    accent: str = Field(default="", max_length=80)
    voice_category: Literal["", "male", "female", "androgynous", "unspecified"] = ""
    pitch: Literal["", "low", "mid", "high"] = ""
    perceived_age: Literal["", "childlike", "youthful", "adult", "older"] = ""
    texture: str = Field(default="", max_length=40)
    delivery_preset: str = Field(default="", max_length=40)
    tag: str = Field(default="", max_length=40)
    use_case: str = Field(default="", max_length=40)
    collection_id: str = Field(default="", max_length=160)
    kind: Literal["all", "managed", "provider"] = "all"
    origin: str = Field(default="", max_length=40)
    service_id: str = Field(default="", max_length=160)
    model: str = Field(default="", max_length=200)
    ready_only: bool = False
    reviewed_only: bool = False
    sort: Literal["relevance", "name", "recently_added", "recently_updated"] = "relevance"
    limit: int = Field(default=30, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=1024)


class VoiceCatalogCapabilitiesInput(ToolInput):
    service_id: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=200)


class GetVoiceSamplesInput(ToolInput):
    voice_id: str = Field(min_length=1, max_length=160)


class _RevisionWriteInput(ToolInput):
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=_SAFE_KEY)


class PromoteVoiceDesignInput(_RevisionWriteInput):
    voice_id: str = Field(min_length=1, max_length=160)
    artifact_id: str = Field(min_length=1, max_length=160)
    transcript: str = Field(min_length=1, max_length=4_000)
    language: str | None = Field(default=None, max_length=40)
    expected_voice_revision: int = Field(ge=1)


class ImportVoiceReferenceInput(_RevisionWriteInput):
    voice_id: str = Field(min_length=1, max_length=160)
    artifact_id: str = Field(min_length=1, max_length=160)
    transcript: str | None = Field(default=None, max_length=4_000)
    language: str | None = Field(default=None, max_length=40)
    transcript_reviewed: bool = False
    expected_voice_revision: int = Field(ge=1)


class TranscribeVoiceSampleInput(_RevisionWriteInput):
    voice_id: str = Field(min_length=1, max_length=160)
    sample_id: str = Field(min_length=1, max_length=160)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_json_object(value, name="settings", maximum=_SETTINGS_MAX_BYTES)


class ReviewVoiceTranscriptInput(_RevisionWriteInput):
    voice_id: str = Field(min_length=1, max_length=160)
    sample_id: str = Field(min_length=1, max_length=160)
    transcript: str = Field(min_length=1, max_length=4_000)
    language: str | None = Field(default=None, max_length=40)
    expected_voice_revision: int = Field(ge=1)


class PublishVoiceInput(_RevisionWriteInput):
    voice_id: str = Field(min_length=1, max_length=160)
    service_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=1)


class AuditionVoiceInput(_RevisionWriteInput):
    text: str = Field(min_length=1, max_length=4_000)
    service_id: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=200)
    voice: str = Field(default="", max_length=255)
    language: str = Field(default="en", min_length=1, max_length=40)
    generation_prompt: str | None = Field(default=None, max_length=4_000)
    seed: int | None = Field(default=None, ge=0, le=4_294_967_295)


class ListVoiceCollectionsInput(ToolInput):
    pass


class CreateVoiceCollectionInput(_RevisionWriteInput):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class UpdateVoiceCollectionInput(_RevisionWriteInput):
    collection_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    add_members: list[VoiceReferenceInput] = Field(default_factory=list, max_length=200)
    remove_members: list[VoiceReferenceInput] = Field(default_factory=list, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def reject_overlapping_members(self) -> "UpdateVoiceCollectionInput":
        def key(reference: VoiceReferenceInput) -> tuple[Any, ...]:
            return (
                reference.kind,
                reference.voice_id,
                reference.service_id,
                reference.model,
                reference.voice,
            )

        overlap = {key(item) for item in self.add_members} & {
            key(item) for item in self.remove_members
        }
        if overlap:
            raise ValueError("A voice reference cannot be added and removed in one update.")
        return self


class CatalogVoiceMetadataChanges(ToolInput):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    voice_category: VoiceCategory | None = None
    profile: dict[str, Any] | None = None

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _bounded_json_object(value, name="profile", maximum=_PROFILE_MAX_BYTES)

    @model_validator(mode="after")
    def require_change(self) -> "CatalogVoiceMetadataChanges":
        if not self.model_fields_set:
            raise ValueError("At least one metadata field must be provided.")
        return self


class UpdateCatalogVoiceMetadataInput(_RevisionWriteInput):
    reference: VoiceReferenceInput
    expected_revision: int = Field(ge=0)
    changes: CatalogVoiceMetadataChanges

    @model_validator(mode="after")
    def require_provider_reference(self) -> "UpdateCatalogVoiceMetadataInput":
        if self.reference.kind != "provider":
            raise ValueError("Catalog voice metadata overrides require a provider reference.")
        return self


__all__ = [
    "AuditionVoiceInput",
    "CatalogVoiceMetadataChanges",
    "CreateVoiceCollectionInput",
    "GetVoiceSamplesInput",
    "ImportVoiceReferenceInput",
    "ListVoiceCollectionsInput",
    "Pitch",
    "PromoteVoiceDesignInput",
    "PublishVoiceInput",
    "ReviewVoiceTranscriptInput",
    "TranscribeVoiceSampleInput",
    "UpdateCatalogVoiceMetadataInput",
    "UpdateVoiceCollectionInput",
    "VoiceCatalogInput",
    "VoiceCatalogCapabilitiesInput",
    "VoiceCategory",
    "VoiceCreateInput",
    "VoiceReferenceInput",
]
