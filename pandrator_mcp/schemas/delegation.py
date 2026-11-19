"""Strict MCP fields for serial or bounded-parallel delegated batch context."""

from __future__ import annotations

import json
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
    """Keep flat tool arguments once, with conditional execution constraints.

    Avoid object alternatives: clients can render them as replacement signatures,
    and copying every field into both branches triples discovery payloads.
    Runtime validators enforce the same constraints for simpler schema clients.
    """

    if not isinstance(schema.get("properties"), dict):
        raise ValueError("Delegation execution schemas require object properties.")
    schema.pop("oneOf", None)
    schema["allOf"] = [
        {
            "if": {"properties": {"execution_mode": {"const": "serial"}}},
            "then": {"properties": {"max_parallel_batches": {"const": 1}}},
        },
        {
            "if": {
                "properties": {"execution_mode": {"const": "parallel"}},
                "required": ["execution_mode"],
            },
            "then": {
                "properties": {"max_parallel_batches": {"minimum": 2, "maximum": MAX_PARALLEL_BATCHES}},
                "required": ["max_parallel_batches"],
            },
        },
    ]


def native_execution_policy_json_schema(schema: dict[str, Any]) -> None:
    """Keep host signatures flat while documenting runtime cross-field rules.

    Some MCP hosts convert root conditionals into opaque intersection types.
    Portable Pydantic schemas retain the conditionals above; native callers
    receive ordinary properties and are checked by the same input validator.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Delegation execution schemas require object properties.")
    schema.pop("allOf", None)
    schema.pop("oneOf", None)
    rule = "Serial requires max_parallel_batches=1; parallel requires 2 through 8."
    for name in ("execution_mode", "max_parallel_batches"):
        field = properties.get(name)
        if isinstance(field, dict):
            field["description"] = rule
    schema["x-pandrator-execution-policy"] = {
        "serial": {"max_parallel_batches": 1},
        "parallel": {"minimum": 2, "maximum": MAX_PARALLEL_BATCHES},
        "enforced": "runtime",
    }


def _blank_context_value(value: object) -> bool:
    """Whether an optional context value or note carries no content."""

    return isinstance(value, str) and not value.strip()


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
        description="Source term to preferred renderings. Empty values are ignored.",
    )
    entities: dict[_ContextKey, _ContextValue] = Field(
        default_factory=dict,
        max_length=500,
        description="Named entity to a short note. Empty values are ignored.",
    )
    style_rules: list[_ContextNote] = Field(
        default_factory=list,
        max_length=200,
        description="Standing style rules. Empty items are ignored.",
    )
    decisions: list[_ContextNote] = Field(
        default_factory=list,
        max_length=200,
        description="Accepted decisions that later batches must respect. Empty items are ignored.",
    )
    notes: list[_ContextNote] = Field(
        default_factory=list,
        max_length=200,
        description="Free-form context notes. Empty items are ignored.",
    )

    @model_validator(mode="before")
    @classmethod
    def drop_empty_values(cls, value: Any) -> Any:
        """Ignore blank values before the length rules run.

        Callers that build a capsule from a list of names routinely leave empty
        strings behind. Dropping them keeps the contract strict about content
        while removing a pointless validation failure.
        """

        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        for name in ("terminology", "entities"):
            entries = cleaned.get(name)
            if isinstance(entries, dict):
                cleaned[name] = {
                    key: item for key, item in entries.items() if not _blank_context_value(item)
                }
        for name in ("style_rules", "decisions", "notes"):
            entries = cleaned.get(name)
            if isinstance(entries, list):
                cleaned[name] = [item for item in entries if not _blank_context_value(item)]
        return cleaned


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
    "native_execution_policy_json_schema",
]
