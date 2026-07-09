"""Shared source media extension helpers."""

from __future__ import annotations

import os

TEXT_SOURCE_EXTENSIONS = frozenset({".txt", ".pdf", ".epub", ".docx", ".mobi"})
SUBTITLE_SOURCE_EXTENSIONS = frozenset({".srt"})
VIDEO_SOURCE_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".avi", ".mov"})
AUDIO_SOURCE_EXTENSIONS = frozenset(
    {
        ".aac",
        ".aif",
        ".aiff",
        ".flac",
        ".m4a",
        ".mka",
        ".mp3",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }
)

MEDIA_SOURCE_EXTENSIONS = VIDEO_SOURCE_EXTENSIONS | AUDIO_SOURCE_EXTENSIONS
DUBBING_SOURCE_EXTENSIONS = MEDIA_SOURCE_EXTENSIONS | SUBTITLE_SOURCE_EXTENSIONS
SOURCE_FILE_EXTENSIONS = TEXT_SOURCE_EXTENSIONS | DUBBING_SOURCE_EXTENSIONS


def extension_for_path(path_or_extension: str) -> str:
    value = str(path_or_extension or "").strip().lower()
    if not value:
        return ""
    if value.startswith(".") and os.path.basename(value) == value:
        return value
    return os.path.splitext(value)[1].lower()


def is_audio_source(path_or_extension: str) -> bool:
    return extension_for_path(path_or_extension) in AUDIO_SOURCE_EXTENSIONS


def is_video_source(path_or_extension: str) -> bool:
    return extension_for_path(path_or_extension) in VIDEO_SOURCE_EXTENSIONS


def is_media_source(path_or_extension: str) -> bool:
    return extension_for_path(path_or_extension) in MEDIA_SOURCE_EXTENSIONS


def is_dubbing_source(path_or_extension: str) -> bool:
    return extension_for_path(path_or_extension) in DUBBING_SOURCE_EXTENSIONS
