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
            if (
                normalized in _FORBIDDEN_SETTING_KEYS
                or normalized.endswith(
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
    workflow_kind: Literal[
        "audiobook",
        "subtitles",
        "voiceover",
    ] | None = None
    state: str | None = Field(default=None, max_length=40)


class GetSessionInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)


class GetWorkflowInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)


class GetSessionSettingsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    section: _SETTING_SECTIONS


class ListSourcesInput(ToolInput):
    state: Literal["current", "trashed"] = "current"
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
    workflow_kind: Literal[
        "audiobook",
        "subtitles",
        "voiceover",
    ] | None = None
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
            raise ValueError(
                "At least one session field must be changed."
            )
        if (
            self.included_stages is not None
            and len(self.included_stages)
            != len(set(self.included_stages))
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
            raise ValueError(
                "Session settings must be finite JSON values."
            ) from error
        if len(encoded) > 128 * 1024:
            raise ValueError(
                "Session settings exceed the MCP size limit."
            )
        return value
