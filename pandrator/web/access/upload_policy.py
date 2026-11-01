"""Current and leased actor authority for private workspace uploads."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..models import UploadSessionRecord
from ..upload_identity import UploadOwnership, upload_creation_ownership
from .artifact_visibility import resource_visibility_clause
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .project_permissions import ACTION_SCOPES, validate_project_action
from .source_library_policy import require_source_library_workspace


def _upload_action(action: ProjectAction) -> ProjectAction:
    action = validate_project_action(action)
    if action not in {ProjectAction.READ, ProjectAction.EDIT}:
        raise TypeError("An upload READ or EDIT action is required.")
    return action


def _fresh(unit: Session, action: ProjectAction) -> UserActor | HostActor:
    actor = current_execution_actor.get()
    with unit.no_autoflush:
        try:
            require_execution_actor_in_session(
                unit, actor, required_scopes=ACTION_SCOPES[action], allow_host=True,
            )
        except PermissionError:
            raise AccessDenied() from None
    if type(actor) is not UserActor and type(actor) is not HostActor:
        raise AccessDenied()
    return actor


def require_upload_creation_access(
    unit: Session,
    *,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> UploadOwnership:
    """Prove authority before resolving structural ownership or allocating files."""
    actor = current_execution_actor.get()
    if actor is None:
        return upload_creation_ownership(unit, session_id=session_id, workspace_id=workspace_id)
    actor = _fresh(unit, ProjectAction.EDIT)
    if type(actor) is UserActor:
        if session_id is not None:
            require_current_session_access(unit, session_id, ProjectAction.EDIT)
        else:
            workspace_id = require_source_library_workspace(
                unit, ProjectAction.EDIT, workspace_id=workspace_id,
            )
    return upload_creation_ownership(unit, session_id=session_id, workspace_id=workspace_id)


def require_upload_lookup_workspace(
    unit: Session,
    upload_id: str,
    action: ProjectAction = ProjectAction.READ,
    *,
    workspace_id: str | None = None,
) -> str | None:
    """Select only an authorized owner before hydrating the private upload body."""
    action = _upload_action(action)
    actor = current_execution_actor.get()
    if actor is None:
        return workspace_id
    actor = _fresh(unit, action)
    if type(actor) is HostActor:
        return workspace_id
    if not isinstance(actor, UserActor):
        raise AccessDenied()
    statement = select(UploadSessionRecord.workspace_id).where(
        UploadSessionRecord.id == upload_id,
        resource_visibility_clause(UploadSessionRecord, actor, action=action),
    )
    if workspace_id is not None:
        statement = statement.where(UploadSessionRecord.workspace_id == workspace_id)
    with unit.no_autoflush:
        selected = unit.scalar(statement)
    if selected is None:
        raise KeyError("Upload not found.")
    return selected


def require_upload_maintenance_access(unit: Session) -> None:
    """Reserve bulk expiration for trusted internal and freshly proved host work."""
    actor = current_execution_actor.get()
    if actor is None:
        return
    if type(actor) is not HostActor:
        raise AccessDenied()
    _fresh(unit, ProjectAction.EDIT)
