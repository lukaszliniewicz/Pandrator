import base64
import copy
import io
import json
import logging
import math
import os
import re
import time
import wave
from contextlib import ExitStack, contextmanager
from queue import Queue
from threading import Event, Lock, RLock, Thread
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlunparse
from xml.sax.saxutils import escape as escape_xml

import requests
from pydub import AudioSegment

from ..constants import (
    KOKORO_NAMED_VOICE_META,
    KOKORO_OPENAI_ALIAS_VOICES,
    KOKORO_PREFIX_LANGUAGE_CODES,
    MAGPIE_TTS_MODELS,
    SILERO_LANGUAGES,
    magpie_voice_catalog,
)
from .retry_utils import (
    retry_after_seconds,
    retry_delay_seconds,
    retryable_error,
    status_code_from_error,
    wait_for_retry,
)
from .tts_provider_profiles import (
    AUDIO_CPP_MODEL_CATALOG,
    AUDIO_CPP_MODEL_VOICE_MODES,
    AUDIO_CPP_PREBUILT_VOICES,
    AZURE_SPEECH_ADAPTER,
    AZURE_SPEECH_MODELS,
    AZURE_SPEECH_OUTPUT_FORMAT,
    AZURE_SPEECH_VOICE_CATALOGUES,
    AZURE_SPEECH_VOICES,
)

_litellm_speech = None
_litellm_speech_import_attempted = False
_litellm_speech_import_error: BaseException | None = None
_litellm_speech_import_lock = Lock()

# audio.cpp keeps resident model state, so requests to one normalized endpoint
# must never overlap.  RLocks intentionally allow the ordered batch adapter to
# hold the endpoint lock while each item enters text_to_audio again.
_audio_cpp_endpoint_locks_guard = Lock()
_audio_cpp_endpoint_locks: dict[str, RLock] = {}
AUDIO_CPP_API_BASE_URL = "http://127.0.0.1:8060"
AUDIO_CPP_MAX_REFERENCE_BYTES = 5 * 1024 * 1024


def _import_litellm_speech_client():
    from litellm import speech as litellm_speech

    return litellm_speech


def _get_litellm_speech_client():
    global _litellm_speech, _litellm_speech_import_attempted
    global _litellm_speech_import_error
    if _litellm_speech_import_attempted:
        return _litellm_speech

    with _litellm_speech_import_lock:
        if not _litellm_speech_import_attempted:
            try:
                _litellm_speech = _import_litellm_speech_client()
            except Exception as error:  # pragma: no cover - runtime dependency guard
                _litellm_speech_import_error = error
                logging.warning(
                    "LiteLLM speech support could not be loaded (%s): %s",
                    type(error).__name__,
                    error,
                )
            else:
                _litellm_speech_import_error = None
            finally:
                _litellm_speech_import_attempted = True

    return _litellm_speech


# XTTS default URLs
XTTS_API_BASE_URL = "http://127.0.0.1:8020"

# VoxCPM default URLs
VOXCPM_API_BASE_URL = "http://127.0.0.1:8020"

# FishS2 default URLs
FISHS2_API_BASE_URL = "http://127.0.0.1:8020"

# Chatterbox default URLs
CHATTERBOX_API_BASE_URL = "http://127.0.0.1:8040"

# Kobold Qwen default URLs
KOBOLD_QWEN_API_BASE_URL = "http://127.0.0.1:8042"

# Voxtral default URLs
VOXTRAL_API_BASE_URL = "http://127.0.0.1:8000"

# Silero default URLs
SILERO_API_BASE_URL = "http://127.0.0.1:8001"
SILERO_DEFAULT_MODEL = "v5_cis_base_nostress"
SILERO_TTS_MODELS = [
    "v5_cis_base_nostress",
    "v5_cis_ext",
    "v5_5_ru",
    "v3_en",
    "v3_en_indic",
    "v3_de",
    "v3_es",
    "v3_fr",
    "v3_indic",
]

# Kokoro default URLs
KOKORO_API_BASE_URL = "http://127.0.0.1:8880"

# Magpie default URLs
MAGPIE_API_BASE_URL = "http://127.0.0.1:8030"
TTS_GENERATION_TIMEOUT_SECONDS = 300
# A first Qwen CustomVoice request may need to download several gigabytes and
# then restart KoboldCpp with the newly selected model.  Keep the request alive
# for that one-time preparation instead of failing at the normal TTS timeout.
KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS = 1800

XTTS_OPENAI_PLACEHOLDER_API_KEY = "sk-placeholder"
XTTS_DEFAULT_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
XTTS_UPLOAD_FILE_PURPOSE = "user_data"
XTTS_DISCOVERABLE_FILE_PURPOSES = ("user_data", "assistants")
VOXCPM_DEFAULT_MODEL = "openbmb/VoxCPM2"
VOXCPM_MODEL_ALIAS = "voxcpm2"
VOXCPM_DEFAULT_VOICE = "default"
VOXCPM_DEFAULT_CFG_VALUE = 1.5
VOXCPM_DEFAULT_INFERENCE_TIMESTEPS = 15
VOXCPM_DEFAULT_NORMALIZE = False
VOXCPM_DEFAULT_DENOISE = False
VOXCPM_DEFAULT_RETRY_BADCASE = True
VOXCPM_DEFAULT_RETRY_BADCASE_MAX_TIMES = 3
VOXCPM_DEFAULT_RETRY_BADCASE_RATIO_THRESHOLD = 6.0
VOXCPM_DEFAULT_MIN_LEN = 2
VOXCPM_DEFAULT_MAX_LEN = 4096
VOXCPM_TTS_MODELS = [VOXCPM_DEFAULT_MODEL, VOXCPM_MODEL_ALIAS]
VOXCPM_UPLOAD_FILE_PURPOSE = "user_data"
FISHS2_DEFAULT_MODEL = "fishaudio/s2-pro"
FISHS2_MODEL_ALIASES = [
    "fishs2",
    "fish-s2",
    "s2-pro",
]
FISHS2_DEFAULT_VOICE = "default"
FISHS2_UPLOAD_FILE_PURPOSE = "user_data"
FISHS2_DEFAULT_TEMPERATURE = 0.7

# Chatterbox default models
CHATTERBOX_DEFAULT_MODEL = "chatterbox-turbo"
CHATTERBOX_TTS_MODELS = [
    "chatterbox-turbo",
    "chatterbox-multilingual",
    "chatterbox-en",
]
KOBOLD_QWEN_DEFAULT_MODEL = "Prebuilt Voices"
KOBOLD_QWEN_DEFAULT_VOICE = "Aiden"
KOBOLD_QWEN_TTS_MODELS = ["Prebuilt Voices", "Voice Cloning"]
KOBOLD_QWEN_SAMPLE_VOICE = "kobo"
KOBOLD_QWEN_TTS_VOICES = [
    "Aiden",
    "Dylan",
    "Eric",
    "Ono_Anna",
    "Ryan",
    "Serena",
    "Sohee",
    "Uncle_Fu",
    "Vivian",
]
VOICE_CLONING_SERVICE_IDS = {
    "xtts",
    "voxcpm",
    "fishs2",
    "chatterbox",
    "kobold_qwen",
}
# These first-party wrappers expose idempotent deletion for uploaded reference
# voices. Keep this separate from cloning support so custom/external services do
# not inherit destructive capabilities merely because they accept uploads.
VOICE_DELETION_SERVICE_IDS = set(VOICE_CLONING_SERVICE_IDS)
VOICE_REFERENCE_TEXT_MODES = {
    "xtts": "ignored",
    "voxcpm": "optional",
    "fishs2": "required",
    "chatterbox": "ignored",
    "kobold_qwen": "ignored",
}
FISHS2_DEFAULT_TOP_P = 0.7
FISHS2_DEFAULT_CHUNK_LENGTH = 200
FISHS2_DEFAULT_LATENCY = "balanced"
FISHS2_DEFAULT_NORMALIZE = True
FISHS2_DEFAULT_PROSODY_VOLUME = 0.0
FISHS2_DEFAULT_NORMALIZE_LOUDNESS = True
VOXTRAL_DEFAULT_MODEL = "auto"
VOXTRAL_DEFAULT_VOICE = "casual_female"
VOXTRAL_INSTRUCTIONS_PREFIX = "voxtral_options:"
VOXTRAL_TTS_MODELS = ["auto", "gguf", "bf16"]
KOKORO_DEFAULT_MODEL = "kokoro"
KOKORO_DEFAULT_VOICE = "af_heart"
KOKORO_TTS_MODELS = [
    "kokoro",
    "tts-1",
    "tts-1-hd",
    "gpt-4o-mini-tts",
]
KOKORO_TTS_VOICES = [
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
    "dm_martin",
    "ef_dora",
    "em_alex",
    "em_santa",
    "ff_siwis",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
    "if_sara",
    "im_nicola",
    "jf_alpha",
    "jf_gongitsune",
    "jf_nezumi",
    "jf_tebukuro",
    "jm_kumo",
    "pf_dora",
    "pm_alex",
    "pm_santa",
    "zf_xiaobei",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunjian",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
]


def normalize_kokoro_language_code(language_value: str | None) -> str:
    normalized = str(language_value or "").strip().lower()
    if not normalized:
        return ""

    aliases = {
        "en-us": "en",
        "pt-br": "pt",
        "fr-fr": "fr",
        "zh": "zh-cn",
    }
    return aliases.get(normalized, normalized)


CHATTERBOX_LANGUAGE_CODES = {
    "ar",
    "da",
    "de",
    "el",
    "en",
    "es",
    "fi",
    "fr",
    "he",
    "hi",
    "it",
    "ja",
    "ko",
    "ms",
    "nl",
    "no",
    "pl",
    "pt",
    "ru",
    "sv",
    "sw",
    "tr",
    "zh",
}


def normalize_chatterbox_language_code(language_value: str | None) -> str:
    """Collapse regional tags when Chatterbox supports their base language."""
    normalized = str(language_value or "").strip().lower().replace("_", "-")
    if not normalized:
        return "en"
    base_language = normalized.split("-", 1)[0]
    return base_language if base_language in CHATTERBOX_LANGUAGE_CODES else normalized


def _strip_kokoro_weight_suffix(voice_token: str) -> str:
    trimmed = str(voice_token or "").strip()
    weighted_match = re.fullmatch(r"(.+?)(\(\s*\d+(?:\.\d+)?\s*\))", trimmed)
    if not weighted_match:
        return trimmed
    return weighted_match.group(1).strip()


def _infer_kokoro_voice_component_language_code(voice_token: str) -> str:
    token_without_weight = _strip_kokoro_weight_suffix(voice_token)
    prefix, separator, _ = token_without_weight.partition("_")
    normalized_prefix = prefix.lower().strip()

    if separator and len(normalized_prefix) == 2:
        return KOKORO_PREFIX_LANGUAGE_CODES.get(normalized_prefix[0], "")

    if normalized_prefix in KOKORO_OPENAI_ALIAS_VOICES and not separator:
        return "en"

    if not separator and normalized_prefix in KOKORO_NAMED_VOICE_META:
        lang_key, _gender_key = KOKORO_NAMED_VOICE_META[normalized_prefix]
        return KOKORO_PREFIX_LANGUAGE_CODES.get(lang_key, "")

    return ""


def infer_kokoro_voice_language_code(voice_id: str | None) -> str:
    normalized_voice_id = str(voice_id or "").strip()
    if not normalized_voice_id:
        return ""

    parts = [part.strip() for part in normalized_voice_id.split("+") if part.strip()]
    if not parts:
        return ""

    language_codes = [
        _infer_kokoro_voice_component_language_code(part) for part in parts
    ]
    language_codes = [code for code in language_codes if code]
    if not language_codes:
        return ""

    first_language = language_codes[0]
    if all(code == first_language for code in language_codes):
        return first_language

    return ""


OPENAI_AUDIO_DEFAULT_MODEL = "gpt-4o-mini-tts"
OPENAI_AUDIO_DEFAULT_VOICE = "alloy"
GEMINI_AUDIO_DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_AUDIO_DEFAULT_VOICE = "Kore"
VERTEX_AUDIO_DEFAULT_LOCATION = "us-central1"
OPENAI_AUDIO_BASE_URL = "https://api.openai.com/v1"
GEMINI_AUDIO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
ELEVENLABS_API_BASE_URL = "https://api.elevenlabs.io"

OPENAI_SERVICE = "OpenAI"
GEMINI_SERVICE = "Google Gemini"
VERTEX_SERVICE = "Google Vertex AI"
ELEVENLABS_SERVICE = "ElevenLabs"
LEGACY_GEMINI_SERVICE = "Gemini"
OPENAI_COMPAT_SERVICE = "Custom"
LEGACY_OPENAI_COMPAT_SERVICE = "OpenAI-Compatible"

OPENAI_PROVIDER = "openai"
GEMINI_PROVIDER = "gemini"
VERTEX_PROVIDER = "vertex_ai"
ELEVENLABS_PROVIDER = "elevenlabs"
AZURE_SPEECH_PROVIDER = "azure"
SUPPORTED_AUDIO_PROVIDERS = {
    OPENAI_PROVIDER,
    GEMINI_PROVIDER,
    ELEVENLABS_PROVIDER,
    AZURE_SPEECH_PROVIDER,
}

OPENAI_TTS_MODELS = [
    "gpt-4o-mini-tts",
    "tts-1-hd",
    "tts-1",
]

OPENAI_GENERATION_PROMPT_MODELS = ["gpt-4o-mini-tts"]

GEMINI_TTS_MODELS = [
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-tts",
    "gemini-2.5-pro-tts",
]

ELEVENLABS_TTS_DEFAULT_MODEL = "eleven_multilingual_v2"
ELEVENLABS_TTS_OUTPUT_FORMAT = "mp3_44100_128"
# ElevenLabs documents ``language_code`` as unsupported for this model. Keep
# this an explicit exception instead of maintaining a closed allow-list: the
# catalogue can gain models without making an otherwise valid synthesis fail.
ELEVENLABS_MODELS_WITHOUT_LANGUAGE_CODE = frozenset({"eleven_multilingual_v2"})


class ElevenLabsCatalogError(RuntimeError):
    """Safe, status-aware failure from an ElevenLabs catalogue endpoint."""

    def __init__(self, operation: str, status_code: int = 0):
        self.operation = operation
        self.status_code = int(status_code or 0)
        if self.status_code in {401, 403}:
            message = f"ElevenLabs API key was rejected while listing {operation}."
        elif self.status_code == 429:
            message = f"ElevenLabs rate limit reached while listing {operation}."
        elif self.status_code >= 500:
            message = f"ElevenLabs returned HTTP {self.status_code} while listing {operation}."
        elif self.status_code:
            message = f"ElevenLabs returned HTTP {self.status_code} while listing {operation}."
        else:
            message = f"Could not reach ElevenLabs while listing {operation}."
        super().__init__(message)


def normalize_elevenlabs_language_code(language_value: object) -> str:
    """Return an ElevenLabs ISO 639-1 code for a concrete locale value.

    Pandrator language selectors may contain a region (for example ``en-US``)
    while the native ElevenLabs request expects a two-letter ISO 639-1 code.
    Keep this deliberately narrow: automatic/undetermined values and values
    that are not a simple language or language-region tag are omitted so the
    provider can retain its own language detection.
    """
    normalized = str(language_value or "").strip().lower().replace("_", "-")
    if normalized in {"", "auto", "und"}:
        return ""
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]{2,8})?", normalized):
        return ""
    return normalized.split("-", 1)[0]


GENERATION_PROMPT_MODELS_FIELD = "generation_prompt_models"
KOBOLD_QWEN_GENERATION_PROMPT_MODELS = ["Prebuilt Voices", "qwen3-tts-customvoice"]

OPENAI_TTS_VOICES = [
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
]

OPENAI_TTS_CLASSIC_VOICES = [
    "alloy",
    "ash",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
]

GEMINI_TTS_VOICES = [
    "Achernar",
    "Achird",
    "Algenib",
    "Algieba",
    "Alnilam",
    "Aoede",
    "Autonoe",
    "Callirrhoe",
    "Charon",
    "Despina",
    "Enceladus",
    "Erinome",
    "Fenrir",
    "Gacrux",
    "Iapetus",
    "Kore",
    "Laomedeia",
    "Leda",
    "Orus",
    "Pulcherrima",
    "Puck",
    "Rasalgethi",
    "Sadachbia",
    "Sadaltager",
    "Schedar",
    "Sulafat",
    "Umbriel",
    "Vindemiatrix",
    "Zephyr",
    "Zubenelgenubi",
]

GEMINI_MODEL_ALIASES = {
    "gemini-3.1-flash-tts": "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts": "gemini-2.5-flash-tts",
    "gemini-2.5-pro-preview-tts": "gemini-2.5-pro-tts",
}

# Speech APIs do not expose billing metadata in their audio responses. These
# official public list prices therefore produce an explicit estimate; custom
# service configurations can override them through a ``pricing`` mapping.
DEFAULT_TTS_PRICING = {
    "gpt-4o-mini-tts": {
        "input_cost_per_million_tokens": 0.60,
        "output_cost_per_million_audio_tokens": 12.0,
        "audio_tokens_per_second": 20.833333,
    },
    "tts-1": {"input_cost_per_million_characters": 15.0},
    "tts-1-hd": {"input_cost_per_million_characters": 30.0},
    "gemini-3.1-flash-tts-preview": {
        "input_cost_per_million_tokens": 1.0,
        "output_cost_per_million_audio_tokens": 20.0,
        "audio_tokens_per_second": 25.0,
    },
    "gemini-2.5-flash-tts": {
        "input_cost_per_million_tokens": 0.50,
        "output_cost_per_million_audio_tokens": 10.0,
        "audio_tokens_per_second": 25.0,
    },
    "gemini-2.5-pro-tts": {
        "input_cost_per_million_tokens": 1.0,
        "output_cost_per_million_audio_tokens": 20.0,
        "audio_tokens_per_second": 25.0,
    },
}

FIRST_CLASS_SERVICE_ORDER = [
    "audio_cpp",
    "xtts",
    "voxcpm",
    "fishs2",
    "voxtral",
    "kokoro",
    "magpie",
    "silero",
    "chatterbox",
    "kobold_qwen",
    OPENAI_PROVIDER,
    GEMINI_PROVIDER,
    VERTEX_PROVIDER,
    ELEVENLABS_PROVIDER,
]
FIRST_CLASS_SERVICE_IDS = set(FIRST_CLASS_SERVICE_ORDER)
FIRST_CLASS_SERVICE_NAMES = {
    "audio_cpp": "audio.cpp",
    "xtts": "XTTS",
    "voxcpm": "VoxCPM",
    "fishs2": "FishS2",
    "voxtral": "Voxtral",
    "kokoro": "Kokoro",
    "magpie": "Magpie",
    "silero": "Silero",
    "chatterbox": "Chatterbox",
    "kobold_qwen": "Qwen3 TTS",
    OPENAI_PROVIDER: OPENAI_SERVICE,
    GEMINI_PROVIDER: GEMINI_SERVICE,
    VERTEX_PROVIDER: VERTEX_SERVICE,
    ELEVENLABS_PROVIDER: ELEVENLABS_SERVICE,
}
SERVICE_ID_ALIASES = {
    "audio.cpp": "audio_cpp",
    "audio-cpp": "audio_cpp",
    "audiocpp": "audio_cpp",
    "voxcpm2": "voxcpm",
    "voxcpm-2": "voxcpm",
    "fish-s2": "fishs2",
    "fishs2-cpp": "fishs2",
    "google": GEMINI_PROVIDER,
    "google-gemini": GEMINI_PROVIDER,
    "gemini": GEMINI_PROVIDER,
    "vertex": VERTEX_PROVIDER,
    "vertex-ai": VERTEX_PROVIDER,
    "google-vertex-ai": VERTEX_PROVIDER,
    "eleven-labs": ELEVENLABS_PROVIDER,
    "eleven_labs": ELEVENLABS_PROVIDER,
    "elevenlabs": ELEVENLABS_PROVIDER,
    "kobold-qwen": "kobold_qwen",
    "koboldqwen": "kobold_qwen",
    "qwen": "kobold_qwen",
    "qwen3": "kobold_qwen",
    "qwen3-tts": "kobold_qwen",
}
PREBUILT_VOICE_PROVIDER_FIELD = "supports_prebuilt_voices"
OPENAI_COMPAT_ADAPTER = "openai_compatible"
AUDIO_CPP_ADAPTER = "audio_cpp"
GENERIC_JSON_ADAPTER = "generic_json"
ELEVENLABS_NATIVE_ADAPTER = "elevenlabs_native"
SUPPORTED_CUSTOM_TTS_ADAPTERS = {
    OPENAI_COMPAT_ADAPTER,
    AUDIO_CPP_ADAPTER,
    GENERIC_JSON_ADAPTER,
    ELEVENLABS_NATIVE_ADAPTER,
    AZURE_SPEECH_ADAPTER,
}


def _read_setting(settings, key: str, default=None):
    if settings is None:
        return default
    if isinstance(settings, dict):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _normalize_custom_adapter(raw_adapter: object) -> str:
    normalized = str(raw_adapter or "").strip().lower().replace("-", "_")
    aliases = {
        "openai": OPENAI_COMPAT_ADAPTER,
        "openai_compatible": OPENAI_COMPAT_ADAPTER,
        "audio_cpp": AUDIO_CPP_ADAPTER,
        "audiocpp": AUDIO_CPP_ADAPTER,
        "generic": GENERIC_JSON_ADAPTER,
        "json": GENERIC_JSON_ADAPTER,
        "generic_json": GENERIC_JSON_ADAPTER,
        "elevenlabs": ELEVENLABS_NATIVE_ADAPTER,
        "elevenlabs_native": ELEVENLABS_NATIVE_ADAPTER,
        "eleven_labs": ELEVENLABS_NATIVE_ADAPTER,
        "azure": AZURE_SPEECH_ADAPTER,
        "azure_speech": AZURE_SPEECH_ADAPTER,
    }
    return aliases.get(normalized, OPENAI_COMPAT_ADAPTER)


def _normalize_adapter_config(raw_config) -> dict[str, object]:
    config = raw_config if isinstance(raw_config, dict) else {}
    adapter = _normalize_custom_adapter(config.get("adapter"))
    request_fields = config.get("request_fields", {})
    if not isinstance(request_fields, dict):
        request_fields = {}
    normalized_fields = {
        key: str(request_fields.get(key) or "").strip()
        for key in ("text", "model", "voice", "speed", "format")
    }
    if adapter in {OPENAI_COMPAT_ADAPTER, AUDIO_CPP_ADAPTER}:
        normalized_fields = {
            "text": "input",
            "model": "model",
            "voice": "voice",
            "speed": "speed",
            "format": "response_format",
        }

    request_defaults = config.get("request_defaults", {})
    if not isinstance(request_defaults, dict):
        request_defaults = {}
    normalized_defaults = {
        str(key): value
        for key, value in request_defaults.items()
        if str(key).strip() and isinstance(value, (str, int, float, bool, type(None)))
    }

    metadata: dict[str, object] = {}
    for key in (
        "model_catalog",
        "model_voice_modes",
        "voice_catalogues",
        "voice_metadata",
        "default_voices",
        "default_voices_by_language",
        "pricing",
    ):
        value = config.get(key)
        if isinstance(value, (dict, list)):
            metadata[key] = copy.deepcopy(value)
    generation_prompt_models = config.get(GENERATION_PROMPT_MODELS_FIELD)
    if isinstance(generation_prompt_models, list):
        metadata[GENERATION_PROMPT_MODELS_FIELD] = [
            str(model).strip()
            for model in generation_prompt_models
            if str(model).strip()
        ]

    normalized = {
        "adapter": adapter,
        "profile_id": str(config.get("profile_id") or "").strip(),
        "speech_path": str(config.get("speech_path") or "").strip(),
        "models_path": str(config.get("models_path") or "").strip(),
        "voices_path": str(config.get("voices_path") or "").strip(),
        "request_fields": normalized_fields,
        "request_defaults": normalized_defaults,
        "auth_mode": str(config.get("auth_mode") or "bearer").strip().lower(),
        "direct_http": _coerce_bool(config.get("direct_http"), False),
        "credential_required": _coerce_bool(config.get("credential_required"), False),
        "voice_reference_text": str(
            config.get("voice_reference_text") or "ignored"
        ).strip()
        or "ignored",
    }
    for key in ("supports_voice_cloning", "supports_voice_deletion"):
        if key in config:
            normalized[key] = _coerce_bool(config.get(key), False)
    key_env = str(config.get("api_key_env") or "").strip()
    if key_env:
        normalized["api_key_env"] = key_env
    normalized.update(metadata)
    return normalized


