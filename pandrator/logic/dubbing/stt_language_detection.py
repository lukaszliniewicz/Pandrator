"""Bounded multilingual Whisper Tiny language detection, without transcription."""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandrator.logic.dubbing.crispasr_qwen_assets as crispasr_qwen_assets

from ..cancellable_process import ProcessCancelled
from .stt_backends import CrispASRRuntimeStatus, probe_crispasr_runtime
from .stt_languages import WHISPER_LARGE_V3_LANGUAGE_CODES

DETECTOR_MIN_RUNTIME_VERSION = (0, 8, 40)
DETECTOR_TIMEOUT_SECONDS = 120.0
DETECTOR_MAX_SAMPLE_MS = 15_000
DETECTOR_ASSET_KEY = "whisper_tiny_language_detector"
_REPORT = re.compile(
    r"^whisper_full_with_state: auto-detected language: ([a-z]+) \(p = ([0-9]+(?:\.[0-9]+)?)\)\s*$",
    re.MULTILINE,
)


class LanguageDetectionError(ValueError):
    """Detector output is unavailable or unsafe to use for routing."""


@dataclass(frozen=True)
class LanguageDetectionResult:
    language: str
    confidence: float


def parse_language_detection(output: str) -> LanguageDetectionResult:
    reports = _REPORT.findall(str(output or ""))
    diagnostics = [line for line in str(output or "").splitlines() if "auto-detected language:" in line]
    if len(reports) != 1 or len(diagnostics) != 1:
        raise LanguageDetectionError("language_detection_missing_malformed_or_duplicate_report")
    language, raw_confidence = reports[0]
    confidence = float(raw_confidence)
    if language not in WHISPER_LARGE_V3_LANGUAGE_CODES:
        raise LanguageDetectionError("language_detection_unknown_language")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise LanguageDetectionError("language_detection_invalid_confidence")
    return LanguageDetectionResult(language, confidence)


def _check_cancelled(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise ProcessCancelled("Language detection was canceled.")


def detect_source_language(
    normalized_audio: str | Path,
    *,
    settings: dict[str, Any],
    excerpt_func: Callable[..., str],
    cancel_event: threading.Event | None = None,
    ffmpeg_executable: str = "ffmpeg",
    crispasr_executable: str = "",
    run_func: Callable[..., Any] = subprocess.run,
    runtime_status: CrispASRRuntimeStatus | None = None,
) -> LanguageDetectionResult:
    """Use only the first <=15 seconds, clean scratch even after cancellation.

    Pinned upstream v0.8.40 examples/cli/cli.cpp owns the legacy flags:
    --detect-language exits after detection and --no-gpu disables GPU.
    https://github.com/CrispStrobe/CrispASR/blob/v0.8.40/examples/cli/cli.cpp
    """
    _check_cancelled(cancel_event)
    runtime = runtime_status or probe_crispasr_runtime(
        run_func=run_func,
        environ={**os.environ, "CRISPASR_EXECUTABLE": crispasr_executable}
        if crispasr_executable else None,
    )
    version = re.match(r"v?(\d+)\.(\d+)\.(\d+)", runtime.version)
    if not runtime.installed or version is None or tuple(map(int, version.groups())) < DETECTOR_MIN_RUNTIME_VERSION:
        raise LanguageDetectionError("language_detection_requires_crispasr_0.8.40")
    try:
        with wave.open(str(normalized_audio), "rb") as source:
            sample_ms = min(DETECTOR_MAX_SAMPLE_MS, int(source.getnframes() * 1000 / source.getframerate()))
    except (OSError, wave.Error, ZeroDivisionError) as error:
        raise LanguageDetectionError("language_detection_invalid_normalized_audio") from error
    if sample_ms < 1:
        raise LanguageDetectionError("language_detection_empty_audio")
    # The caller supplies its shared extraction helper to avoid an import cycle.
    from .qwen_asr import _run_tool

    with tempfile.TemporaryDirectory(prefix="pandrator-language-detection-") as scratch:
        _check_cancelled(cancel_event)
        excerpt = excerpt_func(
            normalized_audio, scratch, "sample", 0, sample_ms,
            ffmpeg_executable=ffmpeg_executable, run_func=run_func,
            cancel_event=cancel_event,
        )
        _check_cancelled(cancel_event)
        model = crispasr_qwen_assets.ensure_asset(
            DETECTOR_ASSET_KEY, settings, cancel_event=cancel_event,
        )
        _check_cancelled(cancel_event)
        command = [
            crispasr_executable or runtime.executable, "-m", str(model),
            "-f", excerpt, "--detect-language", "--no-gpu",
        ]
        try:
            result = _run_tool(
                command, run_func=run_func, cancel_event=cancel_event,
                timeout=DETECTOR_TIMEOUT_SECONDS, backend="cpu",
            )
        except ProcessCancelled:
            raise
        except Exception as error:
            raise LanguageDetectionError(f"language_detection_failed:{type(error).__name__}") from error
        _check_cancelled(cancel_event)
        def decode(value: Any) -> str:
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        return parse_language_detection("\n".join((decode(result.stdout), decode(result.stderr))))
