"""Update drain waits for canonical cancellation and native owner acknowledgement."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psutil
import pytest
from sqlalchemy import select

from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Job, JobEvent
from pandrator_installer import update_jobs
from tests.web_test_support import prepare_web_test_data_root

PYTHON = Path(sys.executable)


@pytest.fixture
def owned_job(tmp_path: Path) -> Iterator[tuple[Database, JobQueue, Job]]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    queue = JobQueue(database)
    queue.enqueue("update-drain.fixture")
    job = queue.claim("fixture-owner", lease_seconds=60)
    assert job is not None
    try:
        yield database, queue, job
    finally:
        database.dispose()


def queue_snapshot(database: Database) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with database.session() as session:
        jobs = list(
            session.execute(
                select(
                    Job.id,
                    Job.status,
                    Job.lease_owner,
                    Job.lease_generation,
                    Job.lease_expires_at,
                    Job.finished_at,
                    Job.updated_at,
                ).order_by(Job.id)
            ).tuples()
        )
        events = list(
            session.execute(select(JobEvent.id, JobEvent.event_type).order_by(JobEvent.id)).tuples()
        )
    return jobs, events


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_budget_refuses_before_inspection(tmp_path: Path, timeout: float) -> None:
    with patch.object(update_jobs, "_inspect_active_jobs") as inspect:
        with pytest.raises(ValueError, match="timeout must be finite"):
            update_jobs.prepare_job_drain(
                tmp_path / "missing.db", python=PYTHON, cancel_running=True, drain_timeout=timeout
            )
        inspect.assert_not_called()


@pytest.mark.parametrize("timeout", [0.0, -1.0])
@pytest.mark.parametrize("status", [None, "running", "cancel_requested"])
def test_zero_budget_admits_only_empty_queue_without_cancellation(
    tmp_path: Path, timeout: float, status: str | None
) -> None:
    path = tmp_path / "queue.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE jobs(status TEXT)")
        if status:
            connection.execute("INSERT INTO jobs VALUES (?)", (status,))
    with patch.object(update_jobs, "_request_job_cancellations") as cancel:
        if status:
            with pytest.raises(RuntimeError, match="did not drain"):
                update_jobs.prepare_job_drain(
                    path, python=PYTHON, cancel_running=True, drain_timeout=timeout
                )
        else:
            update_jobs.prepare_job_drain(
                path, python=PYTHON, cancel_running=True, drain_timeout=timeout
            )
        cancel.assert_not_called()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT status FROM jobs").fetchall() == (
            [(status,)] if status else []
        )


def test_inspection_is_one_readonly_snapshot_and_closes_connection(tmp_path: Path) -> None:
    path = tmp_path / "escaped ?# queue.db"
    native_connect = sqlite3.connect
    with closing(native_connect(path)) as connection, connection:
        connection.execute("CREATE TABLE jobs(status TEXT)")
        connection.executemany(
            "INSERT INTO jobs VALUES (?)", [("running",), ("cancel_requested",), ("succeeded",)]
        )
    statements: list[str] = []
    connections: list[sqlite3.Connection] = []

    def connect(uri_path: str, **kwargs: Any) -> sqlite3.Connection:
        assert uri_path == path.resolve().as_uri() + "?mode=ro"
        assert kwargs == {"uri": True, "timeout": 0.25}
        opened = native_connect(uri_path, **kwargs)
        opened.set_trace_callback(statements.append)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            opened.execute("INSERT INTO jobs VALUES ('running')")
        statements.clear()
        connections.append(opened)
        return opened

    with patch.object(update_jobs.sqlite3, "connect", side_effect=connect):
        assert update_jobs._inspect_active_jobs(path, timeout=0.25) == (2, 1)
    assert len(statements) == 1 and statements[0].startswith("SELECT")
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("stage", ["connect", "query", "close"])
def test_sqlite_inspection_errors_preserve_cause_and_close(tmp_path: Path, stage: str) -> None:
    path = tmp_path / "queue.db"
    native_connect = sqlite3.connect
    with closing(native_connect(path)) as connection, connection:
        connection.execute("CREATE TABLE jobs(status TEXT)")
    opened: list[sqlite3.Connection] = []
    sentinel = sqlite3.OperationalError(f"{stage} sentinel")

    class InterruptedConnection(sqlite3.Connection):
        def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            if stage == "query":
                raise sentinel
            return super().execute(*args, **kwargs)

        def close(self) -> None:
            super().close()
            if stage == "close":
                raise sentinel

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if stage == "connect":
            raise sentinel
        connection = native_connect(*args, factory=InterruptedConnection, **kwargs)
        opened.append(connection)
        return connection

    with patch.object(update_jobs.sqlite3, "connect", side_effect=connect):
        with pytest.raises(RuntimeError, match="Could not inspect or cancel") as caught:
            update_jobs.prepare_job_drain(
                path, python=PYTHON, cancel_running=False, drain_timeout=0
            )
    assert caught.value.__cause__ is sentinel
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(connection, "SELECT 1")


def test_native_cancellation_preserves_lease_until_owner_acknowledges(
    owned_job: tuple[Database, JobQueue, Job],
) -> None:
    database, queue, job = owned_job
    original = queue_snapshot(database)
    update_jobs._request_job_cancellations(database.path, python=PYTHON, timeout=5)
    requested = queue_snapshot(database)
    before, after = original[0][0], requested[0][0]
    assert after[0] == before[0] and after[1] == "cancel_requested"
    assert after[2:6] == before[2:6]
    assert after[6] > before[6]
    assert [event[1] for event in requested[1]].count("job.cancel_requested") == 1
    with patch.object(update_jobs, "_request_job_cancellations") as duplicate:
        with pytest.raises(RuntimeError, match="must be acknowledged"):
            update_jobs.prepare_job_drain(
                database.path, python=PYTHON, cancel_running=True, drain_timeout=0.02
            )
        duplicate.assert_not_called()
    assert queue_snapshot(database) == requested
    assert queue.cancel_owned(job.id, "fixture-owner", lease_generation=job.lease_generation)
    update_jobs.prepare_job_drain(
        database.path, python=PYTHON, cancel_running=True, drain_timeout=0
    )
    completed = queue_snapshot(database)[0][0]
    assert completed[1:3] == ("canceled", None)
    assert completed[4] is None and completed[5] is not None


@pytest.mark.parametrize(
    "incompatibility",
    [
        "missing_session",
        "missing_cancel",
        "owner",
        "generation",
        "expiry",
        "finished",
        "id",
        "terminal",
    ],
)
def test_native_compatibility_guard_rolls_back_all_requests_and_events(
    owned_job: tuple[Database, JobQueue, Job], incompatibility: str
) -> None:
    database, queue, _job = owned_job
    queue.enqueue("update-drain.second-fixture")
    assert queue.claim("second-owner", lease_seconds=60) is not None
    original = queue_snapshot(database)
    setup = """
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import utcnow
"""
    if incompatibility == "missing_session":
        setup += "Database.immediate_session = None\n"
    elif incompatibility == "missing_cancel":
        setup += "JobQueue.request_cancel_in_session = None\n"
    else:
        mutation = {
            "owner": "result.lease_owner = 'replacement-owner'",
            "generation": "result.lease_generation += 1",
            "expiry": "result.lease_expires_at = None",
            "finished": "result.finished_at = utcnow()",
            "id": "result.id = 'replacement-id'",
            "terminal": "result.status = 'canceled'",
        }[incompatibility]
        setup += f"""