def _normalize_provider_id(raw_value: str | None) -> str:
    lowered = str(raw_value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def _parse_model_list(raw_models, provider: str) -> list[str]:
    candidates: list[str] = []
    if isinstance(raw_models, list):
        candidates = [str(item) for item in raw_models]
    elif isinstance(raw_models, str):
        candidates = [str(item) for item in re.split(r"[,\n;]", raw_models)]

    models: list[str] = []
    for model in candidates:
        normalized = _normalize_model_for_provider(model, provider)
        if normalized:
            models.append(normalized)

    return _dedupe_ordered(models)


def _parse_voice_list(raw_voices, provider: str) -> list[str]:
    candidates: list[str] = []
    if isinstance(raw_voices, list):
        candidates = [str(item) for item in raw_voices]
    elif isinstance(raw_voices, str):
        candidates = [str(item) for item in re.split(r"[,\n;]", raw_voices)]

    voices: list[str] = []
    for voice in candidates:
        normalized = _normalize_voice_for_provider(voice, provider)
        if normalized:
            voices.append(normalized)

    return _dedupe_ordered(voices)


def _default_service_configs() -> list[dict[str, object]]:
    local_services = [
        ("audio_cpp", AUDIO_CPP_API_BASE_URL),
        ("xtts", XTTS_API_BASE_URL),
        ("voxcpm", VOXCPM_API_BASE_URL),
        ("fishs2", FISHS2_API_BASE_URL),
        ("voxtral", VOXTRAL_API_BASE_URL),
        ("kokoro", KOKORO_API_BASE_URL),
        ("magpie", MAGPIE_API_BASE_URL),
        ("silero", SILERO_API_BASE_URL),
        ("chatterbox", CHATTERBOX_API_BASE_URL),
        ("kobold_qwen", KOBOLD_QWEN_API_BASE_URL),
    ]
    local_catalogues: dict[str, tuple[list[str], str, list[str], str, bool]] = {
        "audio_cpp": (
            ["qwen3_tts_1_7b_base_q8_0"],
            "qwen3_tts_1_7b_base_q8_0",
            [],
            "",
            True,
        ),
        "xtts": ([XTTS_DEFAULT_MODEL], XTTS_DEFAULT_MODEL, [], "", False),
        "voxcpm": (
            list(VOXCPM_TTS_MODELS),
            VOXCPM_DEFAULT_MODEL,
            [VOXCPM_DEFAULT_VOICE],
            VOXCPM_DEFAULT_VOICE,
            False,
        ),
        # Fish's API advertises several compatibility aliases, but all of
        # them address the same S2 Pro model. Quantization is a service
        # configuration choice, not a per-request model.
        "fishs2": (
            [FISHS2_DEFAULT_MODEL],
            FISHS2_DEFAULT_MODEL,
            [FISHS2_DEFAULT_VOICE],
            FISHS2_DEFAULT_VOICE,
            False,
        ),
        "voxtral": (
            list(VOXTRAL_TTS_MODELS),
            VOXTRAL_DEFAULT_MODEL,
            [VOXTRAL_DEFAULT_VOICE],
            VOXTRAL_DEFAULT_VOICE,
            True,
        ),
        "kokoro": (
            list(KOKORO_TTS_MODELS),
            KOKORO_DEFAULT_MODEL,
            list(KOKORO_TTS_VOICES),
            KOKORO_DEFAULT_VOICE,
            True,
        ),
        "magpie": (
            list(MAGPIE_TTS_MODELS),
            MAGPIE_TTS_MODELS[0],
            magpie_voice_catalog(),
            magpie_voice_catalog()[0],
            True,
        ),
        "silero": (list(SILERO_TTS_MODELS), SILERO_DEFAULT_MODEL, [], "", True),
        "chatterbox": (
            list(CHATTERBOX_TTS_MODELS),
            CHATTERBOX_DEFAULT_MODEL,
            [],
            "",
            False,
        ),
        "kobold_qwen": (
            list(KOBOLD_QWEN_TTS_MODELS),
            KOBOLD_QWEN_DEFAULT_MODEL,
            list(KOBOLD_QWEN_TTS_VOICES),
            KOBOLD_QWEN_DEFAULT_VOICE,
            True,
        ),
    }
    configs: list[dict[str, object]] = []
    for service_id, api_base in local_services:
        models, default_model, voices, default_voice, prebuilt = local_catalogues[
            service_id
        ]
        record = {
            "id": service_id,
            "name": FIRST_CLASS_SERVICE_NAMES[service_id],
            "kind": "local",
            "api_base": api_base,
            "models": models,
            "default_model": default_model,
            "voices": voices,
            "default_voice": default_voice,
            "voice_catalogues": {default_model: voices} if default_model else {},
            "default_voices": {default_model: default_voice}
            if default_model and default_voice
            else {},
            PREBUILT_VOICE_PROVIDER_FIELD: prebuilt,
            "supports_voice_cloning": service_id in VOICE_CLONING_SERVICE_IDS,
            "supports_voice_deletion": service_id in VOICE_DELETION_SERVICE_IDS,
            "voice_reference_text": VOICE_REFERENCE_TEXT_MODES.get(
                service_id, "ignored"
            ),
            "model_voice_modes": {
                model: "prebuilt" if prebuilt else "cloning" for model in models
            },
        }
        if service_id == "audio_cpp":
            record.update(
                {
                    "provider": "audio_cpp",
                    "adapter": AUDIO_CPP_ADAPTER,
                    "speech_path": "/v1/audio/speech",
                    "models_path": "/v1/models",
                    "voices_path": "/v1/audio/voices",
                    "auth_mode": "none",
                    "direct_http": True,
                    "credential_required": False,
                    "supports_dynamic_catalog": True,
                    "supports_voice_cloning": True,
                    "supports_voice_deletion": False,
                    "voice_reference_text": "optional",
                    "model_catalog": copy.deepcopy(AUDIO_CPP_MODEL_CATALOG),
                    "model_voice_modes": copy.deepcopy(AUDIO_CPP_MODEL_VOICE_MODES),
                    "voice_catalogues": {
                        "qwen3_tts_1_7b_customvoice_q8_0": list(KOBOLD_QWEN_TTS_VOICES),
                        "magpie_tts_q8_0": magpie_voice_catalog(),
                        "pocket_tts_english_q8_0": ["alba"],
                    },
                    "default_voices": {
                        "qwen3_tts_1_7b_customvoice_q8_0": KOBOLD_QWEN_DEFAULT_VOICE,
                        "magpie_tts_q8_0": magpie_voice_catalog()[0],
                        "pocket_tts_english_q8_0": "alba",
                    },
                }
            )
        if service_id == "kobold_qwen":
            # Qwen exposes two different model capabilities through one API.
            # Keep their catalogues separate so clients never offer a preset to
            # the Base model or an uploaded reference to CustomVoice.  KoboldCpp
            # ships the ``kobo`` reference internally even before a user uploads
            # a WAV, so seed it as the cloning model's usable sample voice.
            record["voice_catalogues"] = {
                KOBOLD_QWEN_DEFAULT_MODEL: list(KOBOLD_QWEN_TTS_VOICES),
                "Voice Cloning": [KOBOLD_QWEN_SAMPLE_VOICE],
            }
            record["default_voices"] = {
                KOBOLD_QWEN_DEFAULT_MODEL: KOBOLD_QWEN_DEFAULT_VOICE,
                "Voice Cloning": KOBOLD_QWEN_SAMPLE_VOICE,
            }
            record["model_voice_modes"] = {
                KOBOLD_QWEN_DEFAULT_MODEL: "prebuilt",
                "Voice Cloning": "cloning",
            }
            record[GENERATION_PROMPT_MODELS_FIELD] = list(
                KOBOLD_QWEN_GENERATION_PROMPT_MODELS
            )
        configs.append(record)
    configs.extend(
        [
            {
                "id": OPENAI_PROVIDER,
                "name": "OpenAI",
                "kind": "commercial",
                "provider": OPENAI_PROVIDER,
                "api_base": OPENAI_AUDIO_BASE_URL,
                "api_key_env": "OPENAI_API_KEY",
                "api_key": "",
                "is_custom": False,
                "models": list(OPENAI_TTS_MODELS),
                "default_model": OPENAI_AUDIO_DEFAULT_MODEL,
                "voices": list(OPENAI_TTS_VOICES),
                "default_voice": OPENAI_AUDIO_DEFAULT_VOICE,
                GENERATION_PROMPT_MODELS_FIELD: list(OPENAI_GENERATION_PROMPT_MODELS),
                PREBUILT_VOICE_PROVIDER_FIELD: True,
                "pricing": copy.deepcopy(DEFAULT_TTS_PRICING),
            },
            {
                "id": GEMINI_PROVIDER,
                "name": GEMINI_SERVICE,
                "kind": "commercial",
                "provider": GEMINI_PROVIDER,
                "api_base": GEMINI_AUDIO_BASE_URL,
                "api_key_env": "GEMINI_API_KEY",
                "api_key": "",
                "is_custom": False,
                "models": list(GEMINI_TTS_MODELS),
                "default_model": GEMINI_AUDIO_DEFAULT_MODEL,
                "voices": list(GEMINI_TTS_VOICES),
                "default_voice": GEMINI_AUDIO_DEFAULT_VOICE,
                GENERATION_PROMPT_MODELS_FIELD: list(GEMINI_TTS_MODELS),
                PREBUILT_VOICE_PROVIDER_FIELD: True,
                "pricing": copy.deepcopy(DEFAULT_TTS_PRICING),
            },
            {
                "id": VERTEX_PROVIDER,
                "name": VERTEX_SERVICE,
                "kind": "commercial",
                "provider": VERTEX_PROVIDER,
                "api_base": "https://aiplatform.googleapis.com",
                "api_key_env": "",
                "api_key": "",
                "is_custom": False,
                "models": list(GEMINI_TTS_MODELS),
                "default_model": GEMINI_AUDIO_DEFAULT_MODEL,
                "voices": list(GEMINI_TTS_VOICES),
                "default_voice": GEMINI_AUDIO_DEFAULT_VOICE,
                "vertex_project": "",
                "vertex_location": VERTEX_AUDIO_DEFAULT_LOCATION,
                GENERATION_PROMPT_MODELS_FIELD: list(GEMINI_TTS_MODELS),
                PREBUILT_VOICE_PROVIDER_FIELD: True,
                "pricing": copy.deepcopy(DEFAULT_TTS_PRICING),
            },
            {
                "id": ELEVENLABS_PROVIDER,
                "name": ELEVENLABS_SERVICE,
                "description": "Native ElevenLabs text-to-speech API. Requires an ElevenLabs API key.",
                "kind": "commercial",
                "provider": ELEVENLABS_PROVIDER,
                "api_base": ELEVENLABS_API_BASE_URL,
                "api_key_env": "ELEVENLABS_API_KEY",
                "api_key": "",
                "is_custom": False,
                "adapter": ELEVENLABS_NATIVE_ADAPTER,
                "models": [ELEVENLABS_TTS_DEFAULT_MODEL],
                "default_model": ELEVENLABS_TTS_DEFAULT_MODEL,
                "voices": [],
                "default_voice": "",
                "voice_catalogues": {},
                "voice_metadata": {},
                "supports_prebuilt_voices": True,
                "credential_required": True,
            },
        ]
    )
    return configs


def _normalize_service_id(raw_value: str | None) -> str:
    service_id = _normalize_provider_id(raw_value)
    return SERVICE_ID_ALIASES.get(service_id, service_id)


def get_first_class_service_name(raw_value: str | None) -> str:
    service_id = _normalize_service_id(raw_value)
    return FIRST_CLASS_SERVICE_NAMES.get(service_id, "")


def _merge_service_config(
    base_record: dict[str, object],
    raw_record: dict,
) -> dict[str, object]:
    record = copy.deepcopy(base_record)
    service_id = str(record["id"])
    api_base = _normalize_base_url(
        raw_record.get("api_base") or raw_record.get("base_url") or "",
        "",
    )
    if api_base:
        record["api_base"] = api_base

    provider_key = str(record.get("provider") or service_id)
    record["api_key_env"] = str(
        raw_record.get("api_key_env")
        if "api_key_env" in raw_record
        else record.get("api_key_env") or ""
    ).strip()
    record["api_key"] = str(raw_record.get("api_key") or "").strip()
    if str(raw_record.get("secret_ref") or "").strip():
        record["secret_ref"] = str(raw_record["secret_ref"]).strip()

    for key in (
        "adapter",
        "profile_id",
        "speech_path",
        "models_path",
        "voices_path",
        "auth_mode",
        "vertex_project",
        "vertex_location",
        "connection_mode",
        "managed_service_id",
    ):
        if str(raw_record.get(key) or "").strip():
            record[key] = str(raw_record[key]).strip()
    if "direct_http" in raw_record:
        record["direct_http"] = _coerce_bool(raw_record.get("direct_http"), False)
    if "credential_required" in raw_record:
        record["credential_required"] = _coerce_bool(
            raw_record.get("credential_required"),
            False,
        )
    for key in ("request_fields", "request_defaults"):
        if isinstance(raw_record.get(key), dict):
            record[key] = copy.deepcopy(raw_record[key])
    for key in (
        "settings",
        "voice_catalogues",
        "voice_metadata",
        "default_voices",
        "default_voices_by_language",
        "pricing",
    ):
        if isinstance(raw_record.get(key), dict):
            record[key] = copy.deepcopy(raw_record[key])
    if PREBUILT_VOICE_PROVIDER_FIELD in raw_record:
        record[PREBUILT_VOICE_PROVIDER_FIELD] = bool(
            raw_record[PREBUILT_VOICE_PROVIDER_FIELD]
        )
    for key in (
        "model_catalog",
        "model_voice_modes",
        "voice_reference_text",
        "supports_voice_cloning",
        "supports_voice_deletion",
    ):
        if key in raw_record:
            value = raw_record[key]
            if key in {"supports_voice_cloning", "supports_voice_deletion"}:
                record[key] = bool(value)
            elif key == "voice_reference_text":
                record[key] = str(value or "").strip() or "ignored"
            elif isinstance(value, (dict, list)):
                record[key] = copy.deepcopy(value)

    models = _parse_model_list(raw_record.get("models", []), provider_key)
    if models:
        record["models"] = models
    else:
        record.setdefault("models", [])
    default_model = _normalize_model_for_provider(
        str(raw_record.get("default_model") or "").strip(),
        provider_key,
    )
    if default_model:
        record["default_model"] = default_model
        if default_model not in record["models"]:
            record["models"].insert(0, default_model)

    voices = _parse_voice_list(raw_record.get("voices", []), provider_key)
    if voices:
        record["voices"] = voices
    else:
        record.setdefault("voices", [])
    default_voice = _normalize_voice_for_provider(
        str(raw_record.get("default_voice") or "").strip(),
        provider_key,
    )
    if default_voice:
        record["default_voice"] = default_voice
        if default_voice not in record["voices"]:
            record["voices"].insert(0, default_voice)

    return record


def get_service_configs(tts_settings) -> list[dict[str, object]]:
    services = {
        str(item["id"]): copy.deepcopy(item) for item in _default_service_configs()
    }

    legacy_raw_json = str(
        _read_setting(tts_settings, "openai_audio_endpoints_json", "") or ""
    )
    for legacy_record in _legacy_endpoints_to_provider_configs(legacy_raw_json):
        service_id = _normalize_service_id(
            legacy_record.get("id") or legacy_record.get("name")
        )
        if service_id not in services:
            continue
        services[service_id] = _merge_service_config(
            services[service_id],
            legacy_record,
        )

    raw_sources = [
        _read_setting(tts_settings, "provider_configs", []),
        _read_setting(tts_settings, "service_configs", []),
    ]
    for raw_configs in raw_sources:
        if not isinstance(raw_configs, list):
            continue
        for raw_record in raw_configs:
            if not isinstance(raw_record, dict):
                continue
            service_id = _normalize_service_id(
                raw_record.get("id") or raw_record.get("name")
            )
            if service_id not in services:
                continue
            services[service_id] = _merge_service_config(
                services[service_id],
                raw_record,
            )

    first_class = [
        services[service_id]
        for service_id in FIRST_CLASS_SERVICE_ORDER
        if service_id in services
    ]
    first_class.extend(get_provider_configs(tts_settings))
    return first_class


def get_service_config(
    tts_settings, service_name_or_id: str
) -> dict[str, object] | None:
    service_id = _normalize_service_id(service_name_or_id)
    for service in get_service_configs(tts_settings):
        if str(service.get("id") or "") == service_id:
            return service
    return None


def estimate_tts_usage(
    text: str, duration_ms: int, tts_settings
) -> dict[str, object] | None:
    """Estimate billable TTS usage for a configured commercial service.

    Speech endpoints return audio bytes without token or price metadata.  The
    result is therefore intentionally marked estimated and retains the usage
    units used in the calculation for auditing in the UI.
    """
    service_name = str(_read_setting(tts_settings, "service", "") or "")
    service = get_service_config(tts_settings, service_name)
    if service is None:
        return None
    configured_pricing = _read_setting(tts_settings, "pricing", None)
    pricing = (
        configured_pricing
        if isinstance(configured_pricing, dict)
        else service.get("pricing")
    )
    if not isinstance(pricing, dict):
        pricing = {}
    model = str(
        _read_setting(tts_settings, "model", "") or service.get("default_model") or ""
    ).strip()
    model_pricing = pricing.get(model)
    if not isinstance(model_pricing, dict):
        model_pricing = {}
    commercial = str(service.get("kind") or "").lower() == "commercial" or bool(
        model_pricing
    )
    if not commercial:
        return None

    characters = len(str(text or ""))
    input_tokens = max(0, round(characters / 4))
    audio_seconds = max(0.0, float(duration_ms or 0) / 1000.0)
    audio_tokens_per_second = max(
        0.0, float(model_pricing.get("audio_tokens_per_second") or 0)
    )
    output_audio_tokens = round(audio_seconds * audio_tokens_per_second)
    cost = 0.0
    priced = False
    if model_pricing.get("input_cost_per_million_characters") is not None:
        cost += (
            characters
            * float(model_pricing["input_cost_per_million_characters"])
            / 1_000_000
        )
        priced = True
    if model_pricing.get("input_cost_per_million_tokens") is not None:
        cost += (
            input_tokens
            * float(model_pricing["input_cost_per_million_tokens"])
            / 1_000_000
        )
        priced = True
    if model_pricing.get("output_cost_per_million_audio_tokens") is not None:
        cost += (
            output_audio_tokens
            * float(model_pricing["output_cost_per_million_audio_tokens"])
            / 1_000_000
        )
        priced = True
    return {
        "provider": str(service.get("provider") or service.get("id") or service_name),
        "model": model,
        "commercial": True,
        "estimated": True,
        "cost_usd": cost if priced else None,
        "cost_source": "configured_tts_pricing"
        if configured_pricing
        else "public_list_price",
        "input_characters": characters,
        "input_tokens": input_tokens,
        "output_audio_tokens": output_audio_tokens,
        "duration_ms": max(0, int(duration_ms or 0)),
    }


def get_service_base_url(tts_settings, service_name_or_id: str) -> str:
    service = get_service_config(tts_settings, service_name_or_id)
    if service is None:
        return ""
    return str(service.get("api_base") or "").strip().rstrip("/")


def resolve_service_base_url(tts_settings, service_name_or_id: str) -> str:
    requested_service = get_first_class_service_name(service_name_or_id)
    active_service = get_first_class_service_name(
        _read_setting(tts_settings, "service", "")
    )
    if (
        requested_service
        and requested_service == active_service
        and _coerce_bool(
            _read_setting(tts_settings, "use_external_server", False), False
        )
    ):
        external_url = _normalize_base_url(
            _read_setting(tts_settings, "external_server_url", ""),
            "",
        )
        if external_url:
            return external_url

    return get_service_base_url(tts_settings, service_name_or_id)


def save_service_config(
    tts_settings,
    service_name_or_id: str,
    api_base: str,
    api_key: str = "",
    models: list[str] | str | None = None,
    voices: list[str] | str | None = None,
) -> tuple[bool, list[dict[str, object]], str]:
    service_id = _normalize_service_id(service_name_or_id)
    if service_id not in FIRST_CLASS_SERVICE_IDS:
        return (
            False,
            get_service_configs(tts_settings),
            "Select a first-class TTS service.",
        )

    normalized_api_base = _normalize_base_url(api_base, "")
    if not normalized_api_base:
        return False, get_service_configs(tts_settings), "API base URL is required."

    services = get_service_configs(tts_settings)
    updated_services: list[dict[str, object]] = []
    for service in services:
        if str(service.get("id") or "") != service_id:
            updated_services.append(service)
            continue

        updated = copy.deepcopy(service)
        updated["api_base"] = normalized_api_base
        if str(api_key or "").strip():
            updated["api_key"] = str(api_key or "").strip()
        if updated.get("kind") == "commercial":
            provider_key = str(updated.get("provider") or service_id)
            parsed_models = _parse_model_list(models or [], provider_key)
            if parsed_models:
                updated["models"] = parsed_models
                updated["default_model"] = parsed_models[0]
            parsed_voices = _parse_voice_list(voices or [], provider_key)
            if parsed_voices:
                updated["voices"] = parsed_voices
                updated["default_voice"] = parsed_voices[0]
        updated_services.append(updated)

    return True, updated_services, ""


def _legacy_endpoints_to_provider_configs(raw_json: str) -> list[dict[str, object]]:
    raw_text = str(raw_json or "").strip()
    if not raw_text:
        return []

    is_valid, error = validate_openai_audio_endpoints_json(raw_text)
    if not is_valid:
        logging.warning("Skipping legacy OpenAI-compatible audio endpoints: %s", error)
        return []

    payload = json.loads(raw_text)
    providers: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        display_name = str(item.get("name", "")).strip()
        provider_id = _normalize_provider_id(display_name)
        if not provider_id:
            continue

        api_base = str(item.get("base_url", item.get("api_base", ""))).strip()
        if not api_base:
            continue

        provider_key = _infer_audio_provider(
            name=display_name,
            base_url=api_base,
            raw_provider=str(item.get("provider", "") or "").strip(),
        )

        models = _parse_model_list(item.get("models", []), provider_key)
        default_model = _normalize_model_for_provider(
            str(item.get("default_model", "")).strip(),
            provider_key,
        )
        if default_model and default_model not in models:
            models.insert(0, default_model)

        voices = _parse_voice_list(item.get("voices", []), provider_key)
        default_voice = _normalize_voice_for_provider(
            str(item.get("default_voice", "")).strip(),
            provider_key,
        )
        if default_voice and default_voice not in voices:
            voices.insert(0, default_voice)

        providers.append(
            {
                "id": provider_id,
                "name": display_name or provider_id,
                "provider": provider_key,
                "api_base": api_base,
                "api_key_env": str(item.get("api_key_env", "")).strip(),
                "api_key": str(item.get("api_key", "")).strip(),
                "secret_ref": str(item.get("secret_ref", "")).strip(),
                "is_custom": provider_id not in FIRST_CLASS_SERVICE_IDS,
                "models": models,
                "default_model": default_model,
                "voices": voices,
                "default_voice": default_voice,
                PREBUILT_VOICE_PROVIDER_FIELD: _coerce_bool(
                    item.get(PREBUILT_VOICE_PROVIDER_FIELD),
                    True,
                ),
            }
        )

    return providers


def get_provider_configs(tts_settings) -> list[dict[str, object]]:
    custom_configs: dict[str, dict[str, object]] = {}

    raw_provider_configs = _read_setting(tts_settings, "provider_configs", [])
    if isinstance(raw_provider_configs, list):
        for raw_provider in raw_provider_configs:
            if not isinstance(raw_provider, dict):
                continue

            raw_id = str(
                raw_provider.get("id") or raw_provider.get("name") or ""
            ).strip()
            provider_id = _normalize_provider_id(raw_id)
            if (
                not provider_id
                or _normalize_service_id(provider_id) in FIRST_CLASS_SERVICE_IDS
            ):
                continue

            api_base = _normalize_base_url(
                raw_provider.get("api_base") or raw_provider.get("base_url") or "",
                "",
            )
            provider_key = _infer_audio_provider(
                name=str(raw_provider.get("name") or provider_id),
                base_url=api_base,
                raw_provider=str(raw_provider.get("provider") or provider_id),
            )

            if not api_base:
                continue
            record = {
                "id": provider_id,
                "name": str(raw_provider.get("name") or provider_id).strip()
                or provider_id,
                "provider": provider_key,
                "api_base": api_base,
                "api_key_env": str(raw_provider.get("api_key_env") or "").strip(),
                "api_key": str(raw_provider.get("api_key") or "").strip(),
                "secret_ref": str(raw_provider.get("secret_ref") or "").strip(),
                "is_custom": True,
            }
            adapter_config = _normalize_adapter_config(raw_provider)
            record.update(adapter_config)
            adapter = str(adapter_config["adapter"])
            profile_id = str(adapter_config.get("profile_id") or "")

            models = _parse_model_list(raw_provider.get("models", []), provider_key)
            default_model = _normalize_model_for_provider(
                str(raw_provider.get("default_model") or "").strip(),
                provider_key,
            )
            if default_model and default_model not in models:
                models.insert(0, default_model)

            if (
                not models
                and adapter in {OPENAI_COMPAT_ADAPTER, AUDIO_CPP_ADAPTER}
                and not profile_id
            ):
                builtin_models = (
                    [str(item["id"]) for item in AUDIO_CPP_MODEL_CATALOG]
                    if adapter == AUDIO_CPP_ADAPTER
                    else _provider_model_catalog(provider_key)
                )
                models = list(builtin_models)

            if not default_model:
                default_model = (
                    models[0]
                    if models and adapter != AZURE_SPEECH_ADAPTER
                    else (
                        _provider_default_model(provider_key)
                        if adapter in {OPENAI_COMPAT_ADAPTER, AUDIO_CPP_ADAPTER}
                        and not profile_id
                        else ""
                    )
                )

            voices = _parse_voice_list(raw_provider.get("voices", []), provider_key)
            default_voice = _normalize_voice_for_provider(
                str(raw_provider.get("default_voice") or "").strip(),
                provider_key,
            )
            if default_voice and default_voice not in voices:
                voices.insert(0, default_voice)

            if (
                not voices
                and adapter in {OPENAI_COMPAT_ADAPTER, AUDIO_CPP_ADAPTER}
                and not profile_id
            ):
                voices = (
                    list(AUDIO_CPP_PREBUILT_VOICES)
                    if adapter == AUDIO_CPP_ADAPTER
                    else _provider_voice_catalog(provider_key, default_model)
                )

            if not default_voice:
                default_voice = (
                    voices[0]
                    if voices and adapter != AZURE_SPEECH_ADAPTER
                    else (
                        _provider_default_voice(provider_key)
                        if adapter in {OPENAI_COMPAT_ADAPTER, AUDIO_CPP_ADAPTER}
                        and not profile_id
                        else ""
                    )
                )

            record["models"] = _dedupe_ordered(models)
            record["default_model"] = default_model
            record["voices"] = _dedupe_ordered(voices)
            record["default_voice"] = default_voice
            raw_supports_prebuilt = raw_provider.get(PREBUILT_VOICE_PROVIDER_FIELD)
            if raw_supports_prebuilt is None:
                raw_supports_prebuilt = raw_provider.get("has_prebuilt_voices")
            record[PREBUILT_VOICE_PROVIDER_FIELD] = _coerce_bool(
                raw_supports_prebuilt,
                bool(record["voices"]),
            )
            for key in (
                "settings",
                "model_catalog",
                "voice_catalogues",
                "voice_metadata",
                "default_voices",
                "default_voices_by_language",
                "pricing",
            ):
                value = raw_provider.get(key)
                if isinstance(value, dict) or (
                    key == "model_catalog" and isinstance(value, list)
                ):
                    record[key] = copy.deepcopy(value)
            for key in (
                "voice_reference_text",
                "supports_voice_cloning",
                "supports_voice_deletion",
            ):
                if key in raw_provider:
                    record[key] = (
                        _coerce_bool(raw_provider.get(key), False)
                        if key != "voice_reference_text"
                        else str(raw_provider.get(key) or "ignored").strip()
                        or "ignored"
                    )
            if isinstance(raw_provider.get(GENERATION_PROMPT_MODELS_FIELD), list):
                record[GENERATION_PROMPT_MODELS_FIELD] = [
                    str(model).strip()
                    for model in raw_provider[GENERATION_PROMPT_MODELS_FIELD]
                    if str(model).strip()
                ]

            custom_configs[provider_id] = record

    legacy_raw_json = str(
        _read_setting(tts_settings, "openai_audio_endpoints_json", "") or ""
    )
    for legacy_provider in _legacy_endpoints_to_provider_configs(legacy_raw_json):
        provider_id = str(legacy_provider.get("id") or "")
        if not provider_id:
            continue
        if _normalize_service_id(provider_id) in FIRST_CLASS_SERVICE_IDS:
            continue

        if provider_id not in custom_configs:
            custom_configs[provider_id] = dict(legacy_provider)

    return sorted(
        custom_configs.values(),
        key=lambda item: str(item.get("name") or item.get("id") or "").lower(),
    )


def save_provider(
    tts_settings,
    provider_name: str,
    provider_type: str,
    api_base: str,
    api_key: str = "",
    models: list[str] | str | None = None,
    voices: list[str] | str | None = None,
    supports_prebuilt_voices: bool | None = None,
    provider_id: str = "",
    adapter_config: dict | None = None,
) -> tuple[bool, list[dict[str, object]], str, str]:
    display_name = str(provider_name or "").strip()
    if not display_name:
        return (
            False,
            get_provider_configs(tts_settings),
            "",
            "Provider name is required.",
        )

    normalized_provider_id = _normalize_provider_id(provider_id or display_name)
    if not normalized_provider_id:
        return (
            False,
            get_provider_configs(tts_settings),
            "",
            "Provider name must include letters or numbers.",
        )
    if (
        _normalize_service_id(normalized_provider_id) in FIRST_CLASS_SERVICE_IDS
        or _normalize_service_id(display_name) in FIRST_CLASS_SERVICE_IDS
    ):
        return (
            False,
            get_provider_configs(tts_settings),
            "",
            f"'{display_name}' is reserved for a first-class TTS service.",
        )

    normalized_provider_type = _normalize_audio_provider(provider_type)
    if not normalized_provider_type:
        return (
            False,
            get_provider_configs(tts_settings),
            "",
            "Provider type must be OpenAI, Gemini, or Azure compatible.",
        )

    normalized_api_base = _normalize_base_url(api_base, "")
    if not normalized_api_base:
        return (
            False,
            get_provider_configs(tts_settings),
            "",
            "API base URL is required.",
        )

    provider_configs = get_provider_configs(tts_settings)
    existing = next(
        (
            item
            for item in provider_configs
            if str(item.get("id") or "") == normalized_provider_id
        ),
        None,
    )
    is_custom = True
    source_adapter_config = adapter_config
    if source_adapter_config is None and existing is not None:
        source_adapter_config = existing
    normalized_adapter_config = _normalize_adapter_config(source_adapter_config)
    adapter = str(normalized_adapter_config["adapter"])
    if adapter == GENERIC_JSON_ADAPTER:
        if not str(normalized_adapter_config.get("speech_path") or "").strip():
            return (
                False,
                provider_configs,
                "",
                "A discovered speech path is required for generic JSON endpoints.",
            )
        request_fields = normalized_adapter_config.get("request_fields", {})
        if (
            not isinstance(request_fields, dict)
            or not str(request_fields.get("text") or "").strip()
        ):
            return (
                False,
                provider_configs,
                "",
                "A text request field is required for generic JSON endpoints.",
            )

    parsed_models = _parse_model_list(models or [], normalized_provider_type)
    if not parsed_models and existing is not None:
        parsed_models = _parse_model_list(
            existing.get("models", []), normalized_provider_type
        )
    if not parsed_models and isinstance(source_adapter_config, dict):
        parsed_models = _parse_model_list(
            source_adapter_config.get("models", []), normalized_provider_type
        )
    profile_id = str(normalized_adapter_config.get("profile_id") or "")
    if not parsed_models and adapter == OPENAI_COMPAT_ADAPTER and not profile_id:
        parsed_models = list(_provider_model_catalog(normalized_provider_type))

    default_model = (
        parsed_models[0]
        if parsed_models
        else (
            _provider_default_model(normalized_provider_type)
            if adapter == OPENAI_COMPAT_ADAPTER and not profile_id
            else ""
        )
    )

    parsed_voices = _parse_voice_list(voices or [], normalized_provider_type)
    if not parsed_voices and existing is not None:
        parsed_voices = _parse_voice_list(
            existing.get("voices", []), normalized_provider_type
        )
    if not parsed_voices and isinstance(source_adapter_config, dict):
        parsed_voices = _parse_voice_list(
            source_adapter_config.get("voices", []), normalized_provider_type
        )
    if not parsed_voices and adapter == OPENAI_COMPAT_ADAPTER and not profile_id:
        parsed_voices = list(
            _provider_voice_catalog(normalized_provider_type, default_model)
        )

    default_voice = (
        parsed_voices[0]
        if parsed_voices
        else (
            _provider_default_voice(normalized_provider_type)
            if adapter == OPENAI_COMPAT_ADAPTER and not profile_id
            else ""
        )
    )
    if supports_prebuilt_voices is None:
        if existing is not None:
            provider_supports_prebuilt_voices = _coerce_bool(
                existing.get(PREBUILT_VOICE_PROVIDER_FIELD),
                bool(existing.get("voices", [])),
            )
        else:
            provider_supports_prebuilt_voices = bool(parsed_voices)
    else:
        provider_supports_prebuilt_voices = bool(supports_prebuilt_voices)

    updated_record: dict[str, object] = {
        "id": normalized_provider_id,
        "name": display_name,
        "provider": normalized_provider_type,
        "api_base": normalized_api_base,
        "api_key_env": "",
        "api_key": str(api_key or "").strip(),
        "is_custom": is_custom,
        "models": parsed_models,
        "default_model": default_model,
        "voices": parsed_voices,
        "default_voice": default_voice,
        PREBUILT_VOICE_PROVIDER_FIELD: provider_supports_prebuilt_voices,
    }
    updated_record.update(normalized_adapter_config)

    updated_provider_configs: list[dict[str, object]] = []
    found = False
    for item in provider_configs:
        item_id = str(item.get("id") or "")
        if item_id == normalized_provider_id:
            updated_provider_configs.append(updated_record)
            found = True
            continue
        updated_provider_configs.append(item)

    if not found:
        updated_provider_configs.append(updated_record)

    updated_provider_configs = sorted(
        updated_provider_configs,
        key=lambda item: str(item.get("name") or "").lower(),
    )
    return True, updated_provider_configs, normalized_provider_id, ""


def remove_custom_provider(
    tts_settings,
    provider_name_or_id: str,
) -> tuple[bool, list[dict[str, object]], str]:
    provider_id = _normalize_provider_id(provider_name_or_id)
    if not provider_id:
        return (
            False,
            get_provider_configs(tts_settings),
            "Select a custom provider first.",
        )

    if _normalize_service_id(provider_id) in FIRST_CLASS_SERVICE_IDS:
        return (
            False,
            get_provider_configs(tts_settings),
            "First-class TTS services cannot be removed.",
        )

    provider_configs = get_provider_configs(tts_settings)
    updated_provider_configs = [
        item for item in provider_configs if str(item.get("id") or "") != provider_id
    ]

    if len(updated_provider_configs) == len(provider_configs):
        return (
            False,
            provider_configs,
            f"Provider '{provider_name_or_id}' was not found.",
        )

    return True, updated_provider_configs, ""


def _normalize_base_url(base_url: str | None, fallback: str) -> str:
    normalized = (base_url or fallback).strip().rstrip("/")
    return normalized or fallback


def _openai_auth_headers(
    api_key: str = XTTS_OPENAI_PLACEHOLDER_API_KEY,
) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _configured_endpoint_url(base_url: str, path: str) -> str:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        return str(base_url or "").strip().rstrip("/")
    if normalized_path.startswith(("http://", "https://")):
        return normalized_path

    parsed = urlparse(str(base_url or "").strip())
    origin = urlunparse(
        parsed._replace(path="", params="", query="", fragment="")
    ).rstrip("/")
    return urljoin(f"{origin}/", normalized_path.lstrip("/"))


def _audio_cpp_endpoint_key(base_url: str) -> str:
    """Return a stable endpoint identity without request or voice data."""
    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    path = parsed.path.rstrip("/") or "/"
    return f"{scheme}://{hostname}:{port}{path}"


def _audio_cpp_endpoint_lock_for(base_url: str) -> RLock:
    key = _audio_cpp_endpoint_key(base_url)
    with _audio_cpp_endpoint_locks_guard:
        lock = _audio_cpp_endpoint_locks.get(key)
        if lock is None:
            lock = RLock()
            _audio_cpp_endpoint_locks[key] = lock
        return lock


@contextmanager
def audio_cpp_endpoint_lock(tts_settings: dict):
    """Serialize synthesis requests to one audio.cpp endpoint across jobs."""
    endpoint, _error = resolve_openai_audio_endpoint(tts_settings)
    if (
        endpoint is None
        or _normalize_custom_adapter(endpoint.get("adapter")) != AUDIO_CPP_ADAPTER
    ):
        yield
        return
    with _audio_cpp_endpoint_lock_for(str(endpoint.get("base_url") or "")):
        yield


def _coerce_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


XTTS_OVERRIDE_SPECS = (
    ("temperature", "temperature", "xtts_send_temperature", _coerce_float, 0.75),
    ("top_p", "top_p", "xtts_send_top_p", _coerce_float, 0.85),
    ("top_k", "top_k", "xtts_send_top_k", _coerce_int, 50),
    (
        "repetition_penalty",
        "repetition_penalty",
        "xtts_send_repetition_penalty",
        _coerce_float,
        5.0,
    ),
    (
        "length_penalty",
        "length_penalty",
        "xtts_send_length_penalty",
        _coerce_float,
        1.0,
    ),
    ("do_sample", "do_sample", "xtts_send_do_sample", _coerce_bool, True),
    ("num_beams", "num_beams", "xtts_send_num_beams", _coerce_int, 1),
    (
        "enable_text_splitting",
        "enable_text_splitting",
        "xtts_send_enable_text_splitting",
        _coerce_bool,
        True,
    ),
    ("gpt_cond_len", "gpt_cond_len", "xtts_send_gpt_cond_len", _coerce_int, 12),
    (
        "gpt_cond_chunk_len",
        "gpt_cond_chunk_len",
        "xtts_send_gpt_cond_chunk_len",
        _coerce_int,
        4,
    ),
    ("max_ref_len", "max_ref_len", "xtts_send_max_ref_len", _coerce_int, 12),
    (
        "sound_norm_refs",
        "sound_norm_refs",
        "xtts_send_sound_norm_refs",
        _coerce_bool,
        False,
    ),
    (
        "stream_chunk_size",
        "stream_chunk_size",
        "xtts_send_stream_chunk_size",
        _coerce_int,
        100,
    ),
    (
        "overlap_wav_len",
        "overlap_wav_len",
        "xtts_send_overlap_wav_len",
        _coerce_int,
        1024,
    ),
)
XTTS_OVERRIDE_KEYS = tuple(spec[0] for spec in XTTS_OVERRIDE_SPECS)
XTTS_OVERRIDE_ALIASES = ("temp", "max_ref_length")


def _build_xtts_overrides(tts_settings: dict) -> dict[str, object]:
    overrides: dict[str, object] = {}

    for output_name, setting_name, send_flag, coercer, fallback in XTTS_OVERRIDE_SPECS:
        if not _coerce_bool(tts_settings.get(send_flag), False):
            continue
        overrides[output_name] = coercer(tts_settings.get(setting_name), fallback)

    return overrides


def _try_parse_json_object(raw_text: str) -> dict | None:
    trimmed = str(raw_text or "").strip()
    if not trimmed or not trimmed.startswith("{"):
        return None

    try:
        payload = json.loads(trimmed)
    except json.JSONDecodeError:
        return None

    if isinstance(payload, dict):
        return payload
    return None


def _looks_like_xtts_model(model_name: str) -> bool:
    normalized = str(model_name or "").strip().lower()
    return "xtts" in normalized


def _looks_like_xtts_endpoint(endpoint: dict[str, str] | None) -> bool:
    if not isinstance(endpoint, dict):
        return False

    hint = " ".join(
        [
            str(endpoint.get("name", "") or ""),
            str(endpoint.get("base_url", "") or ""),
            str(endpoint.get("default_model", "") or ""),
        ]
    ).lower()
    return "xtts" in hint


def _is_xtts_target(model_name: str, endpoint: dict[str, str] | None = None) -> bool:
    return _looks_like_xtts_model(model_name) or _looks_like_xtts_endpoint(endpoint)


def _build_xtts_instructions_payload(
    tts_settings: dict, existing_instructions: str
) -> str:
    payload = _try_parse_json_object(existing_instructions) or {}
    xtts_overrides = _build_xtts_overrides(tts_settings)

    for key in (*XTTS_OVERRIDE_KEYS, *XTTS_OVERRIDE_ALIASES):
        payload.pop(key, None)

    existing_xtts = payload.get("xtts")
    merged_xtts: dict[str, object] = {}
    if isinstance(existing_xtts, dict):
        merged_xtts.update(existing_xtts)

    for key in (*XTTS_OVERRIDE_KEYS, *XTTS_OVERRIDE_ALIASES):
        merged_xtts.pop(key, None)

    merged_xtts.update(xtts_overrides)

    payload["language"] = str(tts_settings.get("language") or "en").strip() or "en"
    if merged_xtts:
        payload["xtts"] = merged_xtts
    else:
        payload.pop("xtts", None)

    return json.dumps(payload, ensure_ascii=False)


def _normalize_voxcpm_model(
    model_name: str, fallback: str = VOXCPM_DEFAULT_MODEL
) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        return fallback

    lowered = normalized.lower()
    if lowered in {"openbmb/voxcpm2", "voxcpm2"}:
        return VOXCPM_DEFAULT_MODEL
    return normalized


def _normalize_fishs2_model(
    model_name: str, fallback: str = FISHS2_DEFAULT_MODEL
) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        return fallback

    lowered = normalized.lower()
    if lowered in {"fishs2", "fish-s2", "s2-pro", "fishaudio/s2-pro"}:
        return FISHS2_DEFAULT_MODEL
    return normalized


def normalize_tts_model_catalog(
    service_id: str | None,
    models: list[str] | tuple[str, ...],
) -> list[str]:
    """Canonicalize live model catalogues without inventing distinct models.

    Fish S2 exposes OpenAI-compatible aliases for the same S2 Pro model. If
    those aliases are projected as separate choices, every selection produces
    identical audio and users can reasonably mistake them for quantizations.
    """

    normalized_service = _normalize_service_id(service_id)
    if normalized_service == "fishs2":
        return _dedupe_ordered(
            [_normalize_fishs2_model(str(model), fallback="") for model in models]
        )
    return _dedupe_ordered([str(model) for model in models])


def _build_voxcpm_options(tts_settings: dict) -> dict[str, object]:
    cfg_value = _coerce_float(
        tts_settings.get("voxcpm_cfg_value"),
        VOXCPM_DEFAULT_CFG_VALUE,
    )
    cfg_value = min(20.0, max(0.01, cfg_value))

    inference_timesteps = _coerce_int(
        tts_settings.get("voxcpm_inference_timesteps"),
        VOXCPM_DEFAULT_INFERENCE_TIMESTEPS,
    )
    inference_timesteps = min(200, max(1, inference_timesteps))

    retry_badcase_max_times = _coerce_int(
        tts_settings.get("voxcpm_retry_badcase_max_times"),
        VOXCPM_DEFAULT_RETRY_BADCASE_MAX_TIMES,
    )
    retry_badcase_max_times = min(20, max(1, retry_badcase_max_times))

    retry_badcase_ratio_threshold = _coerce_float(
        tts_settings.get("voxcpm_retry_badcase_ratio_threshold"),
        VOXCPM_DEFAULT_RETRY_BADCASE_RATIO_THRESHOLD,
    )
    retry_badcase_ratio_threshold = min(50.0, max(0.01, retry_badcase_ratio_threshold))

    min_len = _coerce_int(
        tts_settings.get("voxcpm_min_len"),
        VOXCPM_DEFAULT_MIN_LEN,
    )
    min_len = max(1, min_len)

    max_len = _coerce_int(
        tts_settings.get("voxcpm_max_len"),
        VOXCPM_DEFAULT_MAX_LEN,
    )
    max_len = max(1, max_len)
    if max_len < min_len:
        max_len = min_len

    return {
        "cfg_value": cfg_value,
        "inference_timesteps": inference_timesteps,
        "normalize": _coerce_bool(
            tts_settings.get("voxcpm_normalize"),
            VOXCPM_DEFAULT_NORMALIZE,
        ),
        "denoise": _coerce_bool(
            tts_settings.get("voxcpm_denoise"),
            VOXCPM_DEFAULT_DENOISE,
        ),
        "retry_badcase": _coerce_bool(
            tts_settings.get("voxcpm_retry_badcase"),
            VOXCPM_DEFAULT_RETRY_BADCASE,
        ),
        "retry_badcase_max_times": retry_badcase_max_times,
        "retry_badcase_ratio_threshold": retry_badcase_ratio_threshold,
        "min_len": min_len,
        "max_len": max_len,
    }


def _build_fishs2_options(tts_settings: dict) -> dict[str, object]:
    temperature = _coerce_float(
        tts_settings.get("fishs2_temperature"),
        FISHS2_DEFAULT_TEMPERATURE,
    )
    temperature = min(1.0, max(0.0, temperature))

    top_p = _coerce_float(
        tts_settings.get("fishs2_top_p"),
        FISHS2_DEFAULT_TOP_P,
    )
    top_p = min(1.0, max(0.0, top_p))

    chunk_length = _coerce_int(
        tts_settings.get("fishs2_chunk_length"),
        FISHS2_DEFAULT_CHUNK_LENGTH,
    )
    chunk_length = min(300, max(100, chunk_length))

    latency = (
        str(tts_settings.get("fishs2_latency") or FISHS2_DEFAULT_LATENCY)
        .strip()
        .lower()
    )
    if latency not in {"normal", "balanced"}:
        latency = FISHS2_DEFAULT_LATENCY

    speed = _coerce_float(tts_settings.get("speed"), 1.0)
    speed = min(2.0, max(0.5, speed))

    volume = _coerce_float(
        tts_settings.get("fishs2_prosody_volume"),
        FISHS2_DEFAULT_PROSODY_VOLUME,
    )
    volume = min(20.0, max(-20.0, volume))

    return {
        "temperature": temperature,
        "top_p": top_p,
        "chunk_length": chunk_length,
        "latency": latency,
        "normalize": _coerce_bool(
            tts_settings.get("fishs2_normalize"),
            FISHS2_DEFAULT_NORMALIZE,
        ),
        "prosody": {
            "speed": speed,
            "volume": volume,
            "normalize_loudness": _coerce_bool(
                tts_settings.get("fishs2_normalize_loudness"),
                FISHS2_DEFAULT_NORMALIZE_LOUDNESS,
            ),
        },
    }


def _normalize_voxtral_model(
    model_name: str, fallback: str = VOXTRAL_DEFAULT_MODEL
) -> str:
    normalized = str(model_name or "").strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    if normalized in {"auto", "gguf", "bf16"}:
        return normalized
    return fallback


def _build_voxtral_options(tts_settings: dict) -> dict[str, object]:
    return {
        "max_frames": _coerce_int(tts_settings.get("voxtral_max_frames"), 1024),
        "euler_steps": _coerce_int(tts_settings.get("voxtral_euler_steps"), 8),
        "chunk": _coerce_bool(tts_settings.get("voxtral_chunk"), False),
        "max_chunk_chars": _coerce_int(
            tts_settings.get("voxtral_max_chunk_chars"), 500
        ),
        "chunk_silence_ms": _coerce_int(
            tts_settings.get("voxtral_chunk_silence_ms"), 0
        ),
        "strip_quotes": _coerce_bool(tts_settings.get("voxtral_strip_quotes"), False),
        "strip_diacritics": _coerce_bool(
            tts_settings.get("voxtral_strip_diacritics"), False
        ),
        "level_audio": _coerce_bool(tts_settings.get("voxtral_level_audio"), False),
    }


def _parse_voxtral_instructions_options(instructions: str) -> dict[str, object]:
    raw = str(instructions or "").strip()
    if not raw:
        return {}

    raw_json = ""
    if raw.lower().startswith(VOXTRAL_INSTRUCTIONS_PREFIX):
        raw_json = raw[len(VOXTRAL_INSTRUCTIONS_PREFIX) :].strip()
    elif raw.startswith("{"):
        raw_json = raw
    else:
        return {}

    payload = _try_parse_json_object(raw_json)
    if payload is None:
        return {}

    return payload


def _build_voxtral_instructions_payload(
    tts_settings: dict, existing_instructions: str
) -> str:
    payload = _parse_voxtral_instructions_options(existing_instructions)
    options = _build_voxtral_options(tts_settings)

    for key in (*options, "language"):
        payload.pop(key, None)
    payload.update(options)

    return f"{VOXTRAL_INSTRUCTIONS_PREFIX}{json.dumps(payload, ensure_ascii=False)}"


def _dedupe_sorted(items: list[str]) -> list[str]:
    unique = {item.strip() for item in items if isinstance(item, str) and item.strip()}
    return sorted(unique)


def _dedupe_ordered(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        deduped.append(normalized)
        seen.add(normalized)
    return deduped


def _normalize_audio_provider(raw_provider: str | None) -> str:
    provider = str(raw_provider or "").strip().lower()
    aliases = {
        "google": GEMINI_PROVIDER,
        "google-ai": GEMINI_PROVIDER,
        "google_ai": GEMINI_PROVIDER,
        "google-ai-studio": GEMINI_PROVIDER,
        "ai-studio": GEMINI_PROVIDER,
        "eleven-labs": ELEVENLABS_PROVIDER,
        "eleven_labs": ELEVENLABS_PROVIDER,
        "elevenlabs": ELEVENLABS_PROVIDER,
        "azure": AZURE_SPEECH_PROVIDER,
        "azure-speech": AZURE_SPEECH_PROVIDER,
        "azure_speech": AZURE_SPEECH_PROVIDER,
    }
    provider = aliases.get(provider, provider)
    return provider if provider in SUPPORTED_AUDIO_PROVIDERS else ""


def _infer_audio_provider(
    name: str, base_url: str, raw_provider: str | None = None
) -> str:
    explicit = _normalize_audio_provider(raw_provider)
    if explicit:
        return explicit

    hint = f"{name} {base_url}".lower()
    if "generativelanguage.googleapis.com" in hint or "gemini" in hint:
        return GEMINI_PROVIDER
    if "api.elevenlabs.io" in hint or "elevenlabs" in hint or "eleven labs" in hint:
        return ELEVENLABS_PROVIDER
    return OPENAI_PROVIDER


def _provider_default_model(provider: str) -> str:
    if provider == GEMINI_PROVIDER:
        return GEMINI_AUDIO_DEFAULT_MODEL
    if provider == ELEVENLABS_PROVIDER:
        return ELEVENLABS_TTS_DEFAULT_MODEL
    return OPENAI_AUDIO_DEFAULT_MODEL


def _provider_default_voice(provider: str) -> str:
    if provider == GEMINI_PROVIDER:
        return GEMINI_AUDIO_DEFAULT_VOICE
    if provider == ELEVENLABS_PROVIDER:
        return ""
    return OPENAI_AUDIO_DEFAULT_VOICE


def _provider_model_catalog(provider: str) -> list[str]:
    if provider == GEMINI_PROVIDER:
        return list(GEMINI_TTS_MODELS)
    if provider == ELEVENLABS_PROVIDER:
        return [ELEVENLABS_TTS_DEFAULT_MODEL]
    return list(OPENAI_TTS_MODELS)


def _provider_voice_catalog(provider: str, model_name: str = "") -> list[str]:
    if provider == GEMINI_PROVIDER:
        return list(GEMINI_TTS_VOICES)

    if provider == ELEVENLABS_PROVIDER:
        return []

    normalized_model = _normalize_model_for_provider(model_name, provider).lower()
    if normalized_model in {"tts-1", "tts-1-hd"}:
        return list(OPENAI_TTS_CLASSIC_VOICES)
    return list(OPENAI_TTS_VOICES)


def _provider_for_tts_service(raw_service: str | None) -> str:
    normalized = str(raw_service or "").strip().lower()
    if normalized in {"audio.cpp", "audio_cpp", "audio-cpp", "audiocpp"}:
        return AUDIO_CPP_ADAPTER
    if normalized == OPENAI_SERVICE.lower():
        return OPENAI_PROVIDER
    if normalized in {GEMINI_SERVICE.lower(), LEGACY_GEMINI_SERVICE.lower()}:
        return GEMINI_PROVIDER
    if normalized == ELEVENLABS_SERVICE.lower():
        return ELEVENLABS_PROVIDER
    return ""


def _service_audio_endpoint(tts_settings, provider: str) -> dict[str, object]:
    normalized_provider = (
        AUDIO_CPP_ADAPTER
        if str(provider or "").strip().lower().replace("-", "_")
        in {"audio_cpp", "audiocpp"}
        else _normalize_audio_provider(provider)
    )
    service = get_service_config(tts_settings, normalized_provider)
    if service is None:
        service = get_service_config({}, normalized_provider) or {}
    base_url = str(service.get("api_base") or "").strip()
    if normalized_provider == AUDIO_CPP_ADAPTER:
        base_url = str(
            _read_setting(tts_settings, "audio_cpp_base_url", "") or base_url
        ).strip()

    return {
        "name": normalized_provider,
        "display_name": str(service.get("name") or normalized_provider),
        "base_url": base_url,
        "api_key": str(service.get("api_key") or ""),
        "api_key_env": str(service.get("api_key_env") or ""),
        "secret_ref": str(service.get("secret_ref") or ""),
        "provider": normalized_provider,
        "adapter": str(service.get("adapter") or ""),
        "default_model": str(
            service.get("default_model")
            or (
                ""
                if normalized_provider == AUDIO_CPP_ADAPTER
                else _provider_default_model(normalized_provider)
            )
        ),
        "default_voice": str(
            service.get("default_voice")
            or (
                ""
                if normalized_provider == AUDIO_CPP_ADAPTER
                else _provider_default_voice(normalized_provider)
            )
        ),
        "models": list(service.get("models") or []),
        "voices": list(service.get("voices") or []),
        "speech_path": str(service.get("speech_path") or ""),
        "models_path": str(service.get("models_path") or ""),
        "voices_path": str(service.get("voices_path") or ""),
        "auth_mode": str(service.get("auth_mode") or "bearer"),
        "direct_http": bool(service.get("direct_http")),
        "model_catalog": copy.deepcopy(service.get("model_catalog") or []),
        "model_voice_modes": copy.deepcopy(service.get("model_voice_modes") or {}),
        "voice_catalogues": copy.deepcopy(service.get("voice_catalogues") or {}),
        "voice_reference_text": str(service.get("voice_reference_text") or "ignored"),
    }


def _strip_provider_prefix(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    if "/" not in normalized:
        return normalized

    prefix, remainder = normalized.split("/", 1)
    if prefix.strip().lower() in {"openai", "gemini", "vertex_ai", "azure"}:
        return remainder.strip()
    return normalized


def _normalize_model_for_provider(model_name: str, provider: str) -> str:
    normalized = _strip_provider_prefix(model_name)
    if normalized.lower().startswith("models/"):
        normalized = normalized.split("/", 1)[1].strip()
    if provider == GEMINI_PROVIDER:
        alias = GEMINI_MODEL_ALIASES.get(normalized.lower())
        if alias:
            return alias
    return normalized


def _normalize_voice_for_provider(voice_name: str, provider: str) -> str:
    normalized = str(voice_name or "").strip()
    if not normalized:
        return ""

    voice_map = {voice.lower(): voice for voice in _provider_voice_catalog(provider)}
    return voice_map.get(normalized.lower(), normalized)


def _to_litellm_model_name(provider: str, model_name: str) -> str:
    normalized = _normalize_model_for_provider(model_name, provider)
    if "/" in normalized:
        maybe_provider, remainder = normalized.split("/", 1)
        if maybe_provider.lower() in SUPPORTED_AUDIO_PROVIDERS and remainder.strip():
            return f"{maybe_provider.lower()}/{remainder.strip()}"
    return f"{provider}/{normalized}"


def _merge_catalog_with_discovered(
    preferred: list[str], discovered: list[str]
) -> list[str]:
    return _dedupe_ordered(preferred + discovered)


OPENAI_CANDIDATE_FALLBACK_STATUS_CODES = {404, 405, 501}


def _should_try_next_openai_candidate(status_code: int) -> bool:
    return int(status_code) in OPENAI_CANDIDATE_FALLBACK_STATUS_CODES


def _openai_url_candidates(base_url: str, suffix: str) -> list[str]:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        candidates = [f"{normalized}/{suffix}"]
    else:
        candidates = [
            f"{normalized}/v1/{suffix}",
            f"{normalized}/{suffix}",
        ]

    deduped: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def _openai_models_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "models")


def _openai_voices_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "voices")


def _openai_audio_voices_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/voices")


def _openai_voice_catalog_urls(base_url: str) -> list[str]:
    return _dedupe_ordered(
        _openai_audio_voices_urls(base_url) + _openai_voices_urls(base_url)
    )


def _openai_audio_speech_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/speech")


def _openai_audio_speech_batch_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/speech/batch")


def _openai_capabilities_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "capabilities")


