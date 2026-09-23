"""Shared input-selection flags for guided and directly configured sessions."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .database import Database
from .models import OutcomePlan


def workflow_transformations(
    session: Session, session_id: str, outcome: OutcomePlan | None, database: Database,
) -> dict[str, Any]:
    # A saved outcome is an explicit workflow choice, including disabled steps.
    if outcome is not None:
        value = (outcome.value_json or {}).get("transformations")
        return dict(value) if isinstance(value, dict) else {}

    # MCP sessions can be configured directly without creating an outcome plan.
    # Use their effective text settings at every planning/execution boundary.
    from .workspace_settings import WorkspaceSettingsService

    settings = WorkspaceSettingsService(database).get_in_session(session, session_id, "text")["effective"]
    return {key: bool(settings.get(key)) for key in (
        "llm_tts_optimization", "llm_tts_document_optimization",
    )}
