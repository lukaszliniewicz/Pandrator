"""Capture-once delivery dispatch and header-only runtime authority checks."""

from __future__ import annotations

from contextvars import Context
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy import delete, event, update

from pandrator.web.access import delivery_execution_policy as delivery_policy
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.delivery_execution_policy import (
    DeliveryExecutionProof,
    capture_delivery_execution,
    delivery_child_scope,
    require_delivery_execution,
)
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
from pandrator.web.models import ApiToken, Job, JobActorRecord, SessionRecord, WorkflowRun
from tests.test_generation_execution_policy import SCOPES, _lease, _lease_scope, credential
from tests.test_generation_execution_policy import case as case
from tests.test_generation_execution_policy import no_network as no_network
from tests.test_generation_execution_policy import template as template

ENTRIES = {
    "export.create": "export",
    "export.variant": "export_variant",
    "workflow.continue": "continue_workflow",
}


def _job(
    case: Any,
    actor: ExecutionActor | None,
    *,
    kind: str = "export.create",
    root: Any = None,
) -> tuple[Job, dict[str, Any]]:
    root = root or case.a
    payload = {
        "session_id": root.id,
        "settings": {"format": "mp3", "quality": 1, "nested": [{"copy": True}]},
    }
    workflow_id = None
    if kind == "workflow.continue":
        workflow_id = new_id()
        with case.database.immediate_session() as session:
            session.add(WorkflowRun(id=workflow_id, session_id=root.id, preset="custom"))
    with execution_actor_scope(actor):
        queued = case.services.jobs.enqueue(
            kind,
            payload,
            session_id=root.id,
            project_id=root.project_id,
            workflow_run_id=workflow_id,
        )
    job = case.services.jobs.claim("delivery-policy-worker", lease_seconds=300)
    assert job is not None and job.id == queued.id
    return job, {**deepcopy(payload), "_job_id": job.id, "_lease_generation": job.lease_generation}


def _capture(
    case: Any,
    actor: Any,
    job: Job | None,
    payload: Any,
    *,
    entry: Any = "export",
    lease: Any = ...,
) -> DeliveryExecutionProof:
    lease = _lease(job) if lease is ... and job is not None else None if lease is ... else lease
    with execution_actor_scope(actor), _lease_scope(lease):
        with case.database.snapshot_session() as session:
            return capture_delivery_execution(session, payload, entry=entry)


def _require(case: Any, actor: Any, job: Job | None, proof: Any) -> None:
    with execution_actor_scope(actor), _lease_scope(_lease(job) if job else None):
        with case.database.snapshot_session() as session:
            require_delivery_execution(session, proof)


def _change(case: Any, model: Any, identifier: str, **values: Any) -> None:
    with case.database.immediate_session() as session:
        session.execute(update(model).where(model.id == identifier).values(**values))


@pytest.mark.parametrize("kind", list(ENTRIES))
def test_member_primary_entries_capture_and_revalidate(case: Any, kind: str):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor, kind=kind)
    proof = _capture(case, actor, job, payload, entry=ENTRIES[kind])
    assert proof == DeliveryExecutionProof(case.a.id, case.a.project_id, case.team, job.id, kind)
    assert not hasattr(proof, "__dict__")
    with pytest.raises(FrozenInstanceError):
        cast(Any, proof).session_id = "changed"
    _require(case, actor, job, proof)


@pytest.mark.parametrize(
    "kind,target", [("export.variant", "export"), ("workflow.continue", "export_variant")]
)
def test_trusted_inline_chain_has_no_second_payload_read(case: Any, kind: str, target: str):
    actor = credential(case, "member").actor
    job, original = _job(case, actor, kind=kind)
    proof = _capture(case, actor, job, original, entry=ENTRIES[kind])
    child = {
        **deepcopy(original),
        "pinned_assembly_artifact_id": "synthetic-assembly",
        "settings": {"stage": "derived"},
    }
    statements: list[str] = []
    with (
        execution_actor_scope(actor),
        _lease_scope(_lease(job)),
        delivery_child_scope(proof, cast(Any, target), child),
    ):
        with case.database.snapshot_session() as session:
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            assert capture_delivery_execution(session, child, entry=cast(Any, target)) == proof
            assert (
                sum(
                    statement.lstrip().startswith("SELECT sessions.id, projects.id")
                    for statement in statements
                )
                == 1
            )
            require_delivery_execution(session, proof)
    assert statements and not any("payload_json" in statement for statement in statements)
    with pytest.raises(AccessDenied):
        _capture(case, actor, job, child, entry=target)


