"""Deterministic assembly of immutable generation takes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from pydub import AudioSegment


OUTPUT_FORMATS = {"wav", "mp3", "m4b", "opus", "flac"}


def compose_audio(
    parts: Iterable[tuple[AudioSegment, int]],
    settings: dict[str, Any] | None = None,
) -> AudioSegment:
    """Join ordered audio parts with per-segment fades and inter-part silence."""
    values = list(parts)
    if not values:
        raise ValueError("At least one audio take is required for assembly.")
    options = settings or {}
    fade_enabled = bool(options.get("fade_enabled", options.get("enable_fade", False)))
    fade_in = max(0, int(options.get("fade_in_ms", options.get("fade_in_duration", 0)) or 0))
    fade_out = max(0, int(options.get("fade_out_ms", options.get("fade_out_duration", 0)) or 0))
    combined = AudioSegment.empty()
    for index, (source, silence_after_ms) in enumerate(values):
        audio = source
        if fade_enabled:
            if fade_in:
                audio = audio.fade_in(min(fade_in, len(audio)))
            if fade_out:
                audio = audio.fade_out(min(fade_out, len(audio)))
        combined += audio
        if index < len(values) - 1 and int(silence_after_ms or 0) > 0:
            combined += AudioSegment.silent(
                duration=max(0, int(silence_after_ms)),
                frame_rate=max(8000, int(audio.frame_rate or 24000)),
            )
    return combined


def export_audio(audio: AudioSegment, destination: Path, output_format: str, bitrate: str = "192k") -> None:
    """Export an assembly using the container/codec expected by Pandrator."""
    normalized = str(output_format or "wav").strip().lower()
    if normalized not in OUTPUT_FORMATS:
        raise ValueError(f"Unsupported audio output format: {output_format}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {}
    ffmpeg_format = normalized
    if normalized == "wav":
        options["codec"] = "pcm_s16le"
    elif normalized == "mp3":
        options.update(codec="libmp3lame", bitrate=bitrate)
    elif normalized == "m4b":
        ffmpeg_format = "mp4"
        options.update(codec="aac", bitrate=bitrate)
    elif normalized == "opus":
        options.update(codec="libopus", bitrate=bitrate)
    exported = audio.export(destination, format=ffmpeg_format, **options)
    exported.close()

