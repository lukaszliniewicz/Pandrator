"""Current project source attachments publish selected artifact bytes only."""

from __future__ import annotations

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from ..accounts.credential_resources import credential_project_clause
from ..accounts.execution_actor import UserActor
from ..accounts.models import Workspace
from ..models import Artifact, SessionRecord, SessionSource, SourceAsset
from ..projects.models import Project
from .artifact_visibility import artifact_roots_clause
from .contracts import ProjectAction
from .project_visibility import project_ids_for_user


def current_source_dependency_clause(
    actor: UserActor,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> ColumnElement[bool]:
    """Require a current selected source and current target-project authority.

    The caller proves ``actor`` lifetime in the same unit of work. Origin-project
    visibility is deliberately irrelevant once a source asset is attached to a
    readable project in the same active workspace.
    """
    root_session = aliased(SessionRecord)
    readable_projects = project_ids_for_user(
        actor.user_id, action=ProjectAction.READ
    ).where(credential_project_clause(actor))

    target_filters: list[ColumnElement[bool]] = []
    if project_id is not None:
        target_filters.append(Project.id == project_id)
    if session_id is not None:
        target_filters.append(SessionRecord.id == session_id)

    return and_(
        Artifact.state != "deleted",
        artifact_roots_clause(Artifact),
        exists(
            select(SessionSource.id)
            .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
            .join(SessionRecord, SessionRecord.id == SessionSource.session_id)
            .join(Project, Project.id == SessionRecord.project_id)
            .join(Workspace, Workspace.id == Project.workspace_id)
            .where(
                SourceAsset.artifact_id == Artifact.id,
                SourceAsset.state == "current",
                SessionSource.is_current.is_(True),
                SourceAsset.workspace_id == Artifact.workspace_id,
                SourceAsset.workspace_id == Project.workspace_id,
                Workspace.state == "active",
                SessionRecord.trashed_at.is_(None),
                Project.id.in_(readable_projects.correlate(None)),
                *target_filters,
                exists(
                    select(root_session.id)
                    .where(
                        root_session.id == Project.source_session_id,
                        root_session.project_id == Project.id,
                        root_session.trashed_at.is_(None),
                    )
                    .correlate(Project)
                ),
            )
            .correlate(Artifact)
        ),
    )


__all__ = ["current_source_dependency_clause"]
