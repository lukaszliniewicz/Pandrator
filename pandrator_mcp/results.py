"""Transport-neutral tool outcomes used to populate MCP envelopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import NextAction
from .schemas.common import WarningMessage, WorkReference


@dataclass(slots=True)
class ToolOutcome:
    result: Any
    work: WorkReference | None = None
    warnings: list[WarningMessage] = field(default_factory=list)
    next_actions: list[NextAction] = field(default_factory=list)
