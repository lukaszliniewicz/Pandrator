"""Bounded, resumable chunk uploads suitable for Waitress request buffering."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import threading
import uuid
from datetime import timedelta, timezone
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from werkzeug.utils import secure_filename

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .database import Database
from .models import SessionRecord, UploadSessionRecord, utcnow
from .workspace import SourceLibraryService


DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MAX_CHUNK_SIZE = 16 * 1024 * 1024
DEFAULT_MAX_UPLOAD_SIZE = 100 * 1024 * 1024 * 1024


class ChunkUploadService:
    def __init__(self, database: Database, paths: DataPaths, artifacts: ArtifactService, sources: SourceLibraryService):
        self.database = database
        self.paths = paths
        self.artifacts = artifacts
        self.sources = sources
        self._lock = threading.RLock()

    def initialize(
        self,
        *,
        filename: str,
        size_bytes: int,
        mime_type: str | None = None,
        session_id: str | None = None,
        expected_hash: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_size: int = DEFAULT_MAX_UPLOAD_SIZE,
    ) -> dict:
        safe_name = secure_filename(filename) or f"upload-{uuid.uuid4()}"
        size_bytes = int(size_bytes)
        chunk_size = max(1024 * 1024, min(int(chunk_size), MAX_CHUNK_SIZE))
        if size_bytes <= 0 or size_bytes > max_size:
            raise ValueError(f"Upload size must be between 1 byte and {max_size} bytes.")
        if expected_hash and (len(expected_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_hash)):
            raise ValueError("Expected SHA-256 is invalid.")
        upload_id = str(uuid.uuid4())
        relative = self.paths.relative_managed_path(self.paths.temporary / "uploads" / upload_id)
        (self.paths.root / relative).mkdir(parents=True, exist_ok=False)
        record = UploadSessionRecord(
            id=upload_id,
            session_id=session_id,
            filename=safe_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            chunk_size=chunk_size,
            chunk_count=math.ceil(size_bytes / chunk_size),
            received_json={},
            expected_hash=expected_hash.lower() if expected_hash else None,
            temporary_relative_path=relative,
            expires_at=utcnow() + timedelta(hours=24),
        )
        with self.database.session() as session:
            if session_id and session.get(SessionRecord, session_id) is None:
                shutil.rmtree(self.paths.root / relative, ignore_errors=True)
                raise KeyError(session_id)
            session.add(record)
        return self.status(upload_id)

    def _record(self, upload_id: str) -> UploadSessionRecord:
        with self.database.session() as session:
            record = session.get(UploadSessionRecord, upload_id)
            if record is None:
                raise KeyError(upload_id)
            session.expunge(record)
            return record

    def status(self, upload_id: str) -> dict:
        record = self._record(upload_id)
        return {
            "id": record.id,
            "session_id": record.session_id,
            "filename": record.filename,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "chunk_size": record.chunk_size,
            "chunk_count": record.chunk_count,
            "received": sorted(int(index) for index in (record.received_json or {})),
            "state": record.state,
            "expires_at": record.expires_at.isoformat(),
        }

    def write_chunk(self, upload_id: str, index: int, stream: BinaryIO, *, supplied_hash: str | None = None) -> dict:
        with self._lock:
            record = self._record(upload_id)
            expires_at = record.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if record.state != "open" or expires_at <= utcnow():
                raise ValueError("Upload is not open.")
            index = int(index)
            if index < 0 or index >= record.chunk_count:
                raise ValueError("Chunk index is outside the upload.")
            expected_size = record.chunk_size if index < record.chunk_count - 1 else record.size_bytes - record.chunk_size * (record.chunk_count - 1)
            destination = self.paths.managed_path(record.temporary_relative_path) / f"{index:08d}.part"
            temporary = destination.with_suffix(".tmp")
            digest = hashlib.sha256()
            written = 0
            with temporary.open("wb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > expected_size:
                        raise ValueError("Chunk is larger than expected.")
                    output.write(chunk)
                    digest.update(chunk)
            if written != expected_size:
                temporary.unlink(missing_ok=True)
                raise ValueError(f"Chunk size mismatch: expected {expected_size}, received {written}.")
            actual_hash = digest.hexdigest()
            if supplied_hash and supplied_hash.lower() != actual_hash:
                temporary.unlink(missing_ok=True)
                raise ValueError("Chunk SHA-256 mismatch.")
            os.replace(temporary, destination)
            with self.database.session() as session:
                current = session.get(UploadSessionRecord, upload_id)
                received = dict(current.received_json or {})
                received[str(index)] = actual_hash
                current.received_json = received
                current.updated_at = utcnow()
            return {"index": index, "size_bytes": written, "sha256": actual_hash}

    def complete(self, upload_id: str) -> dict:
        with self._lock:
            record = self._record(upload_id)
            if record.state == "completed":
                raise ValueError("Upload has already been completed.")
            received = record.received_json or {}
            missing = [index for index in range(record.chunk_count) if str(index) not in received]
            if missing:
                raise ValueError(f"Upload is incomplete; missing {len(missing)} chunk(s).")
            directory = self.paths.managed_path(record.temporary_relative_path)
            assembled = directory / "assembled.part"
            digest = hashlib.sha256()
            size = 0
            with assembled.open("xb") as output:
                for index in range(record.chunk_count):
                    part = directory / f"{index:08d}.part"
                    with part.open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
            actual_hash = digest.hexdigest()
            if size != record.size_bytes:
                assembled.unlink(missing_ok=True)
                raise ValueError("Assembled upload size does not match the declared size.")
            if record.expected_hash and record.expected_hash != actual_hash:
                assembled.unlink(missing_ok=True)
                raise ValueError("Assembled upload SHA-256 mismatch.")
            destination = self.paths.uploads / f"{uuid.uuid4()}-{record.filename}"
            os.replace(assembled, destination)
            artifact = self.artifacts.register(
                destination,
                kind="source",
                role="upload",
                session_id=record.session_id,
                calculate_hash=False,
                metadata={"original_filename": record.filename, "upload_id": record.id},
            )
            with self.database.session() as session:
                managed = session.get(type(artifact), artifact.id)
                managed.content_hash = actual_hash
                current = session.get(UploadSessionRecord, upload_id)
                current.state = "completed"
                current.updated_at = utcnow()
            asset = self.sources.ensure_for_artifact(artifact.id, display_name=record.filename, kind=Path(record.filename).suffix.lower().lstrip(".") or "file")
            attachment = self.sources.attach(record.session_id, asset.id) if record.session_id else None
            shutil.rmtree(directory, ignore_errors=True)
            return {"upload_id": upload_id, "artifact_id": artifact.id, "source_asset_id": asset.id, "attachment": attachment, "filename": record.filename, "size_bytes": size, "sha256": actual_hash}

    def cancel(self, upload_id: str) -> None:
        with self._lock:
            record = self._record(upload_id)
            with self.database.session() as session:
                current = session.get(UploadSessionRecord, upload_id)
                current.state = "canceled"
                current.updated_at = utcnow()
            shutil.rmtree(self.paths.managed_path(record.temporary_relative_path), ignore_errors=True)

    def cleanup_expired(self) -> int:
        removed = 0
        with self.database.session() as session:
            records = list(session.scalars(select(UploadSessionRecord).where(UploadSessionRecord.expires_at <= utcnow(), UploadSessionRecord.state == "open")).all())
            paths = [record.temporary_relative_path for record in records]
            for record in records:
                record.state = "expired"
                record.updated_at = utcnow()
                removed += 1
        for relative in paths:
            shutil.rmtree(self.paths.managed_path(relative), ignore_errors=True)
        return removed
