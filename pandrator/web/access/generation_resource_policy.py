"""Resolve generation resource headers before reading their protected bodies."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..job_execution import current_job_lease
from ..models import GenerationPlan, GenerationPlanRevision, GenerationRun, GenerationSegment
from .contracts import ProjectAction
from .current_session_policy import require_current_session_access
from .session_policy import SessionNotFound

GenerationResource = Literal["session", "run", "segment"]


def require_generation_resource_access(
    session: Session,
    resource: GenerationResource,
    identifier: str,
    action: ProjectAction = ProjectAction.READ,
) -> str:
    with session.no_autoflush:
        if resource == "session":
            session_id = identifier
        elif resource == "run":
            session_id = session.scalar(
                select(GenerationRun.session_id).where(GenerationRun.id == identifier)
            )
        elif resource == "segment":
            session_id = session.scalar(
                select(GenerationPlan.session_id)
                .join(GenerationPlanRevision, GenerationPlanRevision.plan_id == GenerationPlan.id)
                .join(
                    GenerationSegment,
                    GenerationSegment.plan_revision_id == GenerationPlanRevision.id,
                )
                .where(GenerationSegment.id == identifier)
            )
        else:
            raise TypeError("A valid generation resource is required.")
        if session_id is None:
            raise SessionNotFound()
        require_current_session_access(session, session_id, action)
        return session_id


def require_generation_content_write(
    session: Session, resource: GenerationResource, identifier: str
) -> str:
    """Manual edits use EDIT; derived writes under a proved job use RUN."""
    action = ProjectAction.RUN if current_job_lease.get() is not None else ProjectAction.EDIT
    return require_generation_resource_access(session, resource, identifier, action)
