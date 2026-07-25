"""Managed artifact registration and containment checks."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .database import Database
from .models import Artifact, ArtifactEdge, ExportRecord, utcnow
from .artifact_selection import activate_registered_artifact


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
    "bilingual_subtitle_overlay",
}


@dataclass(frozen=True, slots=True)
class PreparedArtifactRegistration:
    relative_path: str
    mime_type: str | None
    size_bytes: int
    content_hash: str | None
    settings_hash: str | None


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
        prepared = self.prepare_registration(
            path,
            calculate_hash=calculate_hash,
            settings=settings,
        )
        with self.database.session() as session:
            artifact = self.register_in_session(
                session,
                path,
                kind=kind,
                role=role,
                session_id=session_id,
                parent_ids=parent_ids,
                calculate_hash=calculate_hash,
                metadata=metadata,
                settings=settings,
                _prepared=prepared,
            )
            session.flush()
            session.expunge(artifact)
            return artifact

    def prepare_registration(
        self,
        path: Path,
        *,
        calculate_hash: bool = True,
        settings: dict | None = None,
    ) -> PreparedArtifactRegistration:
        """Read file metadata before opening a database write transaction."""

        relative_path = self.paths.relative_managed_path(path)
        stat = path.stat()
        content_hash = sha256_file(path) if calculate_hash else None
        settings_hash = (
            hashlib.sha256(
                json.dumps(
                    settings,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if settings is not None
            else None
        )
        return PreparedArtifactRegistration(
            relative_path=relative_path,
            mime_type=mimetypes.guess_type(path.name)[0],
            size_bytes=stat.st_size,
            content_hash=content_hash,
            settings_hash=settings_hash,
        )

    def register_in_session(
        self,
        session: Session,
        path: Path,
        *,
        kind: str,
        role: str = "artifact",
        session_id: str | None = None,
        parent_ids: list[str] | None = None,
        calculate_hash: bool = True,
        metadata: dict | None = None,
        settings: dict | None = None,
        _prepared: PreparedArtifactRegistration | None = None,
    ) -> Artifact:
        """Register an artifact inside the caller's transaction.

        Callers that also update takes, usage, or workflow state can use this
        method to make those related writes one atomic unit of work.
        """

        prepared = _prepared or self.prepare_registration(
            path,
            calculate_hash=calculate_hash,
            settings=settings,
        )
        relative_path = prepared.relative_path
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

        artifact = session.scalar(
            select(Artifact).where(Artifact.relative_path == relative_path)
        )
        created = artifact is None
        if artifact is None:
            artifact = Artifact(
                session_id=session_id,
                kind=kind,
                role=role,
                relative_path=relative_path,
                mime_type=prepared.mime_type,
                size_bytes=prepared.size_bytes,
                content_hash=prepared.content_hash,
                settings_hash=prepared.settings_hash,
                metadata_json=metadata or {},
            )
            session.add(artifact)
            session.flush()
        else:
            was_deleted = artifact.state == "deleted"
            artifact.session_id = session_id or artifact.session_id
            artifact.kind = kind
            artifact.role = role
            artifact.mime_type = prepared.mime_type
            artifact.size_bytes = prepared.size_bytes
            artifact.content_hash = (
                prepared.content_hash or artifact.content_hash
            )
            artifact.settings_hash = (
                prepared.settings_hash or artifact.settings_hash
            )
            artifact.state = "current"
            restored_metadata = dict(
                metadata
                if metadata is not None
                else artifact.metadata_json or {}
            )
            if was_deleted:
                restored_metadata.pop("deleted_at", None)
            artifact.metadata_json = restored_metadata
            artifact.updated_at = utcnow()

        normalized_parent_ids = list(dict.fromkeys(parent_ids or []))
        existing_parent_ids: set[str] = set()
        if normalized_parent_ids and not created:
            existing_parent_ids = set(
                session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == artifact.id,
                        ArtifactEdge.parent_artifact_id.in_(
                            normalized_parent_ids
                        ),
                    )
                ).all()
            )
        session.add_all(
            [
                ArtifactEdge(
                    parent_artifact_id=parent_id,
                    child_artifact_id=artifact.id,
                )
                for parent_id in normalized_parent_ids
                if parent_id not in existing_parent_ids
            ]
        )
        session.flush()
        activate_registered_artifact(session, artifact)
        session.flush()
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
            if artifact is None or artifact.state == "deleted":
                raise KeyError(artifact_id)
            path = self.paths.managed_path(artifact.relative_path)
            session.expunge(artifact)
        return artifact, path

    def remove_output(self, session_id: str, artifact_id: str) -> dict[str, str]:
        """Remove a finalized export without exposing arbitrary artifact deletion."""
        with self.database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None or artifact.state == "deleted" or artifact.session_id != session_id:
                raise KeyError(artifact_id)
            if not (
                artifact.kind == "export"
                or artifact.role == "export"
                or artifact.role.startswith("export_")
            ):
                raise ValueError("Only finalized exports can be removed from the Output tab.")
            path = self.paths.managed_path(artifact.relative_path)
            if path.exists():
                path.unlink()
            removed_at = utcnow()
            artifact.state = "deleted"
            artifact.metadata_json = {
                **dict(artifact.metadata_json or {}),
                "deleted_at": removed_at.isoformat(),
            }
            artifact.updated_at = removed_at
            for export in session.scalars(
                select(ExportRecord).where(ExportRecord.artifact_id == artifact.id)
            ).all():
                export.status = "deleted"
        return {"artifact_id": artifact_id, "state": "deleted"}

    def reconcile(self, session_id: str | None = None) -> list[dict]:
        reports: list[dict] = []
        with self.database.session() as session:
            statement = select(Artifact).where(Artifact.state != "deleted")
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

