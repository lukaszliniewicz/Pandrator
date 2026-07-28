"""Bounded Manager plan and runtime arguments.

Connection data, credentials, arbitrary paths, and commands are deliberately
not part of this model-visible contract.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .common import ToolInput

_COMPONENT_ID = r"^[a-z][a-z0-9_-]{0,79}$"
_SERVICE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$"
_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
_OPTION_KEY = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_FORBIDDEN_OPTION_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "base_url",
        "ca_bundle",
        "command",
        "credential",
        "endpoint",
        "host",
        "origin",
        "password",
        "path",
        "port",
        "private_key",
        "proxy",
        "secret",
        "token",
        "url",
        "workspace",
    }
)
ManagerOptionValue = str | int | float | bool | None


class ManagerDesiredComponentInput(ToolInput):
    component_id: str = Field(pattern=_COMPONENT_ID)
    present: bool = True
    compute: Literal[
        "auto",
        "cpu",
        "cuda",
        "vulkan",
        "metal",
        "rocm",
        "wgpu",
    ] = "auto"
    quantization: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,119}$",
    )
    options: dict[str, ManagerOptionValue] = Field(
        default_factory=dict,
        max_length=40,
    )

    @field_validator("options")
    @classmethod
    def validate_options(
        cls,
        value: dict[str, ManagerOptionValue],
    ) -> dict[str, ManagerOptionValue]:
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if (
                not _OPTION_KEY.fullmatch(normalized)
                or normalized in _FORBIDDEN_OPTION_KEYS
                or normalized.endswith(
                    (
                        "_api_key",
                        "_command",
                        "_credential",
                        "_password",
                        "_path",
                        "_private_key",
                        "_secret",
                        "_token",
                        "_url",
                    )
                )
            ):
                raise ValueError(
                    "Manager component options cannot contain paths, "
                    "commands, credentials, or connection endpoints."
                )
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(
                    "Manager component options must be finite JSON values."
                )
            if isinstance(item, str) and len(item) > 500:
                raise ValueError(
                    "Manager component option values are too long."
                )
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 32 * 1024:
            raise ValueError(
                "Manager component options exceed the MCP size limit."
            )
        return value


class PlanComponentChangeInput(ToolInput):
    kind: Literal["install", "update", "repair", "remove"]
    components: tuple[ManagerDesiredComponentInput, ...] = Field(
        min_length=1,
        max_length=30,
    )
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @model_validator(mode="after")
    def validate_component_intent(self) -> "PlanComponentChangeInput":
        identifiers = [
            component.component_id for component in self.components
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(
                "A component may appear only once in a Manager plan."
            )
        should_be_present = self.kind != "remove"
        if any(
            component.present != should_be_present
            for component in self.components
        ):
            raise ValueError(
                "Remove plans require present=false; install, update, "
                "and repair plans require present=true."
            )
        return self

    def desired_states(self) -> dict[str, dict[str, Any]]:
        return {
            component.component_id: component.model_dump(
                mode="json",
                exclude={"component_id"},
                exclude_none=True,
            )
            for component in self.components
        }


class ExecuteComponentPlanInput(ToolInput):
    plan_id: str = Field(min_length=1, max_length=120)
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted_confirmations: tuple[str, ...] = Field(
        default=(),
        max_length=30,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class ControlRuntimeInput(ToolInput):
    action: Literal["start", "stop", "restart"]
    runtime_target: Literal[
        "application",
        "managed_services",
    ]
    service_ids: tuple[str, ...] = Field(
        default=(),
        max_length=30,
    )
    confirmation: Literal["runtime-control"]
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @field_validator("service_ids")
    @classmethod
    def validate_service_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Managed service IDs must be unique.")
        if any(not re.fullmatch(_SERVICE_ID, item) for item in value):
            raise ValueError("A managed service ID is invalid.")
        return value

    @model_validator(mode="after")
    def validate_runtime_target(self) -> "ControlRuntimeInput":
        if self.runtime_target == "application" and self.service_ids:
            raise ValueError(
                "Application runtime control cannot include service IDs."
            )
        if (
            self.runtime_target == "managed_services"
            and not self.service_ids
        ):
            raise ValueError(
                "Managed runtime control requires explicit service IDs."
            )
        return self


# Compatibility aliases for integrations that imported the Phase-1
# placeholders before Manager mutations were enabled.
ManagerRuntimeInput = ControlRuntimeInput
ManagerPlanExecutionInput = ExecuteComponentPlanInput
