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
    "日本語": "ja",
    "chinese (traditional)": "zh-tw",
    "chinese (simplified)": "zh-cn",
    "traditional chinese": "zh-tw",
    "simplified chinese": "zh-cn",
    "한국어": "ko",
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

LANGUAGE_DISPLAY_NAMES = {
    code: name.title() for name, code in LANGUAGE_CODE_ALIASES.items()
}
LANGUAGE_DISPLAY_NAMES.update({
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "zh-cn": "Chinese (Simplified)", "zh-tw": "Chinese (Traditional)",
})


def normalize_language_code(language: str, default: str = "en") -> str:
    """Normalize user-facing language names and service codes."""
    normalized = str(language or "").strip().lower().replace("_", "-")
    if not normalized:
        return default
    if normalized in LANGUAGE_CODE_ALIASES:
        return LANGUAGE_CODE_ALIASES[normalized]
    iso_aliases = {"jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh", "cmn": "zh"}
    normalized = iso_aliases.get(normalized, normalized)
    parts = normalized.split("-")
    base = iso_aliases.get(parts[0], parts[0])
    if base in {"ja", "ko"}:
        return base
    if base == "zh":
        if "hant" in parts or any(part in {"tw", "hk", "mo"} for part in parts):
            return "zh-tw"
        if "hans" in parts or any(part in {"cn", "sg"} for part in parts):
            return "zh-cn"
        return "zh"
    if normalized in FFMPEG_SUBTITLE_LANGUAGE_CODES:
        return normalized
    if base in FFMPEG_SUBTITLE_LANGUAGE_CODES and len(parts) > 1:
        return normalized if len(normalized) <= 5 else base
    return normalized if len(normalized) <= 5 else default


def ffmpeg_subtitle_language_code(language: str, default: str = "eng") -> str:
    """Return an ISO-639-style three-letter code suitable for FFmpeg metadata."""
    normalized = normalize_language_code(language, default="en")
    return FFMPEG_SUBTITLE_LANGUAGE_CODES.get(normalized, FFMPEG_SUBTITLE_LANGUAGE_CODES.get(normalized.split("-")[0], default))


def subtitle_language_title(language: str, default: str = "Subtitles") -> str:
    """Return a concise human-facing title for a subtitle track."""
    normalized = normalize_language_code(language, default="und")
    if normalized in {"", "auto", "und", "unknown"}:
        return default
    return LANGUAGE_DISPLAY_NAMES.get(normalized, normalized.upper())
