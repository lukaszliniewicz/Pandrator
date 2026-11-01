"""Inline source-cleaning/preparation authority bound to a queued workflow job."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.delivery_execution_policy import (
    DeliveryExecutionProof,
    capture_delivery_execution,
)
from pandrator.web.access.source_preparation_policy import (
    require_source_cleaning_access,
    require_text_preparation_access,
    source_processing_child_scope,
)
from pandrator.web.accounts.execution_actor import HostActor, UserActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.job_execution import current_job_lease
from pandrator.web.models import Artifact, Job
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library
from tests.test_source_preparation_workspace_access import (
    _actor,
    _leased_actor,
    _project,
)


def _input(f: Any, target: dict[str, Any]):
    path = f.services.paths.uploads / f"source-processing-child-{uuid4()}.txt"
    path.write_text("Source-processing child input.", encoding="utf-8")
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="source",
            role="upload",
            session_id=target["id"],
        )
    return artifact


def _workflow_parent(f: Any, target: dict[str, Any], actor: UserActor):
    stored_payload = {
        "session_id": target["id"],
        "target_stage": "generate_audio",
    }
    with execution_actor_scope(actor):
        queued = f.services.jobs.enqueue(
            "workflow.continue",
            stored_payload,
            session_id=target["id"],
            project_id=target["project_id"],
        )
        job = f.services.jobs.claim(
            "source-processing-child-worker",
            lease_seconds=900,
        )
    assert job is not None and job.id == queued.id
    worker_payload = {
        **stored_payload,
        "_job_id": job.id,
        "_lease_generation": job.lease_generation,
    }
    with _leased_actor(actor, job):
        with f.case.database.snapshot_session() as unit:
            proof = capture_delivery_execution(
                unit,
                worker_payload,
                entry="continue_workflow",
            )
    return job, proof


def _child_payload(target: dict[str, Any], source: Artifact, job: Job) -> dict[str, Any]:
    return {
        "session_id": target["id"],
        "source_artifact_id": source.id,
        "_job_id": job.id,
        "_lease_generation": job.lease_generation,
    }


def _require_child(
    unit: Any,
    job_kind: str,
    session_id: str,
    source_artifact_id: str,
) -> bool | None:
    if job_kind == "source.clean":
        require_source_cleaning_access(unit, session_id, source_artifact_id)
        return None
    return require_text_preparation_access(unit, session_id, source_artifact_id)


@pytest.mark.parametrize("job_kind", ["source.clean", "text.prepare"])
def test_real_workflow_continue_proof_allows_matching_inline_source_child(
    library: Any,
    job_kind: str,
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    job, proof = _workflow_parent(f, target, actor)
    child_payload = _child_payload(target, source, job)

    assert proof.job_id == job.id
    assert proof.job_kind == "workflow.continue"
    with _leased_actor(actor, job):
        with source_processing_child_scope(proof, job_kind, child_payload):
            with f.case.database.snapshot_session() as unit:
                assert _require_child(unit, job_kind, target["id"], source.id) is (
                    None if job_kind == "source.clean" else True
                )


@pytest.mark.parametrize("job_kind", ["source.clean", "text.prepare"])
def test_unscoped_child_does_not_inherit_parent_kind_lease(
    library: Any,
    job_kind: str,
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    job, _proof = _workflow_parent(f, target, actor)

    # Root RUN and ordinary input access are valid; the real parent lease alone
    # must not masquerade as a standalone source.clean/text.prepare job.
    with execution_actor_scope(actor), f.case.database.snapshot_session() as unit:
        assert _require_child(unit, job_kind, target["id"], source.id) is (
            None if job_kind == "source.clean" else True
        )
    with _leased_actor(actor, job), f.case.database.snapshot_session() as unit:
        with pytest.raises(AccessDenied):
            _require_child(unit, job_kind, target["id"], source.id)


@pytest.mark.parametrize("change", ["kind", "session", "source", "job", "generation"])
def test_child_scope_rejects_wrong_kind_or_binding(
    library: Any,
    change: str,
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    other_target = _project(u.alice, u.ah, u.workspace_id)
    source = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    job, proof = _workflow_parent(f, target, actor)
    payload = _child_payload(target, source, job)
    child_kind = "source.clean"
    if change == "kind":
        child_kind = "workflow.continue"
    elif change == "session":
        payload["session_id"] = other_target["id"]
    elif change == "source":
        payload["source_artifact_id"] = str(uuid4())
    elif change == "job":
        payload["_job_id"] = str(uuid4())
    else:
        payload["_lease_generation"] = job.lease_generation + 1

    with _leased_actor(actor, job):
        if change == "source":
            with source_processing_child_scope(proof, child_kind, payload):
                with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                    require_source_cleaning_access(unit, target["id"], source.id)
        else:
            with pytest.raises(AccessDenied):
                with source_processing_child_scope(proof, child_kind, payload):
                    pass


def test_scope_resets_after_normal_and_exceptional_exit(library: Any):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    job, proof = _workflow_parent(f, target, actor)
    payload = _child_payload(target, source, job)

    with _leased_actor(actor, job):
        with source_processing_child_scope(proof, "source.clean", payload):
            with f.case.database.snapshot_session() as unit:
                require_source_cleaning_access(unit, target["id"], source.id)
        with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
            require_source_cleaning_access(unit, target["id"], source.id)

        with pytest.raises(RuntimeError, match="synthetic child failure"):
            with source_processing_child_scope(proof, "source.clean", payload):
                raise RuntimeError("synthetic child failure")
        with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
            require_source_cleaning_access(unit, target["id"], source.id)


def test_parent_authority_is_rechecked_after_workspace_membership_loss(library: Any):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    job, proof = _workflow_parent(f, target, actor)
    payload = _child_payload(target, source, job)

    with _leased_actor(actor, job):
        with source_processing_child_scope(proof, "text.prepare", payload):
            with f.case.database.snapshot_session() as unit:
                assert require_text_preparation_access(unit, target["id"], source.id) is True
            with f.case.database.immediate_session() as unit:
                unit.delete(
                    unit.get_one(
                        WorkspaceMembership,
                        (u.workspace_id, u.alice_id),
                    )
                )
            with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                require_text_preparation_access(unit, target["id"], source.id)


@pytest.mark.parametrize("stale", [False, True])
def test_fake_or_stale_lease_cannot_authorize_child_frame(library: Any, stale: bool):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source = _input(f, target)
    actor = _actor(f.case, u.alice_id, workspaces=(u.workspace_id,))
    job, proof = _workflow_parent(f, target, actor)
    payload = _child_payload(target, source, job)

    with _leased_actor(actor, job):
        if not stale:
            lease_token = current_job_lease.set(
                {
                    "job_id": str(uuid4()),
                    "owner_id": "source-processing-child-worker",
                    "lease_generation": job.lease_generation,
                }
            )
            try:
                with pytest.raises(AccessDenied):
                    with source_processing_child_scope(proof, "source.clean", payload):
                        pass
            finally:
                current_job_lease.reset(lease_token)
        else:
            with source_processing_child_scope(proof, "source.clean", payload):
                with f.case.database.immediate_session() as unit:
                    unit.get_one(Job, job.id).status = "failed"
                with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                    require_source_cleaning_access(unit, target["id"], source.id)


@pytest.mark.parametrize("leased", [False, True])
def test_trusted_none_policy_and_no_lease_scope_add_no_sql(library: Any, leased: bool):
    unit = Mock()
    proof = DeliveryExecutionProof("session", "project", "workspace", None, None)
    payload = {
        "session_id": "session",
        "source_artifact_id": "source",
    }
    lease_token = current_job_lease.set(
        {"job_id": "internal-job", "owner_id": "internal-worker", "lease_generation": 1}
        if leased
        else None
    )
    try:
        with execution_actor_scope(None):
            assert require_text_preparation_access(unit, "ignored", "ignored") is True
            require_source_cleaning_access(unit, "ignored", "ignored", agent_run_id="ignored")
            with source_processing_child_scope(proof, "source.clean", payload):
                pass
            with pytest.raises(AccessDenied):
                with source_processing_child_scope(proof, "workflow.continue", payload):
                    pass
    finally:
        current_job_lease.reset(lease_token)
    assert unit.mock_calls == []
