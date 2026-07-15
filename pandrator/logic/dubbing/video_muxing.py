"""FFmpeg command builders for dubbing video muxing."""

from __future__ import annotations

import os
from pathlib import Path

from .languages import ffmpeg_subtitle_language_code


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


def default_vaapi_render_device() -> str | None:
    """Return the first Linux render node suitable for FFmpeg VA-API output."""

    dri = Path("/dev/dri")
    if not dri.is_dir():
        return None
    return next((str(path) for path in sorted(dri.glob("renderD*")) if path.exists()), None)


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
) -> list[str]:
    """Build the FFmpeg command for soft or burned subtitle output."""
    normalized_mode = str(subtitle_mode or "soft").strip().lower()
    if normalized_mode not in {"soft", "burned"}:
        normalized_mode = "soft"

    if normalized_mode == "burned":
        escaped_subtitle_path = escape_ffmpeg_subtitles_filter_path(equalized_srt_path)
        normalized_encoder = str(video_encoder or "libx264").strip().lower()
        if normalized_encoder not in BURN_VIDEO_ENCODERS:
            raise ValueError(f"Unsupported burned-subtitle video encoder: {normalized_encoder}")
        try:
            normalized_quality = int(video_quality)
        except (TypeError, ValueError) as error:
            raise ValueError("Burned-subtitle video quality must be an integer from 0 to 51.") from error
        if not 0 <= normalized_quality <= 51:
            raise ValueError("Burned-subtitle video quality must be between 0 and 51.")
        normalized_speed = str(video_speed or "balanced").strip().lower()
        if normalized_speed not in {"fast", "balanced", "quality"}:
            raise ValueError("Burned-subtitle encoding speed must be fast, balanced, or quality.")
        normalized_audio = str(audio_codec or "copy").strip().lower()
        if normalized_audio not in {"copy", "aac"}:
            raise ValueError("Burned-subtitle audio handling must be copy or AAC.")

        command = [
            ffmpeg_executable,
            "-y",
        ]
        subtitle_filter = f"subtitles=filename='{escaped_subtitle_path}'"
        if normalized_encoder.endswith("_vaapi"):
            resolved_device = str(hardware_device or default_vaapi_render_device() or "").strip()
            if not resolved_device:
                raise ValueError("VA-API encoding requires an accessible /dev/dri/renderD* device.")
            command.extend(["-vaapi_device", resolved_device])
            subtitle_filter += ",format=nv12,hwupload"
        command.extend(["-i", synced_video_path, "-vf", subtitle_filter, "-c:v", normalized_encoder])

        if normalized_encoder in {"libx264", "libx265"}:
            command.extend(
                [
                    "-preset",
                    {"fast": "fast", "balanced": "medium", "quality": "slow"}[normalized_speed],
                    "-crf",
                    str(normalized_quality),
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
        elif normalized_encoder.endswith("_nvenc"):
            command.extend(
                [
                    "-preset",
                    {"fast": "p3", "balanced": "p4", "quality": "p6"}[normalized_speed],
                    "-rc",
                    "vbr",
                    "-cq",
                    str(normalized_quality),
                    "-b:v",
                    "0",
                ]
            )
        elif normalized_encoder.endswith("_amf"):
            command.extend(
                [
                    "-quality",
                    {"fast": "speed", "balanced": "balanced", "quality": "quality"}[normalized_speed],
                    "-rc",
                    "cqp",
                    "-qp_i",
                    str(normalized_quality),
                    "-qp_p",
                    str(normalized_quality),
                ]
            )
        elif normalized_encoder.endswith("_qsv"):
            command.extend(
                [
                    "-preset",
                    {"fast": "fast", "balanced": "medium", "quality": "slow"}[normalized_speed],
                    "-global_quality",
                    str(normalized_quality),
                ]
            )
        elif normalized_encoder.endswith("_vaapi"):
            command.extend(["-qp", str(normalized_quality)])

        command.extend(["-c:a", normalized_audio])
        if normalized_audio == "aac":
            command.extend(["-b:a", str(audio_bitrate or "192k")])
        command.extend(["-movflags", "+faststart", temp_output_path])
        return command

    subtitle_language_code = ffmpeg_subtitle_language_code(subtitle_language)
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
        temp_output_path,
    ]


def build_replace_video_audio_command(
    video_path: str,
    audio_path: str,
    temp_output_path: str,
    *,
    ffmpeg_executable: str = "ffmpeg",
    audio_codec: str = "aac",
) -> list[str]:
    """Build the FFmpeg command for replacing a video's audio stream."""
    return [
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
        "-shortest",
        temp_output_path,
    ]


def build_multi_soft_subtitle_command(
    video_path: str,
    subtitle_tracks: list[dict[str, str]],
    output_path: str,
    *,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    """Build an MP4 remux command with one or more language-labelled subtitle tracks."""
    command = [ffmpeg_executable, "-y", "-i", video_path]
    for track in subtitle_tracks:
        command.extend(["-i", str(track.get("path") or "")])
    command.extend(["-map", "0:v:0", "-map", "0:a?"])
    for index in range(len(subtitle_tracks)):
        command.extend(["-map", f"{index + 1}:0"])
    command.extend(["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text"])
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
                f"-disposition:s:{index}",
                disposition,
            ]
        )
    command.append(output_path)
    return command
