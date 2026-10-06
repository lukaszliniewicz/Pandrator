"""Service catalogue configuration, normalization and request-local snapshots."""

import copy
import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import date

from ..constants import MAGPIE_TTS_MODELS, magpie_voice_catalog
from . import audio_cpp_model_metadata as _audio_cpp_model_metadata_owner
from .tts_openai_http_policy import _normalize_base_url
from .tts_provider_profiles import (
    AUDIO_CPP_MODEL_CATALOG,
    AUDIO_CPP_MODEL_VOICE_MODES,
    AUDIO_CPP_PREBUILT_VOICES,
    AUDIO_CPP_VOICE_DESIGN_MODELS,
    AZURE_SPEECH_ADAPTER,
)

# Kobold Qwen default URLs
KOBOLD_QWEN_API_BASE_URL = "http://127.0.0.1:8042"

LEGACY_GEMINI_SERVICE = "Gemini"


AUDIO_CPP_API_BASE_URL = "http://127.0.0.1:8060"


# XTTS default URLs
XTTS_API_BASE_URL = "http://127.0.0.1:8020"


# VoxCPM default URLs
VOXCPM_API_BASE_URL = "http://127.0.0.1:8020"


# FishS2 default URLs
FISHS2_API_BASE_URL = "http://127.0.0.1:8020"


# Chatterbox default URLs
CHATTERBOX_API_BASE_URL = "http://127.0.0.1:8040"


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


XTTS_DEFAULT_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"


VOXCPM_DEFAULT_MODEL = "openbmb/VoxCPM2"


VOXCPM_MODEL_ALIAS = "voxcpm2"


VOXCPM_DEFAULT_VOICE = "default"


VOXCPM_TTS_MODELS = [VOXCPM_DEFAULT_MODEL, VOXCPM_MODEL_ALIAS]


FISHS2_DEFAULT_MODEL = "fishaudio/s2-pro"


FISHS2_DEFAULT_VOICE = "default"


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


VOXTRAL_DEFAULT_MODEL = "auto"


VOXTRAL_DEFAULT_VOICE = "casual_female"


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


OPENAI_AUDIO_DEFAULT_MODEL = "gpt-4o-mini-tts"


OPENAI_AUDIO_DEFAULT_VOICE = "alloy"


GEMINI_AUDIO_DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"


VERTEX_AUDIO_DEFAULT_MODEL = GEMINI_AUDIO_DEFAULT_MODEL


GEMINI_AUDIO_DEFAULT_VOICE = "Kore"


VERTEX_AUDIO_DEFAULT_LOCATION = "global"


OPENAI_AUDIO_BASE_URL = "https://api.openai.com/v1"


GEMINI_AUDIO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


ELEVENLABS_API_BASE_URL = "https://api.elevenlabs.io"


OPENAI_SERVICE = "OpenAI"


GEMINI_SERVICE = "Google Gemini"


VERTEX_SERVICE = "Google Vertex AI"


ELEVENLABS_SERVICE = "ElevenLabs"


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
    "gemini-3.8-flash-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
]


VERTEX_TTS_MODELS = [
    "gemini-3.8-flash-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-tts",
    "gemini-2.5-pro-tts",
]


ELEVENLABS_TTS_DEFAULT_MODEL = "eleven_multilingual_v2"


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
    "gemini-2.5-flash-tts": "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-tts": "gemini-2.5-pro-preview-tts",
}


