"""Short-transaction sound-mix admission and bounded status projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import sha256_file
from .database import Database
from .jobs import JobQueue
from .models import Artifact, GenerationPlan, Job, SessionRecord
from .sound_mix_snapshot import PreparedSoundMix, _load_snapshot, _snapshot_pins
from .sound_project_models import ProjectSoundCue, ProjectSoundPlan

_ACTIVE_JOB_STATUSES = ("queued", "running", "cancel_requested")


@dataclass(frozen=True, slots=True)
class VerifiedMixCache:
    artifact_id: str
    content_hash: str


def find_verified_mix_cache(
    database: Database,
    paths: DataPaths,
    session_id: str,
    request_fingerprint: str,
) -> VerifiedMixCache | None:
    """Read one exact current cache candidate and verify its managed file."""

    with database.snapshot_session() as session:
        artifact = session.scalar(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.role == "sound_mix.audio",
                Artifact.kind == "audio",
                Artifact.state == "current",
                Artifact.metadata_json["sound_mix"]["request_fingerprint"].as_string()
                == request_fingerprint,
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(1)
        )
        if artifact is None or not artifact.content_hash:
            return None
        content = {
            "id": artifact.id,
            "relative_path": artifact.relative_path,
            "content_hash": artifact.content_hash,
        }
    try:
        path = paths.managed_path(content["relative_path"])
        if not path.is_file() or sha256_file(path) != content["content_hash"]:
            return None
    except (OSError, ValueError):
        return None
    return VerifiedMixCache(content["id"], content["content_hash"])


def assert_current_pins(session: Session, prepared: PreparedSoundMix) -> None:
    """Rebuild only database pins while a caller holds the SQLite writer."""

    current = _load_snapshot(
        session,
        prepared.session_id,
        prepared.base_artifact_id,
        prepared.timeline_artifact_id,
    )
    if _snapshot_pins(current) != prepared.pins:
        raise ValueError("Sound-mix inputs changed. Refresh the sound plan and try again.")


def admit_in_session(
    session: Session,
    jobs: JobQueue,
    prepared: PreparedSoundMix,
) -> dict[str, Any]:
    """Reuse a verified cache/job or enqueue exactly one session-scoped render."""

    if not prepared.has_cues:
        assert_current_pins(session, prepared)
        return {
            "job_id": None,
            "artifact_id": None,
            "status": "inactive",
            "cache_hit": False,
            "sound_plan_revision": prepared.sound_plan_revision,
        }
    assert_current_pins(session, prepared)
    if prepared.cache_artifact_id:
        cached = session.get(Artifact, prepared.cache_artifact_id)
        mix_metadata = (cached.metadata_json or {}).get("sound_mix") if cached else None
        if (
            cached is not None
            and cached.state == "current"
            and cached.kind == "audio"
            and cached.session_id == prepared.session_id
            and cached.content_hash == prepared.cache_artifact_hash
            and isinstance(mix_metadata, dict)
            and mix_metadata.get("request_fingerprint") == prepared.request_fingerprint
        ):
            return {
                "job_id": None,
                "artifact_id": cached.id,
                "status": "ready",
                "cache_hit": True,
                "sound_plan_revision": prepared.sound_plan_revision,
            }

    active_job = session.scalar(
        select(Job)
        .where(
            Job.session_id == prepared.session_id,
            Job.kind == "sound.mix",
            Job.status.in_(_ACTIVE_JOB_STATUSES),
            Job.payload_json["request_fingerprint"].as_string()
            == prepared.request_fingerprint,
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    )
    if active_job is not None:
        return {
            "job_id": active_job.id,
            "artifact_id": None,
            "status": active_job.status,
            "cache_hit": False,
            "sound_plan_revision": prepared.sound_plan_revision,
        }

    job = jobs.enqueue_in_session(
        session,
        "sound.mix",
        prepared.payload,
        session_id=prepared.session_id,
        max_attempts=1,
        resource_keys=[f"session:{prepared.session_id}"],
    )
    return {
        "job_id": job.id,
        "artifact_id": None,
        "status": job.status,
        "cache_hit": False,
        "sound_plan_revision": prepared.sound_plan_revision,
    }


def get_status(
    database: Database,
    session_id: str,
    base_artifact_id: str | None = None,
) -> dict[str, Any]:
    """Return latest mix/job state without opening, hashing, or decoding audio."""

    latest_plan_id = (
        select(ProjectSoundPlan.id)
        .where(ProjectSoundPlan.session_id == SessionRecord.id)
        .order_by(ProjectSoundPlan.revision.desc())
        .limit(1)
        .correlate(SessionRecord)
        .scalar_subquery()
    )
    plan_revision = (
        select(ProjectSoundPlan.revision)
        .where(ProjectSoundPlan.id == latest_plan_id)
        .limit(1)
        .correlate(SessionRecord)
        .scalar_subquery()
    )
    speech_revision = (
        select(ProjectSoundPlan.speech_plan_revision_id)
        .where(ProjectSoundPlan.id == latest_plan_id)
        .limit(1)
        .correlate(SessionRecord)
        .scalar_subquery()
    )
    active_speech_revision = (
        select(GenerationPlan.active_revision_id)
        .where(GenerationPlan.session_id == SessionRecord.id)
        .limit(1)
        .correlate(SessionRecord)
        .scalar_subquery()
    )
    has_cues = exists(
        select(ProjectSoundCue.id).where(
            ProjectSoundCue.plan_id == latest_plan_id,
            ProjectSoundCue.included.is_(True),
        )
    )
    with database.snapshot_session() as session:
        header = session.execute(
            select(
                SessionRecord.workflow_kind,
                latest_plan_id.label("plan_id"),
                plan_revision.label("plan_revision"),
                speech_revision.label("speech_revision"),
                active_speech_revision.label("active_speech_revision"),
                has_cues.label("has_cues"),
            ).where(SessionRecord.id == session_id)
        ).one_or_none()
        if header is None:
            raise KeyError(session_id)
        plan_id = header.plan_id
        artifact_statement = select(Artifact).where(
            Artifact.session_id == session_id,
            Artifact.role == "sound_mix.audio",
            Artifact.state != "deleted",
            Artifact.metadata_json["sound_mix"]["sound_plan_id"].as_string()
            == (plan_id or ""),
        )
        if base_artifact_id is not None:
            artifact_statement = artifact_statement.where(
                Artifact.metadata_json["sound_mix"]["base_artifact_id"].as_string()
                == base_artifact_id
            )
        latest_artifact = session.scalar(
            artifact_statement.order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(1)
        )
        job_statement = select(Job.id, Job.status, Job.result_json, Job.payload_json).where(
            Job.session_id == session_id,
            Job.kind == "sound.mix",
            Job.payload_json["sound_plan_id"].as_string() == (plan_id or ""),
        )
        if base_artifact_id is not None:
            job_statement = job_statement.where(
                Job.payload_json["base_artifact_id"].as_string() == base_artifact_id
            )
        latest_job = session.execute(
            job_statement.order_by(Job.created_at.desc(), Job.id.desc()).limit(1)
        ).one_or_none()
        result = latest_job.result_json if latest_job and isinstance(latest_job.result_json, dict) else {}
        job_payload = (
            latest_job.payload_json
            if latest_job and isinstance(latest_job.payload_json, dict)
            else {}
        )
        artifact_mix = (
            (latest_artifact.metadata_json or {}).get("sound_mix")
            if latest_artifact is not None
            else {}
        )
        if not isinstance(artifact_mix, dict):
            artifact_mix = {}
        if not bool(header.has_cues):
            status = "inactive"
        elif latest_job and latest_job.status in _ACTIVE_JOB_STATUSES:
            status = latest_job.status
        elif latest_artifact is not None:
            status = "ready" if latest_artifact.state == "current" else "stale"
        elif latest_job is not None:
            status = str(latest_job.status)
        else:
            status = "idle"
        return {
            "session_id": session_id,
            "base_artifact_id": base_artifact_id
            or artifact_mix.get("base_artifact_id")
            or job_payload.get("base_artifact_id"),
            "sound_plan_id": plan_id,
            "sound_plan_revision": int(header.plan_revision or 0),
            "speech_plan_revision_id": header.speech_revision or header.active_speech_revision,
            "artifact_id": latest_artifact.id if latest_artifact else result.get("artifact_id"),
            "request_fingerprint": artifact_mix.get("request_fingerprint")
            or job_payload.get("request_fingerprint"),
            "job_id": latest_job.id if latest_job else None,
            "status": status,
        }


__all__ = [
    "VerifiedMixCache",
    "admit_in_session",
    "assert_current_pins",
    "find_verified_mix_cache",
    "get_status",
]