@pytest.mark.parametrize("entry", ["export", "export_variant", "continue_workflow", "unknown"])
def test_primary_entry_and_job_kind_must_agree_without_scope(case: Any, entry: str):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)
    if entry == "export":
        assert _capture(case, actor, job, payload, entry=entry)
    else:
        with pytest.raises(AccessDenied):
            _capture(case, actor, job, payload, entry=entry)


@pytest.mark.parametrize(
    "change",
    [
        "missing_both",
        "missing_job",
        "missing_generation",
        "wrong_job",
        "wrong_generation",
        "bool_generation",
    ],
)
def test_initial_dispatch_injected_values_are_exact(case: Any, change: str):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)
    if change == "missing_both":
        del payload["_job_id"]
        del payload["_lease_generation"]
    elif change == "missing_job":
        del payload["_job_id"]
    elif change == "missing_generation":
        del payload["_lease_generation"]
    elif change == "wrong_job":
        payload["_job_id"] = "another-job"
    else:
        payload["_lease_generation"] = True if change == "bool_generation" else 2
    with pytest.raises(AccessDenied):
        _capture(case, actor, job, payload)


@pytest.mark.parametrize(
    "value", [True, "1", float("nan"), (1,), {1: "not a string key"}, object()]
)
def test_initial_dispatch_strict_json_and_payload_equality(case: Any, value: Any):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)
    payload["settings"]["quality"] = value
    with pytest.raises(AccessDenied):
        _capture(case, actor, job, payload)


def test_object_order_is_irrelevant_but_added_fields_are_not(case: Any):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)
    payload["settings"] = dict(reversed(list(payload["settings"].items())))
    assert _capture(case, actor, job, payload)
    with pytest.raises(AccessDenied):
        _capture(case, actor, job, {**payload, "new": "field"})


@pytest.mark.parametrize(
    "denial",
    [
        "wrong_root",
        "actor_mismatch",
        "view_only",
        "unsupported_job",
        "no_lease",
        "trash",
        "archived",
    ],
)
def test_initial_denial_reads_no_protected_body(case: Any, denial: str):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)
    lease = _lease(job)
    if denial == "wrong_root":
        payload["session_id"] = case.b.id
    elif denial == "actor_mismatch":
        actor = credential(case, "member").actor
    elif denial == "view_only":
        with case.database.immediate_session() as session:
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == actor.user_id,
                )
            )
    elif denial == "unsupported_job":
        _change(case, Job, job.id, kind="generation.assemble")
    elif denial == "no_lease":
        lease = None
    elif denial == "trash":
        _change(case, SessionRecord, case.a.id, trashed_at=utcnow())
    else:
        _change(case, Workspace, case.team, state="archived")
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(lease):
        with case.database.snapshot_session() as session:
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            with pytest.raises(AccessDenied):
                capture_delivery_execution(session, payload, entry="export")
    assert not any(
        "payload_json" in statement or "settings_json" in statement for statement in statements
    )


@pytest.mark.parametrize(
    "loss", ["membership", "disabled", "scope", "resource", "expired", "owner", "generation"]
)
def test_runtime_revalidation_rejects_current_authority_loss(case: Any, loss: str):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)
    proof = _capture(case, actor, job, payload)
    with case.database.immediate_session() as session:
        if loss == "membership":
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == actor.user_id,
                )
            )
        elif loss == "disabled":
            session.execute(update(User).where(User.id == actor.user_id).values(is_active=False))
        elif loss == "scope":
            session.execute(
                update(ApiToken)
                .where(ApiToken.id == actor.token_id)
                .values(scopes_json=["app.read"])
            )
        elif loss == "resource":
            session.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == actor.token_id
                )
            )
        elif loss == "expired":
            session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )
        elif loss == "owner":
            session.execute(update(Job).where(Job.id == job.id).values(lease_owner="changed-owner"))
        else:
            session.execute(update(Job).where(Job.id == job.id).values(lease_generation=2))
    with pytest.raises(AccessDenied):
        _require(case, actor, job, proof)


def test_capture_reads_queue_once_and_runtime_gates_are_header_only_no_autoflush(case: Any):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)
    statements: list[str] = []
    with execution_actor_scope(actor), _lease_scope(_lease(job)):
        with case.database.snapshot_session() as session:
            pending = session.get_one(User, actor.user_id)
            pending.display_name = "pending delivery edit"
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            proof = capture_delivery_execution(session, payload, entry="export")
            assert sum("payload_json" in statement for statement in statements) == 1
            statements.clear()
            require_delivery_execution(session, proof)
            require_delivery_execution(session, proof)
            assert pending in session.dirty and session.in_transaction()
    assert statements and all(
        statement.lstrip().upper().startswith("SELECT") for statement in statements
    )
    assert not any(
        "payload_json" in statement or "settings_json" in statement for statement in statements
    )


