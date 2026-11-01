from __future__ import annotations

import math
from datetime import UTC, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, func, select

import pandrator.web.storage_admission as storage_admission
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import (
    AgentRun,
    Job,
    JobEvent,
    ResourceClaim,
    SessionRecord,
    StorageReservation,
    utcnow,
)
from pandrator.web.output_storage import reserve_output_space, storage_wait_detail
from pandrator.web.projects.identity import create_session_project
from pandrator.web.storage_admission import StoragePressure
from tests import web_test_support

MIB = 1024 * 1024


@pytest.fixture
def job_runtime(tmp_path: Path):
    paths = web_test_support.prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield database, JobQueue(database)
    finally:
        database.dispose()


def _session_id(database: Database) -> str:
    with database.session() as session:
        record = SessionRecord(name="Storage wait fixture", storage_key="storage-wait")
        create_session_project(session, record)
        session.flush()
        return record.id


def _fake_disk_usage(monkeypatch: pytest.MonkeyPatch, *, free_bytes: int) -> None:
    monkeypatch.setattr(
        storage_admission,
        "shutil",
        SimpleNamespace(
            disk_usage=lambda _path: SimpleNamespace(free=free_bytes),
        ),
    )


def _storage_pressure_handler(
    database: Database,
    tmp_path: Path,
    pressure_errors: list[StoragePressure],
    *,
    on_pressure=None,
):
    def reserve_then_fail(payload: dict[str, Any], _progress, _cancel_event):
        reserve_output_space(database, tmp_path / "output.wav", 2 * MIB)
        try:
            reserve_output_space(database, tmp_path / "output.wav", 256 * MIB)
        except StoragePressure as error:
            pressure_errors.append(error)
            if on_pressure is not None:
                on_pressure(payload)
            raise
        raise AssertionError("The fixed test disk should reject the second estimate.")

    return reserve_then_fail


