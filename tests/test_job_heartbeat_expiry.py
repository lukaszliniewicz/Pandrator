"""A current generation cannot revive a job after its durable lease expires."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import delete, event, func, select

from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import Job, JobEvent, ResourceClaim, utcnow
from tests.web_test_support import prepare_web_test_data_root


@pytest.mark.parametrize("expiry", ["past", "missing"])
def test_expired_or_missing_lease_cannot_renew_job_progress_or_resource_claims(tmp_path, expiry):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    try:
        queue = JobQueue(database)
        job = queue.enqueue("noop", resource_keys=["synthetic:lease-expiry"])
        claimed = queue.claim("expiry-worker")
        assert claimed and claimed.id == job.id
        generation = claimed.lease_generation
        assert queue.acquire_resources(
            job.id, "expiry-worker", ["synthetic:lease-expiry"], lease_generation=generation
        )
        with database.immediate_session() as session:
            stored = session.get(Job, job.id)
            assert stored is not None
            stored.lease_expires_at = utcnow() - timedelta(seconds=1) if expiry == "past" else None
        with database.snapshot_session() as session:
            stored = session.get(Job, job.id)
            claim = session.get(ResourceClaim, "synthetic:lease-expiry")
            assert stored and claim
            original = (
                stored.lease_expires_at,
                stored.progress,
                stored.progress_detail,
                claim.expires_at,
            )
            events_before = session.scalar(select(func.count()).select_from(JobEvent))
        assert (
            queue.heartbeat(
                job.id,
                "expiry-worker",
                lease_generation=generation,
                progress=0.9,
                detail="must not publish progress",
            )
            is False
        )
        assert (
            queue.heartbeat_resources(job.id, "expiry-worker", lease_generation=generation) is False
        )
        with database.snapshot_session() as session:
            stored = session.get(Job, job.id)
            claim = session.get(ResourceClaim, "synthetic:lease-expiry")
            assert stored and claim
            assert (
                stored.lease_expires_at,
                stored.progress,
                stored.progress_detail,
                claim.expires_at,
            ) == original
            assert session.scalar(select(func.count()).select_from(JobEvent)) == events_before
    finally:
        database.dispose()


def test_live_matching_lease_still_renews_without_an_additional_read(tmp_path):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    try:
        queue = JobQueue(database)
        job = queue.enqueue("noop")
        claimed = queue.claim("live-worker")
        assert claimed and claimed.id == job.id
        reads = []

        def record_read(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().lower().startswith("select"):
                reads.append(statement)

        event.listen(database.engine, "before_cursor_execute", record_read)
        try:
            assert queue.heartbeat(
                job.id,
                "live-worker",
                lease_generation=claimed.lease_generation,
                progress=0.5,
                detail="Halfway",
            )
        finally:
            event.remove(database.engine, "before_cursor_execute", record_read)
        assert len(reads) == 1
        with database.snapshot_session() as session:
            stored = session.get(Job, job.id)
            assert stored and stored.progress == 0.5 and stored.progress_detail == "Halfway"
    finally:
        database.dispose()


@pytest.mark.parametrize("expiry", ["past", "missing"])
def test_expired_owner_cannot_acquire_new_resource_claims(tmp_path, expiry):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    try:
        queue = JobQueue(database)
        job = queue.enqueue("noop", resource_keys=["synthetic:acquire"])
        claimed = queue.claim("worker")
        assert claimed is not None
        with database.immediate_session() as unit:
            stored = unit.get(Job, job.id)
            assert stored is not None
            stored.lease_expires_at = utcnow() - timedelta(seconds=1) if expiry == "past" else None
        assert not queue.acquire_resources(
            job.id, "worker", ["synthetic:acquire"],
            lease_generation=claimed.lease_generation,
        )
        with database.snapshot_session() as unit:
            assert unit.scalar(select(func.count()).select_from(ResourceClaim)) == 0
    finally:
        database.dispose()


def test_resource_renewal_checks_all_keys_and_normalizes_legacy_aliases(tmp_path):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    try:
        queue = JobQueue(database)
        required = ["gpu:0", "service:tts:xtts"]
        job = queue.enqueue("noop", resource_keys=required)
        claimed = queue.claim("worker")
        assert claimed is not None
        generation = claimed.lease_generation
        assert queue.acquire_resources(job.id, "worker", required, lease_generation=generation)
        with database.immediate_session() as unit:
            resource = unit.get(ResourceClaim, "service:tts:xtts")
            assert resource is not None
            resource.resource_key = "service:tts:XTTS"
        assert queue.heartbeat_resources(
            job.id, "worker", lease_generation=generation, expected_keys=required,
        )
        with database.immediate_session() as unit:
            unit.execute(delete(ResourceClaim).where(ResourceClaim.resource_key == "gpu:0"))
        assert not queue.heartbeat_resources(
            job.id, "worker", lease_generation=generation, expected_keys=required,
        )
    finally:
        database.dispose()


def test_worker_stops_after_resource_authority_is_lost(tmp_path):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    try:
        queue = JobQueue(database)
        job = queue.enqueue("noop", resource_keys=["synthetic:required"])

        def handler(_payload, progress, cancel_event):
            progress(0.1, "check resources")
            assert cancel_event.is_set()
            return {"must_not_succeed": True}

        worker = Worker(queue, "worker", {"noop": handler})
        with patch("pandrator.web.jobs.threading.Thread"), patch.object(
            queue, "heartbeat_resources", return_value=False,
        ):
            assert worker.run_once()
        stored = queue.get(job.id)
        assert (stored.status, stored.error_code) == ("failed", "worker_lease_lost")
        with database.snapshot_session() as unit:
            assert unit.scalar(select(func.count()).select_from(ResourceClaim)) == 0
    finally:
        database.dispose()


def test_expired_resource_cannot_be_revived_by_a_live_job(tmp_path):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    try:
        queue = JobQueue(database)
        key = "synthetic:expired-resource"
        job = queue.enqueue("noop", resource_keys=[key])
        claimed = queue.claim("worker")
        assert claimed is not None
        assert queue.acquire_resources(job.id, "worker", [key], lease_generation=claimed.lease_generation)
        with database.immediate_session() as unit:
            claim = unit.get(ResourceClaim, key)
            assert claim is not None
            claim.expires_at = utcnow() - timedelta(seconds=1)
            expired_at = claim.expires_at
        assert not queue.heartbeat_resources(
            job.id, "worker", lease_generation=claimed.lease_generation, expected_keys=[key],
        )
        with database.snapshot_session() as unit:
            claim = unit.get(ResourceClaim, key)
            assert claim is not None
            assert claim.expires_at.replace(tzinfo=None) == expired_at.replace(tzinfo=None)
    finally:
        database.dispose()
