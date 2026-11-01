from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select

import pandrator.web.jobs as jobs
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import AgentRun, Job, JobEvent, utcnow
from pandrator.web.projects.copy_contracts import PROJECT_COPY_JOB_KIND
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def runtime(tmp_path: Path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield database, JobQueue(database)
    finally:
        database.dispose()


def _claimed_job(
    database: Database,
    queue: JobQueue,
    *,
    kind: str = "lease.fence.test",
    max_attempts: int = 2,
) -> tuple[Job, str, tuple[str, str]]:
    session_id = SessionService(database).create("Lease fencing test").id
    enqueued = queue.enqueue(kind, session_id=session_id, max_attempts=max_attempts)
    claimed = queue.claim("owner")
    assert claimed is not None and claimed.id == enqueued.id
    with database.session() as session:
        active = AgentRun(
            kind="translation",
            session_id=session_id,
            job_id=claimed.id,
            status="running",
        )
        completed = AgentRun(
            kind="correction",
            session_id=session_id,
            job_id=claimed.id,
            status="completed",
        )
        session.add_all([active, completed])
        session.flush()
        run_ids = (active.id, completed.id)
    return claimed, session_id, run_ids


def _set_lease(
    database: Database,
    job_id: str,
    state: str,
) -> None:
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        if state == "expired":
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
        elif state == "missing":
            job.lease_expires_at = None
        else:
            raise AssertionError(f"Unexpected lease state: {state}")


def _snapshot(database: Database, job_id: str):
    """Read durable queue state directly, without queue reconciliation."""
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job_state = (
            job.status,
            job.lease_owner,
            job.lease_expires_at,
            job.lease_generation,
            job.attempts,
            job.max_attempts,
            job.available_at,
            job.progress,
            job.progress_detail,
            job.result_json,
            job.error_code,
            job.error_message,
            job.finished_at,
            job.updated_at,
        )
        events = tuple(
            (row.id, row.event_type, row.payload_json)
            for row in session.scalars(
                select(JobEvent)
                .where(JobEvent.job_id == job_id)
                .order_by(JobEvent.id)
            )
        )
        runs = tuple(
            (row.id, row.status, row.updated_at)
            for row in session.scalars(
                select(AgentRun)
                .where(AgentRun.job_id == job_id)
                .order_by(AgentRun.id)
            )
        )
    return job_state, events, runs


@pytest.mark.parametrize("lease_state", ["expired", "missing"])
def test_expired_or_missing_lease_cannot_complete_through_wrapper(
    runtime, lease_state: str
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)
    _set_lease(database, claimed.id, lease_state)
    before = _snapshot(database, claimed.id)

    with pytest.raises(RuntimeError, match="^Job lease is no longer owned by this worker\\.$"):
        queue.complete(
            claimed.id,
            "owner",
            {"result": "late"},
            lease_generation=claimed.lease_generation,
        )

    assert _snapshot(database, claimed.id) == before


@pytest.mark.parametrize("lease_state", ["expired", "missing"])
def test_expired_or_missing_lease_cannot_complete_in_session(
    runtime, lease_state: str
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)
    _set_lease(database, claimed.id, lease_state)
    before = _snapshot(database, claimed.id)

    with pytest.raises(RuntimeError, match="^Job lease is no longer owned by this worker\\.$"):
        with database.immediate_session() as session:
            queue.complete_in_session(
                session,
                claimed.id,
                "owner",
                {"result": "late"},
                lease_generation=claimed.lease_generation,
            )

    assert _snapshot(database, claimed.id) == before


@pytest.mark.parametrize("lease_state", ["expired", "missing"])
@pytest.mark.parametrize("max_attempts", [1, 2])
def test_expired_or_missing_lease_cannot_fail_or_schedule_retry(
    runtime, lease_state: str, max_attempts: int
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue, max_attempts=max_attempts)
    _set_lease(database, claimed.id, lease_state)
    before = _snapshot(database, claimed.id)

    assert not queue.fail(
        claimed.id,
        "owner",
        "late_failure",
        "Late failure",
        lease_generation=claimed.lease_generation,
    )

    assert _snapshot(database, claimed.id) == before


@pytest.mark.parametrize("lease_state", ["expired", "missing"])
def test_expired_or_missing_lease_cannot_defer_for_resources(
    runtime, lease_state: str
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)
    _set_lease(database, claimed.id, lease_state)
    before = _snapshot(database, claimed.id)

    assert not queue.defer_for_resources(
        claimed.id,
        "owner",
        lease_generation=claimed.lease_generation,
        retry_delay_seconds=0.1,
    )

    assert _snapshot(database, claimed.id) == before


@pytest.mark.parametrize("lease_state", ["expired", "missing"])
@pytest.mark.parametrize("job_status", ["running", "cancel_requested"])
def test_expired_or_missing_lease_cannot_acknowledge_cancellation(
    runtime, lease_state: str, job_status: str
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)
    with database.session() as session:
        job = session.get(Job, claimed.id)
        assert job is not None
        job.status = job_status
    _set_lease(database, claimed.id, lease_state)
    before = _snapshot(database, claimed.id)

    assert not queue.cancel_owned(
        claimed.id,
        "owner",
        lease_generation=claimed.lease_generation,
    )

    assert _snapshot(database, claimed.id) == before


@pytest.mark.parametrize("lease_state", ["expired", "missing"])
def test_expired_or_missing_lease_is_not_reported_as_owned(
    runtime, lease_state: str
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)
    _set_lease(database, claimed.id, lease_state)
    before = _snapshot(database, claimed.id)

    assert not queue.owns_lease(
        claimed.id,
        "owner",
        lease_generation=claimed.lease_generation,
    )

    assert _snapshot(database, claimed.id) == before


@pytest.mark.parametrize("lease_state", ["expired", "missing"])
def test_expired_or_missing_lease_cannot_update_linked_agent_runs(
    runtime, lease_state: str
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)
    _set_lease(database, claimed.id, lease_state)
    before = _snapshot(database, claimed.id)

    Worker(queue, "owner")._update_agent_run_status(
        claimed,
        "failed",
        claimed.lease_generation,
    )

    assert _snapshot(database, claimed.id) == before


@pytest.mark.parametrize("completion_api", ["wrapper", "in_session"])
def test_current_lease_can_complete(runtime, completion_api: str) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)

    assert queue.owns_lease(
        claimed.id,
        "owner",
        lease_generation=claimed.lease_generation,
    )
    if completion_api == "wrapper":
        queue.complete(
            claimed.id,
            "owner",
            {"result": "current"},
            lease_generation=claimed.lease_generation,
        )
    else:
        with database.immediate_session() as session:
            queue.complete_in_session(
                session,
                claimed.id,
                "owner",
                {"result": "current"},
                lease_generation=claimed.lease_generation,
            )

    state, events, _ = _snapshot(database, claimed.id)
    assert state[0] == "succeeded"
    assert state[9] == {"result": "current"}
    assert [row[1] for row in events].count("job.succeeded") == 1


