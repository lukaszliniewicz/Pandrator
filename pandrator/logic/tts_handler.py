import io
import json
import logging
import os
import copy
import re
from contextlib import ExitStack
from urllib.parse import urljoin, urlparse, urlunparse

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

_litellm_speech = None
_litellm_speech_import_attempted = False


def _get_litellm_speech_client():
    global _litellm_speech, _litellm_speech_import_attempted
    if not _litellm_speech_import_attempted:
        _litellm_speech_import_attempted = True
        try:
            from litellm import speech as litellm_speech
        except Exception as e:  # pragma: no cover - runtime dependency guard
            logging.debug("LiteLLM speech import failed: %s", e)
        else:
            _litellm_speech = litellm_speech

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
        _infer_kokoro_voice_component_language_code(part)
        for part in parts
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
OPENAI_AUDIO_BASE_URL = "https://api.openai.com/v1"
GEMINI_AUDIO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

OPENAI_SERVICE = "OpenAI"
GEMINI_SERVICE = "Google Gemini"
LEGACY_GEMINI_SERVICE = "Gemini"
OPENAI_COMPAT_SERVICE = "Custom"
LEGACY_OPENAI_COMPAT_SERVICE = "OpenAI-Compatible"

OPENAI_PROVIDER = "openai"
GEMINI_PROVIDER = "gemini"
SUPPORTED_AUDIO_PROVIDERS = {OPENAI_PROVIDER, GEMINI_PROVIDER}

OPENAI_TTS_MODELS = [
    "gpt-4o-mini-tts",
    "tts-1-hd",
    "tts-1",
]

GEMINI_TTS_MODELS = [
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
]

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
    "gemini-2.5-flash-tts": "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-tts": "gemini-2.5-pro-preview-tts",
}

FIRST_CLASS_SERVICE_ORDER = [
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
]
FIRST_CLASS_SERVICE_IDS = set(FIRST_CLASS_SERVICE_ORDER)
FIRST_CLASS_SERVICE_NAMES = {
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
}
SERVICE_ID_ALIASES = {
    "voxcpm2": "voxcpm",
    "voxcpm-2": "voxcpm",
    "fish-s2": "fishs2",
    "fishs2-cpp": "fishs2",
    "google": GEMINI_PROVIDER,
    "google-gemini": GEMINI_PROVIDER,
    "gemini": GEMINI_PROVIDER,
    "kobold-qwen": "kobold_qwen",
    "koboldqwen": "kobold_qwen",
    "qwen": "kobold_qwen",
    "qwen3": "kobold_qwen",
    "qwen3-tts": "kobold_qwen",
}
PREBUILT_VOICE_PROVIDER_FIELD = "supports_prebuilt_voices"
OPENAI_COMPAT_ADAPTER = "openai_compatible"
GENERIC_JSON_ADAPTER = "generic_json"
SUPPORTED_CUSTOM_TTS_ADAPTERS = {OPENAI_COMPAT_ADAPTER, GENERIC_JSON_ADAPTER}


