"""Canonical language coverage and validation for local STT engines.

The language tuples below are deliberately model-specific.  ``None`` means
that the model's coverage is not authoritative and callers must not invent a
restriction from a sample or an evaluation subset.
"""

from __future__ import annotations

from .languages import normalize_language_code


# Source: NVIDIA's official Hugging Face model card for
# nvidia/parakeet-tdt-0.6b-v3 (https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
PARAKEET_V3_LANGUAGE_CODES = (
    "bg",
    "hr",
    "cs",
    "da",
    "nl",
    "en",
    "et",
    "fi",
    "fr",
    "de",
    "el",
    "hu",
    "it",
    "lv",
    "lt",
    "mt",
    "pl",
    "pt",
    "ro",
    "sk",
    "sl",
    "es",
    "sv",
    "ru",
    "uk",
)


# Source: OpenAI's official Whisper tokenizer language table
# (https://github.com/openai/whisper/blob/main/whisper/tokenizer.py).
WHISPER_LARGE_V3_LANGUAGE_CODES = (
    "af",
    "am",
    "ar",
    "as",
    "az",
    "ba",
    "be",
    "bg",
    "bn",
    "bo",
    "br",
    "bs",
    "ca",
    "cs",
    "cy",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "eu",
    "fa",
    "fi",
    "fo",
    "fr",
    "gl",
    "gu",
    "ha",
    "haw",
    "he",
    "hi",
    "hr",
    "ht",
    "hu",
    "hy",
    "id",
    "is",
    "it",
    "ja",
    "jw",
    "ka",
    "kk",
    "km",
    "kn",
    "ko",
    "la",
    "lb",
    "ln",
    "lo",
    "lt",
    "lv",
    "mg",
    "mi",
    "mk",
    "ml",
    "mn",
    "mr",
    "ms",
    "mt",
    "my",
    "ne",
    "nl",
    "nn",
    "no",
    "oc",
    "pa",
    "pl",
    "ps",
    "pt",
    "ro",
    "ru",
    "sa",
    "sd",
    "si",
    "sk",
    "sl",
    "sn",
    "so",
    "sq",
    "sr",
    "su",
    "sv",
    "sw",
    "ta",
    "te",
    "tg",
    "th",
    "tk",
    "tl",
    "tr",
    "tt",
    "uk",
    "ur",
    "uz",
    "vi",
    "yi",
    "yo",
    "yue",
    "zh",
)


_SUPPORTED_LANGUAGES: dict[str, tuple[str, ...] | None] = {
    "whisper": WHISPER_LARGE_V3_LANGUAGE_CODES,
    "parakeet": PARAKEET_V3_LANGUAGE_CODES,
    "moss": None,
}
_LANGUAGE_ALIASES = {"nb": "no", "iw": "he", "jv": "jw"}
_COMPACT_REGION_ALIASES = {"ptbr": "pt", "zhcn": "zh"}
_KNOWN_LANGUAGE_CODES = frozenset(WHISPER_LARGE_V3_LANGUAGE_CODES)


def supported_stt_languages(canonical_engine: str) -> tuple[str, ...] | None:
    """Return authoritative language coverage for a canonical local engine."""

    return _SUPPORTED_LANGUAGES.get(str(canonical_engine or "").strip().lower())


def normalize_stt_language(language: str | None) -> str:
    """Normalize an STT language code while preserving unknown values.

    Region suffixes for recognized language codes are intentionally discarded
    because local model coverage is expressed as language codes (for example,
    ``pt-BR`` becomes ``pt``). Unknown values remain intact so unrestricted
    providers can receive the caller's explicit value.
    """

    raw = str(language or "").strip()
    if not raw:
        return "auto"
    fallback = raw.lower().replace("_", "-").replace(" ", "-")
    normalized = normalize_language_code(raw, default=fallback)
    if normalized == "auto":
        return "auto"
    normalized = _COMPACT_REGION_ALIASES.get(normalized, normalized)
    base = normalized.split("-", 1)[0]
    if base in _KNOWN_LANGUAGE_CODES or base in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES.get(base, base)
    return normalized


def validate_stt_language(canonical_engine: str, language: str | None) -> str:
    """Normalize and validate one language against authoritative coverage.

    Engines with unknown coverage (currently MOSS and future providers) keep
    accepting explicit values.  Local engines with a known tuple fail before
    any model resolution, download, or inference work begins.
    """

    normalized = normalize_stt_language(language)
    supported = supported_stt_languages(canonical_engine)
    if normalized != "auto" and supported is not None and normalized not in supported:
        engine = str(canonical_engine or "").strip() or "selected STT engine"
        raise ValueError(
            f"Unsupported language '{normalized}' for {engine}; "
            f"supported languages are: {', '.join(supported)}."
        )
    return normalized


__all__ = [
    "PARAKEET_V3_LANGUAGE_CODES",
    "WHISPER_LARGE_V3_LANGUAGE_CODES",
    "normalize_stt_language",
    "supported_stt_languages",
    "validate_stt_language",
]
