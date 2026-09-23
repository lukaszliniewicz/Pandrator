"""Video render modes, cooperative cancellation and stream-copy fallback policy."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from pandrator.logic.cancellable_process import ProcessCancelled, run_cancellable

Progress = Callable[[float, str | None], None]


@dataclass(frozen=True, slots=True)
class VideoEncodingOptions:
    ffmpeg_executable: str
    encoder: str
    quality: Any
    speed: str
    resolution: str
    audio_codec: str
    audio_bitrate: str


def check_video_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("Video export canceled.")


def run_video_command(command: list[str], cancel_event: threading.Event) -> None:
    """Stop owned processes on cancellation and retain FFmpeg failure details."""
    check_video_cancelled(cancel_event)
    try:
        run_cancellable(
            command, cancel_event=cancel_event, check=True, capture_output=True, text=True
        )
    except ProcessCancelled as error:
        raise InterruptedError("Video export canceled.") from error
    except subprocess.CalledProcessError:
        # A canceled command must not start an expensive fallback render.
        check_video_cancelled(cancel_event)
        raise
    check_video_cancelled(cancel_event)


def _failure_reason(error: subprocess.CalledProcessError, default: str) -> str:
    lines = str(error.stderr or error.stdout or "").strip().splitlines()
    return lines[-1] if lines else default


def _encoder_available(options: VideoEncodingOptions) -> bool:
    from .capabilities import ffmpeg_video_encoder_ids

    return options.encoder in ffmpeg_video_encoder_ids(options.ffmpeg_executable)


def _require_encoder(options: VideoEncodingOptions) -> None:
    if not _encoder_available(options):
        raise RuntimeError(
            f"The selected FFmpeg build does not provide the {options.encoder} video encoder."
        )


def _encoding_kwargs(options: VideoEncodingOptions, *, fallback: bool = False) -> dict[str, Any]:
    return {
        "ffmpeg_executable": options.ffmpeg_executable,
        "video_encoder": options.encoder,
        "video_quality": options.quality,
        "video_speed": options.speed,
        "video_resolution": "source" if fallback else options.resolution,
        "audio_codec": "aac" if fallback else options.audio_codec,
        "audio_bitrate": options.audio_bitrate,
    }


def _run_labeled_command(
    command: list[str], label: str, cancel_event: threading.Event
) -> None:
    try:
        run_video_command(command, cancel_event)
    except subprocess.CalledProcessError as error:
        reason = _failure_reason(error, "FFmpeg returned a non-zero exit status.")
        raise RuntimeError(f"{label} failed: {reason}") from error


def _run_with_fallback(
    command: list[str],
    fallback_command: Callable[[], list[str]],
    options: VideoEncodingOptions,
    *,
    remux_error_label: str,
    fallback_error_label: str,
    progress_detail: str,
    progress: Progress,
    cancel_event: threading.Event,
) -> bool:
    """Try stream copy once; return whether the compatibility transcode ran."""
    try:
        run_video_command(command, cancel_event)
    except subprocess.CalledProcessError as remux_error:
        if not _encoder_available(options):
            reason = _failure_reason(
                remux_error, "FFmpeg could not stream-copy the source into MP4."
            )
            raise RuntimeError(
                f"{remux_error_label} and the selected "
                f"{options.encoder} fallback encoder is unavailable: {reason}"
            ) from remux_error
        progress(0.68, progress_detail)
        check_video_cancelled(cancel_event)
        _run_labeled_command(
            fallback_command(), f"{fallback_error_label} with {options.encoder}", cancel_event
        )
        return True
    return False


def render_soft_subtitle_video(
    source: Path,
    tracks: list[dict[str, Any]],
    destination: Path,
    options: VideoEncodingOptions,
    *,
    transcode_video: bool,
    progress: Progress,
    cancel_event: threading.Event,
) -> bool:
    from pandrator.logic.dubbing.video_muxing import build_multi_soft_subtitle_command

    if transcode_video:
        _require_encoder(options)
    command = build_multi_soft_subtitle_command(
        str(source), tracks, str(destination), transcode_video=transcode_video,
        **_encoding_kwargs(options),
    )
    progress(
        0.65,
        "Transcoding media with selectable subtitles"
        if transcode_video else "Rendering web-optimized media with selectable subtitles",
    )
    if transcode_video:
        run_video_command(command, cancel_event)
        return True
    return _run_with_fallback(
        command,
        lambda: build_multi_soft_subtitle_command(
            str(source), tracks, str(destination), transcode_video=True,
            **_encoding_kwargs(options, fallback=True),
        ),
        options,
        remux_error_label="Selectable-subtitle MP4 remuxing failed",
        fallback_error_label="Selectable-subtitle fallback",
        progress_detail="Stream-copy subtitle mux was unavailable; transcoding a web-compatible MP4",
        progress=progress,
        cancel_event=cancel_event,
    )


def render_burned_subtitle_video(
    source: Path,
    subtitle: Path,
    destination: Path,
    options: VideoEncodingOptions,
    *,
    language: str,
    progress: Progress,
    cancel_event: threading.Event,
) -> None:
    from pandrator.logic.dubbing.video_muxing import build_add_subtitles_command
    from pandrator.logic.dubbing_handler import resolve_ffmpeg_for_burned_subtitles

    burn_ffmpeg = resolve_ffmpeg_for_burned_subtitles()
    if not burn_ffmpeg:
        raise RuntimeError(
            "Burned subtitles require an FFmpeg build with the subtitles/libass filter. Install or select Pandrator's bundled FFmpeg, or use soft subtitles."
        )
    burn_options = replace(options, ffmpeg_executable=burn_ffmpeg)
    _require_encoder(burn_options)
    command = build_add_subtitles_command(
        str(source), str(subtitle), str(destination), subtitle_mode="burned",
        subtitle_language=language, **_encoding_kwargs(burn_options),
    )
    progress(0.65, "Rendering burned subtitles into video")
    _run_labeled_command(
        command, f"Burned-subtitle transcoding with {options.encoder}", cancel_event
    )


def render_transcoded_video(
    source: Path, destination: Path, options: VideoEncodingOptions,
    *, progress: Progress, cancel_event: threading.Event,
) -> None:
    from pandrator.logic.dubbing.video_muxing import build_video_transcode_command

    _require_encoder(options)
    command = build_video_transcode_command(str(source), str(destination), **_encoding_kwargs(options))
    progress(0.65, "Transcoding video output")
    _run_labeled_command(command, f"Video transcoding with {options.encoder}", cancel_event)


def render_web_video(
    source: Path, destination: Path, options: VideoEncodingOptions,
    *, tail_extension_ms: int, progress: Progress, cancel_event: threading.Event,
) -> bool:
    from pandrator.logic.dubbing.video_muxing import (
        build_video_transcode_command,
        build_web_optimized_remux_command,
    )

    progress(
        0.65,
        f"Optimizing frozen-tail media for web playback (+{tail_extension_ms} ms)"
        if tail_extension_ms > 0
        else "Optimizing media for web playback without video transcoding",
    )
    command = build_web_optimized_remux_command(
        str(source), str(destination), ffmpeg_executable=options.ffmpeg_executable,
        audio_codec=options.audio_codec, audio_bitrate=options.audio_bitrate,
    )
    return _run_with_fallback(
        command,
        lambda: build_video_transcode_command(
            str(source), str(destination), **_encoding_kwargs(options, fallback=True)
        ),
        options,
        remux_error_label="Web-optimized MP4 remuxing failed",
        fallback_error_label="Web-compatible video fallback",
        progress_detail="Stream-copy remux was unavailable; transcoding a web-compatible MP4",
        progress=progress,
        cancel_event=cancel_event,
    )
