"""Disposable current-authority and non-disclosure checks for generation execution."""

from __future__ import annotations

import shutil
import socket
from contextlib import contextmanager
from contextvars import Context
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest
import requests.sessions
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.generation_execution_policy import (
    GenerationExecutionProof,
    generation_child_scope,
    require_generation_execution,
)
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.browser_sessions import BrowserSessionStore
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import (
    ExecutionActor,
    HostActor,
    UserActor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
    BrowserAuthProjectGrant,
)
from pandrator.web.auth import AuthService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import (
    ApiToken,
    AutomationClient,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    Job,
    JobActorRecord,
    SessionRecord,
    WorkflowRun,
)
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.sessions import SessionService

SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})


@pytest.fixture(scope="module", autouse=True)
def no_network() -> Iterator[None]:
    attempts: list[str] = []
    patcher = pytest.MonkeyPatch()

    def blocked(*_args: Any, **_kwargs: Any) -> None:
        attempts.append("network")
        raise AssertionError("Generation policy tests must not use the network.")

    patcher.setattr(requests.sessions.Session, "request", blocked)
    patcher.setattr(socket.socket, "connect", blocked)
    try:
        yield
    finally:
        patcher.undo()
        assert not attempts


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Any:
    path = tmp_path_factory.mktemp("generation-policy-template") / "database.sqlite3"
    upgrade_database(path)
    database = Database(path)
    with database.immediate_session() as session:
        owner = session.scalar(select(User).where(User.legacy_subject == "owner"))
        assert owner is not None
        users = {"owner": owner.id}
        workspace = session.scalar(
            select(Workspace).where(Workspace.personal_owner_user_id == owner.id)
        )
        assert workspace is not None
        personal = {owner.id: workspace.id}
        for name in ("editor", "viewer", "member", "outsider"):
            user_id = new_id()
            users[name] = user_id
            session.add(
                User(
                    id=user_id,
                    display_name=name,
                    is_active=True,
                    is_instance_admin=name == "outsider",
                )
            )
            session.flush()
            session.add(UserCredential(user_id=user_id, login_name=user_id, auth_revision=1))
            workspace = Workspace(
                id=new_id(), kind="personal", name=name, personal_owner_user_id=user_id
            )
            session.add(workspace)
            session.flush()
            session.add(
                WorkspaceMembership(workspace_id=workspace.id, user_id=user_id, role="owner")
            )
            personal[user_id] = workspace.id
        team = Workspace(id=new_id(), kind="team", name="Generation policy team")
        session.add(team)
        session.flush()
        for name, role in (("owner", "owner"), ("member", "member")):
            session.add(WorkspaceMembership(workspace_id=team.id, user_id=users[name], role=role))
    sessions = SessionService(database)
    a = sessions.create("Generation project", workspace_id=team.id)
    b = sessions.create("Foreign shared project", workspace_id=team.id)
    with database.immediate_session() as session:
        for name, role in (("editor", "editor"), ("viewer", "viewer")):
            session.add(ProjectMembership(project_id=a.project_id, user_id=users[name], role=role))
    database.dispose()
    return SimpleNamespace(path=path, users=users, personal=personal, team=team.id, a=a, b=b)


@pytest.fixture
def case(tmp_path: Path, template: Any) -> Iterator[Any]:
    path = tmp_path / "database.sqlite3"
    shutil.copy2(template.path, path)
    database = Database(path)
    try:
        yield SimpleNamespace(
            database=database,
            auth=AuthService(database),
            users=template.users,
            personal=template.personal,
            team=template.team,
            a=template.a,
            b=template.b,
            services=SimpleNamespace(jobs=JobQueue(database), sessions=SessionService(database)),
        )
    finally:
        database.dispose()


def credential(case: Any, name: str, *, scopes: frozenset[str] = SCOPES) -> Any:
    token, _raw = case.auth.create_api_token(
        "Generation fixture token",
        user_id=case.users[name],
        auth_revision=1,
        scopes=scopes,
    )
    with case.database.immediate_session() as session:
        session.add(ApiTokenWorkspaceGrant(token_id=token.id, workspace_id=case.team))
    actor = UserActor(
        user_id=case.users[name],
        auth_revision=1,
        subject=token.subject,
        principal_kind=token.principal_kind,
        scopes=scopes,
        token_id=token.id,
    )
    return SimpleNamespace(actor=actor)


@contextmanager
def _lease_scope(value: object) -> Iterator[None]:
    token = current_job_lease.set(cast(Any, value))
    try:
        yield
    finally:
        current_job_lease.reset(token)


def _run(case: Any, root: Any = None) -> str:
    root = root or case.a
    with case.database.immediate_session() as session:
        plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == root.id))
        if plan is None:
            plan = GenerationPlan(id=new_id(), session_id=root.id)
            session.add(plan)
            session.flush()
            revision = GenerationPlanRevision(
                id=new_id(),
                plan_id=plan.id,
                revision_number=1,
                content_hash="plan-v1",
            )
            session.add(revision)
            session.flush()
            plan.active_revision_id = revision.id
        else:
            revision = session.get(GenerationPlanRevision, plan.active_revision_id)
            assert revision is not None
        run_id = new_id()
        sequence = 1 + (
            session.scalar(
                select(func.max(GenerationRun.sequence_number)).where(
                    GenerationRun.session_id == root.id,
                )
            )
            or 0
        )
        session.add(
            GenerationRun(
                id=run_id,
                session_id=root.id,
                plan_revision_id=revision.id,
                settings_hash="settings-v1",
                sequence_number=sequence,
                settings_snapshot_json={"private": "snapshot sentinel"},
            )
        )
    return run_id


