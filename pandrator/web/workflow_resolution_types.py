"""Value type shared by workflow input resolution and compatibility callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ResolvedWorkflowStage:
    """Immutable queue submission resolved without changing durable state."""

    job_kind: str
    payload: dict[str, Any]
    resource_keys: tuple[str, ...]
    session_revision: int
    workflow_kind: str
    source_artifact_id: str | None
    source_content_hash: str | None
    outcome_revision: int
