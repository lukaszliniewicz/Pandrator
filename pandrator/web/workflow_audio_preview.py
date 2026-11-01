"""Audio preview rendering and publication with explicit worker dependencies."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select

from pandrator.logic.media_input_policy import LOCAL_MEDIA_INPUT_OPTIONS
from pandrator.runtime import DataPaths

from .access.contracts import AccessDenied
from .access.media_execution_policy import (
    MediaSourceSnapshot,
    require_media_execution_artifact,
)
from .access.output_mix_policy import MixPreviewInput, require_output_mix_access
from .accounts.execution_actor import current_execution_actor
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
    """Render an independent preview and publish inside a proved writer."""
    from .media_process import (
        MediaProcessCancelled,
        MediaProcessError,
        resolve_ffmpeg_executable,
        run_media_process,
    )

    if cancel_event.is_set():
        return {}
    database = context.artifacts.database
    try:
        with database.snapshot_session() as session:
            source = require_media_execution_artifact(session, payload, "audio.preview")
            snapshot = MediaSourceSnapshot.from_artifact(source)
            source_path = context.paths.managed_path(source.relative_path)
            if not source_path.is_file():
                raise FileNotFoundError("The source audio file is missing.")
        destination_dir = (
            context.session_dir(source.session_id)
            if source.session_id else context.paths.artifacts / "audio-previews"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
    except AccessDenied:
        raise
    except (KeyError, OSError, ValueError):
        raise RuntimeError("The source audio preview could not be prepared.") from None

    destination = destination_dir / f"audio-preview-v1-{source.id}-{context.new_id()}.mp3"
    temporary = destination_dir / f".audio-preview-v1-{source.id}-{context.new_id()}.mp3"
    temporary_created = False
    destination_created = False
    committed = False
    settings = {"preview_version": "v1", "codec": "mp3", "bitrate_kbps": 64,
                "channels": 1, "sample_rate_hz": 48000}
    try:
        progress(0.05, "Preparing source audio preview")
        if cancel_event.is_set():
            return {}
        command = [
            resolve_ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
            *LOCAL_MEDIA_INPUT_OPTIONS, "-i", str(source_path), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-map_metadata", "-1", "-ac", "1", "-ar", "48000",
            "-c:a", "libmp3lame", "-b:a", "64k", str(temporary),
        ]
        with temporary.open("xb"):
            temporary_created = True
        progress(0.1, "Transcoding source audio preview")
        try:
            run_media_process(command, cancel_event=cancel_event)
        except MediaProcessCancelled:
            return {}
        except MediaProcessError:
            raise RuntimeError("The source audio preview could not be transcoded.") from None
        if cancel_event.is_set():
            return {}
        prepared = replace(
            context.artifacts.prepare_registration(temporary, settings=settings),
            relative_path=context.paths.relative_managed_path(destination),
        )
        progress(0.9, "Registering source audio preview")
        if cancel_event.is_set():
            return {}
        with database.immediate_session() as session:
            current = require_media_execution_artifact(
                session, payload, "audio.preview", expected_source=snapshot,
            )
            if cancel_event.is_set():
                return {}
            with destination.open("xb"):
                destination_created = True
            os.replace(temporary, destination)
            artifact = context.artifacts.register_in_session(
                session, destination, kind="audio", role="source_audio_preview",
                session_id=current.session_id, workspace_id=current.workspace_id,
                project_id=current.project_id, parent_ids=[current.id],
                settings=settings,
                metadata={"source_artifact_id": current.id,
                          "source_content_hash": current.content_hash,
                          "source_artifact_hash": current.content_hash,
                          "preview_version": "v1"},
                _prepared=prepared,
            )
        committed = True
        return {"artifact_id": artifact.id, "source_artifact_id": source.id}
    except MediaProcessCancelled:
        return {}
    except AccessDenied:
        raise
    except Exception:  # noqa: BLE001
        raise RuntimeError("The source audio preview could not be prepared.") from None
    finally:
        cleanup_failed = False
        for path, owned in (
            (temporary, temporary_created),
            (destination, destination_created and not committed),
        ):
            if owned:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed = True
        if cleanup_failed:
            raise RuntimeError("The source audio preview could not be prepared.") from None


def _prove_output_mix(context: AudioPreviewContext, captured: MixPreviewInput | None) -> None:
    if captured is None:
        return
    with context.artifacts.database.snapshot_session() as unit:
        require_output_mix_access(
            unit,
            captured.session_id,
            captured.source.id,
            captured.dubbing.id,
            expected=captured,
        )


def _mix_preview_metadata(
    payload: dict[str, Any],
    settings: dict[str, Any],
    source_id: str,
    dubbing_id: str,
    start: float,
    duration: float,
    automatic: bool,
    method: str,
) -> dict[str, Any]:
    return {
        "generation_run_id": str(payload.get("generation_run_id") or ""),
        "source_artifact_id": source_id,
        "dubbing_artifact_id": dubbing_id,
        "start_seconds": start,
        "duration_seconds": duration,
        "automatic_start": automatic,
        "automatic_start_method": method,
        "mix": {
            "source_gain_db": settings.get("mix_source_gain_db", 0.0),
            "voice_gain_db": settings.get("mix_voice_gain_db", 0.0),
            "voice_lufs": settings.get("mix_voice_lufs", -16.0),
            "ducking": settings.get("mix_ducking", "strong"),
            "attack_ms": settings.get("mix_attack_ms", 25),
            "release_ms": settings.get("mix_release_ms", 350),
        },
    }


def _mix_preview_result(
    artifact: Artifact,
    start: float,
    duration: float,
    automatic: bool,
    method: str,
) -> dict[str, Any]:
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
        "start_seconds": start,
        "duration_seconds": duration,
        "automatic_start": automatic,
        "automatic_start_method": method,
    }


def _cleanup_current_mix(
    context: AudioPreviewContext,
    destination: Path,
    temporary: Path,
    *,
    temporary_created: bool,
    destination_created: bool,
    committed: bool,
) -> None:
    if temporary_created:
        temporary.unlink(missing_ok=True)
    if destination_created and not committed:
        relative = context.paths.relative_managed_path(destination)
        with context.artifacts.database.session() as unit:
            registered = unit.scalar(
                select(Artifact.id).where(Artifact.relative_path == relative).limit(1)
            )
        if registered is None:
            destination.unlink(missing_ok=True)


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
    source_id = str(payload.get("source_artifact_id") or "")
    dubbing_id = str(payload.get("dubbing_artifact_id") or "")
    captured: MixPreviewInput | None = None
    dubbing_metadata: Any = None
    legacy_dubbing: Artifact | None = None
    if current_execution_actor.get() is not None:
        with context.artifacts.database.snapshot_session() as unit:
            captured = require_output_mix_access(unit, session_id, source_id, dubbing_id)
            if captured is not None and payload.get("start_seconds") is None:
                dubbing_metadata = unit.scalar(
                    select(Artifact.metadata_json).where(Artifact.id == captured.dubbing.id)
                )
    if captured is not None:
        source_path = context.paths.managed_path(captured.source.relative_path)
        dubbing_path = context.paths.managed_path(captured.dubbing.relative_path)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        if not dubbing_path.is_file():
            raise FileNotFoundError(dubbing_path)
        source_id, dubbing_id = captured.source.id, captured.dubbing.id
    else:
        source, source_path = context.resolve_input(source_id)
        dubbing, dubbing_path = context.resolve_input(dubbing_id)
        if source.session_id != session_id or dubbing.session_id != session_id:
            raise ValueError("Mix preview inputs do not belong to this session.")
        source_id, dubbing_id = source.id, dubbing.id
        legacy_dubbing = dubbing

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
        _prove_output_mix(context, captured)
        source_info = probe_audio_stream(source_path, cancel_event=cancel_event)
        _prove_output_mix(context, captured)
        dubbing_info = probe_audio_stream(dubbing_path, cancel_event=cancel_event)
        if automatic_start:
            if captured is None:
                assert legacy_dubbing is not None
                dubbing_metadata = legacy_dubbing.metadata_json
            timeline_starts = [
                float(item["target_start_ms"])
                for item in (dubbing_metadata or {}).get("takes", [])
                if isinstance(item, dict) and isinstance(item.get("target_start_ms"), (int, float))
            ]
            if timeline_starts:
                requested_start = max(0.0, min(timeline_starts) / 1000.0 - 1.0)
                automatic_start_method = "assembly_timeline"
            else:
                _prove_output_mix(context, captured)
                requested_start = find_first_audible_seconds(
                    dubbing_path, cancel_event=cancel_event
                )
                automatic_start_method = "audio_detection"
    except MediaProcessCancelled:
        return {}
    available_seconds = min(source_info.duration_ms, dubbing_info.duration_ms) / 1000.0
    remaining_seconds = available_seconds - requested_start
    if remaining_seconds < 0.25:
        raise ValueError("The preview start is beyond the available source and voiceover audio.")
    duration_seconds = min(requested_duration, remaining_seconds)

    _prove_output_mix(context, captured)
    destination_dir = (
        context.paths.sessions / captured.storage_key
        if captured is not None
        else context.session_dir(session_id)
    ) / "previews"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / (
        f"soundtrack-mix-preview-{context.new_id()}.wav"
        if captured is not None
        else "soundtrack-mix-preview.wav"
    )
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
    previous_destination = (
        destination_dir / f".soundtrack-mix-preview-{context.new_id()}.previous"
        if captured is None
        else None
    )
    previous_destination_staged = False
    replaced_destination = False
    temporary_created = False
    committed = False
    result: dict[str, Any] | None = None
    artifact: Artifact | None = None
    if captured is None:
        progress(0.2, "Rendering soundtrack mix preview")
    try:
        if captured is not None:
            _prove_output_mix(context, captured)
            with temporary_destination.open("xb"):
                temporary_created = True
            progress(0.2, "Rendering soundtrack mix preview")
        _prove_output_mix(context, captured)
        run_media_process(command, cancel_event=cancel_event)
        if cancel_event.is_set():
            return {}
        if captured is None:
            assert previous_destination is not None
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
        metadata = _mix_preview_metadata(
            payload,
            settings,
            source_id,
            dubbing_id,
            requested_start,
            duration_seconds,
            automatic_start,
            automatic_start_method,
        )
        if captured is not None:
            _prove_output_mix(context, captured)
            prepared = replace(
                context.artifacts.prepare_registration(temporary_destination, settings=settings),
                relative_path=context.paths.relative_managed_path(destination),
            )
            with context.artifacts.database.immediate_session() as unit:
                require_output_mix_access(
                    unit, session_id, source_id, dubbing_id, expected=captured
                )
                if cancel_event.is_set():
                    return {}
                if (
                    unit.scalar(
                        select(Artifact.id)
                        .where(Artifact.relative_path == prepared.relative_path)
                        .limit(1)
                    )
                    is not None
                ):
                    raise FileExistsError("Mix preview output already exists.")
                try:
                    with destination.open("xb"):
                        replaced_destination = True
                except FileExistsError:
                    raise FileExistsError("Mix preview output already exists.") from None
                os.replace(temporary_destination, destination)
                artifact = context.artifacts.register_in_session(
                    unit,
                    destination,
                    kind="audio",
                    role="mix_preview",
                    session_id=session_id,
                    parent_ids=[source_id, dubbing_id],
                    settings=settings,
                    metadata=metadata,
                    _prepared=prepared,
                )
                result = _mix_preview_result(
                    artifact,
                    requested_start,
                    duration_seconds,
                    automatic_start,
                    automatic_start_method,
                )
        else:
            artifact = context.artifacts.register(
                destination,
                kind="audio",
                role="mix_preview",
                session_id=session_id,
                parent_ids=[source_id, dubbing_id],
                replace_parent_ids=True,
                settings=settings,
                metadata=metadata,
            )
        # Registration commits the receipt. Later cleanup/reporting failures
        # must not restore bytes that no longer match that receipt.
        committed = True
    except MediaProcessCancelled:
        return {}
    finally:
        if captured is not None:
            _cleanup_current_mix(
                context,
                destination,
                temporary_destination,
                temporary_created=temporary_created,
                destination_created=replaced_destination,
                committed=committed,
            )
        else:
            assert previous_destination is not None
            if not committed:
                if previous_destination_staged:
                    os.replace(previous_destination, destination)
                elif replaced_destination:
                    destination.unlink(missing_ok=True)
            temporary_destination.unlink(missing_ok=True)
            if committed:
                previous_destination.unlink(missing_ok=True)

    progress(1.0, "Soundtrack mix preview ready")
    if captured is not None:
        assert result is not None
        return result
    assert artifact is not None
    return _mix_preview_result(
        artifact, requested_start, duration_seconds, automatic_start, automatic_start_method
    )