def _job(
    case: Any,
    actor: ExecutionActor | None,
    *,
    root: Any = None,
    kind: str = "generation.run",
    stored: dict[str, Any] | None = None,
    workflow_session_id: str | None = None,
) -> tuple[str, Job, Job, dict[str, Any]]:
    root = root or case.a
    run_id = _run(case, root)
    payload = (
        stored
        if stored is not None
        else {
            "generation_run_id": run_id,
            "operation": "generate",
            "segment_ids": [],
        }
    )
    if kind in {"workflow.continue", "audiobook.generate_audio", "dubbing.generate_audio"}:
        payload = (
            stored
            if stored is not None
            else {
                "session_id": root.id,
                "target_stage": "export",
                "settings": {"private": "job sentinel"},
            }
        )
    workflow_id = None
    if workflow_session_id is not None:
        with case.database.immediate_session() as session:
            workflow_id = new_id()
            session.add(
                WorkflowRun(id=workflow_id, session_id=workflow_session_id, preset="custom")
            )
    with execution_actor_scope(actor):
        queued = case.services.jobs.enqueue(
            kind,
            payload,
            workspace_id=case.team if root == case.a or root == case.b else None,
            project_id=root.project_id,
            session_id=root.id,
            workflow_run_id=workflow_id,
        )
    claimed = case.services.jobs.claim("generation-policy-worker", lease_seconds=300)
    assert claimed is not None and claimed.id == queued.id
    with case.database.immediate_session() as session:
        session.execute(
            update(GenerationRun).where(GenerationRun.id == run_id).values(job_id=queued.id)
        )
    supplied = (
        dict(payload)
        if kind == "generation.run"
        else {"generation_run_id": run_id, "operation": "generate", "segment_ids": []}
    )
    supplied.update(_job_id=claimed.id, _lease_generation=claimed.lease_generation)
    return run_id, queued, claimed, supplied


def _lease(job: Job) -> dict[str, Any]:
    return {"job_id": job.id, "owner_id": job.lease_owner, "lease_generation": job.lease_generation}


def _call(
    case: Any,
    actor: ExecutionActor | None,
    job: Job | None,
    payload: Any,
    *,
    expected: Any = None,
    lease: Any = ...,
) -> GenerationExecutionProof:
    lease = _lease(job) if lease is ... and job is not None else None if lease is ... else lease
    with execution_actor_scope(actor), _lease_scope(lease):
        with case.database.snapshot_session() as session:
            return require_generation_execution(session, payload, expected=expected)


def _change(case: Any, model: Any, identifier: str, **values: Any) -> None:
    with case.database.immediate_session() as session:
        session.execute(update(model).where(model.id == identifier).values(**values))


def _child(case: Any, parent_id: str, job: Job, root: Any = None) -> str:
    root = root or case.a
    child_id = _run(case, root)
    with case.database.immediate_session() as session:
        plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == root.id))
        assert plan is not None
        revision_number = 1 + (
            session.scalar(
                select(func.max(GenerationPlanRevision.revision_number)).where(
                    GenerationPlanRevision.plan_id == plan.id,
                )
            )
            or 0
        )
        revision = GenerationPlanRevision(
            id=new_id(),
            plan_id=plan.id,
            revision_number=revision_number,
            content_hash="derived-plan",
        )
        session.add(revision)
        session.flush()
        session.execute(
            update(GenerationRun)
            .where(GenerationRun.id == child_id)
            .values(
                source_generation_run_id=parent_id,
                job_id=job.id,
                plan_revision_id=revision.id,
            )
        )
    return child_id


def _resume(child_id: str, job: Job | None = None) -> dict[str, Any]:
    payload = {"generation_run_id": child_id, "operation": "resume", "segment_ids": []}
    if job is not None:
        payload.update(_job_id=job.id, _lease_generation=job.lease_generation)
    return payload


@pytest.mark.parametrize("name", ["member", "owner"])
def test_current_member_and_owner_run(case: Any, name: str):
    actor = credential(case, name).actor
    run_id, _, job, payload = _job(case, actor)
    proof = _call(case, actor, job, payload)
    assert proof.run_id == run_id and proof.session_id == case.a.id
    assert proof.project_id == case.a.project_id and proof.workspace_id == case.team
    assert proof.job_id == job.id and proof.job_kind == "generation.run"
    assert proof.settings_hash == "settings-v1" and proof.legacy_host_access is False
    with pytest.raises(FrozenInstanceError):
        cast(Any, proof).run_id = "changed"


@pytest.mark.parametrize("name", ["viewer", "editor", "outsider"])
def test_view_and_instance_admin_do_not_confer_run(case: Any, name: str):
    actor = credential(case, name).actor
    _, _, job, payload = _job(case, actor)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


@pytest.mark.parametrize("loss", ["membership", "scope", "grant", "revoked", "disabled", "epoch"])
def test_current_authority_loss_denies(case: Any, loss: str):
    cred = credential(case, "member")
    actor = cred.actor
    _, _, job, payload = _job(case, actor)
    assert _call(case, actor, job, payload)
    with case.database.immediate_session() as session:
        if loss == "membership":
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == actor.user_id,
                )
            )
        elif loss == "scope":
            session.execute(
                update(ApiToken)
                .where(ApiToken.id == actor.token_id)
                .values(scopes_json=["app.read"])
            )
        elif loss == "grant":
            session.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == actor.token_id
                )
            )
        elif loss == "revoked":
            session.execute(
                update(ApiToken).where(ApiToken.id == actor.token_id).values(revoked_at=utcnow())
            )
        elif loss == "disabled":
            session.execute(update(User).where(User.id == actor.user_id).values(is_active=False))
        else:
            session.execute(
                update(UserCredential)
                .where(UserCredential.user_id == actor.user_id)
                .values(auth_revision=2)
            )
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


