"""Queue uploaded voice recordings without leaving rejected uploads behind."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .jobs import JobQueue
from .models import Job
from .stt_resources import audio_cpp_resource_keys

logger = logging.getLogger(__name__)


def queue_voice_recording(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    jobs: JobQueue,
    incoming: FileStorage,
    *,
    voice_id: str,
    expected_revision: int,
    noise_reduction: str,
    replace_sample_id: str | None = None,
) -> Job:
    """Save input, then commit its artifact and job together.

    Upload I/O and hashing precede the short write transaction. The recording
    becomes durable only when both rows commit; on failure, discard only this
    request's temporary file. Existing samples remain owned by the worker.
    """
    suffix = Path(secure_filename(incoming.filename or "")).suffix or ".webm"
    temporary = paths.temporary / f"voice-{uuid.uuid4()}{suffix}"
    try:
        incoming.save(temporary)
        prepared = artifacts.prepare_registration(temporary)
        payload = {
            "voice_id": voice_id,
            "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg",
            "expected_voice_revision": expected_revision,
            "noise_reduction": noise_reduction,
        }
        if replace_sample_id is not None:
            payload["replace_sample_id"] = replace_sample_id
        resource_keys = [f"voice:{voice_id}"] + (
            audio_cpp_resource_keys() if noise_reduction == "deepfilternet2" else []
        )
        with database.immediate_session() as session:
            source_artifact = artifacts.register_in_session(
                session, temporary, kind="audio", role="recording_upload", _prepared=prepared
            )
            payload["source_artifact_id"] = source_artifact.id
            job = jobs.enqueue_in_session(
                session, "voice.normalize_recording", payload, resource_keys=resource_keys
            )
        return job
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove rejected voice recording %s", temporary, exc_info=True)
        raise
