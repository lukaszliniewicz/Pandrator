"""Job timestamps keep their instant after SQLite round trips and during waits."""

from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.http_serialization import job_payload, model_payload
from pandrator.web.jobs import JobQueue
from pandrator.web.sessions import SessionService
from pandrator.web.work import WorkService
from tests.web_test_support import prepare_web_test_data_root


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 10, 31, 6, 15),
        datetime(2026, 10, 31, 6, 15, tzinfo=UTC),
        datetime(2026, 10, 31, 7, 15, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_http_datetimes_are_explicit_utc_but_calendar_dates_stay_dates(value):
    result = model_payload(SimpleNamespace(at=value, day=date(2026, 10, 31)), ("at", "day"))
    assert result == {"at": "2026-10-31T06:15:00+00:00", "day": "2026-10-31"}


def test_job_round_trip_and_storage_retry_project_the_same_instant_without_lazy_reads(tmp_path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        session_id = SessionService(database).create("Time projection").id
        queue = JobQueue(database)
        job = queue.enqueue("noop", {}, session_id=session_id)
        accepted = job_payload(job)
        persisted = job_payload(queue.get(job.id))
        assert "available_at" not in accepted and "available_at" not in persisted
        assert accepted["created_at"] == persisted["created_at"]
        assert datetime.fromisoformat(persisted["created_at"]).utcoffset() == timedelta(0)
        claimed = queue.claim("time-projection-worker")
        assert claimed is not None
        assert queue.defer_for_storage(
            job.id,
            "time-projection-worker",
            lease_generation=claimed.lease_generation,
            detail="Storage capacity wait",
        )
        projected = job_payload(queue.get(job.id))
        work = WorkService(queue, SecretRedactor(database))
        view = work.get(job.id)
        assert view.state == "queued" and view.available_at is not None
        assert view.available_at == datetime.fromisoformat(projected["available_at"])
        assert view.available_at.utcoffset() == timedelta(0)
        assert 30000 < view.poll_after_ms <= 60000
        assert view.model_dump(mode="json")["available_at"].endswith("Z")
        queries = []

        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            queries.append(statement)

        event.listen(database.engine, "before_cursor_execute", observe)
        try:
            listed = work.list(limit=10)
            assert listed[0].available_at == view.available_at
        finally:
            event.remove(database.engine, "before_cursor_execute", observe)
        assert not any(statement.startswith("SELECT jobs.available_at") for statement in queries)
    finally:
        database.dispose()
