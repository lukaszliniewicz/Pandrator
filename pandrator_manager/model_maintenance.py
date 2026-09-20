"""Manager-side guard for model maintenance against app work."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from .context import WorkspaceLayout
from .errors import ManagerError

AUDIO_CPP_COMPONENT_ID = "audio_cpp"
APP_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancel_requested"})
APP_ACTIVE_GENERATION_STATES = frozenset(
    {"queued", "running", "pausing", "pause_requested", "cancel_requested"}
)
_BUSY_TIMEOUT_MS = 1_000


def _sqlite_rw_uri(path: Path) -> str:
    # ``mode=rw`` prevents an initial-install check from creating a database.
    return f"file:{quote(path.as_posix(), safe='/:')}?mode=rw"


def plan_touches_audio_cpp(plan) -> bool:
    """Return whether a typed Manager plan owns an audio.cpp task."""

    return any(
        task.component_id == AUDIO_CPP_COMPONENT_ID
        for task in plan.tasks
    )


def _active_application_row(connection: sqlite3.Connection):
    job_states = tuple(sorted(APP_ACTIVE_JOB_STATES))
    generation_states = tuple(sorted(APP_ACTIVE_GENERATION_STATES))
    job_placeholders = ",".join("?" for _ in job_states)
    generation_placeholders = ",".join("?" for _ in generation_states)
    job = connection.execute(
        f"""
        SELECT id, kind, status
        FROM jobs
        WHERE status IN ({job_placeholders})
        ORDER BY created_at, id
        LIMIT 1
        """,
        job_states,
    ).fetchone()
    if job is not None:
        return "job", job
    generation = connection.execute(
        f"""
        SELECT id, status
        FROM generation_runs
        WHERE status IN ({generation_placeholders})
        ORDER BY created_at, id
        LIMIT 1
        """,
        generation_states,
    ).fetchone()
    if generation is not None:
        return "generation_run", generation
    return None


def ensure_application_quiescent_for_model_maintenance(
    layout: WorkspaceLayout,
) -> None:
    """Take a short app writer lock and fail if mutable app work exists."""

    application_database = layout.data / "pandrator.sqlite3"
    if not application_database.is_file():
        # A manager can install the app before the app has ever initialized its
        # database.  There is no app transaction to coordinate in that case.
        return

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _sqlite_rw_uri(application_database),
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("BEGIN IMMEDIATE")
        active = _active_application_row(connection)
        if active is not None:
            kind, row = active
            if kind == "job":
                identifier = str(row[0])
                status = str(row[2])
                raise ManagerError(
                    "application_busy",
                    "Cannot change audio.cpp models while Pandrator work is active.",
                    {"job_id": identifier, "job_kind": str(row[1]), "status": status},
                    409,
                )
            raise ManagerError(
                "application_busy",
                "Cannot change audio.cpp models while a Pandrator generation is active.",
                {"generation_run_id": str(row[0]), "status": str(row[1])},
                409,
            )
        connection.commit()
    except ManagerError:
        raise
    except (sqlite3.DatabaseError, OSError) as error:
        raise ManagerError(
            "application_database_unavailable",
            "Could not verify Pandrator activity before changing audio.cpp models; refusing to continue.",
            {"database": str(application_database)},
            503,
        ) from error
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()
