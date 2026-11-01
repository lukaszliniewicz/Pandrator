"""Sound-library import preparation and candidate creation."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .access.contracts import ProjectAction
from .access.sound_library_policy import require_sound_library_workspace
from .artifacts import ArtifactService
from .audio_imports import (
    PreparedAudioImport,
    cleanup_prepared_audio_import,
    prepare_audio_import,
    publish_audio_import_in_session,
)
from .database import Database
from .models import SoundVariant
from .sound_library_queries import candidate_summary

PreparedSoundImport = PreparedAudioImport


def prepare_import(
    paths: DataPaths,
    artifacts: ArtifactService,
    path: Path,
    display_name: str,
) -> PreparedSoundImport:
    return prepare_audio_import(paths, artifacts, path, display_name)


def cleanup_prepared_import(prepared: PreparedSoundImport) -> None:
    cleanup_prepared_audio_import(prepared)


def import_in_session(
    session: Session,
    paths: DataPaths,
    artifacts: ArtifactService,
    prepared: PreparedSoundImport,
    *,
    workspace_id: str | None = None,
) -> dict:
    workspace = require_sound_library_workspace(
        session, ProjectAction.EDIT, workspace_id=workspace_id
    )
    artifact = publish_audio_import_in_session(
        session,
        prepared,
        artifacts=artifacts,
        paths=paths,
        role="sound.original",
        workspace_id=workspace.id,
    )
    variant = SoundVariant(
        workspace_id=workspace.id,
        artifact_id=artifact.id,
        state="ready",
        provenance_json={
            "operation": "import",
            "display_name": prepared.display_name,
        },
        sample_rate=prepared.sample_rate,
        channels=prepared.channels,
        frame_count=prepared.frame_count,
        duration_seconds=prepared.duration_seconds,
    )
    session.add(variant)
    session.flush()
    return {
        "candidate": candidate_summary(variant),
        "artifact_id": artifact.id,
        "sha256": prepared.sha256,
        "size_bytes": prepared.size_bytes,
    }


def import_audio(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    path: Path,
    display_name: str,
    *,
    db_session: Session | None = None,
    workspace_id: str | None = None,
) -> dict:
    if db_session is not None:
        workspace_id = require_sound_library_workspace(
            db_session, ProjectAction.EDIT, workspace_id=workspace_id
        ).id
    else:
        with database.snapshot_session() as session:
            workspace_id = require_sound_library_workspace(
                session, ProjectAction.EDIT, workspace_id=workspace_id
            ).id
    prepared = prepare_import(paths, artifacts, path, display_name)
    try:
        if db_session is not None:
            return import_in_session(
                db_session,
                paths,
                artifacts,
                prepared,
                workspace_id=workspace_id,
            )
        with database.immediate_session() as session:
            return import_in_session(
                session,
                paths,
                artifacts,
                prepared,
                workspace_id=workspace_id,
            )
    finally:
        cleanup_prepared_import(prepared)


__all__ = [
    "PreparedSoundImport",
    "cleanup_prepared_import",
    "import_audio",
    "import_in_session",
    "prepare_import",
]
