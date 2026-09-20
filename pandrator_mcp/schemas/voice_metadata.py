"""Portable managed-voice metadata update contracts."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .common import ToolInput

VoiceCategory = Literal["male", "female", "androgynous", "unspecified"]


class _StrictModel(ToolInput):
    """Keep this portable contract independent of the application package."""

    model_config = ConfigDict(extra="forbid", strict=True)


class VoiceMetadataChanges(_StrictModel):
    """The explicitly supplied fields to patch on one managed voice."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    language: str | None = Field(default=None, max_length=40)
    description: str | None = None
    voice_category: VoiceCategory | None = None
    profile: dict[str, Any] | None = None

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            size = len(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Voice profile must contain finite JSON values.") from error
        if size > 64 * 1024:
            raise ValueError("Voice profile may not exceed 64 KiB.")
        return value

    @model_validator(mode="after")
    def require_explicit_change(self) -> "VoiceMetadataChanges":
        fields = {"name", "language", "description", "voice_category", "profile"}
        if not self.model_fields_set.intersection(fields):
            raise ValueError("At least one voice metadata field must be provided.")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Voice name cannot be cleared.")
        return self


class UpdateVoiceMetadataInput(_StrictModel):
    """Input for updating one managed voice with an If-Match revision."""

    voice_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)
    changes: VoiceMetadataChanges


VOICE_METADATA_INPUT_MODELS = (UpdateVoiceMetadataInput,)


__all__ = [
    "VOICE_METADATA_INPUT_MODELS",
    "UpdateVoiceMetadataInput",
    "VoiceCategory",
    "VoiceMetadataChanges",
]
