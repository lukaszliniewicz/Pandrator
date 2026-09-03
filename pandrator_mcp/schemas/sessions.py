"""Read and revision-safe session tool arguments."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .common import ToolInput

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
_STAGES = Literal[
    "transcribe",
    "correct",
    "translate",
    "clean_source",
    "prepare_text",
    "optimize_document",
    "optimize_tts",
    "generate_audio",
    "export",
]
_SETTING_SECTIONS = Literal[
    "text",
    "stt",
    "subtitles",
    "correction",
    "translation",
    "tts",
    "audio",
    "rvc",
    "source_cleaning",
    "output",
]
_FORBIDDEN_SETTING_KEYS = frozenset(
    {
        "api_base",
        "api_key",
        "authorization",
        "base_url",
        "ca_bundle",
        "command",
        "connection",
        "credential",
        "credential_reference",
        "endpoint",
        "external_path",
        "host",
        "origin",
        "password",
        "path",
        "port",
        "private_key",
        "proxy",
        "proxy_origin",
        "secret",
        "token",
        "url",
        "workspace",
    }
)


def _guard_settings(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> None:
    if depth > 10:
        raise ValueError("Session settings are nested too deeply.")
    counter = budget if budget is not None else [0]
    counter[0] += 1
    if counter[0] > 2_000:
        raise ValueError("Session settings contain too many values.")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_SETTING_KEYS or normalized.endswith(
                (
                    "_api_key",
                    "_command",
                    "_credential",
                    "_endpoint",
                    "_origin",
                    "_password",
                    "_path",
                    "_private_key",
                    "_proxy",
                    "_secret",
                    "_token",
                    "_url",
                )
            ):
                raise ValueError(
                    "Session settings cannot contain paths, commands, "
                    "credentials, or connection endpoints."
                )
            _guard_settings(
                item,
                depth=depth + 1,
                budget=counter,
            )
    elif isinstance(value, (list, tuple)):
        for item in value:
            _guard_settings(
                item,
                depth=depth + 1,
                budget=counter,
            )


class ListSessionsInput(ToolInput):
    limit: int = Field(default=50, ge=1, le=100)
    workflow_kind: (
        Literal[
            "audiobook",
            "subtitles",
            "voiceover",
        ]
        | None
    ) = None
    state: str | None = Field(default=None, max_length=40)
    query: str | None = Field(
        default=None,
        max_length=100,
        description="Optional search term to filter sessions by name, language, or identifier.",
    )


class GetSessionInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)


class GetWorkflowInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)


SubtitleStage = Literal[
    "transcribe",
    "transcription",
    "correct",
    "correction",
    "translate",
    "translation",
    "tts_optimized",
    "tts_optimization",
]


class PreviewSubtitlesInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    stage: SubtitleStage | None = None
    artifact_id: str | None = Field(default=None, max_length=80)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
    query: str | None = Field(
        default=None,
        max_length=100,
        description="Optional search term to filter cues by text or speaker.",
    )
    around_ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Center the view on a specific cue ordinal with surrounding context.",
    )
    context: int = Field(
        default=3,
        ge=0,
        le=20,
        description="Number of context cues before and after around_ordinal.",
    )
    start_ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Optional 1-indexed starting cue ordinal to slice.",
    )
    end_ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Optional 1-indexed ending cue ordinal (inclusive) to slice.",
    )


class ReplaceSubtitleTextInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    stage: SubtitleStage
    expected_revision: int = Field(ge=1)
    search_text: str = Field(
        min_length=1,
        max_length=500,
        description="The substring, word, or pattern to find in subtitle cues.",
    )
    replacement_text: str = Field(
        max_length=500,
        description="The text to replace matching occurrences with.",
    )
    match_case: bool = Field(
        default=False,
        description="Whether case matching should be exact.",
    )
    whole_word: bool = Field(
        default=True,
        description="Whether to match whole words only (prevents accidental substring replacements).",
    )
    is_regex: bool = Field(
        default=False,
        description="Treat search_text as a regular expression.",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, returns matches and proposed diff without mutating the database.",
    )
    idempotency_key: str = Field(min_length=1, max_length=120)


class CuePatchInput(ToolInput):
    ordinal: int = Field(ge=1, description="The 1-indexed cue number to patch.")
    text: str | None = Field(default=None, max_length=2000, description="New text for this cue.")
    speaker: str | None = Field(default=None, max_length=160, description="New speaker label.")
    start_ms: int | None = Field(
        default=None, ge=0, description="Optional start time in milliseconds."
    )
    end_ms: int | None = Field(default=None, ge=0, description="Optional end time in milliseconds.")


class PatchSubtitleCuesInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    stage: SubtitleStage
    expected_revision: int = Field(ge=1)
    cues: list[CuePatchInput] = Field(
        min_length=1,
        max_length=100,
        description="List of cue patches to apply by ordinal.",
    )
    idempotency_key: str = Field(min_length=1, max_length=120)


class ImportSubtitlesInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    stage: SubtitleStage
    expected_revision: int = Field(
        ge=0,
        description="Current subtitle revision; use 0 to create the first document for this stage.",
    )
    srt_content: str | None = Field(
        default=None,
        description="Raw SRT format text content to import.",
    )
    filename: str | None = Field(
        default=None,
        max_length=255,
        description="Relative path of an SRT file inside the workspace exports or source root.",
    )
    idempotency_key: str = Field(min_length=1, max_length=120)


class GetSessionSettingsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    section: _SETTING_SECTIONS


class ListSourcesInput(ToolInput):
    state: Literal["current", "trashed"] = "current"
    query: str | None = Field(default=None, max_length=160)
    kind: str | None = Field(default=None, max_length=80)
    mime_type: str | None = Field(default=None, max_length=160)
    limit: int = Field(default=50, ge=1, le=100)


class CreateSessionInput(ToolInput):
    name: str = Field(min_length=1, max_length=200)
    workflow_kind: Literal[
        "audiobook",
        "subtitles",
        "voiceover",
    ] = "audiobook"
    source_language: str = Field(
        default="auto",
        min_length=2,
        max_length=40,
    )
    target_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=40,
    )
    workflow_preset: str = Field(
        default="custom",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    included_stages: tuple[_STAGES, ...] = Field(
        default=(),
        max_length=12,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @field_validator("included_stages")
    @classmethod
    def unique_stages(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Included workflow stages must be unique.")
        return value


class UpdateSessionInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    workflow_kind: (
        Literal[
            "audiobook",
            "subtitles",
            "voiceover",
        ]
        | None
    ) = None
    source_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=40,
    )
    target_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=40,
    )
    workflow_preset: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    included_stages: tuple[_STAGES, ...] | None = Field(
        default=None,
        max_length=12,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @model_validator(mode="after")
    def require_change(self) -> "UpdateSessionInput":
        changes = self.model_fields_set - {
            "session_id",
            "expected_revision",
            "idempotency_key",
        }
        if not changes:
            raise ValueError("At least one session field must be changed.")
        if self.included_stages is not None and len(self.included_stages) != len(
            set(self.included_stages)
        ):
            raise ValueError("Included workflow stages must be unique.")
        return self

    def changes(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={
                "session_id",
                "expected_revision",
                "idempotency_key",
            },
            exclude_unset=True,
        )


class AttachExistingSourceInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    source_asset_id: str = Field(min_length=1, max_length=80)
    role: Literal["primary", "reference"] = "primary"
    expected_session_revision: int = Field(ge=1)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class UpdateSessionSettingsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    section: _SETTING_SECTIONS
    expected_revision: int = Field(ge=0)
    value: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @field_validator("value")
    @classmethod
    def validate_value(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        _guard_settings(value)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("Session settings must be finite JSON values.") from error
        if len(encoded) > 128 * 1024:
            raise ValueError("Session settings exceed the MCP size limit.")
        return value