def test_foreign_shared_and_personal_roots(case: Any):
    actor = credential(case, "editor").actor
    _, _, job, payload = _job(case, actor, root=case.b)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)
    personal = case.services.sessions.create(
        "Foreign personal", workspace_id=case.personal[case.users["outsider"]]
    )
    case.services.jobs.complete(job.id, job.lease_owner, {}, lease_generation=job.lease_generation)
    _, _, personal_job, personal_payload = _job(case, actor, root=personal)
    with pytest.raises(AccessDenied):
        _call(case, actor, personal_job, personal_payload)


@pytest.mark.parametrize("problem", ["no_lease", "no_record", "other_actor", "bad_record"])
def test_captured_actor_and_lease_required(case: Any, problem: str):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    with case.database.immediate_session() as session:
        if problem == "no_record":
            session.execute(delete(JobActorRecord).where(JobActorRecord.job_id == job.id))
        elif problem == "bad_record":
            session.execute(
                update(JobActorRecord)
                .where(JobActorRecord.job_id == job.id)
                .values(context_json={"private": "do not leak"})
            )
    if problem == "other_actor":
        actor = credential(case, "editor").actor
    with pytest.raises(AccessDenied, match="^The requested action is not permitted\\.$"):
        _call(case, actor, job, payload, lease=None if problem == "no_lease" else _lease(job))


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "missing"),
        ("owner_id", "other"),
        ("lease_generation", True),
        ("lease_generation", 0),
        ("lease_generation", 2),
        ("extra", "bad"),
        ("job_id", " padded "),
        ("owner_id", None),
    ],
)
def test_malformed_and_mismatched_lease(case: Any, field: str, value: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload, lease={**_lease(job), field: value})


@pytest.mark.parametrize(
    "values",
    [
        {"status": "queued"},
        {"lease_owner": "other"},
        {"lease_generation": 2},
        {"lease_expires_at": utcnow() - timedelta(seconds=10)},
        {"lease_expires_at": None},
    ],
)
def test_inactive_lease_denies(case: Any, values: dict[str, Any]):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    _change(case, Job, job.id, **values)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("_job_id", None),
        ("_job_id", "other"),
        ("_job_id", " padded "),
        ("_lease_generation", True),
        ("_lease_generation", "1"),
        ("_lease_generation", 2),
    ],
)
def test_injected_values_are_exact(case: Any, field: str, value: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, {**payload, field: value})


@pytest.mark.parametrize("field", ["_job_id", "_lease_generation"])
def test_direct_dispatch_requires_both_injected_fields(case: Any, field: str):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    del payload[field]
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


@pytest.mark.parametrize(
    "change",
    [
        {"operation": "rvc"},
        {"segment_ids": ["changed"]},
        {"new": "field"},
        {"segment_ids": ()},
        {"segment_ids": [float("nan")]},
        {"segment_ids": [{1: "bad"}]},
    ],
)
def test_direct_payload_changes_and_non_json_values_deny(case: Any, change: dict[str, Any]):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, {**payload, **change})


def test_json_bool_integer_equivalence_is_not_authority(case: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    stored = {key: value for key, value in payload.items() if not key.startswith("_")}
    stored["controls"] = {"count": [1]}
    _change(case, Job, job.id, payload_json=stored)
    assert _call(case, actor, job, {**payload, "controls": {"count": [1]}})
    with pytest.raises(AccessDenied):
        _call(case, actor, job, {**payload, "controls": {"count": [True]}})


@pytest.mark.parametrize("stored", [[], None, {"generation_run_id": "changed"}])
def test_malformed_or_drifted_stored_payload(case: Any, stored: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    _change(case, Job, job.id, payload_json=stored)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


def test_invalid_stored_json_and_cyclic_caller_use_fixed_denial(case: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    cycle: list[Any] = []
    cycle.append(cycle)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, {**payload, "cycle": cycle})
    with case.database.immediate_session() as session:
        session.execute(
            text("UPDATE jobs SET payload_json = :value WHERE id = :id"),
            {
                "value": "private malformed body",
                "id": job.id,
            },
        )
    with pytest.raises(AccessDenied, match="^The requested action is not permitted\\.$"):
        _call(case, actor, job, payload)


@pytest.mark.parametrize(
    "kind", ["workflow.continue", "audiobook.generate_audio", "dubbing.generate_audio"]
)
@pytest.mark.parametrize("injected", [True, False])
def test_linked_bridge_accepts_only_internal_generate_request(case: Any, kind: str, injected: bool):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor, kind=kind, workflow_session_id=case.a.id)
    if not injected:
        payload.pop("_job_id")
        payload.pop("_lease_generation")
        payload.pop("segment_ids")
    proof = _call(case, actor, job, payload)
    assert proof.job_kind == kind


@pytest.mark.parametrize(
    "change",
    [
        {"operation": "rvc"},
        {"operation": None},
        {"segment_ids": ["segment"]},
        {"segment_ids": None},
        {"settings": {}},
        {"session_id": "ignored"},
        {"_lease_generation": True},
    ],
)
def test_bridge_cannot_smuggle_generation_overrides(case: Any, change: dict[str, Any]):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor, kind="workflow.continue")
    with pytest.raises(AccessDenied):
        _call(case, actor, job, {**payload, **change})


