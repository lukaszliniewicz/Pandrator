"""Bounded preparation and publication for managed global audio imports."""

from __future__ import annotations

import hashlib
import math
import mimetypes
import os
import stat
import tempfile
import uuid
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, PreparedArtifactRegistration
from .media_process import MediaProcessError, probe_audio_stream
from .models import Artifact

MAX_IMPORT_BYTES = 50 * 1024 * 1024
MAX_IMPORT_DURATION_SECONDS = 300.0
_SUPPORTED_SUFFIXES = frozenset({".wav", ".mp3", ".flac", ".ogg", ".m4a"})
_COPY_CHUNK_SIZE = 1024 * 1024
_AUDIO_ROLES = frozenset({"sound.original", "voice.reference"})


@dataclass(frozen=True, slots=True)
class PreparedAudioImport:
    staged_path: Path
    display_name: str
    suffix: str
    size_bytes: int
    sha256: str
    duration_seconds: float
    sample_rate: int
    channels: int
    frame_count: int | None
    registration: PreparedArtifactRegistration


def _staging_directory(paths: DataPaths) -> Path:
    directory = paths.root / "sounds" / "staging"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as source_file, destination.open("wb") as output:
        while chunk := source_file.read(_COPY_CHUNK_SIZE):
            size += len(chunk)
            if size > MAX_IMPORT_BYTES:
                raise ValueError("Sound imports cannot exceed 50 MiB.")
            output.write(chunk)
            digest.update(chunk)
        output.flush()
        os.fsync(output.fileno())
    if size == 0:
        raise ValueError("The sound import is empty.")
    return size, digest.hexdigest()


def _wav_frames(path: Path) -> int | None:
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            return frames if frames > 0 else None
    except (wave.Error, EOFError, OSError):
        return None


def prepare_audio_import(
    paths: DataPaths,
    artifacts: ArtifactService,
    staged_path: Path,
    display_name: str,
) -> PreparedAudioImport:
    """Copy, hash, and inspect a private upload before a writer transaction."""
    source = Path(staged_path)
    if not isinstance(display_name, str) or not display_name or len(display_name) > 255:
        raise ValueError("display_name must contain 1 to 255 characters.")
    try:
        source_stat = source.lstat()
    except OSError as error:
        raise ValueError("The staged sound file is unavailable.") from error
    if not stat.S_ISREG(source_stat.st_mode):
        raise ValueError("The staged sound file must be a regular file.")
    if source_stat.st_size <= 0 or source_stat.st_size > MAX_IMPORT_BYTES:
        raise ValueError("Sound imports must be between 1 byte and 50 MiB.")
    suffix = source.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ValueError("Supported sound imports are WAV, MP3, FLAC, OGG, and M4A.")

    directory = _staging_directory(paths)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="audio-import-", suffix=suffix, dir=directory
    )
    os.close(descriptor)
    prepared_path = Path(temporary_name)
    try:
        size_bytes, digest = _copy_and_hash(source, prepared_path)
        info = probe_audio_stream(prepared_path)
        duration_seconds = float(info.duration_ms) / 1000.0
        if not math.isfinite(duration_seconds) or not 0 < duration_seconds <= MAX_IMPORT_DURATION_SECONDS:
            raise ValueError("Imported audio must be finite and no longer than 300 seconds.")
        if info.sample_rate_hz <= 0 or info.channels <= 0:
            raise MediaProcessError("The import has invalid audio stream metadata.")
        registration = replace(
            artifacts.prepare_registration(prepared_path, calculate_hash=False),
            mime_type=mimetypes.guess_type(f"audio{suffix}")[0]
            or "application/octet-stream",
            size_bytes=size_bytes,
            content_hash=digest,
        )
        return PreparedAudioImport(
            staged_path=prepared_path,
            display_name=display_name,
            suffix=suffix,
            size_bytes=size_bytes,
            sha256=digest,
            duration_seconds=duration_seconds,
            sample_rate=info.sample_rate_hz,
            channels=info.channels,
            frame_count=_wav_frames(prepared_path) if suffix == ".wav" else None,
            registration=registration,
        )
    except Exception:
        prepared_path.unlink(missing_ok=True)
        raise


def cleanup_prepared_audio_import(prepared: PreparedAudioImport) -> None:
    """Remove only the private preparation copy; safe after successful consume."""
    try:
        prepared.staged_path.unlink(missing_ok=True)
    except OSError:
        return


def _remove_if_same(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except OSError:
        return


def _rollback_cleanup(session: Session, path: Path, identity: tuple[int, int]) -> None:
    connection = session.connection()
    state = {"finished": False}

    def rolled_back(_connection: Connection) -> None:
        if not state["finished"]:
            state["finished"] = True
            _remove_if_same(path, identity)

    def committed(_connection: Connection) -> None:
        state["finished"] = True

    # Database.immediate_session commits/rolls back its Connection directly,
    # outside Session.commit()/rollback(). Connection events cover both that
    # transaction boundary and ordinary caller-owned SQLAlchemy sessions.
    event.listen(connection, "rollback", rolled_back, once=True)
    event.listen(connection, "commit", committed, once=True)


def publish_audio_import_in_session(
    session: Session,
    prepared: PreparedAudioImport,
    *,
    artifacts: ArtifactService,
    paths: DataPaths,
    role: Literal["sound.original", "voice.reference"],
) -> Artifact:
    """Publish an immutable, global audio artifact under one fixed domain role."""
    if role not in _AUDIO_ROLES:
        raise ValueError("Unsupported managed audio import role.")
    source = prepared.staged_path
    try:
        source_stat = source.lstat()
    except OSError as error:
        raise ValueError("The prepared audio import is no longer available.") from error
    if not stat.S_ISREG(source_stat.st_mode):
        raise ValueError("The prepared audio import must be a regular file.")

    directory = paths.root / "sounds" / "audio"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    destination: Path | None = None
    identity: tuple[int, int] | None = None
    try:
        for _ in range(8):
            candidate = directory / f"{uuid.uuid4()}{prepared.suffix}"
            relative_path = paths.relative_managed_path(candidate)
            already_registered = session.scalar(
                select(Artifact.id).where(Artifact.relative_path == relative_path)
            )
            if already_registered is not None:
                continue
            try:
                # A hard link is an atomic, no-clobber publication on the
                # shared managed filesystem; unlike replace(), it cannot
                # overwrite a prior immutable artifact path.
                os.link(source, candidate)
            except FileExistsError:
                continue
            destination = candidate
            destination_stat = destination.lstat()
            identity = (destination_stat.st_dev, destination_stat.st_ino)
            source.unlink()
            registration = replace(
                prepared.registration,
                relative_path=relative_path,
            )
            artifact = artifacts.register_in_session(
                session,
                destination,
                kind="audio",
                role=role,
                session_id=None,
                calculate_hash=False,
                metadata={"operation": "import"},
                _prepared=registration,
            )
            _rollback_cleanup(session, destination, identity)
            return artifact
        raise RuntimeError("Could not allocate an unused managed audio path.")
    except Exception:
        if destination is not None and identity is not None:
            _remove_if_same(destination, identity)
        raise


__all__ = [
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_DURATION_SECONDS",
    "PreparedAudioImport",
    "cleanup_prepared_audio_import",
    "prepare_audio_import",
    "publish_audio_import_in_session",
]
