"""Voice-reference and RVC jobs with explicit workflow dependencies."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pandrator.logic.dubbing.transcript_normalization import load_transcript
from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .credentials import hydrate_stt_settings, hydrate_tts_settings, redact_inline_secrets
from .database import Database
from .models import AppSetting, Artifact, Voice, VoiceSample, utcnow
from .voice_library import (
    mark_provider_registrations_stale,
    remove_managed_files,
    retire_sample_artifact,
    sample_file_status,
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

    sample_artifact, sample_path = context._resolve_input(
        str(payload.get("sample_artifact_id") or "")
    )
    operation_dir = context.paths.voices / str(
        payload.get("voice_id") or "transcription"
    )
    operation_dir.mkdir(parents=True, exist_ok=True)
    progress(0.05, "Preparing reference transcription")
    runtime_settings = hydrate_stt_settings(
        context.database,
        context.paths,
        dict(payload.get("settings") or {}),
    )
    transcription_result = transcribe_source_file_with_metadata(
        operation_dir,
        sample_path,
        runtime_settings,
        ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
        crispasr_executable=str(payload.get("crispasr_executable") or ""),
        progress_callback=context._scaled_progress_callback(progress, 0.05, 0.85),
        cancel_event=cancel_event,
    )
    output_path = Path(transcription_result.srt_path)
    if cancel_event.is_set():
        return {}
    progress(0.9, "Registering reference transcription")
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
    artifact = context.artifacts.register(
        output_path,
        kind="srt",
        role="voice_transcription",
        parent_ids=[sample_artifact.id],
        settings=requested_settings,
        metadata=transcription_metadata,
    )
    timing_artifact = context.artifacts.register(
        Path(transcription_result.word_timestamps_path),
        kind="json",
        role="voice_word_timestamps",
        parent_ids=[sample_artifact.id, artifact.id],
        settings=requested_settings,
        metadata=transcription_metadata,
    )
    # Read the canonical transcript so voice-reference text stays
    # independent from subtitle layout and structured speaker metadata.
    transcript = " ".join(
        segment.text.replace("\n", " ").strip()
        for segment in load_transcript(
            transcription_result.word_timestamps_path
        ).segments
    )
    progress(1.0, "Reference transcription ready for review")
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "word_timestamps_artifact_id": timing_artifact.id,
        "sample_id": payload.get("sample_id"),
        "transcript": transcript,
    }


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
    source_artifact, source_path = context._resolve_input(
        str(payload.get("source_artifact_id") or "")
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
    with context.database.session() as session:
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
        if noise_reduction == VOICE_NOISE_REDUCTION_DEEPFILTERNET2:
            progress(0.05, "Preparing audio for DeepFilterNet2 cleanup")
            resampled = (
                context.paths.temporary
                / f"voice-clean-{source_artifact.id}-{uuid.uuid4().hex}-48k.wav"
            )
            cleanup_temporary.append(resampled)
            subprocess.run(
                [
                    ffmpeg_executable,
                    "-y",
                    "-i",
                    str(source_path),
                    "-ac",
                    "1",
                    "-ar",
                    str(VOICE_CLEANUP_INPUT_SAMPLE_RATE),
                    "-c:a",
                    "pcm_s16le",
                    str(resampled),
                ],
                check=True,
                capture_output=True,
                text=True,
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
        subprocess.run(command, check=True, capture_output=True, text=True)
        if cancel_event.is_set():
            return {}
        sample_metadata: dict[str, Any] | None = (
            {"sample_provenance": sample_provenance}
            if sample_provenance is not None
            else None
        )
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
        with context.database.session() as session:
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
        "path": artifact.relative_path,
        "voice_revision": voice_revision,
        "replaced": bool(replace_sample_id),
    }


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
    with context.database.session() as session:
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
        sample = next(
            (
                item
                for item in samples
                if sample_file_status(session, context.paths, item)[0] == "ready"
            ),
            None,
        )
        if sample is None:
            raise ValueError(
                "Add or replace a readable voice sample before uploading this voice."
            )
        provider_records = dict((voice.metadata_json or {}).get("providers") or {})
        existing = dict(provider_records.get(service_id) or {})
        requested_provider_voice_id = str(
            existing.get("voice_id") or _managed_provider_voice_id(voice)
        ).strip()
        voice_name = voice.name
        source_voice_revision = voice.revision

    sample_artifact, sample_path = context._resolve_input(sample.artifact_id)
    if sample_path.suffix.lower() != ".wav":
        raise ValueError("Provider voice uploads require a normalized WAV sample.")
    if cancel_event.is_set():
        return {}
    progress(0.1, f"Uploading {voice_name} to {service_name}")
    with context.database.session() as session:
        connections = session.get(AppSetting, "services.tts")
        defaults = session.get(AppSetting, "defaults.tts")
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
    runtime_settings = hydrate_tts_settings(
        context.database,
        context.paths,
        {
            **default_value,
            **connection_value,
            "service": service_id,
        },
        manager_bridge=context.manager_bridge,
    )
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
        with context.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice was removed before it could be linked.")
            current_sample = session.get(VoiceSample, sample.id)
            if (
                current_sample is None
                or current_sample.artifact_id != sample.artifact_id
            ):
                raise ValueError(
                    "The voice sample changed before it could be linked."
                )
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            providers[registration_service_id] = registration
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
            next_voice_revision = voice.revision
        progress(1.0, f"{voice_name} is ready in {service_name}")
        return {
            "voice_id": voice_id,
            "service_id": service_id,
            "provider_voice_id": provider_voice_id,
            "voice_revision": next_voice_revision,
            "linked": True,
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
    provider_voice_id = context.tts_providers.upload_voice(
        normalized_service_id,
        str(sample_path),
        base_url=base_url,
        service=service_name,
        prompt_text=uploaded_reference_text or None,
        voice_id=requested_provider_voice_id,
        api_key=str(service_config.get("api_key") or ""),
    )
    # A remote mutation has happened. Persist its ownership record even if
    # cancellation was requested in the meantime; otherwise the provider
    # copy becomes an unmanageable orphan.
    with context.database.session() as session:
        voice = session.get(Voice, voice_id)
        if voice is None:
            raise ValueError("Voice was removed while it was being uploaded.")
        metadata = deepcopy(voice.metadata_json or {})
        providers = dict(metadata.get("providers") or {})
        current_sample = session.get(VoiceSample, sample.id)
        still_current = bool(
            voice.revision == source_voice_revision
            and current_sample is not None
            and current_sample.artifact_id == sample.artifact_id
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
    progress(1.0, f"{voice_name} is ready in {service_name}")
    return {
        "voice_id": voice_id,
        "service_id": service_id,
        "provider_voice_id": provider_voice_id,
        "voice_revision": next_voice_revision,
        "cancellation_requested_after_upload": cancel_event.is_set(),
    }


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
    with context.database.session() as session:
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

        connections = session.get(AppSetting, "services.tts")
        defaults = session.get(AppSetting, "defaults.tts")
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

    runtime_settings = hydrate_tts_settings(
        context.database,
        context.paths,
        {
            **default_value,
            **connection_value,
            "service": service_id,
        },
        manager_bridge=context.manager_bridge,
    )
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
        with context.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice not found.")
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            current = dict(providers.get(service_id) or {})
            if current.get("resource_kind") != "linked_reference":
                raise ValueError(
                    "The linked voice registration changed while removing it."
                )
            providers.pop(service_id, None)
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
            next_revision = voice.revision
        progress(1.0, f"{service_name} link removed")
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
    progress(0.1, f"Removing {provider_voice_id} from {service_name}")
    remote_deleted = context.tts_providers.delete_voice(
        normalized_service_id,
        provider_voice_id,
        base_url=base_url,
        service=str(service_config.get("name") or service_name),
        api_key=str(service_config.get("api_key") or ""),
    )
    # As with upload, once the remote side effect starts we reconcile local
    # state even if cancellation arrives during the request.
    with context.database.session() as session:
        voice = session.get(Voice, voice_id)
        if voice is None:
            return {
                "voice_id": voice_id,
                "service_id": service_id,
                "provider_voice_id": provider_voice_id,
                "remote_deleted": remote_deleted,
                "local_voice_missing": True,
            }
        metadata = deepcopy(voice.metadata_json or {})
        providers = dict(metadata.get("providers") or {})
        current = dict(providers.get(service_id) or {})
        if (
            current.get("voice_id") != provider_voice_id
            or current.get("endpoint_fingerprint") != current_fingerprint
        ):
            raise ValueError(
                "The provider registration changed while removal was running."
            )
        providers.pop(service_id, None)
        metadata["providers"] = providers
        voice.metadata_json = metadata
        voice.revision += 1
        voice.updated_at = utcnow()
        next_revision = voice.revision
    progress(1.0, f"{service_name} copy removed")
    return {
        "voice_id": voice_id,
        "service_id": service_id,
        "provider_voice_id": provider_voice_id,
        "remote_deleted": remote_deleted,
        "voice_revision": next_revision,
        "cancellation_requested_after_delete": cancel_event.is_set(),
    }


def upload_rvc_model(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    from pandrator.logic import rvc_handler

    pth_artifact, pth_path = context._resolve_input(
        str(payload.get("pth_artifact_id") or "")
    )
    index_artifact, index_path = context._resolve_input(
        str(payload.get("index_artifact_id") or "")
    )
    if pth_path.suffix.lower() != ".pth":
        raise ValueError("The RVC weights artifact must be a .pth file.")
    if index_path.suffix.lower() not in {".index", ".idx"}:
        raise ValueError("The RVC index artifact must be an .index or .idx file.")
    if not rvc_handler.is_rvc_available():
        raise RuntimeError("The RVC service is not available.")
    progress(0.15, "Installing RVC model")
    model_root = context.paths.models / "rvc"
    model_root.mkdir(parents=True, exist_ok=True)
    model_name = rvc_handler.upload_rvc_model(
        str(pth_path), str(index_path), str(model_root)
    )
    if cancel_event.is_set():
        return {}
    manifest = model_root / model_name / "pandrator-model.json"
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
    artifact = context.artifacts.register(
        manifest,
        kind="model",
        role="rvc_model",
        parent_ids=[pth_artifact.id, index_artifact.id],
        metadata={"model_name": model_name},
    )
    progress(1.0, "RVC model ready")
    return {
        "model_name": model_name,
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
    }


def convert_with_rvc(
    context: VoiceWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    from pydub import AudioSegment

    from pandrator.logic import rvc_handler

    source_artifact, source_path = context._resolve_input(
        str(payload.get("source_artifact_id") or "")
    )
    session_id = str(payload.get("session_id") or "") or source_artifact.session_id
    settings = dict(payload.get("settings") or {})
    if not str(settings.get("rvc_model") or "").strip():
        raise ValueError("Select an RVC model before conversion.")
    if not rvc_handler.is_rvc_available():
        raise RuntimeError("The RVC service is not available.")
    progress(0.1, "Loading source audio")
    audio = AudioSegment.from_file(source_path)
    if cancel_event.is_set():
        return {}
    progress(0.3, "Converting voice with RVC")
    converted = rvc_handler.process_with_rvc(
        audio, {**settings, "raise_on_error": True}
    )
    destination_dir = (
        context._session_dir(session_id)
        if session_id
        else context.paths.artifacts / "rvc"
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = (
        destination_dir
        / f"{source_path.stem}-rvc-{hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:10]}.wav"
    )
    converted.export(destination, format="wav")
    artifact = context.artifacts.register(
        destination,
        kind="audio",
        role="rvc_audio",
        session_id=session_id,
        parent_ids=[source_artifact.id],
        settings=settings,
        metadata={"rvc_model": settings["rvc_model"]},
    )
    progress(1.0, "RVC audio ready")
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "model_name": settings["rvc_model"],
    }


