"""Pure SQL predicates for current artifact and usage ownership visibility."""

from __future__ import annotations

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.sql.elements import ColumnElement

from ..accounts.credential_resources import (
    credential_project_clause,
    credential_workspace_clause,
)
from ..accounts.execution_actor import UserActor
from ..accounts.models import Workspace
from ..models import Artifact, SessionRecord, UploadSessionRecord, UsageEvent
from ..projects.models import Project
from .contracts import ProjectAction
from .project_permissions import validate_project_action
from .project_visibility import (
    project_ids_for_user,
    workspace_authority,
)


def _project_root_clause(
    resource: type[Artifact] | type[UsageEvent] | type[UploadSessionRecord],
) -> ColumnElement[bool]:
    same_project_session = or_(
        resource.session_id.is_(None),
        exists(
            select(SessionRecord.id)
            .where(
                SessionRecord.id == resource.session_id,
                SessionRecord.project_id == Project.id,
            )
            .correlate(resource, Project)
        ),
    )
    return exists(
        select(Project.id)
        .join(Workspace, Workspace.id == Project.workspace_id)
        .where(
            Project.id == resource.project_id,
            Project.workspace_id == resource.workspace_id,
            Workspace.state == "active",
            same_project_session,
        )
        .correlate(resource)
    )


def _rootless_workspace_clause(
    resource: type[Artifact] | type[UsageEvent] | type[UploadSessionRecord],
) -> ColumnElement[bool]:
    return exists(
        select(Workspace.id)
        .where(
            Workspace.id == resource.workspace_id,
            Workspace.state == "active",
        )
        .correlate(resource)
    )


def artifact_roots_clause(
    resource: type[Artifact] | type[UsageEvent] | type[UploadSessionRecord],
) -> ColumnElement[bool]:
    """Require an actual active project/session root or an active rootless workspace."""
    return or_(
        _project_root_clause(resource),
        and_(
            resource.project_id.is_(None),
            resource.session_id.is_(None),
            _rootless_workspace_clause(resource),
        ),
    )


def _project_visibility_clause(
    resource: type[Artifact] | type[UsageEvent] | type[UploadSessionRecord],
    actor: UserActor,
    action: ProjectAction,
) -> ColumnElement[bool]:
    readable_projects: Select[tuple[str]] = project_ids_for_user(
        actor.user_id,
        action=action,
    ).where(credential_project_clause(actor))
    return exists(
        select(Project.id)
        .join(Workspace, Workspace.id == Project.workspace_id)
        .where(
            Project.id == resource.project_id,
            Project.workspace_id == resource.workspace_id,
            Workspace.state == "active",
            Project.id.in_(readable_projects.correlate(None)),
            or_(
                resource.session_id.is_(None),
                exists(
                    select(SessionRecord.id)
                    .where(
                        SessionRecord.id == resource.session_id,
                        SessionRecord.project_id == Project.id,
                    )
                    .correlate(resource, Project)
                ),
            ),
        )
        .correlate(resource)
    )


def _rootless_visibility_clause(
    resource: type[Artifact] | type[UsageEvent] | type[UploadSessionRecord],
    actor: UserActor,
    action: ProjectAction,
) -> ColumnElement[bool]:
    return and_(
        resource.project_id.is_(None),
        resource.session_id.is_(None),
        exists(
            select(Workspace.id)
            .where(
                Workspace.id == resource.workspace_id,
                Workspace.state == "active",
                workspace_authority(actor.user_id, action=action),
                credential_workspace_clause(actor),
            )
            .correlate(resource)
        ),
    )


def resource_visibility_clause(
    resource: type[Artifact] | type[UsageEvent] | type[UploadSessionRecord],
    actor: UserActor,
    *,
    action: ProjectAction = ProjectAction.READ,
) -> ColumnElement[bool]:
    """Build current-role and credential scoped SQL, without validating actor lifetime.

    The caller must prove the appropriate actor lifetime in this same unit of work;
    this predicate alone is not an authorization grant.
    """
    validated_action = validate_project_action(action)
    return or_(
        _project_visibility_clause(resource, actor, validated_action),
        _rootless_visibility_clause(resource, actor, validated_action),
    )


__all__ = ["artifact_roots_clause", "resource_visibility_clause"]
