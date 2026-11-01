"""Fresh sound-processing authority and session-bound input headers."""

from __future__ import annotations

from collections.abc import Callable, Collection

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..database import Database
from ..models import Artifact, SessionRecord
from .artifact_policy import ArtifactNotFound
from .artifact_visibility import resource_visibility_clause
from .contracts import ProjectAction
from .current_session_policy import require_current_session_access
from .session_policy import SessionNotFound
from .sound_dependency_visibility import current_sound_dependency_clause


def require_sound_processing_session(session: Session, session_id: str) -> None:
    """Prove RUN and a live exact session before reading processing inputs."""
    if current_execution_actor.get() is None:
        return
    require_current_session_access(session, session_id, ProjectAction.RUN)
    with session.no_autoflush:
        identifier = session.scalar(
            select(SessionRecord.id).where(
                SessionRecord.id == session_id,
                SessionRecord.trashed_at.is_(None),
                SessionRecord.status != "purging",
            )
        )
    if identifier is None:
        raise SessionNotFound()


def require_sound_input_access(
    session: Session, session_id: str, artifact_id: str, *, session_bound: bool = False
) -> None:
    """Check headers only; caller proves processing authority in this unit."""
    actor = current_execution_actor.get()
    if actor is None or type(actor) is HostActor:
        return
    if type(actor) is not UserActor:
        raise ArtifactNotFound()
    visible = resource_visibility_clause(Artifact, actor)
    allowed = and_(Artifact.session_id == session_id, visible)
    if not session_bound:
        project_id = select(SessionRecord.project_id).where(SessionRecord.id == session_id)
        allowed = or_(
            allowed,
            current_sound_dependency_clause(actor, session_id=session_id),
            and_(
                Artifact.session_id.is_(None),
                Artifact.project_id == project_id.scalar_subquery(),
                Artifact.kind == "audio",
                Artifact.role == "sound.original",
                Artifact.state == "current",
                visible,
            ),
        )
    with session.no_autoflush:
        identifier = session.scalar(
            select(Artifact.id).where(
                Artifact.id == artifact_id, Artifact.state != "deleted", allowed
            )
        )
    if identifier is None:
        raise ArtifactNotFound()


def sound_execution_callbacks(
    database: Database,
    session_id: str,
    *,
    session_artifact_ids: Collection[str] = (),
    before_io: Callable[[], None] | None = None,
    authorization_guard: Callable[[Session], None] | None = None,
    authorize_input: Callable[[Session, str], None] | None = None,
) -> tuple[
    Callable[[], None] | None,
    Callable[[Session], None] | None,
    Callable[[Session, str], None] | None,
]:
    """Fill absent fences without wrapping caller-owned callback objects."""
    if current_execution_actor.get() is None:
        return before_io, authorization_guard, authorize_input
    if authorization_guard is None:

        def guard(unit: Session) -> None:
            require_sound_processing_session(unit, session_id)

        authorization_guard = guard
    if before_io is None:
        chosen_guard = authorization_guard

        def check_io() -> None:
            with database.snapshot_session() as unit:
                chosen_guard(unit)

        before_io = check_io
    if authorize_input is None:

        def check_input(unit: Session, identifier: str) -> None:
            require_sound_input_access(
                unit,
                session_id,
                identifier,
                session_bound=identifier in session_artifact_ids,
            )

        authorize_input = check_input
    return before_io, authorization_guard, authorize_input
