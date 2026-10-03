"""Publish edited PDFs and provenance together without reusing prior outputs."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import Artifact, Job, utcnow

logger = logging.getLogger(__name__)


def _require_pdf_claim(
    session: Session,
    cancel_event: threading.Event,
    *,
    job_id: str | None,
    lease_generation: int | None,
) -> None:
    if cancel_event.is_set():
        raise InterruptedError("PDF editing was canceled or its worker lease was lost.")
    # Direct callers have no durable job. Workers always supply both fields;
    # generic administrative jobs may have no queue session ID.
    if job_id is None and lease_generation is None:
        return
    current = session.scalar(
        select(Job.id).where(
            Job.id == job_id,
            Job.kind == "pdf.apply_edits",
            Job.status == "running",
            Job.lease_owner.is_not(None),
            Job.lease_generation == lease_generation,
            Job.lease_expires_at > utcnow(),
        )
    ) if job_id is not None and lease_generation is not None else None
    if current is None:
        raise InterruptedError("PDF editing was canceled or its worker lease is no longer current.")


def _allocate_pdf_pair(
    session: Session, paths: DataPaths, requested_destination: Path,
) -> tuple[Path, Path]:
    # The caller holds the SQLite writer reservation across allocation and
    # registration. Both the PDF and sidecar, including historical rows, count.
    for version in range(1, 100_000):
        destination = (
            requested_destination
            if version == 1
            else requested_destination.with_name(
                f"{requested_destination.stem}_{version}{requested_destination.suffix}"
            )
        )
        manifest = destination.with_suffix(destination.suffix + ".pandrator.json")
        if destination.exists() or manifest.exists():
            continue
        registered = session.scalar(
            select(Artifact.id)
            .where(Artifact.relative_path.in_(
                [paths.relative_managed_path(destination), paths.relative_managed_path(manifest)]
            ))
            .limit(1)
        )
        if registered is None:
            return destination, manifest
    raise RuntimeError(f"Could not allocate an edited PDF path for {requested_destination.name}.")


def publish_pdf_edit(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    rendered: Path,
    rendered_manifest: Path,
    provenance: dict[str, Any],
    requested_destination: Path,
    *,
    source_artifact_id: str,
    session_id: str | None,
    cancel_event: threading.Event,
    job_id: str | None = None,
    lease_generation: int | None = None,
) -> tuple[Artifact, Artifact]:
    """Fence one native edit attempt and commit its two artifact records together.

    Rendering and PDF hashing occur before the writer reservation. Final path
    allocation, file moves, registration and claim validation share it. Cleanup
    owns only files moved by this attempt; committed pairs survive late cancel.
    """
    prepared_pdf = artifacts.prepare_registration(rendered)
    moved: list[Path] = []
    committed = False
    try:
        with database.immediate_session() as session:
            _require_pdf_claim(
                session, cancel_event, job_id=job_id, lease_generation=lease_generation,
            )
            destination, manifest = _allocate_pdf_pair(
                session, paths, requested_destination.resolve(),
            )
            published_provenance = {
                **provenance,
                "output": {**provenance["output"], "path": str(destination)},
            }
            rendered_manifest.write_text(json.dumps(published_provenance, indent=2), encoding="utf-8")
            prepared_manifest = artifacts.prepare_registration(rendered_manifest)
            os.replace(rendered, destination)
            moved.append(destination)
            os.replace(rendered_manifest, manifest)
            moved.append(manifest)
            output_artifact = artifacts.register_in_session(
                session, destination, kind="pdf", role="pdf_edited", session_id=session_id,
                parent_ids=[source_artifact_id],
                metadata={"provenance_manifest": paths.relative_managed_path(manifest)},
                _prepared=replace(prepared_pdf, relative_path=paths.relative_managed_path(destination)),
            )
            manifest_artifact = artifacts.register_in_session(
                session, manifest, kind="json", role="provenance", session_id=session_id,
                parent_ids=[source_artifact_id, output_artifact.id],
                _prepared=replace(prepared_manifest, relative_path=paths.relative_managed_path(manifest)),
            )
            _require_pdf_claim(
                session, cancel_event, job_id=job_id, lease_generation=lease_generation,
            )
        committed = True
        return output_artifact, manifest_artifact
    finally:
        if not committed:
            for path in moved:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove unpublished PDF output %s", path, exc_info=True)
