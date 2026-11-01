"""Retained accounting ownership, independent of permission to execute work."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import false, inspect, or_, select
from sqlalchemy.orm import Session

from .accounts.models import Workspace
from .job_identity import job_creation_ownership, job_ownership
from .models import (
    AgentRun,
    Artifact,
    GenerationRun,
    Job,
    SessionRecord,
    SourceAsset,
    TranslationProject,
    UsageEvent,
    WorkflowRun,
)
from .projects.models import Project


@dataclass(frozen=True)
class UsageOwnership:
    workspace_id: str
    project_id: str | None


def _merge(owners: list[UsageOwnership]) -> UsageOwnership:
    workspaces = {owner.workspace_id for owner in owners}
    projects = {owner.project_id for owner in owners if owner.project_id is not None}
    if len(workspaces) != 1 or len(projects) > 1:
        raise ValueError("The usage references conflicting ownership roots.")
    return UsageOwnership(next(iter(workspaces)), next(iter(projects), None))


class _UsageRoots:
    def __init__(self, session: Session):
        self.session = session

    def workspace(self, workspace_id: str) -> UsageOwnership:
        state = self.session.scalar(select(Workspace.state).where(Workspace.id == workspace_id))
        if state not in {"active", "archived"}:
            raise ValueError("The usage references a missing or invalid workspace.")
        return UsageOwnership(workspace_id, None)

    def project(self, project_id: str) -> UsageOwnership:
        row = self.session.execute(
            select(Project.workspace_id, Project.source_session_id).where(Project.id == project_id)
        ).one_or_none()
        if row is None:
            raise ValueError("The usage references a missing project.")
        workspace = self.workspace(row.workspace_id)
        if row.source_session_id is None:
            surviving = self.session.scalar(
                select(SessionRecord.id).where(SessionRecord.project_id == project_id).limit(1)
            )
            if surviving is not None:
                raise ValueError("The usage project has no source session.")
        else:
            source_project = self.session.scalar(
                select(SessionRecord.project_id).where(SessionRecord.id == row.source_session_id)
            )
            if source_project != project_id:
                raise ValueError("The usage project has an inconsistent source root.")
        translations = self.session.execute(
            select(TranslationProject.project_root_id, TranslationProject.source_session_id).where(
                or_(
                    TranslationProject.project_root_id == project_id,
                    TranslationProject.source_session_id == row.source_session_id
                    if row.source_session_id is not None
                    else false(),
                )
            )
        ).all()
        for translation in translations:
            source_project = self.session.scalar(
                select(SessionRecord.project_id).where(
                    SessionRecord.id == translation.source_session_id
                )
            )
            if source_project != project_id or translation.project_root_id not in {
                None,
                project_id,
            }:
                raise ValueError("The usage project and translation roots disagree.")
        return UsageOwnership(workspace.workspace_id, project_id)

    def session_root(self, session_id: str) -> UsageOwnership:
        project_id = self.session.scalar(
            select(SessionRecord.project_id).where(SessionRecord.id == session_id)
        )
        if project_id is None:
            raise ValueError("The usage session has no retained project.")
        return self.project(project_id)

    def workflow(self, workflow_run_id: str) -> UsageOwnership:
        session_id = self.session.scalar(
            select(WorkflowRun.session_id).where(WorkflowRun.id == workflow_run_id)
        )
        if session_id is None:
            raise ValueError("The usage workflow has no retained session.")
        return self.session_root(session_id)

    def job(self, job_id: str) -> UsageOwnership:
        row = self.session.execute(
            select(Job.workspace_id, Job.project_id, Job.session_id, Job.workflow_run_id).where(
                Job.id == job_id
            )
        ).one_or_none()
        if row is None:
            raise ValueError("The usage references a missing job.")
        # A detached value object keeps the existing job validator's exact
        # workflow/session rules without relying on a cached ORM instance.
        owner = job_ownership(self.session, Job(**row._mapping), allow_archived=True)
        return UsageOwnership(owner.workspace_id, owner.project_id)

    def artifact(self, artifact_id: str) -> UsageOwnership:
        row = self.session.execute(
            select(Artifact.workspace_id, Artifact.project_id, Artifact.session_id).where(
                Artifact.id == artifact_id
            )
        ).one_or_none()
        if row is None or row.workspace_id is None:
            raise ValueError("The usage artifact has no stored workspace.")
        owners = [self.workspace(row.workspace_id)]
        if row.session_id is not None:
            session_owner = self.session_root(row.session_id)
            if row.project_id is None or session_owner.project_id != row.project_id:
                raise ValueError("The usage artifact session and project disagree.")
            owners.append(session_owner)
        if row.project_id is not None:
            owners.append(self.project(row.project_id))
        for workspace_id in self.session.scalars(
            select(SourceAsset.workspace_id).where(SourceAsset.artifact_id == artifact_id)
        ):
            if workspace_id is None:
                raise ValueError("The usage source asset has no stored workspace.")
            owners.append(self.workspace(workspace_id))
        return _merge(owners)

    def generation(self, generation_run_id: str) -> UsageOwnership:
        row = self.session.execute(
            select(GenerationRun.session_id, GenerationRun.job_id).where(
                GenerationRun.id == generation_run_id
            )
        ).one_or_none()
        if row is None:
            raise ValueError("The usage references a missing generation run.")
        owners = [self.session_root(row.session_id)]
        if row.job_id is not None:
            owners.append(self.job(row.job_id))
        return _merge(owners)

    def agent(self, agent_run_id: str) -> UsageOwnership:
        row = self.session.execute(
            select(
                AgentRun.session_id,
                AgentRun.job_id,
                AgentRun.source_artifact_id,
                AgentRun.result_artifact_id,
            ).where(AgentRun.id == agent_run_id)
        ).one_or_none()
        if row is None:
            raise ValueError("The usage references a missing agent run.")
        owners = [self.session_root(row.session_id)]
        if row.job_id is not None:
            owners.append(self.job(row.job_id))
        if row.source_artifact_id is not None:
            source = self.artifact(row.source_artifact_id)
            # Independent session copies can reuse a source asset from another
            # project in this workspace. Usage belongs to the executing run.
            owners.append(UsageOwnership(source.workspace_id, None))
        if row.result_artifact_id is not None:
            owners.append(self.artifact(row.result_artifact_id))
        return _merge(owners)


def usage_creation_ownership(
    session: Session,
    *,
    session_id: str | None = None,
    workflow_run_id: str | None = None,
    job_id: str | None = None,
    artifact_id: str | None = None,
    generation_run_id: str | None = None,
    agent_run_id: str | None = None,
    workspace_id: str | None = None,
    project_id: str | None = None,
) -> UsageOwnership:
    """Validate all supplied SQL evidence, including already-executed history.

    Archive and trash do not erase accounting ownership. Missing or conflicting
    roots are never repaired from provider JSON or silently assigned elsewhere.
    """
    roots = _UsageRoots(session)
    with session.no_autoflush:
        owners = []
        for identity, resolver in (
            (session_id, roots.session_root),
            (workflow_run_id, roots.workflow),
            (job_id, roots.job),
            (artifact_id, roots.artifact),
            (generation_run_id, roots.generation),
            (agent_run_id, roots.agent),
            (workspace_id, roots.workspace),
            (project_id, roots.project),
        ):
            if identity is not None:
                if not isinstance(identity, str) or not identity:
                    raise ValueError("The usage contains an invalid ownership reference.")
                owners.append(resolver(identity))
        if not owners:
            initial = job_creation_ownership(session)
            return UsageOwnership(initial.workspace_id, None)
        return _merge(owners)


def bind_usage_ownership(session: Session, event: UsageEvent) -> UsageOwnership:
    """Bind a new receipt or validate retained scope in the caller's writer."""
    owner = usage_creation_ownership(
        session,
        session_id=event.session_id,
        workflow_run_id=event.workflow_run_id,
        job_id=event.job_id,
        artifact_id=event.artifact_id,
        generation_run_id=event.generation_run_id,
        agent_run_id=event.agent_run_id,
        workspace_id=event.workspace_id,
        project_id=event.project_id,
    )
    if event.workspace_id is not None or event.project_id is not None:
        if (event.workspace_id, event.project_id) != (owner.workspace_id, owner.project_id):
            raise ValueError("The usage receipt's stored ownership disagrees with its roots.")
    else:
        with session.no_autoflush:
            persisted = inspect(event).persistent or (
                event.id is not None
                and session.scalar(select(UsageEvent.id).where(UsageEvent.id == event.id))
                is not None
            )
        if persisted:
            raise ValueError("The retained usage receipt has no stored ownership.")
        event.workspace_id, event.project_id = owner.workspace_id, owner.project_id
    return owner


__all__ = ["UsageOwnership", "bind_usage_ownership", "usage_creation_ownership"]
