"""Media-edit rendering with explicit worker dependencies."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .workflow_text_protocols import StoreSrtDocumentProtocol, StoreTimedWordsProtocol

if TYPE_CHECKING:
    from pandrator.logic.media_edit import MediaCue

    from .artifacts import ArtifactService
    from .database import Database
    from .media_edit import MediaEditService
    from .models import Artifact


@dataclass(frozen=True, slots=True)
class MediaEditRenderContext:
    database: Database
    artifacts: ArtifactService
    media_edit: MediaEditService
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _media_edit_cues: Callable[[dict[str, Any]], list[MediaCue]]
    _operation_dir: Callable[[str, str], Path]
    _store_srt_document: StoreSrtDocumentProtocol
    _store_timed_words: StoreTimedWordsProtocol
    _sha256_file: Callable[[Path], str]
    _required_media_edit_time: Callable[[dict[str, Any], str], int]
    _monotonic: Callable[[], float]


def media_edit_render(context: MediaEditRenderContext, payload, progress, cancel_event):
    """Render one reviewed immutable media-edit revision."""

    from pandrator.logic.dubbing.audio_sync import media_has_audio_stream
    from pandrator.logic.dubbing.srt_utils import compose_srt
    from pandrator.logic.dubbing.video_muxing import (
        build_removal_only_video_command,
        normalize_video_resolution,
    )
    from pandrator.logic.media_edit import (
        KeepRange,
        caption_to_srt,
        media_cues_to_transcript,
        retime_cues,
    )
    from pandrator.web.capabilities import ffmpeg_video_encoder_ids

    from .media_process import (
        MediaProcessCancelled,
        MediaProcessError,
        resolve_ffmpeg_executable,
        resolve_ffprobe_executable,
        run_media_process,
    )

    if cancel_event.is_set():
        return {}
    session_id = str(payload.get("session_id") or "")
    try:
        revision_number = int(payload.get("revision"))
    except (TypeError, ValueError) as error:
        raise ValueError("Media-edit render revision must be an integer.") from error
    revision = context.media_edit.revision(session_id, revision_number)
    if revision is None:
        raise ValueError(
            f"Media-edit revision {revision_number} is not available for this session."
        )
    if not bool(revision.get("reviewed")):
        raise ValueError("The media-edit revision must be reviewed before rendering.")
    source_info = revision.get("source_media_artifact")
    source_id = str((source_info or {}).get("id") or "")
    source_artifact, source_path = context._resolve_input(source_id)
    expected_hash = str(source_artifact.content_hash or "").strip()
    if not expected_hash:
        raise ValueError("The pinned source artifact has no registered content hash.")
    if context._sha256_file(source_path) != expected_hash:
        raise ValueError("The pinned source artifact changed after the revision was created.")

    settings = dict(payload.get("settings") or {})
    settings_hash = str(payload.get("settings_hash") or "")
    cues = context._media_edit_cues(revision)
    keep_ranges = tuple(
        KeepRange(
            str(item.get("id") or f"keep-{index:06d}"),
            context._required_media_edit_time(item, "start_ms"),
            context._required_media_edit_time(item, "end_ms"),
            item.get("label"),
        )
        for index, item in enumerate(revision.get("keep_ranges") or [], start=1)
        if isinstance(item, dict)
    )
    if not keep_ranges:
        raise ValueError("The selected media-edit revision has no retained ranges.")
    retimed_cues = retime_cues(cues, keep_ranges)
    operation_dir = context._operation_dir(session_id, "media-edit-render")
    plan_id = str(revision.get("plan_id") or "")
    revision_id = str(revision.get("revision_id") or "")
    revision_tag = f"{plan_id}-r{revision_number}-{revision_id}"
    subtitle_path = operation_dir / f"media-edit-{revision_tag}.srt"
    word_timestamps_path = operation_dir / f"media-edit-{revision_tag}.json"
    output_path = operation_dir / f"media-edit-{revision_tag}.mp4"
    word_count = sum(len(cue.words) for cue in retimed_cues)
    word_timestamps_path.write_text(
        json.dumps(
            media_cues_to_transcript(
                retimed_cues,
                source_format="media_edit",
                metadata={
                    "plan_id": plan_id,
                    "revision_id": revision_id,
                    "revision": revision_number,
                    "timing_method": "media_edit_retime",
                    "word_count": word_count,
                },
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if word_count:
        from pandrator.logic.dubbing.subtitle_finalization import (
            compose_transcript_segments_with_ownership,
        )

        composition = compose_transcript_segments_with_ownership(
            word_timestamps_path,
            settings,
        )
        published_cues = composition.segments
        word_segment_ordinals = composition.word_segment_ordinals
        subtitle_path.write_text(compose_srt(published_cues), encoding="utf-8")
        composition_mode = "word_timed_semantic"
    else:
        published_cues = list(retimed_cues)
        word_segment_ordinals = None
        subtitle_path.write_text(caption_to_srt(retimed_cues), encoding="utf-8")
        composition_mode = "source_cues"
    composition_metadata = {
        "source_cue_count": len(retimed_cues),
        "composed_cue_count": len(published_cues),
        "composition_mode": composition_mode,
    }
    encoder = str(settings.get("burn_video_encoder") or "libx264").strip().lower()
    parent_ids = [
        source_id,
        str((revision.get("editorial_transcript_artifact") or {}).get("id") or ""),
        str((revision.get("timing_artifact") or {}).get("id") or ""),
    ]
    parent_ids = [item for item in parent_ids if item]
    metadata = {
        "plan_id": plan_id,
        "media_edit_revision_id": revision_id,
        "revision_id": revision_id,
        "revision": revision_number,
        "content_hash": revision.get("content_hash"),
        "source_artifact_id": source_id,
        "source_content_hash": expected_hash,
        "settings_hash": settings_hash,
        "effective_settings": settings,
        "video_encoder": encoder,
        "word_count": word_count,
        **composition_metadata,
    }
    # Persist candidates before selecting the complete subtitle/word pair.
    if cancel_event.is_set():
        return {}
    subtitle_artifact = context.artifacts.register(
        subtitle_path,
        kind="srt",
        role="media_edit_subtitles_candidate",
        session_id=session_id,
        parent_ids=parent_ids,
        settings=settings,
        metadata=metadata,
    )
    word_timestamps_artifact = context.artifacts.register(
        word_timestamps_path,
        kind="json",
        role="media_edit_word_timestamps_candidate",
        session_id=session_id,
        parent_ids=[subtitle_artifact.id],
        settings=settings,
        metadata={
            **metadata,
            "subtitle_artifact_id": subtitle_artifact.id,
            "timing_method": "media_edit_retime",
        },
    )
    editorial_id = str((revision.get("editorial_transcript_artifact") or {}).get("id") or "")
    editorial_artifact = None
    if editorial_id:
        editorial_artifact, _ = context._resolve_input(editorial_id)
    document_id, document_revision_id = context._store_srt_document(
        session_id,
        subtitle_artifact,
        "media_edit_subtitles",
        parent_artifact=editorial_artifact,
        speaker_overrides={
            index: cue.speaker for index, cue in enumerate(published_cues, start=1) if cue.speaker
        },
    )
    stored_word_count = context._store_timed_words(
        document_revision_id,
        word_timestamps_path,
        segment_by_word_ordinal=word_segment_ordinals,
    )
    progress(0.18, "Publishing subtitle revision and timed words")
    if cancel_event.is_set():
        return {}
    subtitle_registration = context.artifacts.prepare_registration(subtitle_path, settings=settings)
    word_registration = context.artifacts.prepare_registration(
        word_timestamps_path, settings=settings
    )
    try:
        with context.database.session() as session:
            if cancel_event.is_set():
                raise MediaProcessCancelled("Media-edit publication was canceled.")
            # Keep native document/revision metadata when promoting in place.
            subtitle_artifact = context.artifacts.register_in_session(
                session,
                subtitle_path,
                kind="srt",
                role="media_edit_subtitles",
                session_id=session_id,
                parent_ids=parent_ids,
                settings=settings,
                _prepared=subtitle_registration,
            )
            word_timestamps_artifact = context.artifacts.register_in_session(
                session,
                word_timestamps_path,
                kind="json",
                role="media_edit_word_timestamps",
                session_id=session_id,
                parent_ids=[subtitle_artifact.id],
                settings=settings,
                _prepared=word_registration,
            )
            if cancel_event.is_set():
                raise MediaProcessCancelled("Media-edit publication was canceled.")
    except MediaProcessCancelled:
        return {}
    # Correction can consume this persisted pair during the long encode.
    # Encode failure or cancellation preserves the published subtitles.
    if cancel_event.is_set():
        return {}
    duration_ms = sum(item.end_ms - item.start_ms for item in keep_ranges)
    result = {
        "subtitle_artifact_id": subtitle_artifact.id,
        "plan_id": plan_id,
        "revision_id": revision_id,
        "revision": revision_number,
        "duration_ms": duration_ms,
        "media_edit_word_timestamps_artifact_id": word_timestamps_artifact.id,
        "word_timestamps_artifact_id": word_timestamps_artifact.id,
        "word_timestamps_path": word_timestamps_artifact.relative_path,
        "word_count": stored_word_count,
        **composition_metadata,
        "document_id": document_id,
        "document_revision_id": document_revision_id,
    }

    if payload.get("subtitles_only") is True:
        progress(1.0, "Resegmented subtitles and timed words ready")
        return {**result, "subtitles_only": True}

    ffmpeg_executable = resolve_ffmpeg_executable(
        str(settings.get("ffmpeg_executable") or "") or None
    )
    encoder = str(settings.get("burn_video_encoder") or "libx264").strip().lower()
    if encoder not in ffmpeg_video_encoder_ids(ffmpeg_executable):
        raise ValueError(f"The selected FFmpeg build does not provide the {encoder} video encoder.")
    resolution = normalize_video_resolution(settings.get("burn_video_resolution", "source"))

    def run_audio_probe(command, **_options):
        return run_media_process(
            command,
            cancel_event=cancel_event,
            capture_stdout=True,
            timeout_seconds=30.0,
        )

    try:
        has_audio = media_has_audio_stream(
            source_path,
            ffprobe_executable=resolve_ffprobe_executable(
                str(settings.get("ffprobe_executable") or "") or None
            ),
            run_func=run_audio_probe,
        )
    except MediaProcessCancelled:
        return {}
    except (OSError, subprocess.SubprocessError, MediaProcessError) as error:
        raise ValueError("The pinned source media could not be inspected for audio.") from error
    command = build_removal_only_video_command(
        str(source_path),
        str(output_path),
        tuple((item.start_ms, item.end_ms) for item in keep_ranges),
        has_audio=has_audio,
        ffmpeg_executable=ffmpeg_executable,
        video_encoder=encoder,
        video_quality=settings.get("burn_video_quality", 18),
        video_speed=str(settings.get("burn_video_speed") or "balanced"),
        audio_bitrate=str(settings.get("burn_audio_bitrate") or "192k"),
        hardware_device=(
            str(
                settings.get("burn_video_hardware_device") or settings.get("hardware_device") or ""
            ).strip()
            or None
        ),
        video_resolution=resolution,
        include_progress=True,
    )
    progress(0.2, "Subtitle revision and timed words ready; rendering edited media")
    if cancel_event.is_set():
        return {}
    last_processed_ms = 0
    last_reported_progress = 0.2
    last_reported_percent = 0.0
    last_reported_at = context._monotonic()

    def report_render_progress(record: dict[str, str]) -> None:
        nonlocal last_processed_ms
        nonlocal last_reported_progress
        nonlocal last_reported_percent
        nonlocal last_reported_at

        out_times_us = []
        for field in ("out_time_us", "out_time_ms"):
            try:
                parsed = int(record.get(field, ""))
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                out_times_us.append(parsed)
        if not out_times_us:
            return

        processed_ms = min(duration_ms, max(out_times_us) // 1000)
        processed_ms = max(last_processed_ms, processed_ms)
        if processed_ms <= last_processed_ms:
            return
        last_processed_ms = processed_ms

        fraction = min(1.0, processed_ms / duration_ms)
        mapped_progress = min(0.95, 0.2 + 0.75 * fraction)
        mapped_progress = max(last_reported_progress, mapped_progress)
        percent = fraction * 100
        now = context._monotonic()
        if now - last_reported_at < 1.0 and percent - last_reported_percent < 1.0:
            return
        if mapped_progress <= last_reported_progress:
            return

        progress(
            mapped_progress,
            f"Rendering edited media: {processed_ms / 1000:.1f}s / "
            f"{duration_ms / 1000:.1f}s ({percent:.0f}%)",
        )
        last_reported_progress = mapped_progress
        last_reported_percent = percent
        last_reported_at = now

    try:
        run_media_process(
            command,
            cancel_event=cancel_event,
            progress_callback=report_render_progress,
        )
    except MediaProcessCancelled:
        output_path.unlink(missing_ok=True)
        return {}
    except MediaProcessError as error:
        output_path.unlink(missing_ok=True)
        raise ValueError(
            "Media-edit rendering requires a video source and FFmpeg could not produce the MP4."
        ) from error
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    if cancel_event.is_set():
        output_path.unlink(missing_ok=True)
        return {}
    media_artifact = context.artifacts.register(
        output_path,
        kind="video",
        role="media_edit_media",
        session_id=session_id,
        parent_ids=parent_ids,
        settings=settings,
        metadata=metadata,
    )
    progress(1.0, "Edited media ready")
    return {**result, "media_artifact_id": media_artifact.id}
