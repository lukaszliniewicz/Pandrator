"""Disposable durable purge admission, claim, and result checks."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify

from pandrator.runtime import DataPaths
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database, upgrade_database
from pandrator.web.idempotency import IdempotencyService
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Job, SessionPurge, SessionRecord
from pandrator.web.session_purge import PurgeBlocked, SessionPurgeService
from pandrator.web.session_purge_job_routes import register_session_purge_job_routes
from pandrator.web.session_purge_jobs import INSPECTION_KIND, PURGE_KIND, purge_job_handlers
from pandrator.web.sessions import RevisionConflict, SessionService
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.schemas.session_purge import (
    DeleteSessionPermanentlyInput,
    PreviewSessionDeletionInput,
)
from pandrator_mcp.tools.session_purge import (
    delete_session_permanently,
    preview_session_deletion,
)


@pytest.fixture
def fixture(tmp_path):
    paths = DataPaths(tmp_path).ensure()
    upgrade_database(paths.database)
    database = Database(paths.database)
    queue = JobQueue(database)
    purge = SessionPurgeService(database, paths)
    sessions = SessionService(database)
    record = sessions.create("durable purge")
    (paths.sessions / record.storage_key).mkdir()
    trashed = sessions.trash(record.id, record.revision)
    app = Flask(__name__)

    class Guards:
        @staticmethod
        def require_scope(_scope):
            return lambda fn: fn

        @staticmethod
        def principal():
            return SimpleNamespace(subject="test-owner")

        @staticmethod
        def error_response(code, message, status, details=None):
            return jsonify({"code": code, "message": message, "details": details}), status

    services = SimpleNamespace(
        database=database, paths=paths, jobs=queue,
        idempotency=IdempotencyService(database, SecretRedactor(database)),
    )
    register_session_purge_job_routes(
        app, SimpleNamespace(services=services, guards=Guards())
    )
    yield SimpleNamespace(
        paths=paths, database=database, queue=queue, purge=purge,
        record=trashed, app=app, handlers=purge_job_handlers(database, paths),
    )
    database.dispose()


def _post(client, path, key, body=None):
    return client.post(path, json=body or {}, headers={"Idempotency-Key": key})


def test_inspection_admission_replay_and_public_fingerprint(fixture, monkeypatch):
    original = SessionPurgeService.preview
    worker_started = threading.Event()

    def slow_preview(service, target):
        assert worker_started.is_set(), "Preview ran inside the admission request."
        time.sleep(0.1)
        return original(service, target)

    monkeypatch.setattr(SessionPurgeService, "preview", slow_preview)
    client = fixture.app.test_client()
    path = f"/api/v1/sessions/{fixture.record.id}/purge-inspections"
    started = time.monotonic()
    admitted = _post(client, path, "inspect-key-0001")
    assert admitted.status_code == 202
    assert time.monotonic() - started < 2
    job_id = admitted.json["job_id"]
    assert _post(client, path, "inspect-key-0001").json["job_id"] == job_id
    mismatch = _post(client, path.replace(fixture.record.id, "other"), "inspect-key-0001")
    assert mismatch.status_code == 409
    assert client.get(f"/api/v1/purge-inspections/{job_id}").status_code == 202
    claimed = fixture.queue.claim("test-worker")
    assert claimed.id == job_id and claimed.session_id is None and claimed.workflow_run_id is None
    assert claimed.max_attempts == 3
    worker_started.set()
    result = fixture.handlers[INSPECTION_KIND](claimed.payload_json, lambda *_: None, threading.Event())
    assert "impact_token" not in result and len(result["impact_fingerprint"]) == 64
    fixture.queue.complete(job_id, "test-worker", result, lease_generation=claimed.lease_generation)
    stored = fixture.queue.get(job_id).result_json
    assert stored["impact_fingerprint"] == result["impact_fingerprint"]
    response = client.get(f"/api/v1/purge-inspections/{job_id}")
    assert response.status_code == 200
    assert response.json["inspection"]["impact_token"] == result["impact_fingerprint"]
    assert "impact_fingerprint" not in response.json["inspection"]


def test_slow_snapshot_inspection_allows_other_writers_and_rejects_stale_revision(
    fixture, monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    outcome: list[object] = []
    original = fixture.purge._inspect

    def paused_inspect(session, record, *, activity_blockers=None):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("Disposable preview was not released.")
        return original(session, record, activity_blockers=activity_blockers)

    monkeypatch.setattr(fixture.purge, "_inspect", paused_inspect)

    def inspect_in_thread():
        try:
            outcome.append(fixture.purge.preview(fixture.record.id))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=inspect_in_thread, daemon=True)
    thread.start()
    try:
        assert entered.wait(10), "Inspection did not enter its read snapshot."
        started = time.monotonic()
        unrelated = fixture.queue.enqueue("test.unrelated", {"marker": "concurrency"})
        assert unrelated.id
        assert time.monotonic() - started < 2
        started = time.monotonic()
        admitted = _post(
            fixture.app.test_client(),
            f"/api/v1/sessions/{fixture.record.id}/purge-inspections",
            "parallel-inspection-0001",
        )
        assert admitted.status_code == 202
        assert time.monotonic() - started < 2
        with fixture.database.immediate_session() as session:
            session.get(SessionRecord, fixture.record.id).revision += 1
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], RevisionConflict)


def test_new_journal_claim_overrides_claim_induced_revision_change(fixture, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    outcome: list[object] = []
    original = fixture.purge._inspect

    def paused_inspect(session, record, *, activity_blockers=None):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("Disposable preview was not released.")
        return original(session, record, activity_blockers=activity_blockers)

    def forbidden_preflight(*_args):
        raise AssertionError("A newly claimed purge must skip preview preflight.")

    monkeypatch.setattr(fixture.purge, "_inspect", paused_inspect)
    monkeypatch.setattr(fixture.purge, "_preflight_delete", forbidden_preflight)

    def inspect_in_thread():
        try:
            outcome.append(fixture.purge.preview(fixture.record.id))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=inspect_in_thread, daemon=True)
    thread.start()
    try:
        assert entered.wait(10), "Inspection did not enter its read snapshot."
        with fixture.database.immediate_session() as session:
            record = session.get(SessionRecord, fixture.record.id)
            session.add(SessionPurge(
                session_id=record.id, state="purging", manifest_json=[],
                expected_revision=record.revision, impact_token="b" * 64,
            ))
            record.status = "purging"
            record.revision += 1
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], dict)
    assert outcome[0]["revision"] == fixture.record.revision
    assert outcome[0]["impact_token"] == "b" * 64


def test_purge_work_survives_target_and_cancel_fence(fixture):
    client = fixture.app.test_client()
    preview = fixture.purge.preview(fixture.record.id)
    path = f"/api/v1/sessions/{fixture.record.id}/purge-admissions"
    body = {"expected_revision": preview["revision"],
            "impact_token": preview["impact_token"], "confirm": True}
    admitted = _post(client, path, "purge-key-0001", body)
    assert admitted.status_code == 202
    job_id = admitted.json["job_id"]
    assert _post(client, path, "purge-key-0001", body).json["job_id"] == job_id
    assert _post(client, path, "purge-key-0001", {**body, "impact_token": "a" * 64}).status_code == 409
    claimed = fixture.queue.claim("test-worker")
    assert claimed.id == job_id and claimed.session_id is None and claimed.workflow_run_id is None
    with pytest.raises(PurgeBlocked, match="purge_job_cancelled_or_unclaimed"):
        fixture.handlers[PURGE_KIND]({**claimed.payload_json, "_job_id": job_id,
                                      "_lease_generation": claimed.lease_generation + 1},
                                     lambda *_: None, threading.Event())
    with fixture.database.session() as session:
        assert session.get(SessionPurge, fixture.record.id) is None
    result = fixture.handlers[PURGE_KIND]({**claimed.payload_json, "_job_id": job_id,
                                           "_lease_generation": claimed.lease_generation},
                                           lambda *_: None, threading.Event())
    assert result["state"] == "complete"
    with fixture.database.session() as session:
        assert session.get(SessionRecord, fixture.record.id) is None
        assert session.get(Job, job_id) is not None
        assert session.get(SessionPurge, fixture.record.id).state == "complete"
    with pytest.raises(ValueError, match="cannot be cancelled"):
        fixture.queue.request_cancel(job_id)
    fixture.queue.complete(job_id, "test-worker", result, lease_generation=claimed.lease_generation)
    assert _post(client, path, "purge-key-0001", body).json["job_id"] == job_id


def test_queued_cancel_prevents_claim_and_completed_journal_replays(fixture):
    preview = fixture.purge.preview(fixture.record.id)
    job = fixture.queue.enqueue(
        PURGE_KIND, {"target_session_id": fixture.record.id,
                     "expected_revision": preview["revision"],
                     "impact_fingerprint": preview["impact_token"]},
        session_id=None, workflow_run_id=None, max_attempts=3,
    )
    fixture.queue.request_cancel(job.id)
    assert fixture.queue.get(job.id).status == "canceled"
    assert fixture.queue.claim("test-worker") is None
    with fixture.database.session() as session:
        assert session.get(SessionRecord, fixture.record.id) is not None


def test_failed_journal_raises_and_startup_sweep_converges(fixture, monkeypatch):
    file = fixture.paths.sessions / fixture.record.storage_key / "owned.txt"
    file.write_text("owned")
    preview = fixture.purge.preview(fixture.record.id)
    job = fixture.queue.enqueue(
        PURGE_KIND, {"target_session_id": fixture.record.id,
                     "expected_revision": preview["revision"],
                     "impact_fingerprint": preview["impact_token"]},
        session_id=None, workflow_run_id=None, max_attempts=3,
    )
    claimed = fixture.queue.claim("test-worker")
    assert claimed.id == job.id
    original = SessionPurgeService._remove_managed_entry

    def failed_unlink(*_args, **_kwargs):
        raise OSError("disposable unlink failure")

    monkeypatch.setattr(SessionPurgeService, "_remove_managed_entry", failed_unlink)
    with pytest.raises(RuntimeError, match="disposable unlink failure"):
        fixture.handlers[PURGE_KIND]({**claimed.payload_json, "_job_id": job.id,
                                     "_lease_generation": claimed.lease_generation},
                                     lambda *_: None, threading.Event())
    with fixture.database.session() as session:
        assert session.get(SessionPurge, fixture.record.id).state == "failed"
        assert session.get(SessionRecord, fixture.record.id) is not None
    with pytest.raises(ValueError, match="cannot be cancelled"):
        fixture.queue.request_cancel(job.id)
    assert fixture.queue.fail(
        job.id, "test-worker", "RuntimeError", "disposable unlink failure",
        lease_generation=claimed.lease_generation,
    )
    monkeypatch.setattr(SessionPurgeService, "_remove_managed_entry", original)
    assert fixture.purge.sweep(limit=1)["complete"] == 1
    reclaimed = fixture.queue.claim("test-worker")
    assert reclaimed.id == job.id and reclaimed.lease_generation > claimed.lease_generation
    result = fixture.handlers[PURGE_KIND]({**reclaimed.payload_json, "_job_id": job.id,
                                           "_lease_generation": reclaimed.lease_generation},
                                           lambda *_: None, threading.Event())
    assert result["state"] == "complete" and not file.exists()
    fixture.queue.complete(job.id, "test-worker", result, lease_generation=reclaimed.lease_generation)
    assert fixture.queue.get(job.id).status == "succeeded"


def test_repeated_failed_journal_becomes_failed_work(fixture, monkeypatch):
    file = fixture.paths.sessions / fixture.record.storage_key / "owned.txt"
    file.write_text("owned")
    preview = fixture.purge.preview(fixture.record.id)
    job = fixture.queue.enqueue(
        PURGE_KIND, {"target_session_id": fixture.record.id,
                     "expected_revision": preview["revision"],
                     "impact_fingerprint": preview["impact_token"]},
        session_id=None, workflow_run_id=None, max_attempts=3,
    )

    def failed_unlink(*_args, **_kwargs):
        raise OSError("disposable unlink failure")

    monkeypatch.setattr(SessionPurgeService, "_remove_managed_entry", failed_unlink)
    for attempt in range(3):
        claimed = fixture.queue.claim("test-worker")
        assert claimed.id == job.id and claimed.attempts == attempt + 1
        with pytest.raises(RuntimeError, match="disposable unlink failure"):
            fixture.handlers[PURGE_KIND]({**claimed.payload_json, "_job_id": job.id,
                                          "_lease_generation": claimed.lease_generation},
                                         lambda *_: None, threading.Event())
        assert fixture.queue.fail(
            job.id, "test-worker", "RuntimeError", "disposable unlink failure",
            lease_generation=claimed.lease_generation,
        )
    assert fixture.queue.get(job.id).status == "failed"
    with fixture.database.session() as session:
        assert session.get(SessionPurge, fixture.record.id).state == "failed"
        assert session.get(SessionRecord, fixture.record.id) is not None


def test_mcp_admission_default_and_explicit_legacy_path():
    application = Mock()
    application.admit_session_purge_inspection.return_value = {"job_id": "inspection-1", "state": "queued"}
    application.admit_session_purge.return_value = {"job_id": "deletion-1", "state": "queued"}
    application.preview_session_deletion.return_value = {"can_purge": True}
    application.delete_session_permanently.return_value = {"state": "complete"}
    runtime = Mock()
    runtime.require_application.return_value = application

    inspection = preview_session_deletion(runtime, PreviewSessionDeletionInput(session_id="s"))
    assert inspection.work.id == "inspection-1"
    assert inspection.result["idempotency_key"].startswith("purge-inspection:")
    assert inspection.next_actions[0].arguments["work_id"] == "inspection-1"
    token = "c" * 64
    arguments = DeleteSessionPermanentlyInput(
        session_id="s", expected_revision=2, impact_token=token, confirm=True,
    )
    first = delete_session_permanently(runtime, arguments)
    second = delete_session_permanently(runtime, arguments)
    assert first.work.id == "deletion-1"
    assert first.result["idempotency_key"] == second.result["idempotency_key"]
    assert application.admit_session_purge.call_args.kwargs["idempotency_key"] == first.result["idempotency_key"]

    assert preview_session_deletion(runtime, PreviewSessionDeletionInput(
        session_id="s", admission=False,
    )).result == {"can_purge": True}
    assert delete_session_permanently(runtime, DeleteSessionPermanentlyInput(
        session_id="s", expected_revision=2, impact_token=token,
        confirm=True, admission=False,
    )).result == {"state": "complete"}


def test_mcp_client_uses_durable_routes_and_guard_body():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"job_id": "job-1", "state": "queued"})
    client.admit_session_purge_inspection("s/a", idempotency_key="inspect-0001")
    assert client._request_json.call_args.args == (
        "/api/v1/sessions/s%2Fa/purge-inspections",
    )
    assert client._request_json.call_args.kwargs == {
        "method": "POST", "body": {}, "idempotency_key": "inspect-0001",
    }
    client.admit_session_purge(
        "s/a", expected_revision=4, impact_token="c" * 64,
        idempotency_key="delete-0001",
    )
    assert client._request_json.call_args.args == (
        "/api/v1/sessions/s%2Fa/purge-admissions",
    )
    assert client._request_json.call_args.kwargs == {
        "method": "POST",
        "body": {"expected_revision": 4, "impact_token": "c" * 64, "confirm": True},
        "idempotency_key": "delete-0001",
    }
    client.get_session_purge_inspection("job/1")
    assert client._request_json.call_args.args == (
        "/api/v1/purge-inspections/job%2F1",
    )
