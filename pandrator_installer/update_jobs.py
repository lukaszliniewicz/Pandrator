"""Inspect update job drain state with deterministic SQLite ownership."""

import sqlite3
from contextlib import closing
from pathlib import Path


def prepare_job_drain(database_path: Path, *, cancel_running: bool) -> int:
    try:
        with closing(sqlite3.connect(database_path)) as connection, connection:
            running = int(
                connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[
                    0
                ]
            )
            if running and cancel_running:
                connection.execute(
                    "UPDATE jobs SET status = 'cancel_requested' WHERE status = 'running'"
                )
                connection.commit()
            return running
    except sqlite3.Error as error:
        raise RuntimeError(
            f"Could not inspect or cancel running jobs; refusing to prepare an update: {error}"
        ) from error
