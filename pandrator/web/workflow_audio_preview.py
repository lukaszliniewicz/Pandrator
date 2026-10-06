"""Audio preview rendering and publication with explicit worker dependencies."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .models import Artifact


@dataclass(frozen=True)
class AudioPreviewContext:
    paths: DataPaths
    artifacts: ArtifactService
    resolve_input: Callable[[str], tuple[Artifact, Path]]
    session_dir: Callable[[str], Path]
    new_id: Callable[[], str]


def generate_audio_preview(
    context: AudioPreviewContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Transcode the first source audio stream to a browser-safe MP3."""
    from .media_process import (
        MediaProcessCancelled,
        MediaProcessError,
        resolve_ffmpeg_executable,
        run_media_process,
    )

    try:
        source, source_path = context.resolve_input(str(payload.get("source_artifact_id") or ""))
        destination_dir = (
            context.session_dir(source.session_id)
            if source.session_id
            else context.paths.artifacts / "audio-previews"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
    except (KeyError, OSError, ValueError):
        raise RuntimeError("The source audio preview could not be prepared.") from None
    destination = destination_dir / f"audio-preview-v1-{source.id}.mp3"
    temporary_destination = (
        destination_dir / f".audio-preview-v1-{source.id}-{context.new_id()}.mp3"
    )
    previous_destination = (
        destination_dir / f".audio-preview-v1-{source.id}-{context.new_id()}.previous"
    )
    settings = {
        "preview_version": "v1",
        "codec": "mp3",
        "bitrate_kbps": 64,
        "channels": 1,
        "sample_rate_hz": 48000,
    }
    replaced_destination = False
    previous_destination_staged = False
    registered = False
    progress(0.05, "Preparing source audio preview")
    command = [
        resolve_ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(temporary_destination),
    ]
    try:
        progress(0.1, "Transcoding source audio preview")
        try:
            run_media_process(command, cancel_event=cancel_event)
        except MediaProcessCancelled:
            raise
        except MediaProcessError:
            raise RuntimeError("The source audio preview could not be transcoded.") from None
        if cancel_event.is_set():
            temporary_destination.unlink(missing_ok=True)
            return {}
        if destination.is_file():
            os.replace(destination, previous_destination)
            previous_destination_staged = True
        os.replace(temporary_destination, destination)
        replaced_destination = True
        if cancel_event.is_set():
            destination.unlink(missing_ok=True)
            if previous_destination_staged:
                os.replace(previous_destination, destination)
                previous_destination_staged = False
            replaced_destination = False
            return {}
        progress(0.9, "Registering source audio preview")
        if cancel_event.is_set():
            destination.unlink(missing_ok=True)
            if previous_destination_staged:
                os.replace(previous_destination, destination)
                previous_destination_staged = False
            replaced_destination = False
            return {}
        artifact = context.artifacts.register(
            destination,
            kind="audio",
            role="source_audio_preview",
            session_id=source.session_id,
            parent_ids=[source.id],
            replace_parent_ids=True,
            settings=settings,
            metadata={
                "source_artifact_id": source.id,
                "source_content_hash": source.content_hash,
                "source_artifact_hash": source.content_hash,
                "preview_version": "v1",
            },
        )
        registered = True
        previous_destination.unlink(missing_ok=True)
        previous_destination_staged = False
        replaced_destination = False
        return {"artifact_id": artifact.id, "source_artifact_id": source.id}
    except MediaProcessCancelled:
        temporary_destination.unlink(missing_ok=True)
        if not registered:
            if replaced_destination:
                destination.unlink(missing_ok=True)
            if previous_destination_staged:
                os.replace(previous_destination, destination)
        return {}
    # This is the final cleanup and redaction boundary for filesystem,
    # artifact-registration, and database failures.
    except Exception:  # noqa: BLE001
        temporary_destination.unlink(missing_ok=True)
        if not registered:
            if replaced_destination:
                destination.unlink(missing_ok=True)
            if previous_destination_staged:
                os.replace(previous_destination, destination)
        raise RuntimeError("The source audio preview could not be prepared.") from None


def preview_output_mix(
    context: AudioPreviewContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Render a short, managed sample with the exact export mix graph."""

    from pandrator.logic.dubbing.audio_sync import build_mix_preview_command

    from .media_process import (
        MediaProcessCancelled,
        find_first_audible_seconds,
        probe_audio_stream,
        resolve_ffmpeg_executable,
        run_media_process,
    )

    session_id = str(payload.get("session_id") or "")
    source, source_path = context.resolve_input(str(payload.get("source_artifact_id") or ""))
    dubbing, dubbing_path = context.resolve_input(str(payload.get("dubbing_artifact_id") or ""))
    if source.session_id != session_id or dubbing.session_id != session_id:
        raise ValueError("Mix preview inputs do not belong to this session.")

    settings = dict(payload.get("settings") or {})
    automatic_start = payload.get("start_seconds") is None
    automatic_start_method = "manual"
    try:
        requested_start = (
            0.0 if automatic_start else max(0.0, float(cast(Any, payload.get("start_seconds"))))
        )
        requested_duration = min(
            30.0,
            max(4.0, float(payload.get("duration_seconds") or 12.0)),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Mix preview timing must use numeric seconds.") from error

    progress(0.08, "Inspecting preview audio")
    try:
        source_info = probe_audio_stream(source_path, cancel_event=cancel_event)
        dubbing_info = probe_audio_stream(dubbing_path, cancel_event=cancel_event)
        if automatic_start:
            timeline_starts = [
                float(item["target_start_ms"])
                for item in (dubbing.metadata_json or {}).get("takes", [])
                if isinstance(item, dict) and isinstance(item.get("target_start_ms"), (int, float))
            ]
            if timeline_starts:
                requested_start = max(0.0, min(timeline_starts) / 1000.0 - 1.0)
                automatic_start_method = "assembly_timeline"
            else:
                requested_start = find_first_audible_seconds(
                    dubbing_path,
                    cancel_event=cancel_event,
                )
                automatic_start_method = "audio_detection"
    except MediaProcessCancelled:
        return {}
    available_seconds = (
        min(
            source_info.duration_ms,
            dubbing_info.duration_ms,
        )
        / 1000.0
    )
    remaining_seconds = available_seconds - requested_start
    if remaining_seconds < 0.25:
        raise ValueError("The preview start is beyond the available source and voiceover audio.")
    duration_seconds = min(requested_duration, remaining_seconds)

    destination_dir = context.session_dir(session_id) / "previews"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "soundtrack-mix-preview.wav"
    temporary_destination = destination_dir / f".soundtrack-mix-preview-{context.new_id()}.wav"
    command = build_mix_preview_command(
        source_path,
        dubbing_path,
        temporary_destination,
        start_seconds=requested_start,
        duration_seconds=duration_seconds,
        source_gain_db=settings.get("mix_source_gain_db", 0.0),
        voice_gain_db=settings.get("mix_voice_gain_db", 0.0),
        voice_lufs=settings.get("mix_voice_lufs", -16.0),
        ducking=str(settings.get("mix_ducking") or "strong"),
        attack_ms=settings.get("mix_attack_ms", 25),
        release_ms=settings.get("mix_release_ms", 350),
        ffmpeg_executable=resolve_ffmpeg_executable(),
    )
    previous_destination = destination_dir / f".soundtrack-mix-preview-{context.new_id()}.previous"
    previous_destination_staged = False
    replaced_destination = False
    committed = False
    progress(0.2, "Rendering soundtrack mix preview")
    try:
        run_media_process(command, cancel_event=cancel_event)
        if cancel_event.is_set():
            return {}
        if destination.is_file():
            os.replace(destination, previous_destination)
            previous_destination_staged = True
        os.replace(temporary_destination, destination)
        replaced_destination = True
        if cancel_event.is_set():
            return {}
        progress(0.9, "Registering soundtrack mix preview")
        if cancel_event.is_set():
            return {}
        artifact = context.artifacts.register(
            destination,
            kind="audio",
            role="mix_preview",
            session_id=session_id,
            parent_ids=[source.id, dubbing.id],
            replace_parent_ids=True,
            settings=settings,
            metadata={
                "generation_run_id": str(payload.get("generation_run_id") or ""),
                "source_artifact_id": source.id,
                "dubbing_artifact_id": dubbing.id,
                "start_seconds": requested_start,
                "duration_seconds": duration_seconds,
                "automatic_start": automatic_start,
                "automatic_start_method": automatic_start_method,
                "mix": {
                    "source_gain_db": settings.get("mix_source_gain_db", 0.0),
                    "voice_gain_db": settings.get("mix_voice_gain_db", 0.0),
                    "voice_lufs": settings.get("mix_voice_lufs", -16.0),
                    "ducking": settings.get("mix_ducking", "strong"),
                    "attack_ms": settings.get("mix_attack_ms", 25),
                    "release_ms": settings.get("mix_release_ms", 350),
                },
            },
        )
        # Registration commits the receipt. Later cleanup/reporting failures
        # must not restore bytes that no longer match that receipt.
        committed = True
    except MediaProcessCancelled:
        return {}
    finally:
        if not committed:
            if previous_destination_staged:
                os.replace(previous_destination, destination)
            elif replaced_destination:
                destination.unlink(missing_ok=True)
        temporary_destination.unlink(missing_ok=True)
        if committed:
            previous_destination.unlink(missing_ok=True)

    progress(1.0, "Soundtrack mix preview ready")
    return {
        "artifact_id": artifact.id,
        "artifact": {
            "id": artifact.id,
            "session_id": artifact.session_id,
            "kind": artifact.kind,
            "role": artifact.role,
            "relative_path": artifact.relative_path,
            "mime_type": artifact.mime_type,
            "size_bytes": artifact.size_bytes,
            "content_hash": artifact.content_hash,
            "state": artifact.state,
            "metadata_json": artifact.metadata_json,
            "created_at": artifact.created_at.isoformat(),
        },
        "start_seconds": requested_start,
        "duration_seconds": duration_seconds,
        "automatic_start": automatic_start,
        "automatic_start_method": automatic_start_method,
    }