VERTEX_MODEL_ALIASES = {
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


def _default_tts_pricing(
    provider: str, *, as_of: date | None = None
) -> dict[str, dict[str, float]]:
    pricing = copy.deepcopy(DEFAULT_TTS_PRICING)
    # Developer API launch rates expire; Cloud's promotion is a billing credit,
    # so its usage estimate retains the regular rate before credits.
    promotional = provider == GEMINI_PROVIDER and (as_of or date.today()) < date(2027, 1, 1)
    pricing["gemini-3.8-flash-tts"] = {
        "input_cost_per_million_tokens": 0.50 if promotional else 1.0,
        "output_cost_per_million_audio_tokens": 9.0 if promotional else 18.0,
        "audio_tokens_per_second": 25.0,
    }
    return pricing


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
                    GENERATION_PROMPT_MODELS_FIELD: list(AUDIO_CPP_VOICE_DESIGN_MODELS),
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
                "pricing": _default_tts_pricing(GEMINI_PROVIDER),
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
                "models": list(VERTEX_TTS_MODELS),
                "default_model": VERTEX_AUDIO_DEFAULT_MODEL,
                "voices": list(GEMINI_TTS_VOICES),
                "default_voice": GEMINI_AUDIO_DEFAULT_VOICE,
                "vertex_project": "",
                "vertex_location": VERTEX_AUDIO_DEFAULT_LOCATION,
                GENERATION_PROMPT_MODELS_FIELD: list(VERTEX_TTS_MODELS),
                PREBUILT_VOICE_PROVIDER_FIELD: True,
                "pricing": _default_tts_pricing(VERTEX_PROVIDER),
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
        models = record.get("models")
        if isinstance(models, list) and default_model not in models:
            models.insert(0, default_model)

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
        voices = record.get("voices")
        if isinstance(voices, list) and default_voice not in voices:
            voices.insert(0, default_voice)

    return record


# Settings keys merged into the provider catalogue by get_service_configs.
# Voice, language, model, and speaker overlays never feed the merge, so a
# cache keyed on exactly these inputs stays correct while distinct voices
# share one catalogue build within a request.
_CATALOGUE_INPUT_KEYS = (
    "openai_audio_endpoints_json",
    "provider_configs",
    "service_configs",
)


def _service_config_cache_key(tts_settings) -> str:
    """Hash the settings inputs merged into the provider catalogue.

    Catalogue construction freezes clock-derived default pricing for a request.
    The caller must supply a request-local cache so each new request constructs
    a fresh catalogue from its settings inputs and current default pricing.
    """
    if isinstance(tts_settings, dict):
        relevant = {key: tts_settings.get(key) for key in _CATALOGUE_INPUT_KEYS}
    else:
        relevant = {
            key: _read_setting(tts_settings, key) for key in _CATALOGUE_INPUT_KEYS
        }
    try:
        normalized = json.dumps(relevant, sort_keys=True, default=str)
    except (TypeError, ValueError):
        normalized = repr(relevant)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _shared_service_configs(tts_settings, _cache=None) -> list[dict[str, object]]:
    cache_key: tuple[str, str] | None = None
    if _cache is not None:
        cache_key = ("service_configs", _service_config_cache_key(tts_settings))
        hit = _cache.get(cache_key)
        if hit is not None:
            return hit
    services = {
        str(item["id"]): copy.deepcopy(item) for item in _default_service_configs()
    }

    legacy_raw_json = str(
        _read_setting(tts_settings, "openai_audio_endpoints_json", "") or ""
    )
    for legacy_record in _legacy_endpoints_to_provider_configs(legacy_raw_json):
        service_id = _normalize_service_id(
            str(legacy_record.get("id") or legacy_record.get("name") or "")
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
    from .speech_capabilities import decorate_service_capabilities

    for service in first_class:
        decorate_service_capabilities(service)
    if _cache is not None and cache_key is not None:
        # Only private lookups may access this shared catalogue. Public
        # results always copy before returning mutable records.
        _cache[cache_key] = first_class
    return first_class


def get_service_configs(tts_settings, _cache=None) -> list[dict[str, object]]:
    services = _shared_service_configs(tts_settings, _cache)
    return copy.deepcopy(services) if _cache is not None else services


def get_service_config(
    tts_settings, service_name_or_id: str, _cache=None
) -> dict[str, object] | None:
    services = _shared_service_configs(tts_settings, _cache)
    service_id = _normalize_service_id(service_name_or_id)
    for service in services:
        if str(service.get("id") or "") == service_id:
            return copy.deepcopy(service) if _cache is not None else service
    return None


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


def _dedupe_ordered(items: Iterable[object]) -> list[str]:
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
    if provider == VERTEX_PROVIDER:
        return VERTEX_AUDIO_DEFAULT_MODEL
    if provider == ELEVENLABS_PROVIDER:
        return ELEVENLABS_TTS_DEFAULT_MODEL
    return OPENAI_AUDIO_DEFAULT_MODEL


def _provider_default_voice(provider: str) -> str:
    if provider in {GEMINI_PROVIDER, VERTEX_PROVIDER}:
        return GEMINI_AUDIO_DEFAULT_VOICE
    if provider == ELEVENLABS_PROVIDER:
        return ""
    return OPENAI_AUDIO_DEFAULT_VOICE


def _provider_model_catalog(provider: str) -> list[str]:
    if provider == GEMINI_PROVIDER:
        return list(GEMINI_TTS_MODELS)
    if provider == VERTEX_PROVIDER:
        return list(VERTEX_TTS_MODELS)
    if provider == ELEVENLABS_PROVIDER:
        return [ELEVENLABS_TTS_DEFAULT_MODEL]
    return list(OPENAI_TTS_MODELS)


def _provider_voice_catalog(provider: str, model_name: str = "") -> list[str]:
    if provider in {GEMINI_PROVIDER, VERTEX_PROVIDER}:
        return list(GEMINI_TTS_VOICES)

    if provider == ELEVENLABS_PROVIDER:
        return []

    normalized_model = _normalize_model_for_provider(model_name, provider).lower()
    if normalized_model in {"tts-1", "tts-1-hd"}:
        return list(OPENAI_TTS_CLASSIC_VOICES)
    return list(OPENAI_TTS_VOICES)


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
    normalized_provider = str(provider or "").strip().lower()
    aliases = {
        GEMINI_PROVIDER: GEMINI_MODEL_ALIASES,
        VERTEX_PROVIDER: VERTEX_MODEL_ALIASES,
    }.get(normalized_provider)
    if aliases:
        alias = aliases.get(normalized.lower())
        if alias:
            return alias
    return normalized


def _normalize_voice_for_provider(voice_name: str, provider: str) -> str:
    normalized = str(voice_name or "").strip()
    if not normalized:
        return ""

    voice_map = {voice.lower(): voice for voice in _provider_voice_catalog(provider)}
    return voice_map.get(normalized.lower(), normalized)


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


def _audio_cpp_model_metadata(model: str, endpoint: dict) -> dict[str, object]:
    return _audio_cpp_model_metadata_owner._audio_cpp_model_metadata(
        model,
        endpoint,
        model_catalog=AUDIO_CPP_MODEL_CATALOG,
    )


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

def _endpoint_string_list(record: Mapping[str, object], key: str) -> list[str]:
    """Return the string list carried by a heterogeneous provider record."""
    values = record.get(key)
    return [str(value) for value in values] if isinstance(values, list) else []

def _service_audio_endpoint(tts_settings, provider: str, _cache=None) -> dict[str, object]:
    normalized_provider = (
        AUDIO_CPP_ADAPTER
        if str(provider or "").strip().lower().replace("-", "_")
        in {"audio_cpp", "audiocpp"}
        else _normalize_audio_provider(provider)
    )
    service = get_service_config(tts_settings, normalized_provider, _cache)
    if service is None:
        service = get_service_config({}, normalized_provider, _cache) or {}
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
        "models": _endpoint_string_list(service, "models"),
        "voices": _endpoint_string_list(service, "voices"),
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
            "models": _endpoint_string_list(provider_record, "models"),
            "voices": _endpoint_string_list(provider_record, "voices"),
            "adapter": adapter,
            "profile_id": profile_id,
            "speech_path": str(provider_record.get("speech_path") or ""),
            "models_path": str(provider_record.get("models_path") or ""),
            "voices_path": str(provider_record.get("voices_path") or ""),
            "request_fields": dict(request_fields) if isinstance(
                request_fields := provider_record.get("request_fields"), dict
            ) else {},
            "request_defaults": dict(request_defaults) if isinstance(
                request_defaults := provider_record.get("request_defaults"), dict
            ) else {},
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

def resolve_openai_audio_endpoint(
    tts_settings: dict,
    _cache=None,
) -> tuple[dict[str, object] | None, str]:
    """Resolves the selected custom audio endpoint from settings."""
    endpoints = _parse_openai_audio_endpoints(tts_settings)

    service_provider = _provider_for_tts_service(tts_settings.get("service"))
    if service_provider:
        return _service_audio_endpoint(tts_settings, service_provider, _cache), ""

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