def _configured_openai_urls(
    endpoint: dict[str, object], path_key: str, fallback_urls: list[str]
) -> list[str]:
    configured_path = str(endpoint.get(path_key) or "").strip()
    configured_urls = (
        [_configured_endpoint_url(str(endpoint.get("base_url") or ""), configured_path)]
        if configured_path
        else []
    )
    return _dedupe_ordered(configured_urls + fallback_urls)


def _openai_files_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "files")


def _voxtral_models_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/models")


def _voxtral_voices_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/voices")


def _kokoro_models_urls(base_url: str) -> list[str]:
    return _openai_models_urls(base_url)


def _kokoro_voices_urls(base_url: str) -> list[str]:
    return _openai_voice_catalog_urls(base_url)


def _extract_models_from_openai_payload(payload) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", [])
    if not isinstance(data, list):
        return []

    models: list[str] = []
    for model in data:
        if isinstance(model, dict):
            model_id = str(model.get("id", "")).strip()
            if model_id:
                models.append(model_id)
    return _dedupe_sorted(models)


def _extract_voices_from_openai_payload(payload) -> list[str]:
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = []
        data = payload.get("data", [])
        if isinstance(data, list):
            candidates.extend(data)

        voices = payload.get("voices", [])
        if isinstance(voices, list):
            candidates.extend(voices)
    else:
        return []

    discovered: list[str] = []
    for voice in candidates:
        if isinstance(voice, dict):
            voice_id = str(
                voice.get("voice_id") or voice.get("id") or voice.get("name") or ""
            ).strip()
            if voice_id:
                discovered.append(voice_id)
            continue

        trimmed = str(voice or "").strip()
        if trimmed:
            discovered.append(trimmed)

    return _dedupe_sorted(discovered)


