"""Managed artifact registration and containment checks."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .database import Database
from .models import Artifact, ArtifactEdge, utcnow


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
    ) -> Artifact:
        relative_path = self.paths.relative_managed_path(path)
        stat = path.stat()
        content_hash = sha256_file(path) if calculate_hash else None
        mime_type = mimetypes.guess_type(path.name)[0]
        with self.database.session() as session:
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
                artifact.metadata_json = metadata or artifact.metadata_json
                artifact.updated_at = utcnow()

            for parent_id in parent_ids or []:
                edge = session.get(ArtifactEdge, (parent_id, artifact.id))
                if edge is None:
                    session.add(ArtifactEdge(parent_artifact_id=parent_id, child_artifact_id=artifact.id))
            session.flush()
            session.expunge(artifact)
            return artifact

    def resolve(self, artifact_id: str) -> tuple[Artifact, Path]:
        with self.database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None:
                raise KeyError(artifact_id)
            path = self.paths.managed_path(artifact.relative_path)
            session.expunge(artifact)
        return artifact, path

    def reconcile(self) -> list[dict]:
        reports: list[dict] = []
        with self.database.session() as session:
            artifacts = list(session.scalars(select(Artifact)).all())
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