def _count_rows(database: Database, model: type) -> int:
    with database.session() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _as_utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_pressure_wait_retries_same_job_and_retires_effects(
    job_runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    database, queue = job_runtime
    _fake_disk_usage(monkeypatch, free_bytes=128 * MIB)
    session_id = _session_id(database)
    original_payload = {"source_artifact_id": "pinned-artifact", "revision": 7}
    job = queue.enqueue(
        "storage.wait.test",
        original_payload,
        session_id=session_id,
        max_attempts=1,
        resource_keys=["gpu:0"],
    )
    with database.session() as session:
        session.add(
            AgentRun(
                kind="storage_wait_test",
                session_id=session_id,
                job_id=job.id,
                status="running",
            )
        )

    pressure_errors: list[StoragePressure] = []
    payloads: list[dict[str, Any]] = []
    effects: list[str] = []
    stale_defer_results: list[bool] = []
    first_generation: int | None = None

    def first_attempt(payload, progress, cancel_event):
        nonlocal first_generation
        payloads.append({key: value for key, value in payload.items() if not key.startswith("_")})
        first_generation = int(payload["_lease_generation"])
        progress(0.4, "Partial work")

        reserve_output_space(database, tmp_path / "output.wav", 2 * MIB)
        assert _count_rows(database, StorageReservation) == 1
        try:
            reserve_output_space(database, tmp_path / "output.wav", 256 * MIB)
        except StoragePressure as error:
            pressure_errors.append(error)
            raise
        raise AssertionError("The fixed test disk should reject the second estimate.")

    first_worker = Worker(
        queue,
        "worker-first",
        {"storage.wait.test": first_attempt},
    )
    assert first_worker.run_once()

    held = queue.get(job.id)
    assert held.id == job.id
    assert held.payload_json == original_payload
    assert held.status == "queued"
    assert held.attempts == 0
    assert held.error_code == "storage_pressure"
    assert pressure_errors
    expected_detail = storage_wait_detail(pressure_errors[0])
    assert held.error_message == expected_detail
    assert held.progress_detail == expected_detail
    assert held.progress == pytest.approx(0.4)
    assert held.lease_owner is None
    assert held.lease_expires_at is None
    assert held.available_at is not None
    held_available_at = _as_utc(held.available_at)
    assert utcnow() < held_available_at <= utcnow() + timedelta(seconds=61)
    assert _count_rows(database, StorageReservation) == 0
    assert _count_rows(database, ResourceClaim) == 0
    assert first_generation is not None
    claimed_generation = first_generation

    with database.session() as session:
        wait_events = list(
            session.scalars(
                select(JobEvent).where(
                    JobEvent.job_id == job.id,
                    JobEvent.event_type == "job.waiting_for_storage",
                )
            )
        )
        agent_run = session.scalar(select(AgentRun).where(AgentRun.job_id == job.id))
    assert len(wait_events) == 1
    wait_payload = wait_events[0].payload_json
    assert wait_payload["retry_after_ms"] == 60_000
    assert wait_payload["detail"] == expected_detail
    assert "resources" not in wait_payload
    assert "worker_id" not in wait_payload
    assert agent_run is not None
    assert agent_run.status == "running"
    assert not any(
        record.levelname == "ERROR" and "Worker job" in record.getMessage()
        for record in caplog.records
    )

    with database.immediate_session() as session:
        held_row = session.get(Job, job.id)
        assert held_row is not None
        held_row.available_at = utcnow() - timedelta(seconds=1)

    def successful_attempt(payload, _progress, _cancel_event):
        payloads.append({key: value for key, value in payload.items() if not key.startswith("_")})
        assert payload["_lease_generation"] == claimed_generation + 1
        stale_defer_results.append(
            queue.defer_for_storage(
                job.id,
                "worker-first",
                lease_generation=claimed_generation,
                detail="stale owner detail",
            )
        )
        current = queue.get(job.id)
        assert current.status == "running"
        assert current.lease_owner == "worker-second"
        assert current.error_code is None
        assert current.error_message is None
        assert current.progress_detail is None
        assert current.progress == pytest.approx(0.4)
        effects.append("completed")
        return {"effect_count": len(effects)}

    second_worker = Worker(
        queue,
        "worker-second",
        {"storage.wait.test": successful_attempt},
    )
    assert second_worker.run_once()

    completed = queue.get(job.id)
    assert completed.id == job.id
    assert completed.payload_json == original_payload
    assert completed.status == "succeeded"
    assert completed.attempts == 1
    assert completed.lease_generation == claimed_generation + 1
    assert completed.result_json == {"effect_count": 1}
    assert completed.error_code is None
    assert completed.error_message is None
    assert completed.progress_detail is None
    assert payloads == [original_payload, original_payload]
    assert stale_defer_results == [False]
    assert effects == ["completed"]
    assert _count_rows(database, ResourceClaim) == 0
    assert _count_rows(database, StorageReservation) == 0


def test_cancel_requested_storage_pressure_uses_cancellation_path(
    job_runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, queue = job_runtime
    _fake_disk_usage(monkeypatch, free_bytes=128 * MIB)
    pressure_errors: list[StoragePressure] = []
    job = queue.enqueue(
        "storage.wait.cancel",
        resource_keys=["gpu:0"],
        max_attempts=2,
    )

    def request_cancel(payload):
        queue.request_cancel(payload["_job_id"])

    handler = _storage_pressure_handler(
        database,
        tmp_path,
        pressure_errors,
        on_pressure=request_cancel,
    )
    worker = Worker(queue, "worker", {"storage.wait.cancel": handler})

    assert worker.run_once()

    canceled = queue.get(job.id)
    assert canceled.status == "canceled"
    assert canceled.attempts == 1
    assert canceled.error_code is None
    assert _count_rows(database, StorageReservation) == 0
    assert _count_rows(database, ResourceClaim) == 0
    with database.session() as session:
        waiting_events = list(
            session.scalars(
                select(JobEvent).where(
                    JobEvent.job_id == job.id,
                    JobEvent.event_type == "job.waiting_for_storage",
                )
            )
        )
    assert waiting_events == []


def test_expired_and_stale_leases_cannot_defer_or_mutate_successor(job_runtime) -> None:
    database, queue = job_runtime
    job = queue.enqueue("storage.wait.fence", max_attempts=3)
    expired_claim = queue.claim("worker-old")
    assert expired_claim is not None

    with database.immediate_session() as session:
        row = session.get(Job, job.id)
        assert row is not None
        row.lease_expires_at = utcnow() - timedelta(seconds=1)

    assert not queue.defer_for_storage(
        job.id,
        "worker-old",
        lease_generation=expired_claim.lease_generation,
        detail="expired claim",
    )
    expired = queue.get(job.id)
    assert expired.status == "running"
    assert expired.lease_owner == "worker-old"
    assert expired.lease_generation == expired_claim.lease_generation

    successor = queue.claim("worker-new")
    assert successor is not None
    assert successor.id == job.id
    assert successor.lease_generation == expired_claim.lease_generation + 1
    with database.immediate_session() as session:
        row = session.get(Job, job.id)
        assert row is not None
        row.error_code = "successor_error"
        row.error_message = "successor message"
        row.progress_detail = "successor detail"

    assert not queue.defer_for_storage(
        job.id,
        "worker-old",
        lease_generation=expired_claim.lease_generation,
        detail="stale claim",
    )
    current = queue.get(job.id)
    assert current.status == "running"
    assert current.lease_owner == "worker-new"
    assert current.lease_generation == successor.lease_generation
    assert current.error_code == "successor_error"
    assert current.error_message == "successor message"
    assert current.progress_detail == "successor detail"


@pytest.mark.parametrize(
    ("retry_delay_seconds", "expected_retry_after_ms"),
    [(0.01, 1_000), (999.0, 300_000)],
)
def test_storage_retry_delay_is_bounded(
    job_runtime, retry_delay_seconds: float, expected_retry_after_ms: int
) -> None:
    database, queue = job_runtime
    job = queue.enqueue("storage.wait.delay")
    claimed = queue.claim("worker")
    assert claimed is not None

    assert queue.defer_for_storage(
        job.id,
        "worker",
        lease_generation=claimed.lease_generation,
        detail="bounded delay",
        retry_delay_seconds=retry_delay_seconds,
    )

    with database.session() as session:
        event_row = session.scalar(
            select(JobEvent).where(
                JobEvent.job_id == job.id,
                JobEvent.event_type == "job.waiting_for_storage",
            )
        )
        current = session.get(Job, job.id)
    assert event_row is not None
    assert event_row.payload_json["retry_after_ms"] == expected_retry_after_ms
    assert current is not None and current.available_at is not None
    expected_seconds = expected_retry_after_ms / 1000
    assert _as_utc(current.available_at) - _as_utc(current.updated_at) == timedelta(
        seconds=expected_seconds
    )


@pytest.mark.parametrize("retry_delay_seconds", [math.nan, math.inf, -math.inf])
def test_non_finite_storage_retry_delay_is_rejected_before_sql(
    job_runtime, monkeypatch: pytest.MonkeyPatch, retry_delay_seconds: float
) -> None:
    _database, queue = job_runtime
    job = queue.enqueue("storage.wait.invalid-delay")
    claimed = queue.claim("worker")
    assert claimed is not None

    def unexpected_sql(*_args, **_kwargs):
        raise AssertionError("Non-finite delay reached the storage wait transaction.")

    monkeypatch.setattr(queue.database, "run_immediate", unexpected_sql)
    with pytest.raises(ValueError, match="finite"):
        queue.defer_for_storage(
            job.id,
            "worker",
            lease_generation=claimed.lease_generation,
            detail="invalid delay",
            retry_delay_seconds=retry_delay_seconds,
        )


def test_no_storage_handler_performs_no_reservation_sql(job_runtime) -> None:
    database, queue = job_runtime
    job = queue.enqueue("storage.wait.empty-scope")
    storage_sql: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if "storage_reservations" in statement.lower():
            storage_sql.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture_sql)
    try:
        worker = Worker(
            queue,
            "worker",
            {"storage.wait.empty-scope": lambda _payload, _progress, _cancel: None},
        )
        assert worker.run_once()
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_sql)

    assert queue.get(job.id).status == "succeeded"
    assert storage_sql == []