native = JobQueue.request_cancel_in_session
calls = 0
def incompatible(self, session, job_id):
    global calls
    result = native(self, session, job_id)
    calls += 1
    if calls == 2:
        {mutation}
    return result
JobQueue.request_cancel_in_session = incompatible
"""
    native_run = subprocess.run

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[1:3] == ["-I", "-c"]
        assert command[3] == update_jobs._CANCEL_RUNNING_PROGRAM
        altered = list(command)
        altered[3] = setup + command[3]
        return native_run(altered, **kwargs)

    with patch.object(update_jobs.subprocess, "run", side_effect=run):
        with pytest.raises(RuntimeError, match="could not request cancellation safely") as caught:
            update_jobs._request_job_cancellations(database.path, python=PYTHON, timeout=5)
    assert isinstance(caught.value.__cause__, subprocess.CalledProcessError)
    assert "Traceback" not in str(caught.value)
    assert queue_snapshot(database) == original


@pytest.mark.parametrize(
    "output",
    ["not JSON", "[]", "{}", '{"requested": true}', '{"requested": -1}', '{"requested": 1.5}'],
)
def test_malformed_transport_result_refuses(tmp_path: Path, output: str) -> None:
    result = subprocess.CompletedProcess([], 0, stdout=output)
    with patch.object(update_jobs.subprocess, "run", return_value=result):
        with pytest.raises(RuntimeError, match="invalid cancellation result"):
            update_jobs._request_job_cancellations(tmp_path / "queue.db", python=PYTHON, timeout=1)


def test_empty_cancellation_race_result_is_valid(tmp_path: Path) -> None:
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps({"requested": 0}))
    with patch.object(update_jobs.subprocess, "run", return_value=result):
        update_jobs._request_job_cancellations(tmp_path / "queue.db", python=PYTHON, timeout=1)


def test_native_empty_race_commits_no_duplicate_request(
    owned_job: tuple[Database, JobQueue, Job],
) -> None:
    database, queue, job = owned_job
    queue.request_cancel(job.id)
    assert queue.cancel_owned(job.id, "fixture-owner", lease_generation=job.lease_generation)
    completed = queue_snapshot(database)
    update_jobs._request_job_cancellations(database.path, python=PYTHON, timeout=5)
    assert queue_snapshot(database) == completed


def test_selected_runtime_launch_failure_preserves_queue(
    owned_job: tuple[Database, JobQueue, Job], tmp_path: Path
) -> None:
    database, _queue, _job = owned_job
    original = queue_snapshot(database)
    with pytest.raises(RuntimeError, match="could not request cancellation safely") as caught:
        update_jobs.prepare_job_drain(
            database.path,
            python=tmp_path / "absent-python",
            cancel_running=True,
            drain_timeout=1,
        )
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert queue_snapshot(database) == original


def test_native_transport_timeout_reaps_child_without_queue_mutation(
    owned_job: tuple[Database, JobQueue, Job], tmp_path: Path
) -> None:
    database, _queue, _job = owned_job
    original = queue_snapshot(database)
    witness = tmp_path / "sleeping-child.json"
    sleeping = f"""