def test_bridge_requires_explicit_generate_operation(case: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor, kind="workflow.continue")
    del payload["operation"]
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


def test_bridge_stored_session_must_agree(case: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(
        case, actor, kind="workflow.continue", stored={"session_id": case.b.id}
    )
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


@pytest.mark.parametrize("drift", ["kind", "run_job", "plan"])
def test_current_header_linkage_drift_denies(case: Any, drift: str):
    actor = credential(case, "member").actor
    run_id, _, job, payload = _job(case, actor)
    if drift == "kind":
        _change(case, Job, job.id, kind="future.unrecognized")
    elif drift == "run_job":
        _change(case, GenerationRun, run_id, job_id=None)
    elif drift == "plan":
        other_id = _run(case, case.b)
        with case.database.snapshot_session() as session:
            revision_id = session.scalar(
                select(GenerationRun.plan_revision_id).where(GenerationRun.id == other_id)
            )
        _change(case, GenerationRun, run_id, plan_revision_id=revision_id)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


def test_current_constraints_prevent_rewriting_admitted_workflow_roots(case: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor, kind="workflow.continue", workflow_session_id=case.a.id)
    with pytest.raises(IntegrityError, match="job execution roots are immutable"):
        _change(case, Job, job.id, session_id=case.b.id, project_id=case.b.project_id)
    with pytest.raises(IntegrityError, match="job execution roots are immutable"):
        _change(case, Job, job.id, workflow_run_id=None)
    assert _call(case, actor, job, payload)


def test_current_constraint_prevents_foreign_job_attachment(case: Any):
    actor = credential(case, "member").actor
    run_id, _, job, payload = _job(case, actor)
    _, _, foreign_job, _ = _job(case, actor, root=case.b)
    with pytest.raises(IntegrityError, match="generation job must share its session scope"):
        _change(case, GenerationRun, run_id, job_id=foreign_job.id)
    with pytest.raises(AccessDenied):
        _call(
            case,
            actor,
            foreign_job,
            {
                **payload,
                "_job_id": foreign_job.id,
                "_lease_generation": foreign_job.lease_generation,
            },
        )
    assert _call(case, actor, job, payload)


def test_historical_plan_revision_remains_executable(case: Any):
    actor = credential(case, "member").actor
    run_id, _, job, payload = _job(case, actor)
    with case.database.immediate_session() as session:
        run = session.get(GenerationRun, run_id)
        old = session.get(GenerationPlanRevision, run.plan_revision_id)
        fresh = GenerationPlanRevision(
            id=new_id(), plan_id=old.plan_id, revision_number=2, content_hash="new"
        )
        session.add(fresh)
        session.flush()
        session.execute(
            update(GenerationPlan)
            .where(GenerationPlan.id == old.plan_id)
            .values(active_revision_id=fresh.id)
        )
    assert _call(case, actor, job, payload).plan_revision_id == old.id


@pytest.mark.parametrize("drift", ["hash", "expected_run", "expected_type", "trash", "archived"])
def test_expected_proof_and_active_root_fences(case: Any, drift: str):
    actor = credential(case, "member").actor
    run_id, _, job, payload = _job(case, actor)
    expected: Any = _call(case, actor, job, payload)
    if drift == "hash":
        _change(case, GenerationRun, run_id, settings_hash="changed")
    elif drift == "expected_run":
        expected = replace(expected, run_id="changed")
    elif drift == "expected_type":
        expected = {"run_id": run_id}
    elif drift == "trash":
        _change(case, SessionRecord, case.a.id, trashed_at=utcnow())
    else:
        _change(case, Workspace, case.team, state="archived")
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload, expected=expected)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"generation_run_id": ""},
        {"generation_run_id": " padded "},
        {"generation_run_id": True},
    ],
)
def test_invalid_input_is_a_fixed_denial(case: Any, payload: Any):
    with pytest.raises(AccessDenied, match="^The requested action is not permitted\\.$"):
        _call(case, None, None, payload)


@pytest.mark.parametrize("state", ["expired", "logout"])
def test_admitted_browser_expiry_and_logout_preserve_execution(case: Any, state: str):
    store = BrowserSessionStore(case.database)
    issued = store.issue(
        case.users["member"],
        1,
        subject="generation browser",
        principal_kind="owner_session",
        scopes=SCOPES,
        idle_seconds=3600,
        absolute_seconds=7200,
    )
    actor = UserActor(
        user_id=case.users["member"],
        auth_revision=1,
        subject="generation browser",
        principal_kind="owner_session",
        scopes=SCOPES,
        browser_session_id=issued.record_id,
    )
    _, _, job, payload = _job(case, actor)
    values = (
        {"revoked_at": utcnow()}
        if state == "logout"
        else {
            "expires_at": utcnow() - timedelta(seconds=1),
            "idle_expires_at": utcnow() - timedelta(seconds=1),
        }
    )
    _change(case, BrowserAuthSession, issued.record_id, **values)
    assert store.resolve(issued.raw_token) is None
    assert _call(case, actor, job, payload)


