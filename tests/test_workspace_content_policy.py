"""Workspace-member content authority across current SQL access policies."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pandrator.web.access.artifact_policy import (
    ArtifactNotFound,
    require_artifact_access,
    visible_artifact_ids_statement,
)
from pandrator.web.access.contracts import (
    AccessContext,
    AccessDenied,
    ProjectAction,
    ProjectNotFound,
)
from pandrator.web.access.job_policy import (
    JobAction,
    JobNotFound,
    job_cancellation_clause,
    job_visibility_clause,
    require_job_access,
)
from pandrator.web.access.media_execution_policy import require_media_execution_artifact
from pandrator.web.access.project_permissions import (
    CONTENT_ACTIONS,
    GOVERNANCE_ACTIONS,
)
from pandrator.web.access.project_policy import (
    require_project_access,
    visible_project_ids_statement,
)
from pandrator.web.access.project_visibility import (
    project_ids_for_user,
    workspace_authority,
)
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenProjectGrant
from pandrator.web.models import Artifact, Job, new_id
from pandrator.web.projects.models import ProjectMembership
from tests.test_job_access_routes import case as case  # noqa: F401 - pytest fixture registration
from tests.test_job_access_routes import credential as route_credential

ALL_SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})


def _make_member(
    case: Any,
    name: str,
    *,
    project_role: str | None = None,
) -> None:
    user_id = case.users[name]
    with case.database.immediate_session() as session:
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == user_id,
            )
        )
        session.add(
            WorkspaceMembership(
                workspace_id=case.team,
                user_id=user_id,
                role="member",
            )
        )
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == case.a.project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        if project_role is not None:
            session.add(
                ProjectMembership(
                    project_id=case.a.project_id,
                    user_id=user_id,
                    role=project_role,
                )
            )


def _project_ids(
    session: Session,
    context: AccessContext,
    action: ProjectAction = ProjectAction.READ,
) -> set[str]:
    statement = visible_project_ids_statement(session, context, action)
    return set(session.scalars(statement))


def _artifact(
    case: Any,
    *,
    workspace_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> str:
    identifier = new_id()
    row = Artifact(
        id=identifier,
        workspace_id=workspace_id or case.team,
        project_id=project_id,
        session_id=session_id,
        kind="synthetic.workspace-content",
        role="source",
        relative_path=f"workspace-content/{identifier}.bin",
        content_hash="workspace-content-hash",
        size_bytes=32,
        state="current",
        metadata_json={},
    )
    with case.database.immediate_session() as session:
        session.add(row)
        session.flush()
    return identifier


def _job(case: Any, *, project_id: str | None, author: str) -> str:
    identifier = new_id()
    with case.database.immediate_session() as session:
        session.add(
            Job(
                id=identifier,
                kind="future.synthetic",
                workspace_id=case.team,
                project_id=project_id,
                payload_json={"synthetic": True},
            )
        )
        session.flush()
        session.add(
            JobActorRecord(
                job_id=identifier,
                user_id=case.users[author],
                auth_revision=1,
                context_json={"version": 1, "source": "user"},
            )
        )
    return identifier


def _job_ids(session: Session, context: AccessContext, *, cancel: bool = False) -> set[str]:
    clause = job_cancellation_clause if cancel else job_visibility_clause
    return set(session.scalars(select(Job.id).where(clause(session, context))))


@pytest.mark.parametrize("builder", (project_ids_for_user, workspace_authority))
def test_workspace_visibility_helpers_reject_string_action_lookalikes(builder: Any) -> None:
    with pytest.raises(TypeError, match="project action"):
        builder("synthetic-user", action="read")


def test_zero_project_role_member_has_action_list_and_direct_parity_and_credential_ceiling(
    case: Any,
) -> None:
    _make_member(case, "member")
    member = route_credential(case, "member", scopes=ALL_SCOPES)
    context = AccessContext(member.actor)

    with case.database.snapshot_session() as session:
        access = require_project_access(session, context, case.a.project_id)
        assert access.allowed_actions == CONTENT_ACTIONS
        for action in ProjectAction:
            visible = case.a.project_id in _project_ids(session, context, action)
            if action in CONTENT_ACTIONS:
                assert visible
                assert (
                    action
                    in require_project_access(
                        session, context, case.a.project_id, action
                    ).allowed_actions
                )
            else:
                assert action in GOVERNANCE_ACTIONS
                assert not visible
                with pytest.raises(AccessDenied):
                    require_project_access(session, context, case.a.project_id, action)

    unscoped = route_credential(case, "member", scopes=ALL_SCOPES, grant_team=False)
    with case.database.snapshot_session() as session:
        with pytest.raises(ProjectNotFound):
            require_project_access(session, AccessContext(unscoped.actor), case.a.project_id)
        assert not _project_ids(session, AccessContext(unscoped.actor))

    assert unscoped.actor.token_id is not None
    with case.database.immediate_session() as session:
        session.add(
            ApiTokenProjectGrant(
                token_id=unscoped.actor.token_id,
                project_id=case.a.project_id,
            )
        )
    with case.database.snapshot_session() as session:
        limited_context = AccessContext(unscoped.actor)
        assert case.a.project_id in _project_ids(session, limited_context)
        assert case.a.project_id not in _project_ids(
            session, limited_context, ProjectAction.MANAGE_MEMBERS
        )


def test_member_manager_project_row_does_not_grant_governance(case: Any) -> None:
    _make_member(case, "member", project_role="manager")
    actor = route_credential(case, "member", scopes=ALL_SCOPES)
    context = AccessContext(actor.actor)

    with case.database.snapshot_session() as session:
        access = require_project_access(session, context, case.a.project_id)
        assert access.allowed_actions == CONTENT_ACTIONS
        for action in GOVERNANCE_ACTIONS:
            assert case.a.project_id not in _project_ids(session, context, action)
            with pytest.raises(AccessDenied):
                require_project_access(session, context, case.a.project_id, action)


def test_member_artifact_authority_and_project_only_credential_ceiling(case: Any) -> None:
    _make_member(case, "member")
    actor = route_credential(case, "member", scopes=ALL_SCOPES)
    context = AccessContext(actor.actor)
    project_artifact = _artifact(
        case,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    rootless_artifact = _artifact(case)
    foreign_artifact = _artifact(
        case,
        workspace_id=case.personal[case.users["editor"]],
    )

    with case.database.snapshot_session() as session:
        visible = set(session.scalars(visible_artifact_ids_statement(session, context)))
        assert visible == {project_artifact, rootless_artifact}
        assert require_artifact_access(session, context, project_artifact).id == project_artifact
        assert require_artifact_access(session, context, rootless_artifact).id == rootless_artifact
        assert foreign_artifact not in visible
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, context, foreign_artifact)

    project_only = route_credential(
        case,
        "member",
        scopes=ALL_SCOPES,
        grant_team=False,
    )
    assert project_only.actor.token_id is not None
    with case.database.immediate_session() as session:
        session.add(
            ApiTokenProjectGrant(
                token_id=project_only.actor.token_id,
                project_id=case.a.project_id,
            )
        )
    with case.database.snapshot_session() as session:
        limited_context = AccessContext(project_only.actor)
        assert set(session.scalars(visible_artifact_ids_statement(session, limited_context))) == {
            project_artifact
        }
        with pytest.raises(ArtifactNotFound):
            require_artifact_access(session, limited_context, rootless_artifact)

    instance_admin = route_credential(
        case,
        "outsider",
        scopes=ALL_SCOPES | {"app.admin"},
    )
    with case.database.snapshot_session() as session:
        assert not set(
            session.scalars(
                visible_artifact_ids_statement(session, AccessContext(instance_admin.actor))
            )
        )


def test_member_jobs_are_shared_for_read_run_and_cancellation(case: Any) -> None:
    _make_member(case, "member")
    _make_member(case, "editor")
    member = route_credential(case, "member", scopes=ALL_SCOPES)
    other_member_job = _job(
        case,
        project_id=case.a.project_id,
        author="editor",
    )
    rootless_team_job = _job(case, project_id=None, author="editor")
    context = AccessContext(member.actor)

    with case.database.snapshot_session() as session:
        assert _job_ids(session, context) == {other_member_job, rootless_team_job}
        assert _job_ids(session, context, cancel=True) == {other_member_job, rootless_team_job}
        for identifier in (other_member_job, rootless_team_job):
            assert require_job_access(session, context, identifier, JobAction.READ).id == identifier
            assert require_job_access(session, context, identifier, JobAction.RUN).id == identifier
            assert (
                require_job_access(session, context, identifier, JobAction.CANCEL).id == identifier
            )

    read_only = route_credential(case, "member", scopes=frozenset({"app.read"}))
    with case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            require_job_access(
                session, AccessContext(read_only.actor), other_member_job, JobAction.RUN
            )
        with pytest.raises(AccessDenied):
            require_project_access(
                session,
                AccessContext(read_only.actor),
                case.a.project_id,
                ProjectAction.RUN,
            )
        with pytest.raises(AccessDenied):
            visible_project_ids_statement(
                session,
                AccessContext(read_only.actor),
                ProjectAction.RUN,
            )

    with case.database.immediate_session() as session:
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == case.users["member"],
            )
        )
    with case.database.snapshot_session() as session:
        assert not _job_ids(session, context)
        with pytest.raises(JobNotFound):
            require_job_access(session, context, other_member_job)


def test_workspace_member_can_run_media_source_without_project_membership(case: Any) -> None:
    _make_member(case, "member")
    actor = route_credential(case, "member", scopes=ALL_SCOPES)
    source_id = _artifact(
        case,
        project_id=case.a.project_id,
        session_id=case.a.id,
    )
    with execution_actor_scope(actor.actor):
        with case.database.snapshot_session() as session:
            result = require_media_execution_artifact(
                session,
                {"source_artifact_id": source_id},
                "audio.preview",
            )
    assert result.id == source_id


@pytest.mark.parametrize(
    "scope,action", [("app.run", JobAction.RUN), ("app.cancel", JobAction.CANCEL)]
)
def test_rootless_team_work_requires_read_in_addition_to_effect_scope(
    case: Any, scope: str, action: JobAction
) -> None:
    _make_member(case, "member")
    identifier = _job(case, project_id=None, author="owner")
    narrowed = route_credential(case, "member", scopes=frozenset({scope}))
    with case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            require_job_access(session, AccessContext(narrowed.actor), identifier, action)
