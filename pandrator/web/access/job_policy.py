"""Fresh, SQL-scoped job reads, runs, and cancellation in the caller's transaction."""

from __future__ import annotations

from enum import Enum

from sqlalchemy import and_, case, exists, false, func, or_, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..accounts.actor_authorization import require_actor_in_session
from ..accounts.actor_models import JobActorRecord
from ..accounts.credential_resources import (
    credential_project_clause,
    credential_workspace_clause,
)
from ..accounts.execution_actor import UserActor
from ..accounts.models import User, Workspace, WorkspaceMembership
from ..models import Job, QuickTranscription, SessionRecord, WorkflowRun
from ..projects.models import Project
from .contracts import AccessContext, AccessDenied, ProjectAction
from .private_transcription_policy import PrivateWorkAction, private_job_visibility_clause
from .project_permissions import ACTION_SCOPES
from .project_visibility import project_ids_for_user, workspace_authority

_PURGE_JOB_KINDS = ("maintenance.session_purge_preview", "maintenance.session_purge")


class JobAction(str, Enum):
    READ = "read"
    RUN = "run"
    CANCEL = "cancel"


class JobNotFound(KeyError):
    def __init__(self) -> None:
        super().__init__("Job not found.")


def _fresh_actor(session: Session, context: AccessContext, action: JobAction) -> UserActor:
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    if type(action) is not JobAction:
        raise TypeError("A valid job action is required.")
    try:
        with session.no_autoflush:
            require_actor_in_session(
                session, context.actor, required_scopes=(f"app.{action.value}",)
            )
    except PermissionError:
        raise AccessDenied() from None
    return context.actor


def _actor_proof(user_id: str | None = None) -> ColumnElement[bool]:
    # CASE is essential: SQLite does not promise short-circuit evaluation of AND.
    safe_json = case(
        (func.json_valid(JobActorRecord.context_json), JobActorRecord.context_json),
        else_="{}",
    )
    shape = (
        func.json_type(safe_json) == "object",
        func.json_type(safe_json, "$.version") == "integer",
        func.json_extract(safe_json, "$.version") == 1,
    )
    if user_id is None:
        attribution = (
            JobActorRecord.user_id.is_(None),
            JobActorRecord.auth_revision.is_(None),
            func.json_extract(safe_json, "$.source") == "host",
            func.json_extract(safe_json, "$.label") == "pandrator-cli",
        )
    else:
        attribution = (
            JobActorRecord.user_id == user_id,
            JobActorRecord.auth_revision > 0,
            func.json_extract(safe_json, "$.source") == "user",
        )
    return exists(
        select(JobActorRecord.job_id)
        .where(JobActorRecord.job_id == Job.id, *shape, *attribution)
        .correlate(Job)
    )


def _rootless() -> ColumnElement[bool]:
    return and_(
        Job.project_id.is_(None),
        Job.session_id.is_(None),
        Job.workflow_run_id.is_(None),
    )


def _project_action(action: JobAction) -> ProjectAction:
    return {
        JobAction.READ: ProjectAction.READ,
        JobAction.RUN: ProjectAction.RUN,
        JobAction.CANCEL: ProjectAction.CANCEL_ANY,
    }[action]


def _project_clause(actor: UserActor, action: JobAction) -> ColumnElement[bool]:
    if action is JobAction.CANCEL:
        if "app.read" not in actor.scopes:
            return false()
        shared = project_ids_for_user(actor.user_id, action=ProjectAction.CANCEL_ANY).where(
            credential_project_clause(actor)
        )
        own = project_ids_for_user(actor.user_id, action=ProjectAction.CANCEL_OWN).where(
            credential_project_clause(actor)
        )
        authority = or_(
            Project.id.in_(shared.correlate(None)),
            and_(
                Project.id.in_(own.correlate(None)),
                _actor_proof(actor.user_id),
            ),
        )
    elif action is JobAction.RUN:
        if "app.read" not in actor.scopes:
            return false()
        runnable = project_ids_for_user(actor.user_id, action=ProjectAction.RUN).where(
            credential_project_clause(actor)
        )
        authority = Project.id.in_(runnable.correlate(None))
    else:
        readable = project_ids_for_user(actor.user_id, action=ProjectAction.READ).where(
            credential_project_clause(actor)
        )
        authority = Project.id.in_(readable.correlate(None))
    governance_projects = project_ids_for_user(
        actor.user_id, action=ProjectAction.PURGE
    ).where(credential_project_clause(actor))
    governance = case(
        (Job.kind.in_(_PURGE_JOB_KINDS), Project.id.in_(governance_projects.correlate(None))),
        else_=true(),
    )
    valid_session = exists(
        select(SessionRecord.id)
        .where(SessionRecord.id == Job.session_id, SessionRecord.project_id == Project.id)
        .correlate(Job, Project)
    )
    valid_workflow = exists(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.id == Job.workflow_run_id,
            WorkflowRun.session_id == Job.session_id,
        )
        .correlate(Job)
    )
    return exists(
        select(Project.id)
        .where(
            Project.id == Job.project_id,
            Project.workspace_id == Job.workspace_id,
            authority,
            governance,
            or_(Job.session_id.is_(None), valid_session),
            or_(
                Job.workflow_run_id.is_(None),
                and_(Job.session_id.is_not(None), valid_workflow),
            ),
        )
        .correlate(Job)
    )