def _extract_file_ids_from_openai_payload(
    payload,
    *,
    allowed_purposes: set[str] | None = None,
) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", [])
    if not isinstance(data, list):
        return []

    file_ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        purpose = str(item.get("purpose") or "").strip()
        if allowed_purposes and purpose and purpose not in allowed_purposes:
            continue

        file_id = str(item.get("id") or "").strip()
        if file_id:
            file_ids.append(file_id)

    return _dedupe_ordered(file_ids)


def _extract_models_from_voxtral_payload(payload) -> list[str]:
    if not isinstance(payload, dict):
        return []

    models: list[str] = []
    default_model = _normalize_voxtral_model(
        payload.get("default_model", ""), fallback=""
    )
    if default_model:
        models.append(default_model)

    data = payload.get("data", [])
    if not isinstance(data, list):
        return _dedupe_ordered(models)

    for model in data:
        if not isinstance(model, dict):
            continue
        if model.get("available") is False:
            continue
        model_id = _normalize_voxtral_model(model.get("id", ""), fallback="")
        if model_id:
            models.append(model_id)

    return _dedupe_ordered(models)


def _extract_voices_from_voxtral_payload(payload) -> list[str]:
    if not isinstance(payload, dict):
        return []

    voices: list[str] = []
    data = payload.get("data", [])
    if not isinstance(data, list):
        return []

    for voice in data:
        if isinstance(voice, dict):
            voice_id = str(voice.get("id") or voice.get("voice_id") or "").strip()
            if voice_id:
                voices.append(voice_id)
        elif isinstance(voice, str):
            trimmed = voice.strip()
            if trimmed:
                voices.append(trimmed)

    return _dedupe_ordered(voices)


def validate_openai_audio_endpoints_json(raw_json: str) -> tuple[bool, str]:
    raw_text = (raw_json or "").strip()
    if not raw_text:
        return True, ""

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"

    if not isinstance(payload, list):
        return False, "Audio endpoint config must be a JSON list."

    names: set[str] = set()
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            return False, f"Endpoint #{idx} must be a JSON object."

        name = str(item.get("name", "")).strip()
        base_url = str(item.get("base_url", item.get("api_base", ""))).strip()

        if not name:
            return False, f"Endpoint #{idx} is missing 'name'."
        if name in names:
            return False, f"Endpoint name '{name}' is duplicated."
        names.add(name)

        if not base_url:
            return False, f"Endpoint '{name}' is missing 'base_url'."

    return True, ""


def _parse_openai_audio_endpoints(tts_settings: dict) -> dict[str, dict[str, object]]:
    endpoints: dict[str, dict[str, object]] = {}
    provider_configs = get_provider_configs(tts_settings)
    for provider_record in provider_configs:
        provider_id = str(provider_record.get("id", "")).strip()
        if not provider_id:
            continue

        base_url = str(provider_record.get("api_base", "")).strip().rstrip("/")
        if not base_url:
            continue

        provider = _infer_audio_provider(
            name=str(provider_record.get("name", "") or provider_id),
            base_url=base_url,
            raw_provider=str(provider_record.get("provider", "") or "").strip(),
        )

        default_model = str(provider_record.get("default_model", "")).strip()
        profile_id = str(provider_record.get("profile_id") or "")
        adapter = _normalize_custom_adapter(provider_record.get("adapter"))
        if not default_model and not profile_id and adapter == OPENAI_COMPAT_ADAPTER:
            default_model = _provider_default_model(provider)
        default_model = _normalize_model_for_provider(default_model, provider)

        default_voice = str(provider_record.get("default_voice", "")).strip()
        if not default_voice and not profile_id and adapter == OPENAI_COMPAT_ADAPTER:
            default_voice = _provider_default_voice(provider)
        default_voice = _normalize_voice_for_provider(default_voice, provider)

        endpoints[provider_id] = {
            "name": provider_id,
            "display_name": str(provider_record.get("name", "") or provider_id),
            "base_url": base_url,
            "api_key": str(provider_record.get("api_key", "")).strip(),
            "api_key_env": str(provider_record.get("api_key_env", "")).strip(),
            "secret_ref": str(provider_record.get("secret_ref", "")).strip(),
            "provider": provider,
            "default_model": default_model,
            "default_voice": default_voice,
            "models": list(provider_record.get("models") or []),
            "voices": list(provider_record.get("voices") or []),
            "adapter": adapter,
            "profile_id": profile_id,
            "speech_path": str(provider_record.get("speech_path") or ""),
            "models_path": str(provider_record.get("models_path") or ""),
            "voices_path": str(provider_record.get("voices_path") or ""),
            "request_fields": dict(provider_record.get("request_fields") or {}),
            "request_defaults": dict(provider_record.get("request_defaults") or {}),
            "auth_mode": str(provider_record.get("auth_mode") or "bearer"),
            "direct_http": _coerce_bool(provider_record.get("direct_http"), False),
            "model_catalog": copy.deepcopy(provider_record.get("model_catalog") or []),
            "voice_catalogues": copy.deepcopy(
                provider_record.get("voice_catalogues") or {}
            ),
            "voice_metadata": copy.deepcopy(
                provider_record.get("voice_metadata") or {}
            ),
            "default_voices": copy.deepcopy(
                provider_record.get("default_voices") or {}
            ),
            "default_voices_by_language": copy.deepcopy(
                provider_record.get("default_voices_by_language") or {}
            ),
            "generation_prompt_models": copy.deepcopy(
                provider_record.get(GENERATION_PROMPT_MODELS_FIELD) or []
            ),
            "pricing": copy.deepcopy(provider_record.get("pricing") or {}),
        }

    return endpoints


def list_openai_audio_endpoint_names(tts_settings: dict) -> list[str]:
    """Lists configured custom audio endpoint names."""
    service_provider = _provider_for_tts_service(tts_settings.get("service"))
    if service_provider:
        return [_service_audio_endpoint(tts_settings, service_provider)["name"]]

    return sorted(_parse_openai_audio_endpoints(tts_settings).keys())


def resolve_openai_audio_endpoint(
    tts_settings: dict,
) -> tuple[dict[str, object] | None, str]:
    """Resolves the selected custom audio endpoint from settings."""
    endpoints = _parse_openai_audio_endpoints(tts_settings)

    service_provider = _provider_for_tts_service(tts_settings.get("service"))
    if service_provider:
        return _service_audio_endpoint(tts_settings, service_provider), ""

    if not endpoints:
        return None, "No custom audio endpoints are configured."

    selected_name = str(tts_settings.get("openai_audio_endpoint", "") or "").strip()
    if selected_name:
        endpoint = endpoints.get(selected_name)
        if endpoint is None:
            return (
                None,
                f"Custom audio endpoint '{selected_name}' is not defined in config.",
            )
        return endpoint, ""

    first_name = sorted(endpoints.keys())[0]
    return endpoints[first_name], ""


def resolve_custom_tts_adapter_id(tts_settings: dict) -> str:
    """Return the selected custom endpoint's normalized transport adapter."""

    endpoint, _ = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        return ""
    return _normalize_custom_adapter(str(endpoint.get("adapter") or ""))


def should_show_xtts_advanced_settings(tts_settings: dict) -> bool:
    service = str(tts_settings.get("service") or "").strip()
    if service == "XTTS":
        return True
    if service in {OPENAI_SERVICE, GEMINI_SERVICE, LEGACY_GEMINI_SERVICE}:
        return False
    if service not in {OPENAI_COMPAT_SERVICE, LEGACY_OPENAI_COMPAT_SERVICE}:
        return False

    endpoint, _ = resolve_openai_audio_endpoint(tts_settings)
    model_name = str(tts_settings.get("xtts_model") or "").strip()
    if not model_name and endpoint is not None:
        model_name = str(endpoint.get("default_model", "") or "").strip()

    return _is_xtts_target(model_name, endpoint)


def _resolve_openai_audio_api_key(endpoint: dict[str, str]) -> str:
    key_env = str(endpoint.get("api_key_env", "") or "").strip()
    if key_env:
        env_value = os.getenv(key_env, "").strip()
        if env_value:
            return env_value

    explicit_key = str(endpoint.get("api_key", "") or "").strip()
    if explicit_key:
        return explicit_key

    return XTTS_OPENAI_PLACEHOLDER_API_KEY


def _configured_endpoint_auth_headers(endpoint: dict[str, object]) -> dict[str, str]:
    key_env = str(endpoint.get("api_key_env", "") or "").strip()
    if key_env:
        env_value = os.getenv(key_env, "").strip()
        if env_value:
            auth_mode = str(endpoint.get("auth_mode") or "").lower()
            if auth_mode == "subscription-key":
                return {"Ocp-Apim-Subscription-Key": env_value}
            if auth_mode == "api-key":
                return {"api-key": env_value}
            return _openai_auth_headers(env_value)

    explicit_key = str(endpoint.get("api_key", "") or "").strip()
    if not explicit_key:
        return {}
    if str(endpoint.get("auth_mode") or "").lower() == "subscription-key":
        return {"Ocp-Apim-Subscription-Key": explicit_key}
    if str(endpoint.get("auth_mode") or "").lower() == "api-key":
        return {"api-key": explicit_key}
    return _openai_auth_headers(explicit_key)


def _resolve_azure_speech_api_key(endpoint: dict[str, object]) -> str:
    key_env = str(endpoint.get("api_key_env") or "AZURE_SPEECH_KEY").strip()
    if key_env:
        environment_key = os.getenv(key_env, "").strip()
        if environment_key:
            return environment_key
    return str(endpoint.get("api_key") or "").strip()


def _azure_speech_catalog_values(
    endpoint: dict[str, object], model: str, key: str
) -> list[str]:
    catalogues = endpoint.get("voice_catalogues")
    if isinstance(catalogues, dict) and key == "voices":
        configured = catalogues.get(model)
        if isinstance(configured, list):
            return [str(value).strip() for value in configured if str(value).strip()]
    configured = endpoint.get(key)
    if isinstance(configured, list):
        return [str(value).strip() for value in configured if str(value).strip()]
    if key == "models":
        return list(AZURE_SPEECH_MODELS)
    return list(AZURE_SPEECH_VOICES)


def _canonical_azure_speech_model(endpoint: dict[str, object], value: str) -> str:
    normalized = str(value or "").strip()
    del endpoint
    allowed = list(AZURE_SPEECH_MODELS)
    by_lower = {item.lower(): item for item in allowed}
    canonical = by_lower.get(normalized.lower())
    if canonical:
        return canonical
    raise ValueError(
        "Azure Speech model must be one of: " + ", ".join(AZURE_SPEECH_MODELS)
    )


def _canonical_azure_speech_voice(
    endpoint: dict[str, object], model: str, value: str
) -> str:
    normalized = str(value or "").strip()
    del endpoint
    allowed = list(AZURE_SPEECH_VOICE_CATALOGUES.get(model, ()))
    by_lower = {item.lower(): item for item in allowed}
    canonical = by_lower.get(normalized.lower())
    if canonical:
        return canonical
    raise ValueError(
        f"Azure Speech voice must be a published prebuilt voice for {model}."
    )


