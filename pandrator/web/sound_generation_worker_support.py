"""Lease checks and private-file helpers for sound generation workers."""

from __future__ import annotations

import math
import os
import tempfile
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .job_execution import JobLease, current_job_lease
from .models import Artifact, Job, SoundVariant, utcnow


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _lease(payload: dict[str, Any]) -> JobLease:
    lease = current_job_lease.get()
    if (
        lease is None
        or lease.get("job_id") != payload.get("_job_id")
        or lease.get("lease_generation") != payload.get("_lease_generation")
    ):
        raise RuntimeError("Sound generation requires its current worker lease.")
    return lease


def _require_job(
    session: Session,
    payload: dict[str, Any],
    lease: JobLease,
    cancel_event: threading.Event,
    *,
    check_cancel: bool = True,
) -> tuple[Job, SoundVariant]:
    job_id = str(payload.get("_job_id") or "")
    candidate_id = str(payload.get("candidate_id") or "")
    job = session.get(Job, job_id, populate_existing=True)
    candidate = session.get(SoundVariant, candidate_id, populate_existing=True)
    if (
        job is None
        or candidate is None
        or job.kind != "sound.generate"
        or job.session_id is not None
        or job.payload_json.get("candidate_id") != candidate_id
        or job.payload_json.get("request") != payload.get("request")
        or job.payload_json.get("runtime_identity") != payload.get("runtime_identity")
        or candidate.job_id != job.id
        or job.lease_owner != lease["owner_id"]
        or job.lease_generation != lease["lease_generation"]
        or job.status != "running"
        or job.lease_expires_at is None
        or _aware(job.lease_expires_at) <= utcnow()
        or (check_cancel and cancel_event.is_set())
    ):
        raise RuntimeError("Sound generation lease or cancellation changed.")
    return job, candidate


def _valid_output_properties(properties: Mapping[str, Any]) -> tuple[int, int, int, float]:
    try:
        frames = properties["frames"]
        sample_rate = properties["sample_rate"]
        channels = properties["channels"]
        duration = float(properties["duration_seconds"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("audio.cpp returned incomplete WAV metadata.") from error
    if (
        type(frames) is not int
        or type(sample_rate) is not int
        or type(channels) is not int
        or frames <= 0
        or sample_rate <= 0
        or channels <= 0
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise ValueError("audio.cpp returned invalid WAV metadata.")
    return frames, sample_rate, channels, duration


def _artifact_is_present(session: Session, paths: DataPaths, candidate: SoundVariant) -> bool:
    if candidate.state != "ready" or not candidate.artifact_id:
        return False
    artifact = session.get(Artifact, candidate.artifact_id)
    if (
        artifact is None
        or artifact.kind != "audio"
        or artifact.role != "sound.original"
        or artifact.session_id is not None
        or artifact.state != "current"
    ):
        return False
    try:
        return paths.managed_path(artifact.relative_path).is_file()
    except (OSError, ValueError):
        return False


def _result(job: Job, candidate: SoundVariant) -> dict[str, Any]:
    result = {
        "candidate_id": candidate.id,
        "job_id": job.id,
        "artifact_id": candidate.artifact_id,
        "status": "ready",
    }
    if candidate.item_id is not None:
        result["item_id"] = candidate.item_id
    return result


def _private_output_path(paths: DataPaths) -> Path:
    directory = paths.root / "sounds" / "staging"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    descriptor, name = tempfile.mkstemp(prefix="sound-generate-", suffix=".wav", dir=directory)
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _public_output_directory(paths: DataPaths) -> Path:
    directory = paths.root / "sounds" / "audio"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


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

    def rolled_back(_connection) -> None:
        if not state["finished"]:
            state["finished"] = True
            _remove_if_same(path, identity)

    def committed(_connection) -> None:
        state["finished"] = True

    event.listen(connection, "rollback", rolled_back, once=True)
    event.listen(connection, "commit", committed, once=True)
