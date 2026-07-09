SILERO_LANGUAGES = [
    {"name": "German (v3)", "code": "v3_de.pt"},
    {"name": "English (v3)", "code": "v3_en.pt"},
    {"name": "English Indic (v3)", "code": "v3_en_indic.pt"},
    {"name": "Spanish (v3)", "code": "v3_es.pt"},
    {"name": "French (v3)", "code": "v3_fr.pt"},
    {"name": "Indic (v3)", "code": "v3_indic.pt"},
    {"name": "Russian (v3.1)", "code": "v3_1_ru.pt"},
    {"name": "Tatar (v3)", "code": "v3_tt.pt"},
    {"name": "Ukrainian (v3)", "code": "v3_ua.pt"},
    {"name": "Uzbek (v3)", "code": "v3_uz.pt"},
    {"name": "Kalmyk (v3)", "code": "v3_xal.pt"}
]

WHISPER_LANGUAGES = [
    'Afrikaans', 'Albanian', 'Amharic', 'Arabic', 'Armenian', 'Assamese', 'Azerbaijani', 'Bashkir', 'Basque', 
    'Belarusian', 'Bengali', 'Bosnian', 'Breton', 'Bulgarian', 'Burmese', 'Cantonese', 'Castilian', 'Catalan', 
    'Chinese', 'Croatian', 'Czech', 'Danish', 'Dutch', 'English', 'Estonian', 'Faroese', 'Finnish', 'Flemish', 
    'French', 'Galician', 'Georgian', 'German', 'Greek', 'Gujarati', 'Haitian', 'Haitian Creole', 'Hausa', 
    'Hawaiian', 'Hebrew', 'Hindi', 'Hungarian', 'Icelandic', 'Indonesian', 'Italian', 'Japanese', 'Javanese', 
    'Kannada', 'Kazakh', 'Khmer', 'Korean', 'Lao', 'Latin', 'Latvian', 'Letzeburgesch', 'Lingala', 'Lithuanian', 
    'Luxembourgish', 'Macedonian', 'Malagasy', 'Malay', 'Malayalam', 'Maltese', 'Maori', 'Marathi', 'Moldavian', 
    'Moldovan', 'Mongolian', 'Myanmar', 'Nepali', 'Norwegian', 'Nynorsk', 'Occitan', 'Panjabi', 'Pashto', 
    'Persian', 'Polish', 'Portuguese', 'Punjabi', 'Pushto', 'Romanian', 'Russian', 'Sanskrit', 'Serbian', 
    'Shona', 'Sindhi', 'Sinhala', 'Sinhalese', 'Slovak', 'Slovenian', 'Somali', 'Spanish', 'Sundanese', 
    'Swahili', 'Swedish', 'Tagalog', 'Tajik', 'Tamil', 'Tatar', 'Telugu', 'Thai', 'Tibetan', 'Turkish', 
    'Turkmen', 'Ukrainian', 'Urdu', 'Uzbek', 'Valencian', 'Vietnamese', 'Welsh', 'Yiddish', 'Yoruba'
]

XTTS_LANGUAGES = ["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"]
FISHS2_LANGUAGES = list(XTTS_LANGUAGES)
VOXTRAL_LANGUAGES = ["ar", "en", "de", "es", "fr", "hi", "it", "nl", "pt"]
KOKORO_LANGUAGES = ["en", "en-gb", "de", "es", "fr", "hi", "it", "ja", "pt", "zh-cn"]
QWEN_LANGUAGES = ["zh-cn", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"]

MAGPIE_LANGUAGES = ["en", "es", "de", "fr", "vi", "it", "zh", "hi", "ja"]
MAGPIE_DEFAULT_MODEL = "magpie-tts-multilingual"
MAGPIE_TTS_MODELS = ["magpie-tts-multilingual"]
MAGPIE_API_BASE_URL = "http://127.0.0.1:8030"
MAGPIE_LOCALE_MAP = {
    "EN-US": "en", "ES-US": "es", "FR-FR": "fr", "DE-DE": "de",
    "VI-VN": "vi", "IT-IT": "it", "ZH-CN": "zh", "HI-IN": "hi", "JA-JP": "ja",
}
MAGPIE_SPEAKERS = ["Sofia", "Aria", "Jason", "Leo", "John Van Stan"]
MAGPIE_EMOTIONS = ["Angry", "Calm", "Happy", "Neutral", "Sad", "Fearful"]
MAGPIE_LOCALES_WITH_EMOTIONS = {"EN-US"}


def magpie_voice_catalog() -> list[str]:
    voices = []
    for locale in MAGPIE_LOCALE_MAP:
        for speaker in MAGPIE_SPEAKERS:
            voices.append(f"Magpie-Multilingual.{locale}.{speaker}")
            if locale in MAGPIE_LOCALES_WITH_EMOTIONS:
                for emotion in MAGPIE_EMOTIONS:
                    voices.append(f"Magpie-Multilingual.{locale}.{speaker}.{emotion}")
    return voices

KOKORO_PREFIX_LANGUAGE_CODES = {
    "a": "en",
    "b": "en-gb",
    "d": "de",
    "e": "es",
    "f": "fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt",
    "z": "zh-cn",
}

KOKORO_VOICE_LANGUAGE_GROUPS = {
    "a": "American English",
    "b": "British English",
    "d": "German",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Portuguese",
    "z": "Chinese (Simplified)",
}

KOKORO_OPENAI_ALIAS_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "cedar",
    "coral",
    "echo",
    "fable",
    "marin",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
}

KOKORO_NAMED_VOICE_META: dict[str, tuple[str, str]] = {
    "martin": ("d", "m"),
}

LANGUAGE_DISPLAY_NAMES = {
    "ar": "Arabic",
    "bg": "Bulgarian",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "en-gb": "English (British)",
    "en-us": "English (American)",
    "es": "Spanish",
    "et": "Estonian",
    "fi": "Finnish",
    "fr": "French",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mt": "Maltese",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sv": "Swedish",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "zh-cn": "Chinese (Simplified)",
}
