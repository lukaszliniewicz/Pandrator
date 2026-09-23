"""Video export preparation, rendering and invocation-owned temporary files."""

from __future__ import annotations

import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from .export_inputs import ExportInputs, MediaExportSelection
from .export_publication import publish_video_export
from .export_subtitles import subtitle_track_details
from .export_video_commands import (
    VideoEncodingOptions,
    check_video_cancelled,
    render_burned_subtitle_video,
    render_soft_subtitle_video,
    render_transcoded_video,
    render_web_video,
    run_video_command,
)
from .models import Artifact, new_id
from .workflow_output_context import OutputWorkflowContext


@dataclass(frozen=True, slots=True)
class PreparedVideoAudio:
    path: Path
    parent_ids: list[str]
    tail_extension_ms: int


def _prepare_video_audio(
    context: OutputWorkflowContext,
    inputs: ExportInputs,
    selection: MediaExportSelection,
    *,
    media_path: Path,
    scratch_dir: Path,
    ffmpeg_executable: str,
    video_encoder: str,
    video_audio_bitrate: str,
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> PreparedVideoAudio:
    """Prepare speech/mixed audio and any frozen tail in the caller's workspace."""
    from pandrator.logic.dubbing.video_muxing import (
        build_replace_video_audio_command,
        build_video_tail_extension_command,
    )
    from pandrator.web.capabilities import ffmpeg_video_encoder_ids

    session_id = inputs.session_id
    settings = inputs.settings
    record = inputs.record
    upload_media = selection.upload_media
    if upload_media is None:
        raise ValueError("Video export requires a source video.")
    dubbing_audio = selection.dubbing_audio
    audio_mode = selection.audio_mode
    working_video = media_path
    audio_parent_ids: list[str] = [upload_media.id]
    tail_extension_ms = 0
    if dubbing_audio and audio_mode in {"dubbed", "mixed"}:
        from .soundtrack_export import (
            ensure_soundtrack_master,
            probe_soundtrack_media,
            resolve_video_tail_extension_ms,
        )

        reference_info = probe_soundtrack_media(media_path)
        _speech_record, speech_probe_path = context._resolve_input(
            dubbing_audio.id
        )
        speech_info = probe_soundtrack_media(speech_probe_path)
        # Freeze math runs against the VIDEO stream duration (not the
        # container duration). resolve_video_tail_extension_ms is the
        # single shared computation: 0 when the timeline fits, the
        # frame-ceiled extension when approved, otherwise a clear
        # duration warning. Its result is passed to both the video
        # tail step and the soundtrack master below so neither
        # double-extends nor clips.
        reference_duration = reference_info.get(
            "video_duration"
        ) or reference_info["duration"]
        # Any true-positive speech overrun must survive the final
        # mux: sub-tolerance overruns ride the full audio track
        # (shortest=False, no freeze/reencode), larger ones use the
        # shared frozen-tail extension or raise. The -shortest fast
        # path is only kept when the audio cannot exceed the video.
        overrun = speech_info["duration"] - reference_duration
        tail_extension_ms = 0
        if (
            reference_info["has_video"]
            and bool(settings.get("audio_match_source_duration", True))
        ):
            tail_extension_ms = resolve_video_tail_extension_ms(
                reference_duration=reference_duration,
                generated_duration=speech_info["duration"],
                fps=reference_info.get("fps"),
                settings=settings,
            )
        preserve_audio_tail = tail_extension_ms > 0 or overrun > 0
        if tail_extension_ms:
            if video_encoder not in ffmpeg_video_encoder_ids(
                ffmpeg_executable
            ):
                raise RuntimeError(
                    f"The selected FFmpeg build does not provide the {video_encoder} video encoder."
                )
            tail_video = (
                scratch_dir / f".{record.storage_key}-tail-{new_id()}.mp4"
            )
            tail_command = build_video_tail_extension_command(
                str(media_path),
                str(tail_video),
                tail_extension_ms / 1000,
                ffmpeg_executable=ffmpeg_executable,
                video_encoder=video_encoder,
                video_quality=settings.get("burn_video_quality", 18),
                video_speed=str(
                    settings.get("burn_video_speed") or "balanced"
                ),
                audio_codec="copy",
                audio_bitrate=video_audio_bitrate,
                video_resolution=settings.get(
                    "burn_video_resolution", "source"
                ),
            )
            progress(
                0.38,
                f"Extending the final video frame by {tail_extension_ms} ms "
                "to cover the voiceover tail",
            )
            run_video_command(tail_command, cancel_event)
            working_video = tail_video
        _audio_record, audio_path = context._resolve_input(dubbing_audio.id)
        audio_video = (
            scratch_dir / f".{record.storage_key}-audio-{new_id()}.mp4"
        )
        if audio_mode == "dubbed":
            command = build_replace_video_audio_command(
                str(working_video),
                str(audio_path),
                str(audio_video),
                ffmpeg_executable=ffmpeg_executable,
                audio_bitrate=video_audio_bitrate,
                shortest=not preserve_audio_tail,
            )
            progress(0.4, "Replacing source audio with generated speech")
        else:
            master = ensure_soundtrack_master(
                context, session_id=session_id, source=upload_media,
                speech=dubbing_audio, audio_mode="mixed", settings=settings,
                cancel_event=cancel_event,
                tail_extension_ms=tail_extension_ms,
            )
            _master, master_path = context._resolve_input(master.id)
            audio_parent_ids.append(master.id)
            command = build_replace_video_audio_command(
                str(working_video), str(master_path), str(audio_video),
                ffmpeg_executable=ffmpeg_executable,
                audio_bitrate=video_audio_bitrate,
                shortest=not preserve_audio_tail,
            )
            progress(0.4, "Using the selected mixed soundtrack")
        run_video_command(command, cancel_event)
        progress(0.58, "Media audio track ready")
        working_video = audio_video
        audio_parent_ids.append(dubbing_audio.id)
    return PreparedVideoAudio(working_video, audio_parent_ids, tail_extension_ms)


def render_video_export(
    context: OutputWorkflowContext,
    inputs: ExportInputs,
    selection: MediaExportSelection,
    *,
    output_dir: Path,
    export_name: str,
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
    job_id: str | None = None,
    lease_generation: int | None = None,
) -> Artifact:
    """Render one video and clean all transient files even if preparation fails."""
    from pandrator.logic.dubbing.bilingual_ass import write_bilingual_ass
    from pandrator.logic.dubbing.srt_utils import srt_to_vtt
    from pandrator.logic.dubbing.subtitle_finalization import finalize_srt_file
    from pandrator.logic.dubbing.video_muxing import normalize_video_resolution

    session_id = inputs.session_id
    settings = inputs.settings
    record = inputs.record
    upload_media = selection.upload_media
    if upload_media is None:
        raise ValueError("Video export requires a source video.")
    selected_subtitles = selection.selected_subtitles
    audio_mode = selection.audio_mode
    subtitle_mode = selection.subtitle_mode
    video_dir = output_dir / "video"

    def track_details(item: Artifact) -> tuple[str, str, str, bool]:
        return subtitle_track_details(
            item, record=record, settings=settings, track_count=len(selected_subtitles)
        )

    check_video_cancelled(cancel_event)
    progress(0.35, "Preparing source media")
    _media_record, media_path = context._resolve_input(upload_media.id)
    ffmpeg_executable = str(
        os.environ.get("PANDRATOR_FFMPEG_EXE")
        or shutil.which("ffmpeg")
        or "ffmpeg"
    )
    video_audio_bitrate = str(
        settings.get("burn_audio_bitrate") or "192k"
    ).strip()
    video_audio_codec = (
        str(settings.get("burn_audio_codec") or "copy").strip().lower()
    )
    if (
        audio_mode in {"dubbed", "mixed"} or video_audio_codec == "aac"
    ) and not re.fullmatch(
        r"[1-9][0-9]*(?:[kKmM])?",
        video_audio_bitrate,
    ):
        raise ValueError("Video AAC bitrate must look like 192k or 2M.")
    video_encoder = (
        str(settings.get("burn_video_encoder") or "libx264").strip().lower()
    )
    # Own scratch files from the first preparation command through promotion.
    # Registered helpers and final exports remain outside this workspace.
    with TemporaryDirectory(
        prefix=f".{record.storage_key}-video-", dir=output_dir
    ) as temporary_root:
        scratch_dir = Path(temporary_root)
        prepared = _prepare_video_audio(
            context, inputs, selection, media_path=media_path, scratch_dir=scratch_dir,
            ffmpeg_executable=ffmpeg_executable, video_encoder=video_encoder,
            video_audio_bitrate=video_audio_bitrate, progress=progress,
            cancel_event=cancel_event,
        )
        working_video = prepared.path
        audio_parent_ids = prepared.parent_ids
        tail_extension_ms = prepared.tail_extension_ms

        def _finalize_track_scratch(track_name: str) -> Path:
            return scratch_dir / f".{record.storage_key}-{track_name}-final-{new_id()}.srt"

        user_video_transcode = bool(settings.get("video_transcode")) or (
            subtitle_mode == "burned" and bool(selected_subtitles)
        )
        # A frozen tail already reencoded the video with the selected
        # encoder. Force the transcoded flag/metadata without paying
        # for a second full transcode when the user did not request one.
        video_transcode = user_video_transcode or tail_extension_ms > 0
        output_video_resolution = (
            normalize_video_resolution(
                settings.get("burn_video_resolution", "source")
            )
            if video_transcode
            else "source"
        )
        # Replacement and mixed soundtracks were encoded to AAC in the
        # preparation step above. Copy them during the video render so
        # the user's chosen bitrate is not lost to a second encode.
        render_audio_codec = (
            video_audio_codec if audio_mode == "source" else "copy"
        )
        encoding = VideoEncodingOptions(
            ffmpeg_executable=ffmpeg_executable,
            encoder=video_encoder,
            quality=settings.get("burn_video_quality", 18),
            speed=str(settings.get("burn_video_speed") or "balanced"),
            resolution=output_video_resolution,
            audio_codec=render_audio_codec,
            audio_bitrate=video_audio_bitrate,
        )
        variant = (
            f"_{subtitle_mode}"
            if subtitle_mode in {"soft", "burned"} and selected_subtitles
            else ""
        )
        video_dir.mkdir(parents=True, exist_ok=True)
        requested_destination = video_dir / f"{export_name}{variant}.mp4"
        render_destination = (
            scratch_dir / f".{record.storage_key}-render-{new_id()}.mp4"
        )
        video_track_artifacts: list[Artifact] = []
        # Playback/render helpers (player VTT sidecars, bilingual ASS
        # overlays) are registered but kept out of the deliverable
        # export folders; only the mp4 and explicit subtitle/text
        # exports land under exports/.
        intermediates_subtitle_dir = (
            output_dir.parent / "intermediates" / "subtitles"
        )
        finalized_subtitle_paths: dict[str, Path] = {}
        for item in selected_subtitles:
            track_name = track_details(item)[0]
            _subtitle_record, subtitle_path = context._resolve_input(
                item.id
            )
            scratch = _finalize_track_scratch(track_name)
            finalize_srt_file(subtitle_path, scratch, {**settings, "subtitle_language": track_details(item)[1]})
            finalized_subtitle_paths[item.id] = scratch
        if subtitle_mode == "soft" and selected_subtitles:
            tracks = []
            for index, item in enumerate(selected_subtitles, start=1):
                subtitle_path = finalized_subtitle_paths[item.id]
                track_name, language, title, is_default = track_details(
                    item
                )
                tracks.append(
                    {
                        "path": str(subtitle_path),
                        "language": language,
                        "title": title,
                        "default": is_default,
                    }
                )
                intermediates_subtitle_dir.mkdir(
                    parents=True, exist_ok=True
                )
                vtt_path = context.artifacts.next_available_path(
                    intermediates_subtitle_dir
                    / f"{record.storage_key}_{track_name}_player.vtt"
                )
                vtt_path.write_text(
                    srt_to_vtt(
                        subtitle_path.read_text(encoding="utf-8-sig")
                    ),
                    encoding="utf-8",
                )
                video_track_artifacts.append(
                    context.artifacts.register(
                        vtt_path,
                        kind="vtt",
                        role=f"video_subtitle_track_{track_name}",
                        session_id=session_id,
                        parent_ids=[item.id],
                        settings=settings,
                        metadata={
                            "language": language,
                            "title": title,
                            "default": is_default,
                        },
                    )
                )
                progress(
                    0.58 + 0.06 * (index / len(selected_subtitles)),
                    f"Prepared selectable subtitle track {index} of {len(selected_subtitles)}",
                )
            video_transcode = render_soft_subtitle_video(
                working_video, tracks, render_destination, encoding,
                transcode_video=user_video_transcode, progress=progress,
                cancel_event=cancel_event,
            ) or video_transcode
        elif subtitle_mode == "burned" and selected_subtitles:
            subtitle_paths = [
                finalized_subtitle_paths[item.id]
                for item in selected_subtitles
            ]
            burn_path = subtitle_paths[-1]
            if len(subtitle_paths) == 2:
                intermediates_subtitle_dir.mkdir(
                    parents=True, exist_ok=True
                )
                burn_path = Path(
                    write_bilingual_ass(
                        str(subtitle_paths[0]),
                        str(subtitle_paths[1]),
                        str(
                            context.artifacts.next_available_path(
                                intermediates_subtitle_dir
                                / "bilingual_subtitles.ass"
                            )
                        ),
                    )
                )
                context.artifacts.register(
                    burn_path,
                    kind="ass",
                    role="bilingual_subtitle_overlay",
                    session_id=session_id,
                    parent_ids=[item.id for item in selected_subtitles],
                    settings=settings,
                )
            render_burned_subtitle_video(
                working_video, burn_path, render_destination, encoding,
                language=str(settings.get("target_language") or "und"),
                progress=progress, cancel_event=cancel_event,
            )
        elif user_video_transcode:
            render_transcoded_video(
                working_video, render_destination, encoding,
                progress=progress, cancel_event=cancel_event,
            )
        else:
            video_transcode = render_web_video(
                working_video, render_destination, encoding,
                tail_extension_ms=tail_extension_ms, progress=progress,
                cancel_event=cancel_event,
            ) or video_transcode
        check_video_cancelled(cancel_event)
        if tail_extension_ms > 0:
            progress(
                0.9,
                f"Rendered media output ready (last frame frozen +{tail_extension_ms} ms)",
            )
        else:
            progress(0.9, "Rendered media output ready")
        subtitle_track_metadata = [
            {
                "artifact_id": item.id,
                "language": str(
                    (item.metadata_json or {}).get("language") or "und"
                ),
                "title": str(
                    (item.metadata_json or {}).get("title") or "Subtitles"
                ),
                "default": bool((item.metadata_json or {}).get("default")),
            }
            for item in video_track_artifacts
        ]
        return publish_video_export(
            context, render_destination, requested_destination,
            cancel_event=cancel_event, job_id=job_id, lease_generation=lease_generation,
            session_id=session_id,
            parent_ids=audio_parent_ids
            + [item.id for item in selected_subtitles]
            + [item.id for item in video_track_artifacts],
            settings=settings,
            metadata={
                "output_settings": inputs.output_settings_snapshot,
                "audio_mode": audio_mode,
                "subtitle_mode": subtitle_mode,
                "video_resolution": output_video_resolution,
                "video_transcoded": video_transcode,
                "video_encoder": video_encoder if video_transcode else None,
                "tail_extension_ms": tail_extension_ms,
                "audio_bitrate": (
                    video_audio_bitrate
                    if audio_mode in {"dubbed", "mixed"}
                    or render_audio_codec == "aac"
                    else None
                ),
                "subtitle_tracks": subtitle_track_metadata,
                "mix": {
                    "source_gain_db": settings.get(
                        "mix_source_gain_db", 0.0
                    ),
                    "voice_gain_db": settings.get("mix_voice_gain_db", 0.0),
                    "voice_lufs": settings.get("mix_voice_lufs", -16.0),
                    "ducking": settings.get("mix_ducking", "strong"),
                    "attack_ms": settings.get("mix_attack_ms", 25),
                    "release_ms": settings.get("mix_release_ms", 350),
                }
                if audio_mode == "mixed"
                else None,
            },
        )
