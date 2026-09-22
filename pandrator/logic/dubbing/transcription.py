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


def _vocal_isolation_choice(settings: dict) -> str:
    try:
        from .qwen_asr import normalize_vocal_isolation
    except ImportError:
        raw = str(settings.get("transcription_vocal_isolation") or "off").strip().lower()
        return "off" if raw in {"", "off", "none", "disabled"} else raw
    return normalize_vocal_isolation(settings.get("transcription_vocal_isolation"))


def _wav_frame_count(path: str | os.PathLike[str]) -> int | None:
    try:
        import wave as _wave

        with _wave.open(str(path), "rb") as source:
            return int(source.getnframes())
    except Exception:
        return None


def _forward_isolation_progress(
    progress: Callable[[float, str | None], None] | None,
) -> Callable[[str, int, int], None] | None:
    """Adapt the shared 3-arg ``progress(stage, done, total)`` to 0..1."""

    if progress is None:
        return None
    # Sub-ranges inside the caller's isolation window.
    ranges = {
        "download": (0.0, 0.4),
        "verify": (0.4, 0.5),
        "reuse": (0.4, 0.5),
        "normalize": (0.5, 0.6),
        "isolate": (0.6, 0.9),
        "install": (0.9, 0.95),
        "clean": (0.6, 0.9),
    }

    def forward(stage: str, done: int, total: int) -> None:
        low, high = ranges.get(str(stage), (0.0, 1.0))
        try:
            fraction = max(0.0, min(1.0, float(done) / max(1, int(total))))
        except (TypeError, ValueError):
            fraction = 1.0
        progress(low + (high - low) * fraction, f"Isolating vocals: {stage}")

    return forward


