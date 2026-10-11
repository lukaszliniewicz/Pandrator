"""Run native sound generation and publish only under the current job lease."""

from __future__ import annotations

import os
import stat
import threading
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.logic import audio_cpp_sfx
from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .jobs import JobQueue
from .models import Artifact
from .sound_generation_admission import SettingsProvider, _runtime_identity, runtime_settings
from .sound_generation_worker_support import (
    _artifact_is_present,
    _lease,
    _private_output_path,
    _public_output_directory,
    _remove_if_same,
    _require_job,
    _result,
    _rollback_cleanup,
    _valid_output_properties,
)
from .sound_schemas import DEFAULT_SOUND_MODEL_ID


def run_generate(
    database: Database,
    jobs: JobQueue,
    paths: DataPaths,
    artifacts: ArtifactService,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
    *,
    settings_provider: SettingsProvider | None = None,
) -> dict[str, Any]:
    """Generate privately, then publish only while the current lease is valid."""
    lease = _lease(payload)
    settings = runtime_settings(settings_provider)
    candidate_id = str(payload.get("candidate_id") or "")
    request = payload.get("request")
    identity = payload.get("runtime_identity")
    if not isinstance(request, dict) or not isinstance(identity, dict):
        raise ValueError("Invalid sound generation job payload.")
    canonical_request = audio_cpp_sfx.validate_request(request)
    if canonical_request != request:
        raise ValueError("Sound generation request is not canonical.")
    kind = canonical_request.get("kind")
    if kind is None and canonical_request.get("model_id") == DEFAULT_SOUND_MODEL_ID:
        # Older SFX candidates predate the kind field; their fixed model implies
        # the existing sound kind. New models must be canonicalized explicitly.
        kind = "sound"
    if kind not in {"sound", "music"}:
        raise ValueError("Canonical sound generation request has no valid kind.")
    with database.immediate_session() as session:
        job, candidate = _require_job(session, payload, lease, cancel_event)
        if candidate.fingerprint != audio_cpp_sfx.generation_fingerprint(
            canonical_request, identity
        ):
            raise RuntimeError("Sound candidate fingerprint does not match its job payload.")
        if candidate.state == "ready":
            if not _artifact_is_present(session, paths, candidate):
                raise RuntimeError("The already-published sound artifact is missing or invalid.")
            result = _result(job, candidate)
            if not isinstance(job.result_json, dict) or job.result_json.get("candidate_id") != candidate.id:
                job.result_json = result
            return result
        if candidate.state != "queued":
            raise RuntimeError("Sound candidate is no longer eligible for generation.")

    current_identity = _runtime_identity(settings, canonical_request["model_id"])
    if current_identity != identity:
        raise RuntimeError(
            "The audio.cpp runtime or model changed after admission; submit the sound request again."
        )

    output_path = _private_output_path(paths)
    output_identity: tuple[int, int] | None = None
    published_path: Path | None = None
    published_identity: tuple[int, int] | None = None
    publication_committed = False
    try:
        progress(0.05, "Preparing sound generation")

        def native_progress(phase: str, completed: int, total: int) -> None:
            fraction = completed / total if total > 0 else 0.0
            fraction = min(1.0, max(0.0, fraction))
            progress(0.1 + 0.7 * fraction, f"{phase}: {completed}/{total}")

        properties = audio_cpp_sfx.generate_sound(
            canonical_request,
            output_path,
            settings=settings,
            cancel_event=cancel_event,
            progress=native_progress,
        )
        if cancel_event.is_set():
            raise RuntimeError("Sound generation was canceled before publication.")
        output_stat = output_path.lstat()
        if not stat.S_ISREG(output_stat.st_mode):
            raise ValueError("audio.cpp output must be a regular WAV file.")
        output_identity = (output_stat.st_dev, output_stat.st_ino)
        frames, sample_rate, channels, duration_seconds = _valid_output_properties(properties)
        registration = artifacts.prepare_registration(output_path, calculate_hash=True)

        with database.immediate_session() as session:
            job, candidate = _require_job(session, payload, lease, cancel_event)
            if candidate.fingerprint != audio_cpp_sfx.generation_fingerprint(
                canonical_request, identity
            ):
                raise RuntimeError("Sound candidate changed before publication.")
            if candidate.state == "ready":
                if not _artifact_is_present(session, paths, candidate):
                    raise RuntimeError("The already-published sound artifact is missing or invalid.")
                assert output_identity is not None
                _remove_if_same(output_path, output_identity)
                return _result(job, candidate)
            if candidate.state != "queued":
                raise RuntimeError("Sound candidate is no longer eligible for publication.")

            directory = _public_output_directory(paths)
            artifact: Artifact | None = None
            relative_path = ""
            for _ in range(8):
                destination = directory / f"{uuid.uuid4()}.wav"
                relative_path = paths.relative_managed_path(destination)
                registered = session.scalar(
                    select(Artifact.id).where(Artifact.relative_path == relative_path)
                )
                if registered is not None:
                    continue
                try:
                    os.link(output_path, destination)
                except FileExistsError:
                    continue
                published_path = destination
                destination_stat = destination.lstat()
                published_identity = (destination_stat.st_dev, destination_stat.st_ino)
                output_path.unlink()
                prepared_registration = replace(
                    registration,
                    relative_path=relative_path,
                )
                artifact = artifacts.register_in_session(
                    session,
                    destination,
                    kind="audio",
                    role="sound.original",
                    session_id=None,
                    calculate_hash=False,
                    metadata={
                        "provider": "audio_cpp",
                        "operation": "gen",
                        "kind": kind,
                        "model_id": canonical_request["model_id"],
                        "request": deepcopy(canonical_request),
                        "runtime_identity": deepcopy(identity),
                    },
                    _prepared=prepared_registration,
                )
                _rollback_cleanup(session, destination, published_identity)
                break
            if artifact is None:
                raise RuntimeError("Could not allocate an unused sound output path.")

            candidate.artifact_id = artifact.id
            candidate.state = "ready"
            candidate.sample_rate = sample_rate
            candidate.channels = channels
            candidate.frame_count = frames
            candidate.duration_seconds = duration_seconds
            job.result_json = _result(job, candidate)
            session.flush()
            # This is the publication fence: no DB state is committed after a
            # stale lease, cancellation, or competing worker wins ownership.
            _require_job(session, payload, lease, cancel_event)

        publication_committed = True
        output_path.unlink(missing_ok=True)
        assert published_path is not None
        progress(1.0, "Sound ready")
        return {
            "candidate_id": candidate_id,
            "job_id": lease["job_id"],
            "artifact_id": candidate.artifact_id,
            "status": "ready",
        }
    except Exception:
        if output_identity is not None:
            _remove_if_same(output_path, output_identity)
        else:
            output_path.unlink(missing_ok=True)
        if (
            not publication_committed
            and published_path is not None
            and published_identity is not None
        ):
            _remove_if_same(published_path, published_identity)
        raise