@pytest.mark.parametrize("state", ["logout", "expired"])
def test_admitted_browser_delivery_survives_logout_and_natural_expiry(case: Any, state: str):
    store = BrowserSessionStore(case.database)
    issued = store.issue(
        case.users["member"],
        1,
        subject="delivery browser",
        principal_kind="owner_session",
        scopes=SCOPES,
        idle_seconds=3600,
        absolute_seconds=7200,
    )
    actor = UserActor(
        user_id=case.users["member"],
        auth_revision=1,
        subject="delivery browser",
        principal_kind="owner_session",
        scopes=SCOPES,
        browser_session_id=issued.record_id,
    )
    job, payload = _job(case, actor)
    proof = _capture(case, actor, job, payload)
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
    _require(case, actor, job, proof)
    assert _capture(case, actor, job, payload) == proof


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
@pytest.mark.parametrize("leased", [True, False])
def test_original_none_and_valid_host_compatibility(case: Any, identity: str, leased: bool):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Original delivery", workspace_id=case.personal[case.users["owner"]]
        )
    )
    actor = HostActor("pandrator-cli") if identity == "host" else None
    if leased:
        job, payload = _job(case, actor, root=root)
    else:
        job = None
        payload = {"session_id": root.id, "settings": {"local": True}}
    proof = _capture(case, actor, job, payload)
    _require(case, actor, job, proof)
    if not leased:
        with pytest.raises(AccessDenied):
            _capture(case, actor, None, {**payload, "_job_id": "claimed"})
        with (
            execution_actor_scope(actor),
            _lease_scope(None),
            delivery_child_scope(proof, "export_variant", payload),
        ):
            assert _capture(case, actor, None, payload, entry="export_variant") == proof


@pytest.mark.parametrize("identity", ["host", "legacy_none"])
def test_unleased_opaque_settings_yield_without_sealing(
    case: Any,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
):
    root = (
        case.a
        if identity == "host"
        else case.services.sessions.create(
            "Original opaque delivery",
            workspace_id=case.personal[case.users["owner"]],
        )
    )
    actor = HostActor("pandrator-cli") if identity == "host" else None
    payload = {"session_id": root.id, "settings": {"opaque_legacy_value": object()}}
    proof = _capture(case, actor, None, payload)
    digest_calls: list[str] = []

    def forbidden_digest(_payload: Any) -> str:
        digest_calls.append("called")
        raise AssertionError("Unleased legacy settings must not be hashed or serialized")

    monkeypatch.setattr(delivery_policy, "_digest", forbidden_digest)
    with (
        execution_actor_scope(actor),
        _lease_scope(None),
        delivery_child_scope(proof, "export", payload),
    ):
        assert _capture(case, actor, None, payload) == proof
        _require(case, actor, None, proof)
    assert digest_calls == []


def test_none_cannot_borrow_team_or_captured_original_host_job(case: Any):
    with pytest.raises(AccessDenied):
        _capture(case, None, None, {"session_id": case.a.id})
    root = case.services.sessions.create(
        "Captured delivery", workspace_id=case.personal[case.users["owner"]]
    )
    job, payload = _job(case, HostActor("pandrator-cli"), root=root)
    with pytest.raises(AccessDenied):
        _capture(case, None, job, payload)
    with case.database.immediate_session() as session:
        session.execute(delete(JobActorRecord).where(JobActorRecord.job_id == job.id))
    assert _capture(case, None, job, payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", " padded "),
        ("project_id", "different"),
        ("job_id", None),
        ("job_kind", None),
        ("job_kind", "unknown"),
    ],
)
def test_runtime_proof_shape_and_header_retarget_deny(case: Any, field: str, value: Any):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor)
    proof = _capture(case, actor, job, payload)
    with pytest.raises(AccessDenied):
        _require(case, actor, job, replace(proof, **{field: value}))
    with pytest.raises(AccessDenied):
        _require(case, actor, job, {"session_id": case.a.id})


def test_sealed_child_ignores_argument_reference_mutation_but_denies_changed_payload(case: Any):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor, kind="export.variant")
    proof = _capture(case, actor, job, payload, entry="export_variant")
    child = {**deepcopy(payload), "pinned_assembly_artifact_id": "assembly-pin"}
    sealed_copy = deepcopy(child)
    with (
        execution_actor_scope(actor),
        _lease_scope(_lease(job)),
        delivery_child_scope(proof, "export", child),
    ):
        child["settings"]["quality"] = 2
        child["pinned_assembly_artifact_id"] = "changed-pin"
        with pytest.raises(AccessDenied):
            _capture(case, actor, job, child)
        assert _capture(case, actor, job, sealed_copy) == proof


