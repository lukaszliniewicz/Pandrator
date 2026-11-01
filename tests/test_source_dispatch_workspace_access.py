"""Scalar dispatch boundaries and synthetic-parser preparation/recovery cases."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import event, select

from pandrator.logic.source_cleaning import SourceBlock, SourceDocument
from pandrator.runtime import DataPaths
from pandrator.web import source_cleaning_dispatch as dispatch
from pandrator.web.access import source_dispatch_policy as policy
from pandrator.web.access.contracts import AccessDenied, ProjectAction
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.artifacts import ArtifactService
from pandrator.web.dispatch import DispatchError
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    ApiToken,
    SessionRecord,
    SourceCleaningDispatchBatch,
    SourceCleaningDispatchRun,
    new_id,
)
from pandrator.web.projects.models import Project, ProjectMembership
from tests.test_job_access_policy import _context
from tests.test_job_access_policy import block_network_attempts as block_network_attempts
from tests.test_job_access_policy import case as case

RUN = frozenset({"app.read", "app.run"})
READ = frozenset({"app.read"})


def _create(d, unit, *, artifact_id=None):
    return d.service.create_in_session(
        unit,
        session_id=d.target,
        source_artifact_id=artifact_id or d.artifact.id,
        instructions="Synthetic parser boundary",
        evidence_limit=20,
        remove_footnotes=None,
        filter_citations=None,
        pdf_ocr_mode=None,
        pdf_ocr_language=None,
        pdf_ocr_dpi=None,
        pdf_remove_toc=None,
        pdf_remove_repeated_marginals=None,
    )


def _artifact(d, suffix="pdf", *, workspace=None, target=None):
    path = d.paths.uploads / f"{new_id()}.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Synthetic parser input; not a production PDF/EPUB.")
    return d.artifacts.register(
        path,
        kind=suffix,
        role="source",
        session_id=target,
        workspace_id=workspace,
        metadata={"original_filename": path.name},
    )


@pytest.fixture
def work(case):
    with case.database.session() as unit:
        unit.add(WorkspaceMembership(workspace_id=case.team, user_id=case.user, role="member"))
        project = Project(
            workspace_id=case.team,
            source_session_id=None,
            name="Creator dispatch project",
            created_by_user_id=case.owner,
        )
        unit.add(project)
        unit.flush()
        target = new_id()
        record = SessionRecord(
            id=target, storage_key=target, project_id=project.id, name="Dispatch target"
        )
        unit.add(record)
        unit.flush()
        project.source_session_id = target
        unit.add(ProjectMembership(project_id=project.id, user_id=case.other, role="viewer"))
        unit.add(ProjectMembership(project_id=project.id, user_id=case.user, role="viewer"))
        unit.flush()
        project_id = project.id
    paths = DataPaths(case.database.path.parent)
    artifacts = ArtifactService(case.database, paths)
    queue = JobQueue(case.database)
    d = SimpleNamespace(
        case=case,
        paths=paths,
        artifacts=artifacts,
        queue=queue,
        target=target,
        project_id=project_id,
    )
    d.artifact = _artifact(d, workspace=case.team, target=target)
    d.foreign = _artifact(d, workspace=case.personal[case.owner])
    d.runner = _context(case, case.user, scopes=RUN, workspaces=(case.team,)).actor
    d.service = dispatch.SourceCleaningDispatchRunService(
        case.database, artifacts, lambda sid: paths.sessions / sid, jobs=queue
    )
    with execution_actor_scope(d.runner), case.database.immediate_session() as unit:
        d.payload = _create(d, unit)
    d.run_id = d.payload["id"]
    return d


def _batch(d, *, completed=False):
    with d.case.database.session() as unit:
        batch = SourceCleaningDispatchBatch(
            dispatch_run_id=d.run_id,
            ordinal=0,
            phase="metadata",
            input_json={"packet_version": dispatch._PACKET_VERSION, "private": "unread packet"},
            input_hash="a" * 64,
            status="completed" if completed else "ready",
            claim_key="replay" if completed else None,
            submission_key="replay" if completed else None,
            output_hash=dispatch._canonical_hash({}) if completed else None,
        )
        unit.add(batch)
        unit.flush()
        return batch.id


@contextmanager
def _leased(actor, job, *, owner=None):
    lease: JobLease = {
        "job_id": job.id,
        "owner_id": owner or job.lease_owner,
        "lease_generation": job.lease_generation,
    }
    token = current_job_lease.set(lease)
    try:
        with execution_actor_scope(actor):
            yield
    finally:
        current_job_lease.reset(token)


def _document(d):
    return SourceDocument(
        source_type="pdf_structured",
        source_path=str(d.paths.root / d.artifact.relative_path),
        filename="Synthetic.pdf",
        blocks=[SourceBlock("b1", "Synthetic accepted text.", 1, 1)],
    )


def _synthetic_pdf(d, monkeypatch):
    parser = Mock(return_value=_document(d))
    monkeypatch.setattr(dispatch.source_cleaning, "build_source_document", parser)
    monkeypatch.setattr(
        dispatch.source_cleaning, "propose_deterministic_operations", lambda *_args, **_kwargs: []
    )
    return parser


def test_none_validates_arguments_but_adds_no_policy_query_or_preparation_unit():
    unit: Any = MagicMock()
    database: Any = MagicMock()
    service = dispatch.SourceCleaningDispatchRunService(
        database, MagicMock(), Mock(), workspace_settings=MagicMock()
    )
    with execution_actor_scope(None):
        for resource in ("session", "run", "batch"):
            policy.require_source_dispatch_access(unit, resource, "id")
        policy.require_source_preparation_access(unit, "run")
        policy._require_source_input_access(unit, "session", "artifact")
        service._check_preparation_access("run")
        for resource, identifier, action in (
            ("other", "id", ProjectAction.READ),
            ("run", "", ProjectAction.READ),
            ("run", "id", ProjectAction.EDIT),
        ):
            with pytest.raises(TypeError):
                policy.require_source_dispatch_access(unit, resource, identifier, action)
    assert unit.mock_calls == [] and database.mock_calls == []


@pytest.mark.parametrize("actor_kind", ["member_read_only", "viewer", "foreign_admin"])
def test_read_and_run_boundaries_precede_private_bodies_and_replay(work, actor_kind):
    d = work
    batch_id = _batch(d)
    if actor_kind == "member_read_only":
        actor = _context(d.case, d.case.user, scopes=READ, workspaces=(d.case.team,)).actor
    elif actor_kind == "viewer":
        actor = _context(d.case, d.case.other, scopes=RUN, projects=(d.project_id,)).actor
    else:
        actor = _context(
            d.case,
            d.case.outsider,
            scopes=RUN | {"app.admin"},
            workspaces=(d.case.personal[d.case.outsider],),
        ).actor
    if actor_kind != "foreign_admin":
        with execution_actor_scope(actor):
            assert d.service.get(d.run_id)["id"] == d.run_id
            assert d.service.list_runs(d.target)[0]["id"] == d.run_id
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(d.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(actor), d.case.database.immediate_session() as unit:
            if actor_kind == "foreign_admin":
                for action in (
                    lambda: d.service.get(d.run_id, db_session=unit),
                    lambda: d.service.list_runs(d.target, db_session=unit),
                ):
                    with pytest.raises(DispatchError) as caught:
                        action()
                    assert caught.value.code == "not_found" and caught.value.status == 404
            actions = (
                lambda: _create(d, unit),
                lambda: d.service.claim_in_session(
                    unit, run_id=d.run_id, claim_key="replay", lease_seconds=60
                ),
                lambda: d.service.renew_in_session(
                    unit, batch_id=batch_id, lease_token="unread", lease_seconds=60
                ),
                lambda: d.service.release_in_session(unit, batch_id=batch_id, lease_token="unread"),
                lambda: d.service.inspect_in_session(
                    unit,
                    batch_id=batch_id,
                    lease_token="unread",
                    action="inspect_document_structure",
                    arguments={},
                    view="working",
                ),
                lambda: d.service.submit_in_session(
                    unit,
                    batch_id=batch_id,
                    lease_token="unread",
                    submission_key="replay",
                    result={},
                ),
                lambda: d.service.retry_finalization_in_session(unit, run_id=d.run_id),
            )
            for action in actions:
                with pytest.raises((AccessDenied, DispatchError)):
                    action()
    finally:
        event.remove(d.case.database.engine, "before_cursor_execute", capture)
    assert all(
        "source_cleaning_dispatch_runs.settings_json" not in statement
        and "source_cleaning_dispatch_batches.input_json" not in statement
        for statement in statements
    )


@pytest.mark.parametrize("lost", ["scope", "role"])
def test_current_loss_blocks_claim_submit_replay_and_completed_finalize_shortcut(work, lost):
    d = work
    batch_id = _batch(d, completed=True)
    with d.case.database.session() as unit:
        if lost == "scope":
            token = unit.get(ApiToken, d.runner.token_id)
            assert token is not None
            token.scopes_json = ["app.read"]
        else:
            membership = unit.get(WorkspaceMembership, (d.case.team, d.case.user))
            assert membership is not None
            unit.delete(membership)
    with execution_actor_scope(d.runner), d.case.database.immediate_session() as unit:
        with pytest.raises(AccessDenied):
            d.service.claim_in_session(unit, run_id=d.run_id, claim_key="replay", lease_seconds=60)
        with pytest.raises(AccessDenied):
            d.service.submit_in_session(
                unit, batch_id=batch_id, lease_token="unread", submission_key="replay", result={}
            )
        with pytest.raises(AccessDenied):
            d.service.retry_finalization_in_session(unit, run_id=d.run_id)
        completed: Any = SimpleNamespace(id=d.run_id, status="completed")
        with pytest.raises(AccessDenied):
            d.service._retry_finalize(unit, completed)


def test_foreign_source_and_index_deny_before_artifact_body_path_or_hash(work, monkeypatch):
    d = work
    hashing = Mock(side_effect=AssertionError("Private input hash must not be read"))
    monkeypatch.setattr(dispatch, "sha256_file", hashing)
    paths = Mock(side_effect=AssertionError("Private input path must not be read"))
    d.service.artifacts = SimpleNamespace(paths=SimpleNamespace(managed_path=paths))
    foreign: Any = SimpleNamespace(
        session_id=d.target, source_artifact_id=d.foreign.id, index_artifact_id=d.foreign.id
    )
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    with execution_actor_scope(d.runner), d.case.database.session() as unit:
        policy.require_source_dispatch_access(unit, "session", d.target, ProjectAction.RUN)
        event.listen(d.case.database.engine, "before_cursor_execute", capture)
        try:
            for operation in (
                lambda: d.service._load_source(
                    unit, session_id=d.target, source_artifact_id=d.foreign.id
                ),
                lambda: d.service._verify_pinned_source(unit, foreign),
                lambda: d.service._load_index(unit, foreign),
            ):
                with pytest.raises(DispatchError) as caught:
                    operation()
                assert caught.value.code == "source_not_found" and caught.value.status == 404
        finally:
            event.remove(d.case.database.engine, "before_cursor_execute", capture)
    hashing.assert_not_called()
    paths.assert_not_called()
    assert all("artifacts.metadata_json" not in statement for statement in statements)


def test_direct_initial_denial_leaves_run_and_failure_bookkeeping_untouched(work, monkeypatch):
    d = work
    marker = Mock(side_effect=AssertionError("Foreign initial denial cannot mutate a run"))
    parser = Mock()
    monkeypatch.setattr(d.service, "_mark_preparation_failed", marker)
    monkeypatch.setattr(dispatch.source_cleaning, "build_source_document", parser)
    viewer = _context(d.case, d.case.other, scopes=RUN, projects=(d.project_id,)).actor
    with execution_actor_scope(viewer), pytest.raises(AccessDenied):
        d.service.prepare_run(d.run_id, Mock(), threading.Event())
    marker.assert_not_called()
    parser.assert_not_called()
    with d.case.database.session() as unit:
        run = unit.get(SourceCleaningDispatchRun, d.run_id)
        assert run is not None and run.status == "preparing" and run.error_code is None


@pytest.mark.parametrize("point", ["pdf_callback", "after_native"])
def test_admitted_pdf_role_loss_blocks_output_but_allows_failure_bookkeeping(
    work, monkeypatch, point
):
    d = work
    job = d.queue.claim("source-worker", lease_seconds=300)
    assert job is not None and job.id == d.payload["job_id"]
    original_marker = d.service._mark_preparation_failed
    marker = Mock(wraps=original_marker)
    monkeypatch.setattr(d.service, "_mark_preparation_failed", marker)
    register = Mock(wraps=d.artifacts.register_in_session)
    monkeypatch.setattr(d.artifacts, "register_in_session", register)
    progress = Mock()

    def parser(*_args, **kwargs):
        with d.case.database.session() as unit:
            membership = unit.get(WorkspaceMembership, (d.case.team, d.case.user))
            assert membership is not None
            unit.delete(membership)
        if point == "pdf_callback":
            kwargs["progress_callback"]("Synthetic revoked callback")
        return _document(d)

    monkeypatch.setattr(dispatch.source_cleaning, "build_source_document", parser)
    monkeypatch.setattr(
        dispatch.source_cleaning, "propose_deterministic_operations", lambda *_args, **_kwargs: []
    )
    with _leased(d.runner, job), pytest.raises(AccessDenied):
        d.service.prepare_run(d.run_id, progress, threading.Event())
    assert marker.call_count >= 1
    register.assert_not_called()
    assert not (
        d.paths.sessions / d.target / "source-cleaning-dispatch" / d.run_id / "source-index.json"
    ).exists()
    if point == "pdf_callback":
        assert all(call.args[0] != 0.25 for call in progress.call_args_list)
    with d.case.database.session() as unit:
        run = unit.get(SourceCleaningDispatchRun, d.run_id)
        assert run is not None and run.status == "failed"


def test_current_loss_after_last_precommit_progress_blocks_publication(work, monkeypatch):
    d = work
    parser = _synthetic_pdf(d, monkeypatch)
    register = Mock(wraps=d.artifacts.register_in_session)
    monkeypatch.setattr(d.artifacts, "register_in_session", register)
    forwarded = []

    def progress(value, _message):
        forwarded.append(value)
        if value == 0.8:
            with d.case.database.session() as unit:
                membership = unit.get(WorkspaceMembership, (d.case.team, d.case.user))
                assert membership is not None
                unit.delete(membership)

    with execution_actor_scope(d.runner), pytest.raises(AccessDenied):
        d.service.prepare_run(d.run_id, progress, threading.Event())
    parser.assert_called_once()
    assert 0.8 in forwarded and 1.0 not in forwarded
    register.assert_not_called()
    with d.case.database.session() as unit:
        run = unit.get(SourceCleaningDispatchRun, d.run_id)
        assert run is not None and run.status == "failed" and run.index_artifact_id is None
        assert not list(
            unit.scalars(
                select(SourceCleaningDispatchBatch).where(
                    SourceCleaningDispatchBatch.dispatch_run_id == d.run_id,
                )
            )
        )


def test_epub_role_loss_between_extract_and_document_build_stops_next_native_step(
    work, monkeypatch
):
    d = work
    epub = _artifact(d, "epub", workspace=d.case.team, target=d.target)
    with execution_actor_scope(d.runner), d.case.database.immediate_session() as unit:
        payload = _create(d, unit, artifact_id=epub.id)

    def extract(*_args, **_kwargs):
        with d.case.database.session() as unit:
            membership = unit.get(WorkspaceMembership, (d.case.team, d.case.user))
            assert membership is not None
            unit.delete(membership)
        return SimpleNamespace(ok=True, text="Synthetic EPUB text")

    build = Mock()
    monkeypatch.setattr(dispatch, "extract_epub_with_diagnostics", extract)
    monkeypatch.setattr(dispatch.source_cleaning, "build_cleaned_epub_source_document", build)
    with execution_actor_scope(d.runner), pytest.raises(AccessDenied):
        d.service.prepare_run(payload["id"], Mock(), threading.Event())
    build.assert_not_called()
    with d.case.database.session() as unit:
        run = unit.get(SourceCleaningDispatchRun, payload["id"])
        assert run is not None and run.status == "failed"


def test_member_preparation_and_finalization_after_creator_departure_with_synthetic_pdf(
    work, monkeypatch
):
    d = work
    with d.case.database.session() as unit:
        owner = unit.get(WorkspaceMembership, (d.case.team, d.case.owner))
        assert owner is not None
        unit.delete(owner)
    parser = _synthetic_pdf(d, monkeypatch)
    job = d.queue.claim("source-worker", lease_seconds=300)
    assert job is not None
    with _leased(d.runner, job):
        ready = d.service.prepare_run(d.run_id, Mock(), threading.Event())
        assert ready["status"] == "ready" and ready["batch_count"] == len(
            dispatch._DISPATCH_PHASE_ORDER
        )
        with d.case.database.immediate_session() as unit:
            run = unit.get(SourceCleaningDispatchRun, d.run_id)
            assert run is not None
            batches = list(
                unit.scalars(
                    select(SourceCleaningDispatchBatch).where(
                        SourceCleaningDispatchBatch.dispatch_run_id == d.run_id
                    )
                )
            )
            for batch in batches:
                batch.status = "completed"
                batch.normalized_output_json = {
                    "operations": [],
                    "accepted_proposal_operations": [],
                    "rejected_proposal_ids": [],
                }
            run.completed_batch_count = len(batches)
            run.status = "finalizing"
            receipt, status = d.service.retry_finalization_in_session(unit, run_id=d.run_id)
            assert status == 200 and receipt["finalized"] is True
            assert run.status == "completed" and run.result_artifact_id is not None
    parser.assert_called_once()
    assert (
        d.paths.root / d.artifact.relative_path
    ).read_bytes() == b"Synthetic parser input; not a production PDF/EPUB."


@pytest.mark.parametrize("failure", ["authority", "filesystem"])
def test_typed_finalization_denial_is_not_a_retryable_filesystem_failure(
    work, monkeypatch, failure
):
    d = work
    error = (
        AccessDenied()
        if failure == "authority"
        else PermissionError("Synthetic filesystem failure")
    )
    monkeypatch.setattr(d.service, "_materialize", Mock(side_effect=error))
    with execution_actor_scope(d.runner), d.case.database.immediate_session() as unit:
        run = unit.get(SourceCleaningDispatchRun, d.run_id)
        assert run is not None
        run.status = "finalizing"
        if failure == "authority":
            with pytest.raises(AccessDenied):
                d.service._retry_finalize(unit, run)
            assert run.error_code is None
        else:
            d.service._retry_finalize(unit, run)
            assert run.error_code == "materialization_failed"
        assert run.status == "finalizing"


@pytest.mark.parametrize("loss", ["scope", "role", "token"])
def test_owned_job_denial_terminalizes_only_its_run_in_existing_writer(work, monkeypatch, loss):
    d = work
    job = d.queue.claim("source-worker", lease_seconds=300)
    assert job is not None
    with d.case.database.session() as unit:
        if loss == "role":
            membership = unit.get(WorkspaceMembership, (d.case.team, d.case.user))
            assert membership is not None
            unit.delete(membership)
        else:
            token = unit.get(ApiToken, d.runner.token_id)
            assert token is not None
            if loss == "scope":
                token.scopes_json = ["app.read"]
            else:
                token.revoked_at = dispatch.utcnow()
    native = Mock()
    monkeypatch.setattr(dispatch.source_cleaning, "build_source_document", native)
    calls = []
    original = dispatch.SourceCleaningDispatchRunService._mark_preparation_failed

    def marker(service, run_id, error, *, db_session=None):
        calls.append((run_id, db_session is not None))
        return original(service, run_id, error, db_session=db_session)

    monkeypatch.setattr(
        dispatch.SourceCleaningDispatchRunService, "_mark_preparation_failed", marker
    )
    with _leased(d.runner, job), pytest.raises(AccessDenied):
        dispatch.prepare_source_cleaning_dispatch_job(
            d.case.database,
            d.artifacts,
            d.service.session_dir_resolver,
            {"source_cleaning_dispatch_run_id": d.run_id},
            Mock(),
            threading.Event(),
        )
    native.assert_not_called()
    assert calls == [(d.run_id, True)]
    with d.case.database.session() as unit:
        run = unit.get(SourceCleaningDispatchRun, d.run_id)
        assert run is not None and run.status == "failed" and run.error_code == "preparation_failed"


@pytest.mark.parametrize("wrong", ["run", "forged_owner"])
def test_wrong_run_or_forged_actual_lease_never_marks_any_run_failed(work, monkeypatch, wrong):
    d = work
    job = d.queue.claim("source-worker", lease_seconds=300)
    assert job is not None
    with execution_actor_scope(d.runner), d.case.database.immediate_session() as unit:
        second = _create(d, unit)
    native = Mock()
    monkeypatch.setattr(dispatch.source_cleaning, "build_source_document", native)
    selected = second["id"] if wrong == "run" else d.run_id
    with _leased(d.runner, job, owner="forged" if wrong == "forged_owner" else None):
        if wrong == "run":
            with d.case.database.session() as unit:
                policy.require_source_preparation_access(unit, d.run_id)
        with pytest.raises(AccessDenied):
            dispatch.prepare_source_cleaning_dispatch_job(
                d.case.database,
                d.artifacts,
                d.service.session_dir_resolver,
                {"source_cleaning_dispatch_run_id": selected},
                Mock(),
                threading.Event(),
            )
    native.assert_not_called()
    with d.case.database.session() as unit:
        rows = list(
            unit.scalars(
                select(SourceCleaningDispatchRun).where(
                    SourceCleaningDispatchRun.id.in_([d.run_id, second["id"]])
                )
            )
        )
        assert len(rows) == 2 and all(
            run.status == "preparing" and run.error_code is None for run in rows
        )
