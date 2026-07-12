"""Managed artifact registration and containment checks."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .database import Database
from .models import Artifact, ArtifactEdge, utcnow


SINGLETON_SESSION_ROLES = {
    "transcription",
    "correction",
    "translation",
    "tts_optimized",
    "reviewed_transcription",
    "reviewed_correction",
    "reviewed_translation",
    "clean_text",
    "prepared_text",
    "speech_blocks",
    "dubbing_audio",
    "audiobook_audio",
    "assembled_audio",
    "bilingual_subtitle_overlay",
    "export",
}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def copy_stream_and_hash(source: BinaryIO, destination: Path, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as output:
        while chunk := source.read(chunk_size):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


class ArtifactService:
    def __init__(self, database: Database, paths: DataPaths):
        self.database = database
        self.paths = paths

    def register(
        self,
        path: Path,
        *,
        kind: str,
        role: str = "artifact",
        session_id: str | None = None,
        parent_ids: list[str] | None = None,
        calculate_hash: bool = True,
        metadata: dict | None = None,
        settings: dict | None = None,
    ) -> Artifact:
        relative_path = self.paths.relative_managed_path(path)
        stat = path.stat()
        content_hash = sha256_file(path) if calculate_hash else None
        settings_hash = (
            hashlib.sha256(
                json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest()
            if settings is not None
            else None
        )
        mime_type = mimetypes.guess_type(path.name)[0]
        with self.database.session() as session:
            replaced = list(
                session.scalars(
                    select(Artifact).where(
                        Artifact.session_id == session_id,
                        Artifact.role == role,
                        Artifact.state == "current",
                        Artifact.relative_path != relative_path,
                    )
                ).all()
            ) if session_id and role in SINGLETON_SESSION_ROLES else []
            for previous in replaced:
                previous.state = "stale"
                self._mark_descendants_stale(session, previous.id)

            artifact = session.scalar(select(Artifact).where(Artifact.relative_path == relative_path))
            if artifact is None:
                artifact = Artifact(
                    session_id=session_id,
                    kind=kind,
                    role=role,
                    relative_path=relative_path,
                    mime_type=mime_type,
                    size_bytes=stat.st_size,
                    content_hash=content_hash,
                    settings_hash=settings_hash,
                    metadata_json=metadata or {},
                )
                session.add(artifact)
                session.flush()
            else:
                artifact.session_id = session_id or artifact.session_id
                artifact.kind = kind
                artifact.role = role
                artifact.mime_type = mime_type
                artifact.size_bytes = stat.st_size
                artifact.content_hash = content_hash or artifact.content_hash
                artifact.settings_hash = settings_hash or artifact.settings_hash
                artifact.state = "current"
                artifact.metadata_json = metadata or artifact.metadata_json
                artifact.updated_at = utcnow()

            for parent_id in parent_ids or []:
                edge = session.get(ArtifactEdge, (parent_id, artifact.id))
                if edge is None:
                    session.add(ArtifactEdge(parent_artifact_id=parent_id, child_artifact_id=artifact.id))
            session.flush()
            session.expunge(artifact)
            return artifact

    @staticmethod
    def _mark_descendants_stale(session, artifact_id: str) -> None:
        """Invalidate derived artifacts while preserving every file for review."""
        pending = [artifact_id]
        visited: set[str] = set()
        while pending:
            parent_id = pending.pop()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            child_ids = list(
                session.scalars(
                    select(ArtifactEdge.child_artifact_id).where(ArtifactEdge.parent_artifact_id == parent_id)
                ).all()
            )
            for child_id in child_ids:
                child = session.get(Artifact, child_id)
                if child is not None and child.state == "current":
                    child.state = "stale"
                pending.append(child_id)

    def invalidate_descendants(self, artifact_id: str) -> None:
        with self.database.session() as session:
            if session.get(Artifact, artifact_id) is None:
                raise KeyError(artifact_id)
            self._mark_descendants_stale(session, artifact_id)

    def resolve(self, artifact_id: str) -> tuple[Artifact, Path]:
        with self.database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None:
                raise KeyError(artifact_id)
            path = self.paths.managed_path(artifact.relative_path)
            session.expunge(artifact)
        return artifact, path

    def reconcile(self, session_id: str | None = None) -> list[dict]:
        reports: list[dict] = []
        with self.database.session() as session:
            statement = select(Artifact)
            if session_id:
                statement = statement.where(Artifact.session_id == session_id)
            artifacts = list(session.scalars(statement).all())
            for artifact in artifacts:
                try:
                    path = self.paths.managed_path(artifact.relative_path)
                except ValueError as error:
                    reports.append({"artifact_id": artifact.id, "status": "escaped", "detail": str(error)})
                    continue
                if not path.is_file():
                    reports.append({"artifact_id": artifact.id, "status": "missing", "path": str(path)})
                    continue
                stat = path.stat()
                if artifact.size_bytes is not None and stat.st_size != artifact.size_bytes:
                    reports.append(
                        {
                            "artifact_id": artifact.id,
                            "status": "changed",
                            "path": str(path),
                            "expected_size": artifact.size_bytes,
                            "actual_size": stat.st_size,
                        }
                    )
        return reports