def test_current_lease_can_fail_and_schedule_retry(runtime) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue, max_attempts=2)

    assert queue.fail(
        claimed.id,
        "owner",
        "current_failure",
        "Current failure",
        lease_generation=claimed.lease_generation,
    )

    state, events, runs = _snapshot(database, claimed.id)
    assert state[0] == "queued"
    assert any(row[1] == "job.retry_scheduled" for row in events)
    assert sorted(row[1] for row in runs) == ["completed", "retrying"]


def test_current_lease_can_defer_for_resources(runtime) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)

    assert queue.defer_for_resources(
        claimed.id,
        "owner",
        lease_generation=claimed.lease_generation,
        retry_delay_seconds=0.1,
    )

    state, events, _ = _snapshot(database, claimed.id)
    assert state[0] == "queued"
    assert state[1] is None
    assert any(row[1] == "job.waiting_for_resource" for row in events)


@pytest.mark.parametrize("job_status", ["running", "cancel_requested"])
def test_current_lease_can_acknowledge_cancellation(runtime, job_status: str) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)
    if job_status == "cancel_requested":
        queue.request_cancel(claimed.id)

    assert queue.cancel_owned(
        claimed.id,
        "owner",
        lease_generation=claimed.lease_generation,
    )

    state, events, _ = _snapshot(database, claimed.id)
    assert state[0] == "canceled"
    assert any(row[1] == "job.canceled" for row in events)


def test_current_lease_can_update_only_its_active_linked_agent_run(runtime) -> None:
    database, queue = runtime
    claimed, _, run_ids = _claimed_job(database, queue)

    Worker(queue, "owner")._update_agent_run_status(
        claimed,
        "failed",
        claimed.lease_generation,
    )

    _, _, runs = _snapshot(database, claimed.id)
    assert {run_id: status for run_id, status, _ in runs} == {
        run_ids[0]: "failed",
        run_ids[1]: "completed",
    }


@pytest.mark.parametrize("kind", ["subtitle.evidence", PROJECT_COPY_JOB_KIND])
def test_identical_atomic_success_acknowledgement_is_idempotent(
    runtime, kind: str
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue, kind=kind)
    result = {"sealed": True}

    queue.complete(
        claimed.id,
        "owner",
        result,
        lease_generation=claimed.lease_generation,
    )
    after_first = _snapshot(database, claimed.id)
    queue.complete(
        claimed.id,
        "owner",
        result,
        lease_generation=claimed.lease_generation,
    )
    assert _snapshot(database, claimed.id) == after_first

    with pytest.raises(RuntimeError, match="^Job lease is no longer owned by this worker\\.$"):
        queue.complete(
            claimed.id,
            "owner",
            {"sealed": False},
            lease_generation=claimed.lease_generation,
        )
    assert _snapshot(database, claimed.id) == after_first


def test_completion_waiting_for_writer_refuses_lease_expired_while_blocked(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, queue = runtime
    claimed, _, _ = _claimed_job(database, queue)
    start = utcnow()
    expiry = start + timedelta(seconds=1)
    with database.session() as session:
        job = session.get(Job, claimed.id)
        assert job is not None
        job.lease_expires_at = expiry

    fake_now = [start]
    monkeypatch.setattr(jobs, "utcnow", lambda: fake_now[0])
    begin_attempted = threading.Event()

    def signal_completion_begin(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            begin_attempted.set()

    event.listen(database.engine, "before_cursor_execute", signal_completion_begin)
    try:
        with closing(sqlite3.connect(database.path, timeout=5)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with ThreadPoolExecutor(max_workers=1) as executor:
                completion = executor.submit(
                    queue.complete,
                    claimed.id,
                    "owner",
                    {"result": "blocked"},
                    lease_generation=claimed.lease_generation,
                )
                assert begin_attempted.wait(5)
                fake_now[0] = expiry + timedelta(microseconds=1)
                writer.rollback()
                with pytest.raises(
                    RuntimeError,
                    match="^Job lease is no longer owned by this worker\\.$",
                ):
                    completion.result(timeout=5)
    finally:
        event.remove(database.engine, "before_cursor_execute", signal_completion_begin)

    state, events, _ = _snapshot(database, claimed.id)
    assert state[0] == "running"
    assert state[2] == expiry.replace(tzinfo=None)
    assert not any(row[1] == "job.succeeded" for row in events)