def test_api_natural_expiry_preserves_admitted_work(case: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    _change(case, ApiToken, actor.token_id, expires_at=utcnow() - timedelta(seconds=1))
    assert _call(case, actor, job, payload)


@pytest.mark.parametrize("scopes", [frozenset({"app.read"}), frozenset({"app.run"})])
def test_captured_scopes_must_include_read_and_run(case: Any, scopes: frozenset[str]):
    actor = credential(case, "member", scopes=scopes).actor
    _, _, job, payload = _job(case, actor)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


def test_captured_administrator_role_loss_denies(case: Any):
    actor = credential(case, "owner", scopes=SCOPES | {"app.admin"}).actor
    _, _, job, payload = _job(case, actor)
    assert _call(case, actor, job, payload)
    _change(case, User, actor.user_id, is_instance_admin=False)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


def _client_actor(case: Any, *, browser: bool = False) -> UserActor:
    client_id = new_id()
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Generation fixture client",
                subject=client_id,
                redirect_uris_json=["http://localhost/callback"],
                allowed_scopes_json=sorted(SCOPES),
                target_instance_id="synthetic",
                canonical_origin="http://localhost",
                created_by="generation-policy-test",
                user_id=case.users["member"],
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add(AutomationClientProjectGrant(client_id=client_id, project_id=case.a.project_id))
    resources = CredentialResources(project_ids=frozenset({case.a.project_id}))
    if browser:
        store = BrowserSessionStore(case.database)
        issued = store.issue(
            case.users["member"],
            1,
            subject="scoped browser",
            principal_kind="automation_client",
            scopes=SCOPES,
            client_id=client_id,
            resource_grants=resources,
            idle_seconds=3600,
            absolute_seconds=7200,
        )
        return UserActor(
            user_id=case.users["member"],
            auth_revision=1,
            subject="scoped browser",
            principal_kind="automation_client",
            scopes=SCOPES,
            browser_session_id=issued.record_id,
            client_id=client_id,
        )
    token, _raw = case.auth.create_api_token(
        "Scoped generation fixture",
        user_id=case.users["member"],
        auth_revision=1,
        scopes=SCOPES,
        client_id=client_id,
        resource_grants=resources,
    )
    return UserActor(
        user_id=case.users["member"],
        auth_revision=1,
        subject=token.subject,
        principal_kind=token.principal_kind,
        scopes=SCOPES,
        token_id=token.id,
        client_id=client_id,
    )


@pytest.mark.parametrize(
    "loss", ["client_revoked", "client_scope", "client_grant", "browser_grant"]
)
def test_client_and_browser_resource_loss_deny(case: Any, loss: str):
    actor = _client_actor(case, browser=loss == "browser_grant")
    _, _, job, payload = _job(case, actor)
    assert _call(case, actor, job, payload)
    with case.database.immediate_session() as session:
        if loss == "client_revoked":
            session.execute(
                update(AutomationClient)
                .where(AutomationClient.id == actor.client_id)
                .values(revoked_at=utcnow())
            )
        elif loss == "client_scope":
            session.execute(
                update(AutomationClient)
                .where(AutomationClient.id == actor.client_id)
                .values(allowed_scopes_json=["app.read"])
            )
        elif loss == "client_grant":
            session.execute(
                delete(AutomationClientProjectGrant).where(
                    AutomationClientProjectGrant.client_id == actor.client_id
                )
            )
        else:
            session.execute(
                delete(BrowserAuthProjectGrant).where(
                    BrowserAuthProjectGrant.browser_session_id == actor.browser_session_id
                )
            )
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


def test_trusted_host_leased_and_unleased_active_roots(case: Any):
    host = HostActor("pandrator-cli")
    run_id = _run(case)
    assert _call(case, host, None, {"generation_run_id": run_id}).legacy_host_access is False
    _, _, job, payload = _job(case, host)
    assert _call(case, host, job, payload)
    _change(case, Job, job.id, lease_expires_at=utcnow() - timedelta(seconds=1))
    with pytest.raises(AccessDenied):
        _call(case, host, job, payload)


@pytest.mark.parametrize("leased", [True, False])
def test_legacy_none_is_limited_to_original_namespace(case: Any, leased: bool):
    root = case.services.sessions.create(
        "Original solo root", workspace_id=case.personal[case.users["owner"]]
    )
    if leased:
        _, _, job, payload = _job(case, None, root=root)
    else:
        job, payload = None, {"generation_run_id": _run(case, root)}
    assert _call(case, None, job, payload).legacy_host_access is True
    foreign_run = _run(case)
    with pytest.raises(AccessDenied):
        _call(case, None, None, {"generation_run_id": foreign_run})
    personal = case.services.sessions.create(
        "Other personal root", workspace_id=case.personal[case.users["member"]]
    )
    with pytest.raises(AccessDenied):
        _call(case, None, None, {"generation_run_id": _run(case, personal)})


@pytest.mark.parametrize("actor_kind", ["user", "host", "malformed"])
def test_unleased_none_validates_captured_identity(case: Any, actor_kind: str):
    root = case.services.sessions.create(
        "Solo captured root", workspace_id=case.personal[case.users["owner"]]
    )
    actor = credential(case, "owner").actor if actor_kind == "user" else HostActor("pandrator-cli")
    _, _, job, payload = _job(case, actor, root=root)
    payload.pop("_job_id")
    payload.pop("_lease_generation")
    if actor_kind == "malformed":
        with case.database.immediate_session() as session:
            session.execute(
                update(JobActorRecord)
                .where(JobActorRecord.job_id == job.id)
                .values(context_json={"source": "host"})
            )
    if actor_kind == "host":
        assert _call(case, None, None, payload)
    else:
        with pytest.raises(AccessDenied):
            _call(case, None, None, payload)


def test_leased_none_requires_absent_actor_record_and_active_lease(case: Any):
    root = case.services.sessions.create(
        "Solo legacy job", workspace_id=case.personal[case.users["owner"]]
    )
    _, _, job, payload = _job(case, HostActor("pandrator-cli"), root=root)
    with pytest.raises(AccessDenied):
        _call(case, None, job, payload)
    with case.database.immediate_session() as session:
        session.execute(delete(JobActorRecord).where(JobActorRecord.job_id == job.id))
    assert _call(case, None, job, payload)
    _change(case, Job, job.id, lease_expires_at=utcnow() - timedelta(seconds=1))
    with pytest.raises(AccessDenied):
        _call(case, None, job, payload)


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
@pytest.mark.parametrize("local_controls", [False, True])
def test_unleased_local_invocation_does_not_read_dispatch_body(
    case: Any,
    identity: str,
    local_controls: bool,
):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Legacy local generation",
            workspace_id=case.personal[case.users["owner"]],
        )
    )
    host = HostActor("pandrator-cli")
    actor = host if identity == "host" else None
    run_id, _, job, _ = _job(case, host, root=root)
    payload: dict[str, Any] = {"generation_run_id": run_id}
    if local_controls:
        payload.update(operation="rvc", segment_ids=["local-segment"])
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(None):
        with case.database.snapshot_session() as session:
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            proof = require_generation_execution(session, payload)
            assert require_generation_execution(session, payload, expected=proof) == proof
    assert proof.job_id == job.id and proof.job_kind == "generation.run"
    assert proof.legacy_host_access is (identity == "legacy_none")
    assert statements and all(
        statement.lstrip().upper().startswith("SELECT") for statement in statements
    )
    assert not any(
        "payload_json" in statement or "settings_snapshot_json" in statement
        for statement in statements
    )


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
@pytest.mark.parametrize("denial", ["header", "root", "plan", "identity", "expected"])
def test_unleased_local_controls_preserve_authority_fences(
    case: Any,
    identity: str,
    denial: str,
):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Legacy local fences",
            workspace_id=case.personal[case.users["owner"]],
        )
    )
    host = HostActor("pandrator-cli")
    actor = host if identity == "host" else None
    run_id, _, job, _ = _job(case, host, root=root)
    payload = {"generation_run_id": run_id, "operation": "rvc", "segment_ids": ["local"]}
    expected = _call(case, actor, None, payload)
    if denial == "header":
        _change(case, Job, job.id, kind="unsupported.local")
    elif denial == "root":
        _change(case, SessionRecord, root.id, trashed_at=utcnow())
    elif denial == "plan":
        other_id = _run(case, case.b)
        with case.database.snapshot_session() as session:
            revision_id = session.scalar(
                select(GenerationRun.plan_revision_id).where(GenerationRun.id == other_id)
            )
        _change(case, GenerationRun, run_id, plan_revision_id=revision_id)
    elif denial == "identity":
        with case.database.immediate_session() as session:
            session.execute(
                update(JobActorRecord)
                .where(JobActorRecord.job_id == job.id)
                .values(
                    context_json={"version": 1, "source": "host", "label": "malformed-host"},
                )
            )
    else:
        _change(case, GenerationRun, run_id, settings_hash="local-drift")
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(None):
        with case.database.snapshot_session() as session:
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            with pytest.raises(AccessDenied):
                require_generation_execution(session, payload, expected=expected)
    assert statements
    assert not any(
        "payload_json" in statement or "settings_snapshot_json" in statement
        for statement in statements
    )


