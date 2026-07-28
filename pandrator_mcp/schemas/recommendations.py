"""Read-only next-step recommendation arguments."""

from __future__ import annotations

from pydantic import Field

from .common import ToolInput


class RecommendNextStepsInput(ToolInput):
    session_id: str | None = Field(default=None, max_length=80)
    goal: str | None = Field(default=None, max_length=500)