def _validate_azure_speech_request(
    endpoint: dict[str, object], tts_settings: dict
) -> tuple[str, str, str, str]:
    base_url = str(endpoint.get("base_url") or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not hostname or "your" in hostname:
        raise ValueError(
            "Azure Speech requires a non-placeholder HTTPS base URL for your Speech resource."
        )

    api_key = _resolve_azure_speech_api_key(endpoint)
    if not api_key:
        raise ValueError(
            "Azure Speech requires a subscription key (set AZURE_SPEECH_KEY or configure a key)."
        )

    model_value = str(
        tts_settings.get("model")
        or tts_settings.get("xtts_model")
        or endpoint.get("default_model")
        or ""
    ).strip()
    if not model_value:
        raise ValueError("Azure Speech requires a model selection.")
    model = _canonical_azure_speech_model(endpoint, model_value)

    voice_value = str(
        tts_settings.get("voice")
        or tts_settings.get("speaker")
        or endpoint.get("default_voice")
        or ""
    ).strip()
    if not voice_value:
        raise ValueError("Azure Speech requires a prebuilt voice selection.")
    voice = _canonical_azure_speech_voice(endpoint, model, voice_value)

    speech_path = str(endpoint.get("speech_path") or "/cognitiveservices/v1").strip()
    if not speech_path:
        raise ValueError("Azure Speech requires a configured speech path.")
    return base_url, model, voice, api_key


def _azure_speech_ssml(text: str, model: str, voice: str, tts_settings: dict) -> str:
    del model  # The selected model is encoded in the full Azure voice ID.
    locale_match = re.match(r"^([A-Za-z]{2,3}-[A-Za-z]{2,3})-", voice)
    locale = locale_match.group(1) if locale_match else "en-US"
    escaped_locale = escape_xml(locale, {'"': "&quot;", "'": "&apos;"})
    escaped_voice = escape_xml(voice, {'"': "&quot;", "'": "&apos;"})
    escaped_text = escape_xml(str(text or ""))

    speed = _coerce_float(tts_settings.get("speed"), 1.0)
    if not math.isfinite(speed):
        speed = 1.0
    speed = min(2.0, max(0.5, speed))
    rate_percent = round((speed - 1.0) * 100)
    rate = f"{rate_percent:+d}%"
    body = f'<prosody rate="{rate}">{escaped_text}</prosody>'

    style = str(tts_settings.get("azure_speech_style") or "").strip()
    if style:
        escaped_style = escape_xml(style, {'"': "&quot;", "'": "&apos;"})
        style_attributes = [f'style="{escaped_style}"']
        raw_style_degree = tts_settings.get("azure_speech_style_degree")
        if raw_style_degree not in (None, ""):
            style_degree = _coerce_float(raw_style_degree, math.nan)
            if not math.isfinite(style_degree) or not 0.01 <= style_degree <= 2.0:
                raise ValueError(
                    "Azure Speech style degree must be between 0.01 and 2.0."
                )
            style_attributes.append(f'styledegree="{style_degree:g}"')
        attributes = " ".join(style_attributes)
        body = f"<mstts:express-as {attributes}>{body}</mstts:express-as>"

    return (
        '<speak version="1.0" '
        'xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="http://www.w3.org/2001/mstts" '
        f'xml:lang="{escaped_locale}">'
        f'<voice xml:lang="{escaped_locale}" name="{escaped_voice}">'
        f"{body}</voice></speak>"
    )


def _request_azure_speech_audio(
    text: str, tts_settings: dict, endpoint: dict[str, object]
) -> requests.Response:
    base_url, model, voice, api_key = _validate_azure_speech_request(
        endpoint, tts_settings
    )
    request_defaults = endpoint.get("request_defaults")
    default_output_format = (
        request_defaults.get("output_format")
        if isinstance(request_defaults, dict)
        else ""
    )
    output_format = (
        str(
            tts_settings.get("azure_speech_output_format")
            or tts_settings.get("output_format")
            or default_output_format
            or AZURE_SPEECH_OUTPUT_FORMAT
        ).strip()
        or AZURE_SPEECH_OUTPUT_FORMAT
    )
    speech_path = str(endpoint.get("speech_path") or "/cognitiveservices/v1").strip()
    ssml = _azure_speech_ssml(text, model, voice, tts_settings)
    url = _configured_endpoint_url(base_url, speech_path)
    try:
        return requests.post(
            url,
            headers={
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": output_format,
                "Ocp-Apim-Subscription-Key": api_key,
            },
            data=ssml,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout as error:
        raise RuntimeError("Azure Speech request timed out.") from error
    except requests.exceptions.RequestException as error:
        raise RuntimeError(f"Azure Speech request failed: {error}") from error


def _elevenlabs_base_url(base_url: str | None = "") -> str:
    normalized = _normalize_base_url(base_url, ELEVENLABS_API_BASE_URL)
    if normalized.lower().endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    return normalized


def _elevenlabs_auth_headers(api_key: str, *, audio: bool = False) -> dict[str, str]:
    normalized_key = str(api_key or "").strip()
    headers = {"Accept": "audio/mpeg" if audio else "application/json"}
    if normalized_key:
        headers["xi-api-key"] = normalized_key
    if audio:
        headers["Content-Type"] = "application/json"
    return headers


def _resolve_elevenlabs_api_key(tts_settings: dict | None = None) -> str:
    service = get_service_config(tts_settings or {}, ELEVENLABS_PROVIDER) or {}
    key_env = str(service.get("api_key_env") or "ELEVENLABS_API_KEY").strip()
    if key_env:
        value = os.getenv(key_env, "").strip()
        if value:
            return value
    return str(service.get("api_key") or "").strip()


def _resolve_elevenlabs_endpoint_api_key(endpoint: dict[str, object]) -> str:
    key_env = str(endpoint.get("api_key_env") or "").strip()
    if key_env:
        value = os.getenv(key_env, "").strip()
        if value:
            return value
    return str(endpoint.get("api_key") or "").strip()


def _elevenlabs_catalog_status(error: BaseException) -> int:
    response = getattr(error, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _request_elevenlabs_audio(
    text: str,
    tts_settings: dict,
    *,
    endpoint: dict[str, object] | None = None,
) -> requests.Response:
    """Call ElevenLabs' native text-to-speech endpoint.

    ElevenLabs is deliberately kept out of the OpenAI/LiteLLM path. Its
    request uses ``xi-api-key``, a path voice identifier, and an
    ``output_format`` query parameter rather than an OpenAI JSON speech
    contract.
    """
    service = get_service_config(tts_settings, ELEVENLABS_PROVIDER) or {}
    selected_endpoint = endpoint or service
    if endpoint is not None:
        api_key = _resolve_elevenlabs_endpoint_api_key(selected_endpoint)
    else:
        api_key = _resolve_elevenlabs_api_key(tts_settings)
    if not api_key:
        raise ValueError("An ElevenLabs API key is required for speech generation.")

    voice_id = str(
        tts_settings.get("elevenlabs_voice_id")
        or tts_settings.get("speaker")
        or tts_settings.get("voice")
        or selected_endpoint.get("default_voice")
        or ""
    ).strip()
    if not voice_id:
        raise ValueError("Select an ElevenLabs voice before generating speech.")

    model_id = str(
        tts_settings.get("elevenlabs_model")
        or tts_settings.get("xtts_model")
        or tts_settings.get("model")
        or selected_endpoint.get("default_model")
        or ELEVENLABS_TTS_DEFAULT_MODEL
    ).strip()
    request_defaults = selected_endpoint.get("request_defaults")
    default_output_format = (
        request_defaults.get("output_format")
        if isinstance(request_defaults, dict)
        else ""
    )
    output_format = (
        str(
            tts_settings.get("elevenlabs_output_format")
            or tts_settings.get("output_format")
            or tts_settings.get("response_format")
            or default_output_format
            or ELEVENLABS_TTS_OUTPUT_FORMAT
        ).strip()
        or ELEVENLABS_TTS_OUTPUT_FORMAT
    )
    payload = {"text": str(text), "model_id": model_id}
    language_code = normalize_elevenlabs_language_code(tts_settings.get("language"))
    if (
        language_code
        and model_id.lower() not in ELEVENLABS_MODELS_WITHOUT_LANGUAGE_CODE
    ):
        payload["language_code"] = language_code
    base_url = _elevenlabs_base_url(
        str(selected_endpoint.get("api_base") or ELEVENLABS_API_BASE_URL)
    )
    url = f"{base_url}/v1/text-to-speech/{quote(voice_id, safe='')}"
    try:
        return requests.post(
            url,
            headers=_elevenlabs_auth_headers(api_key, audio=True),
            params={"output_format": output_format},
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout as error:
        raise RuntimeError("ElevenLabs speech request timed out.") from error
    except requests.exceptions.RequestException as error:
        raise RuntimeError(f"ElevenLabs speech request failed: {error}") from error


def get_elevenlabs_model_catalog(
    base_url: str = ELEVENLABS_API_BASE_URL,
    *,
    api_key: str = "",
    strict: bool = False,
) -> list[dict[str, object]]:
    """Fetch the currently available TTS models and authoritative languages."""
    url = f"{_elevenlabs_base_url(base_url)}/v1/models"
    try:
        response = requests.get(
            url,
            headers=_elevenlabs_auth_headers(api_key),
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.exceptions.RequestException, ValueError) as error:
        if strict:
            raise ElevenLabsCatalogError(
                "models", _elevenlabs_catalog_status(error)
            ) from error
        logging.warning("Could not list ElevenLabs models: %s", error)
        return []

    if not isinstance(payload, list):
        if strict:
            raise ElevenLabsCatalogError("models")
        return []
    models: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict) or item.get("can_do_text_to_speech") is False:
            continue
        model_id = str(item.get("model_id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        entry: dict[str, object] = {
            "id": model_id,
            "model_id": model_id,
            "name": str(item.get("name") or model_id).strip(),
        }
        languages = item.get("languages")
        if isinstance(languages, list):
            authoritative_languages = []
            for language in languages:
                if not isinstance(language, dict):
                    continue
                language_id = str(language.get("language_id") or "").strip()
                name = str(language.get("name") or "").strip()
                if language_id:
                    authoritative_languages.append(
                        {"language_id": language_id, **({"name": name} if name else {})}
                    )
            if authoritative_languages:
                entry["languages"] = authoritative_languages
        description = str(item.get("description") or "").strip()
        if description:
            entry["description"] = description
        models.append(entry)
    return models


def get_elevenlabs_voice_catalog(
    base_url: str = ELEVENLABS_API_BASE_URL,
    *,
    api_key: str = "",
    strict: bool = False,
) -> list[dict[str, object]]:
    """Fetch voice IDs and metadata from ElevenLabs' current v2 voices API."""
    url = f"{_elevenlabs_base_url(base_url)}/v2/voices"
    params: dict[str, object] = {"show_legacy": "true", "page_size": 100}
    voices: list[dict[str, object]] = []
    seen: set[str] = set()
    try:
        for _page in range(20):
            response = requests.get(
                url,
                headers=_elevenlabs_auth_headers(api_key),
                params=params,
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                if strict:
                    raise ElevenLabsCatalogError("voices")
                break
            data = payload.get("voices")
            if not isinstance(data, list):
                if strict:
                    raise ElevenLabsCatalogError("voices")
                break
            for item in data:
                if not isinstance(item, dict):
                    continue
                voice_id = str(item.get("voice_id") or "").strip()
                if not voice_id or voice_id in seen:
                    continue
                seen.add(voice_id)
                voices.append(dict(item))
            next_page = str(payload.get("next_page_token") or "").strip()
            if not next_page:
                break
            params["page_token"] = next_page
    except (requests.exceptions.RequestException, ValueError) as error:
        if strict:
            raise ElevenLabsCatalogError(
                "voices", _elevenlabs_catalog_status(error)
            ) from error
        logging.warning("Could not list ElevenLabs voices: %s", error)
    return voices


def _resolve_service_api_key(
    tts_settings: dict | None, service_id: str, default_env: str
) -> str:
    service = get_service_config(tts_settings or {}, service_id) or {}
    key_env = str(service.get("api_key_env") or default_env).strip()
    if key_env:
        api_key = os.getenv(key_env, "").strip()
        if api_key:
            return api_key
    explicit_key = str(service.get("api_key") or "").strip()
    return explicit_key or XTTS_OPENAI_PLACEHOLDER_API_KEY


def _resolve_voxcpm_api_key(tts_settings: dict | None = None) -> str:
    return _resolve_service_api_key(tts_settings, "voxcpm", "VOXCPM_API_KEY")


def _resolve_fishs2_api_key(tts_settings: dict | None = None) -> str:
    return _resolve_service_api_key(tts_settings, "fishs2", "FISHS2_API_KEY")


def _resolve_voxtral_api_key(tts_settings: dict | None = None) -> str:
    return _resolve_service_api_key(tts_settings, "voxtral", "VOXTRAL_API_KEY")


def _resolve_kokoro_api_key(tts_settings: dict | None = None) -> str:
    return _resolve_service_api_key(tts_settings, "kokoro", "KOKORO_API_KEY")


def _resolve_kobold_qwen_api_key(tts_settings: dict | None = None) -> str:
    return _resolve_service_api_key(tts_settings, "kobold_qwen", "KOBOLD_QWEN_API_KEY")


def check_openai_audio_connection(tts_settings: dict) -> tuple[bool, str]:
    """Checks custom audio endpoint reachability."""
    endpoint, error = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        return False, error

    if _normalize_custom_adapter(endpoint.get("adapter")) == AUDIO_CPP_ADAPTER:
        try:
            response = requests.get(
                _configured_endpoint_url(str(endpoint["base_url"]), "/health"),
                headers=_configured_endpoint_auth_headers(endpoint),
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.exceptions.RequestException, ValueError) as error:
            return False, f"Could not connect to audio.cpp endpoint: {error}"
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return False, "audio.cpp returned an invalid health response."
        model_count = payload.get("models")
        model_summary = (
            str(model_count) if isinstance(model_count, int) else "unknown number of"
        )
        return (
            True,
            (
                f"Connected to audio.cpp ({payload.get('backend') or 'unknown'} backend, "
                f"{model_summary} configured models)."
            ),
        )

    if _normalize_custom_adapter(endpoint.get("adapter")) == AZURE_SPEECH_ADAPTER:
        speech_path = str(
            endpoint.get("speech_path") or "/cognitiveservices/v1"
        ).strip()
        if not speech_path:
            return (
                False,
                f"Endpoint '{endpoint['name']}' has no configured speech route.",
            )
        try:
            response = requests.get(
                _configured_endpoint_url(str(endpoint["base_url"]), speech_path),
                headers=_configured_endpoint_auth_headers(endpoint),
                timeout=8,
            )
        except requests.exceptions.RequestException as error:
            return False, f"Could not connect to endpoint '{endpoint['name']}': {error}"
        if response.status_code in {401, 403}:
            return (
                False,
                f"Endpoint '{endpoint['name']}' rejected the configured Azure Speech subscription key with status {response.status_code}.",
            )
        if response.status_code == 404 or response.status_code >= 500:
            return (
                False,
                f"Endpoint '{endpoint['name']}' returned {response.status_code} for configured speech route {speech_path}.",
            )
        return (
            True,
            f"Connected to endpoint '{endpoint['name']}' at configured speech route {speech_path}.",
        )

    if _normalize_custom_adapter(endpoint.get("adapter")) == GENERIC_JSON_ADAPTER:
        speech_path = str(endpoint.get("speech_path") or "").strip()
        if not speech_path:
            return (
                False,
                f"Endpoint '{endpoint['name']}' has no configured speech route.",
            )
        try:
            response = requests.get(
                _configured_endpoint_url(str(endpoint["base_url"]), speech_path),
                headers=_configured_endpoint_auth_headers(endpoint),
                timeout=8,
            )
        except requests.exceptions.RequestException as e:
            return False, f"Could not connect to endpoint '{endpoint['name']}': {e}"

        if response.status_code in {401, 403}:
            return (
                False,
                f"Endpoint '{endpoint['name']}' rejected the configured API key "
                f"with status {response.status_code}.",
            )
        if response.status_code == 404 or response.status_code >= 500:
            return (
                False,
                f"Endpoint '{endpoint['name']}' returned {response.status_code} "
                f"for configured speech route {speech_path}.",
            )
        return (
            True,
            f"Connected to endpoint '{endpoint['name']}' at configured speech route {speech_path}.",
        )

    if _normalize_custom_adapter(endpoint.get("adapter")) == ELEVENLABS_NATIVE_ADAPTER:
        api_key = _resolve_elevenlabs_endpoint_api_key(endpoint)
        try:
            response = requests.get(
                f"{_elevenlabs_base_url(str(endpoint.get('base_url') or ''))}/v1/models",
                headers=_elevenlabs_auth_headers(api_key),
                timeout=8,
            )
        except requests.exceptions.RequestException as error:
            return False, f"Could not connect to endpoint '{endpoint['name']}': {error}"
        if response.status_code in {401, 403}:
            return (
                False,
                f"Endpoint '{endpoint['name']}' rejected the configured ElevenLabs API key with status {response.status_code}.",
            )
        if response.status_code >= 400:
            return (
                False,
                f"Endpoint '{endpoint['name']}' returned {response.status_code} when listing ElevenLabs models.",
            )
        return True, f"Connected to endpoint '{endpoint['name']}'."

    api_key = _resolve_openai_audio_api_key(endpoint)
    last_status = None
    last_text = ""

    for models_url in _configured_openai_urls(
        endpoint,
        "models_path",
        _openai_models_urls(str(endpoint["base_url"])),
    ):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                last_status = response.status_code
                last_text = response.text
                continue

            if response.status_code >= 400:
                return (
                    False,
                    f"Endpoint '{endpoint['name']}' returned {response.status_code} when listing models: {response.text}",
                )

            return True, f"Connected to endpoint '{endpoint['name']}'."
        except requests.exceptions.RequestException as e:
            return False, f"Could not connect to endpoint '{endpoint['name']}': {e}"

    speech_path = str(endpoint.get("speech_path") or "").strip()
    if speech_path:
        try:
            response = requests.get(
                _configured_endpoint_url(str(endpoint["base_url"]), speech_path),
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if response.status_code not in {404, 501} and response.status_code < 500:
                return (
                    True,
                    f"Connected to endpoint '{endpoint['name']}' at {speech_path}.",
                )
        except requests.exceptions.RequestException:
            pass

    return False, (
        f"Endpoint '{endpoint['name']}' does not expose a reachable models or speech route. "
        f"Last status: {last_status or 'N/A'}. {last_text}"
    )


def _resolve_openai_audio_provider_context(
    tts_settings: dict,
) -> tuple[dict[str, object] | None, str, str, str]:
    endpoint, _ = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        return None, "", "", ""

    provider = _infer_audio_provider(
        name=endpoint.get("name", ""),
        base_url=endpoint.get("base_url", ""),
        raw_provider=endpoint.get("provider", ""),
    )

    profile_id = str(endpoint.get("profile_id") or "")
    default_model = str(endpoint.get("default_model", "")).strip()
    if not default_model and not profile_id:
        default_model = _provider_default_model(provider)
    default_model = _normalize_model_for_provider(default_model, provider)

    default_voice = str(endpoint.get("default_voice", "")).strip()
    if not default_voice and not profile_id:
        default_voice = _provider_default_voice(provider)
    default_voice = _normalize_voice_for_provider(default_voice, provider)

    return endpoint, provider, default_model, default_voice


def get_openai_audio_models_fallback(tts_settings: dict) -> list[str]:
    """Returns fallback model suggestions for custom audio providers."""
    endpoint, provider, default_model, _ = _resolve_openai_audio_provider_context(
        tts_settings
    )
    if endpoint is None:
        return []

    builtin_models = (
        _provider_model_catalog(provider)
        if (
            _normalize_custom_adapter(endpoint.get("adapter")) == OPENAI_COMPAT_ADAPTER
            and not endpoint.get("profile_id")
        )
        else []
    )
    preferred_models = (
        [default_model] + list(endpoint.get("models") or []) + builtin_models
    )
    return _dedupe_ordered(preferred_models)


def get_openai_audio_voices_fallback(tts_settings: dict) -> list[str]:
    """Returns fallback voice suggestions for custom audio providers."""
    endpoint, provider, default_model, default_voice = (
        _resolve_openai_audio_provider_context(tts_settings)
    )
    if endpoint is None:
        return []

    selected_model = (
        str(tts_settings.get("model") or tts_settings.get("xtts_model") or "").strip()
        or default_model
    )
    selected_model = _normalize_model_for_provider(selected_model, provider)

    builtin_voices = (
        _provider_voice_catalog(provider, selected_model)
        if (
            _normalize_custom_adapter(endpoint.get("adapter")) == OPENAI_COMPAT_ADAPTER
            and not endpoint.get("profile_id")
        )
        else []
    )
    preferred_voices = (
        [default_voice] + list(endpoint.get("voices") or []) + builtin_voices
    )
    return _dedupe_ordered(preferred_voices)


def _extract_generic_catalog(payload, kind: str) -> list[str]:
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        singular = "model" if kind == "models" else "voice"
        candidates = []
        for key in (kind, "data", "items", singular):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, (str, int)):
                candidates.append(value)
    else:
        return []

    id_keys = (
        ("id", "model_id", "model", "name")
        if kind == "models"
        else ("id", "voice_id", "speaker_id", "voice", "speaker", "name")
    )
    values: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            item = next(
                (item.get(key) for key in id_keys if item.get(key) is not None), ""
            )
        normalized = str(item or "").strip()
        if normalized:
            values.append(normalized)
    return _dedupe_ordered(values)


def _audio_cpp_model_is_supported(item: dict[str, object]) -> bool:
    model_id = str(item.get("id") or "").strip().casefold()
    family = str(item.get("family") or "").strip().lower()
    # Breeze is present only on audio.cpp's development line. Keep it out of
    # Pandrator until a stable release package is available.
    return family != "breeze_tts" and not model_id.startswith("breeze")


def get_audio_cpp_model_catalog(
    base_url: str,
    *,
    models_path: str = "/v1/models",
    headers: dict[str, str] | None = None,
    request_session: requests.Session | None = None,
) -> list[dict[str, object]]:
    """Fetch configured speech-capable models without leaking server paths."""

    client = request_session or requests
    with _audio_cpp_endpoint_lock_for(base_url):
        response = client.get(
            _configured_endpoint_url(base_url, models_path),
            headers=headers or {},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    candidates = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        raise ValueError(  # noqa: TRY004 - malformed remote payload, not caller type
            "audio.cpp returned an invalid model catalogue."
        )

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_item in candidates:
        if not isinstance(raw_item, dict):
            continue
        model_id = str(raw_item.get("id") or "").strip()
        task = str(raw_item.get("task") or "").strip().lower()
        if not model_id or model_id in seen:
            continue
        if task and task not in {"tts", "clon", "vdes"}:
            continue
        if not _audio_cpp_model_is_supported(raw_item):
            continue
        seen.add(model_id)
        item: dict[str, object] = {"id": model_id}
        for key in ("object", "owned_by", "family", "task", "mode", "loaded"):
            value = raw_item.get(key)
            if isinstance(value, (str, bool, int, float)):
                item[key] = value
        result.append(item)
    return result


def get_audio_cpp_voice_catalog(
    base_url: str,
    model: str,
    *,
    voices_path: str = "/v1/audio/voices",
    headers: dict[str, str] | None = None,
    request_session: requests.Session | None = None,
) -> list[str]:
    """Fetch voice presets and voice-dir entries for one configured model."""

    client = request_session or requests
    with _audio_cpp_endpoint_lock_for(base_url):
        response = client.get(
            _configured_endpoint_url(base_url, voices_path),
            headers=headers or {},
            params={"model": model},
            timeout=8,
        )
        response.raise_for_status()
        return _extract_generic_catalog(response.json(), "voices")


def get_openai_audio_models(tts_settings: dict) -> list[str]:
    """Fetches model IDs from the configured custom audio endpoint."""
    endpoint, provider, default_model, _ = _resolve_openai_audio_provider_context(
        tts_settings
    )
    if endpoint is None:
        return []

    if _normalize_custom_adapter(endpoint.get("adapter")) == AUDIO_CPP_ADAPTER:
        entries = get_audio_cpp_model_catalog(
            str(endpoint["base_url"]),
            models_path=str(endpoint.get("models_path") or "/v1/models"),
            headers=_configured_endpoint_auth_headers(endpoint),
        )
        return [str(item["id"]) for item in entries]

    if _normalize_custom_adapter(endpoint.get("adapter")) == AZURE_SPEECH_ADAPTER:
        return _dedupe_ordered(
            [default_model] + _azure_speech_catalog_values(endpoint, "", "models")
        )

    if _normalize_custom_adapter(endpoint.get("adapter")) == GENERIC_JSON_ADAPTER:
        models = list(endpoint.get("models") or [])
        models_path = str(endpoint.get("models_path") or "").strip()
        if models_path:
            try:
                response = requests.get(
                    _configured_endpoint_url(str(endpoint["base_url"]), models_path),
                    headers=_configured_endpoint_auth_headers(endpoint),
                    timeout=8,
                )
                response.raise_for_status()
                models = _dedupe_ordered(
                    models + _extract_generic_catalog(response.json(), "models")
                )
            except (requests.exceptions.RequestException, ValueError) as e:
                logging.debug(
                    "Could not list models for endpoint '%s': %s", endpoint["name"], e
                )
        return models

    if _normalize_custom_adapter(endpoint.get("adapter")) == ELEVENLABS_NATIVE_ADAPTER:
        discovered = get_elevenlabs_model_catalog(
            str(endpoint.get("base_url") or ELEVENLABS_API_BASE_URL),
            api_key=_resolve_elevenlabs_endpoint_api_key(endpoint),
        )
        return _merge_catalog_with_discovered(
            [default_model] + list(endpoint.get("models") or []),
            [str(item.get("id") or "") for item in discovered],
        )

    models: list[str] = []
    for models_url in _configured_openai_urls(
        endpoint,
        "models_path",
        _openai_models_urls(str(endpoint["base_url"])),
    ):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(_resolve_openai_audio_api_key(endpoint)),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            models = [
                _normalize_model_for_provider(model, provider)
                for model in _extract_models_from_openai_payload(response.json())
            ]
            models = [m for m in models if "tts" in m.lower()]
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error(
                "Failed to list models for endpoint '%s': %s", endpoint["name"], e
            )
            continue

    builtin_models = (
        [] if endpoint.get("profile_id") else _provider_model_catalog(provider)
    )
    preferred_models = (
        [default_model] + list(endpoint.get("models") or []) + builtin_models
    )

    return _merge_catalog_with_discovered(preferred_models, models)


def get_openai_audio_voices(tts_settings: dict) -> list[str]:
    """Fetches voice IDs from the configured custom audio endpoint."""
    endpoint, provider, default_model, default_voice = (
        _resolve_openai_audio_provider_context(tts_settings)
    )
    if endpoint is None:
        return []

    if _normalize_custom_adapter(endpoint.get("adapter")) == AUDIO_CPP_ADAPTER:
        selected_model = str(
            tts_settings.get("model")
            or tts_settings.get("xtts_model")
            or default_model
            or ""
        ).strip()
        if not selected_model:
            return []
        return get_audio_cpp_voice_catalog(
            str(endpoint["base_url"]),
            selected_model,
            voices_path=str(endpoint.get("voices_path") or "/v1/audio/voices"),
            headers=_configured_endpoint_auth_headers(endpoint),
        )

    if _normalize_custom_adapter(endpoint.get("adapter")) == AZURE_SPEECH_ADAPTER:
        selected_model = str(
            tts_settings.get("model") or tts_settings.get("xtts_model") or ""
        ).strip()
        selected_model = selected_model or default_model
        selected_model = _normalize_model_for_provider(selected_model, provider)
        default_voices = endpoint.get("default_voices")
        model_default_voice = (
            str(default_voices.get(selected_model) or "").strip()
            if isinstance(default_voices, dict)
            else ""
        )
        return _dedupe_ordered(
            [model_default_voice or default_voice]
            + _azure_speech_catalog_values(endpoint, selected_model, "voices")
        )

    if _normalize_custom_adapter(endpoint.get("adapter")) == GENERIC_JSON_ADAPTER:
        voices = list(endpoint.get("voices") or [])
        voices_path = str(endpoint.get("voices_path") or "").strip()
        if voices_path:
            try:
                response = requests.get(
                    _configured_endpoint_url(str(endpoint["base_url"]), voices_path),
                    headers=_configured_endpoint_auth_headers(endpoint),
                    timeout=8,
                )
                response.raise_for_status()
                voices = _dedupe_ordered(
                    voices + _extract_generic_catalog(response.json(), "voices")
                )
            except (requests.exceptions.RequestException, ValueError) as e:
                logging.debug(
                    "Could not list voices for endpoint '%s': %s", endpoint["name"], e
                )
        return voices

    if _normalize_custom_adapter(endpoint.get("adapter")) == ELEVENLABS_NATIVE_ADAPTER:
        discovered = get_elevenlabs_voice_catalog(
            str(endpoint.get("base_url") or ELEVENLABS_API_BASE_URL),
            api_key=_resolve_elevenlabs_endpoint_api_key(endpoint),
        )
        return _merge_catalog_with_discovered(
            [default_voice] + list(endpoint.get("voices") or []),
            [str(item.get("voice_id") or "") for item in discovered],
        )

    voices: list[str] = []
    for voices_url in _configured_openai_urls(
        endpoint,
        "voices_path",
        _openai_voice_catalog_urls(str(endpoint["base_url"])),
    ):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(_resolve_openai_audio_api_key(endpoint)),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            voices = [
                _normalize_voice_for_provider(voice, provider)
                for voice in _extract_voices_from_openai_payload(response.json())
            ]
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug(
                "Could not list voices for endpoint '%s': %s", endpoint["name"], e
            )
            continue

    selected_model = str(tts_settings.get("xtts_model") or "").strip()
    if not selected_model:
        selected_model = default_model
    selected_model = _normalize_model_for_provider(selected_model, provider)

    builtin_voices = (
        []
        if endpoint.get("profile_id")
        else _provider_voice_catalog(provider, selected_model)
    )
    preferred_voices = (
        [default_voice] + list(endpoint.get("voices") or []) + builtin_voices
    )

    return _merge_catalog_with_discovered(preferred_voices, voices)


def check_voxcpm_connection(base_url: str = VOXCPM_API_BASE_URL) -> bool:
    """Checks if the VoxCPM server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, VOXCPM_API_BASE_URL)
    api_key = _resolve_voxcpm_api_key()

    probe_urls = [
        f"{normalized_base_url}/health",
        *_openai_models_urls(normalized_base_url),
        *_openai_voice_catalog_urls(normalized_base_url),
        *_openai_files_urls(normalized_base_url),
    ]

    for probe_url in _dedupe_ordered(probe_urls):
        try:
            response = requests.get(
                probe_url,
                headers=_openai_auth_headers(api_key),
                timeout=4,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            continue

    return False


def get_voxcpm_models(base_url: str = VOXCPM_API_BASE_URL) -> list[str]:
    """Fetches available VoxCPM models from server."""
    normalized_base_url = _normalize_base_url(base_url, VOXCPM_API_BASE_URL)
    api_key = _resolve_voxcpm_api_key()

    discovered_models: list[str] = []
    for models_url in _openai_models_urls(normalized_base_url):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_models = [
                _normalize_voxcpm_model(model, fallback="")
                for model in _extract_models_from_openai_payload(response.json())
            ]
            discovered_models = [model for model in discovered_models if model]
            if discovered_models:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list VoxCPM models from %s: %s", models_url, e)
            continue

    return _merge_catalog_with_discovered(VOXCPM_TTS_MODELS, discovered_models)


def get_voxcpm_voices(base_url: str = VOXCPM_API_BASE_URL) -> list[str]:
    """Fetches available VoxCPM voices from server."""
    normalized_base_url = _normalize_base_url(base_url, VOXCPM_API_BASE_URL)
    api_key = _resolve_voxcpm_api_key()

    discovered_voices: list[str] = []
    voice_urls = _dedupe_ordered(
        _openai_voice_catalog_urls(normalized_base_url)
        + _openai_files_urls(normalized_base_url)
    )

    for voices_url in voice_urls:
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_voices = _extract_voices_from_openai_payload(response.json())
            if discovered_voices:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list VoxCPM voices from %s: %s", voices_url, e)
            continue

    return _merge_catalog_with_discovered([VOXCPM_DEFAULT_VOICE], discovered_voices)


def check_fishs2_connection(base_url: str = FISHS2_API_BASE_URL) -> bool:
    """Checks if the FishS2 server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, FISHS2_API_BASE_URL)
    api_key = _resolve_fishs2_api_key()

    probe_urls = [
        f"{normalized_base_url}/health",
        *_openai_models_urls(normalized_base_url),
        *_openai_voice_catalog_urls(normalized_base_url),
        *_openai_files_urls(normalized_base_url),
    ]

    for probe_url in _dedupe_ordered(probe_urls):
        try:
            response = requests.get(
                probe_url,
                headers=_openai_auth_headers(api_key),
                timeout=4,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            continue

    return False


def get_fishs2_models(base_url: str = FISHS2_API_BASE_URL) -> list[str]:
    """Fetches available FishS2 models from server."""
    normalized_base_url = _normalize_base_url(base_url, FISHS2_API_BASE_URL)
    api_key = _resolve_fishs2_api_key()

    discovered_models: list[str] = []
    for models_url in _openai_models_urls(normalized_base_url):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_models = [
                _normalize_fishs2_model(model, fallback="")
                for model in _extract_models_from_openai_payload(response.json())
            ]
            discovered_models = [model for model in discovered_models if model]
            if discovered_models:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list FishS2 models from %s: %s", models_url, e)
            continue

    return normalize_tts_model_catalog(
        "fishs2",
        [FISHS2_DEFAULT_MODEL, *discovered_models],
    )


def get_fishs2_voices(base_url: str = FISHS2_API_BASE_URL) -> list[str]:
    """Fetches available FishS2 voices from server."""
    normalized_base_url = _normalize_base_url(base_url, FISHS2_API_BASE_URL)
    api_key = _resolve_fishs2_api_key()

    discovered_voices: list[str] = []
    voice_urls = _dedupe_ordered(
        _openai_voice_catalog_urls(normalized_base_url)
        + _openai_files_urls(normalized_base_url)
    )

    for voices_url in voice_urls:
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_voices = _extract_voices_from_openai_payload(response.json())
            if discovered_voices:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list FishS2 voices from %s: %s", voices_url, e)
            continue

    return _merge_catalog_with_discovered([FISHS2_DEFAULT_VOICE], discovered_voices)


def check_chatterbox_connection(base_url: str = CHATTERBOX_API_BASE_URL) -> bool:
    """Checks if the Chatterbox server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, CHATTERBOX_API_BASE_URL)
    probe_urls = [
        f"{normalized_base_url}/health",
        *_openai_models_urls(normalized_base_url),
        *_openai_voice_catalog_urls(normalized_base_url),
        *_openai_files_urls(normalized_base_url),
    ]

    for probe_url in _dedupe_ordered(probe_urls):
        try:
            response = requests.get(
                probe_url,
                headers=_openai_auth_headers(XTTS_OPENAI_PLACEHOLDER_API_KEY),
                timeout=4,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            continue

    return False


def get_chatterbox_models(base_url: str = CHATTERBOX_API_BASE_URL) -> list[str]:
    """Fetches available Chatterbox models from server."""
    normalized_base_url = _normalize_base_url(base_url, CHATTERBOX_API_BASE_URL)

    discovered_models: list[str] = []
    for models_url in _openai_models_urls(normalized_base_url):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(XTTS_OPENAI_PLACEHOLDER_API_KEY),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_models = _extract_models_from_openai_payload(response.json())
            if discovered_models:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Chatterbox models from %s: %s", models_url, e)
            continue

    return _merge_catalog_with_discovered(CHATTERBOX_TTS_MODELS, discovered_models)


def get_chatterbox_voices(base_url: str = CHATTERBOX_API_BASE_URL) -> list[str]:
    """Fetches available Chatterbox voices from server."""
    normalized_base_url = _normalize_base_url(base_url, CHATTERBOX_API_BASE_URL)

    discovered_voices: list[str] = []
    voice_urls = _dedupe_ordered(
        _openai_voice_catalog_urls(normalized_base_url)
        + _openai_files_urls(normalized_base_url)
    )

    for voices_url in voice_urls:
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(XTTS_OPENAI_PLACEHOLDER_API_KEY),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_voices = _extract_voices_from_openai_payload(response.json())
            if discovered_voices:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Chatterbox voices from %s: %s", voices_url, e)
            continue

    return _dedupe_ordered(discovered_voices)


def check_kobold_qwen_connection(base_url: str = KOBOLD_QWEN_API_BASE_URL) -> bool:
    """Checks if the Qwen3 TTS server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    api_key = _resolve_kobold_qwen_api_key()
    probe_urls = [
        f"{normalized_base_url}/health",
        *_openai_models_urls(normalized_base_url),
        *_openai_voice_catalog_urls(normalized_base_url),
        *_openai_files_urls(normalized_base_url),
    ]

    for probe_url in _dedupe_ordered(probe_urls):
        try:
            response = requests.get(
                probe_url,
                headers=_openai_auth_headers(api_key),
                timeout=4,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            continue

    return False


def get_kobold_qwen_models(base_url: str = KOBOLD_QWEN_API_BASE_URL) -> list[str]:
    """Fetches available Qwen3 TTS models from server."""
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    api_key = _resolve_kobold_qwen_api_key()

    discovered_models: list[str] = []
    for models_url in _openai_models_urls(normalized_base_url):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_models = _extract_models_from_openai_payload(response.json())
            if discovered_models:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Qwen3 TTS models from %s: %s", models_url, e)
            continue

    return _merge_catalog_with_discovered(KOBOLD_QWEN_TTS_MODELS, discovered_models)


def get_kobold_qwen_voice_catalog(
    base_url: str = KOBOLD_QWEN_API_BASE_URL, api_key: str = ""
) -> list[dict[str, str]]:
    """Fetch Qwen voices while retaining the API's cloned/preset model metadata."""
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    api_key = str(api_key or "").strip() or _resolve_kobold_qwen_api_key()

    discovered: list[dict[str, str]] = []
    voice_urls = _dedupe_ordered(
        _openai_voice_catalog_urls(normalized_base_url)
        + _openai_files_urls(normalized_base_url)
    )

    for voices_url in voice_urls:
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                candidates = payload
            elif isinstance(payload, dict):
                candidates = next(
                    (
                        payload.get(key)
                        for key in ("data", "voices", "items")
                        if isinstance(payload.get(key), list)
                    ),
                    [],
                )
            else:
                candidates = []
            for item in candidates:
                if isinstance(item, dict):
                    voice_id = str(
                        item.get("voice_id") or item.get("id") or item.get("name") or ""
                    ).strip()
                    if not voice_id:
                        continue
                    voice_type = str(item.get("type") or "").strip().lower()
                    model = str(item.get("model") or "").strip()
                    if not voice_type:
                        voice_type = (
                            "preset"
                            if voice_id.lower()
                            in {voice.lower() for voice in KOBOLD_QWEN_TTS_VOICES}
                            else "cloned"
                        )
                    discovered.append(
                        {"id": voice_id, "type": voice_type, "model": model}
                    )
                else:
                    voice_id = str(item or "").strip()
                    if voice_id:
                        discovered.append(
                            {"id": voice_id, "type": "cloned", "model": "Voice Cloning"}
                        )
            if discovered:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug("Could not list Qwen3 TTS voices from %s: %s", voices_url, e)
            continue

    by_id = {item["id"].lower(): item for item in discovered}
    for voice_id in KOBOLD_QWEN_TTS_VOICES:
        by_id.setdefault(
            voice_id.lower(),
            {"id": voice_id, "type": "preset", "model": KOBOLD_QWEN_DEFAULT_MODEL},
        )
    by_id.setdefault(
        KOBOLD_QWEN_SAMPLE_VOICE,
        {"id": KOBOLD_QWEN_SAMPLE_VOICE, "type": "cloned", "model": "Voice Cloning"},
    )
    return list(by_id.values())


def get_kobold_qwen_voices(base_url: str = KOBOLD_QWEN_API_BASE_URL) -> list[str]:
    """Fetches all available Qwen3 TTS voice IDs from server."""
    return [item["id"] for item in get_kobold_qwen_voice_catalog(base_url)]


def check_voxtral_connection(base_url: str = VOXTRAL_API_BASE_URL) -> bool:
    """Checks if the Voxtral server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, VOXTRAL_API_BASE_URL)
    api_key = _resolve_voxtral_api_key()

    probe_urls = [
        f"{normalized_base_url}/health",
        *_voxtral_models_urls(normalized_base_url),
        *_openai_models_urls(normalized_base_url),
        *_voxtral_voices_urls(normalized_base_url),
        *_openai_voice_catalog_urls(normalized_base_url),
    ]

    for probe_url in _dedupe_ordered(probe_urls):
        try:
            response = requests.get(
                probe_url,
                headers=_openai_auth_headers(api_key),
                timeout=4,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            continue

    return False


def get_voxtral_models(base_url: str = VOXTRAL_API_BASE_URL) -> list[str]:
    """Fetches available Voxtral models from server."""
    normalized_base_url = _normalize_base_url(base_url, VOXTRAL_API_BASE_URL)
    api_key = _resolve_voxtral_api_key()

    discovered_models: list[str] = []
    model_urls = _dedupe_ordered(
        _voxtral_models_urls(normalized_base_url)
        + _openai_models_urls(normalized_base_url)
    )

    for models_url in model_urls:
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            payload = response.json()
            discovered_models = _extract_models_from_voxtral_payload(payload)
            if not discovered_models:
                discovered_models = [
                    _normalize_voxtral_model(model, fallback="")
                    for model in _extract_models_from_openai_payload(payload)
                ]
                discovered_models = [model for model in discovered_models if model]

            if discovered_models:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Voxtral models from %s: %s", models_url, e)
            continue

    if discovered_models:
        return _merge_catalog_with_discovered(
            [VOXTRAL_DEFAULT_MODEL], discovered_models
        )

    preferred_models = [VOXTRAL_DEFAULT_MODEL] + VOXTRAL_TTS_MODELS
    return _dedupe_ordered(preferred_models)


def get_voxtral_voices(base_url: str = VOXTRAL_API_BASE_URL) -> list[str]:
    """Fetches available Voxtral voices from server."""
    normalized_base_url = _normalize_base_url(base_url, VOXTRAL_API_BASE_URL)
    api_key = _resolve_voxtral_api_key()

    discovered_voices: list[str] = []
    voice_urls = _dedupe_ordered(
        _voxtral_voices_urls(normalized_base_url)
        + _openai_voice_catalog_urls(normalized_base_url)
    )

    for voices_url in voice_urls:
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            payload = response.json()
            discovered_voices = _extract_voices_from_voxtral_payload(payload)
            if not discovered_voices:
                discovered_voices = _extract_voices_from_openai_payload(payload)

            if discovered_voices:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Voxtral voices from %s: %s", voices_url, e)
            continue

    return _merge_catalog_with_discovered([VOXTRAL_DEFAULT_VOICE], discovered_voices)


def check_kokoro_connection(base_url: str = KOKORO_API_BASE_URL) -> bool:
    """Checks if the Kokoro server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, KOKORO_API_BASE_URL)
    api_key = _resolve_kokoro_api_key()

    probe_urls = [
        f"{normalized_base_url}/health",
        *_kokoro_models_urls(normalized_base_url),
        *_kokoro_voices_urls(normalized_base_url),
    ]

    for probe_url in _dedupe_ordered(probe_urls):
        try:
            response = requests.get(
                probe_url,
                headers=_openai_auth_headers(api_key),
                timeout=4,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            continue

    return False


def get_kokoro_models(base_url: str = KOKORO_API_BASE_URL) -> list[str]:
    """Fetches available Kokoro models from server."""
    normalized_base_url = _normalize_base_url(base_url, KOKORO_API_BASE_URL)
    api_key = _resolve_kokoro_api_key()

    discovered_models: list[str] = []
    for models_url in _kokoro_models_urls(normalized_base_url):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_models = [
                model
                for model in _extract_models_from_openai_payload(response.json())
                if "tts" in model.lower() or model.lower() == "kokoro"
            ]
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Kokoro models from %s: %s", models_url, e)
            continue

    preferred_models = [KOKORO_DEFAULT_MODEL] + KOKORO_TTS_MODELS
    return _merge_catalog_with_discovered(preferred_models, discovered_models)


def get_kokoro_voices(base_url: str = KOKORO_API_BASE_URL) -> list[str]:
    """Fetches available Kokoro voices from server."""
    normalized_base_url = _normalize_base_url(base_url, KOKORO_API_BASE_URL)
    api_key = _resolve_kokoro_api_key()

    discovered_voices: list[str] = []
    for voices_url in _kokoro_voices_urls(normalized_base_url):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_voices = _extract_voices_from_openai_payload(response.json())
            if discovered_voices:
                break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Kokoro voices from %s: %s", voices_url, e)
            continue

    preferred_voices = [KOKORO_DEFAULT_VOICE] + KOKORO_TTS_VOICES
    return _merge_catalog_with_discovered(preferred_voices, discovered_voices)


def check_xtts_connection(base_url: str = XTTS_API_BASE_URL) -> bool:
    """Checks if the XTTS server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, XTTS_API_BASE_URL)
    probe_paths = ["/health", "/v1/models", "/docs", "/"]

    for path in probe_paths:
        try:
            response = requests.get(f"{normalized_base_url}{path}", timeout=3)
            if response.status_code == 404:
                continue
            if response.status_code < 500:
                return True
        except requests.exceptions.RequestException:
            continue

    return False


def check_silero_connection(base_url: str = SILERO_API_BASE_URL) -> bool:
    """Checks if the Silero server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, SILERO_API_BASE_URL)
    for path in ("/ready", "/health", "/v1/models"):
        try:
            response = requests.get(f"{normalized_base_url}{path}", timeout=4)
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            continue
    return False


# Magpie Functions
def check_magpie_connection(base_url: str = MAGPIE_API_BASE_URL) -> bool:
    """Checks if the Magpie TTS server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, MAGPIE_API_BASE_URL)
    try:
        response = requests.get(f"{normalized_base_url}/health", timeout=4)
        return response.status_code < 400
    except requests.exceptions.RequestException:
        return False


def get_magpie_models(base_url: str = MAGPIE_API_BASE_URL) -> list[str]:
    """Fetches available Magpie TTS models from server."""
    normalized_base_url = _normalize_base_url(base_url, MAGPIE_API_BASE_URL)
    try:
        import json

        response = requests.get(f"{normalized_base_url}/v1/models", timeout=8)
        response.raise_for_status()
        payload = response.json()
        discovered = [
            str(m["id"]) for m in payload.get("data", []) if isinstance(m, dict)
        ]
        if discovered:
            return discovered
    except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError):
        pass
    from ..constants import MAGPIE_TTS_MODELS

    return list(MAGPIE_TTS_MODELS)


def get_magpie_voices(base_url: str = MAGPIE_API_BASE_URL) -> list[str]:
    """Returns the predefined Magpie TTS voice catalog."""
    from ..constants import magpie_voice_catalog

    return magpie_voice_catalog()


def _request_magpie_audio(
    text: str, tts_settings: dict, magpie_base_url: str
) -> requests.Response:
    """Sends a TTS request to the Magpie TTS server."""

    voice = (
        str(tts_settings.get("speaker") or "").strip()
        or "Magpie-Multilingual.EN-US.Aria"
    )
    normalized_base_url = _normalize_base_url(magpie_base_url, MAGPIE_API_BASE_URL)

    payload = {
        "model": str(tts_settings.get("xtts_model") or "").strip() or "magpie-tts",
        "input": text,
        "voice": voice,
        "language": str(tts_settings.get("language") or "").strip() or None,
        "speed": float(tts_settings.get("speed") or 1.0),
        "use_cfg": True,
        "apply_text_normalization": False,
        "response_format": "wav",
    }

    last_response = None
    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )
        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No Magpie speech endpoint could be resolved for '{normalized_base_url}'."
    )


# XTTS Functions
def get_xtts_speakers(base_url: str = XTTS_API_BASE_URL) -> list[str]:
    """Fetches discoverable XTTS voice identifiers from server."""
    normalized_base_url = _normalize_base_url(base_url, XTTS_API_BASE_URL)
    # Preferred path: voice catalog endpoints (/v1/audio/voices, /v1/voices).
    discovered_voice_ids: list[str] = []
    for voices_url in _openai_voice_catalog_urls(normalized_base_url):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_voice_ids = _extract_voices_from_openai_payload(response.json())
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug("Could not fetch voices from %s: %s", voices_url, e)
            continue

    discovered_file_ids: list[str] = []
    discoverable_purposes = set(XTTS_DISCOVERABLE_FILE_PURPOSES)

    # Legacy path: OpenAI-compatible files endpoint (/v1/files).
    for purpose in XTTS_DISCOVERABLE_FILE_PURPOSES:
        for files_url in _openai_files_urls(normalized_base_url):
            try:
                response = requests.get(
                    files_url,
                    headers=_openai_auth_headers(),
                    params={"purpose": purpose, "limit": 10000},
                    timeout=8,
                )
                if _should_try_next_openai_candidate(response.status_code):
                    continue

                response.raise_for_status()
                discovered_file_ids.extend(
                    _extract_file_ids_from_openai_payload(
                        response.json(),
                        allowed_purposes=discoverable_purposes,
                    )
                )
                break
            except (requests.exceptions.RequestException, ValueError) as e:
                logging.debug("Could not fetch files from %s: %s", files_url, e)
                continue

    return _dedupe_ordered(discovered_voice_ids + discovered_file_ids)


def get_xtts_models(base_url: str = XTTS_API_BASE_URL) -> list[str]:
    """Fetches available XTTS models from server."""
    normalized_base_url = _normalize_base_url(base_url, XTTS_API_BASE_URL)
    discovered_models: list[str] = []

    for models_url in _openai_models_urls(normalized_base_url):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_models = _extract_models_from_openai_payload(response.json())
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug("Could not fetch models from %s: %s", models_url, e)
            continue

    return _merge_catalog_with_discovered([XTTS_DEFAULT_MODEL], discovered_models)


def _normalize_upload_wav_paths(wav_file_path: str | list[str]) -> list[str]:
    if isinstance(wav_file_path, str):
        candidates = [wav_file_path]
    elif isinstance(wav_file_path, (list, tuple, set)):
        candidates = [str(candidate or "") for candidate in wav_file_path]
    else:
        raise ValueError("Voice upload expects a WAV path or a list of WAV paths.")

    normalized_paths: list[str] = []
    for candidate in candidates:
        normalized_candidate = str(candidate or "").strip()
        if not normalized_candidate:
            continue
        if not normalized_candidate.lower().endswith(".wav"):
            raise ValueError("Only .wav files are supported for speaker voices.")
        if not os.path.isfile(normalized_candidate):
            raise ValueError(f"WAV file not found: {normalized_candidate}")
        normalized_paths.append(normalized_candidate)

    if not normalized_paths:
        raise ValueError("No WAV files were provided for upload.")

    return normalized_paths


def _extract_uploaded_identifier(payload: object) -> str:
    if isinstance(payload, dict):
        return str(
            payload.get("id") or payload.get("voice_id") or payload.get("name") or ""
        ).strip()

    if isinstance(payload, list):
        for item in payload:
            uploaded_identifier = _extract_uploaded_identifier(item)
            if uploaded_identifier:
                return uploaded_identifier

    return ""


def _upload_speaker_voice_openai_compatible(
    wav_file_path: str | list[str],
    *,
    base_url: str,
    fallback_base_url: str,
    service_name: str,
    api_key: str,
    upload_purpose: str,
    prompt_text: str | None = None,
    mode: str | None = None,
    voice_id: str | None = None,
) -> str:
    wav_file_paths = _normalize_upload_wav_paths(wav_file_path)
    normalized_base_url = _normalize_base_url(base_url, fallback_base_url)
    upload_voice_urls = _openai_audio_voices_urls(normalized_base_url)
    upload_file_urls = _openai_files_urls(normalized_base_url)
    normalized_prompt_text = str(prompt_text or "").strip()
    normalized_mode = str(mode or "").strip().lower()
    first_voice_filename = os.path.basename(wav_file_paths[0])
    fallback_voice_name = os.path.splitext(first_voice_filename)[0]
    resolved_voice_id = (
        str(voice_id or fallback_voice_name).strip() or fallback_voice_name
    )

    try:
        # Preferred path: ecosystem voice endpoint (/v1/audio/voices).
        last_voice_response = None
        for upload_voice_url in upload_voice_urls:
            # Keep both multipart field names for compatibility across API wrappers.
            with ExitStack() as stack:
                files_payload = []
                for sample_path in wav_file_paths:
                    sample_filename = os.path.basename(sample_path)
                    sample_handle = stack.enter_context(open(sample_path, "rb"))
                    files_payload.append(
                        (
                            "files",
                            (
                                sample_filename,
                                sample_handle,
                                "audio/wav",
                            ),
                        )
                    )

                audio_sample_handle = stack.enter_context(open(wav_file_paths[0], "rb"))
                files_payload.append(
                    (
                        "audio_sample",
                        (
                            first_voice_filename,
                            audio_sample_handle,
                            "audio/wav",
                        ),
                    )
                )

                form_data = {
                    "voice_id": resolved_voice_id,
                    "name": resolved_voice_id,
                    "purpose": upload_purpose,
                }
                if normalized_prompt_text and len(wav_file_paths) == 1:
                    form_data["prompt_text"] = normalized_prompt_text
                if normalized_mode:
                    form_data["mode"] = normalized_mode

                response = requests.post(
                    upload_voice_url,
                    headers=_openai_auth_headers(api_key),
                    files=files_payload,
                    data=form_data,
                    timeout=120,
                )

            if _should_try_next_openai_candidate(response.status_code):
                last_voice_response = response
                continue

            if response.status_code >= 400:
                raise RuntimeError(
                    f"{service_name} voice upload failed ({response.status_code}): {response.text}"
                )

            try:
                payload = response.json()
            except ValueError:
                payload = {}

            uploaded_voice_id = _extract_uploaded_identifier(payload)
            if not uploaded_voice_id:
                raise RuntimeError(
                    f"{service_name} voice upload succeeded but did not return a voice ID."
                )

            logging.info(
                "Uploaded %s voice '%s' via /audio/voices endpoint (%d sample(s))",
                service_name,
                uploaded_voice_id,
                len(wav_file_paths),
            )
            return uploaded_voice_id

        # Legacy path: OpenAI-compatible files endpoint (/v1/files).
        last_response = None
        for upload_url in upload_file_urls:
            uploaded_file_id = ""
            for sample_path in wav_file_paths:
                sample_filename = os.path.basename(sample_path)
                with open(sample_path, "rb") as wav_file:
                    files = {
                        "file": (
                            sample_filename,
                            wav_file,
                            "audio/wav",
                        )
                    }
                    form_data = {
                        "voice_id": resolved_voice_id,
                        "name": resolved_voice_id,
                        "purpose": upload_purpose,
                    }
                    if normalized_prompt_text and len(wav_file_paths) == 1:
                        form_data["prompt_text"] = normalized_prompt_text
                    if normalized_mode:
                        form_data["mode"] = normalized_mode

                    response = requests.post(
                        upload_url,
                        headers=_openai_auth_headers(api_key),
                        files=files,
                        data=form_data,
                        timeout=120,
                    )

                if _should_try_next_openai_candidate(response.status_code):
                    last_response = response
                    uploaded_file_id = ""
                    break

                if response.status_code >= 400:
                    raise RuntimeError(
                        f"{service_name} voice upload failed ({response.status_code}): {response.text}"
                    )

                try:
                    payload = response.json()
                except ValueError:
                    payload = {}

                uploaded_file_id = _extract_uploaded_identifier(payload)
                if not uploaded_file_id:
                    raise RuntimeError(
                        f"{service_name} file upload succeeded but did not return a file ID."
                    )

            if uploaded_file_id:
                logging.info(
                    "Uploaded %s voice file '%s' via OpenAI-compatible endpoint (%d sample(s))",
                    service_name,
                    uploaded_file_id,
                    len(wav_file_paths),
                )
                return uploaded_file_id

        if last_voice_response is not None and _should_try_next_openai_candidate(
            last_voice_response.status_code
        ):
            logging.debug(
                "%s server at %s does not expose /audio/voices upload; tried /files fallback.",
                service_name,
                normalized_base_url,
            )

        if last_response is not None and _should_try_next_openai_candidate(
            last_response.status_code
        ):
            raise RuntimeError(
                f"{service_name} server at {normalized_base_url} does not support voice upload endpoints (/audio/voices or /files)."
            )

        raise RuntimeError(f"Could not upload speaker voice to {service_name} server.")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"Failed uploading voice to {service_name} server {normalized_base_url}: {e}"
        ) from e
    except OSError as e:
        raise RuntimeError(f"Could not read WAV file for upload: {e}") from e


def _remote_voice_exists(
    voice_id: str,
    *,
    base_url: str,
    api_key: str = "",
) -> bool | None:
    """Verify a remote voice after an idempotent or unsupported DELETE."""

    expected = str(voice_id or "").strip().casefold()
    for voices_url in _openai_voice_catalog_urls(base_url):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
        except requests.exceptions.RequestException:
            continue
        if _should_try_next_openai_candidate(response.status_code):
            continue
        if response.status_code >= 400:
            continue
        try:
            discovered = _extract_voices_from_openai_payload(response.json())
        except ValueError:
            continue
        return any(str(item).strip().casefold() == expected for item in discovered)
    return None


def _delete_speaker_voice_openai_compatible(
    voice_id: str,
    *,
    base_url: str,
    fallback_base_url: str,
    service_name: str,
    api_key: str = "",
) -> bool:
    """Delete an uploaded voice without confusing an absent route with an absent voice."""

    normalized_voice_id = str(voice_id or "").strip()
    if not normalized_voice_id:
        raise ValueError("Provider voice deletion requires a voice ID.")
    if (
        len(normalized_voice_id) > 255
        or normalized_voice_id in {".", ".."}
        or any(character in normalized_voice_id for character in ("/", "\\", "\x00"))
        or any(ord(character) < 32 for character in normalized_voice_id)
    ):
        raise ValueError("Provider voice ID is not safe to delete.")
    normalized_base_url = _normalize_base_url(base_url, fallback_base_url)
    encoded_voice_id = quote(normalized_voice_id, safe="")
    unsupported_responses = 0
    missing_responses = 0

    try:
        for collection_url in _openai_voice_catalog_urls(normalized_base_url):
            response = requests.delete(
                f"{collection_url.rstrip('/')}/{encoded_voice_id}",
                headers=_openai_auth_headers(api_key),
                timeout=30,
            )
            if 200 <= response.status_code < 300:
                return True
            if response.status_code in {404, 410}:
                missing_responses += 1
                continue
            if response.status_code in {405, 501}:
                unsupported_responses += 1
                continue
            raise RuntimeError(
                f"{service_name} voice deletion failed ({response.status_code}): "
                f"{response.text}"
            )
    except requests.exceptions.RequestException as error:
        raise RuntimeError(
            f"Failed deleting voice from {service_name} server "
            f"{normalized_base_url}: {error}"
        ) from error

    remote_exists = _remote_voice_exists(
        normalized_voice_id,
        base_url=normalized_base_url,
        api_key=api_key,
    )
    if remote_exists is False:
        # A prior deletion or manual cleanup is success from the caller's point
        # of view. The follow-up catalogue check keeps this from silently
        # accepting a 404 caused by an unimplemented route.
        return False
    if remote_exists is True:
        raise RuntimeError(
            f"{service_name} still lists voice '{normalized_voice_id}' and does "
            "not appear to support provider-side deletion."
        )
    detail = (
        "did not expose a supported voice deletion endpoint"
        if unsupported_responses
        else "returned a missing response that could not be verified"
    )
    raise RuntimeError(
        f"{service_name} {detail} for voice '{normalized_voice_id}' "
        f"({missing_responses} missing response(s))."
    )


def upload_xtts_speaker_voice(
    wav_file_path: str | list[str],
    base_url: str = XTTS_API_BASE_URL,
    *,
    voice_id: str | None = None,
) -> str:
    """Uploads voice to XTTS and returns uploaded voice identifier."""
    return _upload_speaker_voice_openai_compatible(
        wav_file_path,
        base_url=base_url,
        fallback_base_url=XTTS_API_BASE_URL,
        service_name="XTTS",
        api_key=XTTS_OPENAI_PLACEHOLDER_API_KEY,
        upload_purpose=XTTS_UPLOAD_FILE_PURPOSE,
        voice_id=voice_id,
    )


def upload_voxcpm_speaker_voice(
    wav_file_path: str | list[str],
    base_url: str = VOXCPM_API_BASE_URL,
    *,
    prompt_text: str | None = None,
    mode: str = "reference",
    voice_id: str | None = None,
    api_key: str = "",
) -> str:
    """Uploads voice to VoxCPM and returns uploaded voice identifier."""
    return _upload_speaker_voice_openai_compatible(
        wav_file_path,
        base_url=base_url,
        fallback_base_url=VOXCPM_API_BASE_URL,
        service_name="VoxCPM",
        api_key=str(api_key or "").strip() or _resolve_voxcpm_api_key(),
        upload_purpose=VOXCPM_UPLOAD_FILE_PURPOSE,
        prompt_text=prompt_text,
        mode=mode,
        voice_id=voice_id,
    )


def upload_fishs2_speaker_voice(
    wav_file_path: str | list[str],
    base_url: str = FISHS2_API_BASE_URL,
    *,
    prompt_text: str | None = None,
    voice_id: str | None = None,
    api_key: str = "",
) -> str:
    """Uploads voice to FishS2 and returns uploaded voice identifier."""
    return _upload_speaker_voice_openai_compatible(
        wav_file_path,
        base_url=base_url,
        fallback_base_url=FISHS2_API_BASE_URL,
        service_name="FishS2",
        api_key=str(api_key or "").strip() or _resolve_fishs2_api_key(),
        upload_purpose=FISHS2_UPLOAD_FILE_PURPOSE,
        prompt_text=prompt_text,
        voice_id=voice_id,
    )


def upload_chatterbox_speaker_voice(
    wav_file_path: str | list[str],
    base_url: str = CHATTERBOX_API_BASE_URL,
    *,
    prompt_text: str | None = None,
    voice_id: str | None = None,
) -> str:
    """Uploads voice to Chatterbox and returns uploaded voice identifier."""
    return _upload_speaker_voice_openai_compatible(
        wav_file_path,
        base_url=base_url,
        fallback_base_url=CHATTERBOX_API_BASE_URL,
        service_name="Chatterbox",
        api_key=XTTS_OPENAI_PLACEHOLDER_API_KEY,
        upload_purpose="user_data",
        prompt_text=prompt_text,
        voice_id=voice_id,
    )


def upload_kobold_qwen_speaker_voice(
    wav_file_path: str | list[str],
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    *,
    voice_id: str | None = None,
    api_key: str = "",
) -> str:
    """Uploads voice to Qwen3 TTS and returns uploaded voice identifier."""
    return _upload_speaker_voice_openai_compatible(
        wav_file_path,
        base_url=base_url,
        fallback_base_url=KOBOLD_QWEN_API_BASE_URL,
        service_name="Qwen3 TTS",
        api_key=str(api_key or "").strip() or _resolve_kobold_qwen_api_key(),
        upload_purpose="user_data",
        voice_id=voice_id,
    )


def upload_speaker_voice(
    wav_file_path: str | list[str],
    base_url: str = XTTS_API_BASE_URL,
    *,
    service: str = "XTTS",
    prompt_text: str | None = None,
    mode: str | None = None,
    voice_id: str | None = None,
    api_key: str = "",
) -> str:
    """Uploads a speaker voice file and returns uploaded voice identifier."""
    normalized_service = str(service or "XTTS").strip().lower()
    if normalized_service in {"voxcpm", "voxcpm2"}:
        return upload_voxcpm_speaker_voice(
            wav_file_path,
            base_url=base_url,
            prompt_text=prompt_text,
            mode=mode or "reference",
            voice_id=voice_id,
            api_key=api_key,
        )

    if normalized_service in {"fishs2", "fish-s2", "fishs2-cpp", "fishs2cpp"}:
        return upload_fishs2_speaker_voice(
            wav_file_path,
            base_url=base_url,
            prompt_text=prompt_text,
            voice_id=voice_id,
            api_key=api_key,
        )

    if normalized_service in {"chatterbox", "chatterbox-turbo"}:
        return upload_chatterbox_speaker_voice(
            wav_file_path,
            base_url=base_url,
            prompt_text=prompt_text,
            voice_id=voice_id,
        )

    if normalized_service in {
        "qwen3 tts",
        "qwen3-tts",
        "qwen3",
        "qwen",
        "kobold-qwen",
        "kobold_qwen",
    }:
        return upload_kobold_qwen_speaker_voice(
            wav_file_path,
            base_url=base_url,
            voice_id=voice_id,
            api_key=api_key,
        )

    return upload_xtts_speaker_voice(
        wav_file_path,
        base_url=base_url,
        voice_id=voice_id,
    )


def delete_speaker_voice(
    voice_id: str,
    base_url: str = XTTS_API_BASE_URL,
    *,
    service: str = "XTTS",
    api_key: str = "",
) -> bool:
    """Delete an uploaded voice from a first-party OpenAI-compatible wrapper."""

    normalized_service = str(service or "XTTS").strip().lower()
    if normalized_service in {"voxcpm", "voxcpm2"}:
        fallback_base_url = VOXCPM_API_BASE_URL
        service_name = "VoxCPM"
        resolved_key = str(api_key or "").strip() or _resolve_voxcpm_api_key()
    elif normalized_service in {"fishs2", "fish-s2", "fishs2-cpp", "fishs2cpp"}:
        fallback_base_url = FISHS2_API_BASE_URL
        service_name = "FishS2"
        resolved_key = str(api_key or "").strip() or _resolve_fishs2_api_key()
    elif normalized_service in {"chatterbox", "chatterbox-turbo"}:
        fallback_base_url = CHATTERBOX_API_BASE_URL
        service_name = "Chatterbox"
        resolved_key = str(api_key or "").strip()
    elif normalized_service in {
        "qwen3 tts",
        "qwen3-tts",
        "qwen3",
        "qwen",
        "kobold-qwen",
        "kobold_qwen",
    }:
        fallback_base_url = KOBOLD_QWEN_API_BASE_URL
        service_name = "Qwen3 TTS"
        resolved_key = str(api_key or "").strip() or _resolve_kobold_qwen_api_key()
    else:
        fallback_base_url = XTTS_API_BASE_URL
        service_name = "XTTS"
        resolved_key = str(api_key or "").strip() or XTTS_OPENAI_PLACEHOLDER_API_KEY

    return _delete_speaker_voice_openai_compatible(
        voice_id,
        base_url=base_url,
        fallback_base_url=fallback_base_url,
        service_name=service_name,
        api_key=resolved_key,
    )


# Silero Functions
def set_silero_language(
    language_code: str, base_url: str = SILERO_API_BASE_URL
) -> bool:
    """Compatibility no-op; the new service receives language per request."""
    del language_code
    return check_silero_connection(base_url)


def normalize_silero_language_code(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "hy": "hye",
        "ka": "kat",
        "ky": "kir",
        "tt": "tat",
        "uk": "ukr",
        "ua": "ukr",
        "uz": "uzb",
        "ba": "bak",
        "be": "bel",
        "cv": "chv",
        "kk": "kaz",
        "tg": "tgk",
        "sah": "sah",
        "xal": "xal",
        "english (v3)": "en",
        "english indic (v3)": "en-in",
        "german (v3)": "de",
        "spanish (v3)": "es",
        "french (v3)": "fr",
        "indic (v3)": "indic",
        "russian (v3.1)": "ru",
        "tatar (v3)": "tat",
        "ukrainian (v3)": "ukr",
        "uzbek (v3)": "uzb",
        "kalmyk (v3)": "xal",
    }
    if normalized in aliases:
        return aliases[normalized]
    for item in SILERO_LANGUAGES:
        if normalized == str(item.get("name") or "").strip().lower():
            return str(item.get("code") or "").strip()
    return normalized


def get_silero_model_catalog(base_url: str = SILERO_API_BASE_URL) -> list[dict]:
    """Return model metadata, including installation and licence state."""
    try:
        response = requests.get(
            f"{_normalize_base_url(base_url, SILERO_API_BASE_URL)}/v1/models",
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return [
            dict(item) for item in data if isinstance(item, dict) and item.get("id")
        ]
    except (requests.exceptions.RequestException, ValueError) as exc:
        logging.error("Failed to fetch Silero models: %s", exc)
        return []


def get_silero_models(
    base_url: str = SILERO_API_BASE_URL,
    *,
    installed_only: bool = True,
) -> list[str]:
    catalog = get_silero_model_catalog(base_url)
    models = []
    for item in catalog:
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        if installed_only and not status.get("installed"):
            continue
        models.append(str(item["id"]))
    if models:
        return _dedupe_ordered(models)
    return [] if catalog and installed_only else list(SILERO_TTS_MODELS)


def get_silero_voice_catalog(
    base_url: str = SILERO_API_BASE_URL,
    *,
    model: str = "",
    language: str = "",
    include_unavailable: bool = False,
) -> list[dict]:
    params = {
        "model": str(model or "").strip(),
        "language": normalize_silero_language_code(language),
        "include_unavailable": str(bool(include_unavailable)).lower(),
    }
    params = {key: value for key, value in params.items() if value != ""}
    try:
        response = requests.get(
            f"{_normalize_base_url(base_url, SILERO_API_BASE_URL)}/v1/audio/voices",
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return [
            dict(item) for item in data if isinstance(item, dict) and item.get("id")
        ]
    except (requests.exceptions.RequestException, ValueError) as exc:
        logging.error("Failed to fetch Silero voices: %s", exc)
        return []


def get_silero_speakers(
    base_url: str = SILERO_API_BASE_URL,
    model: str = "",
    language: str = "",
) -> list[str]:
    """Fetches the list of available speakers from the Silero server."""
    return _dedupe_ordered(
        str(item["id"])
        for item in get_silero_voice_catalog(
            base_url,
            model=model,
            language=language,
            include_unavailable=False,
        )
    )


def _build_xtts_openai_payload(text: str, tts_settings: dict) -> dict:
    model = (
        str(tts_settings.get("xtts_model") or XTTS_DEFAULT_MODEL).strip()
        or XTTS_DEFAULT_MODEL
    )
    speaker = str(tts_settings.get("speaker") or "").strip()
    language = str(tts_settings.get("language") or "en").strip() or "en"
    instructions = _build_xtts_instructions_payload(
        tts_settings,
        str(tts_settings.get("openai_audio_instructions") or "").strip(),
    )

    return {
        "model": model,
        "input": text,
        "voice": speaker or "default",
        "language": language,
        "response_format": "wav",
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
        "instructions": instructions,
    }


def _request_xtts_audio(
    text: str, tts_settings: dict, xtts_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(xtts_base_url, XTTS_API_BASE_URL)
    payload = _build_xtts_openai_payload(text, tts_settings)
    last_response = None

    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )
        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No XTTS speech endpoint could be resolved for '{normalized_base_url}'."
    )


def _build_voxcpm_payload(text: str, tts_settings: dict) -> dict:
    model = _normalize_voxcpm_model(
        tts_settings.get("xtts_model", ""),
        fallback=VOXCPM_DEFAULT_MODEL,
    )
    voice = str(tts_settings.get("speaker") or "").strip() or VOXCPM_DEFAULT_VOICE

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "wav",
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
        "voxcpm": _build_voxcpm_options(tts_settings),
    }

    instructions = str(tts_settings.get("openai_audio_instructions") or "").strip()
    if instructions:
        payload["instructions"] = instructions

    return payload


def _is_voxcpm_prompt_pairing_error(response: requests.Response) -> bool:
    if response.status_code != 422:
        return False

    error_message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            error_message = str(error_payload.get("message") or "").strip()

    if not error_message:
        error_message = str(response.text or "").strip()

    normalized = error_message.lower()
    return (
        "prompt_wav_path and prompt_text must both be provided or both be none"
        in normalized
    )


def _request_voxcpm_audio(
    text: str, tts_settings: dict, voxcpm_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(voxcpm_base_url, VOXCPM_API_BASE_URL)
    api_key = _resolve_voxcpm_api_key(tts_settings)
    payload = _build_voxcpm_payload(text, tts_settings)
    last_response = None

    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(api_key),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )

        if (
            _is_voxcpm_prompt_pairing_error(response)
            and str(payload.get("mode") or "").strip().lower() != "hifi"
        ):
            hifi_payload = dict(payload)
            hifi_payload["mode"] = "hifi"
            logging.warning(
                "Retrying VoxCPM request in hifi mode after prompt pairing error for voice '%s'.",
                hifi_payload.get("voice", ""),
            )
            response = requests.post(
                speech_url,
                headers=_openai_auth_headers(api_key),
                json=hifi_payload,
                timeout=TTS_GENERATION_TIMEOUT_SECONDS,
            )

        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No VoxCPM speech endpoint could be resolved for '{normalized_base_url}'."
    )


def _build_fishs2_payload(text: str, tts_settings: dict) -> dict:
    model = _normalize_fishs2_model(
        tts_settings.get("xtts_model", ""),
        fallback=FISHS2_DEFAULT_MODEL,
    )
    voice = str(tts_settings.get("speaker") or "").strip() or FISHS2_DEFAULT_VOICE
    fishs2_options = _build_fishs2_options(tts_settings)
    prosody = (
        fishs2_options.get("prosody")
        if isinstance(fishs2_options.get("prosody"), dict)
        else {}
    )

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "wav",
        "speed": prosody.get("speed", 1.0),
    }
    payload.update(fishs2_options)

    instructions = str(tts_settings.get("openai_audio_instructions") or "").strip()
    if instructions:
        payload["instructions"] = instructions

    return payload


def _request_fishs2_audio(
    text: str, tts_settings: dict, fishs2_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(fishs2_base_url, FISHS2_API_BASE_URL)
    api_key = _resolve_fishs2_api_key(tts_settings)
    payload = _build_fishs2_payload(text, tts_settings)
    last_response = None

    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(api_key),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )

        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No FishS2 speech endpoint could be resolved for '{normalized_base_url}'."
    )


def _build_voxtral_payload(text: str, tts_settings: dict) -> dict:
    model = _normalize_voxtral_model(
        tts_settings.get("xtts_model", ""), fallback=VOXTRAL_DEFAULT_MODEL
    )
    voice = str(tts_settings.get("speaker") or "").strip() or VOXTRAL_DEFAULT_VOICE
    instructions = _build_voxtral_instructions_payload(
        tts_settings,
        str(tts_settings.get("openai_audio_instructions") or "").strip(),
    )

    return {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "wav",
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
        "instructions": instructions,
    }


def _build_kokoro_payload(text: str, tts_settings: dict) -> dict:
    model = _strip_provider_prefix(str(tts_settings.get("xtts_model") or "").strip())
    if not model:
        model = KOKORO_DEFAULT_MODEL

    voice = str(tts_settings.get("speaker") or "").strip() or KOKORO_DEFAULT_VOICE

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "wav",
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
    }

    return payload


def _request_voxtral_audio(
    text: str, tts_settings: dict, voxtral_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(voxtral_base_url, VOXTRAL_API_BASE_URL)
    api_key = _resolve_voxtral_api_key(tts_settings)
    payload = _build_voxtral_payload(text, tts_settings)

    last_response = None
    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(api_key),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )
        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No Voxtral speech endpoint could be resolved for '{normalized_base_url}'."
    )


def _request_kokoro_audio(
    text: str, tts_settings: dict, kokoro_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(kokoro_base_url, KOKORO_API_BASE_URL)
    api_key = _resolve_kokoro_api_key(tts_settings)
    payload = _build_kokoro_payload(text, tts_settings)

    last_response = None
    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(api_key),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )
        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No Kokoro speech endpoint could be resolved for '{normalized_base_url}'."
    )


def _build_openai_compatible_audio_payload(
    text: str,
    tts_settings: dict,
    endpoint: dict[str, str],
) -> dict:
    provider = _infer_audio_provider(
        name=endpoint.get("name", ""),
        base_url=endpoint.get("base_url", ""),
        raw_provider=endpoint.get("provider", ""),
    )

    model_name = str(tts_settings.get("xtts_model") or "").strip()
    if not model_name:
        model_name = str(
            endpoint.get("default_model", "")
        ).strip() or _provider_default_model(provider)
    model_name = _normalize_model_for_provider(model_name, provider)

    voice_name = str(tts_settings.get("speaker") or "").strip()
    if not voice_name:
        voice_name = str(
            endpoint.get("default_voice", "")
        ).strip() or _provider_default_voice(provider)
    voice_name = _normalize_voice_for_provider(voice_name, provider)

    payload = {
        "model": model_name,
        "input": text,
        "voice": voice_name,
        "response_format": "wav",
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
    }

    legacy_instructions = str(
        tts_settings.get("openai_audio_instructions") or ""
    ).strip()
    generation_prompt = str(tts_settings.get("generation_prompt") or "").strip()
    if _is_xtts_target(model_name, endpoint):
        payload["instructions"] = _build_xtts_instructions_payload(
            tts_settings,
            legacy_instructions,
        )
    elif provider == OPENAI_PROVIDER and model_name in OPENAI_GENERATION_PROMPT_MODELS:
        instructions = generation_prompt or legacy_instructions
        if instructions:
            payload["instructions"] = instructions
    elif provider == GEMINI_PROVIDER:
        instructions = generation_prompt or legacy_instructions
        if instructions:
            payload["input"] = _build_guided_speech_prompt(text, instructions)

    return payload


_AUDIO_CPP_QWEN_LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "it": "Italian",
}


def _audio_cpp_model_metadata(model: str, endpoint: dict) -> dict[str, object]:
    normalized = str(model or "").strip().casefold()
    configured_modes = endpoint.get("model_voice_modes")
    mode = ""
    if isinstance(configured_modes, dict):
        mode = next(
            (
                str(value or "").strip().lower()
                for key, value in configured_modes.items()
                if str(key or "").strip().casefold() == normalized
            ),
            "",
        )
    live_item: dict[str, object] = {}
    configured_catalog = endpoint.get("model_catalog")
    if isinstance(configured_catalog, list):
        live_item = next(
            (
                dict(item)
                for item in configured_catalog
                if isinstance(item, dict)
                and str(item.get("id") or "").strip().casefold() == normalized
            ),
            {},
        )
    for item in AUDIO_CPP_MODEL_CATALOG:
        if str(item.get("id") or "").casefold() == normalized:
            result = {**item}
            for key in ("family", "voice_mode", "experimental"):
                if live_item.get(key) is not None:
                    result[key] = live_item[key]
            if live_item.get("mode") and not result.get("voice_mode"):
                result["voice_mode"] = live_item["mode"]
            if mode:
                result["voice_mode"] = mode
            return result
    if live_item:
        result = {"id": model, **live_item}
        if mode:
            result["voice_mode"] = mode
        elif result.get("mode") and not result.get("voice_mode"):
            result["voice_mode"] = result["mode"]
        return result
    if mode:
        return {"id": model, "voice_mode": mode}

    if "customvoice" in normalized or "magpie" in normalized:
        inferred_mode = "prebuilt"
    elif "pocket" in normalized:
        inferred_mode = "hybrid"
    else:
        inferred_mode = "cloning"
    if "qwen" in normalized:
        family = "qwen3_tts"
    elif "fish" in normalized:
        family = "fish_audio_s2"
    elif "voxcpm" in normalized:
        family = "voxcpm2"
    elif "chatterbox" in normalized:
        family = "chatterbox"
    elif "omni" in normalized:
        family = "omnivoice"
    elif "pocket" in normalized:
        family = "pocket_tts"
    elif "firered" in normalized:
        family = "fireredtts3"
    elif "magpie" in normalized:
        family = "magpie_tts"
    else:
        family = ""
    return {"id": model, "family": family, "voice_mode": inferred_mode}


def _audio_cpp_language(model: str, language: object) -> str:
    normalized = str(language or "").strip().lower().replace("_", "-")
    if not normalized or normalized in {"auto", "unknown", "und"}:
        return ""
    iso = normalized.split("-", 1)[0]
    metadata = _audio_cpp_model_metadata(model, {})
    if metadata.get("family") == "qwen3_tts":
        return _AUDIO_CPP_QWEN_LANGUAGE_NAMES.get(iso, "")
    return iso


def _build_audio_cpp_audio_payload(
    text: str,
    tts_settings: dict,
    endpoint: dict,
) -> dict:
    """Build audio.cpp's direct HTTP speech request."""

    model = str(
        tts_settings.get("xtts_model")
        or tts_settings.get("model")
        or endpoint.get("default_model")
        or ""
    ).strip()
    if not model:
        raise ValueError(
            "Select one of the model IDs configured in the audio.cpp server."
        )

    payload: dict[str, Any] = {
        "model": model,
        "input": text,
        "response_format": "wav",
    }
    voice = str(tts_settings.get("speaker") or tts_settings.get("voice") or "").strip()
    if voice:
        payload["voice"] = voice

    language = _audio_cpp_language(
        model,
        tts_settings.get("language") or tts_settings.get("target_language") or "",
    )
    if language:
        payload["language"] = language

    instructions = str(
        tts_settings.get("generation_prompt")
        or tts_settings.get("openai_audio_instructions")
        or ""
    ).strip()
    if instructions:
        payload["instructions"] = instructions

    metadata = _audio_cpp_model_metadata(model, endpoint)
    voice_mode = str(metadata.get("voice_mode") or "cloning").lower()
    is_prebuilt = voice_mode == "prebuilt"
    reference_text = str(tts_settings.get("audio_cpp_reference_text") or "").strip()
    if reference_text and not is_prebuilt:
        payload["reference_text"] = reference_text
    voice_ref = tts_settings.get("audio_cpp_voice_ref")
    if not is_prebuilt and isinstance(voice_ref, dict) and voice_ref:
        payload["voice_ref"] = dict(voice_ref)

    for key in (
        "speed",
        "seed",
        "temperature",
        "top_k",
        "top_p",
        "max_tokens",
        "max_steps",
        "repetition_penalty",
        "guidance_scale",
        "num_inference_steps",
    ):
        value = tts_settings.get(f"audio_cpp_{key}")
        if value in (None, "") and key == "speed":
            value = tts_settings.get("speed")
        if value not in (None, ""):
            payload[key] = (
                str(value)
                if key == "seed"
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 2**53
                else value
            )

    raw_options = tts_settings.get("audio_cpp_options")
    if raw_options is None:
        raw_options = tts_settings.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    family = str(metadata.get("family") or "").lower()
    linked_reference = isinstance(voice_ref, dict)
    if linked_reference and family == "omnivoice" and not reference_text:
        raise ValueError(
            "OmniVoice linked voice references require a reviewed transcript."
        )
    if linked_reference and family == "qwen3_tts":
        options["x_vector_only_mode"] = not bool(reference_text)
    if options:
        payload["options"] = options
    return payload


def _build_guided_speech_prompt(text: str, generation_prompt: str) -> str:
    """Combine Gemini performance direction and transcript without making it ambiguous."""
    return (
        "Perform the transcript below as speech. Follow the speaking directions, "
        "but do not read or mention the directions or labels aloud.\n\n"
        f"Speaking directions:\n{generation_prompt.strip()}\n\n"
        f"Transcript:\n{text}"
    )


def _litellm_response_to_requests_response(litellm_response) -> requests.Response:
    raw_response = getattr(litellm_response, "response", None)

    response = requests.Response()
    response.status_code = int(getattr(raw_response, "status_code", 200) or 200)
    response._content = bytes(getattr(litellm_response, "content", b"") or b"")
    response.headers = requests.structures.CaseInsensitiveDict(
        dict(getattr(raw_response, "headers", {}) or {})
    )

    response_url = ""
    if raw_response is not None:
        try:
            response_url = str(raw_response.url)
        except Exception:
            response_url = ""

    if response_url:
        response.url = response_url

    prepared_request = requests.PreparedRequest()
    prepared_request.prepare(
        method="POST",
        url=response.url or "https://litellm.local/audio/speech",
    )
    response.request = prepared_request

    return response


def _request_litellm_audio(
    payload: dict, endpoint: dict[str, str]
) -> requests.Response:
    litellm_speech = _get_litellm_speech_client()
    if litellm_speech is None:
        detail = ""
        if _litellm_speech_import_error is not None:
            detail = (
                f" ({type(_litellm_speech_import_error).__name__}: "
                f"{_litellm_speech_import_error})"
            )
        raise RuntimeError(
            "LiteLLM speech support could not be loaded"
            f"{detail}. Verify that the 'litellm' package and its dependencies are installed."
        )

    provider = _infer_audio_provider(
        name=endpoint.get("name", ""),
        base_url=endpoint.get("base_url", ""),
        raw_provider=endpoint.get("provider", ""),
    )
    if provider not in SUPPORTED_AUDIO_PROVIDERS:
        raise RuntimeError(
            f"Provider '{provider}' is not supported for LiteLLM speech routing."
        )

    model_name = str(payload.get("model") or "").strip() or _provider_default_model(
        provider
    )
    voice_name = str(payload.get("voice") or "").strip() or _provider_default_voice(
        provider
    )
    api_base = endpoint.get("base_url") or None
    if provider == GEMINI_PROVIDER:
        api_base = None

    request_kwargs = {
        "model": _to_litellm_model_name(provider, model_name),
        "input": str(payload.get("input") or ""),
        "voice": _normalize_voice_for_provider(voice_name, provider),
        "api_key": _resolve_openai_audio_api_key(endpoint),
        "api_base": api_base,
    }

    speed = payload.get("speed")
    if speed is not None:
        request_kwargs["speed"] = speed

    instructions = str(payload.get("instructions") or "").strip()
    if instructions:
        request_kwargs["instructions"] = instructions

    if provider == OPENAI_PROVIDER:
        request_kwargs["response_format"] = str(payload.get("response_format") or "wav")

    logging.info(
        "Generating OpenAI-compatible audio via LiteLLM provider=%s model=%s endpoint=%s",
        provider,
        request_kwargs["model"],
        endpoint.get("name", ""),
    )
    litellm_response = litellm_speech(**request_kwargs)
    return _litellm_response_to_requests_response(litellm_response)


def _request_openai_compatible_audio(
    text: str,
    tts_settings: dict,
    *,
    request_session: requests.Session | None = None,
) -> requests.Response:
    endpoint, error = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        raise RuntimeError(error)

    if _normalize_custom_adapter(endpoint.get("adapter")) == AUDIO_CPP_ADAPTER:
        payload = _build_audio_cpp_audio_payload(text, tts_settings, endpoint)
        speech_path = str(endpoint.get("speech_path") or "/v1/audio/speech")
        transport = request_session or requests
        return transport.post(
            _configured_endpoint_url(str(endpoint["base_url"]), speech_path),
            headers=_configured_endpoint_auth_headers(endpoint),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )

    if _normalize_custom_adapter(endpoint.get("adapter")) == AZURE_SPEECH_ADAPTER:
        return _request_azure_speech_audio(text, tts_settings, endpoint)

    if _normalize_custom_adapter(endpoint.get("adapter")) == ELEVENLABS_NATIVE_ADAPTER:
        return _request_elevenlabs_audio(text, tts_settings, endpoint=endpoint)

    if _normalize_custom_adapter(endpoint.get("adapter")) == GENERIC_JSON_ADAPTER:
        request_fields = endpoint.get("request_fields", {})
        if not isinstance(request_fields, dict):
            request_fields = {}
        request_defaults = endpoint.get("request_defaults", {})
        payload = dict(request_defaults) if isinstance(request_defaults, dict) else {}

        text_field = str(request_fields.get("text") or "").strip()
        if not text_field:
            raise RuntimeError(
                f"Endpoint '{endpoint['name']}' has no configured text request field."
            )
        payload[text_field] = text

        mapped_values = {
            "model": str(
                tts_settings.get("xtts_model") or endpoint.get("default_model") or ""
            ).strip(),
            "voice": str(
                tts_settings.get("speaker") or endpoint.get("default_voice") or ""
            ).strip(),
            "speed": tts_settings.get("speed"),
            "format": "wav",
        }
        for logical_name, value in mapped_values.items():
            field_name = str(request_fields.get(logical_name) or "").strip()
            if field_name and value not in (None, ""):
                payload[field_name] = value

        speech_path = str(endpoint.get("speech_path") or "").strip()
        if not speech_path:
            raise RuntimeError(
                f"Endpoint '{endpoint['name']}' has no configured speech route."
            )
        return requests.post(
            _configured_endpoint_url(str(endpoint["base_url"]), speech_path),
            headers=_configured_endpoint_auth_headers(endpoint),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )

    payload = _build_openai_compatible_audio_payload(text, tts_settings, endpoint)
    provider = _infer_audio_provider(
        name=endpoint.get("name", ""),
        base_url=endpoint.get("base_url", ""),
        raw_provider=endpoint.get("provider", ""),
    )

    uses_nonstandard_gemini_base = (
        provider == GEMINI_PROVIDER
        and _normalize_base_url(endpoint.get("base_url"), "") != GEMINI_AUDIO_BASE_URL
    )
    if (
        provider in SUPPORTED_AUDIO_PROVIDERS
        and not uses_nonstandard_gemini_base
        and not _coerce_bool(endpoint.get("direct_http"), False)
    ):
        try:
            return _request_litellm_audio(payload, endpoint)
        except Exception as e:
            logging.warning(
                "LiteLLM speech call failed for endpoint '%s', falling back to direct HTTP: %s",
                endpoint.get("name", ""),
                e,
            )

    last_response = None
    for speech_url in _configured_openai_urls(
        endpoint,
        "speech_path",
        _openai_audio_speech_urls(str(endpoint["base_url"])),
    ):
        response = requests.post(
            speech_url,
            headers=_configured_endpoint_auth_headers(endpoint),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )
        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No speech endpoint could be resolved for '{endpoint['name']}'."
    )


def _vertex_access_token(service: dict[str, object]) -> tuple[str, str]:
    """Create a short-lived Vertex token from the shared service-account JSON or ADC."""

    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
    except ImportError as error:  # pragma: no cover - dependency guard
        raise RuntimeError("Vertex AI TTS requires the google-auth package.") from error

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    credential_json = str(service.get("api_key") or "").strip()
    if credential_json:
        try:
            credential_info = json.loads(credential_json)
        except json.JSONDecodeError as error:
            raise ValueError(
                "Vertex credentials must be valid service-account JSON."
            ) from error
        credentials = service_account.Credentials.from_service_account_info(
            credential_info,
            scopes=scopes,
        )
        project_id = str(credential_info.get("project_id") or "").strip()
    else:
        credentials, detected_project = google.auth.default(scopes=scopes)
        project_id = str(detected_project or "").strip()

    configured_project = str(service.get("vertex_project") or "").strip()
    project_id = configured_project or project_id
    if not project_id:
        raise ValueError("Vertex AI TTS requires a Google Cloud project ID.")
    if not credentials.valid or not credentials.token:
        credentials.refresh(GoogleAuthRequest())
    token = str(credentials.token or "").strip()
    if not token:
        raise RuntimeError(
            "Google authentication did not return a Vertex access token."
        )
    return token, project_id


def _pcm_to_wav_bytes(pcm: bytes, *, sample_rate: int = 24000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def _request_vertex_ai_audio(text: str, tts_settings: dict) -> requests.Response:
    service = get_service_config(tts_settings, VERTEX_PROVIDER) or {}
    token, project_id = _vertex_access_token(service)
    location = str(
        service.get("vertex_location") or VERTEX_AUDIO_DEFAULT_LOCATION
    ).strip()
    model = str(
        tts_settings.get("xtts_model")
        or tts_settings.get("model")
        or service.get("default_model")
        or GEMINI_AUDIO_DEFAULT_MODEL
    ).strip()
    model = _normalize_model_for_provider(model, GEMINI_PROVIDER)
    voice = str(
        tts_settings.get("speaker")
        or tts_settings.get("voice")
        or service.get("default_voice")
        or GEMINI_AUDIO_DEFAULT_VOICE
    ).strip()
    endpoint = (
        "https://aiplatform.googleapis.com/v1beta1/projects/"
        f"{quote(project_id, safe='')}/locations/{quote(location, safe='')}/"
        f"publishers/google/models/{quote(model, safe='')}:generateContent"
    )
    generation_prompt = str(tts_settings.get("generation_prompt") or "").strip()
    prompt_text = (
        _build_guided_speech_prompt(text, generation_prompt)
        if generation_prompt
        else text
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice},
                }
            },
        },
    }
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=TTS_GENERATION_TIMEOUT_SECONDS,
    )
    if not response.ok:
        return response
    try:
        response_payload = response.json()
        part = response_payload["candidates"][0]["content"]["parts"][0]
        inline_data = part.get("inlineData") or part.get("inline_data")
        pcm = base64.b64decode(str(inline_data["data"]), validate=True)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError("Vertex AI returned no decodable audio payload.") from error

    audio_response = requests.Response()
    audio_response.status_code = 200
    audio_response._content = _pcm_to_wav_bytes(pcm)
    audio_response.headers["Content-Type"] = "audio/wav"
    audio_response.url = endpoint
    return audio_response


def _decode_audio_response(response: requests.Response) -> AudioSegment:
    content_type = (response.headers.get("Content-Type") or "").lower()
    if not response.content:
        raise RuntimeError(
            "The speech service returned an empty response instead of audio."
        )
    if "json" in content_type or content_type.startswith("text/"):
        try:
            payload = response.json()
        except ValueError:
            payload = response.text.strip()
        if isinstance(payload, dict):
            detail = (
                payload.get("detail")
                or payload.get("error")
                or payload.get("message")
                or payload
            )
        else:
            detail = payload
        raise RuntimeError(
            f"The speech service returned an error instead of audio: {detail}"
        )
    format_hint = "wav"
    if "mpeg" in content_type or "mp3" in content_type:
        format_hint = "mp3"
    elif "ogg" in content_type or "opus" in content_type:
        format_hint = "ogg"
    elif "flac" in content_type:
        format_hint = "flac"
    elif "aac" in content_type:
        format_hint = "aac"

    audio_data = io.BytesIO(response.content)
    try:
        return AudioSegment.from_file(audio_data, format=format_hint)
    except Exception:
        audio_data.seek(0)
        return AudioSegment.from_file(audio_data)


def _decode_audio_bytes(audio: bytes, *, format_hint: str = "wav") -> AudioSegment:
    if not audio:
        raise RuntimeError(
            "The speech service returned an empty response instead of audio."
        )
    audio_data = io.BytesIO(audio)
    try:
        return AudioSegment.from_file(audio_data, format=format_hint)
    except Exception:
        audio_data.seek(0)
        return AudioSegment.from_file(audio_data)


def _request_chatterbox_audio(
    text: str, tts_settings: dict, chatterbox_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(
        chatterbox_base_url, CHATTERBOX_API_BASE_URL
    )

    # Map to proper model id if alias is used
    model = str(tts_settings.get("xtts_model", CHATTERBOX_DEFAULT_MODEL) or "").strip()
    if model.lower() in {"turbo", "chatterbox-turbo"}:
        model = "chatterbox-turbo"
    elif model.lower() in {"multilingual", "chatterbox-multilingual"}:
        model = "chatterbox-multilingual"
    elif model.lower() in {"en", "chatterbox-en"}:
        model = "chatterbox-en"
    else:
        model = CHATTERBOX_DEFAULT_MODEL

    payload = {
        "model": model,
        "input": text,
        "voice": tts_settings.get("speaker") or None,
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
        "language": normalize_chatterbox_language_code(tts_settings.get("language")),
    }

    # Pass optional advanced parameters
    payload["temperature"] = _coerce_float(
        tts_settings.get("chatterbox_temperature")
        if tts_settings.get("chatterbox_temperature") is not None
        else tts_settings.get("temperature"),
        0.8,
    )
    payload["exaggeration"] = _coerce_float(
        tts_settings.get("chatterbox_exaggeration"),
        0.5,
    )
    payload["cfg_weight"] = _coerce_float(
        tts_settings.get("chatterbox_cfg_weight"),
        0.5,
    )
    raw_rep_penalty = _coerce_float(
        tts_settings.get("chatterbox_repetition_penalty")
        if tts_settings.get("chatterbox_repetition_penalty") is not None
        else tts_settings.get("repetition_penalty"),
        1.2,
    )
    payload["repetition_penalty"] = max(1.0, raw_rep_penalty)
    payload["min_p"] = _coerce_float(
        tts_settings.get("chatterbox_min_p"),
        0.05,
    )
    payload["top_p"] = _coerce_float(
        tts_settings.get("chatterbox_top_p")
        if tts_settings.get("chatterbox_top_p") is not None
        else tts_settings.get("top_p"),
        0.95,
    )
    payload["top_k"] = _coerce_int(
        tts_settings.get("chatterbox_top_k")
        if tts_settings.get("chatterbox_top_k") is not None
        else tts_settings.get("top_k"),
        1000,
    )
    payload["norm_loudness"] = _coerce_bool(
        tts_settings.get("chatterbox_norm_loudness"),
        True,
    )

    last_response = None
    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(XTTS_OPENAI_PLACEHOLDER_API_KEY),
            json=payload,
            timeout=TTS_GENERATION_TIMEOUT_SECONDS,
        )

        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No Chatterbox speech endpoint could be resolved for '{normalized_base_url}'."
    )


