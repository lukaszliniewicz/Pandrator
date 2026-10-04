"""Drain active queue rows before separate process-exit admission."""

import json
import math
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from .subprocess_env import external_subprocess_environment

_CANCEL_RUNNING_PROGRAM = """
import json
import sys
from pathlib import Path

from sqlalchemy import select
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Job

database = Database(Path(sys.argv[1]))
try:
    queue = JobQueue(database)
    immediate_session = getattr(database, "immediate_session", None)
    request_cancel = getattr(queue, "request_cancel_in_session", None)
    if not callable(immediate_session) or not callable(request_cancel):
        raise RuntimeError("The installed job queue lacks transactional cancellation support.")
    with immediate_session() as session:
        jobs = list(session.scalars(select(Job).where(Job.status == "running")))
        for job in jobs:
            ownership = (
                job.id, job.lease_owner, job.lease_generation,
                job.lease_expires_at, job.finished_at,
            )
            requested = request_cancel(session, job.id)
            if requested.status != "cancel_requested" or (
                requested.id, requested.lease_owner, requested.lease_generation,
                requested.lease_expires_at, requested.finished_at,
            ) != ownership:
                raise RuntimeError("The installed job queue does not preserve cancellation ownership.")
        requested_count = len(jobs)
    print(json.dumps({"requested": requested_count}))
finally:
    database.dispose()
"""


def _inspect_active_jobs(database_path: Path, *, timeout: float) -> tuple[int, int]:
    try:
        with closing(
            sqlite3.connect(
                database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout
            )
        ) as connection:
            counts = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0) "
                "FROM jobs WHERE status IN ('running', 'cancel_requested')"
            ).fetchone()
            return int(counts[0]), int(counts[1])
    except sqlite3.Error as error:
        raise RuntimeError(
            f"Could not inspect or cancel running jobs; refusing to prepare an update: {error}"
        ) from error


def _request_job_cancellations(database_path: Path, *, python: Path, timeout: float) -> None:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", _CANCEL_RUNNING_PROGRAM, str(database_path)],
            env=external_subprocess_environment(),
            shell=False,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise RuntimeError(
            "The installed job queue could not request cancellation safely; "
            "refusing to prepare an update."
        ) from error
    try:
        payload: object = json.loads(result.stdout)
        requested = payload.get("requested") if isinstance(payload, dict) else None
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
            raise ValueError("Invalid requested count")
    except (ValueError, TypeError) as error:
        raise RuntimeError(
            "The installed job queue returned an invalid cancellation result; "
            "refusing to prepare an update."
        ) from error


def prepare_job_drain(
    database_path: Path,
    *,
    python: Path,
    cancel_running: bool,
    drain_timeout: float,
) -> None:
    if not math.isfinite(drain_timeout):
        raise ValueError("Update job drain timeout must be finite.")
    deadline = time.monotonic() + max(0.0, drain_timeout)
    # Terminal queue state can result from lease expiry; lifecycle also
    # requires the managed processes to exit before activating an update.
    while True:
        active, running = _inspect_active_jobs(
            database_path, timeout=min(5.0, max(0.0, deadline - time.monotonic()))
        )
        if not active:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "Running jobs did not drain before the update timeout; "
                "cancellation must be acknowledged before updating."
            )
        if running and cancel_running:
            _request_job_cancellations(database_path, python=python, timeout=remaining)
            continue
        time.sleep(min(0.5, remaining))