def _workspace_clause(actor: UserActor, action: JobAction) -> ColumnElement[bool]:
    own = _actor_proof(actor.user_id)
    no_actor = ~exists(
        select(JobActorRecord.job_id).where(JobActorRecord.job_id == Job.id).correlate(Job)
    )
    return exists(
        select(Workspace.id)
        .where(
            Workspace.id == Job.workspace_id,
            credential_workspace_clause(actor),
            or_(
                and_(
                    Workspace.kind == "personal",
                    Workspace.personal_owner_user_id == actor.user_id,
                    exists(
                        select(WorkspaceMembership.user_id).where(
                            WorkspaceMembership.workspace_id == Workspace.id,
                            WorkspaceMembership.user_id == actor.user_id,
                            WorkspaceMembership.role == "owner",
                        )
                    ),
                    or_(own, no_actor, _actor_proof()),
                ),
                and_(
                    Workspace.kind == "team",
                    # READ is also the non-disclosing visibility probe used after
                    # an action denial; the outer read boundary validates its scope.
                    true()
                    if action is JobAction.READ
                    or ACTION_SCOPES[_project_action(action)] <= actor.scopes
                    else false(),
                    workspace_authority(actor.user_id, action=_project_action(action)),
                ),
            ),
        )
        .correlate(Job)
    )


def _policy_clause(
    session: Session,
    context: AccessContext,
    action: JobAction,
    *,
    private_action: PrivateWorkAction,
) -> ColumnElement[bool]:
    actor = context.actor
    private = or_(
        Job.kind == "transcription.quick",
        exists(
            select(QuickTranscription.id).where(QuickTranscription.job_id == Job.id).correlate(Job)
        ),
    )
    private_authority = private_job_visibility_clause(session, context, private_action)
    host_authority = and_(
        _rootless(),
        true() if "app.admin" in actor.scopes else false(),
        exists(select(User.id).where(User.id == actor.user_id, User.is_instance_admin.is_(True))),
    )
    active_workspace = exists(
        select(Workspace.id)
        .where(Workspace.id == Job.workspace_id, Workspace.state == "active")
        .correlate(Job)
    )
    maintenance_shape = or_(
        Job.kind.not_in(_PURGE_JOB_KINDS),
        and_(
            Job.project_id.is_not(None),
            Job.session_id.is_(None),
            Job.workflow_run_id.is_(None),
        ),
    )
    return and_(
        active_workspace,
        maintenance_shape,
        or_(
            and_(private, private_authority),
            and_(~private, Job.kind == "rvc.model.upload", host_authority),
            and_(
                ~private,
                Job.kind != "rvc.model.upload",
                or_(
                    and_(Job.project_id.is_not(None), _project_clause(actor, action)),
                    and_(_rootless(), _workspace_clause(actor, action)),
                ),
            ),
        ),
    )


def job_visibility_clause(session: Session, context: AccessContext) -> ColumnElement[bool]:
    _fresh_actor(session, context, JobAction.READ)
    return _policy_clause(session, context, JobAction.READ, private_action=PrivateWorkAction.READ)


def job_cancellation_clause(session: Session, context: AccessContext) -> ColumnElement[bool]:
    actor = _fresh_actor(session, context, JobAction.READ)
    if "app.cancel" not in actor.scopes:
        return false()
    return _policy_clause(
        session, context, JobAction.CANCEL, private_action=PrivateWorkAction.CANCEL
    )


def require_job_access(
    session: Session,
    context: AccessContext,
    identifier: str,
    action: JobAction = JobAction.READ,
) -> Job:
    """Load a payload only after SQL authority; preserve the caller's unit of work."""
    _fresh_actor(session, context, action)
    private_action = PrivateWorkAction(action.value)
    allowed = _policy_clause(session, context, action, private_action=private_action)
    with session.no_autoflush:
        record = session.scalar(
            select(Job)
            .where(Job.id == identifier, allowed)
            .execution_options(populate_existing=True)
        )
        if record is not None:
            return record
        # On CANCEL this is only a resource-visibility probe, not a second literal
        # app.read requirement. The existing private policy uses identical roots.
        visible = _policy_clause(session, context, JobAction.READ, private_action=private_action)
        visible_id = session.scalar(select(Job.id).where(Job.id == identifier, visible))
    if visible_id is None:
        raise JobNotFound()
    raise AccessDenied()


__all__ = [
    "JobAction",
    "JobNotFound",
    "job_cancellation_clause",
    "job_visibility_clause",
    "require_job_access",
]
