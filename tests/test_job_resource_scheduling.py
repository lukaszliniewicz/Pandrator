"""Resource waiters must not starve independent durable work."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import event

from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import Job, ResourceClaim, utcnow
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def queue(tmp_path):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    try:
        yield JobQueue(database), database
    finally:
        database.dispose()


def hold(queue, keys):
    job = queue.enqueue("hold", resource_keys=keys, max_attempts=2)
    claimed = queue.claim("holder")
    assert claimed is not None and claimed.id == job.id
    assert queue.acquire_resources(
        job.id, "holder", keys, lease_generation=claimed.lease_generation
    )
    return claimed


def test_scan_passes_more_than_one_page_of_waiters_without_spending_attempts(queue):
    jobs, database = queue
    owner = hold(jobs, ["service:tts:xtts"])
    # Exercise a legacy alias held by a still-running older worker.
    with database.immediate_session() as unit:
        claim = unit.get(ResourceClaim, "service:tts:xtts")
        assert claim is not None
        claim.resource_key = "service:tts:XTTS"
        waiters = [
            jobs.enqueue_in_session(unit, "blocked", resource_keys=["service:tts:xtts"])
            for _ in range(150)
        ]
    independent = jobs.enqueue("render", resource_keys=["session:independent"])
    chosen = jobs.claim("other-worker")
    assert chosen is not None and chosen.id == independent.id
    with database.snapshot_session() as unit:
        stored_waiters = []
        for waiter in waiters:
            stored = unit.get(Job, waiter.id)
            assert stored is not None
            assert (stored.status, stored.attempts, stored.available_at) == ("queued", 0, None)
            stored_waiters.append(stored)
        # The queue is first in, first out by (created_at, id). Waiters enqueued
        # within one clock tick share created_at (about 15 ms on Windows), so the
        # first in line is the lowest id among them, not necessarily waiters[0].
        first = min(stored_waiters, key=lambda job: (job.created_at, job.id)).id
    jobs.release_resources(owner.id, "holder", lease_generation=owner.lease_generation)
    resumed = jobs.claim("other-worker")
    assert resumed is not None and resumed.id == first


def test_all_blocked_poll_does_not_take_writer_lock(queue):
    jobs, database = queue
    hold(jobs, ["gpu:0"])
    waiter = jobs.enqueue("blocked", resource_keys=["gpu:0"])
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(database.engine, "before_cursor_execute", observe)
    try:
        assert jobs.claim("idle-worker") is None
    finally:
        event.remove(database.engine, "before_cursor_execute", observe)
    assert "BEGIN IMMEDIATE" not in statements
    assert jobs.get(waiter.id).attempts == 0


def test_empty_queue_retains_one_read_and_no_writer_or_resource_scan(queue):
    jobs, database = queue
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(database.engine, "before_cursor_execute", observe)
    try:
        assert jobs.claim("idle-worker") is None
    finally:
        event.remove(database.engine, "before_cursor_execute", observe)
    reads = [statement for statement in statements if statement.startswith("SELECT")]
    assert len(reads) == 1
    assert "RESOURCE_CLAIMS" not in reads[0]
    assert "BEGIN IMMEDIATE" not in statements


@pytest.mark.parametrize("held_key,requested_key", [
    ("gpu:cuda", "gpu:vulkan"), ("gpu:0", "gpu:default"),
    ("gpu:metal", "gpu:0"), ("service:tts:Audio.cpp", "service:tts:audio_cpp"),
])
def test_aliases_cannot_acquire_one_device_or_service_twice(queue, held_key, requested_key):
    jobs, _database = queue
    holder = jobs.enqueue("first", resource_keys=[held_key])
    contender = jobs.enqueue("second", resource_keys=[requested_key])
    first, second = jobs.claim("first"), jobs.claim("second")
    assert first is not None and second is not None
    assert jobs.acquire_resources(holder.id, "first", [held_key], lease_generation=first.lease_generation)
    assert not jobs.acquire_resources(
        contender.id, "second", [requested_key], lease_generation=second.lease_generation,
    )
    assert jobs.defer_for_resources(contender.id, "second", lease_generation=second.lease_generation, retry_delay_seconds=0)
    assert jobs.claim("second") is None


def test_expired_claim_does_not_hide_queued_work(queue):
    jobs, database = queue
    hold(jobs, ["gpu:0"])
    with database.immediate_session() as unit:
        claim = unit.get(ResourceClaim, "gpu:0")
        assert claim is not None
        claim.expires_at = utcnow() - timedelta(seconds=1)
    waiter = jobs.enqueue("next", resource_keys=["gpu:0"])
    chosen = jobs.claim("next-worker")
    assert chosen is not None and chosen.id == waiter.id
    assert jobs.acquire_resources(
        chosen.id, "next-worker", chosen.resource_keys_json,
        lease_generation=chosen.lease_generation,
    )


def test_reclaim_preserves_generation_fence_even_with_own_old_claim(queue):
    jobs, database = queue
    owner = hold(jobs, ["gpu:0"])
    with database.immediate_session() as unit:
        job = unit.get(Job, owner.id)
        assert job is not None
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
    replacement = jobs.claim("replacement")
    assert replacement is not None and replacement.id == owner.id
    assert replacement.lease_generation == owner.lease_generation + 1
    assert not jobs.heartbeat(owner.id, "holder", lease_generation=owner.lease_generation)
    assert jobs.acquire_resources(
        owner.id, "replacement", replacement.resource_keys_json,
        lease_generation=replacement.lease_generation,
    )


def test_session_serialization_is_retained_for_disjoint_resources(queue):
    jobs, database = queue
    record = SessionService(database).create("One session")
    jobs.enqueue("first", session_id=record.id, resource_keys=["first:resource"])
    waiter = jobs.enqueue("second", session_id=record.id, resource_keys=["second:resource"])
    independent = jobs.enqueue("independent")
    assert jobs.claim("first-worker") is not None
    chosen = jobs.claim("second-worker")
    assert chosen is not None and chosen.id == independent.id
    assert jobs.get(waiter.id).attempts == 0


def test_independent_workers_overlap_cloud_and_local_work_behind_waiter(queue):
    jobs, database = queue
    cloud = jobs.enqueue("cloud", resource_keys=["service:tts:openai"])
    blocked = jobs.enqueue("cloud", resource_keys=["service:tts:openai"])
    local = jobs.enqueue("local", resource_keys=["service:tts:xtts", "gpu:0"])
    cloud_started, local_started, finish = Event(), Event(), Event()
    other_database = Database(database.path)

    def cloud_handler(_payload, _progress, _cancel):
        cloud_started.set()
        assert finish.wait(10)
        return {"cloud": True}

    def local_handler(_payload, _progress, _cancel):
        assert cloud_started.is_set() and not finish.is_set()
        local_started.set()
        return {"local": True}

    first = Worker(jobs, "cloud-worker", {"cloud": cloud_handler})
    second = Worker(JobQueue(other_database), "local-worker", {"local": local_handler})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            cloud_future = executor.submit(first.run_once)
            try:
                assert cloud_started.wait(10)
                local_future = executor.submit(second.run_once)
                assert local_future.result(timeout=10)
                assert local_started.is_set()
                assert jobs.get(cloud.id).status == "running"
                assert jobs.get(local.id).status == "succeeded"
                assert jobs.get(blocked.id).attempts == 0
            finally:
                finish.set()
            assert cloud_future.result(timeout=10)
        assert jobs.get(cloud.id).status == "succeeded"
    finally:
        finish.set()
        other_database.dispose()
