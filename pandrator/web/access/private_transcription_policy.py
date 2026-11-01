"""HTTP/admission privacy for temporary work; worker lease policy stays separate."""

from __future__ import annotations

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from ..accounts.execution_actor import UserActor
from ..accounts.models import Workspace, WorkspaceMembership
from ..accounts.private_work_models import QuickTranscriptionOwner
from ..models import Job, QuickTranscription
from .contracts import AccessContext
from .private_workspace_policy import (
    PrivateWorkAction,
    _fresh_actor,
    _workspace_predicates,
    require_private_workspace,
)


class PrivateTranscriptionNotFound(KeyError):
    def __init__(self) -> None:
        super().__init__("Transcription not found.")


def _transcription_ids(actor: UserActor) -> Select[tuple[str]]:
    linked_job = aliased(Job)
    valid_job = and_(
        linked_job.kind == "transcription.quick",
        linked_job.workspace_id == QuickTranscriptionOwner.workspace_id,
        linked_job.project_id.is_(None),
        linked_job.session_id.is_(None),
        linked_job.workflow_run_id.is_(None),
    )
    return (
        select(QuickTranscription.id)
        .join(
            QuickTranscriptionOwner,
            QuickTranscriptionOwner.transcription_id == QuickTranscription.id,
        )
        .join(Workspace, Workspace.id == QuickTranscriptionOwner.workspace_id)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .outerjoin(linked_job, linked_job.id == QuickTranscription.job_id)
        .where(
            QuickTranscriptionOwner.user_id == actor.user_id,
            *_workspace_predicates(actor),
            or_(QuickTranscription.job_id.is_(None), valid_job),
        )
        .correlate(None)
    )


def visible_transcription_ids_statement(
    session: Session,
    context: AccessContext,
    action: PrivateWorkAction = PrivateWorkAction.READ,
) -> Select[tuple[str]]:
    return _transcription_ids(_fresh_actor(session, context, action))


def get_private_transcription(
    session: Session,
    context: AccessContext,
    identifier: str,
    action: PrivateWorkAction = PrivateWorkAction.READ,
) -> QuickTranscription:
    visible = visible_transcription_ids_statement(session, context, action)
    with session.no_autoflush:
        record = session.scalar(
            select(QuickTranscription)
            .where(QuickTranscription.id == identifier, QuickTranscription.id.in_(visible))
            .execution_options(populate_existing=True)
        )
    if record is None:
        raise PrivateTranscriptionNotFound()
    return record


def private_job_visibility_clause(
    session: Session,
    context: AccessContext,
    action: PrivateWorkAction = PrivateWorkAction.READ,
):
    """Hide private work in SQL; other job scope integration remains a separate gate."""
    visible = visible_transcription_ids_statement(session, context, action)
    linked = exists(
        select(QuickTranscription.id).where(QuickTranscription.job_id == Job.id).correlate(Job)
    )
    owned = exists(
        select(QuickTranscription.id)
        .where(
            QuickTranscription.job_id == Job.id,
            QuickTranscription.id.in_(visible),
        )
        .correlate(Job)
    )
    return or_(~or_(Job.kind == "transcription.quick", linked), owned)


__all__ = [
    "PrivateTranscriptionNotFound",
    "PrivateWorkAction",
    "get_private_transcription",
    "private_job_visibility_clause",
    "require_private_workspace",
    "visible_transcription_ids_statement",
]
