"""Typed MCP inputs for the reusable pronunciation dictionary."""

from __future__ import annotations

import json
import math
from typing import Any, Literal, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from .common import ToolInput

PronunciationScope = Literal["global", "session"]
PronunciationStatus = Literal["proposed", "reviewed", "disabled"]
PronunciationAlphabet = Literal["respelling"]


def _validate_json_value(value: Any) -> None:
    """Reject Python-only values so metadata remains finite JSON data."""

    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Pronunciation metadata must contain finite JSON values.")
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("Pronunciation metadata object keys must be strings.")
        for item in value.values():
            _validate_json_value(item)
        return
    raise ValueError("Pronunciation metadata must contain finite JSON values.")


def _validate_metadata_size(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    _validate_json_value(value)
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
        raise ValueError("Pronunciation metadata must contain finite JSON values.") from error
    if size > 16 * 1024:
        raise ValueError("Pronunciation metadata may not exceed 16 KiB.")
    return value


class _StrictModel(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListPronunciationsInput(_StrictModel):
    """Filters for reusable pronunciation entries."""

    query: str | None = None
    language: str | None = None
    status: PronunciationStatus | None = None
    scope: PronunciationScope | None = None
    session_id: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class CreatePronunciationInput(_StrictModel):
    """Create one reusable pronunciation dictionary entry."""

    source_form: str = Field(min_length=1, max_length=512)
    phonetic: str = Field(min_length=1, max_length=1024)
    language: str = Field(default="und", min_length=2, max_length=40)
    backend: str = Field(default="*", min_length=1, max_length=80)
    scope: PronunciationScope = "global"
    session_id: str | None = None
    status: PronunciationStatus = "reviewed"
    alphabet: PronunciationAlphabet = "respelling"
    notes: str | None = Field(default=None, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_metadata_size(value) or {}

    @model_validator(mode="after")
    def validate_scope(self) -> "CreatePronunciationInput":
        if self.scope == "session" and (self.session_id is None or not self.session_id.strip()):
            raise ValueError("Session-scoped pronunciations require a session_id.")
        if self.scope == "global" and self.session_id is not None:
            raise ValueError("Global pronunciations cannot include a session_id.")
        return self


class PronunciationChanges(_StrictModel):
    """A non-empty patch matching the application's PronunciationUpdate."""

    # Omission is represented by a null default, while non-nullable API fields
    # keep non-null annotations and therefore reject an explicitly supplied null.
    source_form: str = Field(default=cast(str, None), min_length=1, max_length=512)
    phonetic: str = Field(default=cast(str, None), min_length=1, max_length=1024)
    language: str = Field(default=cast(str, None), min_length=2, max_length=40)
    backend: str = Field(default=cast(str, None), min_length=1, max_length=80)
    scope: PronunciationScope = cast(PronunciationScope, None)
    session_id: str | None = None
    status: PronunciationStatus = cast(PronunciationStatus, None)
    alphabet: PronunciationAlphabet = cast(PronunciationAlphabet, None)
    notes: str | None = Field(default=None, max_length=4000)
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_metadata_size(value)

    @model_validator(mode="after")
    def require_valid_patch(self) -> "PronunciationChanges":
        fields = {
            "source_form",
            "phonetic",
            "language",
            "backend",
            "scope",
            "session_id",
            "status",
            "alphabet",
            "notes",
            "metadata",
        }
        if not self.model_fields_set.intersection(fields):
            raise ValueError("At least one pronunciation field must be provided.")
        if "scope" in self.model_fields_set:
            if (
                self.scope == "session"
                and "session_id" in self.model_fields_set
                and (self.session_id is None or not self.session_id.strip())
            ):
                raise ValueError("Session-scoped pronunciations require a session_id.")
            if (
                self.scope == "global"
                and "session_id" in self.model_fields_set
                and self.session_id is not None
            ):
                raise ValueError("Global pronunciations cannot include a session_id.")
        return self


class UpdatePronunciationInput(_StrictModel):
    """Update one entry at its current revision."""

    entry_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    changes: PronunciationChanges


class DeletePronunciationInput(_StrictModel):
    """Delete one entry at its current revision."""

    entry_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)


PRONUNCIATION_INPUT_MODELS = (
    ListPronunciationsInput,
    CreatePronunciationInput,
    UpdatePronunciationInput,
    DeletePronunciationInput,
)

__all__ = [
    "CreatePronunciationInput",
    "DeletePronunciationInput",
    "ListPronunciationsInput",
    "PRONUNCIATION_INPUT_MODELS",
    "PronunciationAlphabet",
    "PronunciationChanges",
    "PronunciationScope",
    "PronunciationStatus",
    "UpdatePronunciationInput",
]
