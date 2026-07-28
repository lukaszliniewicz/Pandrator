"""Reviewed workflow-plan tool arguments."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import Field, field_validator

from .common import ToolInput

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
            if (
                normalized in _CONNECTION_OR_SECRET_KEYS
                or normalized.endswith(
                    (
                        "_api_key",
                        "_credential",
                        "_password",
                        "_private_key",
                        "_secret",
                        "_token",
                        "_url",
                    )
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
            raise ValueError(
                "Workflow overrides must be finite JSON values."
            ) from error
        if len(encoded) > 128 * 1024:
            raise ValueError(
                "Workflow overrides exceed the MCP size limit."
            )
        return value


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
