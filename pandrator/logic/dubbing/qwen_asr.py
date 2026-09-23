"""Qwen3 ASR transcription via audio.cpp.

Recognizer, not aligner. Transcript-only Qwen3 ASR covers 30 languages;
precise Qwen word timestamps additionally require the validated *source*
language to be in the 11-language forced-aligner coverage (existing
``qwen_alignment`` module). Never the translation target, never guessed
from ``auto``, never fabricated.

CLI contract verified against ``0xShug0/audio.cpp/docs/models/qwen3.md``:
task ``asr``, family ``qwen3_asr``, offline mode only. Only the flags,
session options, and request option documented there are emitted.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..audio_cpp_execution import native_audio_cpp_guard
from ..cancellable_process import ProcessCancelled, run_cancellable
from . import qwen_alignment
from .stt_languages import PARAKEET_V3_LANGUAGE_CODES, normalize_stt_language

logger = logging.getLogger(__name__)

STT_ENGINE_QWEN3 = "qwen3"
STT_ENGINE_QWEN3_ALIASES = frozenset(
    {"qwen3", "qwen", "qwen3_asr", "qwen3-asr", "qwen_3", "qwen_3_asr"}
)

QWEN3_ASR_MODELS = ("qwen3_asr_0_6b", "qwen3_asr_1_7b")
DEFAULT_QWEN3_ASR_MODEL = "qwen3_asr_0_6b"

# Source: Qwen/Qwen3-ASR model card (Qwen3-ASR-0.6B / 1.7B): 30 languages.
# Deliberately distinct from the forced aligner's 11-language coverage in
# qwen_alignment.LANGUAGES. Do NOT merge these lists.
QWEN3_ASR_LANGUAGE_CODES = (
    "zh",
    "en",
    "yue",
    "ar",
    "de",
    "fr",
    "es",
    "pt",
    "id",
    "it",
    "ko",
    "ru",
    "th",
    "vi",
    "ja",
    "tr",
    "hi",
    "ms",
    "nl",
    "sv",
    "da",
    "fi",
    "pl",
    "cs",
    "fil",
    "fa",
    "el",
    "hu",
    "mk",
    "ro",
)

# Full English labels for the audio.cpp ``--language`` hint. The docs use
# ``--language English`` style values, not codes.
QWEN3_ASR_LANGUAGE_LABELS = {
    "zh": "Chinese",
    "en": "English",
    "yue": "Cantonese",
    "ar": "Arabic",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "id": "Indonesian",
    "it": "Italian",
    "ko": "Korean",
    "ru": "Russian",
    "th": "Thai",
    "vi": "Vietnamese",
    "ja": "Japanese",
    "tr": "Turkish",
    "hi": "Hindi",
    "ms": "Malay",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "pl": "Polish",
    "cs": "Czech",
    "fil": "Filipino",
    "fa": "Persian",
    "el": "Greek",
    "hu": "Hungarian",
    "mk": "Macedonian",
    "ro": "Romanian",
}

QWEN3_CHUNK_MODES = ("auto", "fixed", "vad", "none")
VOCAL_ISOLATION_CHOICES = ("off", "bs_roformer", "mel_band_roformer")

# Timing pipeline constraint (distinct from recognizer coverage above).
# Precise word timing is available either natively (validated source
# language in the 11-language Qwen forced aligner) or via the existing
# Canary CTC fallback where its coverage intersects the ASR 30.
# Union, kept in ASR30 order so frontend can render one honest list.
_TIMED_VIA_QWEN = tuple(qwen_alignment.LANGUAGES)
_TIMED_VIA_CANARY = tuple(
    code for code in PARAKEET_V3_LANGUAGE_CODES if code in QWEN3_ASR_LANGUAGE_CODES
)
TIMED_SUPPORTED_LANGUAGES = tuple(
    code
    for code in QWEN3_ASR_LANGUAGE_CODES
    if code in qwen_alignment.LANGUAGES or code in _TIMED_VIA_CANARY
)
TRANSCRIPT_ONLY_LANGUAGES = tuple(
    code for code in QWEN3_ASR_LANGUAGE_CODES if code not in TIMED_SUPPORTED_LANGUAGES
)
# Word timestamps always need a validated explicit source language; ``auto``
# without a recognizer-reported detection is refused preflight, before any
# model download. Never the translation target.
REQUIRES_EXPLICIT_LANGUAGE_FOR_TIMESTAMPS = True
TIMING_FALLBACK = "canary_ctc_where_supported"


def recognizer_languages() -> tuple[str, ...]:
    """All 30 transcript-only recognizer codes."""

    return QWEN3_ASR_LANGUAGE_CODES


def alignment_languages() -> tuple[str, ...]:
    """The 11 native Qwen forced-aligner codes (source language only)."""

    return tuple(qwen_alignment.LANGUAGES)


def timed_supported_languages() -> tuple[str, ...]:
    """Timed pipeline codes: native Qwen union Canary fallback, in ASR30."""

    return TIMED_SUPPORTED_LANGUAGES


def transcript_only_languages() -> tuple[str, ...]:
    """ASR30 codes with transcript-only output (no precise word timing)."""

    return TRANSCRIPT_ONLY_LANGUAGES


def timing_plan_for_language(code: str) -> str:
    """Return ``qwen3_forced_aligner``, ``canary_ctc_fallback``, or ``unsupported``."""

    normalized = str(code or "").strip().lower()
    if normalized in qwen_alignment.LANGUAGES:
        return "qwen3_forced_aligner"
    if normalized in _TIMED_VIA_CANARY:
        return "canary_ctc_fallback"
    return "unsupported"

_BACKENDS = ("cpu", "best", "cuda", "vulkan", "hip", "metal")


class QwenASRError(RuntimeError):
    """Raised when Qwen3 ASR cannot safely produce a transcript."""


def is_qwen3_engine(raw_value: str | None) -> bool:
    return (
        str(raw_value or "").strip().lower().replace("-", "_").replace(" ", "_")
        in STT_ENGINE_QWEN3_ALIASES
    )


def normalize_qwen3_engine(raw_value: str | None) -> str:
    normalized = (
        str(raw_value or "").strip().lower().replace("-", "_").replace(" ", "_")
    )
    return STT_ENGINE_QWEN3 if normalized in STT_ENGINE_QWEN3_ALIASES else normalized


def normalize_qwen_asr_model(raw_value: str | None) -> str:
    normalized = (
        str(raw_value or "").strip().lower().replace("-", "_").replace(" ", "_")
    )
    if not normalized:
        return DEFAULT_QWEN3_ASR_MODEL
    # Accept a trailing ``_hf`` native-checkpoint marker without inventing
    # new model identities.
    if normalized.endswith("_hf"):
        normalized = normalized[: -len("_hf")]
    if normalized in QWEN3_ASR_MODELS:
        return normalized
    raise QwenASRError(
        f"Unknown Qwen3 ASR model {raw_value!r}; use one of "
        f"{', '.join(QWEN3_ASR_MODELS)}."
    )


def normalize_vocal_isolation(raw_value: str | None) -> str:
    normalized = (
        str(raw_value or "").strip().lower().replace("-", "_").replace(" ", "_")
    )
    if normalized in {"", "off", "none", "disabled"}:
        return "off"
    if normalized in {"bs-roformer", "bs_roformer", "bs"}:
        return "bs_roformer"
    if normalized in {
        "mel-band-roformer",
        "mel_band_roformer",
        "melbandroformer",
        "mel_roformer",
    }:
        return "mel_band_roformer"
    raise QwenASRError(
        f"Unknown transcription_vocal_isolation {raw_value!r}; use one of "
        f"{', '.join(VOCAL_ISOLATION_CHOICES)}."
    )


def normalize_qwen_asr_language(language: str | None) -> str:
    """Normalize to a Qwen ASR code, preserving ``auto`` for detection."""

    raw = str(language or "").strip()
    if not raw or raw.lower() in {"auto", "automatic", "detect", "und", "unknown"}:
        return "auto"
    # ``fil`` (Filipino) is a distinct Qwen code; the generic normalizer
    # would fold related tags, so guard it first.
    if raw.strip().lower().replace("_", "-") in {"fil", "filipino"}:
        return "fil"
    normalized = normalize_stt_language(raw)
    base = normalized.split("-", 1)[0]
    # The shared normalizer maps Chinese display names to ``zh-cn``; Qwen
    # coverage is expressed as bare ``zh``.
    if base in {"zh-cn", "zh-tw", "zh_cn", "zh_tw"}:
        return "zh"
    if base in {"cantonese"}:
        return "yue"
    code = base
    if code not in QWEN3_ASR_LANGUAGE_CODES:
        raise QwenASRError(
            f"Unsupported language '{code}' for qwen3; supported languages are: "
            f"{', '.join(QWEN3_ASR_LANGUAGE_CODES)}."
        )
    return code


def asr_language_label(code: str) -> str:
    return QWEN3_ASR_LANGUAGE_LABELS.get(code, "")


def qwen_word_alignment_supported(code: str) -> bool:
    """Word timestamps need the *source* language in aligner coverage."""

    return code in qwen_alignment.LANGUAGES


def qwen_alignment_language_problem(settings: dict, text: str = "") -> str | None:
    """Reject Qwen word alignment for unsupported *source* languages.

    The translation target is deliberately never consulted here.
    """

    code = qwen_alignment.source_language(settings, text)
    if code in {"", "auto", "und", "unknown"}:
        return (
            "unvalidated_qwen_alignment_language:auto: Qwen word timestamps need "
            "a validated source/audio language. Set an explicit source language "
            "in the 11-language aligner coverage (zh, en, yue, fr, de, it, ja, "
            "ko, pt, ru, es); automatic detection was not reported by the "
            "recognizer for this run."
        )
    if code not in qwen_alignment.LANGUAGES:
        return (
            f"unsupported_qwen_alignment_language:{code}: Qwen word timestamps "
            f"support only {', '.join(qwen_alignment.LANGUAGES)}. The translation "
            "target is not used for alignment. Transcript-only Qwen ASR remains "
            "available; use an explicitly compatible CTC/Whisper alignment or "
            "another engine for precise word timing."
        )
    return None


def resolve_executable(settings: dict) -> Path:
    """Reuse the audio.cpp CLI resolution owned by the aligner module."""

    return qwen_alignment.resolve_executable(settings)


def ensure_asr_model(
    model_id: str,
    settings: dict,
    cancel_event: threading.Event | None = None,
    progress: Callable[..., None] | None = None,
) -> Path:
    """Resolve the recognizer checkpoint through the shared native helper.

    The shared-tools agent owns
    ``pandrator.logic.audio_cpp_assets.ensure_model``; this module never
    duplicates its pins, cache, or download logic.
    """

    normalized = normalize_qwen_asr_model(model_id)
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("Qwen3 ASR was canceled.")
    try:
        from ..audio_cpp_assets import ensure_model as shared_ensure_model
    except ImportError as error:
        raise QwenASRError(
            "Qwen3 ASR model provisioning (pandrator.logic.audio_cpp_assets."
            "ensure_model) is not installed yet; the shared native helper owns "
            f"pins/cache for {normalized}. No model was downloaded."
        ) from error
    try:
        path = shared_ensure_model(
            normalized, dict(settings or {}), cancel_event, progress
        )
    except ProcessCancelled:
        raise
    except QwenASRError:
        raise
    except Exception as error:
        raise QwenASRError(
            f"Qwen3 ASR model {normalized} could not be provisioned: {error}"
        ) from error
    resolved = Path(str(path))
    if not resolved.is_file():
        raise QwenASRError(
            f"Qwen3 ASR model {normalized} resolved to a missing file: {resolved}."
        )
    return resolved.resolve()


def ensure_aligner_model(
    settings: dict, cancel_event: threading.Event | None = None
) -> Path:
    return qwen_alignment.ensure_model(settings, cancel_event)


def _setting(settings: dict, name: str, default: Any) -> Any:
    value = settings.get(name)
    return default if value is None or value == "" else value


def _normalize_backend(settings: dict) -> str:
    backend = str(
        _setting(settings, "qwen_asr_backend", None)
        or _setting(settings, "stt_compute_backend", "auto")
    ).strip().lower()
    backend = "best" if backend == "auto" else backend
    if backend not in _BACKENDS:
        raise QwenASRError(f"Unsupported audio.cpp ASR backend: {backend}")
    return backend


def _normalize_chunk_seconds(settings: dict) -> float:
    # Bounded Pandrator pre-chunks (default 30 s) keep each CLI call inside
    # a token budget the 512 default can actually serve. 0 keeps a single
    # pass with native model-session chunking for callers that opt in.
    raw = _setting(settings, "qwen_asr_chunk_seconds", 30)
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise QwenASRError("qwen_asr_chunk_seconds must be 0 or 10-120.") from error
    if not math.isfinite(value) or value < 0:
        raise QwenASRError("qwen_asr_chunk_seconds must be 0 or 10-120.")
    if value == 0:
        return 0.0
    if not 10 <= value <= 120:
        raise QwenASRError("qwen_asr_chunk_seconds must be 0 or 10-120.")
    return value


def _normalize_max_tokens(settings: dict) -> int:
    raw = _setting(settings, "qwen_asr_max_tokens", 512)
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise QwenASRError("qwen_asr_max_tokens must be 32-4096.") from error
    if not 32 <= value <= 4096:
        raise QwenASRError("qwen_asr_max_tokens must be 32-4096.")
    return value


def _normalize_timeout_seconds(settings: dict) -> float:
    # Finite watchdog per native call. 3600 s matches the shared
    # audio_cpp_timeout_seconds convention; bounded chunks finish far sooner.
    raw = _setting(settings, "qwen_asr_timeout_seconds", 3600)
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise QwenASRError("qwen_asr_timeout_seconds must be 60-14400.") from error
    if not math.isfinite(value) or not 60 <= value <= 14400:
        raise QwenASRError("qwen_asr_timeout_seconds must be 60-14400.")
    return value


def _normalize_chunk_mode(settings: dict) -> str:
    mode = str(_setting(settings, "qwen_asr_chunk_mode", "auto")).strip().lower()
    if mode not in QWEN3_CHUNK_MODES:
        raise QwenASRError(
            f"Unknown qwen_asr_chunk_mode {mode!r}; use one of "
            f"{', '.join(QWEN3_CHUNK_MODES)}."
        )
    return mode


def resolve_chunk_mode(settings: dict, chunk_seconds: float) -> str:
    """Resolve the ``--audio-chunk-mode`` flag to emit.

    ``auto`` is context-sensitive, never passed through blindly: bounded
    Pandrator outer chunks (always 10-120 s) resolve to ``none`` so the
    model performs no internal chunking and never touches its unbundled
    VAD path; a single-pass run (``qwen_asr_chunk_seconds=0``) resolves to
    ``fixed`` for model-side fixed chunks, likewise VAD-free. Explicit
    ``fixed``/``vad``/``none`` pass through unchanged. Explicit ``vad``
    needs an operator-provided Silero VAD model (the 0.8.1 runtime does not
    bundle ``assets/framework/models/silero_vad``); its native failure is
    surfaced verbatim (sanitized) rather than masked.
    """

    explicit = _normalize_chunk_mode(settings)
    if explicit != "auto":
        return explicit
    return "none" if chunk_seconds > 0 else "fixed"


def _required_min_tokens(chunk_seconds: float, max_tokens: int) -> int:
    # ~8 decode tokens per second covers dense speech; the 512 default
    # serves the 30 s bounded default. Larger Pandrator chunks need a
    # larger budget, otherwise a long chunk risks silent truncation.
    effective = chunk_seconds if chunk_seconds > 0 else 30.0
    return min(4096, max(512, math.ceil(effective * 8)))


def _estimate_tokens(text: str) -> int:
    # Conservative multilingual estimate: CJK chars carry ~1 token each,
    # alphabetic text ~1 token per 4 chars. Take the max so neither
    # family underestimates toward a truncation cliff.
    stripped = text.strip()
    if not stripped:
        return 0
    return max(math.ceil(len(stripped) / 4), len(stripped))


_TERMINAL_PUNCT = frozenset({".", "!", "?", "。", "！", "？", "…", "♪"})


def _check_truncation(
    *, chunk_index: int, is_final: bool, text: str, max_tokens: int
) -> None:
    """Refuse likely token-truncated chunks instead of truncating silently."""

    estimated = _estimate_tokens(text)
    if estimated < math.ceil(max_tokens * 0.95):
        return
    ends_cleanly = text.rstrip().endswith(tuple(_TERMINAL_PUNCT))
    if is_final and ends_cleanly:
        return
    raise QwenASRError(
        f"Qwen3 ASR chunk {chunk_index} likely hit its {max_tokens}-token budget "
        f"(~{estimated} estimated tokens) without a clean ending. No truncated "
        "transcript was kept. Raise qwen_asr_max_tokens or lower "
        "qwen_asr_chunk_seconds and retry."
    )


def build_asr_command(
    audio_path: str | os.PathLike[str],
    transcript_path: str | os.PathLike[str],
    settings: dict,
    *,
    executable: str | os.PathLike[str] | None = None,
    model_path: str | os.PathLike[str] | None = None,
    words_path: str | os.PathLike[str] | None = None,
    aligner_model_path: str | os.PathLike[str] | None = None,
    language_code: str = "auto",
) -> list[str]:
    """Build one offline ``asr`` invocation using only documented flags."""

    if model_path is None:
        raise QwenASRError("Qwen3 ASR requires a provisioned model path.")
    if executable is None:
        cli = str(resolve_executable(settings))
    else:
        cli = str(executable)
    command = [
        cli,
        "--task",
        "asr",
        "--family",
        "qwen3_asr",
        "--model",
        str(model_path),
        "--backend",
        _normalize_backend(settings),
        "--device",
        str(max(0, int(_setting(settings, "stt_compute_device", 0) or 0))),
        "--threads",
        str(max(1, int(_setting(settings, "stt_threads", 0) or 4))),
        "--audio",
        str(audio_path),
        "--text",
        str(_setting(settings, "qwen_asr_prompt", "")),
        "--max-tokens",
        str(_normalize_max_tokens(settings)),
        "--audio-chunk-mode",
        resolve_chunk_mode(settings, _normalize_chunk_seconds(settings)),
        "--text-out",
        str(transcript_path),
    ]
    if language_code != "auto":
        label = asr_language_label(language_code)
        if not label:
            raise QwenASRError(
                f"Unsupported language '{language_code}' for qwen3."
            )
        command.extend(("--language", label))
    if words_path is not None:
        if aligner_model_path is None:
            raise QwenASRError(
                "Qwen word timestamps require the forced aligner model path "
                "(qwen3_asr.forced_aligner_model_path)."
            )
        command.extend(("--words-out", str(words_path)))
        command.extend(
            (
                "--session-option",
                f"qwen3_asr.forced_aligner_model_path={aligner_model_path}",
            )
        )
        vad_override = str(settings.get("qwen_asr_vad_model_path") or "").strip()
        if vad_override:
            command.extend(
                ("--session-option", f"qwen3_asr.vad_model_path={vad_override}")
            )
        if bool(settings.get("qwen_asr_clamp_timestamps", False)):
            command.extend(
                ("--request-option", "clamp_timestamps_to_audio=true")
            )
    return command


@dataclass(frozen=True)
class QwenASRChunk:
    index: int
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int


def _wav_info(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            params = source.getparams()
            if (
                params.nchannels != 1
                or params.sampwidth != 2
                or params.framerate != 16000
                or params.comptype != "NONE"
            ):
                raise QwenASRError(
                    "Qwen3 ASR requires mono 16-bit 16 kHz PCM WAV audio."
                )
            if params.nframes <= 0:
                raise QwenASRError("Qwen3 ASR audio has no frames.")
            return params.nframes, params.framerate
    except (OSError, wave.Error) as error:
        raise QwenASRError(f"Invalid Qwen3 ASR WAV: {path}") from error


def plan_chunks(audio_path: str | os.PathLike[str], chunk_seconds: float) -> tuple[QwenASRChunk, ...]:
    """Plan exact, non-overlapping frame windows with retained offsets."""

    path = Path(audio_path)
    nframes, rate = _wav_info(path)
    if chunk_seconds <= 0 or nframes <= round(chunk_seconds * rate):
        return (
            QwenASRChunk(
                index=1,
                start_frame=0,
                end_frame=nframes,
                start_ms=0,
                end_ms=round(nframes * 1000 / rate),
            ),
        )
    window = max(1, round(chunk_seconds * rate))
    chunks: list[QwenASRChunk] = []
    start = 0
    while start < nframes:
        end = min(nframes, start + window)
        chunks.append(
            QwenASRChunk(
                index=len(chunks) + 1,
                start_frame=start,
                end_frame=end,
                start_ms=round(start * 1000 / rate),
                end_ms=round(end * 1000 / rate),
            )
        )
        start = end
    return tuple(chunks)


def _write_chunk(source: Path, destination: Path, chunk: QwenASRChunk) -> None:
    try:
        with wave.open(str(source), "rb") as origin:
            params = origin.getparams()
            origin.setpos(chunk.start_frame)
            frames = origin.readframes(chunk.end_frame - chunk.start_frame)
        with wave.open(str(destination), "wb") as target:
            target.setparams(params)
            target.writeframes(frames)
    except (OSError, wave.Error) as error:
        raise QwenASRError(f"Could not write Qwen3 ASR chunk {chunk.index}.") from error


def check_cancelled(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise ProcessCancelled("Qwen3 ASR was canceled.")


def _run_tool(
    command: list[str],
    *,
    run_func: Callable[..., Any],
    cancel_event: threading.Event | None,
    timeout: float | None = None,
) -> Any:
    """Run one bounded native call with cancel plus a finite watchdog.

    ``timeout`` (seconds) bounds the native child even when nobody cancels.
    A watchdog firing is reported as a timeout stage, distinct from user
    cancellation: caller-cancelled runs re-raise ``ProcessCancelled``.
    """

    check_cancelled(cancel_event)
    def run_native(event: threading.Event | None):
        # CTC fallback calls are guarded by their caller with the resolved
        # audio.cpp address; only actual audio.cpp commands have --family.
        with (
            native_audio_cpp_guard(command, event)
            if "--family" in command else nullcontext()
        ):
            return run_cancellable(
                command, cancel_event=event, check=True, capture_output=True
            )

    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as error:
            raise QwenASRError("Qwen3 ASR timeout must be finite seconds.") from error
        if not math.isfinite(timeout) or timeout <= 0:
            raise QwenASRError("Qwen3 ASR timeout must be finite seconds.")
    if timeout is None:
        if run_func is subprocess.run:
            return run_native(cancel_event)
        return run_func(command, check=True, capture_output=True)
    combined = threading.Event()
    stop = threading.Event()

    def _watch() -> None:
        start = time.monotonic()
        while not stop.is_set():
            if cancel_event is not None and cancel_event.is_set():
                combined.set()
                return
            if time.monotonic() - start >= timeout:
                combined.set()
                return
            stop.wait(0.05)

    watcher = threading.Thread(target=_watch, name="qwen-asr-timeout", daemon=True)
    watcher.start()
    try:
        if run_func is subprocess.run:
            return run_native(combined)
        return run_func(command, check=True, capture_output=True)
    except ProcessCancelled:
        if cancel_event is not None and cancel_event.is_set():
            raise
        raise QwenASRError(
            f"Qwen3 ASR timed out after {timeout:g} seconds; the native child "
            "was stopped. No partial transcript was kept."
        ) from None
    finally:
        stop.set()
        watcher.join(timeout=2.0)


def native_error_tail(error: BaseException, *, max_chars: int = 600) -> str:
    """Extract a sanitized native stderr tail from a failed CLI invocation.

    Only the last stderr lines are kept (stage + reason, e.g. the missing
    Silero VAD path). The full argv is deliberately excluded: it carries
    local absolute paths and would bury the reason. Transcript content is
    never part of this surface (stderr only).
    """

    raw = getattr(error, "stderr", None)
    if raw is None:
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tail = "; ".join(lines[-5:])
    return tail[:max_chars].strip()


def parse_transcript_file(path: Path) -> tuple[str, str]:
    """Return (text, detected_language). Never invent either."""

    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError as error:
        raise QwenASRError(f"Qwen3 ASR produced no transcript file: {path}") from error
    # The CLI prints the transcript to stdout and writes --text-out. Some
    # builds prefix a language tag (``[en] text`` or ``<|en|>text``). Parse
    # it only when explicitly present; otherwise the language is unvalidated.
    detected = ""
    candidate = text
    for pattern in ("[", "<|"):
        if candidate.startswith(pattern):
            closer = "]" if pattern == "[" else "|>"
            end = candidate.find(closer)
            if 0 < end <= 8:
                tag = candidate[len(pattern) : end].strip().lower()
                if tag in set(QWEN3_ASR_LANGUAGE_CODES) | {"filipino"}:
                    detected = "fil" if tag == "filipino" else tag
                    candidate = candidate[end + len(closer) :].strip()
                    break
    return candidate, detected


# Word end tolerance past the audio edge: sample-to-ms rounding plus a small
# model overshoot allowance, matching the shared min(50 ms, 1024 samples)
# integrity rule at 16 kHz. Anything beyond this is a timeline error.
_WORD_END_TOLERANCE_MS = 50


def parse_qwen_words(payload: Any, *, chunk_ms: int, duration_ms: int) -> list[dict]:
    """Validate one chunk's word array; offsets are rebased by the caller.

    Accepts the native forced-aligner sample shape
    (``start_sample``/``end_sample`` at 16 kHz) and the second-documented
    seconds shape (``start``/``end``). Rebased spans are validated against
    the actual total audio bounds (``duration_ms``); out-of-bounds spans
    are rejected, never clipped into fake precision.
    """

    try:
        total_ms = int(duration_ms)
    except (TypeError, ValueError) as error:
        raise QwenASRError("Qwen3 ASR word validation needs audio duration.") from error
    if total_ms <= 0:
        raise QwenASRError("Qwen3 ASR word validation needs audio duration.")
    if isinstance(payload, dict):
        for key in ("word_timestamps", "words", "timestamps", "time_stamps"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list) or not payload:
        raise QwenASRError("Qwen3 ASR returned no word timestamp array.")
    words: list[dict] = []
    previous_start = previous_end = -1.0
    for item in payload:
        if not isinstance(item, dict):
            raise QwenASRError("Qwen3 ASR word entry is malformed.")
        surface = str(item.get("word") or item.get("text") or "").strip()
        if not surface:
            raise QwenASRError("Qwen3 ASR word entry has no text.")
        try:
            if "start_sample" in item or "end_sample" in item:
                start = float(item["start_sample"]) / 16000
                end = float(item["end_sample"]) / 16000
            else:
                start = float(item["start"])
                end = float(item["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise QwenASRError("Qwen3 ASR word timestamps are invalid.") from error
        if (
            not all(math.isfinite(v) for v in (start, end))
            or not 0 <= start < end
            or start <= previous_start
            or end <= previous_end
        ):
            raise QwenASRError(
                "Qwen3 ASR word timestamps are nonmonotonic; no repaired timing "
                "was invented."
            )
        start_ms = chunk_ms + round(start * 1000)
        end_ms = chunk_ms + round(end * 1000)
        if end_ms <= start_ms or end_ms - start_ms > 2500:
            raise QwenASRError("Qwen3 ASR word span is implausible.")
        if start_ms < chunk_ms or end_ms > total_ms + _WORD_END_TOLERANCE_MS:
            raise QwenASRError(
                f"Qwen3 ASR word '{surface}' span "
                f"({start_ms}-{end_ms} ms) falls outside the audio bounds "
                f"(0-{total_ms} ms); no clipped timing was invented."
            )
        words.append({"word": surface, "start": start_ms / 1000, "end": end_ms / 1000})
        previous_start, previous_end = start, end
    return words


def _restore_surfaces(
    chunk_index: int, text: str, chunk_words: list[dict]
) -> list[dict]:
    """Project transcript punctuation onto bare word surfaces.

    Native ASR/aligner words carry lexical surfaces without sentence
    punctuation (``test`` vs ``test.``), while downstream SRT composition
    derives cue text from word surfaces. This maps each word back to its
    exact transcript grapheme span via the existing
    ``qwen_alignment.restore_surfaces`` projection: timestamps are never
    touched, only the ``word`` surface. A lexical mismatch keeps the bare
    surfaces (transcription succeeds; punctuation stays at segment level)
    and is logged, never raised.
    """

    if not text.strip() or not chunk_words:
        return chunk_words
    try:
        return qwen_alignment.restore_surfaces(text, chunk_words)
    except qwen_alignment.QwenAlignmentError as error:
        logger.warning(
            "Qwen3 ASR chunk %d: keeping bare word surfaces (%s)",
            chunk_index,
            error,
        )
        return chunk_words


def capabilities(settings: dict | None = None) -> dict:
    """Cheap, read-only recognizer discovery.

    Distinguishes the recognizer from the forced aligner: ``kind`` is
    ``recognizer`` here, ``forced_aligner`` in ``qwen_alignment``.
    """

    values = dict(settings or {})
    error = ""
    try:
        resolve_executable(values)
        available = True
    except qwen_alignment.QwenAlignmentError as problem:
        available, error = False, str(problem)
    return {
        "id": "qwen3",
        "kind": "recognizer",
        "engine": "audio.cpp",
        "family": "qwen3_asr",
        "available": available,
        "reason": error,
        "models": list(QWEN3_ASR_MODELS),
        "default_model": DEFAULT_QWEN3_ASR_MODEL,
        "supported_languages": list(QWEN3_ASR_LANGUAGE_CODES),
        "recognizer_languages": list(QWEN3_ASR_LANGUAGE_CODES),
        "alignment_languages": list(qwen_alignment.LANGUAGES),
        "timed_supported_languages": list(TIMED_SUPPORTED_LANGUAGES),
        "transcript_only_languages": list(TRANSCRIPT_ONLY_LANGUAGES),
        "requires_explicit_language_for_timestamps": REQUIRES_EXPLICIT_LANGUAGE_FOR_TIMESTAMPS,
        "timing_fallback": TIMING_FALLBACK,
        "chunk_mode_resolution": (
            "auto resolves to none with bounded outer chunks "
            "(qwen_asr_chunk_seconds>0) or fixed for single-pass runs; "
            "explicit fixed/vad/none pass through. none/fixed never touch "
            "the unbundled Silero VAD path; explicit vad needs an "
            "operator-provided qwen3_asr.vad_model_path."
        ),
        "word_timing": "qwen3_forced_aligner_when_supported_else_fallback_or_reject",
        "word_timing_languages": list(qwen_alignment.LANGUAGES),
        "diarization": False,
        "download_on_demand": available,
    }


def validate_transcription_settings(
    settings: dict, *, require_word_timestamps: bool = True
) -> dict[str, Any]:
    """Validate Qwen request settings without touching files or models.

    The returned normalized values are consumed by :func:`transcribe`; the
    orchestrator can call this helper before audio extraction or vocal
    isolation. A selected isolation method is validated here but is applied
    by the orchestrator, so this preflight deliberately does not reject it.
    """

    options = dict(settings or {})
    language = normalize_qwen_asr_language(
        options.get("stt_language") or options.get("whisper_language")
    )
    model_id = normalize_qwen_asr_model(options.get("qwen_asr_model"))
    isolation = normalize_vocal_isolation(
        options.get("transcription_vocal_isolation")
    )
    backend = _normalize_backend(options)
    chunk_seconds = _normalize_chunk_seconds(options)
    max_tokens = _normalize_max_tokens(options)
    timeout = _normalize_timeout_seconds(options)
    chunk_mode = resolve_chunk_mode(options, chunk_seconds)

    # Token budget guard BEFORE downloads: a 512-token budget cannot serve
    # a 120 s dense chunk. Fail with a fix, never truncate silently.
    required_min = _required_min_tokens(chunk_seconds, max_tokens)
    if max_tokens < required_min:
        raise QwenASRError(
            f"qwen_asr_max_tokens={max_tokens} is too small for "
            f"qwen_asr_chunk_seconds={chunk_seconds:g} (minimum {required_min}). "
            "Raise qwen_asr_max_tokens or lower qwen_asr_chunk_seconds."
        )

    # Timestamp preflight BEFORE downloads/models/work.
    if require_word_timestamps:
        if language == "auto":
            raise QwenASRError(
                "unvalidated_qwen_alignment_language:auto: timestamped Qwen3 ASR "
                "needs an explicit, validated source language before any model "
                "download. Set stt_language to one of "
                f"{', '.join(TIMED_SUPPORTED_LANGUAGES)}, or run transcript-only "
                "with require_word_timestamps=False."
            )
        if timing_plan_for_language(language) == "unsupported":
            raise QwenASRError(
                f"unsupported_qwen_timestamps:{language}: precise word timing is "
                f"unavailable for '{language}'. Timed pipeline languages are: "
                f"{', '.join(TIMED_SUPPORTED_LANGUAGES)}. Transcript-only Qwen3 "
                "ASR still supports all 30 recognizer languages; use "
                "require_word_timestamps=False or another engine for timing."
            )

    return {
        "language": language,
        "model_id": model_id,
        "isolation": isolation,
        "backend": backend,
        "chunk_seconds": chunk_seconds,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "chunk_mode": chunk_mode,
    }


def transcribe(
    audio_path: str | os.PathLike[str],
    *,
    session_dir: str | os.PathLike[str],
    output_name: str,
    settings: dict,
    executable: str | os.PathLike[str] | None = None,
    run_func: Callable[..., Any] = subprocess.run,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[float, str | None], None] | None = None,
    require_word_timestamps: bool = True,
) -> Any:
    """Run transcript-only or timestamped Qwen3 ASR over bounded chunks.

    ``require_word_timestamps`` is True for the SRT/voiceover pipeline.
    Timestamp-blocked languages fail preflight here, before any model
    download or inference work, without pretending ASR is unsupported:
    transcript-only recognition still covers all 30 languages and can be
    requested explicitly with ``require_word_timestamps=False``.
    """

    from .crispasr import CrispASRTranscriptionResult

    check_cancelled(cancel_event)
    options = dict(settings or {})
    validated = validate_transcription_settings(
        options, require_word_timestamps=require_word_timestamps
    )
    language = validated["language"]
    model_id = validated["model_id"]
    isolation = validated["isolation"]
    if isolation != "off":
        raise QwenASRError(
            "transcription_vocal_isolation must be applied by the transcription "
            "orchestrator before this call (isolated derivative input); "
            "qwen_asr.transcribe received a non-off value without an isolated "
            "source. This is a wiring error, not a skipped step."
        )
    chunk_seconds = validated["chunk_seconds"]
    max_tokens = validated["max_tokens"]
    timeout = validated["timeout"]
    source = Path(audio_path)
    if not source.is_file():
        raise QwenASRError(f"Qwen3 ASR audio does not exist: {source}")
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)

    model_path = ensure_asr_model(model_id, options, cancel_event)
    if executable is None:
        cli_path = str(resolve_executable(options))
    else:
        cli_path = str(executable)

    # Word timestamps only for validated supported *source* languages.
    want_words = True
    aligner_path: Path | None = None
    alignment_problem: str | None = None
    if not require_word_timestamps:
        want_words = False
    elif language == "auto":
        want_words = False
        alignment_problem = (
            "unvalidated_qwen_alignment_language:auto: automatic language was "
            "requested; Qwen word timestamps need a validated source language. "
            "Set an explicit source language or use CTC/Whisper alignment."
        )
    else:
        probe_settings = {**options, "original_language": language}
        alignment_problem = qwen_alignment_language_problem(probe_settings, "")
        if alignment_problem is None:
            aligner_path = ensure_aligner_model(options, cancel_event)
            want_words = True
        else:
            want_words = False

    chunks = plan_chunks(source, chunk_seconds)
    texts: list[str] = []
    detected_languages: list[str] = []
    all_words: list[dict] = []
    words_by_chunk: list[list[dict]] = []
    with tempfile.TemporaryDirectory(
        prefix="pandrator-qwen-asr-", dir=str(session_path)
    ) as temporary:
        root = Path(temporary)
        for position, chunk in enumerate(chunks):
            check_cancelled(cancel_event)
            if progress_callback is not None:
                progress_callback(
                    position / max(1, len(chunks)) * 0.9,
                    f"Qwen3 ASR: transcribing chunk {chunk.index}/{len(chunks)}",
                )
            clip = root / f"chunk-{chunk.index:04d}.wav"
            if len(chunks) == 1:
                clip = source
            else:
                _write_chunk(source, clip, chunk)
            transcript_file = root / f"chunk-{chunk.index:04d}.txt"
            words_file = root / f"chunk-{chunk.index:04d}.words.json" if want_words else None
            command = build_asr_command(
                clip,
                transcript_file,
                options,
                executable=cli_path,
                model_path=model_path,
                words_path=words_file,
                aligner_model_path=aligner_path,
                language_code=language,
            )
            try:
                _run_tool(
                    command,
                    run_func=run_func,
                    cancel_event=cancel_event,
                    timeout=timeout,
                )
            except ProcessCancelled:
                raise
            except QwenASRError:
                # Finite watchdog expiry already carries its stage.
                raise
            except (OSError, subprocess.CalledProcessError) as error:
                exit_code = getattr(error, "returncode", "?")
                tail = native_error_tail(error)
                reason = tail or f"{type(error).__name__} (no native stderr)"
                raise QwenASRError(
                    f"Qwen3 ASR failed on chunk {chunk.index} "
                    f"(audio.cpp asr task, exit {exit_code}): {reason} "
                    "No partial transcript was kept."
                ) from error
            check_cancelled(cancel_event)
            if not transcript_file.is_file():
                raise QwenASRError(
                    f"Qwen3 ASR chunk {chunk.index} produced no transcript; "
                    "refusing to truncate the recording."
                )
            text, detected = parse_transcript_file(transcript_file)
            _check_truncation(
                chunk_index=chunk.index,
                is_final=chunk.index == len(chunks),
                text=text,
                max_tokens=max_tokens,
            )
            texts.append(text)
            if detected:
                detected_languages.append(detected)
            if want_words:
                assert words_file is not None
                if not words_file.is_file():
                    raise QwenASRError(
                        f"Qwen3 ASR chunk {chunk.index} produced no word "
                        "timestamps; refusing to fabricate timing."
                    )
                try:
                    payload = json.loads(words_file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    raise QwenASRError(
                        f"Qwen3 ASR chunk {chunk.index} word output is invalid."
                    ) from error
                chunk_words = parse_qwen_words(
                    payload, chunk_ms=chunk.start_ms, duration_ms=chunk.end_ms
                )
                words_by_chunk.append(
                    _restore_surfaces(chunk.index, text, chunk_words)
                )
                all_words.extend(words_by_chunk[-1])
            elif require_word_timestamps and text.strip():
                # Timed pipeline with a non-Qwen language: the preflight
                # above already guaranteed timing_plan == canary_ctc_fallback
                # here, so refine this chunk with the existing Canary CTC
                # aligner rather than shipping coarse chunk windows.
                from . import crispasr as _crispasr

                ctc_out = root / f"chunk-{chunk.index:04d}.ctc.json"
                try:
                    def bounded_ctc_run(command, **_kwargs):
                        return _run_tool(
                            command, run_func=subprocess.run,
                            cancel_event=cancel_event, timeout=timeout,
                        )

                    with (
                        native_audio_cpp_guard(
                            [cli_path, "--backend", str(options.get("stt_compute_backend") or "best")],
                            cancel_event,
                        ) if run_func is subprocess.run else nullcontext()
                    ):
                        ctc_payload = _crispasr.run_ctc_alignment(
                            clip,
                            transcript_file,
                            ctc_out,
                            {**options, "original_language": language},
                            run_func=bounded_ctc_run if run_func is subprocess.run else run_func,
                            cancel_event=cancel_event,
                        )
                    if ctc_payload is None:
                        ctc_payload = json.loads(ctc_out.read_text(encoding="utf-8"))
                    chunk_words = parse_qwen_words(
                        ctc_payload,
                        chunk_ms=chunk.start_ms,
                        duration_ms=chunk.end_ms,
                    )
                except ProcessCancelled:
                    raise
                except Exception as error:
                    raise QwenASRError(
                        f"Qwen3 ASR chunk {chunk.index}: Canary CTC fallback "
                        f"failed ({error}). No fabricated timing was kept."
                    ) from error
                words_by_chunk.append(
                    _restore_surfaces(chunk.index, text, chunk_words)
                )
                all_words.extend(words_by_chunk[-1])
            else:
                words_by_chunk.append([])
        check_cancelled(cancel_event)

    # Coverage: every planned chunk produced evidence (possibly empty text);
    # a missing chunk is a hard failure, never a silent truncation.
    if len(texts) != len(chunks):
        raise QwenASRError("Qwen3 ASR dropped an audio chunk; transcript refused.")

    # Validate auto-detection before any timestamp use. Transcript-only
    # output stays usable; word output without validation never ships.
    effective_language = language
    if language == "auto":
        distinct = sorted(set(detected_languages))
        if want_words:
            raise QwenASRError(
                "Qwen3 ASR internal error: word timestamps with auto language."
            )
        if len(distinct) > 1:
            effective_language = "auto"
        elif len(distinct) == 1:
            effective_language = distinct[0]

    full_text = " ".join(part for part in (t.strip() for t in texts) if part)
    if not full_text:
        raise QwenASRError("Qwen3 ASR returned an empty transcript.")

    # Canonical transcript JSON (pandrator.transcript.v1): chunk-window
    # segments carry timing; words only when natively aligned.
    nframes, rate = _wav_info(source)
    _ = nframes, rate
    segments: list[dict] = []
    for chunk, text, chunk_words in zip(chunks, texts, words_by_chunk, strict=False):
        if not text.strip() and not chunk_words:
            continue
        if not text.strip() and chunk_words:
            raise QwenASRError("Qwen3 ASR words without transcript text.")
        words = [
            {
                "text": item["word"],
                "start_ms": round(item["start"] * 1000),
                "end_ms": round(item["end"] * 1000),
            }
            for item in chunk_words
        ]
        segments.append(
            {
                "id": f"qwen3-chunk-{chunk.index:04d}",
                "start_ms": chunk.start_ms,
                "end_ms": chunk.end_ms,
                "text": text.strip(),
                "words": words,
            }
        )
    if not segments:
        raise QwenASRError("Qwen3 ASR returned no usable segments.")
    fallback_used = bool(
        require_word_timestamps and not want_words and any(words_by_chunk)
    )
    if want_words:
        word_timing = "qwen3_forced_aligner"
    elif fallback_used:
        word_timing = "canary_ctc_fallback"
    else:
        word_timing = "none_chunk_window"
    metadata: dict[str, Any] = {
        "provider": "audio.cpp",
        "engine": STT_ENGINE_QWEN3,
        "model": model_id,
        "remote": False,
        "chunk_count": len(chunks),
        "chunk_seconds": chunk_seconds,
        "max_tokens": max_tokens,
        "timeout_seconds": timeout,
        "timing_plan": (
            timing_plan_for_language(language)
            if require_word_timestamps
            else "not_requested"
        ),
        "word_timing": word_timing,
        "transcription_vocal_isolation": "off",
    }
    if want_words and aligner_path is not None:
        metadata["qwen_aligner_model"] = qwen_alignment.MODEL_ID
    else:
        metadata["word_timing_unavailable_reason"] = alignment_problem or ""
    payload_dict = {
        "schema": "pandrator.transcript.v1",
        "source_format": "qwen3-asr",
        "language": effective_language,
        "metadata": metadata,
        "segments": segments,
    }
    words_path = session_path / f"{output_name}_words.json"
    words_path.write_text(
        json.dumps(payload_dict, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    srt_placeholder = session_path / f"{output_name}.srt"
    srt_placeholder.write_text("", encoding="utf-8")
    if progress_callback is not None:
        progress_callback(1.0, "Qwen3 ASR transcript ready")
    return CrispASRTranscriptionResult(
        srt_path=str(srt_placeholder),
        word_timestamps_path=str(words_path),
        engine=STT_ENGINE_QWEN3,
        compute_backend=str(options.get("stt_compute_backend") or "auto"),
    )


__all__ = [
    "DEFAULT_QWEN3_ASR_MODEL",
    "QWEN3_ASR_LANGUAGE_CODES",
    "QWEN3_ASR_LANGUAGE_LABELS",
    "QWEN3_ASR_MODELS",
    "QWEN3_CHUNK_MODES",
    "REQUIRES_EXPLICIT_LANGUAGE_FOR_TIMESTAMPS",
    "STT_ENGINE_QWEN3",
    "STT_ENGINE_QWEN3_ALIASES",
    "TIMED_SUPPORTED_LANGUAGES",
    "TIMING_FALLBACK",
    "TRANSCRIPT_ONLY_LANGUAGES",
    "VOCAL_ISOLATION_CHOICES",
    "QwenASRError",
    "alignment_languages",
    "asr_language_label",
    "build_asr_command",
    "capabilities",
    "ensure_aligner_model",
    "ensure_asr_model",
    "is_qwen3_engine",
    "native_error_tail",
    "normalize_qwen3_engine",
    "normalize_qwen_asr_language",
    "normalize_qwen_asr_model",
    "normalize_vocal_isolation",
    "parse_qwen_words",
    "parse_transcript_file",
    "plan_chunks",
    "qwen_alignment_language_problem",
    "qwen_word_alignment_supported",
    "recognizer_languages",
    "resolve_chunk_mode",
    "resolve_executable",
    "timed_supported_languages",
    "timing_plan_for_language",
    "transcribe",
    "transcript_only_languages",
    "validate_transcription_settings",
]
