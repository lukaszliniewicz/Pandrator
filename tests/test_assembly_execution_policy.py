"""Disposable header/authority proofs for direct and synchronous export assemblies."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy import delete, event, select, update

from pandrator.web.access.assembly_execution_policy import (
    AssemblyExecutionProof,
    require_assembly_execution,
    require_export_assembly_job,
)
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.auth_models import BrowserAuthSession
from pandrator.web.accounts.browser_sessions import BrowserSessionStore
from pandrator.web.accounts.execution_actor import (
    ExecutionActor,
    HostActor,
    UserActor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import (
    ApiToken,
    GenerationRun,
    Job,
    JobActorRecord,
    OutputAssembly,
    SessionRecord,
    WorkflowRun,
)
from tests.test_generation_execution_policy import (
    SCOPES,
    _lease,
    _lease_scope,
    _run,
    credential,
)
from tests.test_generation_execution_policy import case as case
from tests.test_generation_execution_policy import no_network as no_network
from tests.test_generation_execution_policy import template as template


def _assembly(case: Any, *, root: Any = None, selected: bool = True) -> str:
    root = root or case.a
    run_id = _run(case, root) if selected else None
    identifier = new_id()
    with case.database.immediate_session() as session:
        session.add(
            OutputAssembly(
                id=identifier,
                session_id=root.id,
                generation_run_id=run_id,
                settings_hash="assembly-v1",
                settings_json={"private": "assembly snapshot sentinel"},
            )
        )
    return identifier


def _job(
    case: Any,
    actor: ExecutionActor | None,
    *,
    kind: str = "generation.assemble",
    root: Any = None,
    selected: bool = True,
    stored: dict[str, Any] | None = None,
) -> tuple[str, Job, dict[str, Any]]:
    root = root or case.a
    assembly_id = _assembly(case, root=root, selected=selected)
    body = (
        stored
        if stored is not None
        else (
            {"output_assembly_id": assembly_id}
            if kind == "generation.assemble"
            else {
                "session_id": root.id,
                "target_stage": "export",
                "settings": {"private": "queued sentinel"},
            }
        )
    )
    workflow_id = None
    if kind == "workflow.continue":
        workflow_id = new_id()
        with case.database.immediate_session() as session:
            session.add(WorkflowRun(id=workflow_id, session_id=root.id, preset="custom"))
    with execution_actor_scope(actor):
        queued = case.services.jobs.enqueue(
            kind,
            body,
            session_id=root.id,
            project_id=root.project_id,
            workflow_run_id=workflow_id,
        )
    job = case.services.jobs.claim("assembly-policy-worker", lease_seconds=300)
    assert job is not None and job.id == queued.id
    with case.database.immediate_session() as session:
        session.execute(
            update(OutputAssembly).where(OutputAssembly.id == assembly_id).values(job_id=job.id)
        )
    return (
        assembly_id,
        job,
        {
            "output_assembly_id": assembly_id,
            "_job_id": job.id,
            "_lease_generation": job.lease_generation,
        },
    )


def _call(
    case: Any,
    actor: Any,
    job: Job | None,
    payload: Any,
    *,
    expected: Any = None,
    lease: Any = ...,
) -> AssemblyExecutionProof:
    lease = _lease(job) if lease is ... and job is not None else None if lease is ... else lease
    with execution_actor_scope(actor), _lease_scope(lease):
        with case.database.snapshot_session() as session:
            return require_assembly_execution(session, payload, expected=expected)


def _export(case: Any, actor: Any, job: Job | None, session_id: Any = None) -> str | None:
    with execution_actor_scope(actor), _lease_scope(_lease(job) if job else None):
        with case.database.snapshot_session() as session:
            return require_export_assembly_job(session, session_id or case.a.id)


def _change(case: Any, model: Any, identifier: str, **values: Any) -> None:
    with case.database.immediate_session() as session:
        session.execute(update(model).where(model.id == identifier).values(**values))


@pytest.mark.parametrize("selected", [True, False])
def test_member_direct_assembly_with_optional_selected_run(case: Any, selected: bool):
    actor = credential(case, "member").actor
    assembly_id, job, payload = _job(case, actor, selected=selected)
    proof = _call(case, actor, job, payload)
    assert proof.assembly_id == assembly_id and proof.session_id == case.a.id
    assert proof.project_id == case.a.project_id and proof.workspace_id == case.team
    assert (
        proof.assembly_job_id == proof.job_id == job.id and proof.job_kind == "generation.assemble"
    )
    assert (
        proof.generation_run_id is not None
    ) is selected and proof.settings_hash == "assembly-v1"
    assert _call(case, actor, job, payload, expected=proof) == proof
    with pytest.raises(FrozenInstanceError):
        cast(Any, proof).assembly_id = "changed"


@pytest.mark.parametrize("kind", ["export.variant", "workflow.continue"])
@pytest.mark.parametrize("injected", [True, False])
def test_member_synchronous_export_bridge_does_not_need_run_in_original_payload(
    case: Any, kind: str, injected: bool
):
    actor = credential(case, "member").actor
    assembly_id, job, payload = _job(case, actor, kind=kind)
    if not injected:
        payload = {"output_assembly_id": assembly_id}
    proof = _call(case, actor, job, payload)
    assert proof.job_id == job.id and proof.job_kind == kind
    assert _export(case, actor, job) == job.id


@pytest.mark.parametrize("kind", ["export.variant", "workflow.continue"])
def test_bridge_requires_selected_run_and_durable_assembly_job_link(case: Any, kind: str):
    actor = credential(case, "member").actor
    assembly_id, job, payload = _job(case, actor, kind=kind, selected=False)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)
    _change(case, OutputAssembly, assembly_id, generation_run_id=_run(case))
    _change(case, OutputAssembly, assembly_id, job_id=None)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


@pytest.mark.parametrize(
    "change", ["missing_job", "missing_generation", "wrong_job", "bool_generation", "settings"]
)
def test_direct_dispatch_shape_and_injected_values_are_strict(case: Any, change: str):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor)
    if change == "missing_job":
        del payload["_job_id"]
    elif change == "missing_generation":
        del payload["_lease_generation"]
    elif change == "wrong_job":
        payload["_job_id"] = "another-job"
    elif change == "bool_generation":
        payload["_lease_generation"] = True
    else:
        payload["settings"] = {"override": True}
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


@pytest.mark.parametrize("kind", ["export.variant", "workflow.continue"])
@pytest.mark.parametrize(
    "change", [{"_lease_generation": True}, {"settings": {}}, {"_job_id": "wrong"}]
)
def test_bridge_optional_fields_are_still_exact(case: Any, kind: str, change: dict[str, Any]):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor, kind=kind)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, {**payload, **change})


@pytest.mark.parametrize(
    "stored",
    [
        [],
        {"output_assembly_id": True},
        {"output_assembly_id": "different"},
        {"output_assembly_id": "different", "settings": {}},
    ],
)
def test_direct_stored_payload_is_exact(case: Any, stored: Any):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor)
    _change(case, Job, job.id, payload_json=stored)
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


@pytest.mark.parametrize("kind", ["export.variant", "workflow.continue"])
def test_bridge_stored_session_is_current_and_typed(case: Any, kind: str):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor, kind=kind)
    _change(case, Job, job.id, payload_json={"session_id": case.b.id})
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)
    with pytest.raises(AccessDenied):
        _export(case, actor, job)


@pytest.mark.parametrize(
    "corruption", ["foreign_run", "foreign_plan", "foreign_job", "trash", "archived"]
)
def test_current_roots_and_selected_run_plan_links_deny_corruption(case: Any, corruption: str):
    actor = credential(case, "member").actor
    assembly_id, job, payload = _job(case, actor)
    if corruption == "foreign_run":
        _change(case, OutputAssembly, assembly_id, generation_run_id=_run(case, case.b))
    elif corruption == "foreign_plan":
        foreign_run = _run(case, case.b)
        with case.database.snapshot_session() as session:
            revision_id = session.scalar(
                select(GenerationRun.plan_revision_id).where(GenerationRun.id == foreign_run)
            )
            selected_id = session.scalar(
                select(OutputAssembly.generation_run_id).where(OutputAssembly.id == assembly_id)
            )
        _change(case, GenerationRun, selected_id, plan_revision_id=revision_id)
    elif corruption == "foreign_job":
        _, foreign_job, _ = _job(case, actor, root=case.b)
        _change(case, OutputAssembly, assembly_id, job_id=foreign_job.id)
        job = foreign_job
        payload.update(_job_id=job.id, _lease_generation=job.lease_generation)
    elif corruption == "trash":
        _change(case, SessionRecord, case.a.id, trashed_at=utcnow())
    else:
        _change(case, Workspace, case.team, state="archived")
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)


@pytest.mark.parametrize("loss", ["membership", "account", "credential_scope", "resource_grant"])
def test_current_member_account_and_credential_authority_is_rechecked(case: Any, loss: str):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor, kind="export.variant")
    assert _call(case, actor, job, payload) and _export(case, actor, job) == job.id
    with case.database.immediate_session() as session:
        if loss == "membership":
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == actor.user_id,
                )
            )
        elif loss == "account":
            session.execute(update(User).where(User.id == actor.user_id).values(is_active=False))
        elif loss == "credential_scope":
            session.execute(
                update(ApiToken)
                .where(ApiToken.id == actor.token_id)
                .values(scopes_json=["app.read"])
            )
        else:
            session.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == actor.token_id
                )
            )
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload)
    with pytest.raises(AccessDenied):
        _export(case, actor, job)


@pytest.mark.parametrize(
    "problem", ["no_lease", "actor_mismatch", "owner", "generation", "expired", "record_missing"]
)
def test_actual_lease_and_captured_actor_are_required(case: Any, problem: str):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor)
    lease = _lease(job)
    if problem == "no_lease":
        lease = None
    elif problem == "actor_mismatch":
        actor = credential(case, "member").actor
    elif problem == "owner":
        lease["owner_id"] = "different-owner"
    elif problem == "generation":
        lease["lease_generation"] = True
    elif problem == "expired":
        _change(case, Job, job.id, lease_expires_at=utcnow() - timedelta(seconds=1))
    else:
        with case.database.immediate_session() as session:
            session.execute(delete(JobActorRecord).where(JobActorRecord.job_id == job.id))
    with pytest.raises(AccessDenied):
        _call(case, actor, job, payload, lease=lease)


@pytest.mark.parametrize("drift", ["selected_run", "assembly_job", "hash", "expected_type"])
def test_expected_proof_fences_assembly_retarget_before_body(case: Any, drift: str):
    actor = credential(case, "member").actor
    assembly_id, job, payload = _job(case, actor)
    proof: Any = _call(case, actor, job, payload)
    if drift == "selected_run":
        _change(case, OutputAssembly, assembly_id, generation_run_id=_run(case))
    elif drift == "assembly_job":
        _change(case, OutputAssembly, assembly_id, job_id=None)
    elif drift == "hash":
        _change(case, OutputAssembly, assembly_id, settings_hash="changed")
    else:
        proof = {"assembly_id": assembly_id}
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(_lease(job)):
        with case.database.snapshot_session() as session:
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            with pytest.raises(AccessDenied):
                require_assembly_execution(session, payload, expected=proof)
    assert not any(
        "payload_json" in statement
        or "settings_json" in statement
        or "settings_snapshot_json" in statement
        for statement in statements
    )


@pytest.mark.parametrize("state", ["logout", "expired"])
def test_admitted_browser_logout_and_expiry_preserve_assembly(case: Any, state: str):
    store = BrowserSessionStore(case.database)
    issued = store.issue(
        case.users["member"],
        1,
        subject="assembly browser",
        principal_kind="owner_session",
        scopes=SCOPES,
        idle_seconds=3600,
        absolute_seconds=7200,
    )
    actor = UserActor(
        user_id=case.users["member"],
        auth_revision=1,
        subject="assembly browser",
        principal_kind="owner_session",
        scopes=SCOPES,
        browser_session_id=issued.record_id,
    )
    _, job, payload = _job(case, actor, kind="export.variant")
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
    assert _export(case, actor, job) == job.id


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
def test_trusted_unleased_compatibility_does_not_read_queued_body(case: Any, identity: str):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Solo assembly", workspace_id=case.personal[case.users["owner"]]
        )
    )
    actor = HostActor("pandrator-cli") if identity == "host" else None
    assembly_id, job, _ = _job(case, actor, root=root)
    _change(case, Job, job.id, payload_json={"settings": {"different": True}})
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(None):
        with case.database.snapshot_session() as session:
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            proof = require_assembly_execution(session, {"output_assembly_id": assembly_id})
            assert require_export_assembly_job(session, root.id) is None
    assert proof.assembly_job_id == job.id and proof.job_id is None and proof.job_kind is None
    assert not any(
        "payload_json" in statement
        or "settings_json" in statement
        or "settings_snapshot_json" in statement
        for statement in statements
    )


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
def test_unleased_sound_settings_do_not_become_queued_payload_overrides(case: Any, identity: str):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Solo local sound assembly",
            workspace_id=case.personal[case.users["owner"]],
        )
    )
    actor = HostActor("pandrator-cli") if identity == "host" else None
    assembly_id, job, _ = _job(case, actor, root=root)
    payload = {"output_assembly_id": assembly_id, "settings": {"include_sounds": False}}
    assert _call(case, actor, None, payload).assembly_job_id == job.id
    with pytest.raises(AccessDenied):
        _call(
            case,
            actor,
            job,
            {**payload, "_job_id": job.id, "_lease_generation": job.lease_generation},
        )
    with pytest.raises(AccessDenied):
        _call(case, actor, None, {**payload, "_job_id": job.id})


def test_original_none_lease_requires_no_captured_actor_record(case: Any):
    root = case.services.sessions.create(
        "Original assembly identity",
        workspace_id=case.personal[case.users["owner"]],
    )
    assembly_id, job, payload = _job(case, HostActor("pandrator-cli"), root=root)
    with pytest.raises(AccessDenied):
        _call(case, None, job, payload)
    with case.database.immediate_session() as session:
        session.execute(delete(JobActorRecord).where(JobActorRecord.job_id == job.id))
    assert _call(case, None, job, payload).assembly_id == assembly_id


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
def test_trusted_leased_host_and_original_none(case: Any, identity: str):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Solo leased assembly", workspace_id=case.personal[case.users["owner"]]
        )
    )
    actor = HostActor("pandrator-cli") if identity == "host" else None
    _, job, payload = _job(case, actor, root=root, kind="export.variant")
    assert _call(case, actor, job, payload)
    assert _export(case, actor, job, root.id) == job.id


def test_legacy_none_cannot_borrow_a_team_root_or_leased_host_record(case: Any):
    host = HostActor("pandrator-cli")
    assembly_id, job, payload = _job(case, host)
    with pytest.raises(AccessDenied):
        _call(case, None, None, {"output_assembly_id": assembly_id})
    with pytest.raises(AccessDenied):
        _call(case, None, job, payload)
    with pytest.raises(AccessDenied):
        _export(case, None, None)


def test_unleased_referenced_user_job_is_none_only_restriction(case: Any):
    root = case.services.sessions.create(
        "Solo captured user", workspace_id=case.personal[case.users["owner"]]
    )
    actor = credential(case, "owner").actor
    assembly_id, _, _ = _job(case, actor, root=root)
    with pytest.raises(AccessDenied):
        _call(case, None, None, {"output_assembly_id": assembly_id})
    assert _call(case, HostActor("pandrator-cli"), None, {"output_assembly_id": assembly_id})
    assert _export(case, None, None, root.id) is None


def test_unleased_actor_record_must_encode_a_valid_identity(case: Any):
    host = HostActor("pandrator-cli")
    root = case.services.sessions.create(
        "Solo host record", workspace_id=case.personal[case.users["owner"]]
    )
    assembly_id, job, _ = _job(case, host, root=root)
    with case.database.immediate_session() as session:
        session.execute(
            update(JobActorRecord)
            .where(JobActorRecord.job_id == job.id)
            .values(context_json={"source": "host"})
        )
    for actor in (host, None):
        with pytest.raises(AccessDenied):
            _call(case, actor, None, {"output_assembly_id": assembly_id})


def test_selected_synthesis_job_is_not_assembly_execution_authority(case: Any):
    root = case.services.sessions.create(
        "Solo prior generation", workspace_id=case.personal[case.users["owner"]]
    )
    assembly_id = _assembly(case, root=root)
    with case.database.snapshot_session() as session:
        run_id = session.scalar(
            select(OutputAssembly.generation_run_id).where(OutputAssembly.id == assembly_id)
        )
    with execution_actor_scope(credential(case, "owner").actor):
        prior = case.services.jobs.enqueue(
            "generation.run",
            {"generation_run_id": run_id},
            session_id=root.id,
            project_id=root.project_id,
        )
    _change(case, GenerationRun, run_id, job_id=prior.id)
    assert _call(case, None, None, {"output_assembly_id": assembly_id})
    assert _export(case, None, None, root.id) is None


@pytest.mark.parametrize("actor_kind", ["viewer", "outsider", "unknown"])
def test_view_instance_admin_and_unknown_identity_do_not_confer_run(case: Any, actor_kind: str):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor)
    denied_actor = object() if actor_kind == "unknown" else credential(case, actor_kind).actor
    with pytest.raises(AccessDenied):
        _call(case, denied_actor, job, payload)


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"output_assembly_id": True}, {"output_assembly_id": " padded "}]
)
def test_malformed_assembly_payload_is_a_fixed_denial(case: Any, payload: Any):
    with pytest.raises(AccessDenied, match="^The requested action is not permitted\\.$"):
        _call(case, None, None, payload)


@pytest.mark.parametrize("denial", ["membership", "trash", "expired"])
def test_initial_denial_has_no_protected_body_reads_or_autoflush(case: Any, denial: str):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor, kind="export.variant")
    if denial == "membership":
        with case.database.immediate_session() as session:
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == actor.user_id,
                )
            )
    elif denial == "trash":
        _change(case, SessionRecord, case.a.id, trashed_at=utcnow())
    else:
        _change(case, Job, job.id, lease_expires_at=utcnow() - timedelta(seconds=1))
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(_lease(job)):
        with case.database.snapshot_session() as session:
            pending = session.get_one(User, actor.user_id)
            pending.display_name = "pending assembly denial"
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            with pytest.raises(AccessDenied):
                require_assembly_execution(session, payload)
            with pytest.raises(AccessDenied):
                require_export_assembly_job(session, case.a.id)
            assert pending in session.dirty and session.in_transaction()
    assert statements and all(
        statement.lstrip().upper().startswith("SELECT") for statement in statements
    )
    assert not any(
        "payload_json" in statement
        or "settings_json" in statement
        or "settings_snapshot_json" in statement
        for statement in statements
    )


def test_export_header_proof_precedes_assembly_and_run_reads_without_owning_transaction(case: Any):
    actor = credential(case, "member").actor
    _, job, payload = _job(case, actor, kind="workflow.continue")
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(_lease(job)):
        with case.database.snapshot_session() as session:
            pending = session.get_one(User, actor.user_id)
            pending.display_name = "pending export proof"
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            assert require_export_assembly_job(session, case.a.id) == job.id
            assert pending in session.dirty and session.in_transaction()
            assert not any(
                "generation_runs" in statement or "output_assemblies" in statement
                for statement in statements
            )
            statements.clear()
            assert require_assembly_execution(session, payload)
            assert pending in session.dirty and session.in_transaction()
    assert statements and all(
        statement.lstrip().upper().startswith("SELECT") for statement in statements
    )
    assert not any(
        "settings_json" in statement or "settings_snapshot_json" in statement
        for statement in statements
    )


def test_export_helper_denies_direct_dispatch_or_missing_user_lease(case: Any):
    actor = credential(case, "member").actor
    _, job, _ = _job(case, actor)
    with pytest.raises(AccessDenied):
        _export(case, actor, job)
    with pytest.raises(AccessDenied):
        _export(case, actor, None)
