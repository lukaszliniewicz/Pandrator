"""Language helpers shared by Pandrator dubbing services."""

from __future__ import annotations


LANGUAGE_CODE_ALIASES = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "polish": "pl",
    "turkish": "tr",
    "russian": "ru",
    "dutch": "nl",
    "czech": "cs",
    "arabic": "ar",
    "bulgarian": "bg",
    "chinese": "zh-cn",
    "croatian": "hr",
    "danish": "da",
    "estonian": "et",
    "finnish": "fi",
    "greek": "el",
    "japanese": "ja",
    "hungarian": "hu",
    "korean": "ko",
    "hindi": "hi",
    "latvian": "lv",
    "lithuanian": "lt",
    "maltese": "mt",
    "romanian": "ro",
    "slovak": "sk",
    "slovenian": "sl",
    "swedish": "sv",
    "ukrainian": "uk",
}

FFMPEG_SUBTITLE_LANGUAGE_CODES = {
    "en": "eng",
    "es": "spa",
    "fr": "fra",
    "de": "deu",
    "it": "ita",
    "pt": "por",
    "pl": "pol",
    "tr": "tur",
    "ru": "rus",
    "nl": "nld",
    "cs": "ces",
    "ar": "ara",
    "bg": "bul",
    "zh": "zho",
    "zh-cn": "zho",
    "zh-tw": "zho",
    "hr": "hrv",
    "da": "dan",
    "et": "est",
    "fi": "fin",
    "el": "ell",
    "ja": "jpn",
    "hu": "hun",
    "ko": "kor",
    "hi": "hin",
    "lv": "lav",
    "lt": "lit",
    "mt": "mlt",
    "ro": "ron",
    "sk": "slk",
    "sl": "slv",
    "sv": "swe",
    "uk": "ukr",
}


def normalize_language_code(language: str, default: str = "en") -> str:
    """Normalize user-facing language names and service codes."""
    normalized = str(language or "").strip().lower().replace("_", "-")
    if not normalized:
        return default
    if normalized in LANGUAGE_CODE_ALIASES:
        return LANGUAGE_CODE_ALIASES[normalized]
    if normalized in FFMPEG_SUBTITLE_LANGUAGE_CODES:
        return normalized
    return normalized if len(normalized) <= 5 else default


def ffmpeg_subtitle_language_code(language: str, default: str = "eng") -> str:
    """Return an ISO-639-style three-letter code suitable for FFmpeg metadata."""
    normalized = normalize_language_code(language, default="en")
    return FFMPEG_SUBTITLE_LANGUAGE_CODES.get(normalized, default)
