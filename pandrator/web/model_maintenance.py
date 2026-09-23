"""Cross-process guard for manager-owned audio.cpp model maintenance."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

AUDIO_CPP_COMPONENT_ID = "audio_cpp"
"""The component ID persisted by Manager plans for audio.cpp."""

MANAGER_TERMINAL_OPERATION_STATES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "recovery_required",
    }
)

_BUSY_TIMEOUT_MS = 1_000


def _sqlite_read_uri(path: Path) -> str:
    return f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"


def _descriptor_root(path: Path) -> Path | None:
    """Return the manager root only for a valid, co-located descriptor."""

    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("workspace"), str):
        return None
    workspace = Path(payload["workspace"]).expanduser().resolve(strict=False)
    root = (workspace / "Pandrator").resolve(strict=False)
    if path.resolve(strict=False).parent != root / "state":
        return None
    return root


def managed_manager_database(
    database_path: str | os.PathLike[str],
    *,
    descriptor_path: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Resolve the manager DB for a managed app database.

    Standalone Pandrator data roots intentionally return ``None``.  The path
    shape and state DB marker are both required so an arbitrary ``state``
    directory cannot make an app database manager-owned.  A configured
    descriptor is accepted only when it resolves to that same root.
    """

    database = Path(database_path).expanduser().resolve(strict=False)
    if database.name != "pandrator.sqlite3" or database.parent.name != "data":
        return None
    root = database.parent.parent
    manager_database = root / "state" / "manager.sqlite3"
    if not manager_database.exists():
        return None
    if not manager_database.is_file():
        raise ValueError("The managed Manager database path is not a regular file.")

    configured = descriptor_path or os.environ.get("PANDRATOR_MANAGER_DESCRIPTOR")
    if configured:
        descriptor_root = _descriptor_root(Path(configured).expanduser())
        if descriptor_root is not None and descriptor_root != root:
            raise ValueError(
                "The configured Manager descriptor does not match the app database root."
            )
    return manager_database


def _plan_touches_audio_cpp(plan_json: str, *, operation_id: str) -> bool:
    try:
        plan = json.loads(plan_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            "The managed Manager database contains an invalid operation plan."
        ) from error
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        raise ValueError(
            "The managed Manager database contains a malformed operation plan."
        )
    for task in plan["tasks"]:
        if not isinstance(task, dict):
            raise ValueError(
                f"The managed Manager operation {operation_id} contains a malformed task."
            )
        component_id = task.get("component_id")
        if component_id is not None and not isinstance(component_id, str):
            raise ValueError(
                f"The managed Manager operation {operation_id} contains an invalid task owner."
            )
        if component_id == AUDIO_CPP_COMPONENT_ID:
            return True
    return False


def has_active_audio_cpp_maintenance(
    manager_database: str | os.PathLike[str],
) -> bool:
    """Read Manager operation state without creating or writing its database."""

    path = Path(manager_database).expanduser().resolve(strict=False)
    if not path.is_file():
        raise ValueError("The managed Manager database is unavailable.")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _sqlite_read_uri(path),
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("BEGIN")
        rows = connection.execute(
            """
            SELECT o.operation_id, o.state, p.plan_json
            FROM operations AS o
            LEFT JOIN plans AS p ON p.plan_id = o.plan_id
            WHERE o.state IS NULL OR o.state NOT IN (?, ?, ?, ?)
            ORDER BY o.updated_at DESC
            """,
            tuple(sorted(MANAGER_TERMINAL_OPERATION_STATES)),
        ).fetchall()
        for operation_id, state, plan_json in rows:
            if not isinstance(operation_id, str) or not isinstance(state, str):
                raise ValueError("The managed Manager database contains invalid operation state.")
            touches_audio_cpp = _plan_touches_audio_cpp(
                plan_json,
                operation_id=operation_id,
            )
            if not touches_audio_cpp:
                continue
            return True
        connection.commit()
        return False
    except (sqlite3.DatabaseError, OSError, ValueError) as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError(
            "The managed Manager database is unavailable or malformed; refusing to enqueue work."
        ) from error
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()


def ensure_app_job_allowed(database_path: str | os.PathLike[str]) -> None:
    """Reject app work while a managed audio.cpp operation is active."""

    manager_database = managed_manager_database(database_path)
    if manager_database is None:
        return
    if has_active_audio_cpp_maintenance(manager_database):
        raise ValueError(
            "Cannot enqueue Pandrator work while audio.cpp model maintenance is active."
        )
