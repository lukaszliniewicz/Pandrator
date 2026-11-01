"""Queue uploaded voice recordings without leaving rejected uploads behind."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from pandrator.runtime import DataPaths

from .access.contracts import AccessContext, AccessDenied
from .access.voice_policy import require_voice_run_access
from .accounts.execution_actor import UserActor, current_execution_actor
from .artifacts import ArtifactService
from .database import Database
from .jobs import JobQueue
from .models import Job, Voice, VoiceSample
from .stt_resources import audio_cpp_resource_keys
from .voice_execution_identity import voice_execution_workspace
from .voice_identity import voice_creation_workspace
from .voice_library import is_bundled_voice

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
    workspace_id: str | None = None,
    access_context: AccessContext | None = None,
) -> Job:
    """Save input, then commit its artifact and job together.

    Upload I/O and hashing precede the short write transaction. The recording
    becomes durable only when both rows commit; on failure, discard only this
    request's temporary file. Existing samples remain owned by the worker.
    """
    if access_context is None and isinstance(current_execution_actor.get(), UserActor):
        raise AccessDenied()
    with database.snapshot_session() as session:
        if access_context is not None:
            voice = require_voice_run_access(
                session, access_context, voice_id, workspace_id=workspace_id
            )
            selected_workspace_id = voice.workspace_id
            if is_bundled_voice(voice):
                raise ValueError("Samples cannot be added to the bundled reference voice.")
        else:
            workspace = voice_creation_workspace(session, workspace_id=workspace_id)
            selected_workspace_id = workspace.id
            voice = session.get(Voice, voice_id)
        if voice is None:
            raise ValueError("Voice not found.")
        if voice.revision != expected_revision:
            raise ValueError("The voice changed before the recording could be uploaded.")
        if replace_sample_id is not None:
            replacement = session.get(VoiceSample, replace_sample_id)
            if replacement is None or replacement.voice_id != voice_id:
                raise ValueError("Voice sample not found.")
        voice_execution_workspace(
            session,
            {"workspace_id": selected_workspace_id},
            voice_id=voice_id,
            sample_ids=[replace_sample_id] if replace_sample_id else (),
            expected_workspace_id=selected_workspace_id,
        )

    suffix = Path(secure_filename(incoming.filename or "")).suffix or ".webm"
    temporary = paths.temporary / f"voice-{uuid.uuid4()}{suffix}"
    try:
        incoming.save(temporary)
        prepared = artifacts.prepare_registration(temporary)
        payload = {
            "voice_id": voice_id,
            "workspace_id": selected_workspace_id,
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
            if access_context is not None:
                voice = require_voice_run_access(
                    session, access_context, voice_id, workspace_id=selected_workspace_id
                )
                if is_bundled_voice(voice):
                    raise ValueError("Samples cannot be added to the bundled reference voice.")
            else:
                voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice was removed before the recording could be queued.")
            if voice.revision != expected_revision:
                raise ValueError("The voice changed before the recording could be queued.")
            if replace_sample_id is not None:
                replacement = session.get(VoiceSample, replace_sample_id)
                if replacement is None or replacement.voice_id != voice_id:
                    raise ValueError("Voice sample was removed before replacement.")
            voice_execution_workspace(
                session,
                payload,
                voice_id=voice_id,
                sample_ids=[replace_sample_id] if replace_sample_id else (),
                expected_workspace_id=selected_workspace_id,
            )
            source_artifact = artifacts.register_in_session(
                session,
                temporary,
                kind="audio",
                role="recording_upload",
                workspace_id=selected_workspace_id,
                _prepared=prepared,
            )
            payload["source_artifact_id"] = source_artifact.id
            payload["source_artifact_sha256"] = source_artifact.content_hash
            payload["source_artifact_role"] = source_artifact.role
            job = jobs.enqueue_in_session(
                session,
                "voice.normalize_recording",
                payload,
                resource_keys=resource_keys,
                workspace_id=voice.workspace_id,
            )
        return job
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove rejected voice recording %s", temporary, exc_info=True)
        raise