def test_unleased_local_controls_do_not_broaden_user_or_captured_user_authority(case: Any):
    root = case.services.sessions.create(
        "Captured solo user",
        workspace_id=case.personal[case.users["owner"]],
    )
    actor = credential(case, "owner").actor
    run_id, _, _, _ = _job(case, actor, root=root)
    payload = {"generation_run_id": run_id, "operation": "rvc", "segment_ids": ["local"]}
    with pytest.raises(AccessDenied):
        _call(case, actor, None, payload)
    with pytest.raises(AccessDenied):
        _call(case, None, None, payload)


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
@pytest.mark.parametrize("kind", ["generation.run", "workflow.continue"])
def test_local_controls_remain_forbidden_under_a_lease(case: Any, identity: str, kind: str):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Leased legacy strict dispatch",
            workspace_id=case.personal[case.users["owner"]],
        )
    )
    actor = HostActor("pandrator-cli") if identity == "host" else None
    _, _, job, payload = _job(case, actor, root=root, kind=kind)
    assert _call(case, actor, job, payload)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, {**payload, "operation": "rvc", "segment_ids": ["local"]})


@pytest.mark.parametrize("denial", ["role", "root", "lease", "expected"])
def test_denial_does_not_fetch_protected_bodies_or_autoflush(case: Any, denial: str):
    actor = credential(case, "viewer" if denial == "role" else "member").actor
    run_id, _, job, payload = _job(case, actor)
    if denial == "root":
        _change(case, SessionRecord, case.a.id, trashed_at=utcnow())
    if denial == "lease":
        _change(case, Job, job.id, lease_expires_at=utcnow() - timedelta(seconds=1))
    expected = (
        GenerationExecutionProof(
            run_id, "wrong", "wrong", "wrong", "wrong", None, None, None, False, False
        )
        if denial == "expected"
        else None
    )
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(_lease(job)):
        with case.database.snapshot_session() as session:
            dirty = session.get(User, actor.user_id)
            dirty.display_name = "pending mutation must not flush"
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            with pytest.raises(AccessDenied):
                require_generation_execution(session, payload, expected=expected)
            assert dirty in session.dirty
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert not any(
        "settings_snapshot_json" in statement or "payload_json" in statement
        for statement in statements
    )


