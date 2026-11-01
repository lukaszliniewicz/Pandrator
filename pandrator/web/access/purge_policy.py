"""Current purge governance before inspection or an irreversible deletion claim."""

from __future__ import annotations

from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session

from ..accounts.administration import AdministratorRequired
from ..accounts.credential_resources import credential_project_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..job_execution import current_job_lease
from ..models import Job, SessionPurge, SessionRecord
from ..projects.models import Project
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .project_permissions import ACTION_SCOPES
from .project_visibility import project_ids_for_user
from .session_policy import SessionNotFound


def require_purge_inspection_access(unit: Session, session_id: str) -> None:
    """Inspect with READ scope and the current workspace Manager role."""
    require_current_session_access(unit, session_id, ProjectAction.READ)
    actor = current_execution_actor.get()
    if actor is None or type(actor) is HostActor:
        return
    if type(actor) is not UserActor:
        raise AccessDenied()
    with unit.no_autoflush:
        identifier = unit.scalar(
            select(SessionRecord.id).where(
                SessionRecord.id == session_id,
                SessionRecord.project_id.in_(
                    project_ids_for_user(actor.user_id, action=ProjectAction.PURGE)
                ),
            )
        )
    if identifier is None:
        raise AccessDenied()


def require_purge_deletion_access(
    unit: Session,
    session_id: str,
    *,
    job_id: str | None = None,
    lease_generation: int | None = None,
) -> None:
    """Authorize a claim, or acknowledge only a leased complete retained journal."""
    actor = current_execution_actor.get()
    if actor is None:
        return
    if job_id is not None:
        lease = current_job_lease.get()
        if (
            type(lease) is not dict
            or lease.get("job_id") != job_id
            or type(lease_generation) is not int
            or lease.get("lease_generation") != lease_generation
        ):
            raise AccessDenied()
    try:
        require_current_session_access(unit, session_id, ProjectAction.PURGE)
        return
    except SessionNotFound:
        if job_id is None:
            raise
    with unit.no_autoflush:
        try:
            require_execution_actor_in_session(
                unit,
                actor,
                required_scopes=ACTION_SCOPES[ProjectAction.PURGE],
                allow_host=True,
            )
        except PermissionError:
            raise AccessDenied() from None
        safe_payload = case((func.json_valid(Job.payload_json), Job.payload_json), else_="{}")
        statement = (
            select(SessionPurge.session_id)
            .join(Job, Job.id == job_id)
            .join(Project, Project.id == Job.project_id)
            .where(
                SessionPurge.session_id == session_id,
                SessionPurge.state == "complete",
                ~exists(select(SessionRecord.id).where(SessionRecord.id == session_id)),
                Job.kind == "maintenance.session_purge",
                func.json_type(safe_payload, "$.target_session_id") == "text",
                func.json_extract(safe_payload, "$.target_session_id") == session_id,
                Project.workspace_id == Job.workspace_id,
            )
        )
        if type(actor) is UserActor:
            statement = statement.where(
                Project.id.in_(project_ids_for_user(actor.user_id, action=ProjectAction.PURGE)),
                credential_project_clause(actor),
            )
        elif type(actor) is not HostActor:
            raise AccessDenied()
        if unit.scalar(statement) is None:
            raise SessionNotFound()


def require_purge_policy_administration(unit: Session) -> None:
    """Keep global retention mutation within current Administrator authority."""
    actor = current_execution_actor.get()
    if actor is None:
        return
    with unit.no_autoflush:
        try:
            require_execution_actor_in_session(
                unit,
                actor,
                required_scopes=("app.admin", "app.write"),
                require_admin=True,
                allow_host=True,
            )
        except PermissionError:
            raise AdministratorRequired() from None


def require_purge_sweep_access(unit: Session) -> None:
    """Global maintenance is trusted Host/None work, never a User wildcard."""
    actor = current_execution_actor.get()
    if actor is None:
        return
    if type(actor) is not HostActor:
        raise AccessDenied()
    with unit.no_autoflush:
        try:
            require_execution_actor_in_session(unit, actor, allow_host=True)
        except PermissionError:
            raise AccessDenied() from None
