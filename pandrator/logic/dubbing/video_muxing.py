"""FFmpeg command builders for dubbing video muxing."""

from __future__ import annotations

import os

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


def build_add_subtitles_command(
    synced_video_path: str,
    equalized_srt_path: str,
    temp_output_path: str,
    subtitle_mode: str = "soft",
    subtitle_language: str = "en",
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    """Build the FFmpeg command for soft or burned subtitle output."""
    normalized_mode = str(subtitle_mode or "soft").strip().lower()
    if normalized_mode not in {"soft", "burned"}:
        normalized_mode = "soft"

    if normalized_mode == "burned":
        escaped_subtitle_path = escape_ffmpeg_subtitles_filter_path(equalized_srt_path)
        return [
            ffmpeg_executable,
            "-y",
            "-i",
            synced_video_path,
            "-vf",
            f"subtitles=filename='{escaped_subtitle_path}'",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "copy",
            temp_output_path,
        ]

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
