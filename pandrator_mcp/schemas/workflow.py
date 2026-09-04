"""Reviewed workflow-plan tool arguments."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from .common import ToolInput
from .delegation import DelegationExecutionMixin

_CONNECTION_OR_SECRET_KEYS = frozenset(
    {
        "api_base",
        "api_key",
        "application_origin",
        "authorization",
        "base_url",
        "ca_bundle",
        "connection",
        "credential",
        "credential_reference",
        "endpoint",
        "host",
        "origin",
        "password",
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
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
_PASSIVE_STAGE_ORDER = (
    "correction",
    "translation",
    "speech_optimization",
)


def _guard_overrides(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> None:
    if depth > 10:
        raise ValueError("Workflow overrides are nested too deeply.")
    counter = budget if budget is not None else [0]
    counter[0] += 1
    if counter[0] > 2_000:
        raise ValueError("Workflow overrides contain too many values.")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _CONNECTION_OR_SECRET_KEYS or normalized.endswith(
                (
                    "_api_key",
                    "_credential",
                    "_password",
                    "_private_key",
                    "_secret",
                    "_token",
                    "_url",
                )
            ):
                raise ValueError(
                    "Workflow overrides cannot contain credentials or connection endpoints."
                )
            _guard_overrides(
                item,
                depth=depth + 1,
                budget=counter,
            )
    elif isinstance(value, (list, tuple)):
        for item in value:
            _guard_overrides(
                item,
                depth=depth + 1,
                budget=counter,
            )


class PlanWorkflowInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    target_stage: Literal[
        "transcribe",
        "correct",
        "translate",
        "clean_source",
        "prepare_text",
        "optimize_document",
        "optimize_tts",
        "generate_audio",
        "export",
    ] = "generate_audio"
    overrides: dict[str, Any] = Field(default_factory=dict)
    expires_in_minutes: int = Field(default=30, ge=1, le=60)

    @field_validator("overrides")
    @classmethod
    def validate_overrides(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        _guard_overrides(value)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("Workflow overrides must be finite JSON values.") from error
        if len(encoded) > 128 * 1024:
            raise ValueError("Workflow overrides exceed the MCP size limit.")
        return value


class PlanOrchestratedWorkflowInput(DelegationExecutionMixin):
    """Arguments for a read-only, model-operated workflow procedure."""

    session_id: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=1_000)
    passive_stages: tuple[Literal["correction", "translation", "speech_optimization"], ...] = Field(
        default=(), max_length=3
    )
    final_stage: Literal["generate_audio", "export"] = "export"
    overrides: dict[str, Any] = Field(default_factory=dict)
    export_mode: Literal["media", "subtitles", "text"] = "media"
    audio_mode: Literal["preserve", "mixed", "dubbing_only"] = "mixed"
    subtitle_mode: Literal["none", "soft", "burned"] = "none"
    subtitle_selection: Literal["source", "translation", "dual"] = "translation"
    subtitle_format: Literal["srt", "vtt"] = "srt"
    materialize: bool = False
    filename: str | None = Field(default=None, max_length=255)
    wait_seconds: int = Field(default=0, ge=0, le=3_600)
    expires_in_minutes: int = Field(default=30, ge=1, le=60)

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Workflow goals must not be blank.")
        return value

    @field_validator("passive_stages")
    @classmethod
    def normalize_passive_stages(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Passive workflow stages must be unique.")
        order = {stage: index for index, stage in enumerate(_PASSIVE_STAGE_ORDER)}
        return tuple(sorted(value, key=order.__getitem__))

    @field_validator("overrides")
    @classmethod
    def validate_overrides(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        _guard_overrides(value)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("Workflow overrides must be finite JSON values.") from error
        if len(encoded) > 128 * 1024:
            raise ValueError("Workflow overrides exceed the MCP size limit.")
        return value

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value.strip()
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise ValueError(
                "Workflow export filename must be a plain filename without path components."
            )
        return value

    @model_validator(mode="after")
    def validate_materialization(self) -> "PlanOrchestratedWorkflowInput":
        if self.final_stage != "export" and (self.materialize or self.filename is not None):
            raise ValueError("Materialization is only available when final_stage is export.")
        if self.filename is not None and not self.materialize:
            raise ValueError("A filename requires materialize=true.")
        return self


_PARAMETER_SECTIONS = Literal[
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


class DescribeParametersInput(ToolInput):
    """Filtered parameter-definition discovery arguments."""

    sections: tuple[_PARAMETER_SECTIONS, ...] = Field(default=(), max_length=10)
    names: tuple[Annotated[str, Field(min_length=1, max_length=50)], ...] = Field(
        default=(),
        max_length=50,
    )
    workflow_kind: Literal["audiobook", "subtitles", "voiceover"] | None = None
    query: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=100, ge=1, le=300)

    @field_validator("sections")
    @classmethod
    def validate_sections(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Parameter sections must be unique.")
        return value

    @field_validator("names")
    @classmethod
    def validate_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name.strip() for name in value):
            raise ValueError("Parameter names must not be blank.")
        if len(value) != len(set(value)):
            raise ValueError("Parameter names must be unique.")
        return value

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Parameter query must not be blank.")
        return value

    @model_validator(mode="after")
    def require_filter(self) -> "DescribeParametersInput":
        if not (self.sections or self.names or self.workflow_kind or self.query):
            raise ValueError("Provide at least one section, name, workflow_kind, or query filter.")
        return self


class ExecuteWorkflowPlanInput(ToolInput):
    plan_id: str = Field(min_length=1, max_length=120)
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted_confirmations: tuple[str, ...] = Field(
        default=(),
        max_length=20,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY.pattern,
    )


# Compatibility for integrations that imported the Phase-1 placeholder.
RunWorkflowInput = ExecuteWorkflowPlanInput
