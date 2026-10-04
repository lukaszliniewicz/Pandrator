"""Update preparation pauses queued work without discarding it."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import Job, JobEvent, utcnow
from pandrator_installer.update_operation import UpdateOperation
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def queue_owner(tmp_path: Path) -> Iterator[tuple[Database, JobQueue]]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield database, JobQueue(database)
    finally:
        database.dispose()


def snapshot(database: Database) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with database.session() as session:
        jobs = list(
            session.execute(
                select(
                    Job.id,
                    Job.status,
                    Job.attempts,
                    Job.lease_generation,
                    Job.lease_owner,
                    Job.lease_expires_at,
                    Job.started_at,
                    Job.finished_at,
                    Job.updated_at,
                ).order_by(Job.id)
            ).tuples()
        )
        events = list(
            session.execute(
                select(
                    JobEvent.id,
                    JobEvent.event_type,
                ).order_by(JobEvent.id)
            ).tuples()
        )
    return jobs, events


def test_preparing_update_pauses_and_resumes_real_worker(
    queue_owner: tuple[Database, JobQueue],
) -> None:
    database, queue = queue_owner
    first = queue.enqueue("fixture", {"label": "first"})
    second = queue.enqueue("fixture", {"label": "second"})
    entered: list[str] = []

    def handler(
        payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel: threading.Event,
    ) -> dict[str, Any]:
        entered.append(payload["label"])
        return {"label": payload["label"]}

    worker = Worker(queue, "fixture-owner", {"fixture": handler})
    assert worker.run_once()
    assert entered == ["first"]
    before = snapshot(database)
    with UpdateOperation(database.path.parent, "fixture"):
        assert not worker.run_once()
        assert snapshot(database) == before
        assert entered == ["first"]
    assert worker.run_once()
    assert entered == ["first", "second"]
    with database.session() as session:
        for job_id in (first.id, second.id):
            job = session.get(Job, job_id)
            assert job is not None and job.status == "succeeded"
            assert job.attempts == 1


@pytest.mark.parametrize("expired", [False, True])
def test_marker_at_native_transaction_entry_prevents_claim_and_reconciliation(
    queue_owner: tuple[Database, JobQueue], expired: bool
) -> None:
    database, queue = queue_owner
    job = queue.enqueue("fixture")
    if expired:
        owned = queue.claim("original-owner")
        assert owned is not None and owned.id == job.id
        with database.session() as session:
            record = session.get(Job, job.id)
            assert record is not None
            record.lease_expires_at = utcnow() - timedelta(seconds=1)
    before = snapshot(database)
    native = database.immediate_session

    @contextmanager
    def marked_transaction() -> Iterator[Session]:
        with UpdateOperation(database.path.parent, "fixture"):
            with native() as session:
                yield session

    with patch.object(database, "immediate_session", side_effect=marked_transaction):
        assert queue.claim("replacement-owner") is None
    assert snapshot(database) == before
    after = queue.claim("replacement-owner")
    if expired:
        assert after is None
        with database.session() as session:
            record = session.get(Job, job.id)
            assert record is not None and record.status == "failed"
            assert record.error_code == "worker_lease_expired"
    else:
        assert after is not None and after.id == job.id
        assert after.attempts == 1


def test_expired_running_lease_is_preserved_then_reclaimed(
    queue_owner: tuple[Database, JobQueue],
) -> None:
    database, queue = queue_owner
    job = queue.enqueue("fixture", max_attempts=3)
    original = queue.claim("original-owner")
    assert original is not None and original.id == job.id
    with database.session() as session:
        record = session.get(Job, job.id)
        assert record is not None
        record.lease_expires_at = utcnow() - timedelta(seconds=1)
    before = snapshot(database)
    with UpdateOperation(database.path.parent, "fixture"):
        assert queue.claim("replacement-owner") is None
        assert snapshot(database) == before
    reclaimed = queue.claim("replacement-owner")
    assert reclaimed is not None and reclaimed.id == job.id
    assert reclaimed.lease_generation == original.lease_generation + 1
    assert reclaimed.attempts == original.attempts + 1
    assert reclaimed.lease_owner == "replacement-owner"
    with database.session() as session:
        assert list(
            session.scalars(
                select(JobEvent.event_type).where(JobEvent.job_id == job.id).order_by(JobEvent.id)
            )
        ) == ["job.queued", "job.started", "job.reclaimed"]


def test_expired_pending_cancellation_is_preserved_then_reconciled(
    queue_owner: tuple[Database, JobQueue],
) -> None:
    database, queue = queue_owner
    job = queue.enqueue("fixture")
    owned = queue.claim("original-owner")
    assert owned is not None
    queue.request_cancel(job.id)
    with database.session() as session:
        record = session.get(Job, job.id)
        assert record is not None
        record.lease_expires_at = utcnow() - timedelta(seconds=1)
    before = snapshot(database)
    with UpdateOperation(database.path.parent, "fixture"):
        assert queue.claim("replacement-owner") is None
        assert snapshot(database) == before
    assert queue.claim("replacement-owner") is None
    with database.session() as session:
        record = session.get(Job, job.id)
        assert record is not None and record.status == "canceled"
        assert record.lease_owner is None and record.lease_expires_at is None
        assert record.finished_at is not None
        assert record.attempts == owned.attempts
        assert list(
            session.scalars(
                select(JobEvent.event_type).where(JobEvent.job_id == job.id).order_by(JobEvent.id)
            )
        ) == ["job.queued", "job.started", "job.cancel_requested", "job.canceled"]


def test_existing_owner_heartbeat_and_cancellation_remain_valid_during_update(
    queue_owner: tuple[Database, JobQueue],
) -> None:
    database, queue = queue_owner
    job = queue.enqueue("fixture")
    owned = queue.claim("original-owner", lease_seconds=5)
    assert owned is not None and owned.id == job.id
    queued = queue.enqueue("fixture")
    with database.session() as session:
        record = session.get(Job, job.id)
        assert record is not None and record.lease_expires_at is not None
        original_expiry = record.lease_expires_at
        queued_before = tuple(
            session.execute(select(Job.__table__).where(Job.id == queued.id)).one()
        )
    with UpdateOperation(database.path.parent, "fixture"):
        assert queue.heartbeat(
            job.id, "original-owner", lease_generation=owned.lease_generation, lease_seconds=60
        )
        with database.session() as session:
            record = session.get(Job, job.id)
            assert record is not None and record.lease_expires_at is not None
            assert record.lease_expires_at > original_expiry
        pending = queue.request_cancel(job.id)
        assert pending.status == "cancel_requested"
        assert pending.lease_owner == owned.lease_owner
        assert pending.lease_generation == owned.lease_generation
        assert queue.should_cancel(
            job.id, "original-owner", lease_generation=owned.lease_generation
        )
        assert queue.cancel_owned(job.id, "original-owner", lease_generation=owned.lease_generation)
        assert queue.claim("replacement-owner") is None
        with database.session() as session:
            assert (
                tuple(session.execute(select(Job.__table__).where(Job.id == queued.id)).one())
                == queued_before
            )
            assert list(
                session.scalars(
                    select(JobEvent.event_type)
                    .where(JobEvent.job_id == job.id)
                    .order_by(JobEvent.id)
                )
            ) == ["job.queued", "job.started", "job.cancel_requested", "job.canceled"]
    resumed = queue.claim("replacement-owner")
    assert resumed is not None and resumed.id == queued.id


def test_marker_in_another_root_does_not_pause_this_queue(
    queue_owner: tuple[Database, JobQueue],
) -> None:
    database, queue = queue_owner
    job = queue.enqueue("fixture")
    other_root = database.path.parent / "separate-installation"
    with UpdateOperation(other_root, "fixture"):
        owned = queue.claim("fixture-owner")
        assert owned is not None and owned.id == job.id
