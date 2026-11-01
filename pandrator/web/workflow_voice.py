"""Voice-reference and RVC jobs with explicit workflow dependencies."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.transcript_normalization import load_transcript
from pandrator.logic.media_input_policy import LOCAL_MEDIA_INPUT_OPTIONS
from pandrator.runtime import DataPaths

from .access.contracts import AccessDenied
from .access.local_tools_policy import LocalToolOperation, require_local_tool_inputs
from .access.voice_sample_execution_policy import require_voice_sample_execution_workspace
from .accounts.execution_actor import HostActor, UserActor, current_execution_actor
from .accounts.models import Workspace
from .artifacts import ArtifactService, sha256_file
from .credential_identity import credential_namespace
from .credentials import hydrate_stt_settings, hydrate_tts_settings, redact_inline_secrets
from .database import Database
from .job_execution import current_job_lease
from .media_process import run_media_process
from .models import Artifact, Voice, VoiceSample, utcnow
from .settings_identity import workspace_setting_in_workspace
from .training_publication import (
    publish_training_bundle,
    require_training_execution_access,
    transition_training,
)
from .voice_library import (
    mark_provider_registrations_stale,
    remove_managed_files,
    retire_sample_artifact,
    sample_file_status,
)
from .voice_provider_effects import (
    VoiceProviderEffect,
    VoiceProviderEffectError,
    binding_fingerprint,
)
from .voice_provider_settings import check_voice_provider_settings
from .voice_reference_reuse import REFERENCE_PREPARATION_PROFILE, verified_sample_artifact
from .voice_reference_trim import (
    inspect_voice_reference_trim,
    reference_trim_requested,
    trim_voice_reference_audio,
)
from .voice_sample_transcription import (
    check_voice_transcription_settings,
    resolve_voice_transcription_settings,
)

if TYPE_CHECKING:
    from .manager_proxy import LocalManagerProxy
    from .tts_providers import TtsProviderRegistry


@dataclass(frozen=True, slots=True)
class VoiceWorkflowContext:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    tts_providers: TtsProviderRegistry
    manager_bridge: LocalManagerProxy | None
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _session_dir: Callable[[str], Path]
    _scaled_progress_callback: Callable[
        [Callable[[float, str | None], None], float, float],
        Callable[[float, str | None], None],
    ]


VOICE_NOISE_REDUCTION_NONE = "none"
VOICE_NOISE_REDUCTION_DEEPFILTERNET2 = "deepfilternet2"
VOICE_NOISE_REDUCTION_OPTIONS = (
    VOICE_NOISE_REDUCTION_NONE,
    VOICE_NOISE_REDUCTION_DEEPFILTERNET2,
)
# DeepFilterNet2 expects wideband input; the cleaned result is still reduced
# to the mono 24 kHz PCM voice-sample format by the existing FFmpeg step.
VOICE_CLEANUP_INPUT_SAMPLE_RATE = 48000


def _provider_endpoint_fingerprint(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _managed_provider_voice_id(voice: Voice) -> str:
    label = re.sub(r"[^a-z0-9]+", "-", voice.name.casefold()).strip("-")
    label = label[:40].rstrip("-") or "voice"
    return f"pandrator-{label}-{voice.id.replace('-', '')[:10]}"


def transcribe_voice(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    from pandrator.logic.dubbing.transcription import (
        transcribe_source_file_with_metadata,
    )

    voice_id = str(payload.get("voice_id") or "").strip() or None
    sample_id = str(payload.get("sample_id") or "").strip() or None
    sample_artifact_id = str(payload.get("sample_artifact_id") or "").strip()
    workspace_id = None

    def recheck(session: Session | None = None) -> Workspace:
        if session is None:
            with context.database.snapshot_session() as current:
                return recheck(current)
        workspace = require_voice_sample_execution_workspace(
            session, payload, "voice.transcribe", voice_id=voice_id,
            sample_ids=[sample_id] if sample_id else (), artifact_ids=[sample_artifact_id],
            expected_workspace_id=workspace_id,
        )
        check_voice_transcription_settings(session, payload, workspace.id)
        return workspace

    with context.database.snapshot_session() as session:
        workspace = recheck(session)
        captured_runtime = resolve_voice_transcription_settings(session, payload, workspace.id)
        if sample_id:
            sample = session.get(VoiceSample, sample_id)
            if sample is None or sample.artifact_id != sample_artifact_id:
                raise ValueError("The selected voice sample no longer matches its audio.")
        workspace_id = workspace.id
    sample_artifact, sample_path = context._resolve_input(
        sample_artifact_id
    )
    operation_dir = (
        context.paths.voices / voice_id / "transcriptions"
        if voice_id
        else context.paths.voices
        / "transcriptions"
        / workspace_id
        / sample_artifact.id
    ) / uuid.uuid4().hex
    output_committed = False
    try:
        operation_dir.mkdir(parents=True, exist_ok=True)
        progress(0.05, "Preparing reference transcription")
        recheck()
        if cancel_event.is_set():
            return {}
        runtime_settings = hydrate_stt_settings(
            context.database,
            context.paths,
            captured_runtime,
            workspace_id=workspace_id,
        )
        recheck()
        transcription_result = transcribe_source_file_with_metadata(
            operation_dir,
            sample_path,
            runtime_settings,
            ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
            crispasr_executable=str(payload.get("crispasr_executable") or ""),
            progress_callback=context._scaled_progress_callback(progress, 0.05, 0.85),
            cancel_event=cancel_event,
        )
        if cancel_event.is_set():
            return {}
        recheck()
        output_path = Path(transcription_result.srt_path)
        progress(0.9, "Registering reference transcription")
        recheck()
        requested_settings = dict(payload.get("settings") or {})
        requested_language = str(requested_settings.get("stt_language") or "auto")
        resolved_language = str(getattr(transcription_result, "resolved_language", "") or "")
        output_language = (
            resolved_language if resolved_language.lower() not in {"", "auto", "und", "unknown"}
            else requested_language
        )
        transcription_metadata = {
            "engine": transcription_result.engine,
            "compute_backend": transcription_result.compute_backend,
            "language": output_language,
            "requested_language": requested_language,
            "stt_routing": dict(getattr(transcription_result, "routing", {}) or {}),
            "requested_settings": redact_inline_secrets(requested_settings),
        }
        timestamps_path = Path(transcription_result.word_timestamps_path)
        prepared_transcription = context.artifacts.prepare_registration(
            output_path, settings=requested_settings
        )
        prepared_timestamps = context.artifacts.prepare_registration(
            timestamps_path, settings=requested_settings
        )
        # Read the canonical transcript so voice-reference text stays
        # independent from subtitle layout and structured speaker metadata.
        transcript = " ".join(
            segment.text.replace("\n", " ").strip()
            for segment in load_transcript(
                transcription_result.word_timestamps_path
            ).segments
        )
        with context.database.immediate_session() as session:
            current_workspace = recheck(session)
            if sample_id:
                current_sample = session.get(VoiceSample, sample_id)
                if current_sample is None or current_sample.artifact_id != sample_artifact.id:
                    raise ValueError("The selected voice sample changed during transcription.")
            artifact = context.artifacts.register_in_session(
                session,
                output_path,
                kind="srt",
                role="voice_transcription",
                workspace_id=current_workspace.id,
                parent_ids=[sample_artifact.id],
                settings=requested_settings,
                metadata=transcription_metadata,
                _prepared=prepared_transcription,
            )
            timing_artifact = context.artifacts.register_in_session(
                session,
                timestamps_path,
                kind="json",
                role="voice_word_timestamps",
                workspace_id=current_workspace.id,
                parent_ids=[sample_artifact.id, artifact.id],
                settings=requested_settings,
                metadata=transcription_metadata,
                _prepared=prepared_timestamps,
            )
            session.flush()
            artifact_id = artifact.id
            artifact_path = artifact.relative_path
            timing_artifact_id = timing_artifact.id
        output_committed = True
        progress(1.0, "Reference transcription ready for review")
        return {
            "artifact_id": artifact_id,
            "path": artifact_path,
            "word_timestamps_artifact_id": timing_artifact_id,
            "sample_id": payload.get("sample_id"),
            "transcript": transcript,
        }

    finally:
        if not output_committed:
            shutil.rmtree(operation_dir, ignore_errors=True)


def normalize_voice_recording(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    voice_id = str(payload.get("voice_id") or "")
    replace_sample_id = str(payload.get("replace_sample_id") or "") or None
    expected_raw = payload.get("expected_voice_revision")
    expected_revision = int(expected_raw) if expected_raw is not None else None
    noise_reduction = (
        str(payload.get("noise_reduction") or VOICE_NOISE_REDUCTION_NONE)
        .strip()
        .lower()
    )
    if noise_reduction not in VOICE_NOISE_REDUCTION_OPTIONS:
        raise ValueError(
            "noise_reduction must be 'none' or 'deepfilternet2'."
        )
    reviewed_transcript = str(payload.get("reviewed_transcript") or "").strip()
    transcript = reviewed_transcript or str(payload.get("unreviewed_transcript") or "").strip()
    transcript_language = (
        str(payload.get("transcript_language") or "").strip() or None
    )
    sample_provenance = payload.get("sample_provenance")
    if isinstance(sample_provenance, dict):
        sample_provenance = redact_inline_secrets(sample_provenance)
    else:
        sample_provenance = None
    source_artifact_id = str(payload.get("source_artifact_id") or "").strip()
    workspace_id = None

    def recheck(session: Session | None = None) -> Workspace:
        if session is None:
            with context.database.snapshot_session() as current:
                return recheck(current)
        return require_voice_sample_execution_workspace(
            session, payload, "voice.normalize_recording", voice_id=voice_id,
            sample_ids=[replace_sample_id] if replace_sample_id else (),
            artifact_ids=[source_artifact_id], expected_workspace_id=workspace_id,
        )

    with context.database.snapshot_session() as session:
        workspace = recheck(session)
        if replace_sample_id:
            existing = session.get(VoiceSample, replace_sample_id)
            if existing is None or existing.voice_id != voice_id:
                raise ValueError("Voice sample not found.")
        voice = session.get(Voice, voice_id)
        if voice is None:
            raise ValueError("Voice not found.")
        if expected_revision is not None and voice.revision != expected_revision:
            raise ValueError("The voice changed before the sample could be saved.")
        workspace_id = workspace.id
    source_artifact, source_path = context._resolve_input(
        source_artifact_id
    )
    expected_source_role = str(payload.get("source_artifact_role") or "").strip()
    if expected_source_role and source_artifact.role != expected_source_role:
        raise ValueError("The reviewed voice-design preview is no longer available.")
    expected_source_hash = (
        str(payload.get("source_artifact_sha256") or "").strip().casefold()
    )
    if expected_source_hash:
        registered_hash = str(source_artifact.content_hash or "").strip().casefold()
        actual_hash = sha256_file(source_path)
        if (
            registered_hash != expected_source_hash
            or actual_hash != expected_source_hash
        ):
            raise ValueError(
                "The reviewed voice-design preview changed before it could be saved."
            )
    trim_requested = reference_trim_requested(
        payload.get("start_seconds", 0.0), payload.get("end_seconds")
    )
    reference_trim = None
    if trim_requested:
        if not expected_source_hash:
            raise ValueError("A selected voice-reference crop requires its source hash.")
        recheck()
        reference_trim = inspect_voice_reference_trim(
            source_path,
            payload.get("start_seconds", 0.0),
            payload.get("end_seconds"),
            ffprobe_executable=str(payload.get("ffprobe_executable") or "") or None,
            cancel_event=cancel_event,
        )
        if sha256_file(source_path) != expected_source_hash:
            raise ValueError("The selected voice-reference source changed during inspection.")
    if reference_trim is not None:
        sample_provenance = {
            **(sample_provenance or {}),
            "reference_trim": reference_trim.provenance(),
        }
    with context.database.snapshot_session() as session:
        recheck(session)
        voice = session.get(Voice, voice_id)
        if voice is None:
            raise ValueError("Voice not found.")
        if expected_revision is not None and voice.revision != expected_revision:
            raise ValueError("The voice changed before the sample could be saved.")
        if replace_sample_id:
            existing = session.get(VoiceSample, replace_sample_id)
            if existing is None or existing.voice_id != voice_id:
                raise ValueError("Voice sample not found.")
    voice_dir = context.paths.voices / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_executable = str(payload.get("ffmpeg_executable") or "ffmpeg")
    normalize_source_path = source_path
    cleanup_report: dict[str, Any] | None = None
    cleanup_temporary: list[Path] = []
    destination = voice_dir / f"sample-{source_artifact.id}-{uuid.uuid4().hex}.wav"
    output_committed = False
    try:
        if reference_trim is not None:
            progress(0.03, "Selecting the voice-reference crop")
            cropped = (
                context.paths.temporary
                / f"voice-trim-{source_artifact.id}-{uuid.uuid4().hex}.wav"
            )
            cleanup_temporary.append(cropped)
            recheck()
            trim_voice_reference_audio(
                source_path,
                cropped,
                reference_trim,
                ffmpeg_executable=ffmpeg_executable,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                return {}
            if sha256_file(source_path) != expected_source_hash:
                raise ValueError("The selected voice-reference source changed during cropping.")
            normalize_source_path = cropped
        if noise_reduction == VOICE_NOISE_REDUCTION_DEEPFILTERNET2:
            progress(0.05, "Preparing audio for DeepFilterNet2 cleanup")
            resampled = (
                context.paths.temporary
                / f"voice-clean-{source_artifact.id}-{uuid.uuid4().hex}-48k.wav"
            )
            cleanup_temporary.append(resampled)
            resample_command = [
                ffmpeg_executable,
                "-y",
                *LOCAL_MEDIA_INPUT_OPTIONS,
                "-i",
                str(normalize_source_path),
                "-ac",
                "1",
                "-ar",
                str(VOICE_CLEANUP_INPUT_SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                str(resampled),
            ]
            recheck()
            if reference_trim is not None:
                run_media_process(resample_command, cancel_event=cancel_event)
            else:
                subprocess.run(
                    resample_command, check=True, capture_output=True, text=True
                )
            if cancel_event.is_set():
                return {}
            # Imported lazily so the default path never requires the
            # cleanup dependency or its model download.
            from pandrator.logic.audio_cpp_processing import clean_voice_sample

            cleaned = (
                context.paths.temporary
                / f"voice-clean-{source_artifact.id}-{uuid.uuid4().hex}-cleaned.wav"
            )
            cleanup_temporary.append(cleaned)
            progress(0.15, "Cleaning background noise with DeepFilterNet2")

            def _cleanup_progress(phase: str, current: int, total: int) -> None:
                try:
                    fraction = float(current) / max(1, int(total))
                except (TypeError, ValueError):
                    fraction = 0.0
                fraction = max(0.0, min(1.0, fraction))
                if str(phase) == "clean":
                    progress(
                        0.15 + 0.5 * fraction,
                        "Cleaning background noise with DeepFilterNet2",
                    )
                else:
                    progress(
                        0.65 + 0.05 * fraction,
                        "Installing cleaned voice audio",
                    )

            recheck()
            cleanup_report = clean_voice_sample(
                source=resampled,
                destination=cleaned,
                settings={"model": VOICE_NOISE_REDUCTION_DEEPFILTERNET2},
                cancel_event=cancel_event,
                progress=_cleanup_progress,
            )
            if cancel_event.is_set():
                return {}
            if not cleaned.is_file():
                raise RuntimeError(
                    "DeepFilterNet2 cleanup did not produce cleaned audio."
                )
            normalize_source_path = cleaned
        progress(
            0.8 if cleanup_report is not None else 0.1, "Normalizing recording"
        )
        command = [
            ffmpeg_executable,
            "-y",
            *LOCAL_MEDIA_INPUT_OPTIONS,
            "-i",
            str(normalize_source_path),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
        recheck()
        if reference_trim is not None:
            run_media_process(command, cancel_event=cancel_event)
        else:
            subprocess.run(command, check=True, capture_output=True, text=True)
        if cancel_event.is_set():
            return {}
        recheck()
        if reference_trim is not None and sha256_file(source_path) != expected_source_hash:
            raise ValueError(
                "The selected voice-reference source changed before it could be saved."
            )
        sample_metadata: dict[str, Any] | None = (
            {"sample_provenance": sample_provenance}
            if sample_provenance is not None
            else None
        )
        if noise_reduction == VOICE_NOISE_REDUCTION_NONE:
            sample_metadata = {
                **(sample_metadata or {}),
                "reference_preparation_profile": REFERENCE_PREPARATION_PROFILE,
            }
            if sample_provenance is not None:
                sample_metadata["sample_provenance"] = {
                    **sample_provenance,
                    "reference_preparation_profile": REFERENCE_PREPARATION_PROFILE,
                }
        if cleanup_report is not None:
            reduction_metadata: dict[str, Any] = {
                "method": VOICE_NOISE_REDUCTION_DEEPFILTERNET2
            }
            if isinstance(cleanup_report, dict):
                for key, value in cleanup_report.items():
                    if str(key) == "output_path":
                        continue
                    if value is None or isinstance(value, (str, int, float, bool)):
                        reduction_metadata[str(key)] = value
            sample_metadata = {
                **(sample_metadata or {}),
                "noise_reduction": reduction_metadata,
            }
        prepared = context.artifacts.prepare_registration(destination)
        removable: list[Path] = []
        with context.database.immediate_session() as session:
            recheck(session)
            if reference_trim is not None:
                current_source = session.get(Artifact, source_artifact.id)
                if (
                    current_source is None
                    or current_source.state == "deleted"
                    or str(current_source.content_hash or "").strip().casefold()
                    != expected_source_hash
                    or sha256_file(source_path) != expected_source_hash
                ):
                    raise ValueError(
                        "The selected voice-reference source changed before publication."
                    )
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice was removed before the sample could be saved.")
            if expected_revision is not None and voice.revision != expected_revision:
                raise ValueError("The voice changed before the sample could be saved.")
            artifact = context.artifacts.register_in_session(
                session,
                destination,
                kind="audio",
                role="voice_sample",
                workspace_id=workspace_id,
                parent_ids=[source_artifact.id],
                metadata=sample_metadata,
                _prepared=prepared,
            )
            if replace_sample_id:
                sample = session.get(VoiceSample, replace_sample_id)
                if sample is None or sample.voice_id != voice_id:
                    raise ValueError(
                        "Voice sample was removed before it could be replaced."
                    )
                old_path = retire_sample_artifact(session, context.paths, sample)
                if old_path is not None:
                    removable.append(old_path)
                sample.artifact_id = artifact.id
                sample.transcript = transcript or None
                sample.transcript_language = (
                    transcript_language if transcript else None
                )
                sample.transcript_reviewed = bool(reviewed_transcript)
                sample.created_at = utcnow()
            else:
                sample = VoiceSample(
                    voice_id=voice_id,
                    artifact_id=artifact.id,
                    workspace_id=workspace_id,
                    transcript=transcript or None,
                    transcript_language=(
                        transcript_language if transcript else None
                    ),
                    transcript_reviewed=bool(reviewed_transcript),
                )
                session.add(sample)
            session.flush()
            sample_id = sample.id
            mark_provider_registrations_stale(
                voice,
                "The local reference audio changed.",
                sample_id=replace_sample_id,
            )
            voice.revision += 1
            voice.updated_at = utcnow()
            voice_revision = voice.revision
        # Only a successful transaction exit transfers ownership to the
        # voice library. Later retirement/progress errors must not remove it.
        output_committed = True
    finally:
        if not output_committed:
            cleanup_temporary.append(destination)
        remove_managed_files(cleanup_temporary)
    remove_managed_files(removable)
    progress(1.0, "Voice sample ready")
    return {
        "sample_id": sample_id,
        "artifact_id": artifact.id,
        "sample_sha256": artifact.content_hash,
        "path": artifact.relative_path,
        "voice_revision": voice_revision,
        "replaced": bool(replace_sample_id),
    }


def _voice_provider_proof(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    kind: Literal["voice.publish", "voice.unpublish"],
    *,
    workspace_id: str | None = None,
    voice_id: str | None = None,
    sample_ids: tuple[str, ...] = (),
    artifact_ids: tuple[str, ...] = (),
    baseline: str | None = None,
    binding_service_id: str | None = None,
    session: Session | None = None,
) -> Workspace:
    if session is None:
        with context.database.snapshot_session() as current:
            return _voice_provider_proof(
                context, payload, kind, workspace_id=workspace_id, voice_id=voice_id,
                sample_ids=sample_ids, artifact_ids=artifact_ids, baseline=baseline,
                binding_service_id=binding_service_id, session=current,
            )
    workspace = require_voice_sample_execution_workspace(
        session, payload, kind, voice_id=voice_id, sample_ids=sample_ids,
        artifact_ids=artifact_ids, expected_workspace_id=workspace_id,
    )
    check_voice_provider_settings(session, payload, workspace.id)
    if baseline is not None:
        voice = session.get(Voice, voice_id)
        if voice is None or binding_fingerprint(
            voice, binding_service_id or str(payload.get("service_id") or "")
        ) != baseline:
            raise VoiceProviderEffectError(
                "provider_binding_changed", "The provider binding changed during this operation."
            )
    return workspace


def publish_voice(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Upload the newest managed sample and persist the provider's voice ID."""
    from pandrator.logic import tts_handler

    voice_id = str(payload.get("voice_id") or "")
    service_id = str(payload.get("service_id") or "").strip()
    service_name = str(payload.get("service") or service_id).strip()
    expected_raw = payload.get("expected_voice_revision")
    expected_revision = int(expected_raw) if expected_raw is not None else None
    pinned_sample_id = str(payload.get("sample_id") or "").strip()
    pinned_artifact_id = str(payload.get("sample_artifact_id") or "").strip()
    pinned_hash = str(payload.get("sample_sha256") or "").strip().casefold()
    has_pins = bool(pinned_sample_id or pinned_artifact_id or pinned_hash)
    if has_pins and not (pinned_sample_id and pinned_artifact_id and pinned_hash):
        raise ValueError("Exact voice publication requires all sample pins.")
    # Prove the job/root before mutable Voice/source validation; retries must
    # observe retained remote effects even after those resources disappear.
    with context.database.snapshot_session() as session:
        workspace = _voice_provider_proof(context, payload, "voice.publish", session=session)
        effect = VoiceProviderEffect.current(
            context.database, "voice.publish", workspace.id, voice_id, service_id
        )
        retained = effect.previous_result(session)
        if retained is not None:
            return retained
        _voice_provider_proof(
            context, payload, "voice.publish", voice_id=voice_id,
            workspace_id=workspace.id, session=session,
        )
        voice = session.get(Voice, voice_id)
        if voice is None:
            raise ValueError("Voice not found.")
        if expected_revision is not None and voice.revision != expected_revision:
            raise ValueError("The voice changed before provider upload began.")
        samples = list(
            session.scalars(
                select(VoiceSample)
                .where(VoiceSample.voice_id == voice_id)
                .order_by(VoiceSample.created_at.desc())
            ).all()
        )
        newest_ready_sample = next(
            (
                item
                for item in samples
                if sample_file_status(session, context.paths, item)[0] == "ready"
            ),
            None,
        )
        newest_ready_sample_id = newest_ready_sample.id if newest_ready_sample is not None else None
        sample = session.get(VoiceSample, pinned_sample_id) if has_pins else newest_ready_sample
        if sample is None:
            raise ValueError(
                "Add or replace a readable voice sample before uploading this voice."
            )
        _voice_provider_proof(
            context, payload, "voice.publish", voice_id=voice_id,
            sample_ids=(sample.id,), workspace_id=workspace.id, session=session,
        )
        if has_pins:
            if sample.voice_id != voice_id or sample.artifact_id != pinned_artifact_id:
                raise ValueError("The selected voice sample changed before provider upload.")
            verified_sample_artifact(session, context.paths, sample, expected_sha256=pinned_hash)
        provider_records = dict((voice.metadata_json or {}).get("providers") or {})
        existing = dict(provider_records.get(service_id) or {})
        requested_provider_voice_id = str(
            existing.get("voice_id") or _managed_provider_voice_id(voice)
        ).strip()
        voice_name = voice.name
        source_voice_revision = voice.revision
        baseline = binding_fingerprint(voice, service_id)
        workspace_id = workspace.id
        host_allowed = credential_namespace(session, workspace_id).host_credentials_allowed

    def prove(session: Session | None = None) -> Workspace:
        if session is None:
            with context.database.snapshot_session() as current:
                return prove(current)
        proved = _voice_provider_proof(
            context, payload, "voice.publish", workspace_id=workspace_id,
            voice_id=voice_id, sample_ids=(sample.id,), artifact_ids=(sample.artifact_id,),
            baseline=baseline, session=session,
        )
        if credential_namespace(session, workspace_id).host_credentials_allowed != host_allowed:
            raise ValueError("The host credential policy changed during execution.")
        current_voice = session.get_one(Voice, voice_id)
        current_sample = session.get_one(VoiceSample, sample.id)
        current_artifact = session.get_one(Artifact, sample.artifact_id)
        if (
            current_voice.revision != source_voice_revision
            or current_sample.artifact_id != sample.artifact_id
            or (has_pins and current_artifact.content_hash != pinned_hash)
        ):
            raise ValueError("The selected voice sample changed before provider upload.")
        return proved

    prove()
    sample_artifact, sample_path = context._resolve_input(sample.artifact_id)
    if sample_path.suffix.lower() != ".wav":
        raise ValueError("Provider voice uploads require a normalized WAV sample.")
    if cancel_event.is_set():
        return {}
    progress(0.1, f"Uploading {voice_name} to {service_name}")
    prove()
    with context.database.snapshot_session() as session:
        prove(session)
        connections = workspace_setting_in_workspace(
            session, workspace_id, "services.tts"
        )
        defaults = workspace_setting_in_workspace(
            session, workspace_id, "defaults.tts"
        )
        connection_value = (
            dict(connections.value_json or {})
            if connections and isinstance(connections.value_json, dict)
            else {}
        )
        default_value = (
            dict(defaults.value_json or {})
            if defaults and isinstance(defaults.value_json, dict)
            else {}
        )
    prove()
    runtime_settings = hydrate_tts_settings(
        context.database,
        context.paths,
        {
            **default_value,
            **connection_value,
            "service": service_id,
        },
        manager_bridge=context.manager_bridge,
        workspace_id=workspace_id,
    )
    prove()
    service_config = (
        tts_handler.get_service_config(
            runtime_settings,
            service_id,
        )
        or {}
    )
    normalized_service_id = (
        str(service_config.get("id") or service_id)
        .strip()
        .lower()
        .replace("-", "_")
    )
    base_url = str(service_config.get("api_base") or "").strip()
    service_name = str(service_config.get("name") or service_name).strip()
    service_adapter = (
        str(service_config.get("adapter") or "").strip().lower().replace("-", "_")
    )
    if service_adapter == "audio_cpp":
        if has_pins and pinned_sample_id != newest_ready_sample_id:
            raise ValueError(
                "audio.cpp linked voices require the newest ready local reference. "
                "Refresh voice samples and publish the current reference."
            )
        registration_service_id = str(
            service_config.get("id") or service_id
        ).strip()
        reviewed_transcript = (
            str(sample.transcript or "").strip()
            if sample.transcript_reviewed
            else ""
        )
        provider_voice_id = _managed_provider_voice_id(voice)
        sample_hash = str(sample_artifact.content_hash or "").strip()
        if not sample_hash:
            sample_hash = hashlib.sha256(sample_path.read_bytes()).hexdigest()
        registration = {
            "voice_id": provider_voice_id,
            "provider_voice_id": provider_voice_id,
            "sample_id": sample.id,
            "sample_hash": sample_hash,
            "source_audio_hash": sample_hash,
            "status": "ready",
            "updated_at": utcnow().isoformat(),
            "managed_by": "pandrator",
            "protocol": "pandrator-linked-voices-v1",
            "resource_kind": "linked_reference",
            "endpoint_fingerprint": _provider_endpoint_fingerprint(base_url),
            "reference_text_mode": "optional",
            "reference_text_hash": (
                hashlib.sha256(reviewed_transcript.encode("utf-8")).hexdigest()
                if reviewed_transcript
                else None
            ),
        }
        linked_baseline = binding_fingerprint(voice, registration_service_id)
        # File validation belongs outside the binding writer. The writer below
        # then compares source revision, artifact identity/hash and full binding.
        with context.database.snapshot_session() as session:
            prove(session)
            if has_pins:
                current_samples = session.scalars(
                    select(VoiceSample).where(VoiceSample.voice_id == voice_id)
                    .order_by(VoiceSample.created_at.desc())
                )
                newest_current = next((item for item in current_samples if sample_file_status(
                    session, context.paths, item
                )[0] == "ready"), None)
                if newest_current is None or newest_current.id != pinned_sample_id:
                    raise ValueError("audio.cpp linked voices require the current local reference.")
                verified_sample_artifact(
                    session, context.paths, session.get_one(VoiceSample, pinned_sample_id),
                    expected_sha256=pinned_hash,
                )
        with context.database.immediate_session() as session:
            prove(session)
            _voice_provider_proof(
                context, payload, "voice.publish", workspace_id=workspace_id,
                voice_id=voice_id, baseline=linked_baseline,
                binding_service_id=registration_service_id, session=session,
            )
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice was removed before it could be linked.")
            if voice.revision != source_voice_revision:
                raise ValueError("The voice changed before it could be linked.")
            current_sample = session.get(VoiceSample, sample.id)
            if (
                current_sample is None
                or current_sample.artifact_id != sample.artifact_id
            ):
                raise ValueError(
                    "The voice sample changed before it could be linked."
                )
            if has_pins:
                current_artifact = session.get(Artifact, current_sample.artifact_id)
                if current_artifact is None or current_artifact.content_hash != pinned_hash:
                    raise ValueError("audio.cpp linked voices require the current local reference.")
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            current_registration = providers.get(registration_service_id)
            reuse_keys = (
                "voice_id", "provider_voice_id", "sample_id", "sample_hash",
                "source_audio_hash", "status", "managed_by", "protocol",
                "resource_kind", "endpoint_fingerprint", "reference_text_hash",
                "reference_text_mode",
            )
            if has_pins and isinstance(current_registration, dict) and all(
                current_registration.get(key) == registration.get(key) for key in reuse_keys
            ):
                return {
                    "voice_id": voice_id, "service_id": service_id,
                    "provider_voice_id": provider_voice_id,
                    "voice_revision": voice.revision, "linked": True,
                    "sample_id": sample.id, "artifact_id": sample.artifact_id,
                    "sample_sha256": sample_hash, "reused_registration": True,
                }
            providers[registration_service_id] = registration
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
            next_voice_revision = voice.revision
        progress(1.0, f"{voice_name} is ready in {service_name}")
        _voice_provider_proof(
            context, payload, "voice.publish", workspace_id=workspace_id, voice_id=voice_id,
        )
        return {
            "voice_id": voice_id,
            "service_id": service_id,
            "provider_voice_id": provider_voice_id,
            "voice_revision": next_voice_revision,
            "linked": True,
            "sample_id": sample.id,
            "artifact_id": sample.artifact_id,
            "sample_sha256": sample_hash,
            "reused_registration": False,
        }
    reference_text_mode = str(
        service_config.get("voice_reference_text") or "ignored"
    )
    reviewed_transcript = (
        str(sample.transcript or "").strip() if sample.transcript_reviewed else ""
    )
    if reference_text_mode == "required" and not reviewed_transcript:
        raise ValueError(
            f"{service_name} requires a reviewed sample transcript before "
            "this voice can be used."
        )
    uploaded_reference_text = (
        reviewed_transcript
        if reference_text_mode in {"required", "optional"}
        else ""
    )
    if has_pins:
        with context.database.snapshot_session() as session:
            prove(session)
            current_voice = session.get(Voice, voice_id)
            current_sample = session.get(VoiceSample, pinned_sample_id)
            if (
                current_voice is None or current_voice.revision != source_voice_revision
                or current_sample is None or current_sample.voice_id != voice_id
                or current_sample.artifact_id != pinned_artifact_id
            ):
                raise ValueError("The selected voice sample changed before provider upload.")
            verified_sample_artifact(session, context.paths, current_sample, expected_sha256=pinned_hash)
    prove()
    token = effect.reserve(
        {
            "service_id": normalized_service_id, "base_url": base_url,
            "api_key": str(service_config.get("api_key") or ""),
            "voice_id": requested_provider_voice_id, "sample_id": sample.id,
            "sample_sha256": sample_artifact.content_hash,
            "prompt_text": uploaded_reference_text, "service": service_name,
        },
        baseline, lambda session: prove(session),
    )

    def before_io() -> None:
        if cancel_event.is_set():
            raise ValueError("Voice provider upload was cancelled before dispatch.")
        effect.dispatch(token, lambda session: prove(session))

    try:
        provider_voice_id = context.tts_providers.upload_voice(
            normalized_service_id,
            str(sample_path),
            base_url=base_url,
            service=service_name,
            prompt_text=uploaded_reference_text or None,
            voice_id=requested_provider_voice_id,
            api_key=str(service_config.get("api_key") or ""),
            before_io=before_io,
            allow_environment_credentials=host_allowed,
        )
        effect.completed(token, provider_voice_id)
    except Exception:
        effect.failed(token)
        raise
    # The completed remote receipt is committed before any local publication.
    try:
        with context.database.immediate_session() as session:
            _voice_provider_proof(
                context, payload, "voice.publish", workspace_id=workspace_id,
                voice_id=voice_id, baseline=baseline, session=session,
            )
            voice = session.get_one(Voice, voice_id)
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            current_sample = session.get(VoiceSample, sample.id)
            current_artifact = (
                session.get(Artifact, current_sample.artifact_id)
                if current_sample is not None
                else None
            )
            still_current = bool(
                voice.revision == source_voice_revision
                and current_sample is not None
                and current_sample.voice_id == voice_id
                and current_sample.workspace_id == workspace_id
                and current_sample.artifact_id == sample.artifact_id
                and current_artifact is not None
                and current_artifact.workspace_id == workspace_id
            )
            providers[service_id] = {
                "voice_id": provider_voice_id,
                "sample_id": sample.id,
                "status": "ready" if still_current else "stale",
                "updated_at": utcnow().isoformat(),
                "managed_by": "pandrator",
                "protocol": "pandrator-voices-v1",
                "resource_kind": "uploaded_reference",
                "endpoint_fingerprint": _provider_endpoint_fingerprint(base_url),
                "source_audio_hash": sample_artifact.content_hash,
                "reference_text_mode": reference_text_mode,
                "reference_text_hash": (
                    hashlib.sha256(uploaded_reference_text.encode("utf-8")).hexdigest()
                    if uploaded_reference_text
                    else None
                ),
                **(
                    {}
                    if still_current
                    else {
                        "stale_reason": (
                            "The local reference changed while it was being uploaded."
                        )
                    }
                ),
            }
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
            next_voice_revision = voice.revision
            result = {
                "voice_id": voice_id, "service_id": service_id,
                "provider_voice_id": provider_voice_id, "voice_revision": next_voice_revision,
                "sample_id": sample.id, "artifact_id": sample.artifact_id,
                "sample_sha256": sample_artifact.content_hash, "reused_registration": False,
                "cancellation_requested_after_upload": cancel_event.is_set(),
            }
            effect.applied(session, token, "ready" if still_current else "stale", result)
    except Exception as exc:
        raise VoiceProviderEffectError(
            "provider_effect_local_not_applied",
            "The provider upload completed; the local binding was not applied."
            + (" Its job receipt was retained." if effect.job_id is not None else ""),
        ) from exc
    progress(1.0, f"{voice_name} is {'ready' if still_current else 'stale'} in {service_name}")
    _voice_provider_proof(
        context, payload, "voice.publish", workspace_id=workspace_id, voice_id=voice_id,
    )
    return result


