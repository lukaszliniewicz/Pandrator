"""Bounded, cancellation-aware source cropping for managed voice references."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from numbers import Real
from pathlib import Path
from typing import Any

from .audio_imports import MAX_IMPORT_DURATION_SECONDS
from .media_process import (
    resolve_ffmpeg_executable,
    resolve_ffprobe_executable,
    run_media_process,
)


class VoiceReferenceTrimError(ValueError):
    """A requested voice-reference crop is invalid for its managed source."""


@dataclass(frozen=True, slots=True)
class VoiceReferenceAudioInfo:
    duration_seconds: Decimal
    sample_rate_hz: int
    channels: int


@dataclass(frozen=True, slots=True)
class VoiceReferenceTrim:
    start_seconds: Decimal
    requested_end_seconds: Decimal | None
    end_seconds: Decimal
    source_duration_seconds: Decimal
    sample_rate_hz: int

    def provenance(self) -> dict[str, float | None]:
        return {
            "start_seconds": float(self.start_seconds),
            "end_seconds": float(self.end_seconds),
            "requested_end_seconds": (
                float(self.requested_end_seconds)
                if self.requested_end_seconds is not None
                else None
            ),
            "source_duration_seconds": float(self.source_duration_seconds),
        }


def _seconds(value: Any, *, field: str, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise VoiceReferenceTrimError(f"{field} must be a finite number of seconds.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise VoiceReferenceTrimError(f"{field} must be a finite number of seconds.") from error
    if not result.is_finite() or result < 0:
        raise VoiceReferenceTrimError(f"{field} must be finite and nonnegative.")
    return result


def reference_trim_requested(start_seconds: Any, end_seconds: Any) -> bool:
    start = _seconds(start_seconds, field="start_seconds")
    end = _seconds(end_seconds, field="end_seconds", optional=True)
    assert start is not None
    return start > 0 or end is not None


def _probe_duration(stream: dict[str, Any], format_info: dict[str, Any]) -> Decimal:
    duration_ticks = stream.get("duration_ts")
    time_base = stream.get("time_base")
    if duration_ticks is not None and time_base:
        try:
            ticks = int(duration_ticks)
            base = Fraction(str(time_base))
            exact = Decimal(ticks * base.numerator) / Decimal(base.denominator)
            if exact.is_finite() and exact > 0:
                return exact
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            pass

    for value in (stream.get("duration"), format_info.get("duration")):
        if value is None:
            continue
        try:
            duration = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if duration.is_finite() and duration > 0:
            return duration
    raise VoiceReferenceTrimError("The selected audio has no usable duration.")


def probe_voice_reference_audio(
    source: Path,
    *,
    ffprobe_executable: str | None = None,
    cancel_event: threading.Event | None = None,
) -> VoiceReferenceAudioInfo:
    """Probe exact stream duration with the shared cancellable media runner."""

    result = run_media_process(
        [
            resolve_ffprobe_executable(ffprobe_executable),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration,duration_ts,time_base,sample_rate,channels:format=duration",
            "-of",
            "json",
            str(source),
        ],
        cancel_event=cancel_event,
        capture_stdout=True,
    )
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams") if isinstance(payload, dict) else None
        stream = streams[0] if isinstance(streams, list) and streams else None
        format_info = payload.get("format") if isinstance(payload, dict) else None
        if not isinstance(stream, dict):
            raise VoiceReferenceTrimError("The selected file has no audio stream.")
        if not isinstance(format_info, dict):
            format_info = {}
        duration = _probe_duration(stream, format_info)
        sample_rate_hz = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
    except (
        AttributeError,
        IndexError,
        TypeError,
        ValueError,
        OverflowError,
        json.JSONDecodeError,
    ) as error:
        if isinstance(error, VoiceReferenceTrimError):
            raise
        raise VoiceReferenceTrimError("The selected audio could not be inspected.") from error

    if sample_rate_hz <= 0 or channels <= 0:
        raise VoiceReferenceTrimError("The selected audio stream is invalid.")
    if duration > Decimal(str(MAX_IMPORT_DURATION_SECONDS)):
        raise VoiceReferenceTrimError("Managed voice-reference audio cannot exceed 300 seconds.")
    return VoiceReferenceAudioInfo(duration, sample_rate_hz, channels)


def validate_voice_reference_trim(
    start_seconds: Any,
    end_seconds: Any,
    audio_info: VoiceReferenceAudioInfo,
) -> VoiceReferenceTrim:
    """Clamp a one-frame end overshoot and reject empty or out-of-range crops."""

    start = _seconds(start_seconds, field="start_seconds")
    requested_end = _seconds(end_seconds, field="end_seconds", optional=True)
    assert start is not None
    if requested_end is not None and requested_end <= start:
        raise VoiceReferenceTrimError("end_seconds must be greater than start_seconds.")
    duration = audio_info.duration_seconds
    if start >= duration:
        raise VoiceReferenceTrimError("start_seconds must be before the end of the selected audio.")
    frame_tolerance = Decimal(1) / Decimal(audio_info.sample_rate_hz)
    if requested_end is not None and requested_end > duration + frame_tolerance:
        raise VoiceReferenceTrimError("end_seconds exceeds the selected audio duration.")
    effective_end = min(requested_end, duration) if requested_end is not None else duration
    assert effective_end is not None
    if effective_end - start < frame_tolerance:
        raise VoiceReferenceTrimError("The selected audio crop is shorter than one frame.")
    return VoiceReferenceTrim(
        start_seconds=start,
        requested_end_seconds=requested_end,
        end_seconds=effective_end,
        source_duration_seconds=duration,
        sample_rate_hz=audio_info.sample_rate_hz,
    )


def inspect_voice_reference_trim(
    source: Path,
    start_seconds: Any,
    end_seconds: Any,
    *,
    ffprobe_executable: str | None = None,
    cancel_event: threading.Event | None = None,
) -> VoiceReferenceTrim:
    """Probe a managed recording and validate its requested crop bounds."""

    audio_info = probe_voice_reference_audio(
        source,
        ffprobe_executable=ffprobe_executable,
        cancel_event=cancel_event,
    )
    return validate_voice_reference_trim(start_seconds, end_seconds, audio_info)


def trim_voice_reference_audio(
    source: Path,
    destination: Path,
    trim: VoiceReferenceTrim,
    *,
    ffmpeg_executable: str | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Write a cropped PCM WAV without modifying the managed source artifact."""

    if destination.exists() or destination.is_symlink():
        raise VoiceReferenceTrimError("The temporary crop output already exists.")
    start = float(trim.start_seconds)
    end = float(trim.end_seconds)
    filter_graph = f"atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS"
    command = [
        resolve_ffmpeg_executable(ffmpeg_executable),
        "-hide_banner",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        filter_graph,
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(destination),
    ]
    run_media_process(command, cancel_event=cancel_event)
    if not destination.is_file() or destination.stat().st_size <= 44:
        raise VoiceReferenceTrimError("FFmpeg did not produce the requested crop.")
