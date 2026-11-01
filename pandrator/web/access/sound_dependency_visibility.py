"""Current project cues publish selected sound bytes, not library metadata."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from ..accounts.credential_resources import credential_project_clause
from ..accounts.execution_actor import UserActor
from ..models import Artifact, SessionRecord, SoundItem, SoundVariant, Workspace
from ..projects.models import Project
from ..sound_project_models import ProjectSoundCue, ProjectSoundPlan
from .contracts import ProjectAction
from .project_visibility import project_ids_for_user


def current_sound_dependency_clause(
    actor: UserActor, *, project_id: str | None = None, session_id: str | None = None,
) -> ColumnElement[bool]:
    """Require current project and credential authority; caller proves actor lifetime."""
    latest_plan = aliased(ProjectSoundPlan)
    source = aliased(SessionRecord)
    latest_revision = (
        select(func.max(latest_plan.revision))
        .where(latest_plan.session_id == SessionRecord.id)
        .correlate(SessionRecord)
        .scalar_subquery()
    )
    readable_projects = project_ids_for_user(
        actor.user_id, action=ProjectAction.READ
    ).where(credential_project_clause(actor))
    if project_id is not None:
        readable_projects = readable_projects.where(Project.id == project_id)
    return and_(
        Artifact.project_id.is_(None),
        Artifact.session_id.is_(None),
        Artifact.kind == "audio",
        Artifact.role == "sound.original",
        Artifact.state == "current",
        exists(
            select(ProjectSoundCue.id)
            .join(ProjectSoundPlan, ProjectSoundPlan.id == ProjectSoundCue.plan_id)
            .join(SessionRecord, SessionRecord.id == ProjectSoundPlan.session_id)
            .join(Project, Project.id == SessionRecord.project_id)
            .join(Workspace, Workspace.id == Project.workspace_id)
            .join(SoundVariant, SoundVariant.id == ProjectSoundCue.variant_id)
            .where(
                *(() if session_id is None else (ProjectSoundPlan.session_id == session_id,)),
                Workspace.state == "active",
                Project.workspace_id == Artifact.workspace_id,
                Project.id.in_(readable_projects.correlate(None)),
                SessionRecord.trashed_at.is_(None),
                ProjectSoundPlan.revision == latest_revision,
                SoundVariant.artifact_id == Artifact.id,
                SoundVariant.workspace_id == Artifact.workspace_id,
                SoundVariant.state == "ready",
                or_(
                    SoundVariant.item_id.is_(None),
                    exists(
                        select(SoundItem.id)
                        .where(
                            SoundItem.id == SoundVariant.item_id,
                            SoundItem.workspace_id == Artifact.workspace_id,
                        )
                        .correlate(SoundVariant, Artifact)
                    ),
                ),
                exists(
                    select(source.id)
                    .where(
                        source.id == Project.source_session_id,
                        source.project_id == Project.id,
                        source.trashed_at.is_(None),
                    )
                    .correlate(Project)
                ),
            )
            .correlate(Artifact)
        ),
    )