@pytest.mark.parametrize(
    "failure",
    ["root", "missing_pair", "partial_pair", "wrong_job", "bool_generation", "entry", "non_json"],
)
def test_child_scope_rejects_malformed_or_retargeted_handoff(case: Any, failure: str):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor, kind="workflow.continue")
    proof = _capture(case, actor, job, payload, entry="continue_workflow")
    child = deepcopy(payload)
    entry = "export_variant"
    if failure == "root":
        child["session_id"] = case.b.id
    elif failure == "missing_pair":
        del child["_job_id"]
        del child["_lease_generation"]
    elif failure == "partial_pair":
        del child["_lease_generation"]
    elif failure == "wrong_job":
        child["_job_id"] = "different-job"
    elif failure == "bool_generation":
        child["_lease_generation"] = True
    elif failure == "entry":
        entry = "continue_workflow"
    else:
        child["settings"]["object"] = object()
    with (
        execution_actor_scope(actor),
        _lease_scope(_lease(job)),
        pytest.raises(AccessDenied),
        delivery_child_scope(proof, cast(Any, entry), child),
    ):
        pytest.fail("Malformed inline handoff entered")


@pytest.mark.parametrize("raise_inside", [False, True])
def test_continue_variant_export_nested_scopes_restore_and_do_not_leak(
    case: Any, raise_inside: bool
):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor, kind="workflow.continue")
    proof = _capture(case, actor, job, payload, entry="continue_workflow")
    variant = {**deepcopy(payload), "settings": {"stage": "variant"}}
    exported = {
        **deepcopy(payload),
        "pinned_assembly_artifact_id": "assembly-pin",
        "settings": {"stage": "export"},
    }
    with (
        execution_actor_scope(actor),
        _lease_scope(_lease(job)),
        delivery_child_scope(proof, "export_variant", variant),
    ):
        assert _capture(case, actor, job, variant, entry="export_variant") == proof
        try:
            with delivery_child_scope(proof, "export", exported):
                assert _capture(case, actor, job, exported) == proof
                with pytest.raises(AccessDenied):
                    _capture(case, actor, job, variant, entry="export_variant")
                if raise_inside:
                    raise RuntimeError("scope cleanup")
        except RuntimeError as error:
            assert raise_inside and str(error) == "scope cleanup"
        assert _capture(case, actor, job, variant, entry="export_variant") == proof
        with pytest.raises(AccessDenied):
            _capture(case, actor, job, exported)
        with pytest.raises(AccessDenied):
            Context().run(lambda: _capture(case, actor, job, variant, entry="export_variant"))
    with pytest.raises(AccessDenied):
        _capture(case, actor, job, variant, entry="export_variant")


def test_scope_does_not_authorize_a_different_job_or_current_role_loss(case: Any):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor, kind="export.variant")
    proof = _capture(case, actor, job, payload, entry="export_variant")
    with (
        execution_actor_scope(actor),
        _lease_scope({**_lease(job), "job_id": "other-job"}),
        pytest.raises(AccessDenied),
        delivery_child_scope(proof, "export", payload),
    ):
        pytest.fail("Wrong-job scope entered")
    with (
        execution_actor_scope(actor),
        _lease_scope(_lease(job)),
        delivery_child_scope(proof, "export", payload),
    ):
        with case.database.immediate_session() as session:
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == actor.user_id,
                )
            )
        with pytest.raises(AccessDenied):
            _capture(case, actor, job, payload)
        with pytest.raises(AccessDenied):
            _require(case, actor, job, proof)


def test_matching_child_scope_cannot_fall_back_to_primary_after_job_kind_drift(case: Any):
    actor = credential(case, "member").actor
    job, payload = _job(case, actor, kind="export.variant")
    proof = _capture(case, actor, job, payload, entry="export_variant")
    statements: list[str] = []
    with (
        execution_actor_scope(actor),
        _lease_scope(_lease(job)),
        delivery_child_scope(proof, "export", payload),
    ):
        _change(case, Job, job.id, kind="export.create")
        with case.database.snapshot_session() as session:
            event.listen(
                session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
            )
            with pytest.raises(AccessDenied):
                capture_delivery_execution(session, payload, entry="export")
    assert not any("payload_json" in statement for statement in statements)


@pytest.mark.parametrize(
    "payload,entry",
    [
        (None, "export"),
        ([], "export"),
        ({}, "export"),
        ({"session_id": True}, "export"),
        ({"session_id": " padded "}, "export"),
        ({"session_id": "valid"}, None),
    ],
)
def test_invalid_capture_inputs_use_fixed_denial(case: Any, payload: Any, entry: Any):
    with pytest.raises(AccessDenied, match="^The requested action is not permitted\\.$"):
        _capture(case, None, None, payload, entry=entry)