import json, os, time
from pathlib import Path
Path({str(witness)!r}).write_text(json.dumps({{"pid": os.getpid()}}))
time.sleep(60)
"""
    native_run = subprocess.run

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        altered = list(command)
        altered[3] = sleeping
        return native_run(altered, **kwargs)

    started = time.monotonic()
    with patch.object(update_jobs.subprocess, "run", side_effect=run):
        with pytest.raises(RuntimeError, match="could not request cancellation safely") as caught:
            update_jobs.prepare_job_drain(
                database.path, python=PYTHON, cancel_running=True, drain_timeout=0.3
            )
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
    assert time.monotonic() - started < 3
    assert witness.is_file()
    assert not psutil.pid_exists(json.loads(witness.read_text())["pid"])
    assert queue_snapshot(database) == original


def test_transport_uses_selected_runtime_and_sanitized_environment(tmp_path: Path) -> None:
    selected = tmp_path / "selected-python"
    database = tmp_path / "queue.db"
    result = subprocess.CompletedProcess([], 0, stdout='{"requested": 1}')
    with (
        patch.dict(os.environ, {"LD_LIBRARY_PATH_ORIG": "/original-libraries"}),
        patch.object(update_jobs.subprocess, "run", return_value=result) as run,
    ):
        update_jobs._request_job_cancellations(database, python=selected, timeout=0.75)
    args, kwargs = run.call_args
    assert args[0] == [
        str(selected),
        "-I",
        "-c",
        update_jobs._CANCEL_RUNNING_PROGRAM,
        str(database),
    ]
    assert kwargs["shell"] is False and kwargs["check"] is True
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] == 0.75
    if sys.platform.startswith("linux"):
        assert kwargs["env"]["LD_LIBRARY_PATH"] == "/original-libraries"
        assert "LD_LIBRARY_PATH_ORIG" not in kwargs["env"]
