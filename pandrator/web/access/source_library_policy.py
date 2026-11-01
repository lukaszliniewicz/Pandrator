"""Fresh workspace library authority and finite project source dependencies."""

from __future__ import annotations

from sqlalchemy import exists, or_, select, true
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from ..accounts.credential_resources import credential_project_clause, credential_workspace_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import Workspace
from ..models import Artifact, SessionRecord, SessionSource, SourceAsset
from ..projects.models import Project
from .artifact_visibility import resource_visibility_clause
from .contracts import AccessDenied, ProjectAction, WorkspaceNotFound
from .current_session_policy import require_current_session_access
from .project_permissions import ACTION_SCOPES, validate_project_action
from .project_visibility import project_ids_for_user, workspace_authority


def _source_action(action: ProjectAction) -> ProjectAction:
    action = validate_project_action(action)
    if action not in {ProjectAction.READ, ProjectAction.EDIT, ProjectAction.RUN}:
        raise TypeError("A source-library READ, EDIT or RUN action is required.")
    return action


def _fresh(unit: Session, action: ProjectAction) -> UserActor | HostActor:
    actor = current_execution_actor.get()
    with unit.no_autoflush:
        try:
            require_execution_actor_in_session(
                unit,
                actor,
                required_scopes=ACTION_SCOPES[action],
                allow_host=True,
            )
        except PermissionError:
            raise AccessDenied() from None
    if type(actor) is not UserActor and type(actor) is not HostActor:
        raise AccessDenied()
    return actor


def _workspace_clause(actor: UserActor, action: ProjectAction) -> ColumnElement[bool]:
    return exists(
        select(Workspace.id)
        .where(
            Workspace.id == SourceAsset.workspace_id,
            Workspace.state == "active",
            workspace_authority(actor.user_id, action=action),
            credential_workspace_clause(actor),
        )
        .correlate(SourceAsset)
    )


def require_source_library_workspace(
    unit: Session,
    action: ProjectAction,
    *,
    workspace_id: str | None = None,
) -> str | None:
    actor = current_execution_actor.get()
    if actor is None:
        return workspace_id
    action = _source_action(action)
    actor = _fresh(unit, action)
    if type(actor) is HostActor:
        return workspace_id
    if type(actor) is not UserActor:
        raise AccessDenied()
    statement = select(Workspace.id).where(
        Workspace.state == "active",
        workspace_authority(actor.user_id, action=action),
        credential_workspace_clause(actor),
    )
    if workspace_id is None:
        statement = statement.where(
            Workspace.kind == "personal",
            Workspace.personal_owner_user_id == actor.user_id,
        )
    else:
        statement = statement.where(Workspace.id == workspace_id)
    with unit.no_autoflush:
        identifier = unit.scalar(statement)
    if identifier is None:
        raise WorkspaceNotFound()
    return identifier


def _project_dependency_clause(actor: UserActor, session_id: str) -> ColumnElement[bool]:
    attached = aliased(SessionSource)
    attached_session = aliased(SessionRecord)
    target_project = (
        select(SessionRecord.project_id).where(SessionRecord.id == session_id).scalar_subquery()
    )
    return exists(
        select(Project.id)
        .where(
            Project.id == target_project,
            Project.workspace_id == SourceAsset.workspace_id,
            or_(
                exists(
                    select(Artifact.id)
                    .where(
                        Artifact.id == SourceAsset.artifact_id,
                        Artifact.project_id == Project.id,
                        Artifact.workspace_id == SourceAsset.workspace_id,
                        resource_visibility_clause(Artifact, actor),
                    )
                    .correlate(Project, SourceAsset)
                ),
                exists(
                    select(attached.id)
                    .join(
                        attached_session,
                        attached_session.id == attached.session_id,
                    )
                    .where(
                        attached.source_asset_id == SourceAsset.id,
                        attached.is_current.is_(True),
                        attached_session.project_id == Project.id,
                    )
                    .correlate(Project, SourceAsset)
                ),
            ),
        )
        .correlate(SourceAsset)
    )


def require_source_asset_access(
    unit: Session,
    source_asset_id: str,
    action: ProjectAction = ProjectAction.READ,
    *,
    project_session_id: str | None = None,
) -> None:
    actor = current_execution_actor.get()
    if actor is None:
        return
    action = _source_action(action)
    if action is ProjectAction.READ and project_session_id is not None:
        require_current_session_access(unit, project_session_id, ProjectAction.READ)
    actor = _fresh(unit, action)
    if type(actor) is HostActor:
        return
    if type(actor) is not UserActor:
        raise AccessDenied()
    allowed = _workspace_clause(actor, action)
    if action is ProjectAction.READ and project_session_id is not None:
        allowed = or_(allowed, _project_dependency_clause(actor, project_session_id))
    with unit.no_autoflush:
        identifier = unit.scalar(
            select(SourceAsset.id).where(
                SourceAsset.id == source_asset_id,
                allowed,
                exists(
                    select(Workspace.id)
                    .where(
                        Workspace.id == SourceAsset.workspace_id,
                        Workspace.state == "active",
                    )
                    .correlate(SourceAsset)
                ),
            )
        )
    if identifier is None:
        raise KeyError("Source asset not found.")


def require_source_artifact_publication_access(unit: Session, artifact_id: str) -> None:
    actor = current_execution_actor.get()
    if actor is None:
        return
    actor = _fresh(unit, ProjectAction.EDIT)
    if type(actor) is HostActor:
        return
    if type(actor) is not UserActor:
        raise AccessDenied()
    with unit.no_autoflush:
        identifier = unit.scalar(
            select(Artifact.id).where(
                Artifact.id == artifact_id,
                resource_visibility_clause(Artifact, actor, action=ProjectAction.EDIT),
            )
        )
    if identifier is None:
        raise KeyError("Source artifact not found.")


def source_reference_visibility_clause() -> ColumnElement[bool]:
    """Filter after a fresh entry proof; do not independently grant authority."""
    actor = current_execution_actor.get()
    if actor is None or type(actor) is HostActor:
        return true()
    if type(actor) is not UserActor:
        raise AccessDenied()
    readable = project_ids_for_user(actor.user_id, action=ProjectAction.READ).where(
        credential_project_clause(actor)
    )
    sessions = (
        select(SessionRecord.id)
        .join(Project, Project.id == SessionRecord.project_id)
        .where(Project.id.in_(readable.correlate(None)))
    )
    return SessionSource.session_id.in_(sessions.correlate(None))


def require_source_backfill_access(unit: Session) -> None:
    actor = current_execution_actor.get()
    if actor is None:
        return
    if type(actor) is not HostActor:
        raise AccessDenied()
    _fresh(unit, ProjectAction.EDIT)
