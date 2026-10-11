"""Small facade for sound-mix preparation, admission, and rendering."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Callable

from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .job_execution import current_job_lease
from .jobs import JobQueue
from .models import Artifact, Job
from .sound_mix_admission import (
    VerifiedMixCache,
    admit_in_session,
    find_verified_mix_cache,
    get_status,
)
from .sound_mix_publication import render_prepared
from .sound_mix_snapshot import PreparedSoundMix, prepare_sound_mix
from .sound_project_service import ProjectSoundService


class SoundMixService:
    """Coordinate immutable sound-mix inputs with existing artifact/job services."""

    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        artifacts: ArtifactService,
        jobs: JobQueue,
        project_sounds: ProjectSoundService | None = None,
    ) -> None:
        self.database = database
        self.paths = paths
        self.artifacts = artifacts
        self.jobs = jobs
        self.project_sounds = project_sounds or ProjectSoundService(database, artifacts)

    def prepare(
        self,
        session_id: str,
        base_artifact_id: str,
        *,
        timeline_artifact_id: str | None = None,
        expected_revision: int | None = None,
    ) -> PreparedSoundMix:
        prepared = prepare_sound_mix(
            self.database,
            self.paths,
            self.artifacts,
            session_id,
            base_artifact_id,
            timeline_artifact_id=timeline_artifact_id,
            expected_revision=expected_revision,
        )
        if not prepared.has_cues:
            return prepared
        cached: VerifiedMixCache | None = find_verified_mix_cache(
            self.database,
            self.paths,
            session_id,
            prepared.request_fingerprint,
        )
        if cached is None:
            return prepared
        return replace(
            prepared,
            cache_artifact_id=cached.artifact_id,
            cache_artifact_hash=cached.content_hash,
        )

    def get_status(
        self, session_id: str, base_artifact_id: str | None = None
    ) -> dict[str, Any]:
        return get_status(self.database, session_id, base_artifact_id)

    def admit_in_session(
        self, session: Session, prepared: PreparedSoundMix
    ) -> dict[str, Any]:
        return admit_in_session(session, self.jobs, prepared)

    @staticmethod
    def _durable_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Sound-mix worker payload must be an object.")
        allowed = {
            "session_id",
            "base_artifact_id",
            "base_content_hash",
            "timeline_artifact_id",
            "timeline_content_hash",
            "sound_plan_id",
            "sound_plan_revision",
            "speech_plan_revision_id",
            "request_fingerprint",
            "_job_id",
            "_lease_generation",
        }
        if payload.keys() - allowed:
            raise ValueError("Sound-mix worker payload contains unsupported fields.")
        return {key: value for key, value in payload.items() if not key.startswith("_")}

    def run_mix(
        self,
        payload: dict[str, Any],
        progress: Callable[[float, str], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        durable_payload = self._durable_payload(payload)
        lease = current_job_lease.get()
        if (
            lease is None
            or lease.get("job_id") != payload.get("_job_id")
            or lease.get("lease_generation") != payload.get("_lease_generation")
        ):
            raise ValueError("Sound-mix worker requires its current job lease.")
        required = {
            "session_id",
            "base_artifact_id",
            "base_content_hash",
            "timeline_artifact_id",
            "timeline_content_hash",
            "sound_plan_id",
            "sound_plan_revision",
            "speech_plan_revision_id",
            "request_fingerprint",
        }
        if durable_payload.keys() != required:
            raise ValueError("Sound-mix worker payload is missing accepted input pins.")
        prepared = self.prepare(
            str(durable_payload["session_id"]),
            str(durable_payload["base_artifact_id"]),
            expected_revision=durable_payload["sound_plan_revision"],
            timeline_artifact_id=(
                str(durable_payload["timeline_artifact_id"])
                if durable_payload["timeline_artifact_id"] is not None
                else None
            ),
        )
        if not prepared.has_cues or prepared.payload != durable_payload:
            raise ValueError("Sound-mix inputs changed after admission; submit a new mix request.")
        with self.database.snapshot_session() as session:
            job = session.get(Job, str(payload.get("_job_id")))
            if (
                job is None
                or job.session_id != prepared.session_id
                or job.kind != "sound.mix"
                or job.payload_json != durable_payload
            ):
                raise ValueError("Sound-mix job payload no longer matches its accepted request.")
        _artifact, receipt = render_prepared(
            self.database,
            self.paths,
            self.artifacts,
            prepared,
            progress=progress,
            cancel_event=cancel_event,
            require_sound_mix_lease=True,
        )
        return receipt

    def render_for_artifact(
        self,
        session_id: str,
        base_artifact_id: str,
        *,
        timeline_artifact_id: str | None = None,
        progress: Callable[[float, str], None],
        cancel_event: threading.Event,
    ) -> Artifact:
        prepared = self.prepare(
            session_id,
            base_artifact_id,
            timeline_artifact_id=timeline_artifact_id,
        )
        artifact, _receipt = render_prepared(
            self.database,
            self.paths,
            self.artifacts,
            prepared,
            progress=progress,
            cancel_event=cancel_event,
        )
        return artifact


__all__ = ["PreparedSoundMix", "SoundMixService"]