def _read_setting(settings, key: str, default=None):
    if settings is None:
        return default
    if isinstance(settings, dict):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _normalize_custom_adapter(raw_adapter: str | None) -> str:
    normalized = str(raw_adapter or "").strip().lower().replace("-", "_")
    aliases = {
        "openai": OPENAI_COMPAT_ADAPTER,
        "openai_compatible": OPENAI_COMPAT_ADAPTER,
        "generic": GENERIC_JSON_ADAPTER,
        "json": GENERIC_JSON_ADAPTER,
        "generic_json": GENERIC_JSON_ADAPTER,
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
    if adapter == OPENAI_COMPAT_ADAPTER:
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

    return {
        "adapter": adapter,
        "profile_id": str(config.get("profile_id") or "").strip(),
        "speech_path": str(config.get("speech_path") or "").strip(),
        "models_path": str(config.get("models_path") or "").strip(),
        "voices_path": str(config.get("voices_path") or "").strip(),
        "request_fields": normalized_fields,
        "request_defaults": normalized_defaults,
    }


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
        "xtts": ([XTTS_DEFAULT_MODEL], XTTS_DEFAULT_MODEL, [], "", False),
        "voxcpm": (list(VOXCPM_TTS_MODELS), VOXCPM_DEFAULT_MODEL, [VOXCPM_DEFAULT_VOICE], VOXCPM_DEFAULT_VOICE, False),
        "fishs2": ([FISHS2_DEFAULT_MODEL, *FISHS2_MODEL_ALIASES], FISHS2_DEFAULT_MODEL, [FISHS2_DEFAULT_VOICE], FISHS2_DEFAULT_VOICE, False),
        "voxtral": (list(VOXTRAL_TTS_MODELS), VOXTRAL_DEFAULT_MODEL, [VOXTRAL_DEFAULT_VOICE], VOXTRAL_DEFAULT_VOICE, True),
        "kokoro": (list(KOKORO_TTS_MODELS), KOKORO_DEFAULT_MODEL, list(KOKORO_TTS_VOICES), KOKORO_DEFAULT_VOICE, True),
        "magpie": (list(MAGPIE_TTS_MODELS), MAGPIE_TTS_MODELS[0], magpie_voice_catalog(), magpie_voice_catalog()[0], True),
        "silero": ([str(item["code"]) for item in SILERO_LANGUAGES], str(SILERO_LANGUAGES[0]["code"]), [], "", True),
        "chatterbox": (list(CHATTERBOX_TTS_MODELS), CHATTERBOX_DEFAULT_MODEL, [], "", False),
        "kobold_qwen": (list(KOBOLD_QWEN_TTS_MODELS), KOBOLD_QWEN_DEFAULT_MODEL, list(KOBOLD_QWEN_TTS_VOICES), KOBOLD_QWEN_DEFAULT_VOICE, True),
    }
    configs: list[dict[str, object]] = []
    for service_id, api_base in local_services:
        models, default_model, voices, default_voice, prebuilt = local_catalogues[service_id]
        configs.append({
            "id": service_id,
            "name": FIRST_CLASS_SERVICE_NAMES[service_id],
            "kind": "local",
            "api_base": api_base,
            "models": models,
            "default_model": default_model,
            "voices": voices,
            "default_voice": default_voice,
            "voice_catalogues": {default_model: voices} if default_model else {},
            "default_voices": {default_model: default_voice} if default_model and default_voice else {},
            PREBUILT_VOICE_PROVIDER_FIELD: prebuilt,
        })
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
                PREBUILT_VOICE_PROVIDER_FIELD: True,
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
                PREBUILT_VOICE_PROVIDER_FIELD: True,
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
    if record.get("kind") == "commercial":
        record["api_key_env"] = str(
            raw_record.get("api_key_env") or record.get("api_key_env") or ""
        ).strip()
        record["api_key"] = str(raw_record.get("api_key") or "").strip()

    for key in ("adapter", "profile_id", "speech_path", "models_path", "voices_path"):
        if str(raw_record.get(key) or "").strip():
            record[key] = str(raw_record[key]).strip()
    for key in ("request_fields", "request_defaults"):
        if isinstance(raw_record.get(key), dict):
            record[key] = copy.deepcopy(raw_record[key])
    for key in ("settings", "voice_catalogues", "default_voices", "default_voices_by_language"):
        if isinstance(raw_record.get(key), dict):
            record[key] = copy.deepcopy(raw_record[key])
    if PREBUILT_VOICE_PROVIDER_FIELD in raw_record:
        record[PREBUILT_VOICE_PROVIDER_FIELD] = bool(raw_record[PREBUILT_VOICE_PROVIDER_FIELD])

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
        str(item["id"]): copy.deepcopy(item)
        for item in _default_service_configs()
    }

    legacy_raw_json = str(_read_setting(tts_settings, "openai_audio_endpoints_json", "") or "")
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


