"""FFmpeg command builders for dubbing video muxing."""

from __future__ import annotations

import os
from pathlib import Path

from .languages import ffmpeg_subtitle_language_code, subtitle_language_title


def escape_ffmpeg_subtitles_filter_path(path: str) -> str:
    """Escape an absolute subtitle path for FFmpeg's subtitles filter."""
    normalized_path = os.path.abspath(path).replace("\\", "/")
    escaped_path = normalized_path.replace(":", r"\:")
    escaped_path = escaped_path.replace("'", r"\'")
    escaped_path = escaped_path.replace(",", r"\,")
    escaped_path = escaped_path.replace("[", r"\[")
    escaped_path = escaped_path.replace("]", r"\]")
    return escaped_path


BURN_VIDEO_ENCODERS = {
    "libx264",
    "libx265",
    "h264_nvenc",
    "hevc_nvenc",
    "h264_amf",
    "hevc_amf",
    "h264_qsv",
    "hevc_qsv",
    "h264_vaapi",
    "hevc_vaapi",
}

VIDEO_RESOLUTION_HEIGHTS = {
    "source": None,
    "360p": 360,
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "1440p": 1440,
    "2160p": 2160,
}


def normalize_video_resolution(value: str | int | None) -> str:
    """Normalize an output-height preset used by video transcodes."""

    normalized = str(value or "source").strip().lower()
    normalized = {
        "auto": "source",
        "native": "source",
        "original": "source",
        "original resolution": "source",
    }.get(normalized, normalized)
    if normalized.isdigit():
        normalized = f"{normalized}p"
    if normalized not in VIDEO_RESOLUTION_HEIGHTS:
        choices = ", ".join(VIDEO_RESOLUTION_HEIGHTS)
        raise ValueError(f"Video resolution must be one of: {choices}.")
    return normalized


def default_vaapi_render_device() -> str | None:
    """Return the first Linux render node suitable for FFmpeg VA-API output."""

    dri = Path("/dev/dri")
    if not dri.is_dir():
        return None
    return next((str(path) for path in sorted(dri.glob("renderD*")) if path.exists()), None)


def _video_transcode_arguments(
    *,
    video_encoder: str,
    video_quality: int,
    video_speed: str,
    audio_codec: str,
    audio_bitrate: str,
    hardware_device: str | None,
    video_resolution: str | int | None,
    extra_filters: tuple[str, ...] = (),
) -> tuple[list[str], list[str]]:
    normalized_encoder = str(video_encoder or "libx264").strip().lower()
    if normalized_encoder not in BURN_VIDEO_ENCODERS:
        raise ValueError(f"Unsupported video encoder: {normalized_encoder}")
    try:
        normalized_quality = int(video_quality)
    except (TypeError, ValueError) as error:
        raise ValueError("Video quality must be an integer from 0 to 51.") from error
    if not 0 <= normalized_quality <= 51:
        raise ValueError("Video quality must be between 0 and 51.")
    normalized_speed = str(video_speed or "balanced").strip().lower()
    if normalized_speed not in {"fast", "balanced", "quality"}:
        raise ValueError("Video encoding speed must be fast, balanced, or quality.")
    normalized_audio = str(audio_codec or "copy").strip().lower()
    if normalized_audio not in {"copy", "aac"}:
        raise ValueError("Video audio handling must be copy or AAC.")
    normalized_resolution = normalize_video_resolution(video_resolution)

    before_input: list[str] = []
    filters: list[str] = []
    target_height = VIDEO_RESOLUTION_HEIGHTS[normalized_resolution]
    if target_height is not None:
        filters.append(f"scale=-2:{target_height}:flags=lanczos")
    filters.extend(extra_filters)
    if normalized_encoder.endswith("_vaapi"):
        resolved_device = str(
            hardware_device or default_vaapi_render_device() or ""
        ).strip()
        if not resolved_device:
            raise ValueError(
                "VA-API encoding requires an accessible /dev/dri/renderD* device."
            )
        before_input.extend(["-vaapi_device", resolved_device])
        filters.extend(["format=nv12", "hwupload"])

    arguments: list[str] = []
    if filters:
        arguments.extend(["-vf", ",".join(filters)])
    arguments.extend(["-c:v", normalized_encoder])
    if normalized_encoder in {"libx264", "libx265"}:
        arguments.extend(
            [
                "-preset",
                {
                    "fast": "fast",
                    "balanced": "medium",
                    "quality": "slow",
                }[normalized_speed],
                "-crf",
                str(normalized_quality),
                "-pix_fmt",
                "yuv420p",
            ]
        )
    elif normalized_encoder.endswith("_nvenc"):
        arguments.extend(
            [
                "-preset",
                {"fast": "p3", "balanced": "p4", "quality": "p6"}[
                    normalized_speed
                ],
                "-rc",
                "vbr",
                "-cq",
                str(normalized_quality),
                "-b:v",
                "0",
            ]
        )
    elif normalized_encoder.endswith("_amf"):
        arguments.extend(
            [
                "-quality",
                {
                    "fast": "speed",
                    "balanced": "balanced",
                    "quality": "quality",
                }[normalized_speed],
                "-rc",
                "cqp",
                "-qp_i",
                str(normalized_quality),
                "-qp_p",
                str(normalized_quality),
            ]
        )
    elif normalized_encoder.endswith("_qsv"):
        arguments.extend(
            [
                "-preset",
                {
                    "fast": "fast",
                    "balanced": "medium",
                    "quality": "slow",
                }[normalized_speed],
                "-global_quality",
                str(normalized_quality),
            ]
        )
    elif normalized_encoder.endswith("_vaapi"):
        arguments.extend(["-qp", str(normalized_quality)])

    arguments.extend(["-c:a", normalized_audio])
    if normalized_audio == "aac":
        arguments.extend(["-b:a", str(audio_bitrate or "192k")])
    return before_input, arguments


