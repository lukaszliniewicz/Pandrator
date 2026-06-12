"""Lazy NeMo text normalization for TTS input."""

import logging
import os
import re
import threading


NEMO_TEXT_NORMALIZATION_LANGUAGES = frozenset(
    {
        "ar",
        "de",
        "en",
        "es",
        "fr",
        "hi",
        "hu",
        "hy",
        "it",
        "ja",
        "ko",
        "pt",
    }
)

NEMO_LANGUAGE_ALIASES = {
    "arabic": "ar",
    "english": "en",
    "en-gb": "en",
    "en-us": "en",
    "french": "fr",
    "fr-fr": "fr",
    "german": "de",
    "hindi": "hi",
    "hungarian": "hu",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "portuguese": "pt",
    "pt-br": "pt",
    "spanish": "es",
    "german (v3)": "de",
    "english (v3)": "en",
    "english indic (v3)": "en",
    "spanish (v3)": "es",
    "french (v3)": "fr",
    "indic (v3)": "hi",
}

_CHAPTER_MARKER = "[[Chapter]]"
_PROTECTED_BOUNDARY_RE = re.compile(r"(\[\[Chapter\]\]|\r?\n+)")
_NON_WHITESPACE_RE = re.compile(r"\S+")
_INTERNAL_TOKEN_MARKUP_RE = re.compile(r"\b(?:tokens?|name|cardinal|ordinal|date|time)\s*\{")
_MAX_WORDS_PER_NORMALIZATION_UNIT = 300

_NORMALIZER_CACHE = {}
_FAILED_LANGUAGES = set()
_NORMALIZER_LOCK = threading.RLock()


def normalize_nemo_language(language: str) -> str | None:
    """Return NeMo's language code when deterministic TN supports the selection."""
    normalized = str(language or "").strip().lower().replace("_", "-")
    if not normalized:
        return None

    normalized = NEMO_LANGUAGE_ALIASES.get(normalized, normalized)
    if normalized in NEMO_TEXT_NORMALIZATION_LANGUAGES:
        return normalized

    if "-" in normalized:
        base_language = normalized.split("-", 1)[0]
        if base_language in NEMO_TEXT_NORMALIZATION_LANGUAGES:
            return base_language

    return None


def is_nemo_normalization_supported(language: str) -> bool:
    return normalize_nemo_language(language) is not None


def _get_cache_dir() -> str:
    configured = str(os.environ.get("PANDRATOR_NEMO_CACHE_DIR") or "").strip()
    if configured:
        cache_dir = os.path.abspath(os.path.expanduser(configured))
    else:
        cache_root = str(os.environ.get("XDG_CACHE_HOME") or "").strip()
        if not cache_root:
            cache_root = os.path.join(os.path.expanduser("~"), ".cache")
        cache_dir = os.path.join(cache_root, "nemo_text_processing")

    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _create_normalizer(language: str):
    from nemo_text_processing.text_normalization.normalize import Normalizer

    return Normalizer(
        input_case="cased",
        lang=language,
        deterministic=True,
        cache_dir=_get_cache_dir(),
        overwrite_cache=False,
        post_process=True,
    )


def _get_normalizer(language: str):
    with _NORMALIZER_LOCK:
        if language in _FAILED_LANGUAGES:
            return None
        if language in _NORMALIZER_CACHE:
            return _NORMALIZER_CACHE[language]

        try:
            normalizer = _create_normalizer(language)
        except Exception as exc:
            _FAILED_LANGUAGES.add(language)
            logging.warning(
                "NeMo text normalization is unavailable for language '%s': %s",
                language,
                exc,
            )
            return None

        _NORMALIZER_CACHE[language] = normalizer
        return normalizer


def _split_long_unit(text: str) -> list[str]:
    words = list(_NON_WHITESPACE_RE.finditer(text))
    if len(words) <= _MAX_WORDS_PER_NORMALIZATION_UNIT:
        return [text]

    units = []
    start = 0
    for word_index in range(_MAX_WORDS_PER_NORMALIZATION_UNIT, len(words), _MAX_WORDS_PER_NORMALIZATION_UNIT):
        end = words[word_index - 1].end()
        units.append(text[start:end])
        start = end
    units.append(text[start:])
    return units


def _normalize_unit(normalizer, text: str) -> str:
    if not text or not text.strip():
        return text

    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()) :]
    body_end = len(text) - len(trailing) if trailing else len(text)
    body = text[len(leading) : body_end]
    if not body:
        return text

    try:
        normalized = normalizer.normalize(
            body,
            verbose=False,
            punct_pre_process=False,
            punct_post_process=False,
        )
    except Exception as exc:
        logging.warning("NeMo text normalization skipped a text segment: %s", exc)
        return text

    if not str(normalized or "").strip():
        logging.warning("NeMo text normalization returned an empty text segment; using the source text.")
        return text
    if _INTERNAL_TOKEN_MARKUP_RE.search(str(normalized)):
        logging.warning("NeMo text normalization returned internal token markup; using the source text.")
        return text

    return f"{leading}{normalized}{trailing}"


def normalize_text_for_tts(text: str, language: str) -> str:
    """Convert written text to spoken form while preserving Pandrator structure."""
    nemo_language = normalize_nemo_language(language)
    if not text or nemo_language is None:
        return text

    normalizer = _get_normalizer(nemo_language)
    if normalizer is None:
        return text

    output = []
    with _NORMALIZER_LOCK:
        for protected_piece in _PROTECTED_BOUNDARY_RE.split(text):
            if not protected_piece:
                continue
            if protected_piece == _CHAPTER_MARKER or protected_piece.lstrip("\r\n") == "":
                output.append(protected_piece)
                continue
            output.extend(_normalize_unit(normalizer, unit) for unit in _split_long_unit(protected_piece))

    return "".join(output)
