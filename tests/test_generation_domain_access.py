"""Direct generation-domain access checks on the execution-policy fixture."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import delete, event, select

from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import (
    HostActor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.database import Database
from pandrator.web.generation_review import revision_detail, revision_history
from pandrator.web.generation_segment_reads import GenerationSegmentReader
from pandrator.web.generation_summary import get_generation_summary
from pandrator.web.generation_topology import GenerationTopologyService
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.models import GenerationSegment
from pandrator.web.workspace import GenerationService
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests.test_generation_execution_policy import (
    _job as policy_job,
)
from tests.test_generation_execution_policy import (
    case as case,
)
from tests.test_generation_execution_policy import (
    credential as policy_credential,
)
from tests.test_generation_execution_policy import (
    template as template,  # noqa: F401
)

_SAFE_DENIALS = (AccessDenied, SessionNotFound)


def _generation_service(case: Any) -> GenerationService:
    return GenerationService(
        case.database,
        case.services.jobs,
        WorkspaceSettingsService(case.database),
    )


def _create_plan(case: Any, root: Any | None = None) -> tuple[str, str]:
    selected_root = root or case.a
    topology = GenerationTopologyService(case.database, case.services.jobs)
    plan = topology.create_plan(
        selected_root.id,
        source_revision_id=None,
        settings={},
        segments=[{"text": "A short sentence for access checks."}],
    )
    with case.database.snapshot_session() as session:
        segment_id = session.scalar(
            select(GenerationSegment.id).where(
                GenerationSegment.plan_revision_id == plan["active_revision_id"]
            )
        )
    assert isinstance(segment_id, str)
    return str(plan["active_revision_id"]), segment_id


@contextmanager
def _capture_statements(database: Database) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lower())

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)


def _assert_no_private_payload_reads(statements: list[str]) -> None:
    private_columns = (
        "generation_plan_revisions.settings_json",
        "generation_segments.text",
        "generation_runs.settings_snapshot_json",
        "output_assemblies.settings_json",
    )
    assert not [
        (column, statement)
        for column in private_columns
        for statement in statements
        if column in statement
    ]


def test_foreign_generation_reads_deny_before_protected_payload_queries(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_plan(case, case.a)
    foreign_revision_id, _ = _create_plan(case, case.b)
    run_id, _queued, _job, _payload = policy_job(
        case, policy_credential(case, "owner").actor, root=case.b
    )
    service = _generation_service(case)
    reader = GenerationSegmentReader(case.database, WorkspaceSettingsService(case.database))
    monkeypatch.setattr(
        reader.settings,
        "resolve",
        lambda *_args, **_kwargs: ({"tts": {}, "audio": {}}, "synthetic"),
    )
    viewer = policy_credential(case, "viewer").actor

    calls = (
        lambda: reader.list_segments(case.b.id),
        lambda: revision_history(case.database, case.b.id, include_audio_reuse=False),
        lambda: revision_detail(case.database, case.b.id, foreign_revision_id),
        lambda: service.prepare_start(case.b.id),
        lambda: service.preview_selection(case.b.id),
        lambda: service.request_pause(run_id),
        lambda: service.resume(run_id),
        lambda: service.latest_assembly(case.b.id),
        lambda: get_generation_summary(case.database, case.b.id),
    )
    with _capture_statements(case.database) as statements:
        with execution_actor_scope(viewer):
            for call in calls:
                with pytest.raises(_SAFE_DENIALS):
                    call()

    _assert_no_private_payload_reads(statements)


def test_viewer_reads_but_cannot_edit_run_or_cancel(case: Any) -> None:
    _plan_revision_id, segment_id = _create_plan(case)
    run_id, _queued, _job, _payload = policy_job(case, policy_credential(case, "owner").actor)
    service = _generation_service(case)
    reader = GenerationSegmentReader(case.database, WorkspaceSettingsService(case.database))
    viewer = policy_credential(case, "viewer").actor

    with execution_actor_scope(viewer):
        listed = reader.list_segments(case.a.id, view="compact")
        assert listed["total"] == 1
        assert revision_history(case.database, case.a.id, include_audio_reuse=False)["total"] == 1
        assert (
            revision_detail(case.database, case.a.id, _plan_revision_id)["revision"]["id"]
            == _plan_revision_id
        )
        assert get_generation_summary(case.database, case.a.id)["total"] == 1
        with pytest.raises(AccessDenied):
            with case.database.immediate_session() as session:
                segment = session.get_one(GenerationSegment, segment_id)
                service.update_segment_in_session(
                    session, segment_id, segment.revision, {"marked": True}
                )
        with pytest.raises(AccessDenied):
            service.prepare_start(case.a.id)
        with pytest.raises(AccessDenied):
            service.request_pause(run_id)
        with pytest.raises(AccessDenied):
            with case.database.immediate_session() as session:
                service.cancel_in_session(session, run_id)
        with pytest.raises(AccessDenied):
            service.delete_run(run_id)


def test_workspace_member_can_edit_and_prepare_generation(case: Any) -> None:
    _plan_revision_id, segment_id = _create_plan(case)
    service = _generation_service(case)
    member = policy_credential(case, "member").actor

    with execution_actor_scope(member):
        with case.database.immediate_session() as session:
            segment = session.get_one(GenerationSegment, segment_id)
            updated = service.update_segment_in_session(
                session, segment_id, segment.revision, {"marked": True}
            )
        prepared = service.prepare_start(case.a.id, segment_ids=[segment_id])
        with case.database.immediate_session() as session:
            started = service.start_in_session(session, case.a.id, prepared=prepared)

    assert updated["id"] == segment_id
    assert prepared["requested_segment_ids"] == [segment_id]
    assert started["status"] == "queued"


def test_membership_revocation_between_prepare_and_writer_denies_start(
    case: Any,
) -> None:
    _plan_revision_id, segment_id = _create_plan(case)
    service = _generation_service(case)
    member = policy_credential(case, "member").actor

    with execution_actor_scope(member):
        prepared = service.prepare_start(case.a.id, segment_ids=[segment_id])

    with case.database.immediate_session() as session:
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == member.user_id,
            )
        )

    with execution_actor_scope(member):
        with pytest.raises(_SAFE_DENIALS):
            with case.database.immediate_session() as session:
                service.start_in_session(session, case.a.id, prepared=prepared)


def test_role_loss_in_the_writer_is_rechecked_before_segment_reads(case: Any) -> None:
    _plan_revision_id, segment_id = _create_plan(case)
    service = _generation_service(case)
    member = policy_credential(case, "member").actor

    with execution_actor_scope(member):
        with pytest.raises(_SAFE_DENIALS):
            with case.database.immediate_session() as session:
                session.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == case.team,
                        WorkspaceMembership.user_id == member.user_id,
                    )
                )
                service.update_segment_in_session(session, segment_id, 1, {"marked": True})


def test_real_worker_lease_allows_derived_plan_write_without_app_write(
    case: Any,
) -> None:
    active_revision_id, segment_id = _create_plan(case)
    member = policy_credential(case, "member", scopes=frozenset({"app.read", "app.run"})).actor
    _run_id, _queued, job, _payload = policy_job(case, member)
    assert job.lease_owner is not None
    assert "app.write" not in member.scopes
    lease: JobLease = {
        "job_id": job.id,
        "owner_id": job.lease_owner,
        "lease_generation": job.lease_generation,
    }
    token = current_job_lease.set(lease)
    try:
        with execution_actor_scope(member):
            with case.database.immediate_session() as session:
                revised = GenerationTopologyService(
                    case.database, case.services.jobs
                ).revise_topology_in_session(
                    session,
                    case.a.id,
                    active_revision_id,
                    {"action": "split", "segment_id": segment_id, "cursor": 8},
                )
    finally:
        current_job_lease.reset(token)

    assert revised["revision_number"] == 2


def test_internal_none_and_validated_host_access_paths_still_read(case: Any) -> None:
    _create_plan(case)
    service = _generation_service(case)
    reader = GenerationSegmentReader(case.database, WorkspaceSettingsService(case.database))

    with execution_actor_scope(None):
        assert reader.list_segments(case.a.id)["total"] == 1
        assert get_generation_summary(case.database, case.a.id)["total"] == 1
    with execution_actor_scope(HostActor("pandrator-cli")):
        assert service.latest_assembly(case.a.id) is None