def test_success_is_select_only_and_does_not_read_snapshot(case: Any):
    actor = credential(case, "member").actor
    _, _, job, payload = _job(case, actor)
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(_lease(job)):
        with case.database.snapshot_session() as session:
            dirty = session.get(User, actor.user_id)
            dirty.display_name = "pending on success"
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            proof = require_generation_execution(session, payload)
            assert require_generation_execution(session, payload, expected=proof) == proof
            assert dirty in session.dirty
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert any("payload_json" in statement for statement in statements)
    assert not any("settings_snapshot_json" in statement for statement in statements)


@pytest.mark.parametrize("kind", ["generation.run", "workflow.continue"])
@pytest.mark.parametrize("chained", [False, True])
def test_trusted_second_pass_reproves_direct_or_workflow_parent(
    case: Any, kind: str, chained: bool
):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor, kind=kind)
    parent = _call(case, actor, job, payload)
    child_id = _child(case, parent_id, job)
    if chained:
        child_id = _child(case, child_id, job)
    with generation_child_scope(parent, payload):
        proof = _call(case, actor, job, _resume(child_id, job if chained else None))
        assert proof.run_id == child_id and proof.plan_revision_id != parent.plan_revision_id
        assert proof.job_id == parent.job_id and proof.session_id == parent.session_id


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
def test_derived_host_and_original_namespace_none_keep_lease_rules(case: Any, identity: str):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Derived solo job",
            workspace_id=case.personal[case.users["owner"]],
        )
    )
    actor = HostActor("pandrator-cli") if identity == "host" else None
    parent_id, _, job, payload = _job(case, actor, root=root)
    parent = _call(case, actor, job, payload)
    child_id = _child(case, parent_id, job, root)
    with generation_child_scope(parent, payload):
        proof = _call(case, actor, job, _resume(child_id))
        assert proof.legacy_host_access is (identity == "legacy_none")
        _change(case, Job, job.id, lease_expires_at=utcnow() - timedelta(seconds=1))
        with pytest.raises(AccessDenied):
            _call(case, actor, job, _resume(child_id))


@pytest.mark.parametrize("injected", [False, True])
def test_source_link_without_trusted_scope_does_not_authorize_dispatch(case: Any, injected: bool):
    actor = credential(case, "member").actor
    parent_id, _, job, _ = _job(case, actor)
    child_id = _child(case, parent_id, job)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, _resume(child_id, job if injected else None))


@pytest.mark.parametrize(
    "change",
    [
        {"operation": "generate"},
        {"operation": None},
        {"segment_ids": ["segment"]},
        {"segment_ids": ()},
        {"unexpected": True},
        {"_job_id": "another-job"},
        {"_lease_generation": True},
        {"_lease_generation": 2},
    ],
)
def test_trusted_child_inner_request_is_exact(case: Any, change: dict[str, Any]):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor)
    parent = _call(case, actor, job, payload)
    child_id = _child(case, parent_id, job)
    with generation_child_scope(parent, payload), pytest.raises(AccessDenied):
        _call(case, actor, job, {**_resume(child_id), **change})


@pytest.mark.parametrize("field", ["operation", "segment_ids"])
def test_trusted_child_requires_both_resume_controls(case: Any, field: str):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor)
    parent = _call(case, actor, job, payload)
    child_id = _child(case, parent_id, job)
    inner = _resume(child_id)
    del inner[field]
    with generation_child_scope(parent, payload), pytest.raises(AccessDenied):
        _call(case, actor, job, inner)


@pytest.mark.parametrize(
    "lineage",
    [
        "non_descendant",
        "self_cycle",
        "two_node_cycle",
        "invalid_plan",
        "foreign_root",
        "foreign_job",
    ],
)
def test_derived_lineage_is_bounded_by_current_job_session_and_valid_plans(case: Any, lineage: str):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor)
    parent = _call(case, actor, job, payload)
    intermediate = _child(case, parent_id, job)
    child_id = _child(case, intermediate, job)
    if lineage == "non_descendant":
        _change(case, GenerationRun, intermediate, source_generation_run_id=None)
    elif lineage == "self_cycle":
        _change(case, GenerationRun, child_id, source_generation_run_id=child_id)
    elif lineage == "two_node_cycle":
        _change(case, GenerationRun, intermediate, source_generation_run_id=child_id)
    elif lineage == "invalid_plan":
        foreign_run = _run(case, case.b)
        with case.database.snapshot_session() as session:
            revision_id = session.scalar(
                select(GenerationRun.plan_revision_id).where(GenerationRun.id == foreign_run)
            )
        _change(case, GenerationRun, intermediate, plan_revision_id=revision_id)
    else:
        root = case.b if lineage == "foreign_root" else case.a
        with case.database.immediate_session() as session:
            foreign_job = Job(
                id=new_id(),
                kind="generation.run",
                workspace_id=case.team,
                project_id=root.project_id,
                session_id=root.id,
                payload_json={},
            )
            session.add(foreign_job)
        foreign_run = _child(case, parent_id, foreign_job, root)
        _change(case, GenerationRun, child_id, source_generation_run_id=foreign_run)
    statements: list[str] = []
    with (
        generation_child_scope(parent, payload),
        execution_actor_scope(actor),
        _lease_scope(_lease(job)),
    ):
        with case.database.snapshot_session() as session:
            connection = session.connection()
            raw = connection.connection.dbapi_connection
            assert raw is not None
            steps = 0

            def budget() -> int:
                nonlocal steps
                steps += 1
                return int(steps > 2000)

            raw.set_progress_handler(budget, 100)
            event.listen(
                connection,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            try:
                with pytest.raises(AccessDenied):
                    require_generation_execution(session, _resume(child_id))
            finally:
                raw.set_progress_handler(None, 0)
    cte = next(statement for statement in statements if "WITH RECURSIVE" in statement.upper())
    assert "UNION ALL" not in cte.upper() and "UNION" in cte.upper()
    assert not any(
        "settings_snapshot_json" in statement or "payload_json" in statement
        for statement in statements
    )


def test_scoped_parent_is_the_lineage_boundary_not_its_older_ancestry(case: Any):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor)
    parent = _call(case, actor, job, payload)
    child_id = _child(case, parent_id, job)
    _change(case, GenerationRun, parent_id, source_generation_run_id=child_id)
    with generation_child_scope(parent, payload):
        assert _call(case, actor, job, _resume(child_id)).run_id == child_id


