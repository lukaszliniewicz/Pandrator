"""Cancellable FFmpeg/FFprobe process helpers for bounded media workflows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class MediaProcessCancelled(RuntimeError):
    """Raised when a media subprocess is stopped by a job cancellation."""


class MediaProcessError(RuntimeError):
    """Raised when FFmpeg or FFprobe cannot complete a media operation."""


@dataclass(frozen=True)
class MediaProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class AudioStreamInfo:
    duration_ms: int
    sample_rate_hz: int
    channels: int


def resolve_ffmpeg_executable(explicit: str | None = None) -> str:
    return str(
        explicit
        or os.environ.get("PANDRATOR_FFMPEG_EXE")
        or shutil.which("ffmpeg")
        or "ffmpeg"
    )


def resolve_ffprobe_executable(explicit: str | None = None) -> str:
    return str(
        explicit
        or os.environ.get("PANDRATOR_FFPROBE_EXE")
        or shutil.which("ffprobe")
        or "ffprobe"
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run_media_process(
    command: Sequence[str | os.PathLike[str]],
    *,
    cancel_event: threading.Event | None = None,
    capture_stdout: bool = False,
) -> MediaProcessResult:
    """Run a hidden media process while polling for cooperative cancellation.

    Output is redirected to temporary files instead of pipes so a verbose or
    malformed media file cannot deadlock the worker by filling an unread pipe.
    """

    if cancel_event is not None and cancel_event.is_set():
        raise MediaProcessCancelled("Media processing was canceled.")
    normalized = [os.fspath(value) for value in command]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                normalized,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file if capture_stdout else subprocess.DEVNULL,
                stderr=stderr_file,
                creationflags=creationflags,
            )
        except OSError as error:
            raise MediaProcessError(
                f"Could not start {Path(normalized[0]).name or normalized[0]}: {error}"
            ) from error
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.1):
                    _stop_process(process)
                    raise MediaProcessCancelled("Media processing was canceled.")
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            _stop_process(process)
            raise

        stdout = ""
        if capture_stdout:
            stdout_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr_file.seek(0)
        stderr = stderr_file.read().decode("utf-8", errors="replace")
        result = MediaProcessResult(process.returncode or 0, stdout, stderr)
        if result.returncode:
            detail = stderr.strip()[-4000:] or "No diagnostic output was produced."
            raise MediaProcessError(
                f"{Path(normalized[0]).name or normalized[0]} exited with code "
                f"{result.returncode}: {detail}"
            )
        return result


def _positive_float(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def probe_audio_stream(
    path: str | os.PathLike[str],
    *,
    ffprobe_executable: str | None = None,
    cancel_event: threading.Event | None = None,
) -> AudioStreamInfo:
    """Read compact audio metadata without decoding the media in Python."""

    source = Path(path)
    result = run_media_process(
        [
            resolve_ffprobe_executable(ffprobe_executable),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration,sample_rate,channels:format=duration",
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
        stream = streams[0] if isinstance(streams, list) and streams else {}
        format_payload = payload.get("format") if isinstance(payload, dict) else {}
        duration_seconds = _positive_float(stream.get("duration"))
        if not duration_seconds and isinstance(format_payload, dict):
            duration_seconds = _positive_float(format_payload.get("duration"))
        sample_rate = int(_positive_float(stream.get("sample_rate")))
        channels = int(_positive_float(stream.get("channels")))
    except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MediaProcessError(f"FFprobe returned invalid audio metadata for {source.name}.") from error
    if duration_seconds <= 0 or sample_rate <= 0 or channels <= 0:
        raise MediaProcessError(f"No usable audio stream metadata was found for {source.name}.")
    return AudioStreamInfo(
        duration_ms=max(1, int(round(duration_seconds * 1000))),
        sample_rate_hz=sample_rate,
        channels=channels,
    )