def build_video_transcode_command(
    video_path: str,
    output_path: str,
    *,
    ffmpeg_executable: str = "ffmpeg",
    video_encoder: str = "libx264",
    video_quality: int = 18,
    video_speed: str = "balanced",
    audio_codec: str = "copy",
    audio_bitrate: str = "192k",
    hardware_device: str | None = None,
    video_resolution: str | int | None = "source",
) -> list[str]:
    """Build a video transcode without requiring a subtitle overlay."""

    before_input, arguments = _video_transcode_arguments(
        video_encoder=video_encoder,
        video_quality=video_quality,
        video_speed=video_speed,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        hardware_device=hardware_device,
        video_resolution=video_resolution,
    )
    return [
        ffmpeg_executable,
        "-y",
        *before_input,
        "-i",
        video_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        *arguments,
        "-movflags",
        "+faststart",
        output_path,
    ]


def build_add_subtitles_command(
    synced_video_path: str,
    equalized_srt_path: str,
    temp_output_path: str,
    subtitle_mode: str = "soft",
    subtitle_language: str = "en",
    ffmpeg_executable: str = "ffmpeg",
    video_encoder: str = "libx264",
    video_quality: int = 18,
    video_speed: str = "balanced",
    audio_codec: str = "copy",
    audio_bitrate: str = "192k",
    hardware_device: str | None = None,
    video_resolution: str | int | None = "source",
) -> list[str]:
    """Build the FFmpeg command for soft or burned subtitle output."""
    normalized_mode = str(subtitle_mode or "soft").strip().lower()
    if normalized_mode not in {"soft", "burned"}:
        normalized_mode = "soft"

    if normalized_mode == "burned":
        escaped_subtitle_path = escape_ffmpeg_subtitles_filter_path(equalized_srt_path)
        before_input, arguments = _video_transcode_arguments(
            video_encoder=video_encoder,
            video_quality=video_quality,
            video_speed=video_speed,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            hardware_device=hardware_device,
            video_resolution=video_resolution,
            # Scale is inserted first by the shared builder so libass renders
            # text at the requested output size.
            extra_filters=(f"subtitles=filename='{escaped_subtitle_path}'",),
        )
        return [
            ffmpeg_executable,
            "-y",
            *before_input,
            "-i",
            synced_video_path,
            *arguments,
            "-movflags",
            "+faststart",
            temp_output_path,
        ]

    subtitle_language_code = ffmpeg_subtitle_language_code(subtitle_language)
    subtitle_title = subtitle_language_title(subtitle_language)
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        synced_video_path,
        "-i",
        equalized_srt_path,
        "-c",
        "copy",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        f"language={subtitle_language_code}",
        "-metadata:s:s:0",
        f"title={subtitle_title}",
        "-metadata:s:s:0",
        f"handler_name={subtitle_title}",
        temp_output_path,
    ]


def build_replace_video_audio_command(
    video_path: str,
    audio_path: str,
    temp_output_path: str,
    *,
    ffmpeg_executable: str = "ffmpeg",
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> list[str]:
    """Build the FFmpeg command for replacing a video's audio stream."""
    command = [
        ffmpeg_executable,
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        str(audio_codec or "aac"),
    ]
    if str(audio_codec or "aac").strip().lower() == "aac":
        command.extend(["-b:a", str(audio_bitrate or "192k")])
    command.extend(["-shortest", temp_output_path])
    return command


def build_multi_soft_subtitle_command(
    video_path: str,
    subtitle_tracks: list[dict[str, str]],
    output_path: str,
    *,
    ffmpeg_executable: str = "ffmpeg",
    transcode_video: bool = False,
    video_encoder: str = "libx264",
    video_quality: int = 18,
    video_speed: str = "balanced",
    audio_codec: str = "copy",
    audio_bitrate: str = "192k",
    hardware_device: str | None = None,
    video_resolution: str | int | None = "source",
) -> list[str]:
    """Build an MP4 remux command with one or more language-labelled subtitle tracks."""
    before_input: list[str] = []
    transcode_arguments: list[str] = []
    if transcode_video:
        before_input, transcode_arguments = _video_transcode_arguments(
            video_encoder=video_encoder,
            video_quality=video_quality,
            video_speed=video_speed,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            hardware_device=hardware_device,
            video_resolution=video_resolution,
        )
    command = [ffmpeg_executable, "-y", *before_input, "-i", video_path]
    for track in subtitle_tracks:
        command.extend(["-i", str(track.get("path") or "")])
    command.extend(["-map", "0:v:0", "-map", "0:a?"])
    for index in range(len(subtitle_tracks)):
        command.extend(["-map", f"{index + 1}:0"])
    if transcode_video:
        command.extend(transcode_arguments)
    else:
        command.extend(["-c:v", "copy", "-c:a", "copy"])
    command.extend(["-c:s", "mov_text"])
    for index, track in enumerate(subtitle_tracks):
        language = ffmpeg_subtitle_language_code(str(track.get("language") or "und"))
        title = str(track.get("title") or language)
        disposition = "default" if bool(track.get("default")) else "0"
        command.extend(
            [
                f"-metadata:s:s:{index}",
                f"language={language}",
                f"-metadata:s:s:{index}",
                f"title={title}",
                f"-metadata:s:s:{index}",
                f"handler_name={title}",
                f"-disposition:s:{index}",
                disposition,
            ]
        )
    command.append(output_path)
    return command
