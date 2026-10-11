"""Lease validation and transactional claim helpers for sound mixes."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .artifacts import ArtifactService
from .database import Database
from .job_execution import current_job_lease
from .media_process import MediaProcessCancelled
from .models import Artifact, Job, utcnow
from .sound_mix_admission import assert_current_pins
from .sound_mix_snapshot import PreparedSoundMix

_ALLOWED_LEASE_KINDS = frozenset(
    {"sound.mix", "generation.assemble", "export.create", "export.variant"}
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_claim(
    session: Session,
    prepared: PreparedSoundMix,
    cancel_event: threading.Event,
    *,
    require_sound_mix: bool = False,
) -> Job | None:
    if cancel_event.is_set():
        raise MediaProcessCancelled("Sound-mix rendering was canceled.")
    lease = current_job_lease.get()
    if lease is None:
        if require_sound_mix:
            raise ValueError("Sound-mix worker publication requires its current job lease.")
        return None
    job_id = str(lease.get("job_id") or "")
    owner_id = str(lease.get("owner_id") or "")
    generation = lease.get("lease_generation")
    if not job_id or not owner_id or type(generation) is not int:
        raise ValueError("The current job lease is incomplete.")
    job = session.get(Job, job_id)
    if (
        job is None
        or job.session_id != prepared.session_id
        or job.kind not in _ALLOWED_LEASE_KINDS
        or (require_sound_mix and job.kind != "sound.mix")
        or job.status != "running"
        or job.lease_owner != owner_id
        or job.lease_generation != generation
        or job.lease_expires_at is None
        or _utc(job.lease_expires_at) <= utcnow()
    ):
        raise ValueError("The sound-mix job no longer owns a valid publication lease.")
    return job


def _cached_artifact(
    database: Database,
    artifacts: ArtifactService,
    prepared: PreparedSoundMix,
    progress_cancel: threading.Event,
    *,
    require_sound_mix: bool,
) -> tuple[Artifact, dict[str, Any]] | None:
    if not prepared.cache_artifact_id:
        return None
    try:
        artifact, path = artifacts.resolve(prepared.cache_artifact_id)
    except KeyError:
        return None
    if (
        artifact.state != "current"
        or artifact.content_hash != prepared.cache_artifact_hash
        or not path.is_file()
    ):
        return None
    from .artifacts import sha256_file

    if sha256_file(path) != artifact.content_hash:
        return None
    with database.immediate_session() as session:
        _validate_claim(
            session,
            prepared,
            progress_cancel,
            require_sound_mix=require_sound_mix,
        )
        assert_current_pins(session, prepared)
        current = session.get(Artifact, artifact.id)
        mix = (current.metadata_json or {}).get("sound_mix") if current else None
        if (
            current is None
            or current.state != "current"
            or current.content_hash != artifact.content_hash
            or not isinstance(mix, dict)
            or mix.get("request_fingerprint") != prepared.request_fingerprint
        ):
            return None
        session.expunge(current)
        artifact = current
    return artifact, {
        "artifact_id": artifact.id,
        "status": "ready",
        "cache_hit": True,
        "request_fingerprint": prepared.request_fingerprint,
        "mix_signature": (artifact.metadata_json or {}).get("sound_mix", {}).get("mix_signature"),
        "sound_plan_revision": prepared.sound_plan_revision,
    }


def _cleanup_uncommitted(
    database: Database,
    output_path: Path,
    relative_path: str,
    content_hash: str | None,
) -> None:
    if not output_path.exists():
        return
    try:
        with database.snapshot_session() as session:
            registered = session.scalar(
                select(Artifact.id).where(
                    Artifact.relative_path == relative_path,
                    Artifact.content_hash == content_hash,
                )
            )
    except Exception:
        # An uncertain commit outcome is a reason to preserve the file.
        return
    if registered is None:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass


def _stale_exact_duplicates(session: Session, prepared: PreparedSoundMix) -> None:
    rows = list(
        session.scalars(
            select(Artifact).where(
                Artifact.session_id == prepared.session_id,
                Artifact.role == "sound_mix.audio",
                Artifact.kind == "audio",
                Artifact.state == "current",
                Artifact.metadata_json["sound_mix"]["request_fingerprint"].as_string()
                == prepared.request_fingerprint,
            )
        ).all()
    )
    for artifact in rows:
        artifact.state = "stale"
        ArtifactService._mark_descendants_stale(session, artifact.id)
