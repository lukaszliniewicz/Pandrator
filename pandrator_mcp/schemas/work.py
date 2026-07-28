"""Durable-work tool arguments."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ToolInput


class ListWorkInput(ToolInput):
    session_id: str | None = Field(default=None, max_length=80)
    kinds: tuple[str, ...] = Field(default=(), max_length=20)
    states: tuple[str, ...] = Field(default=(), max_length=10)
    limit: int = Field(default=50, ge=1, le=100)


class GetWorkInput(ToolInput):
    work_type: Literal["job", "manager_operation"] = "job"
    work_id: str = Field(min_length=1, max_length=120)
    include_events: bool = False
    event_limit: int = Field(default=50, ge=1, le=200)


class GetWorkLogInput(ToolInput):
    work_type: Literal["job", "manager_operation"] = "job"
    work_id: str = Field(min_length=1, max_length=120)
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class CancelWorkInput(ToolInput):
    work_type: Literal["job", "manager_operation"] = "job"
    work_id: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )
