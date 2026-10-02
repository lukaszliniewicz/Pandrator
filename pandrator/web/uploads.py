"""Bounded, resumable chunk uploads suitable for Waitress request buffering."""

from __future__ import annotations

import hashlib
import math
import mimetypes
import os
import re
import shutil
import stat
import threading
import uuid
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.utils import secure_filename

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import SessionRecord, UploadSessionRecord, utcnow
from .source_library import SourceLibraryService
from .upload_activity import UploadBusy, upload_activity

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MAX_CHUNK_SIZE = 16 * 1024 * 1024
DEFAULT_MAX_UPLOAD_SIZE = 100 * 1024 * 1024 * 1024


def managed_upload_directory(paths: DataPaths, upload_id: str, relative: str) -> Path:
    """Accept only the upload's canonical directory, without following symlinks."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", upload_id):
        raise ValueError("Upload ID is not a safe directory name.")
    directory = paths.temporary / "uploads" / upload_id
    expected = directory.relative_to(paths.root).as_posix()
    if relative != expected:
        raise ValueError("Upload temporary path is not its canonical directory.")
    current = paths.root
    for part in Path(relative).parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise ValueError("Upload temporary path contains a symlink or non-directory.")
    return directory


def _regular_upload_file(path: Path) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return path
    if not stat.S_ISREG(mode):
        raise ValueError("Upload contains a symlink or nonregular file.")
    return path


class ChunkUploadService:
    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        artifacts: ArtifactService,
        sources: SourceLibraryService,
    ):
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
        with self._lock, self.database.immediate_session() as session:
            record = self.initialize_in_session(
                session,
                filename=filename,
                size_bytes=size_bytes,
                mime_type=mime_type,
                session_id=session_id,
                expected_hash=expected_hash,
                chunk_size=chunk_size,
                max_size=max_size,
            )
            result = self.status_payload(record)
        return result

    def initialize_in_session(
        self,
        db_session: Session,
        *,
        filename: str,
        size_bytes: int,
        mime_type: str | None = None,
        session_id: str | None = None,
        expected_hash: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_size: int = DEFAULT_MAX_UPLOAD_SIZE,
        upload_id: str | None = None,
    ) -> UploadSessionRecord:
        safe_name = secure_filename(filename) or f"upload-{uuid.uuid4()}"
        size_bytes = int(size_bytes)
        chunk_size = max(1024 * 1024, min(int(chunk_size), MAX_CHUNK_SIZE))
        if size_bytes <= 0 or size_bytes > max_size:
            raise ValueError(
                f"Upload size must be between 1 byte and {max_size} bytes."
            )
        if expected_hash and (
            len(expected_hash) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_hash)
        ):
            raise ValueError("Expected SHA-256 is invalid.")
        upload_id = str(upload_id or uuid.uuid4())
        self._writable_owner(db_session, session_id)
        relative = (self.paths.temporary / "uploads" / upload_id).relative_to(self.paths.root).as_posix()
        directory = managed_upload_directory(self.paths, upload_id, relative)
        directory.mkdir(parents=True, exist_ok=False)
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
        db_session.add(record)
        db_session.flush()
        return record

    @staticmethod
    def _writable_owner(session: Session, session_id: str | None) -> None:
        if session_id is None:
            return
        owner = session.get(SessionRecord, session_id)
        if owner is None:
            raise KeyError(session_id)
        if owner.trashed_at is not None or owner.status in {"trashed", "purging"}:
            raise ValueError("Upload session is trashed or purging.")

    def _current(self, session: Session, upload_id: str, *, replay: bool = False) -> UploadSessionRecord:
        record = session.get(UploadSessionRecord, upload_id)
        if record is None:
            raise KeyError(upload_id)
        self._writable_owner(session, record.session_id)
        if replay and record.state == "completed":
            return record
        expires_at = record.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if record.state != "open" or expires_at <= utcnow():
            raise ValueError("Upload is not open.")
        return record

    def _record(self, upload_id: str) -> UploadSessionRecord:
        with self.database.session() as session:
            record = session.get(UploadSessionRecord, upload_id)
            if record is None:
                raise KeyError(upload_id)
            session.expunge(record)
            return record

    def status(self, upload_id: str) -> dict:
        record = self._record(upload_id)
        return self.status_payload(record)

    @staticmethod
    def status_payload(record: UploadSessionRecord) -> dict:
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
            "result": dict(record.result_json or {}) or None,
            "expires_at": record.expires_at.isoformat(),
        }

    @staticmethod
    def _same_owner(initial: UploadSessionRecord, current: UploadSessionRecord) -> None:
        if (current.session_id != initial.session_id
                or current.temporary_relative_path != initial.temporary_relative_path):
            raise ValueError("Upload ownership or temporary path changed.")

    def _locked_record(self, initial: UploadSessionRecord, *, replay: bool = False) -> UploadSessionRecord:
        with self.database.session() as session:
            current = self._current(session, initial.id, replay=replay)
            self._same_owner(initial, current)
            session.expunge(current)
            return current

    def write_chunk(
        self,
        upload_id: str,
        index: int,
        stream: BinaryIO,
        *,
        supplied_hash: str | None = None,
    ) -> dict:
        initial = self._record(upload_id)
        with upload_activity(self.paths, session_id=initial.session_id, upload_id=upload_id):
            record = self._locked_record(initial)
            index = int(index)
            if index < 0 or index >= record.chunk_count:
                raise ValueError("Chunk index is outside the upload.")
            expected_size = (
                record.chunk_size if index < record.chunk_count - 1
                else record.size_bytes - record.chunk_size * (record.chunk_count - 1)
            )
            directory = managed_upload_directory(self.paths, record.id, record.temporary_relative_path)
            destination = _regular_upload_file(directory / f"{index:08d}.part")
            temporary = directory / f"{index:08d}-{uuid.uuid4()}.tmp"
            digest = hashlib.sha256()
            written = 0
            try:
                # The potentially slow request stream never holds a SQLite writer.
                with temporary.open("xb") as output:
                    while chunk := stream.read(1024 * 1024):
                        written += len(chunk)
                        if written > expected_size:
                            raise ValueError("Chunk is larger than expected.")
                        output.write(chunk)
                        digest.update(chunk)
                if written != expected_size:
                    raise ValueError(f"Chunk size mismatch: expected {expected_size}, received {written}.")
                actual_hash = digest.hexdigest()
                if supplied_hash and supplied_hash.lower() != actual_hash:
                    raise ValueError("Chunk SHA-256 mismatch.")
                with self.database.immediate_session() as session:
                    current = self._current(session, upload_id)
                    self._same_owner(record, current)
                    managed_upload_directory(self.paths, current.id, current.temporary_relative_path)
                    _regular_upload_file(destination)
                    os.replace(temporary, destination)
                    received = dict(current.received_json or {})
                    received[str(index)] = actual_hash
                    current.received_json = received
                    current.updated_at = utcnow()
                return {"index": index, "size_bytes": written, "sha256": actual_hash}
            finally:
                temporary.unlink(missing_ok=True)

    def complete(self, upload_id: str) -> dict:
        initial = self._record(upload_id)
        with upload_activity(self.paths, session_id=initial.session_id, upload_id=upload_id):
            record = self._locked_record(initial, replay=True)
            if record.state == "completed":
                if record.result_json:
                    return dict(record.result_json)
                raise ValueError("The completed upload has no replayable result.")
            received = record.received_json or {}
            missing = [index for index in range(record.chunk_count) if str(index) not in received]
            if missing:
                raise ValueError(f"Upload is incomplete; missing {len(missing)} chunk(s).")
            directory = managed_upload_directory(self.paths, record.id, record.temporary_relative_path)
            assembled = directory / f"assembled-{uuid.uuid4()}.part"
            destination = self.paths.uploads / f"{uuid.uuid4()}-{record.filename}"
            digest = hashlib.sha256()
            size = 0
            try:
                with assembled.open("xb") as output:
                    for index in range(record.chunk_count):
                        part = _regular_upload_file(directory / f"{index:08d}.part")
                        with part.open("rb") as source:
                            while chunk := source.read(1024 * 1024):
                                output.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                actual_hash = digest.hexdigest()
                if size != record.size_bytes:
                    raise ValueError("Assembled upload size does not match the declared size.")
                if record.expected_hash and record.expected_hash != actual_hash:
                    raise ValueError("Assembled upload SHA-256 mismatch.")
                prepared = replace(
                    self.artifacts.prepare_registration(assembled, calculate_hash=False),
                    relative_path=destination.relative_to(self.paths.root).as_posix(),
                    mime_type=mimetypes.guess_type(destination.name)[0],
                )
                with self.database.immediate_session() as session:
                    current = self._current(session, upload_id)
                    self._same_owner(record, current)
                    managed_upload_directory(self.paths, current.id, current.temporary_relative_path)
                    os.replace(assembled, destination)
                    artifact = self.artifacts.register_in_session(
                        session, destination, kind="source", role="upload",
                        session_id=current.session_id, calculate_hash=False,
                        metadata={"original_filename": current.filename, "upload_id": current.id},
                        _prepared=prepared,
                    )
                    artifact.content_hash = actual_hash
                    asset = self.sources.ensure_for_artifact_in_session(
                        session, artifact.id, display_name=current.filename,
                        kind=Path(current.filename).suffix.lower().lstrip(".") or "file",
                    )
                    attachment = (
                        self.sources.attach(current.session_id, asset.id, db_session=session)
                        if current.session_id else None
                    )
                    result = {
                        "upload_id": upload_id, "artifact_id": artifact.id,
                        "source_asset_id": asset.id, "attachment": attachment,
                        "filename": current.filename, "size_bytes": size, "sha256": actual_hash,
                    }
                    current.state = "completed"
                    current.result_json = result
                    current.updated_at = utcnow()
            except Exception:
                # Both candidates belong to this attempt; original chunks survive.
                assembled.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
            # Registration has committed. Cleanup failures must preserve its artifact.
            with self.database.session() as session:
                current = session.get(UploadSessionRecord, upload_id)
                if current is None or current.state != "completed":
                    return result
                self._same_owner(record, current)
                try:
                    self._writable_owner(session, current.session_id)
                except (KeyError, ValueError):
                    return result
                directory = managed_upload_directory(self.paths, current.id, current.temporary_relative_path)
            shutil.rmtree(directory, ignore_errors=True)
            return result

    def cancel(self, upload_id: str) -> None:
        initial = self._record(upload_id)
        with upload_activity(self.paths, session_id=initial.session_id, upload_id=upload_id):
            record = self._locked_record(initial)
            directory = managed_upload_directory(self.paths, record.id, record.temporary_relative_path)
            if directory.exists():
                shutil.rmtree(directory)
            with self.database.immediate_session() as session:
                current = self._current(session, upload_id)
                self._same_owner(record, current)
                current.state = "canceled"
                current.updated_at = utcnow()

    def cleanup_expired(self) -> int:
        removed = 0
        with self.database.session() as session:
            identifiers = list(session.scalars(select(UploadSessionRecord.id).where(
                UploadSessionRecord.expires_at <= utcnow(), UploadSessionRecord.state == "open",
            )).all())
        for identifier in identifiers:
            try:
                initial = self._record(identifier)
                with upload_activity(self.paths, session_id=initial.session_id, upload_id=identifier):
                    with self.database.session() as session:
                        current = session.get(UploadSessionRecord, identifier)
                        if current is None or current.state != "open":
                            continue
                        self._same_owner(initial, current)
                        self._writable_owner(session, current.session_id)
                        expires_at = current.expires_at
                        if expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=UTC)
                        if expires_at > utcnow():
                            continue
                        directory = managed_upload_directory(self.paths, current.id, current.temporary_relative_path)
                    if directory.exists():
                        shutil.rmtree(directory)
                    with self.database.immediate_session() as session:
                        current = session.get(UploadSessionRecord, identifier)
                        if current is None or current.state != "open":
                            continue
                        self._same_owner(initial, current)
                        self._writable_owner(session, current.session_id)
                        current.state = "expired"
                        current.updated_at = utcnow()
                    removed += 1
            except (KeyError, UploadBusy):
                continue
            except ValueError:
                # Trashed owners remain retryable through their purge journal.
                continue
        return removed
