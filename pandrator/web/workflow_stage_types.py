"""Shared immutable workflow stage definition."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StageDefinition:
    key: str
    title: str
    explanation: str
    executable: bool = True
    prerequisite_roles: tuple[str, ...] = ()
    output_role: str | None = None
    job_kind: str | None = None