def test_derived_execution_is_select_only_and_preserves_pending_work(case: Any):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor)
    parent = _call(case, actor, job, payload)
    child_id = _child(case, parent_id, job)
    statements: list[str] = []
    with (
        generation_child_scope(parent, payload),
        execution_actor_scope(actor),
        _lease_scope(_lease(job)),
    ):
        with case.database.snapshot_session() as session:
            pending = session.get_one(User, actor.user_id)
            pending.display_name = "derived pending work"
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            proof = require_generation_execution(session, _resume(child_id))
            assert require_generation_execution(session, _resume(child_id), expected=proof) == proof
            assert pending in session.dirty and session.in_transaction()
    assert statements and all(
        statement.lstrip().upper().startswith(("SELECT", "WITH RECURSIVE"))
        for statement in statements
    )
    assert any("payload_json" in statement for statement in statements)
    assert not any("settings_snapshot_json" in statement for statement in statements)


@pytest.mark.parametrize(
    "drift", ["payload", "parent_hash", "role", "lease", "trash", "child_hash"]
)
def test_derived_scope_reproves_current_parent_and_child_authority(case: Any, drift: str):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor)
    parent = _call(case, actor, job, payload)
    child_id = _child(case, parent_id, job)
    with generation_child_scope(parent, payload):
        child = _call(case, actor, job, _resume(child_id))
        if drift == "payload":
            _change(
                case,
                Job,
                job.id,
                payload_json={"generation_run_id": parent_id, "operation": "changed"},
            )
        elif drift == "parent_hash":
            _change(case, GenerationRun, parent_id, settings_hash="parent-drift")
        elif drift == "child_hash":
            _change(case, GenerationRun, child_id, settings_hash="child-drift")
        elif drift == "lease":
            _change(case, Job, job.id, lease_expires_at=utcnow() - timedelta(seconds=1))
        elif drift == "trash":
            _change(case, SessionRecord, case.a.id, trashed_at=utcnow())
        else:
            with case.database.immediate_session() as session:
                session.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == case.team,
                        WorkspaceMembership.user_id == actor.user_id,
                    )
                )
        statements: list[str] = []
        with execution_actor_scope(actor), _lease_scope(_lease(job)):
            with case.database.snapshot_session() as session:
                event.listen(
                    session.bind,
                    "before_cursor_execute",
                    lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
                )
                with pytest.raises(AccessDenied):
                    require_generation_execution(session, _resume(child_id), expected=child)
        assert not any("settings_snapshot_json" in statement for statement in statements)
        if drift != "payload":
            assert not any("payload_json" in statement for statement in statements)


def test_child_scope_deep_copies_parent_payload_and_restores_context(case: Any):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor)
    payload["controls"] = {"nested": [1]}
    _change(
        case,
        Job,
        job.id,
        payload_json={key: value for key, value in payload.items() if not key.startswith("_")},
    )
    parent = _call(case, actor, job, payload)
    child_id = _child(case, parent_id, job)
    with pytest.raises(RuntimeError, match="scope cleanup"):
        with generation_child_scope(parent, payload):
            payload["controls"]["nested"].append(2)
            payload["generation_run_id"] = child_id
            assert _call(case, actor, job, _resume(child_id)).run_id == child_id
            with pytest.raises(AccessDenied):
                Context().run(lambda: _call(case, actor, job, _resume(child_id, job)))
            raise RuntimeError("scope cleanup")
    with pytest.raises(AccessDenied):
        _call(case, actor, job, _resume(child_id, job))


@pytest.mark.parametrize(
    "malformed",
    ["proof_type", "proof_field", "payload_type", "payload_run", "payload_json", "payload_lease"],
)
def test_child_scope_rejects_malformed_inputs_without_becoming_a_grant(case: Any, malformed: str):
    actor = credential(case, "member").actor
    parent_id, _, job, payload = _job(case, actor)
    parent: Any = _call(case, actor, job, payload)
    if malformed == "proof_type":
        parent = {"run_id": parent_id}
    elif malformed == "proof_field":
        parent = replace(parent, legacy_host_access=1)
    elif malformed == "payload_type":
        payload = cast(Any, [])
    elif malformed == "payload_run":
        payload["generation_run_id"] = "another-run"
    elif malformed == "payload_json":
        payload["extra"] = {"tuple": (1,)}
    else:
        payload["_lease_generation"] = True
    with pytest.raises(AccessDenied), generation_child_scope(parent, payload):
        pytest.fail("Malformed scope entered")


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
def test_unleased_local_behavior_is_unchanged_inside_child_scope(case: Any, identity: str):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Unleased local scope",
            workspace_id=case.personal[case.users["owner"]],
        )
    )
    actor = HostActor("pandrator-cli") if identity == "host" else None
    parent_id = _run(case, root)
    payload = {"generation_run_id": parent_id}
    parent = _call(case, actor, None, payload)
    independent_id = _run(case, root)
    with generation_child_scope(parent, payload):
        assert _call(case, actor, None, _resume(independent_id)).run_id == independent_id
