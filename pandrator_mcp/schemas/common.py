"""Shared, strict schemas for model-visible MCP arguments and results."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..errors import NextAction


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WarningMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class WorkReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["job", "manager_operation", "automation"]
    id: str
    state: Literal[
        "queued",
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
    ]
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: str | None = None
    cancellable: bool
    poll_after_ms: int = Field(ge=0, le=60_000)


T = TypeVar("T")


class ToolEnvelope(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    request_id: str
    result: T | None = None
    work: WorkReference | None = None
    warnings: list[WarningMessage] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)


JsonObject = dict[str, Any]