def apply_vocal_isolation(
    audio_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    source_name: str,
    settings: dict,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float, str | None], None] | None = None,
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
) -> tuple[str, dict | None]:
    """Optionally isolate vocals for transcription only.

    Returns ``(transcribe_path, provenance)``. The original normalized WAV
    is always retained for playback/export; only the returned 16 kHz mono
    derivative is fed to STT/alignment. Duration is verified within the
    shared ``min(50 ms, 1024 samples)`` tolerance. An explicitly selected
    method never silently skips: missing helpers or failures raise.
    """

    from .qwen_asr import QwenASRError

    choice = _vocal_isolation_choice(settings)
    if choice == "off":
        return str(audio_path), None
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("Transcription was canceled.")
    try:
        from ..audio_cpp_processing import isolate_vocals
    except ImportError as error:
        raise QwenASRError(
            "transcription_vocal_isolation="
            f"{choice} was explicitly selected, but the shared native helper "
            "(pandrator.logic.audio_cpp_processing.isolate_vocals) is not "
            "installed yet. Refusing to silently transcribe the unprocessed mix."
        ) from error
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    raw_output = session_path / f"{source_name}_vocals_raw.wav"
    destination = session_path / f"{source_name}_vocals.wav"

    try:
        provenance = isolate_vocals(
            str(audio_path),
            str(raw_output),
            dict(settings),
            cancel_event=cancel_event,
            progress=_forward_isolation_progress(
                (lambda v, d=None: progress(0.18 + 0.04 * v, d))
                if progress is not None
                else None
            ),
            run_func=run_func,
        )
    except ProcessCancelled:
        raise
    except QwenASRError:
        raise
    except Exception as error:
        raise QwenASRError(
            f"Vocal isolation ({choice}) failed: {error}. The unprocessed mix "
            "was not substituted."
        ) from error
    if not isinstance(provenance, dict) or provenance.get("status") != "isolated":
        raise QwenASRError(
            f"Vocal isolation ({choice}) did not report isolated status "
            f"({provenance}); refusing to silently transcribe the unprocessed mix."
        )
    if not raw_output.is_file():
        raise QwenASRError(
            f"Vocal isolation ({choice}) produced no output; refusing to "
            "silently transcribe the unprocessed mix."
        )
    # The separator runs at 44.1 kHz stereo; normalize the derivative back
    # to the 16 kHz mono transcription contract (same shape as extract_audio).
    command = [
        ffmpeg_executable,
        "-i",
        str(raw_output),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-y",
        str(destination),
    ]
    try:
        if cancel_event is not None and run_func is subprocess.run:
            run_cancellable(
                command, cancel_event=cancel_event, check=True, capture_output=True
            )
        else:
            run_func(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        raise QwenASRError(
            "FFmpeg failed to normalize the isolated vocals: "
            f"{safe_decode(getattr(error, 'stderr', None))}"
        ) from error
    if progress is not None:
        progress(0.22, "Vocals isolated for transcription")
    raw_output.unlink(missing_ok=True)
    expected = _wav_frame_count(audio_path)
    actual = _wav_frame_count(destination)
    if expected is not None and actual is not None:
        tolerance = min(round(16000 * 0.05), 1024)
        if abs(expected - actual) > tolerance:
            raise QwenASRError(
                f"Vocal isolation ({choice}) shifted the audio timeline "
                f"({expected} vs {actual} frames, tolerance {tolerance}); "
                "original retained, derivative rejected."
            )
    record = {"method": choice, "derivative": str(destination)}
    record.update(provenance)
    return str(destination), record


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


def _record_isolation_provenance(
    words_path: str, provenance: dict, original_audio: str
) -> None:
    """Attach vocal-isolation provenance to the word-timestamp JSON.

    Best-effort metadata only: a write failure is logged, never a silent
    preprocessing skip (isolation itself already ran or raised above).
    """

    import json as _json

    try:
        path = Path(words_path)
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        logger.warning("Could not record vocal-isolation provenance: %s", error)
        return
    entry = {
        "transcription_vocal_isolation": provenance.get("method"),
        "vocal_isolation_model": provenance.get("model"),
        "vocal_isolation_status": provenance.get("status"),
        "original_audio_retained": str(original_audio),
    }
    try:
        if isinstance(payload, dict) and isinstance(
            payload.get("metadata"), dict
        ):
            payload["metadata"]["vocal_isolation"] = entry
        elif isinstance(payload, dict):
            payload["pandrator_vocal_isolation"] = entry
        else:
            return
        path.write_text(
            _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as error:
        logger.warning("Could not record vocal-isolation provenance: %s", error)


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
    from .stt_backends import normalize_stt_backend

    configured_engine = normalize_stt_backend(
        settings.get("stt_engine") or settings.get("stt_backend") or ""
    )
    if configured_engine == "qwen3":
        from . import qwen_asr

        qwen_asr.validate_transcription_settings(
            settings, require_word_timestamps=True
        )

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
        logger.info("Normalized audio source for transcription: %s", audio_path)
    # Optional vocal isolation runs on a derivative only; the normalized
    # original above is retained for playback/export.
    transcribe_path, isolation_provenance = apply_vocal_isolation(
        audio_path,
        session_path,
        source_name,
        settings,
        cancel_event=cancel_event,
        progress=progress_callback,
        ffmpeg_executable=ffmpeg_executable,
        run_func=run_func,
    )
    if progress_callback is not None:
        progress_callback(0.22, "Running speech recognition")
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("Transcription was canceled.")
    # A request/session alias keeps compatibility with callers that already
    # inject a transport function while making the cloud boundary explicit.
    cloud_request_func = cloud_request_func or _legacy_kwargs.pop("request_func", None)
    cloud_session = cloud_session or _legacy_kwargs.pop("session", None)
    if cloud_stt.is_cloud_stt_engine(configured_engine):
        result = cloud_stt.transcribe(
            transcribe_path,
            session_dir=session_path,
            output_name=source_name,
            settings=settings,
            request_func=cloud_request_func,
            session=cloud_session,
        )
    elif configured_engine == "qwen3":
        # qwen_asr.transcribe owns STT/alignment only; isolation already ran
        # above, so pass it off to avoid double-processing.
        qwen_options = {
            **settings,
            "transcription_vocal_isolation": "off",
        }
        result = qwen_asr.transcribe(
            transcribe_path,
            session_dir=session_path,
            output_name=source_name,
            settings=qwen_options,
            run_func=run_func,
            cancel_event=cancel_event,
            progress_callback=(
                (lambda v, d=None: progress_callback(0.22 + 0.60 * v, d))
                if progress_callback is not None
                else None
            ),
        )
    else:
        result = transcribe(
            transcribe_path,
            session_dir=session_path,
            output_name=source_name,
            settings=settings,
            executable=crispasr_executable,
            run_func=run_func,
            cancel_event=cancel_event,
        )
    if isolation_provenance is not None:
        _record_isolation_provenance(
            result.word_timestamps_path, isolation_provenance, audio_path
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