def _kobold_qwen_cloning_model_from_metadata(tts_settings: dict, voice: str) -> str:
    """Return the catalogue model for a cloned Qwen voice, when supplied."""
    if not voice:
        return ""
    metadata_sources = [tts_settings.get("voice_metadata")]
    provider_configs = tts_settings.get("provider_configs")
    if isinstance(provider_configs, list):
        metadata_sources.extend(
            item.get("voice_metadata")
            for item in provider_configs
            if (
                isinstance(item, dict)
                and _normalize_service_id(
                    item.get("id") or item.get("name") or item.get("provider")
                )
                == "kobold_qwen"
            )
        )
    for metadata in metadata_sources:
        if not isinstance(metadata, dict):
            continue
        for key, item in metadata.items():
            if not isinstance(item, dict):
                continue
            voice_id = str(item.get("id") or item.get("voice_id") or "").strip()
            if not voice_id and ":" in str(key):
                voice_id = str(key).rsplit(":", 1)[-1].strip()
            if voice_id.lower() != voice.lower():
                continue
            voice_type = str(item.get("type") or "").strip().lower()
            if voice_type and voice_type != "preset":
                return "Voice Cloning"
            model = str(item.get("model") or "").strip()
            if model.lower() in {"voice cloning", "qwen3-tts", "qwen3-tts-base"}:
                return "Voice Cloning"
    return ""


