"""Strict MCP fields for serial or bounded-parallel delegated batch context."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from .common import ToolInput

MAX_PARALLEL_BATCHES = 8
_CONTEXT_CAPSULE_BYTES = 128 * 1024
_CONTEXT_DELTA_BYTES = 32 * 1024
_ContextKey = Annotated[str, Field(min_length=1, max_length=200)]
_ContextValue = Annotated[str, Field(min_length=1, max_length=2_000)]
_ContextNote = Annotated[str, Field(min_length=1, max_length=2_000)]


def execution_policy_json_schema() -> dict[str, object]:
    """Return the cross-field JSON Schema used by model-visible tool inputs."""

    return {
        "oneOf": [
            {
                "properties": {
                    "execution_mode": {"const": "serial"},
                    "max_parallel_batches": {"const": 1},
                }
            },
            {
                "required": ["execution_mode", "max_parallel_batches"],
                "properties": {
                    "execution_mode": {"const": "parallel"},
                    "max_parallel_batches": {"minimum": 2, "maximum": 8},
                },
            },
        ]
    }


def _encoded_size(value: ToolInput) -> int:
    return len(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


class _ContextFields(ToolInput):
    terminology: dict[_ContextKey, _ContextValue] = Field(
        default_factory=dict,
        max_length=500,
    )
    entities: dict[_ContextKey, _ContextValue] = Field(
        default_factory=dict,
        max_length=500,
    )
    style_rules: list[_ContextNote] = Field(default_factory=list, max_length=200)
    decisions: list[_ContextNote] = Field(default_factory=list, max_length=200)
    notes: list[_ContextNote] = Field(default_factory=list, max_length=200)


class DelegationContextDeltaInput(_ContextFields):
    @model_validator(mode="after")
    def validate_encoded_size(self):
        if _encoded_size(self) > _CONTEXT_DELTA_BYTES:
            raise ValueError("Context delta exceeds the 32 KiB limit.")
        return self


class DelegationContextCapsuleInput(_ContextFields):
    overview: str = Field(default="", max_length=16_000)

    @model_validator(mode="after")
    def validate_encoded_size(self):
        if _encoded_size(self) > _CONTEXT_CAPSULE_BYTES:
            raise ValueError("Context capsule exceeds the 128 KiB limit.")
        return self


class DelegationExecutionMixin(ToolInput):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=execution_policy_json_schema(),
    )
    execution_mode: Literal["serial", "parallel"] = "serial"
    max_parallel_batches: int = Field(default=1, ge=1, le=MAX_PARALLEL_BATCHES)
    context_capsule: DelegationContextCapsuleInput = Field(
        default_factory=DelegationContextCapsuleInput
    )

    @model_validator(mode="after")
    def validate_execution_width(self):
        if self.execution_mode == "serial" and self.max_parallel_batches != 1:
            raise ValueError("Serial execution requires max_parallel_batches=1.")
        if self.execution_mode == "parallel" and self.max_parallel_batches < 2:
            raise ValueError("Parallel execution requires max_parallel_batches from 2 to 8.")
        return self


__all__ = [
    "DelegationContextCapsuleInput",
    "DelegationContextDeltaInput",
    "DelegationExecutionMixin",
    "MAX_PARALLEL_BATCHES",
    "execution_policy_json_schema",
]
