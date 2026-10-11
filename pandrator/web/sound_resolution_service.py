"""Admission facade for session sound word-placement jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .jobs import JobQueue
from .models import Job
from .settings_policy import RevisionConflict
from .sound_project_service import ProjectSoundService
from .sound_resolution_snapshot import SoundResolutionSnapshot, capture, digest
from .stt_resources import audio_cpp_resource_keys, stt_resource_keys
from .workspace_settings import WorkspaceSettingsService


@dataclass(frozen=True, slots=True)
class PreparedSoundResolution:
    snapshot: SoundResolutionSnapshot
    payload: dict[str, Any]
    resource_keys: tuple[str, ...]


class SoundResolutionService:
    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        artifacts: ArtifactService,
        jobs: JobQueue,
        settings: WorkspaceSettingsService,
        project_sounds: ProjectSoundService,
    ) -> None:
        self.database = database
        self.paths = paths
        self.artifacts = artifacts
        self.jobs = jobs
        self.settings = settings
        self.project_sounds = project_sounds

    def prepare(
        self, session_id: str, *, expected_revision: int, cue_ids: list[str] | None = None
    ) -> PreparedSoundResolution:
        with self.database.snapshot_session() as session:
            snapshot = capture(
                session, session_id, expected_revision, cue_ids, self.settings, self.paths
            )
        payload = {
            "session_id": session_id,
            "pins": snapshot.pins,
            "pin_hash": digest(snapshot.pins),
        }
        keys = sorted(
            {
                f"session:{session_id}",
                *stt_resource_keys(snapshot.settings),
                *audio_cpp_resource_keys("cpu"),
            }
        )
        return PreparedSoundResolution(snapshot, payload, tuple(keys))

    def admit_in_session(
        self, session: Session, prepared: PreparedSoundResolution
    ) -> dict[str, Any]:
        if not isinstance(prepared, PreparedSoundResolution):
            raise TypeError("Expected prepared sound resolution.")
        pinned = prepared.snapshot
        current = capture(
            session,
            pinned.session_id,
            pinned.revision,
            list(pinned.cue_ids),
            self.settings,
            self.paths,
        )
        # The default batch count is part of the admission promise.
        current_pins = {**current.pins, "remaining_count": pinned.remaining_count}
        if digest(current_pins) != prepared.payload["pin_hash"]:
            raise RevisionConflict("Sound resolution inputs changed before admission.")
        active = session.scalar(
            select(Job)
            .where(
                Job.session_id == pinned.session_id,
                Job.kind == "sound.resolve",
                Job.status.in_(("queued", "running", "cancel_requested")),
                Job.payload_json["pin_hash"].as_string() == prepared.payload["pin_hash"],
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        if active is not None:
            return {
                "job_id": active.id,
                "status": active.status,
                "cache_hit": False,
                "revision": pinned.revision,
                "remaining_count": pinned.remaining_count,
            }
        job = self.jobs.enqueue_in_session(
            session,
            "sound.resolve",
            prepared.payload,
            session_id=pinned.session_id,
            max_attempts=1,
            resource_keys=list(prepared.resource_keys),
        )
        return {
            "job_id": job.id,
            "status": job.status,
            "cache_hit": False,
            "revision": pinned.revision,
            "remaining_count": pinned.remaining_count,
        }

    def run_resolution(self, payload: dict[str, Any], progress, cancel_event) -> dict[str, Any]:
        from .sound_resolution_worker import run_resolution

        return run_resolution(self, payload, progress, cancel_event)
