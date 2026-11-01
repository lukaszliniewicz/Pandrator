"""Fresh, SQL-scoped authorization for passive artifact and usage reads."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from ..accounts.actor_authorization import require_actor_in_session
from ..accounts.execution_actor import UserActor
from ..models import Artifact, UsageEvent
from .artifact_visibility import resource_visibility_clause
from .contracts import AccessContext, AccessDenied


class ArtifactNotFound(KeyError):
    """An artifact is absent or unavailable to the current actor."""

    def __init__(self) -> None:
        super().__init__("Artifact not found.")


def _fresh_actor(session: Session, context: AccessContext) -> UserActor:
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    actor = context.actor
    try:
        with session.no_autoflush:
            require_actor_in_session(
                session,
                actor,
                required_scopes=("app.read",),
            )
    except PermissionError:
        raise AccessDenied() from None
    return actor


def visible_artifact_ids_statement(
    session: Session,
    context: AccessContext,
    *,
    sound_dependency_ids: Collection[str] = (),
    sound_dependency_project_id: str | None = None,
) -> Select[tuple[str]]:
    """Build fresh, role-and-credential-scoped artifact IDs for this session.

    Sound IDs opt into finite copy-file projection only. They never authorize
    metadata, context, usage or library listing; ordinary callers omit them.
    """
    actor = _fresh_actor(session, context)
    visible = resource_visibility_clause(Artifact, actor)
    if sound_dependency_ids:
        from .sound_dependency_visibility import current_sound_dependency_clause

        visible = or_(
            visible,
            and_(
                Artifact.id.in_(sound_dependency_ids),
                current_sound_dependency_clause(actor, project_id=sound_dependency_project_id),
            ),
        )
    return select(Artifact.id).where(visible)


def visible_usage_event_ids_statement(
    session: Session,
    context: AccessContext,
) -> Select[tuple[str]]:
    """Build fresh usage-event IDs before callers aggregate or decode payloads."""
    actor = _fresh_actor(session, context)
    return select(UsageEvent.id).where(resource_visibility_clause(UsageEvent, actor))


def require_artifact_access(
    session: Session,
    context: AccessContext,
    artifact_id: str,
    *,
    expected_session_id: str | None = None,
) -> Artifact:
    """Load an artifact only when its current SQL roots are readable."""
    actor = _fresh_actor(session, context)
    statement = select(Artifact).where(
        Artifact.id == artifact_id,
        resource_visibility_clause(Artifact, actor),
    )
    if expected_session_id is not None:
        statement = statement.where(Artifact.session_id == expected_session_id)

    with session.no_autoflush:
        artifact = session.scalar(statement.execution_options(populate_existing=True))
    if artifact is None:
        raise ArtifactNotFound()
    return artifact


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """Only fields needed to serve an authorized managed file."""

    id: str
    relative_path: str
    mime_type: str | None
    content_hash: str | None
    state: str


def require_artifact_content_access(
    session: Session,
    context: AccessContext,
    artifact_id: str,
) -> ArtifactContent:
    """Read bytes under ordinary authority or a current selected dependency.

    Dependency publication grants no artifact metadata, ancestry or library list.
    Ordinary reads retain one content query; only a miss needs the dependency
    lookup.
    """
    actor = _fresh_actor(session, context)
    statement = select(
        Artifact.id, Artifact.relative_path, Artifact.mime_type,
        Artifact.content_hash, Artifact.state,
    ).where(Artifact.id == artifact_id)
    with session.no_autoflush:
        row = session.execute(
            statement.where(resource_visibility_clause(Artifact, actor))
        ).one_or_none()
        if row is None:
            from .sound_dependency_visibility import current_sound_dependency_clause
            from .source_dependency_visibility import current_source_dependency_clause

            row = session.execute(
                statement.where(
                    or_(
                        current_sound_dependency_clause(actor),
                        current_source_dependency_clause(actor),
                    )
                )
            ).one_or_none()
    if row is None:
        raise ArtifactNotFound()
    return ArtifactContent(*row)


__all__ = [
    "ArtifactContent",
    "ArtifactNotFound",
    "require_artifact_access",
    "require_artifact_content_access",
    "visible_artifact_ids_statement",
    "visible_usage_event_ids_statement",
]
