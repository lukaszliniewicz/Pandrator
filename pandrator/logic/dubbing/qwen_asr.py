"""Qwen3 ASR transcription through the pinned CrispASR GGUF pipeline."""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..audio_cpp_assets import AudioAssetsError
from ..audio_cpp_assets import resolve_executable as resolve_audio_cpp_executable
from ..audio_cpp_execution import native_audio_cpp_guard
from ..cancellable_process import ProcessCancelled, run_cancellable
from . import crispasr, crispasr_qwen_assets, qwen_alignment
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

# Compatibility helper for callers displaying a language name. CrispASR
# receives the codes above through -l, never these labels.
QWEN3_ASR_LANGUAGE_LABELS = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "ar": "Arabic",
    "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "id": "Indonesian", "it": "Italian", "ko": "Korean", "ru": "Russian",
    "th": "Thai", "vi": "Vietnamese", "ja": "Japanese", "tr": "Turkish",
    "hi": "Hindi", "ms": "Malay", "nl": "Dutch", "sv": "Swedish",
    "da": "Danish", "fi": "Finnish", "pl": "Polish", "cs": "Czech",
    "fil": "Filipino", "fa": "Persian", "el": "Greek", "hu": "Hungarian",
    "mk": "Macedonian", "ro": "Romanian",
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

_BACKENDS = ("auto", "cpu", "cuda", "vulkan", "metal")


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
    """Return the legacy display label without changing the CLI language code."""

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


def _setting(settings: dict, name: str, default: Any) -> Any:
    value = settings.get(name)
    return default if value is None or value == "" else value


def _normalize_backend(settings: dict) -> str:
    backend = str(
        _setting(settings, "qwen_asr_backend", None)
        or _setting(settings, "stt_compute_backend", "auto")
    ).strip().lower()
    backend = "auto" if backend == "best" else backend
    if backend not in _BACKENDS:
        raise QwenASRError(f"Unsupported CrispASR Qwen backend: {backend}")
    return backend


def _normalize_chunk_seconds(settings: dict) -> float:
    # CrispASR chunks internally. A stored zero uses the bounded default.
    raw = _setting(settings, "qwen_asr_chunk_seconds", 30)
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise QwenASRError("qwen_asr_chunk_seconds must be 0 or 10-120.") from error
    if not math.isfinite(value) or value < 0:
        raise QwenASRError("qwen_asr_chunk_seconds must be 0 or 10-120.")
    if value == 0:
        return 30.0
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
    # Finite watchdog per native call; bounded chunks finish far sooner.
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
    """Resolve whether CrispASR VAD is used with bounded chunks."""

    explicit = _normalize_chunk_mode(settings)
    if explicit == "auto":
        return "vad" if bool(settings.get("crispasr_vad_enabled", True)) else "fixed"
    return explicit


def _required_min_tokens(chunk_seconds: float, max_tokens: int) -> int:
    # ~8 decode tokens per second covers dense speech; the 512 default
    # serves the 30 s bounded default. Larger Pandrator chunks need a
    # larger budget, otherwise a long chunk risks silent truncation.
    effective = chunk_seconds if chunk_seconds > 0 else 30.0
    return min(4096, max(512, math.ceil(effective * 8)))


def resolve_executable(settings: dict) -> Path:
    explicit = str(settings.get("crispasr_executable") or "").strip()
    return Path(crispasr.resolve_executable(explicit))


@lru_cache(maxsize=8)
def _runtime_version_for_file(executable: str, _mtime_ns: int, _size: int) -> tuple[int, int, int]:
    try:
        completed = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QwenASRError(
            "Could not verify the CrispASR runtime; install CrispASR v0.8.36 or later."
        ) from error
    output = f"{completed.stdout}\n{completed.stderr}"
    matched = re.search(
        r"(?i)(?:crisp\s*asr[^\n]*?|version\s*:?\s*)v?(\d+)\.(\d+)\.(\d+)",
        output,
    )
    if completed.returncode != 0 or matched is None:
        raise QwenASRError(
            "Could not verify the CrispASR runtime version; install CrispASR v0.8.36 or later."
        )
    return int(matched.group(1)), int(matched.group(2)), int(matched.group(3))


def _verify_qwen_runtime(executable: str) -> None:
    path = Path(executable)
    resolved = path if path.is_file() else Path(shutil.which(executable) or "")
    if not resolved.is_file():
        raise QwenASRError(
            "CrispASR executable was not found; install CrispASR v0.8.36 or later."
        )
    stat = resolved.stat()
    version = _runtime_version_for_file(str(resolved), stat.st_mtime_ns, stat.st_size)
    if version < (0, 8, 36):
        raise QwenASRError(
            f"CrispASR v{'.'.join(map(str, version))} is too old for Qwen3; "
            "install v0.8.36 or later with --strict-pipeline and --require-word-timestamps."
        )


