"""Strict request contracts for the session-independent sound library."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

DEFAULT_SOUND_MODEL_ID = "stable_audio_3_small_sfx_q8_0"
_UINT32_MAX = 2**32 - 1
_TAG = Annotated[str, Field(max_length=64)]
_PARAMETER_STRING = Annotated[StrictStr, Field(max_length=2000)]
_PARAMETER_KEY = Annotated[StrictStr, Field(max_length=64)]
_ParameterValue = _PARAMETER_STRING | StrictBool | StrictInt | StrictFloat


class SoundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("name", check_fields=False)
    @classmethod
    def nonblank_name(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("Sound names must contain non-whitespace text.")
        return value

    @field_validator("tags", check_fields=False)
    @classmethod
    def normalized_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        tags = [tag.strip() for tag in value]
        if any(not tag for tag in tags):
            raise ValueError("Sound tags must contain non-whitespace text.")
        return list(dict.fromkeys(tags))


class SoundGenerate(SoundRequest):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    prompt: StrictStr = Field(max_length=8000)
    model_id: str = Field(default=DEFAULT_SOUND_MODEL_ID, min_length=1, max_length=255)
    candidate_count: StrictInt = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "Generate this many candidates in one request with the same settings and distinct "
            "seeds. An explicit seed is the base; subsequent seeds increment with wrap."
        ),
    )
    duration_seconds: float | None = Field(
        default=None, ge=1.0, le=600.0, allow_inf_nan=False
    )
    kind: Literal["sound", "music"] | None = None
    backend: Literal["cpu", "vulkan", "cuda"] = "cpu"
    lyrics: StrictStr | None = Field(default=None, max_length=16000)
    parameters: dict[_PARAMETER_KEY, _ParameterValue] | None = Field(
        default=None, max_length=32
    )
    seed: int | None = Field(default=None, ge=0, le=_UINT32_MAX)
    threads: int | None = Field(default=None, ge=1)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(
        cls, value: dict[str, str | bool | int | float] | None
    ) -> dict[str, str | bool | int | float] | None:
        if value is None:
            return None
        if len(value) > 32:
            raise ValueError("parameters may contain at most 32 entries.")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 64:
                raise ValueError("parameter names must be strings of at most 64 characters.")
            if isinstance(item, str):
                if len(item) > 2000:
                    raise ValueError("parameter strings may contain at most 2000 characters.")
            elif type(item) is float and not math.isfinite(item):
                raise ValueError("parameter numbers must be finite.")
            elif type(item) not in (bool, int, float):
                raise ValueError("parameter values must be strings, booleans, integers, or numbers.")
        return value


class SoundSave(SoundRequest):
    candidate_id: str = Field(min_length=1, max_length=36)
    name: str | None = Field(default=None, max_length=255)
    tags: list[_TAG] | None = Field(default=None, max_length=20)
    item_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_save(self) -> SoundSave:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name must be a string when provided.")
        if "tags" in self.model_fields_set and self.tags is None:
            raise ValueError("tags must be a list when provided.")
        if self.item_id is not None and self.expected_revision is None:
            raise ValueError("expected_revision is required when item_id is provided.")
        return self


class SoundUpdate(SoundRequest):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=255)
    tags: list[_TAG] | None = Field(default=None, max_length=20)
    archived: bool | None = None
    preferred_variant_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def validate_patch_values(self) -> SoundUpdate:
        for field_name in ("name", "tags", "archived", "preferred_variant_id"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be non-null when provided.")
        return self
