"""Pure local ASR routing, with a frozen decision before recognition starts."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .crispasr import MODELS, normalize_model_quantization
from .stt_backends import normalize_stt_backend
from .stt_languages import (
    PARAKEET_V3_LANGUAGE_CODES,
    QWEN3_ASR_LANGUAGE_CODES,
    WHISPER_LARGE_V3_LANGUAGE_CODES,
    normalize_stt_language,
    validate_stt_language,
)

# Provisional until the retained-clip release benchmark validates this threshold.
LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD = 0.70


class STTRoutingError(ValueError):
    """No usable supported route satisfies the request."""


@dataclass(frozen=True)
class STTRoute:
    requested_engine: str
    requested_language: str
    resolved_language: str
    language_source: str
    confidence: float | None
    engine: str
    model: str
    alignment_route: str
    fallback_reason: str
    require_word_timestamps: bool
    runtime_requirements: tuple[str, ...]
    metadata_requirements: tuple[str, ...] = ("transcript_json",)
    detected_language: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "runtime_requirements": list(self.runtime_requirements),
            "metadata_requirements": list(self.metadata_requirements),
            "confidence_threshold": LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD,
            "confidence_threshold_provisional": True,
        }


def _usable(status: Any, engine: str) -> bool:
    if status is None:
        return False
    if isinstance(status, Mapping):
        available = status.get("runtime_usable", status.get("installed", False))
        version = str(status.get("version") or "")
    else:
        available = getattr(status, "installed", False)
        version = str(getattr(status, "version", "") or "")
    if engine == "qwen3" and version:
        match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (0, 8, 36):
            return False
    return bool(available)


def resolve_stt_route(
    settings: Mapping[str, Any],
    *,
    resolved_language: str | None = None,
    language_source: str = "declared",
    confidence: float | None = None,
    detection_reason: str = "",
    require_word_timestamps: bool = True,
    runtime_statuses: Mapping[str, Any] | None = None,
) -> STTRoute:
    """Choose only before dispatch; recognition errors never trigger substitution.

    Runtime usability is independent of model cache state. Regional request tags
    remain in provenance while adapters receive model-level language codes.
    """
    requested_engine = str(settings.get("stt_engine") or settings.get("stt_backend") or "")
    requested_language = str(settings.get("stt_language") or settings.get("whisper_language") or "auto")
    engine = normalize_stt_backend(requested_engine)
    language = normalize_stt_language(resolved_language if resolved_language is not None else requested_language)
    detected_language = language if language_source == "detected" else ""
    reason = str(detection_reason or "")[:600]
    if language_source == "detected":
        if confidence is None or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            reason = reason or "language_detection_invalid_confidence"
            confidence = None
        elif confidence < LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD:
            reason = reason or "language_detection_low_confidence"
        if reason:
            language = "auto"
            language_source = "unresolved"
    if engine == "auto":
        from .qwen_asr import timed_supported_languages

        statuses = runtime_statuses or {}
        if reason or language == "auto" or language not in {*WHISPER_LARGE_V3_LANGUAGE_CODES, *QWEN3_ASR_LANGUAGE_CODES}:
            reason = reason or ("source_language_unknown" if language != "auto" else "source_language_unresolved")
            language = "auto"
            if not _usable(statuses.get("whisper"), "whisper"):
                raise STTRoutingError(f"{reason}: choose an explicit source language or install a usable Whisper runtime.")
            engine = "whisper"
        else:
            candidates = [
                ("parakeet", language in PARAKEET_V3_LANGUAGE_CODES),
                ("qwen3", language in QWEN3_ASR_LANGUAGE_CODES and (not require_word_timestamps or language in timed_supported_languages())),
                ("whisper", language in WHISPER_LARGE_V3_LANGUAGE_CODES),
            ]
            engine = next((key for key, supported in candidates if supported and _usable(statuses.get(key), key)), "")
            if not engine:
                raise STTRoutingError(f"No usable ASR runtime supports source language '{language}' with the requested timing. Install a supported runtime or choose an explicit engine.")
            if engine != "parakeet":
                reason = "parakeet_language_unsupported" if language not in PARAKEET_V3_LANGUAGE_CODES else "parakeet_runtime_unavailable"
                if engine == "whisper" and language in QWEN3_ASR_LANGUAGE_CODES:
                    reason += ";qwen_timing_unsupported" if require_word_timestamps and language not in timed_supported_languages() else ";qwen_runtime_unavailable"
    elif engine in {"parakeet", "qwen3"} and language == "auto":
        raise STTRoutingError(f"{reason or 'source_language_unresolved'}: {engine} needs a validated source language; choose it explicitly or enable language detection.")
    validate_stt_language(engine, language)
    if engine == "qwen3":
        from .qwen_asr import normalize_qwen_asr_model, timing_plan_for_language

        model = normalize_qwen_asr_model(settings.get("qwen_asr_model"))
        alignment = timing_plan_for_language(language) if require_word_timestamps else "none"
        requirements = ("crispasr>=0.8.36", model) + ((alignment,) if require_word_timestamps else ())
    elif engine in MODELS:
        model = MODELS[engine].filename_for(normalize_model_quantization(settings.get("stt_model_quantization"), engine))
        alignment = MODELS[engine].word_timing if require_word_timestamps else "none"
        requirements = ("crispasr", model)
    else:
        model, alignment, requirements = engine, "provider" if require_word_timestamps else "none", ("provider",)
    return STTRoute(
        requested_engine, requested_language, language, language_source,
        confidence, engine, model, alignment, reason, require_word_timestamps,
        requirements, ("transcript_json", "word_timestamps") if require_word_timestamps else ("transcript_json",),
        detected_language=detected_language,
    )
