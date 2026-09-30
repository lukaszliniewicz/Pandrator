"""Strict MCP fields for serial or bounded-parallel delegated batch context."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, model_validator

from .common import ToolInput

MAX_PARALLEL_BATCHES = 8
_CONTEXT_CAPSULE_BYTES = 128 * 1024
_CONTEXT_DELTA_BYTES = 32 * 1024
_ContextKey = Annotated[str, Field(min_length=1, max_length=200)]
_ContextValue = Annotated[str, Field(min_length=1, max_length=2_000)]
_ContextNote = Annotated[str, Field(min_length=1, max_length=2_000)]


def execution_policy_json_schema(schema: dict[str, Any]) -> None:
    """Add self-contained serial/parallel alternatives to a tool schema.

    Some MCP clients render each ``oneOf`` branch as the entire input object.
    Copying the base object's fields and required arguments into every branch
    keeps those clients from dropping the tool's ordinary parameters.
    """

    base = deepcopy(schema)
    base.pop("oneOf", None)
    base_properties = base.get("properties")
    if not isinstance(base_properties, dict):
        raise ValueError("Delegation execution schemas require object properties.")

    base_required = base.get("required", [])
    if not isinstance(base_required, list):
        base_required = []

    alternatives: list[dict[str, Any]] = []
    for mode in ("serial", "parallel"):
        alternative = deepcopy(base)
        properties = alternative["properties"]

        execution_mode = deepcopy(properties.get("execution_mode", {}))
        execution_mode["const"] = mode
        properties["execution_mode"] = execution_mode

        parallel_width = deepcopy(properties.get("max_parallel_batches", {}))
        if mode == "serial":
            parallel_width["const"] = 1
        else:
            parallel_width.pop("const", None)
            parallel_width["minimum"] = 2
            parallel_width["maximum"] = MAX_PARALLEL_BATCHES
        properties["max_parallel_batches"] = parallel_width

        required = list(base_required)
        if mode == "parallel":
            for field in ("execution_mode", "max_parallel_batches"):
                if field not in required:
                    required.append(field)
        if required or "required" in alternative:
            alternative["required"] = required
        alternatives.append(alternative)

    schema["oneOf"] = alternatives


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
        json_schema_extra=execution_policy_json_schema,
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