def ensure_asr_model(
    model_id: str,
    settings: dict,
    cancel_event: threading.Event | None = None,
    progress: Callable[..., None] | None = None,
) -> Path:
    normalized = normalize_qwen_asr_model(model_id)
    try:
        return crispasr_qwen_assets.ensure_asset(normalized, settings, cancel_event, progress)
    except ProcessCancelled:
        raise
    except Exception as error:
        raise QwenASRError(f"Qwen3 ASR model {normalized} could not be provisioned: {error}") from error


def ensure_aligner_model(
    settings: dict, cancel_event: threading.Event | None = None
) -> Path:
    try:
        return crispasr_qwen_assets.ensure_asset(
            "qwen3_forced_aligner", settings, cancel_event
        )
    except ProcessCancelled:
        raise
    except Exception as error:
        raise QwenASRError(f"Qwen3 forced aligner could not be provisioned: {error}") from error


def check_cancelled(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise ProcessCancelled("Qwen3 ASR was canceled.")


def _run_tool(
    command: list[str],
    *,
    run_func: Callable[..., Any],
    cancel_event: threading.Event | None,
    timeout: float | None = None,
    backend: str = "auto",
    guard_executable: str | os.PathLike[str] | None = None,
) -> Any:
    """Run CrispASR through the shared cancellable child and finite watchdog."""

    check_cancelled(cancel_event)
    timeout_value = float(timeout) if timeout is not None else None
    if timeout_value is not None and (
        not math.isfinite(timeout_value) or timeout_value <= 0
    ):
        raise QwenASRError("Qwen3 ASR timeout must be finite seconds.")
    combined = threading.Event()
    stop = threading.Event()
    expired = threading.Event()

    def watch() -> None:
        started = time.monotonic()
        while not stop.is_set():
            if cancel_event is not None and cancel_event.is_set():
                combined.set()
                return
            if (
                timeout_value is not None
                and time.monotonic() - started >= timeout_value
            ):
                expired.set()
                combined.set()
                return
            stop.wait(0.05)

    watcher = threading.Thread(target=watch, name="qwen-asr-timeout", daemon=True)
    watcher.start()
    try:
        if run_func is subprocess.run:
            # The guard reads server.json beside audio.cpp, which may use a
            # custom port. CrispASR's directory is the wrong identity.
            if guard_executable is None:
                try:
                    guard_executable = resolve_audio_cpp_executable({})
                except AudioAssetsError:
                    # Crisp-only installations still use the cross-process
                    # lock and the guard's default local-server check.
                    guard_executable = command[0]
            with native_audio_cpp_guard(
                [str(guard_executable), "--backend", backend], combined
            ):
                return run_cancellable(
                    command, cancel_event=combined, check=True, capture_output=True
                )
        return run_func(command, check=True, capture_output=True)
    except ProcessCancelled:
        if cancel_event is not None and cancel_event.is_set():
            raise
        if expired.is_set():
            raise QwenASRError(
                f"Qwen3 ASR timed out after {timeout_value:g} seconds; "
                "no partial transcript was kept."
            ) from None
        raise
    finally:
        stop.set()
        watcher.join(timeout=2.0)


def _native_error_tail(error: BaseException, *, max_chars: int = 600) -> str:
    raw = getattr(error, "stderr", None)
    if raw is None:
        return ""
    value = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    markers = ("error", "failed", "missing", "invalid", "unknown", "not found", "unsupported", "fatal", "unable", "cannot", "no speech detected")
    lines = [line.strip() for line in value.splitlines() if any(marker in line.lower() for marker in markers)]
    return "; ".join(lines[-5:])[:max_chars]


def capabilities(settings: dict | None = None) -> dict:
    """Read-only discovery; no model load, cache download, or CLI probe."""

    values = dict(settings or {})
    executable = resolve_executable(values)
    available = executable.is_file() or bool(shutil.which(str(executable)))
    cached = {}
    for key, asset in crispasr_qwen_assets.ASSETS.items():
        path = crispasr_qwen_assets.cache_path(asset, values)
        cached[key] = path.is_file() and path.stat().st_size == asset.size
    return {
        "id": STT_ENGINE_QWEN3,
        "kind": "recognizer",
        "engine": "crispasr",
        "family": "qwen3_asr",
        "available": available,
        "reason": "" if available else "CrispASR executable was not found.",
        "models": list(QWEN3_ASR_MODELS),
        "default_model": DEFAULT_QWEN3_ASR_MODEL,
        "cached_assets": cached,
        "supported_languages": list(QWEN3_ASR_LANGUAGE_CODES),
        "recognizer_languages": list(QWEN3_ASR_LANGUAGE_CODES),
        "alignment_languages": list(qwen_alignment.LANGUAGES),
        "timed_supported_languages": list(TIMED_SUPPORTED_LANGUAGES),
        "transcript_only_languages": list(TRANSCRIPT_ONLY_LANGUAGES),
        "requires_explicit_language_for_timestamps": REQUIRES_EXPLICIT_LANGUAGE_FOR_TIMESTAMPS,
        "timing_fallback": TIMING_FALLBACK,
        "word_timing": "qwen3_forced_aligner_when_supported_else_fallback_or_reject",
        "word_timing_languages": list(qwen_alignment.LANGUAGES),
        "diarization": False,
        "download_on_demand": available,
    }


def validate_transcription_settings(
    settings: dict, *, require_word_timestamps: bool = True
) -> dict[str, Any]:
    """Reject invalid requests before preprocessing or model acquisition."""

    options = dict(settings or {})
    language = normalize_qwen_asr_language(
        options.get("stt_language") or options.get("whisper_language")
    )
    model_id = normalize_qwen_asr_model(options.get("qwen_asr_model"))
    isolation = normalize_vocal_isolation(options.get("transcription_vocal_isolation"))
    backend = _normalize_backend(options)
    chunk_seconds = _normalize_chunk_seconds(options)
    max_tokens = _normalize_max_tokens(options)
    timeout = _normalize_timeout_seconds(options)
    chunk_mode = resolve_chunk_mode(options, chunk_seconds)
    required_min = _required_min_tokens(chunk_seconds, max_tokens)
    if max_tokens < required_min:
        raise QwenASRError(
            f"qwen_asr_max_tokens={max_tokens} is too small for "
            f"qwen_asr_chunk_seconds={chunk_seconds:g} (minimum {required_min}). "
            "Raise qwen_asr_max_tokens or lower qwen_asr_chunk_seconds."
        )
    if require_word_timestamps:
        if language == "auto":
            raise QwenASRError(
                "unvalidated_qwen_alignment_language:auto: timestamped Qwen3 ASR "
                "needs an explicit, validated source language before any model download."
            )
        if timing_plan_for_language(language) == "unsupported":
            raise QwenASRError(
                f"unsupported_qwen_timestamps:{language}: precise word timing is "
                f"unavailable for '{language}'. Timed pipeline languages are: "
                f"{', '.join(TIMED_SUPPORTED_LANGUAGES)}."
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
) -> crispasr.CrispASRTranscriptionResult:
    check_cancelled(cancel_event)
    options = dict(settings or {})
    validated = validate_transcription_settings(
        options, require_word_timestamps=require_word_timestamps
    )
    if validated["isolation"] != "off":
        raise QwenASRError(
            "transcription_vocal_isolation must be applied by the transcription orchestrator "
            "before this call (isolated derivative input)."
        )
    source = Path(audio_path)
    if not source.is_file():
        raise QwenASRError(f"Qwen3 ASR audio does not exist: {source}")
    cli = str(executable) if executable is not None else str(resolve_executable(options))
    try:
        guard_executable = resolve_audio_cpp_executable(options)
    except AudioAssetsError:
        guard_executable = None
    if run_func is subprocess.run:
        _verify_qwen_runtime(cli)
    if progress_callback is not None:
        progress_callback(0.0, "Preparing Qwen3 ASR")
    model = ensure_asr_model(validated["model_id"], options, cancel_event)
    check_cancelled(cancel_event)
    aligner: Path | str | None = None
    if require_word_timestamps:
        if timing_plan_for_language(validated["language"]) == "qwen3_forced_aligner":
            aligner = ensure_aligner_model(options, cancel_event)
        else:
            cached = crispasr._cached_artifact_path(options, crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT)
            aligner = cached or "canary-ctc-aligner"
    if progress_callback is not None:
        progress_callback(0.15, "Running Qwen3 ASR")

    mode = validated["chunk_mode"]
    effective = {
        **options,
        "stt_engine": STT_ENGINE_QWEN3,
        "stt_language": validated["language"],
        "stt_compute_backend": validated["backend"],
        "stt_chunk_seconds": validated["chunk_seconds"],
        "crispasr_vad_enabled": mode == "vad",
        "crispasr_vad_max_speech_seconds": min(
            validated["chunk_seconds"],
            float(_setting(options, "crispasr_vad_max_speech_seconds", 300)),
        ),
    }
    def bounded_run(command: list[str], **_kwargs: Any) -> Any:
        try:
            return _run_tool(
                command,
                run_func=run_func,
                cancel_event=cancel_event,
                timeout=validated["timeout"],
                backend=validated["backend"],
                guard_executable=guard_executable,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            reason = _native_error_tail(error) or type(error).__name__
            if "no speech detected" in reason.lower():
                raise QwenASRError("Qwen3 ASR detected no speech in this audio.") from error
            if "unknown" in reason.lower() and any(
                flag in reason for flag in ("strict-pipeline", "require-word-timestamps")
            ):
                reason += " Update CrispASR to v0.8.36 or later with Qwen timing support."
            raise QwenASRError(
                f"Qwen3 ASR CrispASR execution failed: {reason}. No partial transcript was kept."
            ) from error

    try:
        result = crispasr.transcribe(
            source,
            session_dir=session_dir,
            output_name=output_name,
            settings=effective,
            executable=cli,
            run_func=bounded_run,
            cancel_event=cancel_event,
            model_path=model,
            aligner_model_path=aligner,
            require_word_timestamps=require_word_timestamps,
        )
    except ProcessCancelled:
        raise
    except crispasr.CrispASRError as error:
        raise QwenASRError(str(error)) from error
    check_cancelled(cancel_event)
    if progress_callback is not None:
        progress_callback(1.0, "Qwen3 ASR complete")
    return result
