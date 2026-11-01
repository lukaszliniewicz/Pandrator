"""Real durable copy admission, Worker leases, publication and privacy fences."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import zlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied, ProjectNotFound, WorkspaceNotFound
from pandrator.web.access.job_policy import JobNotFound
from pandrator.web.access.session_policy import (
    SessionNotFound,
    require_session_access,
    visible_session_ids_statement,
)
from pandrator.web.accounts import installation_mode
from pandrator.web.accounts.actor_authorization import ActorAuthorizationDenied
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import (
    actor_from_storage,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientWorkspaceGrant,
)
from pandrator.web.jobs import Worker
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_jobs as domain
from pandrator.web.projects.copy_contracts import PROJECT_COPY_JOB_KIND, ProjectCopyError
from pandrator.web.projects.copy_jobs import ProjectCopyService, ProjectCopyWorker
from pandrator.web.projects.copy_publication import copy_identity
from tests.project_copy_test_support import (
    cached_destination_connections,
    capture_with_connections,
    preview_with_connections,
    seed_copy_model,
)
from tests.test_project_copy_snapshot import _context
from tests.test_project_copy_snapshot import bundle_case as _bundle_case
from tests.test_project_copy_snapshot import unbound_copy_case as _copy_case
from tests.test_project_operation_access import _person

bundle_case = _bundle_case
copy_case = _copy_case
SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
DENIED = (AccessDenied, ProjectNotFound, WorkspaceNotFound, ActorAuthorizationDenied)
PRIVATE_TEXT = "PRIVATE SOURCE EDITORIAL TEXT COPY JOB"
PRIVATE_NAME = "Private destination title for copy job"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("Network is not part of project copying")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _prepare_job_case(copy_case, monkeypatch):
    case = copy_case
    monkeypatch.setattr(installation_mode, "COLLABORATION_AVAILABLE", True)
    case.services.auth.initialize_owner("disposable project copy job password")
    enabled = case.client.post(
        "/api/v1/admin/installation/collaboration",
        json={"expected_revision": 0},
        headers=case.headers,
    )
    assert enabled.status_code == 200, enabled.get_json()
    with case.database.immediate_session() as session:
        destination = m.Workspace(id=new_id(), kind="team", name="Copy job destination")
        session.add(destination)
        session.flush()
        session.add(
            m.WorkspaceMembership(
                workspace_id=destination.id, user_id=case.principal.user_id, role="owner"
            )
        )
        case.destination = destination.id
        session.get(m.Segment, case.segments[0][1]).text = PRIVATE_TEXT
    case.member = _person(case, workspace_id=case.destination)
    with case.database.immediate_session() as session:
        session.add(
            m.WorkspaceMembership(
                workspace_id=case.destination, user_id=case.member.user_id, role="member"
            )
        )
    case.copies = ProjectCopyService(
        case.database,
        case.services.paths,
        case.services.jobs,
        case.services.idempotency,
        case.services.workspace_settings,
        case.services.audit,
    )
    case.executor = ProjectCopyWorker(
        case.database, case.services.paths, case.services.jobs, case.services.audit
    )
    case.owner_context = _context(case.principal)
    return case


@pytest.fixture
def unbound_job_case(copy_case, monkeypatch):
    return _prepare_job_case(copy_case, monkeypatch)


@pytest.fixture
def job_case(copy_case, monkeypatch):
    case = _prepare_job_case(copy_case, monkeypatch)
    seed_copy_model(case, case.destination)
    return case


def _token(
    case, *, scopes: set[str] | frozenset[str] = SCOPES, client=False, user_id=None, subject=None
):
    user_id = user_id or case.principal.user_id
    token_id = new_id()
    with case.database.immediate_session() as session:
        revision = session.get(UserCredential, user_id).auth_revision
        client_id = new_id() if client else None
        if client_id:
            session.add(
                m.AutomationClient(
                    id=client_id,
                    name="Copy test client",
                    subject=f"client:{client_id}",
                    allowed_scopes_json=sorted(scopes),
                    target_instance_id=case.target["instance_id"],
                    canonical_origin=case.target["canonical_origin"],
                    created_by="copy-job-test",
                    user_id=user_id,
                    user_auth_revision=revision,
                )
            )
            session.flush()
        session.add(
            m.ApiToken(
                id=token_id,
                label="Copy test token",
                token_hash=hashlib.sha256(token_id.encode()).hexdigest(),
                token_prefix=token_id[:12],
                subject=subject or f"copy-token:{token_id}",
                scopes_json=sorted(scopes),
                principal_kind="automation_client" if client_id else "api_token",
                user_id=user_id,
                user_auth_revision=revision,
                client_id=client_id,
            )
        )
        session.flush()
        for workspace in (case.source_workspace, case.destination):
            session.add(ApiTokenWorkspaceGrant(token_id=token_id, workspace_id=workspace))
            if client_id:
                session.add(
                    AutomationClientWorkspaceGrant(client_id=client_id, workspace_id=workspace)
                )
    principal = case.services.auth.resolve_api_token(
        token_id,
        network_zone="loopback",
        target_instance_id=case.target["instance_id"],
        canonical_origin=case.target["canonical_origin"],
    )
    assert principal is not None
    return principal


def _admit(case, *, principal=None, key="copy-job-request-1", fingerprint=None, name=PRIVATE_NAME):
    principal = principal or case.principal
    if not fingerprint:
        preview = preview_with_connections(
            case, _context(principal), case.source.project_id, case.destination
        )
        fingerprint = preview["fingerprint"]
    return case.copies.request(
        principal,
        case.source.project_id,
        case.destination,
        expected_fingerprint=fingerprint,
        name=name,
        idempotency_key=key,
        destination_connections=cached_destination_connections(
            case, case.source.project_id, case.destination
        ),
    )


def _job(case, job_id):
    with case.database.snapshot_session() as session:
        return session.get(m.Job, job_id)


def _worker(case, handler=None, worker_id="copy-test-worker"):
    worker = Worker(
        case.services.jobs, worker_id, {PROJECT_COPY_JOB_KIND: handler or case.executor.run}
    )
    assert worker.run_once()
    return worker


def _none_published(case, job_id):
    root = copy_identity(job_id, "projects", case.source.project_id)
    with case.database.snapshot_session() as session:
        assert session.get(m.Project, root) is None
        assert not list(session.scalars(select(m.Artifact.id).where(m.Artifact.project_id == root)))
    attempt = case.services.paths.artifacts / "project-copies" / job_id
    assert not attempt.exists() or not list(attempt.iterdir())


def _counts(case):
    with case.database.snapshot_session() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.ApiIdempotency, m.Project)
        )


def test_actual_worker_admits_once_keeps_source_editable_and_hides_generic_details(
    job_case, monkeypatch
):
    case = job_case
    local = threading.local()

    def sql(_conn, _cursor, statement, _params, _context, _many):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            local.writer = True

    def finished(_conn):
        local.writer = False

    event.listen(case.database.engine, "before_cursor_execute", sql)
    event.listen(case.database.engine, "commit", finished)
    event.listen(case.database.engine, "rollback", finished)
    captures, encodes, stages = (
        domain.capture_current_project,
        domain._encode_snapshot,
        domain.stage_project_files,
    )

    def checked_capture(*args, **kwargs):
        assert not getattr(local, "writer", False)
        return captures(*args, **kwargs)

    def checked_encode(*args, **kwargs):
        assert not getattr(local, "writer", False)
        return encodes(*args, **kwargs)

    edited = False

    def checked_stage(*args, **kwargs):
        nonlocal edited
        assert not getattr(local, "writer", False)
        # A session-null copy job must not make ordinary source edits busy.
        with case.database.snapshot_session() as session:
            revision = session.get(m.SessionRecord, case.source.id).revision
        response = case.client.patch(
            f"/api/v1/sessions/{case.source.id}",
            json={"name": "Source edited after acceptance"},
            headers={**case.headers, "If-Match": f'"{revision}"'},
        )
        assert response.status_code == 200, response.get_json()
        edited = True
        return stages(*args, **kwargs)

    monkeypatch.setattr(domain, "capture_current_project", checked_capture)
    monkeypatch.setattr(domain, "_encode_snapshot", checked_encode)
    monkeypatch.setattr(domain, "stage_project_files", checked_stage)
    try:
        preview = preview_with_connections(
            case, case.owner_context, case.source.project_id, case.destination
        )
        with execution_actor_scope(None):
            admitted, status = _admit(case, fingerprint=preview["fingerprint"])
            assert current_execution_actor.get() is None
        assert status == 202
        job_id = admitted["id"]
        job = _job(case, job_id)
        assert (
            job.kind == PROJECT_COPY_JOB_KIND
            and job.session_id is None
            and job.workflow_run_id is None
        )
        assert (
            job.project_id == case.source.project_id and job.workspace_id == case.source_workspace
        )
        assert job.max_attempts == 2 and job.resource_keys_json == ["project-copy:disk"]
        assert set(job.payload_json) == domain._PAYLOAD
        with case.database.snapshot_session() as session:
            attribution = session.get(m.JobActorRecord, job_id)
            saved = actor_from_storage(
                attribution.user_id, attribution.auth_revision, attribution.context_json
            )
            assert saved == case.owner_context.actor
            receipt = session.scalar(
                select(m.ApiIdempotency).where(
                    m.ApiIdempotency.operation_id == "copyCurrentProject"
                )
            )
            assert receipt.resource_id == job_id and receipt.response_json["result"] is None
            assert PRIVATE_NAME not in json.dumps(receipt.response_json)
        worker = _worker(case)
        assert edited
        private = case.copies.get(case.owner_context, job_id)
        assert private["state"] == "succeeded" and private["result"]["name"] == PRIVATE_NAME
        root_id = private["result"]["project_id"]
        with case.database.snapshot_session() as session:
            copied = session.get(m.Segment, copy_identity(job_id, "segments", case.segments[0][1]))
            assert copied.text == PRIVATE_TEXT
            assert session.get(m.Segment, case.segments[0][1]).text == PRIVATE_TEXT
            assert (
                len(
                    list(
                        session.scalars(
                            select(m.SessionRecord).where(m.SessionRecord.project_id == root_id)
                        )
                    )
                )
                == 3
            )
            member = _context(case.member)
            assert private["result"]["source_session_id"] in set(
                session.scalars(visible_session_ids_statement(session, member))
            )
            require_session_access(session, member, private["result"]["source_session_id"])
            assert (
                session.scalar(
                    select(m.ProjectMembership).where(m.ProjectMembership.project_id == root_id)
                )
                is None
            )
            audit = list(
                session.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "project.copy"))
            )
            assert len(audit) == 1 and audit[0].resource_id == root_id
            assert set(audit[0].metadata_json) == {
                "source_project_id",
                "destination_workspace_id",
                "fingerprint",
                "job_id",
            }
            successes = list(
                session.scalars(
                    select(m.JobEvent).where(
                        m.JobEvent.job_id == job_id, m.JobEvent.event_type == "job.succeeded"
                    )
                )
            )
            assert len(successes) == 1 and successes[0].payload_json["copy_id"] == job_id
        with patch.object(
            domain,
            "capture_current_project",
            side_effect=AssertionError("Replay must not recapture"),
        ):
            replayed, replay_status = _admit(case, fingerprint=preview["fingerprint"])
        assert replay_status == 200 and replayed == private
        sealed = _job(case, job_id)
        case.services.jobs.complete(
            job_id, worker.worker_id, sealed.result_json, lease_generation=sealed.lease_generation
        )
        with case.database.snapshot_session() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(m.JobEvent)
                    .where(m.JobEvent.job_id == job_id, m.JobEvent.event_type == "job.succeeded")
                )
                == 1
            )
        raw_blob = sealed.payload_json["snapshot_zlib_b64"]
        for url in (
            f"/api/v1/jobs/{job_id}",
            "/api/v1/jobs",
            f"/api/v1/work/{job_id}",
            f"/api/v1/work/{job_id}/events",
            f"/api/v1/jobs/{job_id}/logs",
        ):
            response = case.client.get(url)
            assert response.status_code == 200, (url, response.get_json())
            public = response.get_data(as_text=True)
            for marker in (
                PRIVATE_NAME,
                PRIVATE_TEXT,
                case.destination,
                root_id,
                raw_blob,
                "snapshot_zlib_b64",
                "state_json",
                str(case.root),
            ):
                assert marker not in public
    finally:
        event.remove(case.database.engine, "before_cursor_execute", sql)
        event.remove(case.database.engine, "commit", finished)
        event.remove(case.database.engine, "rollback", finished)


def test_preview_has_no_run_requirement_but_admission_does(job_case):
    case = job_case
    principal = _token(case, scopes={"app.read", "app.write"})
    preview = preview_with_connections(
        case, _context(principal), case.source.project_id, case.destination
    )
    before = _counts(case)
    with pytest.raises(AccessDenied):
        _admit(case, principal=principal, fingerprint=preview["fingerprint"])
    assert _counts(case) == before


@pytest.mark.parametrize(
    "change", ["source_membership", "destination_membership", "token_narrow", "token_revoke"]
)
def test_current_authority_precedes_new_and_receipt_replay_and_worker_io(job_case, change):
    case = job_case
    principal = _token(case)
    preview = preview_with_connections(
        case, _context(principal), case.source.project_id, case.destination
    )
    admitted, _ = _admit(case, principal=principal, fingerprint=preview["fingerprint"])
    with case.database.immediate_session() as session:
        if change.endswith("membership"):
            workspace = case.source_workspace if change == "source_membership" else case.destination
            session.delete(session.get(m.WorkspaceMembership, (workspace, principal.user_id)))
        else:
            token = session.get(m.ApiToken, principal.token_id)
            if change == "token_narrow":
                token.scopes_json = ["app.read"]
            else:
                token.revoked_at = m.utcnow()
    before = _counts(case)
    with patch.object(
        domain, "capture_current_project", side_effect=AssertionError("Denied before graph")
    ):
        for key in ("copy-job-request-1", "copy-job-fresh-denied"):
            with pytest.raises(DENIED):
                _admit(case, principal=principal, fingerprint=preview["fingerprint"], key=key)
    assert _counts(case) == before
    with patch.object(
        domain, "stage_project_files", side_effect=AssertionError("Denied before file IO")
    ) as staging:
        _worker(case)
        assert staging.call_count == 0
    _none_published(case, admitted["id"])


def test_readonly_reader_and_foreign_admin_never_capture_or_reserve(job_case):
    case = job_case
    fingerprint = preview_with_connections(
        case, case.owner_context, case.source.project_id, case.destination
    )["fingerprint"]
    for kind in ("reader", "admin"):
        principal = _person(case, workspace_id=case.destination, admin=kind == "admin")
        with case.database.immediate_session() as session:
            session.add(
                m.WorkspaceMembership(
                    workspace_id=case.destination, user_id=principal.user_id, role="member"
                )
            )
            if kind == "reader":
                session.add(
                    m.ProjectMembership(
                        project_id=case.source.project_id, user_id=principal.user_id, role="viewer"
                    )
                )
                session.add(
                    ApiTokenProjectGrant(
                        token_id=principal.token_id, project_id=case.source.project_id
                    )
                )
        before = _counts(case)
        with patch.object(
            domain, "capture_current_project", side_effect=AssertionError("No graph")
        ):
            with pytest.raises(DENIED):
                _admit(case, principal=principal, fingerprint=fingerprint)
        assert _counts(case) == before


def test_status_uses_actual_initiating_user_across_credentials_not_subject(job_case):
    case = job_case
    admitted, _ = _admit(case)
    _worker(case)
    approved = _token(case, scopes={"app.read"})
    assert case.copies.get(_context(approved), admitted["id"])["state"] == "succeeded"
    other = _person(case, workspace_id=case.destination)
    other = _token(case, scopes={"app.read"}, user_id=other.user_id, subject=case.principal.subject)
    with case.database.immediate_session() as session:
        session.add(
            m.WorkspaceMembership(
                workspace_id=case.destination, user_id=other.user_id, role="member"
            )
        )
        session.add(
            m.ProjectMembership(
                project_id=case.source.project_id, user_id=other.user_id, role="viewer"
            )
        )
    with pytest.raises(JobNotFound):
        case.copies.get(_context(other), admitted["id"])


@pytest.mark.parametrize("phase", ["preview", "admission"])
def test_inconsistent_ambient_actor_denies_preview_and_admission_without_writes(job_case, phase):
    case = job_case
    principal = case.principal
    preview = preview_with_connections(
        case, _context(principal), case.source.project_id, case.destination
    )
    before = _counts(case)
    different = _person(case, workspace_id=case.destination)
    ambient_before = current_execution_actor.get()
    different_actor = _context(different).actor

    with execution_actor_scope(different_actor):
        if phase == "preview":
            with pytest.raises(SessionNotFound):
                preview_with_connections(
                    case, _context(principal), case.source.project_id, case.destination
                )
        else:
            with pytest.raises(ProjectCopyError) as error:
                _admit(
                    case,
                    principal=principal,
                    key=f"copy-job-ambient-{phase}",
                    fingerprint=preview["fingerprint"],
                )
            assert error.value.code == "copy_publication_failed"
        assert current_execution_actor.get() == different_actor

    assert current_execution_actor.get() == ambient_before
    assert _counts(case) == before


@pytest.mark.parametrize("transport", ["logout", "expiry"])
def test_real_worker_continues_after_logout_or_token_expiry(job_case, transport):
    case = job_case
    principal = case.principal if transport == "logout" else _token(case)
    different = _person(case, workspace_id=case.destination)
    admitting_actor = _context(principal).actor
    ambient_before_admission = current_execution_actor.get()
    with execution_actor_scope(admitting_actor):
        admitted, _ = _admit(case, principal=principal)
        assert current_execution_actor.get() == admitting_actor
    assert current_execution_actor.get() == ambient_before_admission
    with case.database.snapshot_session() as session:
        saved = session.get(m.JobActorRecord, admitted["id"])
        assert saved is not None
        assert (
            actor_from_storage(saved.user_id, saved.auth_revision, saved.context_json)
            == admitting_actor
        )
    if transport == "logout":
        response = case.client.post("/api/v1/auth/logout", headers=case.headers)
        assert response.status_code == 200
        assert case.client.get("/api/v1/sessions").status_code == 401
    else:
        assert principal.token_id is not None
        with case.database.immediate_session() as session:
            session.get(m.ApiToken, principal.token_id).expires_at = m.utcnow() - timedelta(
                seconds=1
            )
        assert (
            case.services.auth.resolve_api_token(
                principal.token_id,
                network_zone="loopback",
                target_instance_id=case.target["instance_id"],
                canonical_origin=case.target["canonical_origin"],
            )
            is None
        )
    worker_actor = _context(different).actor
    ambient_before_worker = current_execution_actor.get()
    with execution_actor_scope(worker_actor):
        _worker(case)
        assert current_execution_actor.get() == worker_actor
    assert current_execution_actor.get() == ambient_before_worker
    current = _token(case, scopes={"app.read"})
    assert case.copies.get(_context(current), admitted["id"])["state"] == "succeeded"


@pytest.mark.parametrize(
    "change",
    [
        "reset",
        "disabled",
        "client",
        "scope",
        "file_resource",
        "source_membership",
        "destination_membership",
        "lease",
    ],
)
def test_actual_worker_rechecks_authority_and_lease_after_real_staging(
    job_case, monkeypatch, change
):
    case = job_case
    principal = _token(case, client=change == "client")
    admitted, _ = _admit(case, principal=principal)
    actual_stage = domain.stage_project_files

    def revoke_after_stage(*args, **kwargs):
        staged = actual_stage(*args, **kwargs)
        with case.database.immediate_session() as session:
            if change == "reset":
                session.get(UserCredential, principal.user_id).auth_revision += 1
            elif change == "disabled":
                session.get(m.User, principal.user_id).is_active = False
            elif change == "client":
                session.get(m.AutomationClient, principal.client_id).revoked_at = m.utcnow()
            elif change == "scope":
                session.get(m.ApiToken, principal.token_id).scopes_json = ["app.read"]
            elif change == "file_resource":
                session.delete(
                    session.get(ApiTokenWorkspaceGrant, (principal.token_id, case.source_workspace))
                )
                session.add(
                    ApiTokenProjectGrant(
                        token_id=principal.token_id, project_id=case.source.project_id
                    )
                )
            elif change in {"source_membership", "destination_membership"}:
                workspace = (
                    case.source_workspace if change == "source_membership" else case.destination
                )
                session.delete(session.get(m.WorkspaceMembership, (workspace, principal.user_id)))
        return staged

    monkeypatch.setattr(domain, "stage_project_files", revoke_after_stage)

    def handler(payload, progress, cancel):
        def observed(value, detail):
            progress(value, detail)
            if change == "lease" and detail == "Publishing project copy":
                with case.database.immediate_session() as session:
                    session.get(m.Job, payload["copy_id"]).lease_expires_at = (
                        m.utcnow() - timedelta(seconds=1)
                    )

        return case.executor.run(payload, observed, cancel)

    _worker(case, handler)
    _none_published(case, admitted["id"])
    job = _job(case, admitted["id"])
    assert job.status != "succeeded" and job.result_json is None


def test_codec_is_bounded_strict_and_immutable_with_fixed_errors(job_case, monkeypatch):
    case = job_case
    with case.database.snapshot_session() as session:
        snapshot = capture_with_connections(
            case,
            session,
            case.owner_context,
            case.source.project_id,
            case.destination,
            settings_service=case.services.workspace_settings,
        )
    digest, encoded = domain._encode_snapshot(snapshot)
    decoded = domain._decode_snapshot(encoded, digest)
    assert decoded == replace(snapshot, destination_connection_options_json="[]")
    assert decoded.destination_connection_options_json == "[]"
    assert decoded.state_json == snapshot.state_json
    assert decoded.fingerprint == snapshot.fingerprint
    assert decoded.files == snapshot.files
    compressed = base64.b64decode(encoded)
    for blob in (
        "not base64!",
        base64.b64encode(compressed[:-1]).decode(),
        base64.b64encode(compressed + b"trailing").decode(),
    ):
        with pytest.raises(ProjectCopyError) as error:
            domain._decode_snapshot(blob, digest)
        assert error.value.code == "copy_input_invalid"
    raw = zlib.decompress(compressed)
    envelope = json.loads(raw)
    envelope["extra"] = "private content"
    raw = domain._canonical(envelope).encode()
    with pytest.raises(ProjectCopyError) as error:
        domain._decode_snapshot(
            base64.b64encode(zlib.compress(raw)).decode(), hashlib.sha256(raw).hexdigest()
        )
    assert error.value.code == "copy_input_invalid" and "private content" not in str(error.value)
    assert domain._MAX_RAW_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr(domain, "_MAX_RAW_BYTES", 32)
    with pytest.raises(ProjectCopyError) as error:
        domain._decode_snapshot(encoded, digest)
    assert error.value.code == "copy_input_invalid"


@pytest.mark.parametrize("change", ["header", "blob", "file_bytes"])
def test_invalid_worker_input_or_changed_file_never_publishes(job_case, change):
    case = job_case
    admitted, _ = _admit(case)
    if change == "blob":
        with case.database.immediate_session() as session:
            job = session.get(m.Job, admitted["id"])
            job.payload_json = {**job.payload_json, "snapshot_zlib_b64": "PRIVATE INVALID SNAPSHOT"}
    elif change == "file_bytes":
        case.services.paths.managed_path(case.current.relative_path).write_text(
            "File changed after admission", encoding="utf-8"
        )

    def handler(payload, progress, cancel):
        if change == "header":
            payload = {**payload, "source_project_id": new_id()}
        return case.executor.run(payload, progress, cancel)

    _worker(case, handler)
    _none_published(case, admitted["id"])
    assert _job(case, admitted["id"]).status != "succeeded"


def test_not_ready_stale_and_bounded_requests_abandon_only_pending_reservation(
    job_case, monkeypatch
):
    case = job_case
    old = preview_with_connections(
        case, case.owner_context, case.source.project_id, case.destination
    )["fingerprint"]
    with case.database.immediate_session() as session:
        session.get(m.Segment, case.segments[0][1]).text = "A changed saved point"
    with pytest.raises(ProjectCopyError) as error:
        _admit(case, fingerprint=old)
    assert error.value.code == "copy_preview_stale"
    with case.database.immediate_session() as session:
        plan = m.GenerationPlan(session_id=case.source.id)
        session.add(plan)
        session.flush()
        plan_id = plan.id
    preview = preview_with_connections(
        case, case.owner_context, case.source.project_id, case.destination
    )
    assert not preview["ready"]
    with pytest.raises(ProjectCopyError) as error:
        _admit(case, fingerprint=preview["fingerprint"])
    assert error.value.code == "copy_not_ready"
    with case.database.immediate_session() as session:
        session.delete(session.get(m.GenerationPlan, plan_id))
        session.merge(
            m.SessionSetting(
                session_id=case.source.id,
                section="translation",
                value_json={"base_url": "https://private-binding.invalid"},
            )
        )
    preview = preview_with_connections(
        case, case.owner_context, case.source.project_id, case.destination
    )
    assert not preview["ready"] and "private-binding" not in json.dumps(preview)
    with pytest.raises(ProjectCopyError) as error:
        _admit(case, fingerprint=preview["fingerprint"])
    assert error.value.code == "copy_not_ready"
    with case.database.immediate_session() as session:
        session.get(m.SessionSetting, (case.source.id, "translation")).value_json = {}
    fingerprint = preview_with_connections(
        case, case.owner_context, case.source.project_id, case.destination
    )["fingerprint"]
    monkeypatch.setattr(domain, "_MAX_RAW_BYTES", 32)
    with pytest.raises(ProjectCopyError) as error:
        _admit(case, fingerprint=fingerprint)
    assert error.value.code == "copy_limit_exceeded"
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(m.Job).where(m.Job.kind == PROJECT_COPY_JOB_KIND)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.ApiIdempotency)
                .where(m.ApiIdempotency.operation_id == "copyCurrentProject")
            )
            == 0
        )


def test_failed_admission_cleanup_is_allowed_after_mode_is_disabled(job_case, monkeypatch):
    case = job_case
    original = domain._encode_snapshot

    def disable_after_encoding(snapshot):
        result = original(snapshot)
        with case.database.immediate_session() as session:
            policy = session.get(m.AppSetting, installation_mode.INSTALLATION_POLICY_KEY)
            policy.value_json = {"schema_version": 1, "mode": "solo"}
            policy.revision += 1
        return result

    monkeypatch.setattr(domain, "_encode_snapshot", disable_after_encoding)
    with pytest.raises(installation_mode.InstallationModeError):
        _admit(case)
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(m.ApiIdempotency.id).where(
                    m.ApiIdempotency.operation_id == "copyCurrentProject"
                )
            )
            is None
        )
        assert session.scalar(select(m.Job.id).where(m.Job.kind == PROJECT_COPY_JOB_KIND)) is None


def test_cancel_before_publish_removes_attempt_but_cannot_reverse_sealed_success(job_case):
    case = job_case
    first, _ = _admit(case)

    def cancel_at_publication(payload, progress, canceled):
        def observed(value, detail):
            progress(value, detail)
            if detail == "Publishing project copy":
                case.services.jobs.request_cancel(payload["copy_id"])
                canceled.set()

        return case.executor.run(payload, observed, canceled)

    _worker(case, cancel_at_publication)
    assert _job(case, first["id"]).status == "canceled"
    _none_published(case, first["id"])
    second, _ = _admit(case, key="copy-job-second-success")
    _worker(case)
    done = _job(case, second["id"])
    assert done.status == "succeeded"
    case.services.jobs.request_cancel(done.id)
    assert _job(case, done.id).status == "succeeded"
    with case.database.snapshot_session() as session:
        assert session.get(m.Project, done.result_json["project_id"]) is not None


def test_audit_failure_rolls_back_graph_audit_and_success_without_private_logs(
    job_case, monkeypatch
):
    case = job_case
    admitted, _ = _admit(case)
    actual_audit = case.services.audit.record_command_in_session
    private_path = str(case.services.paths.managed_path(case.current.relative_path))

    def fail_after_real_audit(*args, **kwargs):
        actual_audit(*args, **kwargs)
        raise RuntimeError(PRIVATE_NAME + PRIVATE_TEXT + private_path)

    monkeypatch.setattr(case.services.audit, "record_command_in_session", fail_after_real_audit)
    _worker(case)
    _none_published(case, admitted["id"])
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "project.copy"))
            is None
        )
        assert (
            session.scalar(
                select(m.JobEvent.id).where(
                    m.JobEvent.job_id == admitted["id"], m.JobEvent.event_type == "job.succeeded"
                )
            )
            is None
        )
        events = list(
            session.scalars(select(m.JobEvent).where(m.JobEvent.job_id == admitted["id"]))
        )
        diagnostics = json.dumps([row.payload_json for row in events])
        for private in (
            PRIVATE_NAME,
            PRIVATE_TEXT,
            private_path,
            _job(case, admitted["id"]).payload_json["snapshot_zlib_b64"],
        ):
            assert private not in diagnostics
    assert _job(case, admitted["id"]).error_message == str(
        domain.project_copy_error("copy_publication_failed")
    )


def test_ambiguous_commit_keeps_registered_bytes_and_durable_success(job_case):
    case = job_case
    admitted, _ = _admit(case)
    original = case.database.immediate_session
    raised = False

    def handler(payload, progress, cancel):
        @contextmanager
        def uncertain(*args, **kwargs):
            nonlocal raised
            with original(*args, **kwargs) as session:
                yield session
                job = session.get(m.Job, payload["copy_id"])
                committed_copy = job is not None and job.status == "succeeded" and not raised
                if committed_copy:
                    raised = True
            if committed_copy:
                raise RuntimeError("Private commit acknowledgement detail")

        with patch.object(case.database, "immediate_session", uncertain):
            return case.executor.run(payload, progress, cancel)

    _worker(case, handler)
    assert raised
    done = _job(case, admitted["id"])
    assert done.status == "succeeded" and done.result_json
    with case.database.snapshot_session() as session:
        artifacts = list(
            session.scalars(
                select(m.Artifact).where(m.Artifact.project_id == done.result_json["project_id"])
            )
        )
        assert artifacts and all(
            case.services.paths.managed_path(row.relative_path).is_file() for row in artifacts
        )
    assert case.copies.get(case.owner_context, admitted["id"])["state"] == "succeeded"


def test_source_pointer_replacement_after_capture_does_not_bind_a_different_source(
    job_case, monkeypatch
):
    case = job_case
    source = case.services.sessions.create(
        "Replaceable empty subtitle source", workflow_kind="subtitles"
    )
    preview = preview_with_connections(
        case, case.owner_context, source.project_id, case.destination
    )
    assert preview["ready"]
    original = domain._encode_snapshot
    replacement_id = None

    def replace_after_capture(snapshot):
        nonlocal replacement_id
        encoded = original(snapshot)
        with case.database.immediate_session() as session:
            # Real graph lifecycle: the old source must be gone before its root
            # pointer can be cleared/replaced. All current triggers stay active.
            session.delete(session.get(m.SessionRecord, source.id))
            session.flush()
            root = session.get(m.Project, source.project_id, populate_existing=True)
            assert root.source_session_id is None
            replacement = m.SessionRecord(
                project_id=source.project_id, name="Replacement source", workflow_kind="subtitles"
            )
            session.add(replacement)
            session.flush()
            root.source_session_id = replacement.id
            replacement_id = replacement.id
        return encoded

    monkeypatch.setattr(domain, "_encode_snapshot", replace_after_capture)
    with pytest.raises(ProjectCopyError) as error:
        case.copies.request(
            case.principal,
            source.project_id,
            case.destination,
            expected_fingerprint=preview["fingerprint"],
            idempotency_key="copy-source-replacement",
            destination_connections=cached_destination_connections(
                case, source.project_id, case.destination
            ),
        )
    assert error.value.code == "copy_preview_stale"
    with case.database.snapshot_session() as session:
        assert session.get(m.Project, source.project_id).source_session_id == replacement_id
        assert session.scalar(select(m.Job.id).where(m.Job.kind == PROJECT_COPY_JOB_KIND)) is None
        assert (
            session.scalar(
                select(m.ApiIdempotency.id).where(
                    m.ApiIdempotency.operation_id == "copyCurrentProject"
                )
            )
            is None
        )
        assert (
            session.scalar(select(m.Project.id).where(m.Project.workspace_id == case.destination))
            is None
        )
