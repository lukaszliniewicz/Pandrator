"""Fence final video publication against cancellation and superseded job claims."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .export_video_commands import check_video_cancelled
from .models import Artifact, Job, utcnow
from .workflow_output_context import OutputWorkflowContext

logger = logging.getLogger(__name__)


def _require_video_claim(
    session: Session, cancel_event: threading.Event, *, session_id: str,
    job_id: str | None, lease_generation: int | None,
) -> None:
    check_video_cancelled(cancel_event)
    # Direct invocations have no durable job; workers always supply both fields.
    if job_id is None and lease_generation is None:
        return
    current = session.scalar(select(Job.id).where(
        Job.id == job_id,
        Job.session_id == session_id,
        Job.kind.in_(("export.create", "export.variant")),
        Job.status == "running",
        Job.lease_owner.is_not(None),
        Job.lease_generation == lease_generation,
        Job.lease_expires_at > utcnow(),
    )) if job_id is not None and lease_generation is not None else None
    if current is None:
        raise InterruptedError("Video export was canceled or its worker lease is no longer current.")


def publish_video_export(
    context: OutputWorkflowContext, rendered: Path, requested_destination: Path, *,
    session_id: str, parent_ids: list[str], settings: dict[str, Any],
    metadata: dict[str, Any], cancel_event: threading.Event,
    job_id: str | None = None, lease_generation: int | None = None,
) -> Artifact:
    """Publish one rendered file, retaining committed output after late cancellation.

    Hash outside the writer transaction. Claim validation, final path allocation,
    atomic rename, artifact and lineage writes share that transaction. A stale
    attempt must never overwrite or clean up another attempt's committed file.
    """
    prepared = context.artifacts.prepare_registration(rendered, settings=settings)
    destination: Path | None = None
    committed = False
    try:
        with context.database.immediate_session() as session:
            _require_video_claim(session, cancel_event, session_id=session_id,
                                 job_id=job_id, lease_generation=lease_generation)
            destination = context.artifacts.next_available_path(requested_destination)
            os.replace(rendered, destination)
            prepared = replace(prepared, relative_path=context.paths.relative_managed_path(destination))
            artifact = context.artifacts.register_in_session(
                session, destination, kind="export", role="export", session_id=session_id,
                parent_ids=parent_ids, settings=settings, metadata=metadata, _prepared=prepared,
            )
            _require_video_claim(session, cancel_event, session_id=session_id,
                                 job_id=job_id, lease_generation=lease_generation)
        committed = True
        return artifact
    finally:
        if destination is not None and not committed:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove unpublished video %s", destination, exc_info=True)