def unpublish_voice(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Remove a Pandrator-owned provider voice and then clear registration."""
    from pandrator.logic import tts_handler

    voice_id = str(payload.get("voice_id") or "")
    service_id = str(payload.get("service_id") or "").strip()
    service_name = str(payload.get("service") or service_id).strip()
    expected_raw = payload.get("expected_voice_revision")
    expected_revision = int(expected_raw) if expected_raw is not None else None
    with context.database.snapshot_session() as session:
        workspace = _voice_provider_proof(context, payload, "voice.unpublish", session=session)
        effect = VoiceProviderEffect.current(
            context.database, "voice.unpublish", workspace.id, voice_id, service_id
        )
        retained = effect.previous_result(session)
        if retained is not None:
            return retained
        _voice_provider_proof(
            context, payload, "voice.unpublish", voice_id=voice_id,
            workspace_id=workspace.id, session=session,
        )
        voice = session.get(Voice, voice_id)
        if voice is None:
            raise ValueError("Voice not found.")
        if expected_revision is not None and voice.revision != expected_revision:
            raise ValueError("The voice changed before provider removal began.")
        registration = dict(
            ((voice.metadata_json or {}).get("providers") or {}).get(service_id)
            or {}
        )
        if not registration:
            return {
                "voice_id": voice_id,
                "service_id": service_id,
                "already_absent": True,
                "voice_revision": voice.revision,
            }
        workspace_id = workspace.id
        host_allowed = credential_namespace(session, workspace_id).host_credentials_allowed
        baseline = binding_fingerprint(voice, service_id)
        if registration.get("managed_by") != "pandrator":
            raise ValueError(
                "The provider registration has no Pandrator ownership proof."
            )
        provider_voice_id = str(
            registration.get("voice_id")
            or registration.get("provider_voice_id")
            or ""
        ).strip()
        if not provider_voice_id:
            raise ValueError("The provider registration has no remote voice ID.")

        connections = workspace_setting_in_workspace(
            session, workspace_id, "services.tts"
        )
        defaults = workspace_setting_in_workspace(
            session, workspace_id, "defaults.tts"
        )
        connection_value = (
            dict(connections.value_json or {})
            if connections and isinstance(connections.value_json, dict)
            else {}
        )
        default_value = (
            dict(defaults.value_json or {})
            if defaults and isinstance(defaults.value_json, dict)
            else {}
        )

    def prove(session: Session | None = None) -> Workspace:
        if session is None:
            with context.database.snapshot_session() as current:
                return prove(current)
        proved = _voice_provider_proof(
            context,
            payload,
            "voice.unpublish",
            workspace_id=workspace_id,
            voice_id=voice_id,
            baseline=baseline,
            session=session,
        )
        if credential_namespace(session, workspace_id).host_credentials_allowed != host_allowed:
            raise ValueError("The host credential policy changed during execution.")
        return proved

    prove()
    runtime_settings = hydrate_tts_settings(
        context.database,
        context.paths,
        {
            **default_value,
            **connection_value,
            "service": service_id,
        },
        manager_bridge=context.manager_bridge,
        workspace_id=workspace_id,
    )
    prove()
    service_config = (
        tts_handler.get_service_config(runtime_settings, service_id) or {}
    )
    service_adapter = (
        str(service_config.get("adapter") or "").strip().lower().replace("-", "_")
    )
    if (
        registration.get("resource_kind") == "linked_reference"
        and service_adapter == "audio_cpp"
    ):
        with context.database.immediate_session() as session:
            prove(session)
            voice = session.get_one(Voice, voice_id)
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            providers.pop(service_id, None)
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
            next_revision = voice.revision
        progress(1.0, f"{service_name} link removed")
        _voice_provider_proof(
            context, payload, "voice.unpublish", workspace_id=workspace_id, voice_id=voice_id,
        )
        return {
            "voice_id": voice_id,
            "service_id": service_id,
            "provider_voice_id": provider_voice_id,
            "remote_deleted": False,
            "linked": True,
            "voice_revision": next_revision,
        }
    if not bool(service_config.get("supports_voice_deletion")):
        raise ValueError(
            f"{service_name} does not advertise provider-side voice deletion."
        )
    normalized_service_id = (
        str(service_config.get("id") or service_id)
        .strip()
        .lower()
        .replace("-", "_")
    )
    base_url = str(service_config.get("api_base") or "").strip()
    current_fingerprint = _provider_endpoint_fingerprint(base_url)
    if registration.get("endpoint_fingerprint") != current_fingerprint:
        raise ValueError(
            "The service endpoint changed since this voice was uploaded; "
            "automatic removal was stopped to avoid deleting the wrong resource."
        )
    if cancel_event.is_set():
        return {}
    prove()
    progress(0.1, f"Removing {provider_voice_id} from {service_name}")
    prove()
    token = effect.reserve(
        {
            "service_id": normalized_service_id, "provider_voice_id": provider_voice_id,
            "base_url": base_url, "api_key": str(service_config.get("api_key") or ""),
            "service": str(service_config.get("name") or service_name),
        }, baseline, lambda session: prove(session),
    )

    def before_io() -> None:
        if cancel_event.is_set():
            raise ValueError("Voice provider removal was cancelled before dispatch.")
        effect.dispatch(token, lambda session: prove(session))

    try:
        remote_deleted = context.tts_providers.delete_voice(
            normalized_service_id,
            provider_voice_id,
            base_url=base_url,
            service=str(service_config.get("name") or service_name),
            api_key=str(service_config.get("api_key") or ""),
            before_io=before_io,
            allow_environment_credentials=host_allowed,
        )
        effect.completed(token, provider_voice_id, remote_deleted=remote_deleted)
    except Exception:
        effect.failed(token)
        raise
    try:
        with context.database.immediate_session() as session:
            _voice_provider_proof(
                context, payload, "voice.unpublish", workspace_id=workspace_id,
                voice_id=voice_id, baseline=baseline, session=session,
            )
            voice = session.get_one(Voice, voice_id)
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            providers.pop(service_id, None)
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
            result = {
                "voice_id": voice_id, "service_id": service_id,
                "provider_voice_id": provider_voice_id, "remote_deleted": remote_deleted,
                "voice_revision": voice.revision,
                "cancellation_requested_after_delete": cancel_event.is_set(),
            }
            effect.applied(session, token, "removed", result)
    except Exception as exc:
        raise VoiceProviderEffectError(
            "provider_effect_local_not_applied",
            "Provider removal completed; the local binding was not removed."
            + (" Its job receipt was retained." if effect.job_id is not None else ""),
        ) from exc
    progress(1.0, f"{service_name} copy removed")
    _voice_provider_proof(
        context, payload, "voice.unpublish", workspace_id=workspace_id, voice_id=voice_id,
    )
    return result


def _require_local_tool_worker() -> None:
    actor = current_execution_actor.get()
    if (type(actor) is UserActor and current_job_lease.get() is None
            or actor is not None and type(actor) not in {UserActor, HostActor}):
        raise AccessDenied()


def _local_tool_worker_workspace(
    session: Session, payload: dict[str, Any], operation: LocalToolOperation, *,
    artifact_ids: list[str], session_id: str | None = None,
    expected_workspace_id: str | None = None,
) -> str:
    _require_local_tool_worker()
    if "workspace_id" in payload:
        requested = payload["workspace_id"]
        if not isinstance(requested, str) or not requested:
            raise ValueError("Voice processing has invalid workspace context.")
    else:
        requested = expected_workspace_id
    proved = require_local_tool_inputs(
        session, operation, artifact_ids=artifact_ids, session_id=session_id,
        workspace_id=requested,
    )
    if expected_workspace_id is not None and proved != expected_workspace_id:
        raise ValueError("Voice processing inputs have inconsistent workspace ownership.")
    return proved


def upload_rvc_model(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    _require_local_tool_worker()
    pth_artifact_id = str(payload.get("pth_artifact_id") or "").strip()
    index_artifact_id = str(payload.get("index_artifact_id") or "").strip()
    with context.database.session() as session:
        workspace = _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_MODEL_WRITE,
            artifact_ids=[pth_artifact_id, index_artifact_id],
        )
        workspace_id = workspace
    from pandrator.logic import rvc_handler
    pth_artifact, pth_path = context._resolve_input(
        pth_artifact_id
    )
    index_artifact, index_path = context._resolve_input(
        index_artifact_id
    )
    if pth_path.suffix.lower() != ".pth":
        raise ValueError("The RVC weights artifact must be a .pth file.")
    if index_path.suffix.lower() not in {".index", ".idx"}:
        raise ValueError("The RVC index artifact must be an .index or .idx file.")
    if not rvc_handler.is_rvc_available():
        raise RuntimeError("The RVC service is not available.")
    progress(0.15, "Installing RVC model")
    with context.database.session() as session:
        _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_MODEL_WRITE,
            artifact_ids=[pth_artifact.id, index_artifact.id], expected_workspace_id=workspace_id,
        )
    model_root = context.paths.models / "rvc"
    model_root.mkdir(parents=True, exist_ok=True)
    with context.database.session() as session:
        _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_MODEL_WRITE,
            artifact_ids=[pth_artifact.id, index_artifact.id], expected_workspace_id=workspace_id,
        )
    model_name = rvc_handler.upload_rvc_model(
        str(pth_path), str(index_path), str(model_root)
    )
    if cancel_event.is_set():
        return {}
    manifest = model_root / model_name / "pandrator-model.json"
    with context.database.immediate_session() as session:
        current_workspace = _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_MODEL_WRITE,
            artifact_ids=[pth_artifact.id, index_artifact.id],
            expected_workspace_id=workspace_id,
        )
        manifest.write_text(
            json.dumps(
                {
                    "kind": "rvc",
                    "model_name": model_name,
                    "weights_artifact_id": pth_artifact.id,
                    "index_artifact_id": index_artifact.id,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact = context.artifacts.register_in_session(
            session,
            manifest,
            kind="model",
            role="rvc_model",
            workspace_id=current_workspace,
            parent_ids=[pth_artifact.id, index_artifact.id],
            metadata={"model_name": model_name},
        )
        session.flush()
        artifact_id = artifact.id
        artifact_path = artifact.relative_path
    progress(1.0, "RVC model ready")
    return {
        "model_name": model_name,
        "artifact_id": artifact_id,
        "path": artifact_path,
    }


def convert_with_rvc(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    _require_local_tool_worker()
    from pandrator.logic import rvc_handler
    from pandrator.logic.local_audio_decode import load_local_audio_segment

    source_artifact_id = str(payload.get("source_artifact_id") or "").strip()
    requested_session_id = str(payload.get("session_id") or "").strip() or None
    with context.database.session() as session:
        workspace = _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_CONVERT,
            artifact_ids=[source_artifact_id],
            session_id=requested_session_id,
        )
        workspace_id = workspace
    source_artifact, source_path = context._resolve_input(
        source_artifact_id
    )
    session_id = requested_session_id or source_artifact.session_id
    settings = dict(payload.get("settings") or {})
    if not str(settings.get("rvc_model") or "").strip():
        raise ValueError("Select an RVC model before conversion.")
    with context.database.session() as session:
        _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_CONVERT,
            artifact_ids=[source_artifact.id],
            session_id=session_id,
            expected_workspace_id=workspace_id,
        )
    if not rvc_handler.is_rvc_available():
        raise RuntimeError("The RVC service is not available.")
    progress(0.1, "Loading source audio")
    with context.database.session() as session:
        _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_CONVERT,
            artifact_ids=[source_artifact.id], session_id=session_id,
            expected_workspace_id=workspace_id,
        )
    audio = load_local_audio_segment(source_path)
    if cancel_event.is_set():
        return {}
    progress(0.3, "Converting voice with RVC")
    with context.database.session() as session:
        _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_CONVERT,
            artifact_ids=[source_artifact.id], session_id=session_id,
            expected_workspace_id=workspace_id,
        )
    converted = rvc_handler.process_with_rvc(
        audio, {**settings, "raise_on_error": True}
    )
    with context.database.session() as session:
        _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_CONVERT,
            artifact_ids=[source_artifact.id],
            session_id=session_id,
            expected_workspace_id=workspace_id,
        )
    destination_dir = (
        context._session_dir(session_id)
        if session_id
        else context.paths.artifacts / "rvc" / workspace_id
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = (
        destination_dir
        / f"{source_path.stem}-rvc-{hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:10]}.wav"
    )
    with context.database.session() as session:
        _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_CONVERT,
            artifact_ids=[source_artifact.id], session_id=session_id,
            expected_workspace_id=workspace_id,
        )
    converted.export(destination, format="wav")
    with context.database.immediate_session() as session:
        current_workspace = _local_tool_worker_workspace(
            session, payload, LocalToolOperation.RVC_CONVERT,
            artifact_ids=[source_artifact.id],
            session_id=session_id,
            expected_workspace_id=workspace_id,
        )
        artifact = context.artifacts.register_in_session(
            session,
            destination,
            kind="audio",
            role="rvc_audio",
            session_id=session_id,
            workspace_id=current_workspace,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"rvc_model": settings["rvc_model"]},
        )
        session.flush()
        artifact_id = artifact.id
        artifact_path = artifact.relative_path
    progress(1.0, "RVC audio ready")
    return {
        "artifact_id": artifact_id,
        "path": artifact_path,
        "model_name": settings["rvc_model"],
    }

def train_xtts(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    with context.database.session() as session:
        require_training_execution_access(session, payload)
    from pandrator.logic import xtts_trainer_handler

    publication_result: dict[str, Any] | None = None
    total_epochs = 6
    last_training_fraction = 0.0
    zero_based_epochs: bool | None = None

    def training_output(line: str) -> None:
        nonlocal last_training_fraction, zero_based_epochs
        detail = str(line)[-500:]
        epoch_match = re.search(
            r"\bepoch\s*[:#-]?\s*(\d+)(?:\s*(?:/|of)\s*(\d+))?",
            detail,
            re.IGNORECASE,
        )
        percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", detail)
        if epoch_match:
            raw_epoch = max(0, int(epoch_match.group(1)))
            if raw_epoch == 0:
                zero_based_epochs = True
            reported_total = max(
                1,
                int(epoch_match.group(2) or total_epochs),
            )
            # Training logs vary between zero- and one-based epoch labels.
            completed_before = (
                raw_epoch if zero_based_epochs else max(0, raw_epoch - 1)
            )
            within_epoch = (
                max(0.0, min(100.0, float(percent_match.group(1)))) / 100
                if percent_match
                else 0.0
            )
            last_training_fraction = max(
                last_training_fraction,
                min(1.0, (completed_before + within_epoch) / reported_total),
            )
            display_epoch = min(
                reported_total,
                raw_epoch + 1 if zero_based_epochs else max(1, raw_epoch),
            )
            detail = (
                f"Training epoch {display_epoch} of {reported_total} — {detail}"
            )
        elif percent_match and total_epochs == 1:
            last_training_fraction = max(
                last_training_fraction,
                min(1.0, float(percent_match.group(1)) / 100),
            )
        progress(0.1 + 0.8 * last_training_fraction, detail)

    def training_status(line: str) -> None:
        detail = str(line)[-500:]
        normalized = detail.casefold()
        if "building" in normalized:
            value = 0.05
        elif "in progress" in normalized:
            value = 0.1
        elif "copying model" in normalized or "training finished" in normalized:
            value = 0.92
        elif "completed" in normalized:
            value = 0.98
        else:
            value = 0.1
        progress(value, detail)

    def publish(staging: Path, target: Path) -> None:
        nonlocal publication_result
        publication_result = publish_training_bundle(
            context.database,
            context.paths,
            context.artifacts,
            payload,
            cancel_event,
            staging,
            target,
        )

    def completed_result() -> dict[str, Any]:
        assert publication_result is not None
        try:
            progress(1.0, "XTTS model ready")
        except Exception:
            logging.getLogger(__name__).warning(
                "XTTS model was published, but final progress could not be reported.",
                exc_info=True,
            )
        return publication_result

    try:
        from pandrator.logic.xtts_model_paths import validate_training_model_name

        transition_training(context.database, payload, cancel_event, status="running")
        settings = dict(payload.get("settings") or {})
        try:
            total_epochs = max(1, int(settings.get("epochs") or 6))
        except (TypeError, ValueError):
            total_epochs = 6
        model_name = str(payload.get("model_name") or "")
        validate_training_model_name(model_name)
        _source_artifact, source_path = context._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        source_text_path = ""
        source_text_id = str(payload.get("source_text_artifact_id") or "")
        if source_text_id:
            _text_artifact, text_path = context._resolve_input(source_text_id)
            source_text_path = str(text_path)
        progress(0.02, "Validating XTTS trainer")
        with context.database.session() as session:
            require_training_execution_access(session, payload)
        success, message = xtts_trainer_handler.start_training(
            {
                **settings,
                "model_name": model_name,
                "source_audio_path": str(source_path),
                "source_text_path": source_text_path,
            },
            output_callback=training_output,
            status_callback=training_status,
            stop_event=cancel_event,
            model_root=context.paths.models / "xtts",
            publish_callback=publish,
        )
        if publication_result is not None:
            return completed_result()
        if cancel_event.is_set():
            transition_training(context.database, payload, cancel_event, status="canceled")
            return {}
        if not success:
            raise RuntimeError(message)
        raise RuntimeError("XTTS trainer succeeded without publishing a model bundle.")
    except Exception as error:
        if publication_result is not None:
            logging.getLogger(__name__).warning(
                "XTTS model was published before a subsequent trainer error.",
                exc_info=True,
            )
            return completed_result()
        try:
            transition_training(
                context.database,
                payload,
                cancel_event,
                status=(
                    "canceled"
                    if cancel_event.is_set() or isinstance(error, InterruptedError)
                    else "failed"
                ),
                error_message=str(error),
            )
        except (InterruptedError, ValueError):
            pass
        raise
