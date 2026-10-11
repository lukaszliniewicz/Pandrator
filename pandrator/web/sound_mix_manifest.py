"""Frozen base-timeline geometry and exact duration probing."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .audio_assembly import preferred_pcm_format
from .media_process import MediaProcessError, resolve_ffprobe_executable, run_media_process
from .sound_mix_inputs import _int


def _base_geometry(
    base: dict[str, Any], workflow_kind: str, path: Path
) -> tuple[int, int, int, bool]:
    metadata = base.get("metadata_json") or {}
    timeline = metadata.get("audio_timeline")
    fixed_timeline = workflow_kind != "audiobook"
    _source_rate, channels = preferred_pcm_format(path)
    if timeline is not None:
        if not isinstance(timeline, dict) or timeline.get("version") != 1:
            raise ValueError("Sound mixing requires an audio_timeline version 1 base artifact.")
        rate = _int(timeline.get("sample_rate_hz"), "audio_timeline.sample_rate_hz", minimum=8000)
        frames = _int(timeline.get("total_frames"), "audio_timeline.total_frames", minimum=1)
        if rate > 192000:
            raise ValueError("audio_timeline.sample_rate_hz must not exceed 192000 Hz.")
    elif fixed_timeline:
        rate, probed_channels, frames = _probe_audio_geometry(path)
        rate = _int(rate, "probed sample rate", minimum=8000)
        if rate > 192000:
            raise ValueError("Probed sample rate must not exceed 192000 Hz.")
        channels = 1 if probed_channels <= 1 else 2
        _int(frames, "probed base frame count", minimum=1)
    else:
        raise ValueError("Reassemble audiobook audio with a version 1 audio timeline before sound mixing.")
    return rate, channels, frames, fixed_timeline


def _probe_audio_geometry(path: Path) -> tuple[int, int, int]:
    """Read stream duration without reducing it to rounded milliseconds."""

    result = run_media_process(
        [
            resolve_ffprobe_executable(),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration_ts,time_base,duration,sample_rate,channels:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_stdout=True,
    )
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams") if isinstance(payload, dict) else None
        stream = streams[0] if isinstance(streams, list) and streams else None
        if not isinstance(stream, dict):
            raise ValueError("No audio stream was reported.")
        rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        duration_frames: Fraction
        if stream.get("duration_ts") not in (None, "N/A") and stream.get("time_base"):
            duration_frames = (
                Fraction(str(stream["duration_ts"]))
                * Fraction(str(stream["time_base"]))
                * rate
            )
        else:
            duration = stream.get("duration")
            if duration in (None, "N/A"):
                format_payload = payload.get("format") if isinstance(payload, dict) else None
                duration = format_payload.get("duration") if isinstance(format_payload, dict) else None
            duration_frames = Fraction(str(duration)) * rate
        frames = round(duration_frames)
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        ZeroDivisionError,
        OverflowError,
        json.JSONDecodeError,
    ) as error:
        raise MediaProcessError(
            f"FFprobe returned invalid exact audio duration metadata for {path.name}."
        ) from error
    if rate <= 0 or channels <= 0 or duration_frames <= 0 or frames <= 0:
        raise MediaProcessError(f"No usable exact audio duration was found for {path.name}.")
    return rate, channels, frames


__all__ = ["_base_geometry"]
