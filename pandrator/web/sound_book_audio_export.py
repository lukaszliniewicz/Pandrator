"""Export a current sound-mixed audiobook as a tagged managed audio file."""

from __future__ import annotations

import math
import tempfile
import threading
import wave
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from werkzeug.utils import secure_filename

from .artifacts import sha256_file
from .audio_assembly import (
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
    preferred_pcm_format,
    resolve_assembly_backend,
)
from .export_publication import publish_video_export
from .job_execution import current_job_lease
from .media_process import MediaProcessCancelled
from .models import Artifact, Job, utcnow
from .output_settings_snapshot import build_output_settings_snapshot
from .workflow_output_context import OutputWorkflowContext

_FORMATS = {"wav", "mp3", "m4b", "flac", "opus"}


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"Sound-mixed audio {name} must be an integer.")
    return value


def _cancel(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise MediaProcessCancelled("Sound-mixed audiobook export was canceled.")


def _current_audio(
    context: OutputWorkflowContext,
    audio: Artifact,
    session_id: str,
) -> tuple[Artifact, Path, dict[str, Any], dict[str, Any], int, int, int]:
    with context.database.session() as session:
        current = session.get(Artifact, audio.id)
        if (
            current is None
            or (audio.kind, audio.role, audio.session_id, audio.state)
            != ("audio", "sound_mix.audio", session_id, "current")
            or (current.kind, current.role, current.session_id, current.state)
            != ("audio", "sound_mix.audio", session_id, "current")
            or (current.content_hash, current.relative_path, current.metadata_json)
            != (audio.content_hash, audio.relative_path, audio.metadata_json or {})
        ):
            raise ValueError("The selected sound-mixed audio is unavailable or changed.")
        session.expunge(current)

    path = context.paths.managed_path(current.relative_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("The selected sound-mixed audio is not a regular managed file.")
    if not current.content_hash or sha256_file(path) != current.content_hash:
        raise ValueError("The selected sound-mixed audio content hash changed.")

    metadata = current.metadata_json or {}
    sound_mix = metadata.get("sound_mix")
    if not isinstance(sound_mix, dict):
        raise ValueError("The selected audio has no sound-mix provenance.")
    if any(
        not isinstance(sound_mix.get(key), str) or not sound_mix[key]
        for key in ("request_fingerprint", "mix_signature")
    ):
        raise ValueError("The selected audio has incomplete sound-mix provenance.")
    frame_plan = sound_mix.get("frame_plan")
    if not isinstance(frame_plan, dict):
        raise ValueError("The selected audio has no frozen sound-mix frame plan.")
    rate = _integer(frame_plan.get("rate"), "sample rate", minimum=8000)
    if rate > 192000:
        raise ValueError("Sound-mixed audio sample rate exceeds 192000 Hz.")
    channels = frame_plan.get("channels")
    if type(channels) is not int or channels not in {1, 2}:
        raise ValueError("Sound-mixed audio channels must be mono or stereo.")
    frames = _integer(frame_plan.get("total_frames"), "frame count", minimum=1)
    if not isinstance(frame_plan.get("mix_signature"), str) or not frame_plan["mix_signature"]:
        raise ValueError("The frozen sound-mix frame plan has no signature.")
    timeline = metadata.get("audio_timeline")
    if (
        not isinstance(timeline, dict)
        or _integer(timeline.get("sample_rate_hz"), "timeline sample rate", minimum=1)
        != rate
        or _integer(timeline.get("total_frames"), "timeline frame count", minimum=1)
        != frames
    ):
        raise ValueError("The sound-mix timeline does not match its frozen frame plan.")

    try:
        with wave.open(str(path), "rb") as reader:
            if (
                reader.getcomptype() != "NONE"
                or reader.getsampwidth() != 2
                or reader.getframerate() != rate
                or reader.getnchannels() != channels
                or reader.getnframes() != frames
            ):
                raise ValueError(
                    "The sound-mixed PCM WAV does not match its frozen frame plan."
                )
    except (EOFError, OSError, wave.Error) as error:
        raise ValueError("The selected sound-mixed audio is not a valid PCM WAV.") from error

    return current, path, metadata, sound_mix, frames, rate, channels


def _validate_frame_metadata(metadata: dict[str, Any], total_frames: int) -> None:
    takes = metadata.get("takes")
    if takes is None:
        return
    if not isinstance(takes, list):
        raise ValueError("Sound-mixed take metadata must be a list.")
    for index, take in enumerate(takes):
        if not isinstance(take, dict):
            raise ValueError(f"Sound-mixed take metadata {index} is malformed.")
        has_start, has_end = "start_frame" in take, "end_frame" in take
        if has_start != has_end:
            raise ValueError(f"Sound-mixed take metadata {index} has incomplete frames.")
        if has_start and (
            _integer(take["start_frame"], f"take {index} start_frame")
            > (end := _integer(take["end_frame"], f"take {index} end_frame"))
            or end > total_frames
        ):
            raise ValueError(f"Sound-mixed take metadata {index} is outside the output.")


def _chapter_markers(
    metadata: dict[str, Any], *, rate: int, total_frames: int
) -> list[tuple[float, str]]:
    chapters = metadata.get("chapters")
    if chapters is None:
        return []
    if not isinstance(chapters, list):
        raise ValueError("Sound-mixed chapter metadata must be a list.")
    markers: list[tuple[float, str]] = []
    previous_frame = -1
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            raise ValueError(f"Sound-mixed chapter {index} is malformed.")
        value = chapter.get("start_ms")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Sound-mixed chapter {index} has no valid start time.")
        start_ms = float(value)
        if not math.isfinite(start_ms) or start_ms < 0:
            raise ValueError(f"Sound-mixed chapter {index} has an invalid start time.")
        start_frame = round(start_ms * rate / 1000)
        if start_frame >= total_frames:
            raise ValueError(f"Sound-mixed chapter {index} is outside the audio timeline.")
        if start_frame < previous_frame:
            raise ValueError("Sound-mixed chapters are not in timeline order.")
        previous_frame = start_frame
        markers.append((start_ms / 1000, str(chapter.get("title") or "")))
    return markers


def _cover_path(
    context: OutputWorkflowContext,
    session_id: str,
    settings: dict[str, Any],
) -> tuple[Artifact | None, Path | None]:
    cover_id = str(settings.get("cover_artifact_id") or "").strip()
    if not cover_id:
        return None, None
    cover, resolved = context._resolve_input(cover_id)
    if (cover.session_id, cover.state, cover.kind) != (session_id, "current", "image") or not str(
        cover.mime_type or ""
    ).startswith("image/"):
        raise ValueError("The selected cover must be a current image from this session.")
    managed = context.paths.managed_path(cover.relative_path)
    if managed.is_symlink() or not managed.is_file() or managed != resolved.resolve():
        raise ValueError("The selected cover is not a valid managed image file.")
    if cover.content_hash and sha256_file(managed) != cover.content_hash:
        raise ValueError("The selected cover image content hash changed.")
    return cover, managed


def _validate_active_lease(
    context: OutputWorkflowContext,
    payload: dict[str, Any],
    *,
    session_id: str,
) -> None:
    lease = current_job_lease.get()
    if lease is None:
        return
    job_id = payload.get("_job_id")
    generation = payload.get("_lease_generation")
    if (
        not isinstance(job_id, str)
        or not job_id
        or type(generation) is not int
        or (lease.get("job_id"), lease.get("lease_generation")) != (job_id, generation)
        or not isinstance(lease.get("owner_id"), str)
        or not lease.get("owner_id")
    ):
        raise InterruptedError("The active export worker lease does not match its payload.")
    with context.database.session() as session:
        current_job = session.scalar(
            select(Job.id).where(
                Job.id == job_id,
                Job.session_id == session_id,
                Job.kind.in_(("export.create", "export.variant")),
                Job.status == "running",
                Job.lease_owner == lease.get("owner_id"),
                Job.lease_generation == generation,
                Job.lease_expires_at > utcnow(),
            )
        )
        if current_job is None:
            raise InterruptedError("The active export worker lease is no longer current.")


def export_mixed_book_audio(
    context: OutputWorkflowContext,
    audio: Artifact,
    *,
    session_id: str,
    export_name: str,
    settings: dict[str, Any],
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> Artifact:
    """Encode a managed sound mix as one immutable audiobook export."""
    _cancel(cancel_event)
    current, source_path, source_metadata, sound_mix, frames, rate, channels = (
        _current_audio(context, audio, session_id)
    )
    _validate_frame_metadata(source_metadata, frames)
    chapters = _chapter_markers(source_metadata, rate=rate, total_frames=frames)

    output_format = str(settings.get("format") or "wav").strip().lower()
    if output_format not in _FORMATS:
        raise ValueError(f"Unsupported audiobook output format: {output_format}.")
    filename = secure_filename(export_name)
    if not filename or filename != export_name:
        raise ValueError("The audiobook export name must be a safe filename stem.")
    cover_artifact, cover_path = _cover_path(context, session_id, settings)
    _cancel(cancel_event)

    detected_rate, detected_channels = preferred_pcm_format(
        source_path, cancel_event=cancel_event
    )
    if (detected_rate, detected_channels) != (rate, channels):
        raise ValueError("The sound-mixed PCM format changed after timeline validation.")
    plan = build_audio_assembly_plan(
        [
            AudioAssemblyPart(
                path=source_path,
                expected_duration_ms=round(frames * 1000 / rate),
                label=current.id,
            )
        ],
        output_format=output_format,
        bitrate=str(settings.get("bitrate") or "192k"),
        sample_rate_hz=rate,
        channels=channels,
    )
    backend = resolve_assembly_backend()

    exports_dir = context._session_dir(session_id) / "exports"
    staging_parent = exports_dir / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    requested_destination = exports_dir / "audio" / f"{filename}.{output_format}"
    metadata_settings = build_output_settings_snapshot(
        settings,
        payload.get("resolved_settings_snapshot")
        if isinstance(payload.get("resolved_settings_snapshot"), dict)
        else None,
    )
    tag_metadata = {
        "title": str(settings.get("title") or context._session_record(session_id).name),
        "artist": str(settings.get("artist") or ""),
        "album": str(settings.get("album") or ""),
        "genre": str(settings.get("genre") or ""),
        "language": str(settings.get("language") or ""),
    }
    include_sounds = settings.get("include_sounds", True)

    with tempfile.TemporaryDirectory(prefix="sound-book-audio-", dir=staging_parent) as temp:
        scratch_output = Path(temp) / f"mixed.{output_format}"
        progress(0.1, "Encoding sound-mixed audiobook")
        result = assemble_audio_plan(
            plan,
            scratch_output,
            backend=backend,
            work_dir=Path(temp),
            cancel_event=cancel_event,
            progress=lambda fraction, detail: progress(0.1 + 0.62 * fraction, detail),
        )
        _cancel(cancel_event)
        if result.total_frames != frames or result.sample_rate_hz != rate:
            raise ValueError("Encoded audiobook timeline differs from the sound-mix frame plan.")
        if not scratch_output.is_file() or scratch_output.is_symlink():
            raise ValueError("Audio assembly did not create a regular export file.")

        from pandrator.logic.audio_processor import (
            _add_chapters_to_m4b,
            _save_metadata_and_cover,
        )

        _save_metadata_and_cover(
            str(scratch_output),
            output_format,
            tag_metadata,
            str(cover_path) if cover_path is not None else None,
            raise_on_error=True,
        )
        _cancel(cancel_event)
        if output_format == "m4b" and chapters:
            _add_chapters_to_m4b(
                str(scratch_output),
                chapters,
                total_duration_sec=frames / rate,
                raise_on_error=True,
            )
            _save_metadata_and_cover(
                str(scratch_output),
                output_format,
                tag_metadata,
                str(cover_path) if cover_path is not None else None,
                raise_on_error=True,
            )
        _cancel(cancel_event)
        _validate_active_lease(context, payload, session_id=session_id)

        export_metadata = {
            "format": output_format,
            "requested_format": output_format,
            "include_sounds": include_sounds,
            "duration_ms": round(frames * 1000 / rate),
            "metadata": tag_metadata,
            "cover_artifact_id": cover_artifact.id if cover_artifact else None,
            "sound_mix": deepcopy(sound_mix),
            "chapters": deepcopy(source_metadata.get("chapters", [])),
            "audio_timeline": deepcopy(source_metadata["audio_timeline"]),
            "output_settings": metadata_settings,
        }
        parents = [current.id]
        if cover_artifact is not None:
            parents.append(cover_artifact.id)
        progress(0.9, "Publishing audiobook export")
        requested_destination.parent.mkdir(parents=True, exist_ok=True)
        artifact = publish_video_export(
            context,
            scratch_output,
            requested_destination,
            session_id=session_id,
            parent_ids=parents,
            settings=settings,
            metadata=export_metadata,
            cancel_event=cancel_event,
            job_id=payload.get("_job_id"),
            lease_generation=payload.get("_lease_generation"),
        )
    progress(1.0, "Sound-mixed audiobook export ready")
    return artifact


__all__ = ["export_mixed_book_audio"]
