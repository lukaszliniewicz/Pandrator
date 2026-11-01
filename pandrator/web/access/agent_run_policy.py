"""Fresh project authority for durable agent-run management."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import current_execution_actor
from ..models import AgentRun
from .contracts import ProjectAction
from .current_session_policy import require_current_session_access
from .project_permissions import validate_project_action


def require_current_agent_run_access(
    unit: Session,
    run_id: str,
    action: ProjectAction = ProjectAction.READ,
) -> None:
    """Resolve the run's stored session root before checking current authority."""
    validated_action = validate_project_action(action)
    if current_execution_actor.get() is None:
        return

    with unit.no_autoflush:
        session_id = unit.scalar(select(AgentRun.session_id).where(AgentRun.id == run_id))
    if session_id is None:
        raise KeyError("Agentic operation not found.")

    require_current_session_access(unit, session_id, validated_action)


__all__ = ["require_current_agent_run_access"]