def resolve_kobold_qwen_model(
    tts_settings: dict, fallback: str = KOBOLD_QWEN_DEFAULT_MODEL
) -> str:
    """Resolve Qwen's current model field before its legacy XTTS alias.

    A voice catalogue can identify a cloned reference even when no model was
    selected yet; in that case the only compatible Qwen model is Voice Cloning.
    Explicit model selections deliberately remain authoritative so validation
    can reject an incompatible explicit pairing instead of masking it.
    """
    model = str(tts_settings.get("model") or "").strip()
    if model:
        return model
    legacy_model = str(tts_settings.get("xtts_model") or "").strip()
    if legacy_model:
        return legacy_model
    voice = str(tts_settings.get("speaker") or tts_settings.get("voice") or "").strip()
    return _kobold_qwen_cloning_model_from_metadata(tts_settings, voice) or fallback


def _kobold_qwen_is_ready(base_url: str, api_key: str = "") -> bool:
    """Return child readiness without confusing wrapper liveness with inference."""
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    headers = _openai_auth_headers(api_key or _resolve_kobold_qwen_api_key())
    try:
        response = requests.get(
            f"{normalized_base_url}/readyz",
            headers=headers,
            timeout=2,
        )
        if response.status_code < 400:
            return True
        if response.status_code != 404:
            return False
    except requests.exceptions.RequestException:
        return False

    # Compatibility with wrapper versions predating /readyz.
    try:
        response = requests.get(
            f"{normalized_base_url}/health",
            headers=headers,
            timeout=2,
        )
        if response.status_code >= 400:
            return False
        payload = response.json()
        return bool(
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("kobold_online") is True
        )
    except (requests.exceptions.RequestException, ValueError):
        return False


