"""Synthetic, caller-session acceptance for job access; no worker execution."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import requests.sessions
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.orm import Session

from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.access.job_policy import (
    JobAction,
    JobNotFound,
    job_cancellation_clause,
    job_visibility_clause,
    require_job_access,
)
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import actor_from_principal
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.private_work_models import QuickTranscriptionOwner
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
    AutomationClientWorkspaceGrant,
)
from pandrator.web.api import create_app
from pandrator.web.models import (
    ApiToken,
    AutomationClient,
    Job,
    QuickTranscription,
    SessionRecord,
    WorkflowRun,
    new_id,
    utcnow,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import Project, ProjectMembership

SCOPES = frozenset({"app.read", "app.cancel"})


@pytest.fixture(scope="module", autouse=True)
def block_network_attempts() -> Iterator[None]:
    attempts: list[str] = []
    patcher = pytest.MonkeyPatch()

    def blocked(*_args: object, **_kwargs: object) -> None:
        attempts.append("network")
        raise AssertionError("Job access tests must not use the network.")

    patcher.setattr(requests.sessions.Session, "request", blocked)
    patcher.setattr(socket.socket, "connect", blocked)
    try:
        yield
    finally:
        patcher.undo()
        assert not attempts, f"Unexpected network attempts: {attempts!r}"


@pytest.fixture
def case(tmp_path: Path) -> Iterator[Any]:
    app = create_app(data_root=tmp_path / "job-access", testing=True, background_maintenance=False)
    services = app.extensions["pandrator"]
    database, auth = services["database"], services["auth"]
    try:
        owner_id = auth.initial_account().user_id
        with database.session() as session:
            owner = session.get(User, owner_id)
            assert owner is not None
            owner.is_instance_admin = True
            personal = {owner_id: initial_personal_workspace(session).id}
            user_id, other_id, outsider_id = new_id(), new_id(), new_id()
            for user in (user_id, other_id, outsider_id):
                session.add(
                    User(
                        id=user,
                        display_name="Synthetic user",
                        is_active=True,
                        is_instance_admin=user == outsider_id,
                    )
                )
                session.flush()
                session.add(UserCredential(user_id=user, login_name=user, auth_revision=1))
                workspace = Workspace(
                    id=new_id(),
                    kind="personal",
                    name="Synthetic personal",
                    personal_owner_user_id=user,
                    state="active",
                )
                session.add(workspace)
                session.flush()
                session.add(
                    WorkspaceMembership(workspace_id=workspace.id, user_id=user, role="owner")
                )
                personal[user] = workspace.id
            team = Workspace(id=new_id(), kind="team", name="Synthetic team", state="active")
            session.add(team)
            session.flush()
            for user, role in ((owner_id, "owner"),):
                session.add(WorkspaceMembership(workspace_id=team.id, user_id=user, role=role))
            project_a = create_session_project(
                session, SessionRecord(id=new_id(), name="A"), workspace_id=team.id
            )
            project_b = create_session_project(
                session, SessionRecord(id=new_id(), name="B"), workspace_id=team.id
            )
            project_a.name = "A"
            project_b.name = "B"
            session.flush()
            session.add_all(
                [
                    ProjectMembership(project_id=project_a.id, user_id=user_id, role="editor"),
                    ProjectMembership(project_id=project_b.id, user_id=other_id, role="manager"),
                ]
            )
        yield SimpleNamespace(
            database=database,
            auth=auth,
            owner=owner_id,
            user=user_id,
            other=other_id,
            outsider=outsider_id,
            personal=personal,
            team=team.id,
            a=project_a.id,
            b=project_b.id,
        )
    finally:
        services["quick_transcriptions"].stop_maintenance()
        database.dispose()


def _context(
    case: Any,
    user: str,
    *,
    scopes: frozenset[str] = SCOPES,
    workspaces: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
    subject: str | None = None,
    client_id: str | None = None,
) -> AccessContext:
    _token, raw = case.auth.create_api_token(
        "Synthetic job policy",
        user_id=user,
        auth_revision=1,
        scopes=scopes,
        subject=subject,
        client_id=client_id,
        resource_grants=CredentialResources(frozenset(workspaces), frozenset(projects)),
    )
    principal = case.auth.resolve_api_token(raw, network_zone="loopback", target_instance_id=None)
    assert principal is not None
    actor = actor_from_principal(principal)
    assert actor is not None
    return AccessContext(actor)


def _job(
    session: Session,
    workspace: str,
    *,
    author: str | None = None,
    host: bool = False,
    kind: str = "future.synthetic",
    **roots: Any,
) -> str:
    job = Job(
        id=new_id(), kind=kind, workspace_id=workspace, payload_json={"synthetic": True}, **roots
    )
    session.add(job)
    session.flush()
    if author is not None or host:
        session.add(
            JobActorRecord(
                job_id=job.id,
                user_id=author,
                auth_revision=7 if author else None,
                context_json={"version": 1, "source": "user"}
                if author
                else {
                    "version": 1,
                    "source": "host",
                    "label": "pandrator-cli",
                },
            )
        )
        session.flush()
    return job.id


def _ids(session: Session, context: AccessContext, *, cancel: bool = False) -> set[str]:
    clause = job_cancellation_clause if cancel else job_visibility_clause
    return set(session.scalars(select(Job.id).where(clause(session, context))))


def _hidden(session: Session, context: AccessContext, identifier: str) -> None:
    for action in JobAction:
        if action is JobAction.RUN and "app.run" not in context.actor.scopes:
            with pytest.raises(AccessDenied):
                require_job_access(session, context, identifier, action)
        else:
            with pytest.raises(JobNotFound) as error:
                require_job_access(session, context, identifier, action)
            assert error.value.args == ("Job not found.",)
        assert (Job, (identifier,), None) not in session.identity_map


@pytest.mark.parametrize("role", ["viewer", "reviewer", "editor", "manager"])
def test_project_roles_actual_authorship_and_workspace_authority(case: Any, role: str) -> None:
    first = _context(case, case.user, projects=(case.a,), subject="first-subject")
    second = _context(case, case.user, projects=(case.a,), subject="second-subject")
    admin = _context(case, case.owner, workspaces=(case.team,))
    outsider = _context(case, case.outsider, scopes=SCOPES | {"app.admin"})
    with case.database.session() as session:
        # Keep the role denial observable even with an overbroad captured ceiling.
        session.add(
            ApiTokenWorkspaceGrant(token_id=outsider.actor.token_id, workspace_id=case.team)
        )
        session.flush()
        session.execute(
            update(ProjectMembership)
            .where(
                ProjectMembership.project_id == case.a,
                ProjectMembership.user_id == case.user,
            )
            .values(role=role)
        )
        own = _job(session, case.team, project_id=case.a, author=case.user)
        other = _job(session, case.team, project_id=case.a, author=case.other)
        unknown = _job(session, case.team, project_id=case.a)
        hidden = _job(session, case.team, project_id=case.b, author=case.user)
        assert _ids(session, first) == {own, other, unknown}
        assert _ids(session, second, cancel=True) == set()
        assert require_job_access(session, second, own).id == own
        with pytest.raises(AccessDenied):
            require_job_access(session, second, own, JobAction.CANCEL)
        with pytest.raises(AccessDenied):
            require_job_access(session, first, other, JobAction.CANCEL)
        assert {own, other, unknown, hidden} <= _ids(session, admin, cancel=True)
        _hidden(session, first, hidden)
        _hidden(session, outsider, hidden)


def test_project_only_job_reads_hide_after_source_is_trashed(case: Any) -> None:
    context = _context(case, case.user, projects=(case.a,))
    with case.database.session() as session:
        project = session.get(Project, case.a)
        assert project is not None and project.source_session_id is not None
        source = session.get(SessionRecord, project.source_session_id)
        assert source is not None and source.trashed_at is None
        source.trashed_at = utcnow()
        job = _job(session, case.team, project_id=case.a)
        _hidden(session, context, job)


@pytest.mark.parametrize("role", ["editor", "manager", "reviewer", "viewer"])
def test_project_only_roles_cannot_run_even_for_another_author(case: Any, role: str) -> None:
    actor = _context(case, case.user, scopes=frozenset({"app.read", "app.run"}), projects=(case.a,))
    outsider = _context(case, case.outsider, scopes=frozenset({"app.read", "app.run"}))
    with case.database.session() as session:
        session.execute(
            update(ProjectMembership)
            .where(
                ProjectMembership.project_id == case.a,
                ProjectMembership.user_id == case.user,
            )
            .values(role=role)
        )
        job = _job(session, case.team, project_id=case.a, author=case.other)
        with pytest.raises(AccessDenied):
            require_job_access(session, actor, job, JobAction.RUN)
        with pytest.raises(JobNotFound):
            require_job_access(session, outsider, job, JobAction.RUN)


@pytest.mark.parametrize(
    "scopes", [frozenset({"app.read"}), frozenset({"app.run"})], ids=["read-only", "run-only"]
)
def test_project_run_requires_both_read_and_run_scopes(case: Any, scopes: frozenset[str]) -> None:
    context = _context(case, case.user, scopes=scopes, projects=(case.a,))
    with case.database.session() as session:
        job = _job(session, case.team, project_id=case.a, author=case.other)
        with pytest.raises(AccessDenied):
            require_job_access(session, context, job, JobAction.RUN)


@pytest.mark.parametrize("workspace_role", ["owner", "admin"])
def test_project_run_accepts_current_workspace_owner_or_admin(
    case: Any, workspace_role: str
) -> None:
    if workspace_role == "admin":
        with case.database.session() as session:
            session.execute(
                update(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == case.owner,
                )
                .values(role="admin")
            )
    context = _context(
        case,
        case.owner,
        scopes=frozenset({"app.read", "app.run"}),
        workspaces=(case.team,),
    )
    with case.database.session() as session:
        job = _job(session, case.team, project_id=case.a, author=case.user)
        assert require_job_access(session, context, job, JobAction.RUN).id == job


def test_project_only_job_reads_stay_read_only_across_role_and_grant_changes(case: Any) -> None:
    context = _context(
        case,
        case.user,
        scopes=frozenset({"app.read", "app.run"}),
        projects=(case.a,),
    )
    assert context.actor.token_id is not None
    with case.database.session() as session:
        job = _job(session, case.team, project_id=case.a, author=case.other)
        assert require_job_access(session, context, job, JobAction.READ).id == job
        with pytest.raises(AccessDenied):
            require_job_access(session, context, job, JobAction.RUN)
        session.execute(
            update(ProjectMembership)
            .where(
                ProjectMembership.project_id == case.a,
                ProjectMembership.user_id == case.user,
            )
            .values(role="viewer")
        )
        assert require_job_access(session, context, job, JobAction.READ).id == job
        with pytest.raises(AccessDenied):
            require_job_access(session, context, job, JobAction.RUN)
        session.execute(
            update(ProjectMembership)
            .where(
                ProjectMembership.project_id == case.a,
                ProjectMembership.user_id == case.user,
            )
            .values(role="editor")
        )
        session.execute(
            delete(ApiTokenProjectGrant).where(
                ApiTokenProjectGrant.token_id == context.actor.token_id
            )
        )
        with pytest.raises(JobNotFound):
            require_job_access(session, context, job, JobAction.READ)
        with pytest.raises(JobNotFound):
            require_job_access(session, context, job, JobAction.RUN)


def test_team_projectless_own_work_and_explicit_workspace_ceiling(case: Any) -> None:
    with case.database.session() as session:
        session.add(
            WorkspaceMembership(
                workspace_id=case.team,
                user_id=case.user,
                role="member",
            )
        )
    run_scopes = SCOPES | {"app.run"}
    own = _context(case, case.user, scopes=run_scopes, workspaces=(case.team,))
    another_subject = _context(
        case, case.user, scopes=run_scopes, workspaces=(case.team,), subject="another"
    )
    empty = _context(case, case.user)
    project_only = _context(case, case.user, projects=(case.a,))
    admin = _context(case, case.owner, workspaces=(case.team,))
    with case.database.session() as session:
        mine = _job(session, case.team, author=case.user, kind="training.xtts")
        other = _job(session, case.team, author=case.other)
        unknown = _job(session, case.team)
        for context in (own, another_subject):
            shared_team_jobs = {mine, other, unknown}
            assert _ids(session, context) == shared_team_jobs
            assert _ids(session, context, cancel=True) == shared_team_jobs
            for identifier in shared_team_jobs:
                assert (
                    require_job_access(session, context, identifier, JobAction.RUN).id == identifier
                )
        assert _ids(session, admin, cancel=True) == {mine, other, unknown}
        for context in (empty, project_only):
            _hidden(session, context, mine)
        session.execute(
            update(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == case.user,
            )
            .values(role="admin")
        )
        assert _ids(session, own, cancel=True) == {mine, other, unknown}


def test_personal_owner_legacy_host_and_misrooted_jobs(case: Any) -> None:
    owner = _context(case, case.user, workspaces=(case.personal[case.user],))
    other = _context(case, case.other, workspaces=(case.personal[case.other],))
    with case.database.session() as session:
        workspace = case.personal[case.user]
        own = _job(session, workspace, author=case.user)
        legacy = _job(session, workspace)
        host = _job(session, workspace, host=True)
        misrooted = _job(session, workspace, author=case.other)
        assert _ids(session, owner) == {own, legacy, host}
        assert _ids(session, owner, cancel=True) == {own, legacy, host}
        _hidden(session, owner, misrooted)
        _hidden(session, other, misrooted)


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        "null",
        "[]",
        "1",
        '"string"',
        '{"version":true,"source":"user"}',
        '{"version":1.0,"source":"user"}',
        '{"version":"1","source":"user"}',
        '{"version":2,"source":"user"}',
        '{"version":1,"source":"host","label":"wrong"}',
        "{}",
    ],
)
def test_present_malformed_attribution_never_gets_personal_legacy_fallback(
    case: Any, raw: str
) -> None:
    context = _context(case, case.user, workspaces=(case.personal[case.user],))
    with case.database.session() as session:
        job = _job(session, case.personal[case.user], author=case.user)
        session.execute(
            text("UPDATE job_actors SET context_json=:raw WHERE job_id=:id"),
            {"raw": raw, "id": job},
        )
        session.expunge_all()
        assert _ids(session, context) == set()
        assert _ids(session, context, cancel=True) == set()
        _hidden(session, context, job)


def test_private_qt_uses_owner_policy_without_any_generic_fallback(case: Any) -> None:
    workspace = case.personal[case.user]
    owner = _context(case, case.user, workspaces=(workspace,))
    run_only = _context(case, case.user, scopes=frozenset({"app.run"}), workspaces=(workspace,))
    cancel_only = _context(
        case, case.user, scopes=frozenset({"app.cancel"}), workspaces=(workspace,)
    )
    outsider = _context(
        case,
        case.owner,
        scopes=SCOPES | {"app.admin"},
        workspaces=(case.personal[case.owner],),
    )
    with case.database.session() as session:
        owned = _job(session, workspace, author=case.user, kind="transcription.quick")
        unresolved = _job(session, case.personal[case.owner], kind="future.synthetic", host=True)
        unlinked = _job(session, case.personal[case.owner], kind="transcription.quick", host=True)
        for job, owned_by in ((owned, case.user), (unresolved, None)):
            transcription = QuickTranscription(
                id=new_id(),
                job_id=job,
                owner_subject="same-untrusted-subject",
                source_suffix=".wav",
                size_bytes=1,
                sha256="0" * 64,
                state="queued",
                expires_at=utcnow() + timedelta(days=1),
            )
            session.add(transcription)
            session.flush()
            if owned_by is not None:
                session.add(
                    QuickTranscriptionOwner(
                        transcription_id=transcription.id,
                        user_id=owned_by,
                        workspace_id=workspace,
                    )
                )
                session.flush()
        assert require_job_access(session, owner, owned).id == owned
        assert require_job_access(session, cancel_only, owned, JobAction.CANCEL).id == owned
        assert require_job_access(session, run_only, owned, JobAction.RUN).id == owned
        for job in (unresolved, unlinked):
            with pytest.raises(JobNotFound):
                require_job_access(session, run_only, job, JobAction.RUN)
        assert _ids(session, owner, cancel=True) == {owned}
        for context in (owner, outsider):
            for job in (unresolved, unlinked):
                _hidden(session, context, job)
        _hidden(session, outsider, owned)


def test_literal_host_installation_needs_current_admin_scope_and_clean_roots(case: Any) -> None:
    # Prove content membership and a workspace grant cannot confer host administration.
    with case.database.session() as session:
        session.add(WorkspaceMembership(workspace_id=case.team, user_id=case.user, role="member"))
    admin = _context(case, case.owner, scopes=SCOPES | {"app.admin", "app.run"})
    admin_without_run = _context(case, case.owner, scopes=SCOPES | {"app.admin"})
    run_without_admin = _context(case, case.owner, scopes=SCOPES | {"app.run"})
    narrowed = _context(case, case.owner)
    user = _context(case, case.user, scopes=SCOPES | {"app.run"}, workspaces=(case.team,))
    content_admin = _context(
        case, case.owner, scopes=SCOPES | {"app.admin"}, workspaces=(case.team,)
    )
    with case.database.session() as session:
        host = _job(session, case.team, kind="rvc.model.upload")
        malformed = _job(session, case.team, kind="rvc.model.upload", project_id=case.a)
        training = _job(session, case.team, kind="training.xtts", author=case.other)
        prefix = _job(session, case.team, kind="rvc.model.upload.future", author=case.other)
        assert require_job_access(session, admin, host).id == host
        assert require_job_access(session, admin, host, JobAction.CANCEL).id == host
        assert require_job_access(session, admin, host, JobAction.RUN).id == host
        with pytest.raises(AccessDenied):
            require_job_access(session, admin_without_run, host, JobAction.RUN)
        with pytest.raises(JobNotFound):
            require_job_access(session, run_without_admin, host, JobAction.RUN)
        assert _ids(session, admin, cancel=True) == {host}
        for context in (narrowed, user):
            _hidden(session, context, host)
        for job in (malformed, training, prefix):
            _hidden(session, admin, job)
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == case.owner,
            )
        )
        _hidden(session, content_admin, training)
        _hidden(session, content_admin, prefix)
        assert require_job_access(session, content_admin, host).id == host
        cached = session.get(User, case.owner)
        assert cached is not None and cached.is_instance_admin
        session.execute(
            update(User).where(User.id == case.owner).values(is_instance_admin=False),
            execution_options={"synchronize_session": False},
        )
        assert cached.is_instance_admin
        with pytest.raises(AccessDenied):
            require_job_access(session, admin, host)


def test_sql_visibility_intersects_current_roles_and_grants_before_limit(case: Any) -> None:
    context = _context(case, case.user, projects=(case.a,))
    with case.database.session() as session:
        visible = [_job(session, case.team, project_id=case.a) for _ in range(3)]
        newer = [_job(session, case.team, project_id=case.b) for _ in range(60)]
        session.execute(
            update(Job).where(Job.id.in_(visible)).values(created_at=utcnow() - timedelta(days=1))
        )
        rows = list(
            session.scalars(
                select(Job)
                .where(job_visibility_clause(session, context))
                .order_by(Job.created_at.desc(), Job.id)
                .limit(50)
            )
        )
        assert {row.id for row in rows} == set(visible)
        assert not set(newer) & {row.id for row in rows}
        session.add(ProjectMembership(project_id=case.b, user_id=case.user, role="viewer"))
        session.flush()
        assert _ids(session, context) == set(visible)  # Role alone cannot widen the token.
        session.execute(update(Workspace).where(Workspace.id == case.team).values(state="archived"))
        assert _ids(session, context) == set()


def test_live_client_ceiling_never_turns_project_grants_into_workspace_grants(case: Any) -> None:
    client_id = new_id()
    with case.database.session() as session:
        session.add(
            WorkspaceMembership(
                workspace_id=case.team,
                user_id=case.user,
                role="member",
            )
        )
        session.add(
            AutomationClient(
                id=client_id,
                name="Synthetic job client",
                subject=client_id,
                redirect_uris_json=["http://localhost/callback"],
                allowed_scopes_json=sorted(SCOPES),
                target_instance_id="synthetic",
                canonical_origin="http://localhost",
                created_by="test",
                user_id=case.user,
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add(AutomationClientWorkspaceGrant(client_id=client_id, workspace_id=case.team))
    context = _context(case, case.user, workspaces=(case.team,), client_id=client_id)
    with case.database.session() as session:
        own = _job(session, case.team, author=case.user)
        project = _job(session, case.team, author=case.user, project_id=case.a)
        assert _ids(session, context, cancel=True) == {own, project}
        session.execute(
            delete(AutomationClientWorkspaceGrant).where(
                AutomationClientWorkspaceGrant.client_id == client_id
            )
        )
        session.add(AutomationClientProjectGrant(client_id=client_id, project_id=case.a))
        session.flush()
        assert _ids(session, context) == {project}
        _hidden(session, context, own)
        session.execute(
            update(AutomationClient)
            .where(AutomationClient.id == client_id)
            .values(allowed_scopes_json=["app.read"]),
            execution_options={"synchronize_session": False},
        )
        with pytest.raises(AccessDenied):
            job_visibility_clause(session, context)


@pytest.mark.parametrize("revocation", ["membership", "grant", "user", "epoch", "token"])
def test_same_unit_revocation_ignores_cached_orm_and_preserves_pending_changes(
    case: Any,
    revocation: str,
) -> None:
    context = _context(case, case.user, projects=(case.a,))
    with case.database.session() as setup:
        identifier = _job(setup, case.team, author=case.user, project_id=case.a)
    with case.database.session() as session:
        assert require_job_access(session, context, identifier)
        membership = session.get(ProjectMembership, (case.a, case.user))
        grant = session.get(ApiTokenProjectGrant, (context.actor.token_id, case.a))
        user = session.get(User, case.user)
        credential = session.get(UserCredential, case.user)
        token = session.get(ApiToken, context.actor.token_id)
        assert membership is not None and grant is not None
        assert user is not None and credential is not None and token is not None
        statements = {
            "membership": delete(ProjectMembership).where(
                ProjectMembership.project_id == case.a, ProjectMembership.user_id == case.user
            ),
            "grant": delete(ApiTokenProjectGrant).where(
                ApiTokenProjectGrant.token_id == context.actor.token_id
            ),
            "user": update(User).where(User.id == case.user).values(is_active=False),
            "epoch": update(UserCredential)
            .where(UserCredential.user_id == case.user)
            .values(auth_revision=2),
            "token": update(ApiToken)
            .where(ApiToken.id == context.actor.token_id)
            .values(revoked_at=utcnow()),
        }
        session.execute(statements[revocation], execution_options={"synchronize_session": False})
        assert membership.role == "editor" and user.is_active
        assert credential.auth_revision == 1 and token.revoked_at is None
        pending = Project(id=new_id(), workspace_id=case.team, name="Pending caller work")
        session.add(pending)
        transaction = session.get_transaction()
        flushes: list[bool] = []

        def flushed(*_args: object) -> None:
            flushes.append(True)

        event.listen(session, "before_flush", flushed)
        denial = JobNotFound if revocation in {"membership", "grant"} else AccessDenied
        try:
            with pytest.raises(denial):
                require_job_access(session, context, identifier, JobAction.READ)
            assert pending in session.new and session.get_transaction() is transaction
            assert session.in_transaction() and not flushes
        finally:
            event.remove(session, "before_flush", flushed)
        session.rollback()
    with case.database.session() as verify:
        assert verify.get(Project, pending.id) is None
        assert require_job_access(verify, context, identifier).id == identifier


def test_session_workflow_roots_and_trashed_history_are_authoritative(case: Any) -> None:
    context = _context(case, case.user, scopes=SCOPES | {"app.run"}, projects=(case.a,))
    with case.database.session() as session:
        member = SessionRecord(
            id=new_id(), project_id=case.a, name="Synthetic", trashed_at=utcnow()
        )
        other_member = SessionRecord(id=new_id(), project_id=case.b, name="Synthetic other")
        session.add_all([member, other_member])
        session.flush()
        workflow = WorkflowRun(id=new_id(), session_id=member.id, preset="synthetic")
        other_workflow = WorkflowRun(id=new_id(), session_id=other_member.id, preset="synthetic")
        session.add_all([workflow, other_workflow])
        session.flush()
        valid = _job(
            session,
            case.team,
            author=case.user,
            project_id=case.a,
            session_id=member.id,
            workflow_run_id=workflow.id,
        )
        assert require_job_access(session, context, valid).id == valid
        with pytest.raises(AccessDenied):
            require_job_access(session, context, valid, JobAction.RUN)
        with pytest.raises(AccessDenied):
            require_job_access(session, context, valid, JobAction.CANCEL)
        # Emulate corrupt historical roots only in this disposable fixture database.
        trigger = session.scalar(
            text("SELECT sql FROM sqlite_master WHERE name='pandrator_execution_scope_jobs_insert'")
        )
        assert trigger is not None
        session.execute(text("DROP TRIGGER pandrator_execution_scope_jobs_insert"))
        malformed = [
            _job(
                session, case.team, author=case.user, project_id=case.a, workflow_run_id=workflow.id
            ),
            _job(session, case.team, project_id=case.a, session_id=other_member.id),
            _job(session, case.personal[case.user], project_id=case.a),
            _job(session, case.team, session_id=member.id),
            _job(
                session,
                case.team,
                project_id=case.a,
                session_id=member.id,
                workflow_run_id=other_workflow.id,
            ),
        ]
        session.execute(text(trigger))
        for identifier in malformed:
            _hidden(session, context, identifier)


def test_read_hints_scope_failures_fixed_missing_and_no_hidden_payload_loading(case: Any) -> None:
    read_only = _context(case, case.user, scopes=frozenset({"app.read"}), projects=(case.a,))
    cancel_only = _context(case, case.user, scopes=frozenset({"app.cancel"}), projects=(case.a,))
    context = _context(case, case.user, projects=(case.a,))
    with case.database.session() as session:
        visible = _job(session, case.team, project_id=case.a, author=case.user)
        hidden = _job(session, case.team, project_id=case.b)
        session.execute(
            text("UPDATE jobs SET payload_json=:raw WHERE id=:id"), {"raw": "{broken", "id": hidden}
        )
        assert _ids(session, read_only, cancel=True) == set()
        with pytest.raises(AccessDenied):
            require_job_access(session, read_only, visible, JobAction.CANCEL)
        with pytest.raises(AccessDenied):
            require_job_access(session, cancel_only, visible, JobAction.CANCEL)
        with pytest.raises(AccessDenied):
            job_visibility_clause(session, cancel_only)
        _hidden(session, context, hidden)
        _hidden(session, context, new_id())
        with pytest.raises(TypeError, match="access context"):
            job_visibility_clause(session, cast(AccessContext, None))
        for invalid_action in ("read", "run"):
            with pytest.raises(TypeError, match="job action"):
                require_job_access(session, context, visible, cast(JobAction, invalid_action))
        forged = AccessContext(
            replace(context.actor, scopes=frozenset({"app.read", "app.cancel", "app.run"}))
        )
        with pytest.raises(AccessDenied):
            job_visibility_clause(session, forged)


def test_job_payload_never_supplies_an_ownership_root_and_personal_membership_is_live(
    case: Any,
) -> None:
    workspace = case.personal[case.user]
    context = _context(case, case.user, workspaces=(workspace,))
    with case.database.session() as session:
        personal = _job(session, workspace)
        project = _job(session, case.team, project_id=case.b)
        session.execute(
            update(Job).where(Job.id == personal).values(payload_json={"project_id": case.b})
        )
        session.execute(
            update(Job).where(Job.id == project).values(payload_json={"workspace_id": workspace})
        )
        assert require_job_access(session, context, personal).id == personal
        _hidden(session, context, project)
        membership = session.get(WorkspaceMembership, (workspace, case.user))
        assert membership is not None and membership.role == "owner"
        session.execute(
            update(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == workspace,
                WorkspaceMembership.user_id == case.user,
            )
            .values(role="member"),
            execution_options={"synchronize_session": False},
        )
        assert membership.role == "owner"
        _hidden(session, context, personal)


@pytest.mark.parametrize("kind", ["maintenance.session_purge_preview", "maintenance.session_purge"])
def test_purge_jobs_require_retained_project_shape_and_keep_history(case: Any, kind: str) -> None:
    context = _context(case, case.owner, scopes=frozenset({"app.read"}), projects=(case.a,))
    manager = _context(case, case.owner, projects=(case.a,))
    personal = _context(case, case.user, workspaces=(case.personal[case.user],))
    with case.database.session() as session:
        member = SessionRecord(id=new_id(), project_id=case.a, name="Purge target")
        session.add(member)
        session.flush()
        valid = _job(session, case.team, kind=kind, project_id=case.a, author=case.user)
        rootless = _job(session, case.personal[case.user], kind=kind, author=case.user)
        session_root = _job(
            session, case.team, kind=kind, project_id=case.a, session_id=member.id, author=case.user
        )
        assert require_job_access(session, context, valid).id == valid
        with pytest.raises(AccessDenied):
            require_job_access(session, context, valid, JobAction.CANCEL)
        _hidden(session, personal, rootless)
        _hidden(session, manager, session_root)
        session.delete(member)
        session.flush()
        assert require_job_access(session, context, valid).id == valid