def get_service_config(tts_settings, service_name_or_id: str) -> dict[str, object] | None:
    service_id = _normalize_service_id(service_name_or_id)
    for service in get_service_configs(tts_settings):
        if str(service.get("id") or "") == service_id:
            return service
    return None


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
        and _coerce_bool(_read_setting(tts_settings, "use_external_server", False), False)
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
        return False, get_service_configs(tts_settings), "Select a first-class TTS service."

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
        if updated.get("kind") == "commercial":
            provider_key = str(updated.get("provider") or service_id)
            updated["api_key"] = str(api_key or "").strip()
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

            raw_id = str(raw_provider.get("id") or raw_provider.get("name") or "").strip()
            provider_id = _normalize_provider_id(raw_id)
            if not provider_id or _normalize_service_id(provider_id) in FIRST_CLASS_SERVICE_IDS:
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
                "name": str(raw_provider.get("name") or provider_id).strip() or provider_id,
                "provider": provider_key,
                "api_base": api_base,
                "api_key_env": str(raw_provider.get("api_key_env") or "").strip(),
                "api_key": str(raw_provider.get("api_key") or "").strip(),
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
                and adapter == OPENAI_COMPAT_ADAPTER
                and not profile_id
            ):
                builtin_models = _provider_model_catalog(provider_key)
                models = list(builtin_models)

            if not default_model:
                default_model = (
                    models[0]
                    if models
                    else (
                        _provider_default_model(provider_key)
                        if adapter == OPENAI_COMPAT_ADAPTER and not profile_id
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
                and adapter == OPENAI_COMPAT_ADAPTER
                and not profile_id
            ):
                voices = _provider_voice_catalog(provider_key, default_model)

            if not default_voice:
                default_voice = (
                    voices[0]
                    if voices
                    else (
                        _provider_default_voice(provider_key)
                        if adapter == OPENAI_COMPAT_ADAPTER and not profile_id
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
            for key in ("settings", "voice_catalogues", "default_voices", "default_voices_by_language"):
                if isinstance(raw_provider.get(key), dict):
                    record[key] = copy.deepcopy(raw_provider[key])

            custom_configs[provider_id] = record

    legacy_raw_json = str(_read_setting(tts_settings, "openai_audio_endpoints_json", "") or "")
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
        return False, get_provider_configs(tts_settings), "", "Provider name is required."

    normalized_provider_id = _normalize_provider_id(provider_id or display_name)
    if not normalized_provider_id:
        return False, get_provider_configs(tts_settings), "", "Provider name must include letters or numbers."
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
        return False, get_provider_configs(tts_settings), "", "Provider type must be OpenAI or Gemini compatible."

    normalized_api_base = _normalize_base_url(api_base, "")
    if not normalized_api_base:
        return False, get_provider_configs(tts_settings), "", "API base URL is required."

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
            return False, provider_configs, "", "A discovered speech path is required for generic JSON endpoints."
        request_fields = normalized_adapter_config.get("request_fields", {})
        if not isinstance(request_fields, dict) or not str(request_fields.get("text") or "").strip():
            return False, provider_configs, "", "A text request field is required for generic JSON endpoints."

    parsed_models = _parse_model_list(models or [], normalized_provider_type)
    if not parsed_models and existing is not None:
        parsed_models = _parse_model_list(existing.get("models", []), normalized_provider_type)
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
        parsed_voices = _parse_voice_list(existing.get("voices", []), normalized_provider_type)
    if not parsed_voices and adapter == OPENAI_COMPAT_ADAPTER and not profile_id:
        parsed_voices = list(_provider_voice_catalog(normalized_provider_type, default_model))

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
        return False, get_provider_configs(tts_settings), "Select a custom provider first."

    if _normalize_service_id(provider_id) in FIRST_CLASS_SERVICE_IDS:
        return False, get_provider_configs(tts_settings), "First-class TTS services cannot be removed."

    provider_configs = get_provider_configs(tts_settings)
    updated_provider_configs = [
        item
        for item in provider_configs
        if str(item.get("id") or "") != provider_id
    ]

    if len(updated_provider_configs) == len(provider_configs):
        return False, provider_configs, f"Provider '{provider_name_or_id}' was not found."

    return True, updated_provider_configs, ""


def _normalize_base_url(base_url: str | None, fallback: str) -> str:
    normalized = (base_url or fallback).strip().rstrip("/")
    return normalized or fallback


def _openai_auth_headers(api_key: str = XTTS_OPENAI_PLACEHOLDER_API_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _configured_endpoint_url(base_url: str, path: str) -> str:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        return str(base_url or "").strip().rstrip("/")
    if normalized_path.startswith(("http://", "https://")):
        return normalized_path

    parsed = urlparse(str(base_url or "").strip())
    origin = urlunparse(parsed._replace(path="", params="", query="", fragment="")).rstrip("/")
    return urljoin(f"{origin}/", normalized_path.lstrip("/"))


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


def _build_xtts_instructions_payload(tts_settings: dict, existing_instructions: str) -> str:
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


def _normalize_voxcpm_model(model_name: str, fallback: str = VOXCPM_DEFAULT_MODEL) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        return fallback

    lowered = normalized.lower()
    if lowered in {"openbmb/voxcpm2", "voxcpm2"}:
        return VOXCPM_DEFAULT_MODEL
    return normalized


def _normalize_fishs2_model(model_name: str, fallback: str = FISHS2_DEFAULT_MODEL) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        return fallback

    lowered = normalized.lower()
    if lowered in {"fishs2", "fish-s2", "s2-pro", "fishaudio/s2-pro"}:
        return FISHS2_DEFAULT_MODEL
    return normalized


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

    latency = str(tts_settings.get("fishs2_latency") or FISHS2_DEFAULT_LATENCY).strip().lower()
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


def _normalize_voxtral_model(model_name: str, fallback: str = VOXTRAL_DEFAULT_MODEL) -> str:
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
        "max_chunk_chars": _coerce_int(tts_settings.get("voxtral_max_chunk_chars"), 500),
        "chunk_silence_ms": _coerce_int(tts_settings.get("voxtral_chunk_silence_ms"), 0),
        "strip_quotes": _coerce_bool(tts_settings.get("voxtral_strip_quotes"), False),
        "strip_diacritics": _coerce_bool(tts_settings.get("voxtral_strip_diacritics"), False),
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


def _build_voxtral_instructions_payload(tts_settings: dict, existing_instructions: str) -> str:
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
    }
    provider = aliases.get(provider, provider)
    return provider if provider in SUPPORTED_AUDIO_PROVIDERS else ""


def _infer_audio_provider(name: str, base_url: str, raw_provider: str | None = None) -> str:
    explicit = _normalize_audio_provider(raw_provider)
    if explicit:
        return explicit

    hint = f"{name} {base_url}".lower()
    if "generativelanguage.googleapis.com" in hint or "gemini" in hint:
        return GEMINI_PROVIDER
    return OPENAI_PROVIDER


def _provider_default_model(provider: str) -> str:
    if provider == GEMINI_PROVIDER:
        return GEMINI_AUDIO_DEFAULT_MODEL
    return OPENAI_AUDIO_DEFAULT_MODEL


def _provider_default_voice(provider: str) -> str:
    if provider == GEMINI_PROVIDER:
        return GEMINI_AUDIO_DEFAULT_VOICE
    return OPENAI_AUDIO_DEFAULT_VOICE


def _provider_model_catalog(provider: str) -> list[str]:
    if provider == GEMINI_PROVIDER:
        return list(GEMINI_TTS_MODELS)
    return list(OPENAI_TTS_MODELS)


def _provider_voice_catalog(provider: str, model_name: str = "") -> list[str]:
    if provider == GEMINI_PROVIDER:
        return list(GEMINI_TTS_VOICES)

    normalized_model = _normalize_model_for_provider(model_name, provider).lower()
    if normalized_model in {"tts-1", "tts-1-hd"}:
        return list(OPENAI_TTS_CLASSIC_VOICES)
    return list(OPENAI_TTS_VOICES)


def _provider_for_tts_service(raw_service: str | None) -> str:
    normalized = str(raw_service or "").strip().lower()
    if normalized == OPENAI_SERVICE.lower():
        return OPENAI_PROVIDER
    if normalized in {GEMINI_SERVICE.lower(), LEGACY_GEMINI_SERVICE.lower()}:
        return GEMINI_PROVIDER
    return ""


def _service_audio_endpoint(tts_settings, provider: str) -> dict[str, str]:
    normalized_provider = _normalize_audio_provider(provider)
    service = get_service_config(tts_settings, normalized_provider)
    if service is None:
        service = get_service_config({}, normalized_provider) or {}

    return {
        "name": normalized_provider,
        "display_name": str(service.get("name") or normalized_provider),
        "base_url": str(service.get("api_base") or ""),
        "api_key": str(service.get("api_key") or ""),
        "api_key_env": str(service.get("api_key_env") or ""),
        "provider": normalized_provider,
        "default_model": str(service.get("default_model") or _provider_default_model(normalized_provider)),
        "default_voice": str(service.get("default_voice") or _provider_default_voice(normalized_provider)),
        "models": list(service.get("models") or []),
        "voices": list(service.get("voices") or []),
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

    voice_map = {
        voice.lower(): voice
        for voice in _provider_voice_catalog(provider)
    }
    return voice_map.get(normalized.lower(), normalized)


def _to_litellm_model_name(provider: str, model_name: str) -> str:
    normalized = _normalize_model_for_provider(model_name, provider)
    if "/" in normalized:
        maybe_provider, remainder = normalized.split("/", 1)
        if maybe_provider.lower() in SUPPORTED_AUDIO_PROVIDERS and remainder.strip():
            return f"{maybe_provider.lower()}/{remainder.strip()}"
    return f"{provider}/{normalized}"


def _merge_catalog_with_discovered(preferred: list[str], discovered: list[str]) -> list[str]:
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


def _configured_openai_urls(endpoint: dict[str, object], path_key: str, fallback_urls: list[str]) -> list[str]:
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
                voice.get("voice_id")
                or voice.get("id")
                or voice.get("name")
                or ""
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
    default_model = _normalize_voxtral_model(payload.get("default_model", ""), fallback="")
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
        if not default_model and not profile_id:
            default_model = _provider_default_model(provider)
        default_model = _normalize_model_for_provider(default_model, provider)

        default_voice = str(provider_record.get("default_voice", "")).strip()
        if not default_voice and not profile_id:
            default_voice = _provider_default_voice(provider)
        default_voice = _normalize_voice_for_provider(default_voice, provider)

        endpoints[provider_id] = {
            "name": provider_id,
            "display_name": str(provider_record.get("name", "") or provider_id),
            "base_url": base_url,
            "api_key": str(provider_record.get("api_key", "")).strip(),
            "api_key_env": str(provider_record.get("api_key_env", "")).strip(),
            "provider": provider,
            "default_model": default_model,
            "default_voice": default_voice,
            "models": list(provider_record.get("models") or []),
            "voices": list(provider_record.get("voices") or []),
            "adapter": str(provider_record.get("adapter") or OPENAI_COMPAT_ADAPTER),
            "profile_id": profile_id,
            "speech_path": str(provider_record.get("speech_path") or ""),
            "models_path": str(provider_record.get("models_path") or ""),
            "voices_path": str(provider_record.get("voices_path") or ""),
            "request_fields": dict(provider_record.get("request_fields") or {}),
            "request_defaults": dict(provider_record.get("request_defaults") or {}),
        }

    return endpoints


def list_openai_audio_endpoint_names(tts_settings: dict) -> list[str]:
    """Lists configured custom audio endpoint names."""
    service_provider = _provider_for_tts_service(tts_settings.get("service"))
    if service_provider:
        return [_service_audio_endpoint(tts_settings, service_provider)["name"]]

    return sorted(_parse_openai_audio_endpoints(tts_settings).keys())


def resolve_openai_audio_endpoint(tts_settings: dict) -> tuple[dict[str, object] | None, str]:
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
            return None, f"Custom audio endpoint '{selected_name}' is not defined in config."
        return endpoint, ""

    first_name = sorted(endpoints.keys())[0]
    return endpoints[first_name], ""


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
            return _openai_auth_headers(env_value)

    explicit_key = str(endpoint.get("api_key", "") or "").strip()
    return _openai_auth_headers(explicit_key) if explicit_key else {}


def _resolve_voxcpm_api_key() -> str:
    api_key = os.getenv("VOXCPM_API_KEY", "").strip()
    if api_key:
        return api_key
    return XTTS_OPENAI_PLACEHOLDER_API_KEY


def _resolve_fishs2_api_key() -> str:
    api_key = os.getenv("FISHS2_API_KEY", "").strip()
    if api_key:
        return api_key
    return XTTS_OPENAI_PLACEHOLDER_API_KEY


def _resolve_voxtral_api_key() -> str:
    api_key = os.getenv("VOXTRAL_API_KEY", "").strip()
    if api_key:
        return api_key
    return XTTS_OPENAI_PLACEHOLDER_API_KEY


def _resolve_kokoro_api_key() -> str:
    api_key = os.getenv("KOKORO_API_KEY", "").strip()
    if api_key:
        return api_key
    return XTTS_OPENAI_PLACEHOLDER_API_KEY


def _resolve_kobold_qwen_api_key() -> str:
    api_key = os.getenv("KOBOLD_QWEN_API_KEY", "").strip()
    if api_key:
        return api_key
    return XTTS_OPENAI_PLACEHOLDER_API_KEY


def check_openai_audio_connection(tts_settings: dict) -> tuple[bool, str]:
    """Checks custom audio endpoint reachability."""
    endpoint, error = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        return False, error

    if _normalize_custom_adapter(endpoint.get("adapter")) == GENERIC_JSON_ADAPTER:
        speech_path = str(endpoint.get("speech_path") or "").strip()
        if not speech_path:
            return False, f"Endpoint '{endpoint['name']}' has no configured speech route."
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
                return True, f"Connected to endpoint '{endpoint['name']}' at {speech_path}."
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
    endpoint, provider, default_model, _ = _resolve_openai_audio_provider_context(tts_settings)
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
    preferred_models = [default_model] + list(endpoint.get("models") or []) + builtin_models
    return _dedupe_ordered(preferred_models)


def get_openai_audio_voices_fallback(tts_settings: dict) -> list[str]:
    """Returns fallback voice suggestions for custom audio providers."""
    endpoint, provider, default_model, default_voice = _resolve_openai_audio_provider_context(tts_settings)
    if endpoint is None:
        return []

    selected_model = str(tts_settings.get("xtts_model") or "").strip() or default_model
    selected_model = _normalize_model_for_provider(selected_model, provider)

    builtin_voices = (
        _provider_voice_catalog(provider, selected_model)
        if (
            _normalize_custom_adapter(endpoint.get("adapter")) == OPENAI_COMPAT_ADAPTER
            and not endpoint.get("profile_id")
        )
        else []
    )
    preferred_voices = [default_voice] + list(endpoint.get("voices") or []) + builtin_voices
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
            item = next((item.get(key) for key in id_keys if item.get(key) is not None), "")
        normalized = str(item or "").strip()
        if normalized:
            values.append(normalized)
    return _dedupe_ordered(values)


def get_openai_audio_models(tts_settings: dict) -> list[str]:
    """Fetches model IDs from the configured custom audio endpoint."""
    endpoint, provider, default_model, _ = _resolve_openai_audio_provider_context(tts_settings)
    if endpoint is None:
        return []

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
                models = _dedupe_ordered(models + _extract_generic_catalog(response.json(), "models"))
            except (requests.exceptions.RequestException, ValueError) as e:
                logging.debug("Could not list models for endpoint '%s': %s", endpoint["name"], e)
        return models

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
            logging.error("Failed to list models for endpoint '%s': %s", endpoint["name"], e)
            continue

    builtin_models = [] if endpoint.get("profile_id") else _provider_model_catalog(provider)
    preferred_models = [default_model] + list(endpoint.get("models") or []) + builtin_models

    return _merge_catalog_with_discovered(preferred_models, models)


def get_openai_audio_voices(tts_settings: dict) -> list[str]:
    """Fetches voice IDs from the configured custom audio endpoint."""
    endpoint, provider, default_model, default_voice = _resolve_openai_audio_provider_context(tts_settings)
    if endpoint is None:
        return []

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
                voices = _dedupe_ordered(voices + _extract_generic_catalog(response.json(), "voices"))
            except (requests.exceptions.RequestException, ValueError) as e:
                logging.debug("Could not list voices for endpoint '%s': %s", endpoint["name"], e)
        return voices

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
            logging.debug("Could not list voices for endpoint '%s': %s", endpoint["name"], e)
            continue

    selected_model = str(tts_settings.get("xtts_model") or "").strip()
    if not selected_model:
        selected_model = default_model
    selected_model = _normalize_model_for_provider(selected_model, provider)

    builtin_voices = (
        [] if endpoint.get("profile_id") else _provider_voice_catalog(provider, selected_model)
    )
    preferred_voices = [default_voice] + list(endpoint.get("voices") or []) + builtin_voices

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

    preferred_models = [FISHS2_DEFAULT_MODEL] + FISHS2_MODEL_ALIASES
    return _merge_catalog_with_discovered(preferred_models, discovered_models)


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


def get_kobold_qwen_voices(base_url: str = KOBOLD_QWEN_API_BASE_URL) -> list[str]:
    """Fetches available Qwen3 TTS voices from server."""
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    api_key = _resolve_kobold_qwen_api_key()

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
            logging.debug("Could not list Qwen3 TTS voices from %s: %s", voices_url, e)
            continue

    return _merge_catalog_with_discovered(KOBOLD_QWEN_TTS_VOICES, discovered_voices)


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
        return _merge_catalog_with_discovered([VOXTRAL_DEFAULT_MODEL], discovered_models)

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
    try:
        response = requests.get(f"{_normalize_base_url(base_url, SILERO_API_BASE_URL)}/docs", timeout=3)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException:
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
        discovered = [str(m["id"]) for m in payload.get("data", []) if isinstance(m, dict)]
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


def _request_magpie_audio(text: str, tts_settings: dict, magpie_base_url: str) -> requests.Response:
    """Sends a TTS request to the Magpie TTS server."""
    from ..constants import MAGPIE_LOCALE_MAP, MAGPIE_SPEAKERS

    voice = str(tts_settings.get("speaker") or "").strip() or "Magpie-Multilingual.EN-US.Aria"
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

    raise RuntimeError(f"No Magpie speech endpoint could be resolved for '{normalized_base_url}'.")


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
            payload.get("id")
            or payload.get("voice_id")
            or payload.get("name")
            or ""
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
    resolved_voice_id = str(voice_id or fallback_voice_name).strip() or fallback_voice_name

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

        if (
            last_voice_response is not None
            and _should_try_next_openai_candidate(last_voice_response.status_code)
        ):
            logging.debug(
                "%s server at %s does not expose /audio/voices upload; tried /files fallback.",
                service_name,
                normalized_base_url,
            )

        if (
            last_response is not None
            and _should_try_next_openai_candidate(last_response.status_code)
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
) -> str:
    """Uploads voice to VoxCPM and returns uploaded voice identifier."""
    return _upload_speaker_voice_openai_compatible(
        wav_file_path,
        base_url=base_url,
        fallback_base_url=VOXCPM_API_BASE_URL,
        service_name="VoxCPM",
        api_key=_resolve_voxcpm_api_key(),
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
) -> str:
    """Uploads voice to FishS2 and returns uploaded voice identifier."""
    return _upload_speaker_voice_openai_compatible(
        wav_file_path,
        base_url=base_url,
        fallback_base_url=FISHS2_API_BASE_URL,
        service_name="FishS2",
        api_key=_resolve_fishs2_api_key(),
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
) -> str:
    """Uploads voice to Qwen3 TTS and returns uploaded voice identifier."""
    return _upload_speaker_voice_openai_compatible(
        wav_file_path,
        base_url=base_url,
        fallback_base_url=KOBOLD_QWEN_API_BASE_URL,
        service_name="Qwen3 TTS",
        api_key=_resolve_kobold_qwen_api_key(),
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
        )

    if normalized_service in {"fishs2", "fish-s2", "fishs2-cpp", "fishs2cpp"}:
        return upload_fishs2_speaker_voice(
            wav_file_path,
            base_url=base_url,
            prompt_text=prompt_text,
            voice_id=voice_id,
        )

    if normalized_service in {"chatterbox", "chatterbox-turbo"}:
        return upload_chatterbox_speaker_voice(
            wav_file_path,
            base_url=base_url,
            prompt_text=prompt_text,
            voice_id=voice_id,
        )

    if normalized_service in {"qwen3 tts", "qwen3-tts", "qwen3", "qwen", "kobold-qwen", "kobold_qwen"}:
        return upload_kobold_qwen_speaker_voice(
            wav_file_path,
            base_url=base_url,
            voice_id=voice_id,
        )

    return upload_xtts_speaker_voice(
        wav_file_path,
        base_url=base_url,
        voice_id=voice_id,
    )


# Silero Functions
def set_silero_language(language_code: str, base_url: str = SILERO_API_BASE_URL) -> bool:
    """Sets the active language on the Silero server."""
    try:
        response = requests.post(
            f"{_normalize_base_url(base_url, SILERO_API_BASE_URL)}/tts/language",
            json={"id": language_code},
            timeout=8,
        )
        response.raise_for_status()
        logging.info("Silero language set to %s", language_code)
        return True
    except requests.exceptions.RequestException as e:
        logging.error("Failed to set Silero language: %s", e)
        return False


def get_silero_speakers(base_url: str = SILERO_API_BASE_URL) -> list[str]:
    """Fetches the list of available speakers from the Silero server."""
    try:
        response = requests.get(
            f"{_normalize_base_url(base_url, SILERO_API_BASE_URL)}/tts/speakers",
            timeout=8,
        )
        response.raise_for_status()
        return [speaker["name"] for speaker in response.json()]
    except requests.exceptions.RequestException as e:
        logging.error("Failed to fetch Silero speakers: %s", e)
        return []


def _build_xtts_openai_payload(text: str, tts_settings: dict) -> dict:
    model = str(tts_settings.get("xtts_model") or XTTS_DEFAULT_MODEL).strip() or XTTS_DEFAULT_MODEL
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


def _request_xtts_audio(text: str, tts_settings: dict, xtts_base_url: str) -> requests.Response:
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

    raise RuntimeError(f"No XTTS speech endpoint could be resolved for '{normalized_base_url}'.")


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


def _request_voxcpm_audio(text: str, tts_settings: dict, voxcpm_base_url: str) -> requests.Response:
    normalized_base_url = _normalize_base_url(voxcpm_base_url, VOXCPM_API_BASE_URL)
    api_key = _resolve_voxcpm_api_key()
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

    raise RuntimeError(f"No VoxCPM speech endpoint could be resolved for '{normalized_base_url}'.")


def _build_fishs2_payload(text: str, tts_settings: dict) -> dict:
    model = _normalize_fishs2_model(
        tts_settings.get("xtts_model", ""),
        fallback=FISHS2_DEFAULT_MODEL,
    )
    voice = str(tts_settings.get("speaker") or "").strip() or FISHS2_DEFAULT_VOICE
    fishs2_options = _build_fishs2_options(tts_settings)
    prosody = fishs2_options.get("prosody") if isinstance(fishs2_options.get("prosody"), dict) else {}

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


def _request_fishs2_audio(text: str, tts_settings: dict, fishs2_base_url: str) -> requests.Response:
    normalized_base_url = _normalize_base_url(fishs2_base_url, FISHS2_API_BASE_URL)
    api_key = _resolve_fishs2_api_key()
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

    raise RuntimeError(f"No FishS2 speech endpoint could be resolved for '{normalized_base_url}'.")


def _build_voxtral_payload(text: str, tts_settings: dict) -> dict:
    model = _normalize_voxtral_model(tts_settings.get("xtts_model", ""), fallback=VOXTRAL_DEFAULT_MODEL)
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


def _request_voxtral_audio(text: str, tts_settings: dict, voxtral_base_url: str) -> requests.Response:
    normalized_base_url = _normalize_base_url(voxtral_base_url, VOXTRAL_API_BASE_URL)
    api_key = _resolve_voxtral_api_key()
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

    raise RuntimeError(f"No Voxtral speech endpoint could be resolved for '{normalized_base_url}'.")


def _request_kokoro_audio(text: str, tts_settings: dict, kokoro_base_url: str) -> requests.Response:
    normalized_base_url = _normalize_base_url(kokoro_base_url, KOKORO_API_BASE_URL)
    api_key = _resolve_kokoro_api_key()
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

    raise RuntimeError(f"No Kokoro speech endpoint could be resolved for '{normalized_base_url}'.")


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
        model_name = str(endpoint.get("default_model", "")).strip() or _provider_default_model(provider)
    model_name = _normalize_model_for_provider(model_name, provider)

    voice_name = str(tts_settings.get("speaker") or "").strip()
    if not voice_name:
        voice_name = str(endpoint.get("default_voice", "")).strip() or _provider_default_voice(provider)
    voice_name = _normalize_voice_for_provider(voice_name, provider)

    payload = {
        "model": model_name,
        "input": text,
        "voice": voice_name,
        "response_format": "wav",
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
    }

    instructions = str(tts_settings.get("openai_audio_instructions") or "").strip()
    if _is_xtts_target(model_name, endpoint):
        payload["instructions"] = _build_xtts_instructions_payload(
            tts_settings,
            instructions,
        )
    elif instructions and provider == OPENAI_PROVIDER:
        payload["instructions"] = instructions

    return payload


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


def _request_litellm_audio(payload: dict, endpoint: dict[str, str]) -> requests.Response:
    litellm_speech = _get_litellm_speech_client()
    if litellm_speech is None:
        raise RuntimeError("LiteLLM is not installed. Please install the 'litellm' package.")

    provider = _infer_audio_provider(
        name=endpoint.get("name", ""),
        base_url=endpoint.get("base_url", ""),
        raw_provider=endpoint.get("provider", ""),
    )
    if provider not in SUPPORTED_AUDIO_PROVIDERS:
        raise RuntimeError(f"Provider '{provider}' is not supported for LiteLLM speech routing.")

    model_name = str(payload.get("model") or "").strip() or _provider_default_model(provider)
    voice_name = str(payload.get("voice") or "").strip() or _provider_default_voice(provider)
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


def _request_openai_compatible_audio(text: str, tts_settings: dict) -> requests.Response:
    endpoint, error = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        raise RuntimeError(error)

    if _normalize_custom_adapter(endpoint.get("adapter")) == GENERIC_JSON_ADAPTER:
        request_fields = endpoint.get("request_fields", {})
        if not isinstance(request_fields, dict):
            request_fields = {}
        request_defaults = endpoint.get("request_defaults", {})
        payload = dict(request_defaults) if isinstance(request_defaults, dict) else {}

        text_field = str(request_fields.get("text") or "").strip()
        if not text_field:
            raise RuntimeError(f"Endpoint '{endpoint['name']}' has no configured text request field.")
        payload[text_field] = text

        mapped_values = {
            "model": str(tts_settings.get("xtts_model") or endpoint.get("default_model") or "").strip(),
            "voice": str(tts_settings.get("speaker") or endpoint.get("default_voice") or "").strip(),
            "speed": tts_settings.get("speed"),
            "format": "wav",
        }
        for logical_name, value in mapped_values.items():
            field_name = str(request_fields.get(logical_name) or "").strip()
            if field_name and value not in (None, ""):
                payload[field_name] = value

        speech_path = str(endpoint.get("speech_path") or "").strip()
        if not speech_path:
            raise RuntimeError(f"Endpoint '{endpoint['name']}' has no configured speech route.")
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
    if provider in SUPPORTED_AUDIO_PROVIDERS and not uses_nonstandard_gemini_base:
        try:
            return _request_litellm_audio(payload, endpoint)
        except Exception as e:
            logging.warning(
                "LiteLLM speech call failed for endpoint '%s', falling back to direct HTTP: %s",
                endpoint.get("name", ""),
                e,
            )

    api_key = _resolve_openai_audio_api_key(endpoint)
    last_response = None
    for speech_url in _configured_openai_urls(
        endpoint,
        "speech_path",
        _openai_audio_speech_urls(str(endpoint["base_url"])),
    ):
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

    raise RuntimeError(f"No speech endpoint could be resolved for '{endpoint['name']}'.")


def _decode_audio_response(response: requests.Response) -> AudioSegment:
    content_type = (response.headers.get("Content-Type") or "").lower()
    if not response.content:
        raise RuntimeError("The speech service returned an empty response instead of audio.")
    if "json" in content_type or content_type.startswith("text/"):
        try:
            payload = response.json()
        except ValueError:
            payload = response.text.strip()
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error") or payload.get("message") or payload
        else:
            detail = payload
        raise RuntimeError(f"The speech service returned an error instead of audio: {detail}")
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


def _request_chatterbox_audio(text: str, tts_settings: dict, chatterbox_base_url: str) -> requests.Response:
    normalized_base_url = _normalize_base_url(chatterbox_base_url, CHATTERBOX_API_BASE_URL)
    
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
        "language": tts_settings.get("language") or "en",
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

    raise RuntimeError(f"No Chatterbox speech endpoint could be resolved for '{normalized_base_url}'.")


def _request_kobold_qwen_audio(text: str, tts_settings: dict, kobold_qwen_base_url: str) -> requests.Response:
    normalized_base_url = _normalize_base_url(kobold_qwen_base_url, KOBOLD_QWEN_API_BASE_URL)
    api_key = _resolve_kobold_qwen_api_key()
    model = str(tts_settings.get("xtts_model") or KOBOLD_QWEN_DEFAULT_MODEL).strip()
    if not model:
        model = KOBOLD_QWEN_DEFAULT_MODEL

    voice = str(tts_settings.get("speaker") or KOBOLD_QWEN_DEFAULT_VOICE).strip()
    if not voice:
        voice = KOBOLD_QWEN_DEFAULT_VOICE

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
        "response_format": "wav",
    }

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

    raise RuntimeError(f"No Qwen3 TTS speech endpoint could be resolved for '{normalized_base_url}'.")


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
) -> AudioSegment | None:
    """
    Generates audio from text using the specified TTS service.
    `tts_settings` is a dictionary-like object (e.g., a dataclass).
    """
    service = tts_settings.get("service", "XTTS")
    normalized_silero_base_url = _normalize_base_url(silero_base_url, SILERO_API_BASE_URL)

    for attempt in range(max_attempts):
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
                response = _request_chatterbox_audio(text, tts_settings, chatterbox_base_url)
            elif service == "Qwen3 TTS":
                response = _request_kobold_qwen_audio(text, tts_settings, kobold_qwen_base_url)
            elif service == "Magpie":
                response = _request_magpie_audio(text, tts_settings, magpie_base_url)
            elif service in {
                OPENAI_SERVICE,
                GEMINI_SERVICE,
                LEGACY_GEMINI_SERVICE,
                OPENAI_COMPAT_SERVICE,
                LEGACY_OPENAI_COMPAT_SERVICE,
            }:
                response = _request_openai_compatible_audio(text, tts_settings)
            elif service == "Silero":
                data = {
                    "speaker": tts_settings.get("speaker"),
                    "text": text,
                    "session": "",
                }
                response = requests.post(
                    f"{normalized_silero_base_url}/tts/generate",
                    json=data,
                    timeout=TTS_GENERATION_TIMEOUT_SECONDS,
                )
            else:
                raise ValueError(f"Unsupported TTS service: {service}")

            response.raise_for_status()
            audio = _decode_audio_response(response)
            return audio

        except requests.exceptions.RequestException as e:
            logging.warning("TTS generation attempt %d/%d failed: %s", attempt + 1, max_attempts, e)
            if e.response is not None:
                logging.warning("Server response: %s", e.response.text)
        except Exception as e:
            logging.error("An unexpected error occurred during TTS generation: %s", e)

    logging.error("Failed to generate TTS audio after %d attempts: '%s...'", max_attempts, text[:50])
    return None
