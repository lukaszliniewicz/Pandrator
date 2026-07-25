"""Resolution-bounded waveform peak generation."""

from __future__ import annotations

import os
import math
import re
import threading
from dataclasses import dataclass

from .media_process import (
    MediaProcessCancelled,
    probe_audio_stream,
    resolve_ffmpeg_executable,
    run_media_process,
)


WAVEFORM_MAX_SAMPLE_RATE = 8000
PEAK_LEVEL_RE = re.compile(r"lavfi\.astats\.Overall\.Peak_level=(-?(?:inf|\d+(?:\.\d+)?))")

WaveformCancelled = MediaProcessCancelled


@dataclass(frozen=True)
class WaveformPeaks:
    duration_ms: int
    channels: int
    points: tuple[float, ...]
    analysis_sample_rate_hz: int


def _parse_peak_levels(metadata: str, max_points: int) -> tuple[float, ...]:
    peaks: list[float] = []
    for match in PEAK_LEVEL_RE.finditer(metadata):
        value = match.group(1).lower()
        if value in {"-inf", "inf"}:
            peak = 0.0
        else:
            peak = min(1.0, max(0.0, 10 ** (float(value) / 20)))
        peaks.append(round(peak, 5))
        if len(peaks) >= max_points:
            break
    return tuple(peaks or [0.0])


def generate_waveform_peaks(
    source: str | os.PathLike[str],
    *,
    max_points: int = 1600,
    work_dir: str | os.PathLike[str] | None = None,
    ffmpeg_executable: str | None = None,
    ffprobe_executable: str | None = None,
    cancel_event: threading.Event | None = None,
) -> WaveformPeaks:
    """Compute peak bins in FFmpeg and return only resolution-sized metadata."""

    points = max(128, min(5000, int(max_points or 1600)))
    info = probe_audio_stream(
        source,
        ffprobe_executable=ffprobe_executable,
        cancel_event=cancel_event,
    )
    analysis_rate = min(WAVEFORM_MAX_SAMPLE_RATE, info.sample_rate_hz)
    analysis_samples = max(
        1,
        int(math.ceil(info.duration_ms * analysis_rate / 1000)),
    )
    samples_per_bin = max(1, int(math.ceil(analysis_samples / points)))
    filter_graph = (
        f"aresample={analysis_rate},"
        f"asetnsamples=n={samples_per_bin}:p=1,"
        "astats=metadata=1:reset=1:"
        "measure_overall=Peak_level:measure_perchannel=none,"
        "ametadata=print:key=lavfi.astats.Overall.Peak_level:"
        "file='pipe\\:1'"
    )
    result = run_media_process(
        [
            resolve_ffmpeg_executable(ffmpeg_executable),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            os.fspath(source),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            filter_graph,
            "-f",
            "null",
            "-",
        ],
        cancel_event=cancel_event,
        capture_stdout=True,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise MediaProcessCancelled("Waveform generation was canceled.")
    peaks = _parse_peak_levels(result.stdout, points)
    return WaveformPeaks(
        duration_ms=info.duration_ms,
        channels=info.channels,
        points=peaks,
        analysis_sample_rate_hz=analysis_rate,
    )
