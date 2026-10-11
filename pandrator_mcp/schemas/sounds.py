"""Strict MCP contracts for sound-library and project-sound operations."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from .common import ToolInput
from .project_sounds import ProjectSoundsInput, SoundAnchorInput, SoundPlaybackInput

_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
_UINT32_MAX = 2**32 - 1
_Tag = Annotated[StrictStr, Field(max_length=64)]
_IdempotencyKey = Annotated[
    StrictStr,
    Field(min_length=8, max_length=200, pattern=_IDEMPOTENCY_PATTERN),
]
DEFAULT_SOUND_MODEL_ID = "stable_audio_3_small_sfx_q8_0"


class _StrictInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SoundCatalogueInput(_StrictInput):
    """Read the local audio-generation model catalogue."""

    model_id: StrictStr | None = Field(default=None, min_length=1, max_length=255)
    kind: Literal["sound", "music"] | None = None


class GenerateSoundInput(_StrictInput):
    prompt: StrictStr = Field(max_length=8000)
    model_id: StrictStr = Field(
        default=DEFAULT_SOUND_MODEL_ID, min_length=1, max_length=255
    )
    candidate_count: StrictInt = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "Generate this many candidates in one request with the same settings and distinct "
            "seeds. An explicit seed is the base; subsequent seeds increment with wrap."
        ),
    )
    duration_seconds: StrictFloat | None = Field(default=None, ge=1.0, le=600.0)
    kind: Literal["sound", "music"] | None = None
    backend: Literal["cpu", "vulkan", "cuda"] | None = None
    lyrics: Annotated[StrictStr, Field(max_length=16000)] | None = None
    parameters: dict[
        Annotated[StrictStr, Field(max_length=64)],
        Annotated[StrictStr, Field(max_length=2000)] | StrictBool | StrictInt | StrictFloat,
    ] | None = Field(default=None, max_length=32)
    seed: StrictInt | None = Field(default=None, ge=0, le=_UINT32_MAX)
    threads: StrictInt | None = Field(default=None, ge=1)
    idempotency_key: _IdempotencyKey

    @model_validator(mode="after")
    def validate_parameter_bounds(self) -> GenerateSoundInput:
        if self.parameters is not None:
            if len(self.parameters) > 32:
                raise ValueError("parameters may contain at most 32 entries.")
            for key, value in self.parameters.items():
                if len(key) > 64:
                    raise ValueError("parameter names may contain at most 64 characters.")
                if isinstance(value, str) and len(value) > 2000:
                    raise ValueError("parameter strings may contain at most 2000 characters.")
        return self


class SoundItemChangesInput(_StrictInput):
    name: StrictStr | None = Field(default=None, min_length=1, max_length=255)
    tags: list[_Tag] | None = Field(default=None, max_length=20)
    archived: StrictBool | None = None
    preferred_variant_id: StrictStr | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def require_non_null_change(self) -> SoundItemChangesInput:
        if not self.model_fields_set:
            raise ValueError("Provide at least one sound-library change.")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Sound-library changes must be non-null when provided.")
        if "name" in self.model_fields_set and self.name is not None and not self.name.strip():
            raise ValueError("Sound name may not be blank.")
        return self


class SoundLibraryInput(_StrictInput):
    action: Literal["list", "get", "candidates", "candidate", "save", "update", "discard"]

    query: StrictStr = Field(default="", max_length=200)
    tag: StrictStr | None = Field(default=None, max_length=64)
    cursor: StrictStr | None = Field(default=None, max_length=1024)
    limit: StrictInt = Field(default=40, ge=1, le=100)
    archived: StrictBool = False
    include_detail: StrictBool = False

    item_id: StrictStr | None = Field(default=None, min_length=1, max_length=36)
    candidate_id: StrictStr | None = Field(default=None, min_length=1, max_length=36)
    name: StrictStr | None = Field(default=None, min_length=1, max_length=255)
    tags: list[_Tag] | None = Field(default=None, max_length=20)
    expected_revision: StrictInt | None = Field(default=None, ge=1)
    changes: SoundItemChangesInput | None = None
    idempotency_key: _IdempotencyKey | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> SoundLibraryInput:
        action_fields = {
            "list": {"query", "tag", "cursor", "limit", "archived"},
            "get": {"item_id", "cursor", "limit", "include_detail"},
            "candidates": {"cursor", "limit"},
            "candidate": {"candidate_id", "include_detail"},
            "save": {
                "candidate_id",
                "name",
                "tags",
                "item_id",
                "expected_revision",
                "idempotency_key",
            },
            "update": {"item_id", "expected_revision", "changes", "idempotency_key"},
            "discard": {"candidate_id", "idempotency_key"},
        }
        supplied = self.model_fields_set - {"action"}
        unsupported = supplied - action_fields[self.action]
        if unsupported:
            raise ValueError(
                f"Fields {sorted(unsupported)} are not valid for sound-library action {self.action}."
            )

        if self.include_detail and self.action not in {"get", "candidate"}:
            raise ValueError("include_detail is available only for one item or candidate.")

        if self.action == "get":
            self._require("item_id")
        elif self.action == "candidate":
            self._require("candidate_id")
        elif self.action == "save":
            self._require("candidate_id", "idempotency_key")
            if self.item_id is not None:
                self._require("expected_revision")
                if "name" in supplied or "tags" in supplied:
                    raise ValueError(
                        "Attaching a candidate to an item cannot change its name or tags."
                    )
            else:
                if "expected_revision" in supplied:
                    raise ValueError("expected_revision is used only when attaching to an item.")
                if self.name is None or not self.name.strip():
                    raise ValueError("Saving a new sound item requires a non-blank name.")
                if "tags" in supplied and self.tags is None:
                    raise ValueError("tags must be a list when provided.")
        elif self.action == "update":
            self._require("item_id", "expected_revision", "changes", "idempotency_key")
        elif self.action == "discard":
            self._require("candidate_id", "idempotency_key")
        return self

    def _require(self, *fields: str) -> None:
        missing = [field for field in fields if getattr(self, field) is None]
        if missing:
            raise ValueError(f"Sound-library action {self.action} requires {', '.join(missing)}.")


class ImportAudioReferenceInput(_StrictInput):
    root_name: StrictStr = Field(min_length=1, max_length=80)
    relative_path: StrictStr = Field(min_length=1, max_length=1024)
    target: Literal["sound", "voice_reference"]
    idempotency_key: _IdempotencyKey


SOUND_INPUT_MODELS = (
    SoundCatalogueInput,
    GenerateSoundInput,
    SoundLibraryInput,
    ImportAudioReferenceInput,
    ProjectSoundsInput,
)


__all__ = [
    "DEFAULT_SOUND_MODEL_ID",
    "SOUND_INPUT_MODELS",
    "GenerateSoundInput",
    "ImportAudioReferenceInput",
    "ProjectSoundsInput",
    "SoundAnchorInput",
    "SoundCatalogueInput",
    "SoundItemChangesInput",
    "SoundLibraryInput",
    "SoundPlaybackInput",
]
