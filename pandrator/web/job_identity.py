"""SQL ownership checks for durable work, separate from actor and worker policy."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .accounts.models import User, Workspace, WorkspaceMembership
from .models import Job, SessionRecord, TranslationProject, WorkflowRun
from .projects.models import Project


@dataclass(frozen=True)
class JobOwnership:
    workspace_id: str
    project_id: str | None


def _workspace(session: Session, workspace_id: str, *, allow_archived: bool) -> str:
    row = session.execute(
        select(Workspace.id, Workspace.state).where(Workspace.id == workspace_id)
    ).one_or_none()
    if row is None:
        raise ValueError("The job references a missing workspace.")
    if row.state != "active" and not (allow_archived and row.state == "archived"):
        raise ValueError("The job belongs to an inactive workspace.")
    return row.id


def _project(
    session: Session,
    project_id: str,
    *,
    allow_archived: bool,
    allow_retained_root: bool = False,
) -> JobOwnership:
    row = session.execute(
        select(Project.id, Project.workspace_id, Project.source_session_id).where(
            Project.id == project_id
        )
    ).one_or_none()
    if row is None:
        raise ValueError("The job references a missing project.")
    workspace_id = _workspace(session, row.workspace_id, allow_archived=allow_archived)
    if row.source_session_id is None:
        surviving_session = session.scalar(
            select(SessionRecord.id).where(SessionRecord.project_id == row.id).limit(1)
        )
        if allow_retained_root and surviving_session is None:
            return JobOwnership(workspace_id, row.id)
        raise ValueError("The job project has no source session.")
    source = session.execute(
        select(SessionRecord.id, SessionRecord.project_id).where(
            SessionRecord.id == row.source_session_id
        )
    ).one_or_none()
    if source is None or source.project_id != row.id:
        raise ValueError("The job project has an inconsistent source root.")
    roots = session.scalars(
        select(TranslationProject.project_root_id).where(
            TranslationProject.source_session_id == source.id
        )
    ).all()
    if any(root is not None and root != row.id for root in roots):
        raise ValueError("The job project and translation root disagree.")
    return JobOwnership(workspace_id, row.id)


def _session(session: Session, session_id: str, *, allow_archived: bool) -> JobOwnership:
    row = session.execute(
        select(SessionRecord.id, SessionRecord.project_id).where(SessionRecord.id == session_id)
    ).one_or_none()
    if row is None or row.project_id is None:
        raise ValueError("The job session has no retained project.")
    return _project(session, row.project_id, allow_archived=allow_archived)


def _workflow_session(session: Session, workflow_run_id: str) -> str:
    session_id = session.scalar(
        select(WorkflowRun.session_id).where(WorkflowRun.id == workflow_run_id)
    )
    if session_id is None:
        raise ValueError("The job workflow has no retained session.")
    return session_id


def _initial_workspace(session: Session) -> str:
    owners = session.execute(
        select(User.id, User.is_active, User.is_instance_admin).where(
            User.legacy_subject == "owner"
        )
    ).all()
    if len(owners) != 1 or not owners[0].is_active or not owners[0].is_instance_admin:
        raise RuntimeError("The migration-seeded owner user is missing or invalid.")
    workspaces = session.execute(
        select(Workspace.id, Workspace.state).where(
            Workspace.kind == "personal", Workspace.personal_owner_user_id == owners[0].id
        )
    ).all()
    if len(workspaces) != 1 or workspaces[0].state != "active":
        raise RuntimeError("The migration-seeded owner workspace is missing or inactive.")
    workspace_id = workspaces[0].id
    role = session.scalar(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == owners[0].id,
        )
    )
    if role != "owner":
        raise RuntimeError("The migration-seeded owner workspace membership is invalid.")
    return workspace_id


def job_creation_ownership(
    session: Session,
    *,
    session_id: str | None = None,
    workflow_run_id: str | None = None,
    project_id: str | None = None,
    workspace_id: str | None = None,
) -> JobOwnership:
    """Resolve construction context from current SQL, never from job payloads.

    Column reads deliberately bypass the ORM identity map. Admission runs this
    after obtaining its writer lock and must see roots changed while it waited.
    Session writability and host/actor permission remain separate checks.
    """
    with session.no_autoflush:
        if workflow_run_id is not None:
            workflow_session_id = _workflow_session(session, workflow_run_id)
            if session_id is not None and workflow_session_id != session_id:
                raise ValueError("The job session and workflow disagree.")
            session_id = workflow_session_id
        by_session = (
            _session(session, session_id, allow_archived=False) if session_id is not None else None
        )
        by_project = (
            _project(session, project_id, allow_archived=False) if project_id is not None else None
        )
        if by_session is not None and by_project is not None and by_session != by_project:
            raise ValueError("The job session and requested project disagree.")
        owner = by_session or by_project
        if workspace_id is not None:
            selected = _workspace(session, workspace_id, allow_archived=False)
            if owner is not None and owner.workspace_id != selected:
                raise ValueError("The job workspace and project disagree.")
            if owner is None:
                owner = JobOwnership(selected, None)
        return owner or JobOwnership(_initial_workspace(session), None)


def job_ownership(session: Session, job: Job, *, allow_archived: bool = False) -> JobOwnership:
    """Validate retained scope, including a sessionless project left after purge."""
    with session.no_autoflush:
        if not isinstance(job.workspace_id, str) or not job.workspace_id:
            raise ValueError("The job has no stored workspace ownership.")
        workspace_id = _workspace(session, job.workspace_id, allow_archived=allow_archived)
        session_id = job.session_id
        if job.workflow_run_id is not None:
            workflow_session_id = _workflow_session(session, job.workflow_run_id)
            if session_id is not None and session_id != workflow_session_id:
                raise ValueError("The stored job session and workflow disagree.")
            session_id = workflow_session_id
        owner = (
            _session(session, session_id, allow_archived=allow_archived)
            if session_id is not None
            else None
        )
        if owner is not None and (job.project_id is None or owner.project_id != job.project_id):
            raise ValueError("The stored job session and project disagree.")
        if job.project_id is not None:
            project_owner = _project(
                session,
                job.project_id,
                allow_archived=allow_archived,
                allow_retained_root=session_id is None,
            )
            if project_owner.workspace_id != workspace_id:
                raise ValueError("The stored job project belongs to another workspace.")
        if owner is not None and owner.workspace_id != workspace_id:
            raise ValueError("The stored job session belongs to another workspace.")
        return JobOwnership(workspace_id, job.project_id)


__all__ = ["JobOwnership", "job_creation_ownership", "job_ownership"]
