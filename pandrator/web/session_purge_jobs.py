"""Queue adapters for durable purge inspections and irreversible deletion."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from pandrator.runtime import DataPaths

from .database import Database
from .jobs import JobHandler
from .session_purge import SessionPurgeService

INSPECTION_KIND = "maintenance.session_purge_preview"
PURGE_KIND = "maintenance.session_purge"


def purge_job_handlers(database: Database, paths: DataPaths) -> dict[str, JobHandler]:
    """Return handlers; the caller registers both kinds with the job registry."""
    service = SessionPurgeService(database, paths)

    def inspect(payload: dict[str, Any], progress: Callable[[float, str | None], None],
                cancel_event: threading.Event) -> dict[str, Any]:
        if cancel_event.is_set():
            raise RuntimeError("Purge inspection cancelled before execution.")
        preview = service.preview(str(payload["target_session_id"]))
        token = preview.pop("impact_token", None)
        if token is not None:
            preview["impact_fingerprint"] = token
        return preview

    def purge(payload: dict[str, Any], progress: Callable[[float, str | None], None],
              cancel_event: threading.Event) -> dict[str, Any]:
        result = service.purge(
            str(payload["target_session_id"]),
            int(payload["expected_revision"]),
            str(payload["impact_fingerprint"]),
            job_id=str(payload["_job_id"]),
            lease_generation=int(payload["_lease_generation"]),
        )
        if result.get("state") != "complete":
            raise RuntimeError(str(result.get("error") or "Session purge journal did not complete."))
        return {"state": "complete", "target_session_id": payload["target_session_id"]}

    return {INSPECTION_KIND: inspect, PURGE_KIND: purge}
