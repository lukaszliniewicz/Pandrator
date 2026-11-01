from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

import pandrator.web.jobs as jobs_module
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.model_base import as_utc
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    JobEvent,
    Project,
)
from pandrator.web.sessions import SessionService
from pandrator.web.source_document_reads import list_session_documents
from pandrator.web.work import WorkService
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def runtime(tmp_path: Path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield database, JobQueue(database)
    finally:
        database.dispose()


def test_cancel_storage_held_job_clears_retry_projection_and_prevents_claim(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, queue = runtime
    project_session = SessionService(database).create("Storage cancellation test")
    job = queue.enqueue(
        "storage.cancel.test", session_id=project_session.id, max_attempts=3
    )
    claimed = queue.claim("first-worker")
    assert claimed is not None and claimed.id == job.id
    assert queue.defer_for_storage(
        job.id,
        "first-worker",
        lease_generation=claimed.lease_generation,
        detail="Waiting for output space.",
        retry_delay_seconds=60,
    )

    work = WorkService(queue, SecretRedactor(database))
    held_view = work.list(session_id=project_session.id, states=("queued",))[0]
    assert held_view.state == "queued"
    assert held_view.error is not None and held_view.error.code == "storage_pressure"
    assert held_view.detail == "Waiting for output space."
    assert held_view.available_at is not None

    held = queue.get(job.id)
    attempts_before_cancel = held.attempts
    deferred_until = held.available_at
    assert deferred_until is not None
    canceled = queue.request_cancel(job.id)

    assert canceled.status == "canceled"
    assert canceled.finished_at is not None
    assert canceled.attempts == attempts_before_cancel
    assert canceled.error_code is None
    assert canceled.error_message is None
    assert canceled.available_at is None
    assert canceled.progress_detail is None

    canceled_view = work.list(session_id=project_session.id, states=("cancelled",))[0]
    projected = canceled_view.model_dump(mode="json")
    assert projected["state"] == "cancelled"
    assert projected["error"] is None
    assert projected["detail"] is None
    assert "available_at" not in projected
    assert canceled_view.poll_after_ms == 0

    with database.session() as session:
        event_types = list(
            session.scalars(
                select(JobEvent.event_type)
                .where(JobEvent.job_id == job.id)
                .order_by(JobEvent.id)
            )
        )
    assert "job.waiting_for_storage" in event_types
    assert "job.canceled" in event_types

    # The retry window has elapsed, representing recovered capacity. The canceled
    # row must still be ineligible for acquisition and must not spend an attempt.
    monkeypatch.setattr(
        jobs_module,
        "utcnow",
        lambda: deferred_until + timedelta(seconds=1),
    )
    handler_calls: list[str] = []
    worker = Worker(
        queue,
        "recovered-capacity-worker",
        {"storage.cancel.test": lambda *_args: handler_calls.append("ran") or {}},
    )
    assert not worker.run_once()
    assert handler_calls == []
    after_retry_window = queue.get(job.id)
    assert after_retry_window.status == "canceled"
    assert after_retry_window.attempts == attempts_before_cancel
    assert after_retry_window.error_code is None
    assert after_retry_window.error_message is None
    assert after_retry_window.available_at is None
    assert after_retry_window.progress_detail is None


def test_running_job_cancel_keeps_lease_for_worker_acknowledgement(runtime) -> None:
    _database, queue = runtime
    enqueued = queue.enqueue("running.cancel.test", {}, max_attempts=2)
    claimed = queue.claim("lease-owner")
    assert claimed is not None and claimed.id == enqueued.id

    requested = queue.request_cancel(enqueued.id)

    assert requested.status == "cancel_requested"
    assert requested.lease_owner == "lease-owner"
    assert as_utc(requested.lease_expires_at) == as_utc(claimed.lease_expires_at)
    assert requested.lease_generation == claimed.lease_generation
    assert queue.should_cancel(
        enqueued.id, "lease-owner", lease_generation=claimed.lease_generation
    )
    assert queue.cancel_owned(
        enqueued.id, "lease-owner", lease_generation=claimed.lease_generation
    )
    assert queue.get(enqueued.id).status == "canceled"


def test_document_history_timestamps_normalize_sqlite_naive_utc(runtime) -> None:
    database, _queue = runtime
    record = SessionService(database).create("Document timestamp test")
    naive_utc = datetime(2026, 10, 31, 9, 15, 0)

    with database.immediate_session() as session:
        project = session.get(Project, record.project_id)
        assert project is not None
        document = Document(
            session_id=record.id,
            stage="transcription",
            language="en",
            created_at=naive_utc,
        )
        session.add(document)
        session.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash="timestamp-fixture",
            created_at=naive_utc,
        )
        session.add(revision)
        session.flush()
        artifact = Artifact(
            workspace_id=project.workspace_id,
            project_id=record.project_id,
            session_id=record.id,
            kind="text",
            role="revision",
            relative_path="timestamp-fixture.txt",
            metadata_json={"revision_id": revision.id},
            created_at=naive_utc,
        )
        session.add(artifact)

    payload = list_session_documents(database, record.id)
    item = payload["items"][0]
    projected_revision = item["revisions"][0]
    expected = naive_utc.replace(tzinfo=UTC).isoformat()

    assert item["created_at"] == expected
    assert projected_revision["created_at"] == expected
    assert projected_revision["artifact"]["created_at"] == expected
    for value in (
        item["created_at"],
        projected_revision["created_at"],
        projected_revision["artifact"]["created_at"],
    ):
        assert datetime.fromisoformat(value).utcoffset() == timedelta(0)
