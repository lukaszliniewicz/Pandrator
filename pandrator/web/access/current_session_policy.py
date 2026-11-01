"""Session content access for authenticated requests and captured job actors."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.credential_resources import credential_project_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..job_execution import current_job_lease
from ..models import SessionRecord
from ..projects.models import Project
from .contracts import AccessContext, AccessDenied, ProjectAction
from .project_permissions import ACTION_SCOPES, validate_project_action
from .project_visibility import project_ids_for_user
from .session_policy import SessionNotFound, require_session_access


def require_current_session_access(
    session: Session,
    session_id: str,
    action: ProjectAction = ProjectAction.READ,
) -> None:
    """Check current provenance before loading content in the caller's unit.

    HTTP authentication always supplies a UserActor. None remains the trusted
    internal compatibility path; it cannot authorize an HTTP request. A leased
    job uses durable actor validation, preserving admitted work after logout.
    This explicit leaf is not an ORM hook and never guards failure bookkeeping.
    """
    action = validate_project_action(action)
    actor = current_execution_actor.get()
    if actor is None:
        return
    if type(actor) is not UserActor and type(actor) is not HostActor:
        raise AccessDenied()
    if type(actor) is UserActor and current_job_lease.get() is None:
        require_session_access(session, AccessContext(actor), session_id, action)
        return
    with session.no_autoflush:
        try:
            require_execution_actor_in_session(
                session, actor, required_scopes=ACTION_SCOPES[action], allow_host=True
            )
        except PermissionError:
            raise AccessDenied() from None
        if type(actor) is HostActor:
            return
        if not isinstance(actor, UserActor):
            raise AccessDenied()
        project_id = session.scalar(
            select(SessionRecord.project_id).where(SessionRecord.id == session_id)
        )
        if (
            project_id is None
            or session.scalar(
                select(Project.id).where(
                    Project.id == project_id,
                    Project.id.in_(project_ids_for_user(actor.user_id)),
                    credential_project_clause(actor),
                )
            )
            is None
        ):
            raise SessionNotFound()
        if (
            session.scalar(
                select(Project.id).where(
                    Project.id == project_id,
                    Project.id.in_(project_ids_for_user(actor.user_id, action=action)),
                    credential_project_clause(actor),
                )
            )
            is None
        ):
            raise AccessDenied()
