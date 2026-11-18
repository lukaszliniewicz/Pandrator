"""Durable translation-project creation run by its queue worker.

Forking one language session per planned language hashes and links every
artifact, which took 45 to 80 seconds on four branches. The route therefore
admits the work and returns a handle instead of holding the request open.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from pandrator.runtime import DataPaths

from .access.contracts import ProjectAction
from .access.current_session_policy import require_current_session_access
from .artifacts import ArtifactService
from .database import Database
from .models import Job
from .session_forks import SessionForkService
from .translation_projects import (
    TRANSLATION_PROJECT_CREATE_JOB_KIND,
    create_project_in_session,
    prepare_branch_files,
)

__all__ = [
    "TRANSLATION_PROJECT_CREATE_JOB_KIND",
    "creation_request",
    "run_creation",
]

logger = logging.getLogger(__name__)


def creation_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the explicit request a creation job carries."""

    return {
        "checkpoint_artifact_id": str(payload.get("checkpoint_artifact_id") or ""),
        "name": str(payload.get("name") or ""),
        "expected_revision": int(payload.get("expected_revision") or 0),
        "create_planned_branches": bool(payload.get("create_planned_branches")),
    }


def run_creation(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    payload: dict[str, Any],
    progress=None,
    cancel_event=None,
) -> dict[str, Any]:
    """Create the project and its planned branches as one durable job.

    The fork trees are collected from an authorized read snapshot, materialized
    outside it, then consumed inside the writer that creates the project. A
    failure leaves no project row and no prepared directory behind.
    """

    session_id = str(payload.get("session_id") or "")
    request = creation_request(payload)

    def report(value: float, detail: str | None = None) -> None:
        if progress is not None:
            progress(value, detail)

    def canceled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if canceled():
        return {}
    job_id = str(payload.get("_job_id") or "")
    session_forks = SessionForkService(database, paths, artifacts)
    with database.snapshot_session() as db:
        # The enqueueing request checked EDIT; the leased worker owns the write
        # and must check the captured actor's present authority before any file
        # materialises.
        require_current_session_access(db, session_id, ProjectAction.EDIT)
        prepared = prepare_branch_files(
            db,
            session_id,
            request,
            session_resource=True,
            session_forks=session_forks,
            paths=paths,
        )
    directories: list[Path] = []
    committed = False
    try:
        if prepared is not None:
            if canceled():
                return {}
            report(0.15, "Preparing branch files")
            prepared.materialize()
            report(0.45, "Branch files prepared")
        if canceled():
            return {}
        with database.immediate_session() as db:
            job = db.get(Job, job_id) if job_id else None
            if job is not None and (canceled() or job.status == "cancel_requested"):
                # Cancellation wins over creation; the finally cleans any files.
                return {}
            if prepared is not None:
                prepared.revalidate(db)
            result = create_project_in_session(
                db,
                session_id,
                request["checkpoint_artifact_id"],
                request["name"],
                request["expected_revision"],
                paths=paths,
                create_planned_branches=request["create_planned_branches"],
                session_forks=session_forks,
                created_directories=directories,
                prepared_files=prepared,
            )
            if job is not None:
                project_id = (result.get("project") or {}).get("id")
                if project_id:
                    # The commit point makes a later cancellation request refuse
                    # rather than leave a committed project under a cancelled job.
                    job.payload_json = {
                        **(job.payload_json or {}),
                        "committed_project_id": project_id,
                    }
        committed = True
    finally:
        if prepared is not None:
            prepared.cleanup(committed=committed)
        if not committed:
            for directory in directories:
                shutil.rmtree(directory, ignore_errors=True)
    project = result.get("project") or {}
    branches = [
        {
            "branch_id": branch.get("id"),
            "session_id": branch.get("session_id"),
            "name": branch.get("name"),
            "target_language": branch.get("target_language"),
        }
        for branch in project.get("branches") or []
        if isinstance(branch, dict)
    ]
    try:
        report(1.0, "Translation project created")
    except Exception:  # noqa: BLE001 - the commit is already durable
        logger.warning(
            "Could not report translation project creation progress",
            exc_info=True,
        )
    return {
        "project_id": project.get("id"),
        "project_revision": project.get("revision"),
        "branch_count": len(branches),
        "branches": branches,
    }
