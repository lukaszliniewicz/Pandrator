"""CrispASR transcription helpers for Pandrator dubbing and voice references."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..cancellable_process import ProcessCancelled, run_cancellable
from ..source_media import is_audio_source
from . import cloud_stt
from .crispasr import CrispASRTranscriptionResult, transcribe
from .srt_utils import renumber_subtitles
from .subtitle_finalization import compose_from_transcript_json

logger = logging.getLogger(__name__)


class ExternalToolError(RuntimeError):
    pass


def safe_decode(output: bytes | str | None) -> str:
    if output is None:
        return ""
    return (
        output if isinstance(output, str) else output.decode("utf-8", errors="replace")
    )


def extract_audio(
    source_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    source_name: str,
    *,
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
    cancel_event: threading.Event | None = None,
) -> str:
    audio_path = Path(session_dir) / f"{source_name}.wav"
    try:
        if audio_path.resolve() == Path(source_path).resolve():
            audio_path = Path(session_dir) / f"{source_name}_transcription.wav"
    except OSError:
        pass
    command = [
        ffmpeg_executable,
        "-i",
        str(source_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-af",
        "aresample,loudnorm",
        "-y",
        str(audio_path),
    ]
    try:
        if cancel_event is not None and run_func is subprocess.run:
            result = run_cancellable(
                command,
                cancel_event=cancel_event,
                check=True,
                capture_output=True,
            )
        else:
            result = run_func(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        raise ExternalToolError(
            f"FFmpeg failed to extract audio: {safe_decode(getattr(error, 'stderr', None))}"
        ) from error
    if getattr(result, "stderr", None):
        logger.debug(
            "FFmpeg transcription normalization: %s", safe_decode(result.stderr)
        )
    return str(audio_path)


def extract_audio_excerpt(
    source_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    output_name: str,
    start_ms: int,
    end_ms: int,
    *,
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
    cancel_event: threading.Event | None = None,
) -> str:
    """Extract one bounded, normalized WAV excerpt without invoking a shell."""

    try:
        start = int(start_ms)
        end = int(end_ms)
    except (TypeError, ValueError) as error:
        raise ValueError("Excerpt times must be integers in milliseconds.") from error
    if start < 0 or end <= start:
        raise ValueError("Excerpt start must be before its end and non-negative.")
    duration = end - start
    if duration > 60_000:
        raise ValueError("Excerpt duration must not exceed 60 seconds.")

    name = str(output_name or "").strip()
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Excerpt output name must be a simple file name.")
    output_directory = Path(session_dir)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / (
        name if name.lower().endswith(".wav") else f"{name}.wav"
    )
    def seconds(value: int) -> str:
        return f"{value / 1000:.3f}".rstrip("0").rstrip(".")
    command = [
        ffmpeg_executable,
        "-i",
        str(source_path),
        "-ss",
        seconds(start),
        "-t",
        seconds(duration),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-af",
        "aresample,loudnorm",
        "-y",
        str(output_path),
    ]
    try:
        if cancel_event is not None and run_func is subprocess.run:
            result = run_cancellable(
                command,
                cancel_event=cancel_event,
                check=True,
                capture_output=True,
            )
        else:
            result = run_func(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        raise ExternalToolError(
            f"FFmpeg failed to extract audio excerpt: {safe_decode(getattr(error, 'stderr', None))}"
        ) from error
    except OSError as error:
        raise ExternalToolError(f"FFmpeg could not be started: {error}") from error
    if getattr(result, "stderr", None):
        logger.debug("FFmpeg audio excerpt: %s", safe_decode(result.stderr))
    return str(output_path)


def transcribe_source_file_with_metadata(
    session_dir: str | os.PathLike[str],
    source_file: str | os.PathLike[str],
    settings: dict[str, Any],
    *,
    ffmpeg_executable: str = "ffmpeg",
    crispasr_executable: str = "",
    run_func: Callable[..., Any] = subprocess.run,
    progress_callback: Callable[[float, str | None], None] | None = None,
    cancel_event: threading.Event | None = None,
    cloud_request_func: Callable[..., Any] | None = None,
    cloud_session: Any | None = None,
    source_is_normalized: bool = False,
    **_legacy_kwargs,
) -> CrispASRTranscriptionResult:
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    source_path = Path(source_file)
    source_name = source_path.stem
    if progress_callback is not None:
        progress_callback(0.0, "Normalizing source audio")
    if source_is_normalized:
        if source_path.suffix.lower() != ".wav" or not source_path.is_file():
            raise ValueError("A pre-normalized transcription source must be a WAV file.")
        audio_path = str(source_path)
    else:
        audio_path = extract_audio(
            source_path,
            session_path,
            source_name,
            ffmpeg_executable=ffmpeg_executable,
            run_func=run_func,
            cancel_event=cancel_event,
        )
    if progress_callback is not None:
        progress_callback(0.18, "Source audio normalized")
    if is_audio_source(str(source_path)):
        logger.info("Normalized audio source for CrispASR: %s", audio_path)
    if progress_callback is not None:
        progress_callback(0.22, "Running speech recognition")
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("Transcription was canceled.")
    configured_engine = str(
        settings.get("stt_engine") or settings.get("stt_backend") or ""
    ).strip()
    # A request/session alias keeps compatibility with callers that already
    # inject a transport function while making the cloud boundary explicit.
    cloud_request_func = cloud_request_func or _legacy_kwargs.pop("request_func", None)
    cloud_session = cloud_session or _legacy_kwargs.pop("session", None)
    if cloud_stt.is_cloud_stt_engine(configured_engine):
        result = cloud_stt.transcribe(
            audio_path,
            session_dir=session_path,
            output_name=source_name,
            settings=settings,
            request_func=cloud_request_func,
            session=cloud_session,
        )
    else:
        result = transcribe(
            audio_path,
            session_dir=session_path,
            output_name=source_name,
            settings=settings,
            executable=crispasr_executable,
            run_func=run_func,
            cancel_event=cancel_event,
        )
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("Transcription was canceled.")
    if progress_callback is not None:
        progress_callback(0.82, "Speech recognition complete; composing subtitles")
    Path(result.srt_path).write_text(
        compose_from_transcript_json(result.word_timestamps_path, settings),
        encoding="utf-8",
    )
    processed = postprocess_transcribed_srt(result.srt_path)
    if progress_callback is not None:
        progress_callback(1.0, "Subtitle timing and word metadata ready")
    return CrispASRTranscriptionResult(
        srt_path=processed,
        word_timestamps_path=result.word_timestamps_path,
        engine=result.engine,
        compute_backend=result.compute_backend,
    )


def transcribe_source_file(*args, **kwargs) -> str:
    return transcribe_source_file_with_metadata(*args, **kwargs).srt_path


def postprocess_transcribed_srt(
    srt_path: str | os.PathLike[str], *, merge_threshold: int = 250
) -> str:
    """Normalize cue numbering without applying TTS-oriented merge heuristics.

    ``merge_threshold`` remains accepted for compatibility with callers from the
    Qt application. CrispASR word timings are composed directly into final
    reading-oriented cues, so merging them afterwards could violate line and
    reading-speed limits.
    """
    path = Path(srt_path)
    content = path.read_text(encoding="utf-8-sig")
    renumbered = renumber_subtitles(content)
    if renumbered == content:
        return str(path)
    output_path = path.with_name(f"{path.stem}_normalized{path.suffix}")
    output_path.write_text(renumbered, encoding="utf-8")
    return str(output_path)


def transcribe_video_file(session_dir, video_file, settings, **kwargs) -> str:
    return transcribe_source_file(session_dir, video_file, settings, **kwargs)