def _wait_for_kobold_qwen_recovery(
    base_url: str,
    *,
    api_key: str = "",
    timeout_seconds: float = 90.0,
    retry_after: float = 0.0,
    cancel_event=None,
) -> bool:
    """Wait for Qwen readiness; service downtime does not consume TTS attempts."""
    timeout_seconds = max(1.0, min(300.0, float(timeout_seconds or 90.0)))
    deadline = time.monotonic() + timeout_seconds
    initial_delay = min(timeout_seconds, max(0.0, float(retry_after or 0.0)))
    if initial_delay and not wait_for_retry(initial_delay, cancel_event):
        return False
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return False
        if _kobold_qwen_is_ready(base_url, api_key):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not wait_for_retry(min(0.5, remaining), cancel_event):
            return False
    return False


def _build_kobold_qwen_payload(text: str, tts_settings: dict) -> dict[str, object]:
    model = resolve_kobold_qwen_model(tts_settings)
    if not model:
        model = KOBOLD_QWEN_DEFAULT_MODEL

    normalized_model = model.lower()
    cloning_model = normalized_model in {
        "voice cloning",
        "qwen3-tts",
        "qwen3-tts-base",
    }
    voice = str(tts_settings.get("speaker") or tts_settings.get("voice") or "").strip()
    if not voice:
        voice = KOBOLD_QWEN_SAMPLE_VOICE if cloning_model else KOBOLD_QWEN_DEFAULT_VOICE

    preset_ids = {item.lower() for item in KOBOLD_QWEN_TTS_VOICES}
    if cloning_model and voice.lower() in preset_ids:
        raise ValueError(
            f"Qwen voice '{voice}' is pre-built and cannot be used with Voice Cloning. "
            "Choose a provider-uploaded reference voice instead."
        )
    if (
        normalized_model in {"prebuilt voices", "qwen3-tts-customvoice"}
        and voice.lower() not in preset_ids
    ):
        raise ValueError(
            f"Qwen voice '{voice}' is a cloning reference and cannot be used with Prebuilt Voices."
        )

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
        "response_format": "wav",
    }
    generation_prompt = str(tts_settings.get("generation_prompt") or "").strip()
    if generation_prompt and normalized_model in {
        item.lower() for item in KOBOLD_QWEN_GENERATION_PROMPT_MODELS
    }:
        payload["instructions"] = generation_prompt
    return payload


def _request_kobold_qwen_audio(
    text: str, tts_settings: dict, kobold_qwen_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(
        kobold_qwen_base_url, KOBOLD_QWEN_API_BASE_URL
    )
    api_key = _resolve_kobold_qwen_api_key(tts_settings)
    payload = _build_kobold_qwen_payload(text, tts_settings)

    last_response = None
    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(api_key),
            json=payload,
            timeout=KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS,
        )

        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No Qwen3 TTS speech endpoint could be resolved for '{normalized_base_url}'."
    )


def get_kobold_qwen_batch_capabilities(
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    *,
    api_key: str = "",
) -> dict[str, object]:
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    headers = _openai_auth_headers(api_key or _resolve_kobold_qwen_api_key())
    fallback = {
        "supported": False,
        "streaming": False,
        "default_batch_size": 1,
        "max_batch_size": 1,
    }
    for url in _openai_capabilities_urls(normalized_base_url):
        try:
            response = requests.get(url, headers=headers, timeout=2)
        except requests.RequestException:
            continue
        if _should_try_next_openai_candidate(response.status_code):
            continue
        if response.status_code >= 400:
            return fallback
        try:
            payload = response.json()
        except ValueError:
            return fallback
        batch = payload.get("batch_synthesis") if isinstance(payload, dict) else None
        if not isinstance(batch, dict):
            return fallback
        try:
            default_size = max(1, min(32, int(batch.get("default_batch_size") or 1)))
            maximum_size = max(
                default_size, min(32, int(batch.get("max_batch_size") or default_size))
            )
            parallelism = max(1, int(batch.get("parallelism") or 1))
        except (TypeError, ValueError):
            return fallback
        return {
            "supported": bool(batch.get("supported")),
            "streaming": bool(batch.get("streaming")),
            "endpoint": str(batch.get("endpoint") or "/v1/audio/speech/batch"),
            "protocol": str(batch.get("protocol") or ""),
            "default_batch_size": default_size,
            "max_batch_size": maximum_size,
            "parallelism": parallelism,
        }
    return fallback


def _iter_kobold_qwen_batch_audio_http(
    items: list[dict[str, object]],
    *,
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    api_key: str = "",
    stop_event: Event | None = None,
    cancel_event: Event | None = None,
):
    if not items:
        return
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    resolved_api_key = api_key or _resolve_kobold_qwen_api_key(
        next(
            (
                item["settings"]
                for item in items
                if isinstance(item.get("settings"), dict)
            ),
            {},
        )
    )
    request_items = []
    for item in items:
        item_id = str(item.get("id") or "").strip()
        settings = item.get("settings")
        if not item_id or not isinstance(settings, dict):
            raise ValueError("Qwen batch items require an ID and TTS settings.")
        request_items.append(
            {
                "id": item_id,
                **_build_kobold_qwen_payload(
                    str(item.get("text") or ""),
                    settings,
                ),
            }
        )

    request_payload = {
        "items": request_items,
        "stream": True,
        "fail_fast": False,
    }
    last_response = None
    for batch_url in _openai_audio_speech_batch_urls(normalized_base_url):
        response = requests.post(
            batch_url,
            headers=_openai_auth_headers(resolved_api_key),
            json=request_payload,
            stream=True,
            timeout=(10, KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS),
        )
        if _should_try_next_openai_candidate(response.status_code):
            response.close()
            last_response = response
            continue
        response.raise_for_status()
        with response:
            for raw_line in response.iter_lines(decode_unicode=True):
                if stop_event is not None and stop_event.is_set():
                    return
                line = (
                    raw_line.decode("utf-8")
                    if isinstance(raw_line, bytes)
                    else str(raw_line or "")
                ).strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError as error:
                    raise RuntimeError(
                        "Qwen batch synthesis returned invalid NDJSON."
                    ) from error
                if not isinstance(event, dict) or event.get("type") != "item":
                    continue
                item_id = str(event.get("id") or "").strip()
                if event.get("status") == "completed":
                    try:
                        audio_bytes = base64.b64decode(
                            str(event.get("audio_base64") or ""),
                            validate=True,
                        )
                        audio = _decode_audio_bytes(
                            audio_bytes,
                            format_hint=str(event.get("response_format") or "wav"),
                        )
                    except (ValueError, TypeError) as error:
                        raise RuntimeError(
                            f"Qwen batch item '{item_id}' returned invalid audio."
                        ) from error
                    yield {
                        "id": item_id,
                        "audio": audio,
                        "error": None,
                    }
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    continue
                error_payload = event.get("error")
                if not isinstance(error_payload, dict):
                    error_payload = {"detail": "Qwen batch item failed."}
                yield {
                    "id": item_id,
                    "audio": None,
                    "error": error_payload,
                }
                if cancel_event is not None and cancel_event.is_set():
                    return
        return

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError(
        f"No Qwen3 TTS batch endpoint could be resolved for '{normalized_base_url}'."
    )


def iter_kobold_qwen_batch_audio(
    items: list[dict[str, object]],
    *,
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    api_key: str = "",
    cancel_event: Event | None = None,
):
    """Read the batch stream ahead so inference overlaps local take handling."""
    if not items:
        return
    stop_event = Event()
    messages: Queue[tuple[str, object]] = Queue(maxsize=len(items) + 2)

    def read_stream() -> None:
        try:
            for event in _iter_kobold_qwen_batch_audio_http(
                items,
                base_url=base_url,
                api_key=api_key,
                stop_event=stop_event,
                cancel_event=cancel_event,
            ):
                messages.put(("event", event))
        except Exception as error:  # noqa: BLE001 - cross-thread projection
            messages.put(("error", error))
        finally:
            messages.put(("done", None))

    worker = Thread(
        target=read_stream,
        name="qwen-batch-reader",
        daemon=True,
    )
    worker.start()
    try:
        while True:
            kind, payload = messages.get()
            if kind == "done":
                return
            if kind == "error":
                if isinstance(payload, BaseException):
                    raise payload
                raise RuntimeError(str(payload))
            yield payload
    finally:
        stop_event.set()


# Audio Generation
def text_to_audio(
    text: str,
    tts_settings: dict,
    xtts_base_url: str = XTTS_API_BASE_URL,
    voxcpm_base_url: str = VOXCPM_API_BASE_URL,
    fishs2_base_url: str = FISHS2_API_BASE_URL,
    voxtral_base_url: str = VOXTRAL_API_BASE_URL,
    kokoro_base_url: str = KOKORO_API_BASE_URL,
    silero_base_url: str = SILERO_API_BASE_URL,
    chatterbox_base_url: str = CHATTERBOX_API_BASE_URL,
    kobold_qwen_base_url: str = KOBOLD_QWEN_API_BASE_URL,
    magpie_base_url: str = MAGPIE_API_BASE_URL,
    max_attempts: int = 5,
    cancel_event=None,
    retry_callback=None,
    recovery_callback=None,
    request_session: requests.Session | None = None,
    _audio_cpp_lock_held: bool = False,
    audio_cpp_base_url: str = AUDIO_CPP_API_BASE_URL,
) -> AudioSegment | None:
    """
    Generates audio from text using the specified TTS service.
    `tts_settings` is a dictionary-like object (e.g., a dataclass).
    """
    service_hint = str(tts_settings.get("service") or "").strip().lower()
    if service_hint in {"audio.cpp", "audio_cpp", "audio-cpp", "audiocpp"}:
        tts_settings = dict(tts_settings)
        tts_settings.setdefault("audio_cpp_base_url", audio_cpp_base_url)
    if not _audio_cpp_lock_held:
        endpoint, _error = resolve_openai_audio_endpoint(tts_settings)
        if (
            endpoint is not None
            and _normalize_custom_adapter(endpoint.get("adapter")) == AUDIO_CPP_ADAPTER
        ):
            with audio_cpp_endpoint_lock(tts_settings):
                return text_to_audio(
                    text,
                    tts_settings,
                    xtts_base_url=xtts_base_url,
                    voxcpm_base_url=voxcpm_base_url,
                    fishs2_base_url=fishs2_base_url,
                    voxtral_base_url=voxtral_base_url,
                    kokoro_base_url=kokoro_base_url,
                    silero_base_url=silero_base_url,
                    chatterbox_base_url=chatterbox_base_url,
                    kobold_qwen_base_url=kobold_qwen_base_url,
                    magpie_base_url=magpie_base_url,
                    audio_cpp_base_url=audio_cpp_base_url,
                    max_attempts=max_attempts,
                    cancel_event=cancel_event,
                    retry_callback=retry_callback,
                    recovery_callback=recovery_callback,
                    request_session=request_session,
                    _audio_cpp_lock_held=True,
                )
    # Visual subtitle wrapping must never leak into provider payloads.  This
    # also makes direct callers consistent with dubbing speech blocks.
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    service = tts_settings.get("service", "XTTS")
    normalized_silero_base_url = _normalize_base_url(
        silero_base_url, SILERO_API_BASE_URL
    )

    max_attempts = max(1, min(20, int(max_attempts or 1)))
    try:
        maximum_recovery_cycles = max(
            0,
            min(10, int(tts_settings.get("service_recovery_cycles") or 3)),
        )
    except (TypeError, ValueError):
        maximum_recovery_cycles = 3
    attempt = 0
    recovery_cycles = 0
    while attempt < max_attempts:
        if cancel_event is not None and cancel_event.is_set():
            logging.info(
                "TTS generation canceled before attempt %d/%d",
                attempt + 1,
                max_attempts,
            )
            return None
        attempt += 1
        try:
            if service == "XTTS":
                response = _request_xtts_audio(text, tts_settings, xtts_base_url)
            elif service == "VoxCPM":
                response = _request_voxcpm_audio(text, tts_settings, voxcpm_base_url)
            elif service == "FishS2":
                response = _request_fishs2_audio(text, tts_settings, fishs2_base_url)
            elif service == "Voxtral":
                response = _request_voxtral_audio(text, tts_settings, voxtral_base_url)
            elif service == "Kokoro":
                response = _request_kokoro_audio(text, tts_settings, kokoro_base_url)
            elif service == "Chatterbox":
                response = _request_chatterbox_audio(
                    text, tts_settings, chatterbox_base_url
                )
            elif service == "Qwen3 TTS":
                response = _request_kobold_qwen_audio(
                    text, tts_settings, kobold_qwen_base_url
                )
            elif service == "Magpie":
                response = _request_magpie_audio(text, tts_settings, magpie_base_url)
            elif service_hint in {"audio.cpp", "audio_cpp", "audio-cpp"} or service in {
                OPENAI_SERVICE,
                GEMINI_SERVICE,
                LEGACY_GEMINI_SERVICE,
                OPENAI_COMPAT_SERVICE,
                LEGACY_OPENAI_COMPAT_SERVICE,
            }:
                response = _request_openai_compatible_audio(
                    text,
                    tts_settings,
                    request_session=request_session,
                )
            elif str(service).strip().lower() in {
                ELEVENLABS_SERVICE.lower(),
                ELEVENLABS_PROVIDER,
            }:
                response = _request_elevenlabs_audio(text, tts_settings)
            elif service == VERTEX_SERVICE:
                response = _request_vertex_ai_audio(text, tts_settings)
            elif service == "Silero":
                data = {
                    "model": str(
                        tts_settings.get("silero_model")
                        or tts_settings.get("xtts_model")
                        or SILERO_DEFAULT_MODEL
                    ),
                    "input": text,
                    "voice": str(tts_settings.get("speaker") or ""),
                    "language": normalize_silero_language_code(
                        tts_settings.get("language")
                    ),
                    "response_format": "wav",
                    "speed": _coerce_float(tts_settings.get("speed"), 1.0),
                    "sample_rate": int(tts_settings.get("silero_sample_rate") or 48000),
                    "stress_mode": str(
                        tts_settings.get("silero_stress_mode") or "auto"
                    ),
                }
                response = requests.post(
                    f"{normalized_silero_base_url}/v1/audio/speech",
                    json=data,
                    timeout=TTS_GENERATION_TIMEOUT_SECONDS,
                )
            else:
                raise ValueError(f"Unsupported TTS service: {service}")

            response.raise_for_status()
            audio = _decode_audio_response(response)
            return audio

        except ValueError as e:
            # Invalid service, model, voice, or configuration will not improve
            # with another identical request.
            logging.error("TTS configuration error: %s", e)
            break
        except Exception as e:
            status = status_code_from_error(e)
            logging.warning(
                "TTS generation attempt %d/%d failed%s: %s",
                attempt,
                max_attempts,
                f" (HTTP {status})" if status else "",
                e,
            )
            response = getattr(e, "response", None)
            if response is not None:
                logging.warning(
                    "Server response: %s", str(getattr(response, "text", ""))[:4000]
                )
            if not retryable_error(e):
                logging.error(
                    "TTS request is not retryable%s.",
                    f" (HTTP {status})" if status else "",
                )
                break
            retry_after = retry_after_seconds(e)

            if service == "Qwen3 TTS" and recovery_cycles < maximum_recovery_cycles:
                recovery_cycles += 1
                try:
                    recovery_timeout = max(
                        1.0,
                        min(
                            300.0,
                            float(
                                tts_settings.get("service_recovery_timeout_seconds")
                                or 90.0
                            ),
                        ),
                    )
                except (TypeError, ValueError):
                    recovery_timeout = 90.0
                logging.info(
                    "Waiting up to %.1f seconds for Qwen3 TTS recovery (%d/%d).",
                    recovery_timeout,
                    recovery_cycles,
                    maximum_recovery_cycles,
                )
                if recovery_callback is not None:
                    recovery_callback(
                        recovery_cycles, maximum_recovery_cycles, recovery_timeout
                    )
                recovered = _wait_for_kobold_qwen_recovery(
                    kobold_qwen_base_url,
                    api_key=_resolve_kobold_qwen_api_key(tts_settings),
                    timeout_seconds=recovery_timeout,
                    retry_after=retry_after,
                    cancel_event=cancel_event,
                )
                if cancel_event is not None and cancel_event.is_set():
                    logging.info(
                        "TTS generation canceled while waiting for Qwen3 TTS recovery."
                    )
                    return None
                if recovered:
                    # Infrastructure recovery is bounded separately from real
                    # synthesis attempts, so connection-refused probes during
                    # a Manager restart cannot exhaust the five-attempt budget.
                    attempt -= 1
                    logging.info(
                        "Qwen3 TTS recovered; repeating synthesis attempt %d/%d.",
                        attempt + 1,
                        max_attempts,
                    )
                    continue
                logging.warning(
                    "Qwen3 TTS did not recover within %.1f seconds.", recovery_timeout
                )

        if attempt >= max_attempts:
            break
        try:
            maximum_retry_delay = max(
                1.0,
                min(300.0, float(tts_settings.get("retry_max_delay_seconds") or 90)),
            )
        except (TypeError, ValueError):
            maximum_retry_delay = 90.0
        # Google documents Vertex 429s as transient shared-capacity pressure
        # and recommends truncated exponential backoff with jitter.  A 0.5 s
        # base exhausted all five attempts in roughly eight seconds on a real
        # German TTS run, never allowing the quota/capacity window to recover.
        google_rate_limit = status == 429 and service in {
            VERTEX_SERVICE,
            GEMINI_SERVICE,
            LEGACY_GEMINI_SERVICE,
        }
        try:
            configured_base_delay = float(
                tts_settings.get("rate_limit_retry_base_delay_seconds")
                or (5.0 if google_rate_limit else 0.5)
            )
        except (TypeError, ValueError):
            configured_base_delay = 5.0 if google_rate_limit else 0.5
        delay = retry_delay_seconds(
            attempt,
            retry_after=retry_after,
            base_delay=max(0.1, min(60.0, configured_base_delay)),
            maximum_delay=maximum_retry_delay,
        )
        if retry_callback is not None:
            retry_callback(attempt + 1, max_attempts, delay)
        logging.info(
            "Retrying TTS generation in %.1f seconds (attempt %d/%d).",
            delay,
            attempt + 1,
            max_attempts,
        )
        if not wait_for_retry(delay, cancel_event):
            logging.info("TTS generation canceled while waiting to retry.")
            return None

    logging.error(
        "Failed to generate TTS audio after %d attempts: '%s...'", attempt, text[:50]
    )
    return None
