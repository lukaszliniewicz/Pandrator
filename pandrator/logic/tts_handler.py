import base64
import binascii
import copy
import io
import json
import logging
import math
import os
import re
import time
import wave
from collections.abc import Callable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from contextlib import ExitStack, contextmanager
from threading import Event, Lock, RLock, Thread
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlunparse
from xml.sax.saxutils import escape as escape_xml

import requests
from pydub import AudioSegment
from requests.structures import CaseInsensitiveDict

from ..constants import (
    KOKORO_NAMED_VOICE_META as KOKORO_NAMED_VOICE_META,
)
from ..constants import (
    KOKORO_OPENAI_ALIAS_VOICES as KOKORO_OPENAI_ALIAS_VOICES,
)
from ..constants import (
    KOKORO_PREFIX_LANGUAGE_CODES as KOKORO_PREFIX_LANGUAGE_CODES,
)
from ..constants import (
    MAGPIE_TTS_MODELS as MAGPIE_TTS_MODELS,
)
from ..constants import (
    SILERO_LANGUAGES as SILERO_LANGUAGES,
)
from ..constants import (
    magpie_voice_catalog as magpie_voice_catalog,
)
from . import audio_cpp_speech_payload as _audio_cpp_speech_payload
from . import elevenlabs_catalogue_http as _elevenlabs_catalogue_http
from . import google_tts_audio as _google_tts_audio
from . import kobold_qwen_http as _kobold_qwen_http
from . import kobold_qwen_stream as _kobold_qwen_stream
from . import native_speech_http as _native_speech_http
from . import silero_catalogue_http as _silero_catalogue_http
from . import voxcpm_speech_http as _voxcpm_speech_http
from . import xtts_catalogue_http as _xtts_catalogue_http
from .audio_cpp_execution import local_tts_audio_cpp_guard
from .audio_cpp_parameters import validate_audio_cpp_model_options
from .audio_cpp_speech_payload import (
    _AUDIO_CPP_FIRERED_DIALECTS as _AUDIO_CPP_FIRERED_DIALECTS,
)
from .audio_cpp_speech_payload import (
    _AUDIO_CPP_FIRERED_LANGUAGE_NAMES as _AUDIO_CPP_FIRERED_LANGUAGE_NAMES,
)
from .audio_cpp_speech_payload import (
    _AUDIO_CPP_QWEN_LANGUAGE_NAMES as _AUDIO_CPP_QWEN_LANGUAGE_NAMES,
)
from .elevenlabs_speech_contracts import (
    _validated_elevenlabs_voice_settings as _validated_elevenlabs_voice_settings,
)
from .kobold_qwen_contracts import (
    KoboldQwenBatchAudioEvent,
    KoboldQwenBatchCapabilities,
)
from .kobold_qwen_contracts import (
    KoboldQwenBatchMessage as KoboldQwenBatchMessage,
)
from .kobold_qwen_http import (
    KOBOLD_QWEN_API_BASE_URL as KOBOLD_QWEN_API_BASE_URL,
)
from .kobold_qwen_http import (
    KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS as KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS,
)
from .retry_utils import (
    retry_after_seconds,
    retry_delay_seconds,
    retryable_error,
    status_code_from_error,
    wait_for_retry,
)
from .tts_endpoint_transport import (
    _audio_cpp_endpoint_locks as _audio_cpp_endpoint_locks,
)
from .tts_endpoint_transport import (
    _audio_cpp_endpoint_locks_guard as _audio_cpp_endpoint_locks_guard,
)
from .tts_endpoint_transport import (
    audio_cpp_endpoint_key as _audio_cpp_endpoint_key,
)
from .tts_endpoint_transport import (
    audio_cpp_endpoint_lock_for_key,
    endpoint_lock_guard,
    endpoint_lock_key,
    endpoint_lock_urls,
)
from .tts_openai_http_policy import (
    OPENAI_CANDIDATE_FALLBACK_STATUS_CODES as OPENAI_CANDIDATE_FALLBACK_STATUS_CODES,
)
from .tts_openai_http_policy import (
    XTTS_OPENAI_PLACEHOLDER_API_KEY as XTTS_OPENAI_PLACEHOLDER_API_KEY,
)
from .tts_openai_http_policy import (
    _normalize_base_url as _normalize_base_url,
)
from .tts_openai_http_policy import (
    _openai_audio_speech_batch_urls as _openai_audio_speech_batch_urls,
)
from .tts_openai_http_policy import (
    _openai_audio_speech_urls as _openai_audio_speech_urls,
)
from .tts_openai_http_policy import (
    _openai_audio_voices_urls as _openai_audio_voices_urls,
)
from .tts_openai_http_policy import (
    _openai_auth_headers as _openai_auth_headers,
)
from .tts_openai_http_policy import (
    _openai_capabilities_urls as _openai_capabilities_urls,
)
from .tts_openai_http_policy import (
    _openai_files_urls as _openai_files_urls,
)
from .tts_openai_http_policy import (
    _openai_models_urls as _openai_models_urls,
)
from .tts_openai_http_policy import (
    _openai_url_candidates as _openai_url_candidates,
)
from .tts_openai_http_policy import (
    _openai_voices_urls as _openai_voices_urls,
)
from .tts_openai_http_policy import (
    _should_try_next_openai_candidate as _should_try_next_openai_candidate,
)
from .tts_provider_health_http import probe_get_urls as _probe_get_urls
from .tts_provider_profiles import (
    AUDIO_CPP_MODEL_CATALOG as AUDIO_CPP_MODEL_CATALOG,
)
from .tts_provider_profiles import (
    AUDIO_CPP_MODEL_VOICE_MODES as AUDIO_CPP_MODEL_VOICE_MODES,
)
from .tts_provider_profiles import (
    AUDIO_CPP_PREBUILT_VOICES as AUDIO_CPP_PREBUILT_VOICES,
)
from .tts_provider_profiles import (
    AUDIO_CPP_VOICE_DESIGN_MODELS as AUDIO_CPP_VOICE_DESIGN_MODELS,
)
from .tts_provider_profiles import (
    AZURE_SPEECH_ADAPTER as AZURE_SPEECH_ADAPTER,
)
from .tts_provider_profiles import (
    AZURE_SPEECH_MODELS as AZURE_SPEECH_MODELS,
)
from .tts_provider_profiles import (
    AZURE_SPEECH_OUTPUT_FORMAT as AZURE_SPEECH_OUTPUT_FORMAT,
)
from .tts_provider_profiles import (
    AZURE_SPEECH_VOICE_CATALOGUES as AZURE_SPEECH_VOICE_CATALOGUES,
)
from .tts_service_catalogue import (
    _CATALOGUE_INPUT_KEYS as _CATALOGUE_INPUT_KEYS,
)
from .tts_service_catalogue import (
    AUDIO_CPP_ADAPTER as AUDIO_CPP_ADAPTER,
)
from .tts_service_catalogue import (
    AUDIO_CPP_API_BASE_URL as AUDIO_CPP_API_BASE_URL,
)
from .tts_service_catalogue import (
    AZURE_SPEECH_PROVIDER as AZURE_SPEECH_PROVIDER,
)
from .tts_service_catalogue import (
    CHATTERBOX_API_BASE_URL as CHATTERBOX_API_BASE_URL,
)
from .tts_service_catalogue import (
    CHATTERBOX_DEFAULT_MODEL as CHATTERBOX_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    CHATTERBOX_TTS_MODELS as CHATTERBOX_TTS_MODELS,
)
from .tts_service_catalogue import (
    DEFAULT_TTS_PRICING as DEFAULT_TTS_PRICING,
)
from .tts_service_catalogue import (
    ELEVENLABS_API_BASE_URL as ELEVENLABS_API_BASE_URL,
)
from .tts_service_catalogue import (
    ELEVENLABS_NATIVE_ADAPTER as ELEVENLABS_NATIVE_ADAPTER,
)
from .tts_service_catalogue import (
    ELEVENLABS_PROVIDER as ELEVENLABS_PROVIDER,
)
from .tts_service_catalogue import (
    ELEVENLABS_SERVICE as ELEVENLABS_SERVICE,
)
from .tts_service_catalogue import (
    ELEVENLABS_TTS_DEFAULT_MODEL as ELEVENLABS_TTS_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    FIRST_CLASS_SERVICE_IDS as FIRST_CLASS_SERVICE_IDS,
)
from .tts_service_catalogue import (
    FIRST_CLASS_SERVICE_NAMES as FIRST_CLASS_SERVICE_NAMES,
)
from .tts_service_catalogue import (
    FIRST_CLASS_SERVICE_ORDER as FIRST_CLASS_SERVICE_ORDER,
)
from .tts_service_catalogue import (
    FISHS2_API_BASE_URL as FISHS2_API_BASE_URL,
)
from .tts_service_catalogue import (
    FISHS2_DEFAULT_MODEL as FISHS2_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    FISHS2_DEFAULT_VOICE as FISHS2_DEFAULT_VOICE,
)
from .tts_service_catalogue import (
    GEMINI_AUDIO_BASE_URL as GEMINI_AUDIO_BASE_URL,
)
from .tts_service_catalogue import (
    GEMINI_AUDIO_DEFAULT_MODEL as GEMINI_AUDIO_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    GEMINI_AUDIO_DEFAULT_VOICE as GEMINI_AUDIO_DEFAULT_VOICE,
)
from .tts_service_catalogue import (
    GEMINI_MODEL_ALIASES as GEMINI_MODEL_ALIASES,
)
from .tts_service_catalogue import (
    GEMINI_PROVIDER as GEMINI_PROVIDER,
)
from .tts_service_catalogue import (
    GEMINI_SERVICE as GEMINI_SERVICE,
)
from .tts_service_catalogue import (
    GEMINI_TTS_MODELS as GEMINI_TTS_MODELS,
)
from .tts_service_catalogue import (
    GEMINI_TTS_VOICES as GEMINI_TTS_VOICES,
)
from .tts_service_catalogue import (
    GENERATION_PROMPT_MODELS_FIELD as GENERATION_PROMPT_MODELS_FIELD,
)
from .tts_service_catalogue import (
    GENERIC_JSON_ADAPTER as GENERIC_JSON_ADAPTER,
)
from .tts_service_catalogue import (
    KOBOLD_QWEN_DEFAULT_MODEL as KOBOLD_QWEN_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    KOBOLD_QWEN_DEFAULT_VOICE as KOBOLD_QWEN_DEFAULT_VOICE,
)
from .tts_service_catalogue import (
    KOBOLD_QWEN_GENERATION_PROMPT_MODELS as KOBOLD_QWEN_GENERATION_PROMPT_MODELS,
)
from .tts_service_catalogue import (
    KOBOLD_QWEN_SAMPLE_VOICE as KOBOLD_QWEN_SAMPLE_VOICE,
)
from .tts_service_catalogue import (
    KOBOLD_QWEN_TTS_MODELS as KOBOLD_QWEN_TTS_MODELS,
)
from .tts_service_catalogue import (
    KOBOLD_QWEN_TTS_VOICES as KOBOLD_QWEN_TTS_VOICES,
)
from .tts_service_catalogue import (
    KOKORO_API_BASE_URL as KOKORO_API_BASE_URL,
)
from .tts_service_catalogue import (
    KOKORO_DEFAULT_MODEL as KOKORO_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    KOKORO_DEFAULT_VOICE as KOKORO_DEFAULT_VOICE,
)
from .tts_service_catalogue import (
    KOKORO_TTS_MODELS as KOKORO_TTS_MODELS,
)
from .tts_service_catalogue import (
    KOKORO_TTS_VOICES as KOKORO_TTS_VOICES,
)
from .tts_service_catalogue import (
    LEGACY_GEMINI_SERVICE as LEGACY_GEMINI_SERVICE,
)
from .tts_service_catalogue import (
    MAGPIE_API_BASE_URL as MAGPIE_API_BASE_URL,
)
from .tts_service_catalogue import (
    OPENAI_AUDIO_BASE_URL as OPENAI_AUDIO_BASE_URL,
)
from .tts_service_catalogue import (
    OPENAI_AUDIO_DEFAULT_MODEL as OPENAI_AUDIO_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    OPENAI_AUDIO_DEFAULT_VOICE as OPENAI_AUDIO_DEFAULT_VOICE,
)
from .tts_service_catalogue import (
    OPENAI_COMPAT_ADAPTER as OPENAI_COMPAT_ADAPTER,
)
from .tts_service_catalogue import (
    OPENAI_GENERATION_PROMPT_MODELS as OPENAI_GENERATION_PROMPT_MODELS,
)
from .tts_service_catalogue import (
    OPENAI_PROVIDER as OPENAI_PROVIDER,
)
from .tts_service_catalogue import (
    OPENAI_SERVICE as OPENAI_SERVICE,
)
from .tts_service_catalogue import (
    OPENAI_TTS_CLASSIC_VOICES as OPENAI_TTS_CLASSIC_VOICES,
)
from .tts_service_catalogue import (
    OPENAI_TTS_MODELS as OPENAI_TTS_MODELS,
)
from .tts_service_catalogue import (
    OPENAI_TTS_VOICES as OPENAI_TTS_VOICES,
)
from .tts_service_catalogue import (
    PREBUILT_VOICE_PROVIDER_FIELD as PREBUILT_VOICE_PROVIDER_FIELD,
)
from .tts_service_catalogue import (
    SERVICE_ID_ALIASES as SERVICE_ID_ALIASES,
)
from .tts_service_catalogue import (
    SILERO_API_BASE_URL as SILERO_API_BASE_URL,
)
from .tts_service_catalogue import (
    SILERO_DEFAULT_MODEL as SILERO_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    SILERO_TTS_MODELS as SILERO_TTS_MODELS,
)
from .tts_service_catalogue import (
    SUPPORTED_AUDIO_PROVIDERS as SUPPORTED_AUDIO_PROVIDERS,
)
from .tts_service_catalogue import (
    VERTEX_AUDIO_DEFAULT_LOCATION as VERTEX_AUDIO_DEFAULT_LOCATION,
)
from .tts_service_catalogue import (
    VERTEX_AUDIO_DEFAULT_MODEL as VERTEX_AUDIO_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    VERTEX_MODEL_ALIASES as VERTEX_MODEL_ALIASES,
)
from .tts_service_catalogue import (
    VERTEX_PROVIDER as VERTEX_PROVIDER,
)
from .tts_service_catalogue import (
    VERTEX_SERVICE as VERTEX_SERVICE,
)
from .tts_service_catalogue import (
    VERTEX_TTS_MODELS as VERTEX_TTS_MODELS,
)
from .tts_service_catalogue import (
    VOICE_CLONING_SERVICE_IDS as VOICE_CLONING_SERVICE_IDS,
)
from .tts_service_catalogue import (
    VOICE_DELETION_SERVICE_IDS as VOICE_DELETION_SERVICE_IDS,
)
from .tts_service_catalogue import (
    VOICE_REFERENCE_TEXT_MODES as VOICE_REFERENCE_TEXT_MODES,
)
from .tts_service_catalogue import (
    VOXCPM_API_BASE_URL as VOXCPM_API_BASE_URL,
)
from .tts_service_catalogue import (
    VOXCPM_DEFAULT_MODEL as VOXCPM_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    VOXCPM_DEFAULT_VOICE as VOXCPM_DEFAULT_VOICE,
)
from .tts_service_catalogue import (
    VOXCPM_MODEL_ALIAS as VOXCPM_MODEL_ALIAS,
)
from .tts_service_catalogue import (
    VOXCPM_TTS_MODELS as VOXCPM_TTS_MODELS,
)
from .tts_service_catalogue import (
    VOXTRAL_API_BASE_URL as VOXTRAL_API_BASE_URL,
)
from .tts_service_catalogue import (
    VOXTRAL_DEFAULT_MODEL as VOXTRAL_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    VOXTRAL_DEFAULT_VOICE as VOXTRAL_DEFAULT_VOICE,
)
from .tts_service_catalogue import (
    VOXTRAL_TTS_MODELS as VOXTRAL_TTS_MODELS,
)
from .tts_service_catalogue import (
    XTTS_API_BASE_URL as XTTS_API_BASE_URL,
)
from .tts_service_catalogue import (
    XTTS_DEFAULT_MODEL as XTTS_DEFAULT_MODEL,
)
from .tts_service_catalogue import (
    _audio_cpp_model_metadata as _audio_cpp_model_metadata,
)
from .tts_service_catalogue import (
    _coerce_bool as _coerce_bool,
)
from .tts_service_catalogue import (
    _dedupe_ordered as _dedupe_ordered,
)
from .tts_service_catalogue import (
    _default_service_configs as _default_service_configs,
)
from .tts_service_catalogue import (
    _default_tts_pricing as _default_tts_pricing,
)
from .tts_service_catalogue import (
    _endpoint_string_list as _endpoint_string_list,
)
from .tts_service_catalogue import (
    _infer_audio_provider as _infer_audio_provider,
)
from .tts_service_catalogue import (
    _legacy_endpoints_to_provider_configs as _legacy_endpoints_to_provider_configs,
)
from .tts_service_catalogue import (
    _merge_service_config as _merge_service_config,
)
from .tts_service_catalogue import (
    _normalize_adapter_config as _normalize_adapter_config,
)
from .tts_service_catalogue import (
    _normalize_audio_provider as _normalize_audio_provider,
)
from .tts_service_catalogue import (
    _normalize_custom_adapter as _normalize_custom_adapter,
)
from .tts_service_catalogue import (
    _normalize_model_for_provider as _normalize_model_for_provider,
)
from .tts_service_catalogue import (
    _normalize_provider_id as _normalize_provider_id,
)
from .tts_service_catalogue import (
    _normalize_service_id as _normalize_service_id,
)
from .tts_service_catalogue import (
    _normalize_voice_for_provider as _normalize_voice_for_provider,
)
from .tts_service_catalogue import (
    _parse_model_list as _parse_model_list,
)
from .tts_service_catalogue import (
    _parse_openai_audio_endpoints as _parse_openai_audio_endpoints,
)
from .tts_service_catalogue import (
    _parse_voice_list as _parse_voice_list,
)
from .tts_service_catalogue import (
    _provider_default_model as _provider_default_model,
)
from .tts_service_catalogue import (
    _provider_default_voice as _provider_default_voice,
)
from .tts_service_catalogue import (
    _provider_for_tts_service as _provider_for_tts_service,
)
from .tts_service_catalogue import (
    _provider_model_catalog as _provider_model_catalog,
)
from .tts_service_catalogue import (
    _provider_voice_catalog as _provider_voice_catalog,
)
from .tts_service_catalogue import (
    _read_setting as _read_setting,
)
from .tts_service_catalogue import (
    _service_audio_endpoint as _service_audio_endpoint,
)
from .tts_service_catalogue import (
    _service_config_cache_key as _service_config_cache_key,
)
from .tts_service_catalogue import (
    _shared_service_configs as _shared_service_configs,
)
from .tts_service_catalogue import (
    _strip_provider_prefix as _strip_provider_prefix,
)
from .tts_service_catalogue import (
    get_first_class_service_name as get_first_class_service_name,
)
from .tts_service_catalogue import (
    get_provider_configs as get_provider_configs,
)
from .tts_service_catalogue import (
    get_service_config as get_service_config,
)
from .tts_service_catalogue import (
    get_service_configs as get_service_configs,
)
from .tts_service_catalogue import (
    resolve_openai_audio_endpoint as resolve_openai_audio_endpoint,
)
from .tts_service_catalogue import (
    validate_openai_audio_endpoints_json as validate_openai_audio_endpoints_json,
)
from .tts_usage import TtsUsageEstimate
from .tts_voice_delete_http import (
    _delete_speaker_voice_openai_compatible as _delete_speaker_voice_http,
)
from .tts_voice_delete_http import (
    _remote_voice_exists as _remote_voice_exists_http,
)
from .tts_voice_delete_http import (
    _voice_catalog_can_confirm_absence as _voice_catalog_can_confirm_absence,
)
from .tts_voice_upload_http import (
    _extract_uploaded_identifier as _extract_uploaded_identifier,
)
from .tts_voice_upload_http import (
    _normalize_upload_wav_paths as _normalize_upload_wav_paths,
)
from .tts_voice_upload_http import (
    _upload_speaker_voice_openai_compatible as _upload_speaker_voice_openai_compatible,
)
from .voxcpm_speech_http import (
    _is_voxcpm_prompt_pairing_error as _is_voxcpm_prompt_pairing_error,
)

_litellm_speech = None
_litellm_speech_import_attempted = False
_litellm_speech_import_error: BaseException | None = None
_litellm_speech_import_lock = Lock()

AUDIO_CPP_MAX_REFERENCE_BYTES = 5 * 1024 * 1024


class TtsGenerationError(RuntimeError):
    """A synthesis failure whose provider explanation can reach the job UI."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _tts_error_detail(error: Exception) -> str:
    """Extract a bounded explanation without dumping the request or response JSON."""
    response = getattr(error, "response", None)
    detail: Any = ""
    if response is not None:
        try:
            body = response.json()
        except (ValueError, requests.exceptions.RequestException):
            body = None
        if isinstance(body, dict):
            provider_error = body.get("error")
            if isinstance(provider_error, dict):
                detail = provider_error.get("message")
            elif isinstance(provider_error, str):
                detail = provider_error
            if not isinstance(detail, str) or not detail.strip():
                detail = body.get("detail") or body.get("message")
        if not isinstance(detail, str) or not detail.strip():
            raw = getattr(response, "text", "")
            # Proxy HTML and structured responses without an explanation aren't
            # useful diagnostics and can contain unrelated data.
            detail = (
                raw
                if isinstance(raw, str) and not raw.lstrip().startswith(("<", "{", "["))
                else ""
            )
    if not detail:
        detail = str(error)
    return " ".join(detail.split())[:1000]


def _audio_cpp_permanent_error(detail: str) -> bool:
    """audio.cpp 0.7.2 reports these request-contract failures as HTTP 500."""
    return bool(
        re.search(r"unsupported .+ language tag:", detail, re.IGNORECASE)
        or re.search(r"unknown .+ request option:", detail, re.IGNORECASE)
        or "requires speaker reference audio, not a cached voice id" in detail
    )


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
TTS_GENERATION_TIMEOUT_SECONDS = 300

XTTS_UPLOAD_FILE_PURPOSE = "user_data"
XTTS_DISCOVERABLE_FILE_PURPOSES = ("user_data", "assistants")
VOXCPM_DEFAULT_CFG_VALUE = 1.5
VOXCPM_DEFAULT_INFERENCE_TIMESTEPS = 15
VOXCPM_DEFAULT_NORMALIZE = False
VOXCPM_DEFAULT_DENOISE = False
VOXCPM_DEFAULT_RETRY_BADCASE = True
VOXCPM_DEFAULT_RETRY_BADCASE_MAX_TIMES = 3
VOXCPM_DEFAULT_RETRY_BADCASE_RATIO_THRESHOLD = 6.0
VOXCPM_DEFAULT_MIN_LEN = 2
VOXCPM_DEFAULT_MAX_LEN = 4096
VOXCPM_UPLOAD_FILE_PURPOSE = "user_data"
FISHS2_MODEL_ALIASES = [
    "fishs2",
    "fish-s2",
    "s2-pro",
]
FISHS2_UPLOAD_FILE_PURPOSE = "user_data"
FISHS2_DEFAULT_TEMPERATURE = 0.7
FISHS2_DEFAULT_TOP_P = 0.7
FISHS2_DEFAULT_CHUNK_LENGTH = 200
FISHS2_DEFAULT_LATENCY = "balanced"
FISHS2_DEFAULT_NORMALIZE = True
FISHS2_DEFAULT_PROSODY_VOLUME = 0.0
FISHS2_DEFAULT_NORMALIZE_LOUDNESS = True
VOXTRAL_INSTRUCTIONS_PREFIX = "voxtral_options:"


def normalize_kokoro_language_code(language_value: str | None) -> str:
    from .dubbing.languages import normalize_language_code
    normalized = str(language_value or "").strip().lower().replace("_", "-")
    canonical = normalize_language_code(normalized, default="")
    if canonical.split("-")[0] in {"ja", "zh", "ko"}:
        normalized = "zh-cn" if canonical.startswith("zh") else canonical
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



OPENAI_COMPAT_SERVICE = "Custom"
LEGACY_OPENAI_COMPAT_SERVICE = "OpenAI-Compatible"






ELEVENLABS_TTS_OUTPUT_FORMAT = "mp3_44100_128"
# ElevenLabs documents ``language_code`` as unsupported for this model. Keep
# this an explicit exception instead of maintaining a closed allow-list: the
# catalogue can gain models without making an otherwise valid synthesis fail.
ELEVENLABS_MODELS_WITHOUT_LANGUAGE_CODE = frozenset({"eleven_multilingual_v2"})


class ElevenLabsCatalogError(RuntimeError):
    """Safe, status-aware failure from an ElevenLabs catalogue endpoint."""

    def __init__(self, operation: str, status_code: int = 0, *, incomplete: bool = False):
        self.operation = operation
        self.status_code = int(status_code or 0)
        self.incomplete = incomplete
        if incomplete:
            message = "ElevenLabs voice pagination did not finish; the catalogue may be incomplete."
        elif self.status_code in {401, 403}:
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










SUPPORTED_CUSTOM_TTS_ADAPTERS = {
    OPENAI_COMPAT_ADAPTER,
    AUDIO_CPP_ADAPTER,
    GENERIC_JSON_ADAPTER,
    ELEVENLABS_NATIVE_ADAPTER,
    AZURE_SPEECH_ADAPTER,
}






























def estimate_tts_usage(
    text: str, duration_ms: int, tts_settings
) -> TtsUsageEstimate | None:
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
    service_provider = str(service.get("provider") or service.get("id") or "")
    model_pricing = _model_pricing_for_provider(
        pricing,
        model,
        service_provider,
    )
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


def _audio_cpp_endpoint_lock_key(base_url: str) -> str:
    return endpoint_lock_key(base_url, key_for=_audio_cpp_endpoint_key)


def _audio_cpp_endpoint_lock_urls(endpoint: Mapping[str, object]) -> list[str]:
    base_url = str(endpoint.get("base_url") or "")
    speech_url = _configured_endpoint_url(
        base_url, str(endpoint.get("speech_path") or "/v1/audio/speech")
    )
    return endpoint_lock_urls(base_url, speech_url, lock_key_for=_audio_cpp_endpoint_lock_key)


def _audio_cpp_endpoint_lock_for(base_url: str) -> RLock:
    return audio_cpp_endpoint_lock_for_key(_audio_cpp_endpoint_lock_key(base_url))


@contextmanager
def _audio_cpp_endpoint_guard(base_url: str, cancel_event: Event | None = None):
    with endpoint_lock_guard(_audio_cpp_endpoint_lock_for(base_url), cancel_event):
        yield


@contextmanager
def audio_cpp_endpoint_lock(tts_settings: dict, cancel_event=None):
    """Serialize synthesis requests to one audio.cpp endpoint across jobs."""
    endpoint, _error = resolve_openai_audio_endpoint(tts_settings)
    if (
        endpoint is None
        or _normalize_custom_adapter(endpoint.get("adapter")) != AUDIO_CPP_ADAPTER
    ):
        yield
        return
    urls = _audio_cpp_endpoint_lock_urls(endpoint)
    with ExitStack() as stack:
        for url in urls:
            stack.enter_context(local_tts_audio_cpp_guard(url, cancel_event))
        for url in urls:
            stack.enter_context(_audio_cpp_endpoint_guard(url, cancel_event))
        yield




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
        value = tts_settings.get(setting_name)
        if coercer is _coerce_bool and isinstance(fallback, bool):
            overrides[output_name] = _coerce_bool(value, fallback)
        elif coercer is _coerce_int and isinstance(fallback, int):
            overrides[output_name] = _coerce_int(value, fallback)
        elif coercer is _coerce_float and isinstance(fallback, float):
            overrides[output_name] = _coerce_float(value, fallback)
        else:
            raise TypeError(f"Invalid XTTS override specification: {output_name}")

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


def _looks_like_xtts_endpoint(endpoint: Mapping[str, object] | None) -> bool:
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


def _is_xtts_target(model_name: str, endpoint: Mapping[str, object] | None = None) -> bool:
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


























def _model_pricing_for_provider(
    pricing: dict, model_name: str, provider: str
) -> dict:
    """Find the configured price while accepting saved model-ID aliases."""
    if not isinstance(pricing, dict):
        return {}
    model_id = str(model_name or "").strip()
    normalized = _normalize_model_for_provider(model_id, provider)
    candidates = [model_id, normalized]
    candidates.extend(
        key
        for key in pricing
        if isinstance(key, str)
        and key not in candidates
        and _normalize_model_for_provider(key, provider) == normalized
    )
    for candidate in candidates:
        model_pricing = pricing.get(candidate)
        if isinstance(model_pricing, dict):
            return model_pricing
    return {}




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


def _openai_voice_catalog_urls(base_url: str) -> list[str]:
    return _dedupe_ordered(
        _openai_audio_voices_urls(base_url) + _openai_voices_urls(base_url)
    )


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
    allowed_purposes: AbstractSet[str] | None = None,
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






def list_openai_audio_endpoint_names(tts_settings: dict) -> list[str]:
    """Lists configured custom audio endpoint names."""
    service_provider = _provider_for_tts_service(tts_settings.get("service"))
    if service_provider:
        return [str(_service_audio_endpoint(tts_settings, service_provider)["name"])]

    return sorted(_parse_openai_audio_endpoints(tts_settings).keys())




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


def _resolve_openai_audio_api_key(endpoint: Mapping[str, object]) -> str:
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
    base_url, model, voice, api_key = _validate_azure_speech_request(endpoint, tts_settings)
    request_defaults = endpoint.get("request_defaults")
    default_output_format = (
        request_defaults.get("output_format") if isinstance(request_defaults, dict) else ""
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
        parsed = urlparse(url)
        hostname = str(parsed.hostname or "").lower()
        valid_url = parsed.scheme.lower() == "https" and bool(hostname) and "your" not in hostname
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError(
            "Azure Speech requires a non-placeholder HTTPS speech URL for your Speech resource."
        )
    return _native_speech_http.post_native_speech(
        url,
        request_label="Azure Speech",
        request_options=lambda: {
            "headers": {
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": output_format,
                "Ocp-Apim-Subscription-Key": api_key,
            },
            "data": ssml,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
    )


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
    _timing_sink: dict | None = None,
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
    if not model_id:
        raise ValueError("Select an ElevenLabs model before generating speech.")
    request_defaults = selected_endpoint.get("request_defaults")
    default_output_format = (
        request_defaults.get("output_format") if isinstance(request_defaults, dict) else ""
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
    from .speech_performance import compile_for_provider

    compiled = compile_for_provider(
        str(text),
        {**tts_settings, "xtts_model": model_id, "elevenlabs_model": model_id},
        selected_endpoint,
    )
    payload = {"text": compiled.input, "model_id": model_id}
    payload.update(compiled.request_options)
    language_code = normalize_elevenlabs_language_code(tts_settings.get("language"))
    if language_code and model_id.lower() not in ELEVENLABS_MODELS_WITHOUT_LANGUAGE_CODE:
        payload["language_code"] = language_code
    base_url = _elevenlabs_base_url(
        str(
            selected_endpoint.get("base_url")
            or selected_endpoint.get("api_base")
            or ELEVENLABS_API_BASE_URL
        )
    )
    url = f"{base_url}/v1/text-to-speech/{quote(voice_id, safe='')}"
    if _timing_sink is not None:
        url += "/with-timestamps"
        _timing_sink["provider_text"] = compiled.input
        _timing_sink["output_format"] = output_format
    return _native_speech_http.post_native_speech(
        url,
        request_label="ElevenLabs speech",
        request_options=lambda: {
            "headers": _elevenlabs_auth_headers(api_key, audio=_timing_sink is None),
            "params": {"output_format": output_format},
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
    )


def get_elevenlabs_model_catalog(
    base_url: str = ELEVENLABS_API_BASE_URL,
    *,
    api_key: str = "",
    strict: bool = False,
) -> list[dict[str, object]]:
    """Fetch the currently available TTS models and authoritative languages."""
    return _elevenlabs_catalogue_http.get_elevenlabs_model_catalog(
        base_url,
        api_key=api_key,
        strict=strict,
        normalize_base_url=lambda value: _elevenlabs_base_url(value),
        auth_headers=lambda key: _elevenlabs_auth_headers(key),
        catalog_status=lambda error: _elevenlabs_catalog_status(error),
        error_factory=lambda: ElevenLabsCatalogError,
    )


def get_elevenlabs_voice_catalog(
    base_url: str = ELEVENLABS_API_BASE_URL,
    *,
    api_key: str = "",
    strict: bool = False,
) -> list[dict[str, object]]:
    """Fetch voice IDs and metadata from ElevenLabs' current v2 voices API."""
    return _elevenlabs_catalogue_http.get_elevenlabs_voice_catalog(
        base_url,
        api_key=api_key,
        strict=strict,
        normalize_base_url=lambda value: _elevenlabs_base_url(value),
        auth_headers=lambda key: _elevenlabs_auth_headers(key),
        catalog_status=lambda error: _elevenlabs_catalog_status(error),
        error_factory=lambda: ElevenLabsCatalogError,
    )


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

    return _probe_get_urls(
        _dedupe_ordered(probe_urls),
        headers=lambda: _openai_auth_headers(api_key),
        timeout=4,
        accepts_status=lambda status: (
            not _should_try_next_openai_candidate(status) and status < 400
        ),
    )


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

    return _probe_get_urls(
        _dedupe_ordered(probe_urls),
        headers=lambda: _openai_auth_headers(api_key),
        timeout=4,
        accepts_status=lambda status: (
            not _should_try_next_openai_candidate(status) and status < 400
        ),
    )


def check_chatterbox_connection(base_url: str = CHATTERBOX_API_BASE_URL) -> bool:
    """Checks if the Chatterbox server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, CHATTERBOX_API_BASE_URL)
    probe_urls = [
        f"{normalized_base_url}/health",
        *_openai_models_urls(normalized_base_url),
        *_openai_voice_catalog_urls(normalized_base_url),
        *_openai_files_urls(normalized_base_url),
    ]

    return _probe_get_urls(
        _dedupe_ordered(probe_urls),
        headers=lambda: _openai_auth_headers(XTTS_OPENAI_PLACEHOLDER_API_KEY),
        timeout=4,
        accepts_status=lambda status: (
            not _should_try_next_openai_candidate(status) and status < 400
        ),
    )


def check_kobold_qwen_connection(base_url: str = KOBOLD_QWEN_API_BASE_URL) -> bool:
    """Checks if the Qwen3 TTS server is reachable."""
    return _kobold_qwen_http.check_kobold_qwen_connection(
        base_url,
        resolve_api_key=lambda: _resolve_kobold_qwen_api_key(),
        voice_catalog_urls=lambda base_url: _openai_voice_catalog_urls(base_url),
        dedupe_ordered=lambda items: _dedupe_ordered(items),
    )


def get_kobold_qwen_models(base_url: str = KOBOLD_QWEN_API_BASE_URL) -> list[str]:
    """Fetches available Qwen3 TTS models from server."""
    return _kobold_qwen_http.get_kobold_qwen_models(
        base_url,
        resolve_api_key=lambda: _resolve_kobold_qwen_api_key(),
        extract_models=lambda payload: _extract_models_from_openai_payload(payload),
        merge_catalog=lambda preferred, discovered: _merge_catalog_with_discovered(
            preferred, discovered
        ),
        preferred_models=lambda: KOBOLD_QWEN_TTS_MODELS,
    )


def get_kobold_qwen_voice_catalog(
    base_url: str = KOBOLD_QWEN_API_BASE_URL, api_key: str = ""
) -> list[dict[str, str]]:
    """Fetch Qwen voices while retaining the API's cloned/preset model metadata."""
    return _kobold_qwen_http.get_kobold_qwen_voice_catalog(
        base_url,
        api_key,
        resolve_api_key=lambda: _resolve_kobold_qwen_api_key(),
        voice_catalog_urls=lambda base_url: _openai_voice_catalog_urls(base_url),
        dedupe_ordered=lambda items: _dedupe_ordered(items),
        preset_voices=lambda: KOBOLD_QWEN_TTS_VOICES,
        default_model=lambda: KOBOLD_QWEN_DEFAULT_MODEL,
        sample_voice=lambda: KOBOLD_QWEN_SAMPLE_VOICE,
    )


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

    return _probe_get_urls(
        _dedupe_ordered(probe_urls),
        headers=lambda: _openai_auth_headers(api_key),
        timeout=4,
        accepts_status=lambda status: (
            not _should_try_next_openai_candidate(status) and status < 400
        ),
    )


def check_kokoro_connection(base_url: str = KOKORO_API_BASE_URL) -> bool:
    """Checks if the Kokoro server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, KOKORO_API_BASE_URL)
    api_key = _resolve_kokoro_api_key()

    probe_urls = [
        f"{normalized_base_url}/health",
        *_kokoro_models_urls(normalized_base_url),
        *_kokoro_voices_urls(normalized_base_url),
    ]

    return _probe_get_urls(
        _dedupe_ordered(probe_urls),
        headers=lambda: _openai_auth_headers(api_key),
        timeout=4,
        accepts_status=lambda status: (
            not _should_try_next_openai_candidate(status) and status < 400
        ),
    )


def check_xtts_connection(base_url: str = XTTS_API_BASE_URL) -> bool:
    """Checks if the XTTS server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, XTTS_API_BASE_URL)
    probe_paths = ["/health", "/v1/models", "/docs", "/"]

    return _probe_get_urls(
        (f"{normalized_base_url}{path}" for path in probe_paths),
        timeout=3,
        accepts_status=lambda status: status != 404 and status < 500,
    )


def check_silero_connection(base_url: str = SILERO_API_BASE_URL) -> bool:
    """Checks if the Silero server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, SILERO_API_BASE_URL)
    return _probe_get_urls(
        (f"{normalized_base_url}{path}" for path in ("/ready", "/health", "/v1/models")),
        timeout=4,
        accepts_status=lambda status: status < 400,
    )


# Magpie Functions
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

    return _native_speech_http.post_speech_candidates(
        _openai_audio_speech_urls(normalized_base_url),
        request_options=lambda: {
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        should_try_next=lambda status: _should_try_next_openai_candidate(status),
        no_endpoint_message=(
            f"No Magpie speech endpoint could be resolved for '{normalized_base_url}'."
        ),
    )


# XTTS Functions
def get_xtts_speakers(base_url: str = XTTS_API_BASE_URL) -> list[str]:
    """Fetches discoverable XTTS voice identifiers from server."""
    normalized_base_url = _normalize_base_url(base_url, XTTS_API_BASE_URL)
    return _xtts_catalogue_http.get_xtts_speakers(
        normalized_base_url,
        _openai_voice_catalog_urls=lambda base: _openai_voice_catalog_urls(base),
        _openai_auth_headers=lambda: _openai_auth_headers(),
        _should_try_next_openai_candidate=lambda status: _should_try_next_openai_candidate(status),
        _extract_voices_from_openai_payload=lambda payload: _extract_voices_from_openai_payload(
            payload
        ),
        _openai_files_urls=lambda base: _openai_files_urls(base),
        _extract_file_ids_from_openai_payload=lambda payload, *, allowed_purposes: (
            _extract_file_ids_from_openai_payload(payload, allowed_purposes=allowed_purposes)
        ),
        discoverable_file_purposes=lambda: XTTS_DISCOVERABLE_FILE_PURPOSES,
        _dedupe_ordered=lambda items: _dedupe_ordered(items),
    )


def get_xtts_models(base_url: str = XTTS_API_BASE_URL) -> list[str]:
    """Fetches available XTTS models from server."""
    normalized_base_url = _normalize_base_url(base_url, XTTS_API_BASE_URL)
    return _xtts_catalogue_http.get_xtts_models(
        normalized_base_url,
        _openai_models_urls=lambda base: _openai_models_urls(base),
        _openai_auth_headers=lambda: _openai_auth_headers(),
        _should_try_next_openai_candidate=lambda status: _should_try_next_openai_candidate(status),
        _extract_models_from_openai_payload=lambda payload: _extract_models_from_openai_payload(
            payload
        ),
        default_model=lambda: XTTS_DEFAULT_MODEL,
        _merge_catalog_with_discovered=lambda preferred, discovered: _merge_catalog_with_discovered(
            preferred, discovered
        ),
    )


def _remote_voice_exists(
    voice_id: str,
    *,
    base_url: str,
    api_key: str = "",
) -> bool | None:
    """Verify a remote voice after an idempotent or unsupported DELETE."""
    return _remote_voice_exists_http(
        voice_id,
        base_url=base_url,
        api_key=api_key,
        _openai_voice_catalog_urls=lambda base: _openai_voice_catalog_urls(base),
        _openai_auth_headers=lambda key: _openai_auth_headers(key),
        _should_try_next_openai_candidate=lambda status: _should_try_next_openai_candidate(status),
        _extract_voices_from_openai_payload=lambda payload: _extract_voices_from_openai_payload(
            payload
        ),
        _voice_catalog_can_confirm_absence=lambda payload: _voice_catalog_can_confirm_absence(
            payload
        ),
    )


def _delete_speaker_voice_openai_compatible(
    voice_id: str,
    *,
    base_url: str,
    fallback_base_url: str,
    service_name: str,
    api_key: str = "",
) -> bool:
    """Delete an uploaded voice without confusing an absent route with an absent voice."""
    return _delete_speaker_voice_http(
        voice_id,
        base_url=base_url,
        fallback_base_url=fallback_base_url,
        service_name=service_name,
        api_key=api_key,
        _normalize_base_url=lambda base, fallback: _normalize_base_url(base, fallback),
        _openai_voice_catalog_urls=lambda base: _openai_voice_catalog_urls(base),
        _openai_auth_headers=lambda key: _openai_auth_headers(key),
        _remote_voice_exists=lambda voice_id, *, base_url, api_key="": _remote_voice_exists(
            voice_id, base_url=base_url, api_key=api_key
        ),
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
    return _silero_catalogue_http.get_silero_model_catalog(
        base_url,
        _normalize_base_url=lambda base, fallback: _normalize_base_url(base, fallback),
        default_base_url=lambda: SILERO_API_BASE_URL,
    )


def get_silero_models(
    base_url: str = SILERO_API_BASE_URL,
    *,
    installed_only: bool = True,
) -> list[str]:
    catalog = get_silero_model_catalog(base_url)
    models = []
    for item in catalog:
        raw_status = item.get("status")
        status = raw_status if isinstance(raw_status, dict) else {}
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
    return _silero_catalogue_http.get_silero_voice_catalog(
        base_url,
        model=model,
        language=language,
        include_unavailable=include_unavailable,
        _normalize_base_url=lambda base, fallback: _normalize_base_url(base, fallback),
        default_base_url=lambda: SILERO_API_BASE_URL,
        normalize_silero_language_code=lambda value: normalize_silero_language_code(value),
    )


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
    return _native_speech_http.post_speech_candidates(
        _openai_audio_speech_urls(normalized_base_url),
        request_options=lambda: {
            "headers": _openai_auth_headers(),
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        should_try_next=lambda status: _should_try_next_openai_candidate(status),
        no_endpoint_message=(
            f"No XTTS speech endpoint could be resolved for '{normalized_base_url}'."
        ),
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


def _request_voxcpm_audio(
    text: str, tts_settings: dict, voxcpm_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(voxcpm_base_url, VOXCPM_API_BASE_URL)
    api_key = _resolve_voxcpm_api_key(tts_settings)
    payload = _build_voxcpm_payload(text, tts_settings)
    return _voxcpm_speech_http.post_voxcpm_speech(
        _openai_audio_speech_urls(normalized_base_url),
        payload,
        request_options=lambda current_payload: {
            "headers": _openai_auth_headers(api_key),
            "json": current_payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        prompt_pairing_error=lambda response: _is_voxcpm_prompt_pairing_error(response),
        should_try_next=lambda status: _should_try_next_openai_candidate(status),
        warn=lambda message, voice: logging.warning(message, voice),
        no_endpoint_message=(
            f"No VoxCPM speech endpoint could be resolved for '{normalized_base_url}'."
        ),
    )


def _build_fishs2_payload(text: str, tts_settings: dict) -> dict:
    model = _normalize_fishs2_model(
        tts_settings.get("xtts_model", ""),
        fallback=FISHS2_DEFAULT_MODEL,
    )
    voice = str(tts_settings.get("speaker") or "").strip() or FISHS2_DEFAULT_VOICE
    fishs2_options = _build_fishs2_options(tts_settings)
    raw_prosody = fishs2_options.get("prosody")
    prosody = raw_prosody if isinstance(raw_prosody, dict) else {}

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "wav",
        "speed": prosody.get("speed", 1.0),
    }
    payload.update(fishs2_options)
    from .speech_performance import compile_for_provider

    compiled = compile_for_provider(text, {**tts_settings, "xtts_model": model}, {"id": "fishs2"})
    payload["input"] = compiled.input

    return payload


def _request_fishs2_audio(
    text: str, tts_settings: dict, fishs2_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(fishs2_base_url, FISHS2_API_BASE_URL)
    api_key = _resolve_fishs2_api_key(tts_settings)
    payload = _build_fishs2_payload(text, tts_settings)
    return _native_speech_http.post_speech_candidates(
        _openai_audio_speech_urls(normalized_base_url),
        request_options=lambda: {
            "headers": _openai_auth_headers(api_key),
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        should_try_next=lambda status: _should_try_next_openai_candidate(status),
        no_endpoint_message=(
            f"No FishS2 speech endpoint could be resolved for '{normalized_base_url}'."
        ),
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

    return _native_speech_http.post_speech_candidates(
        _openai_audio_speech_urls(normalized_base_url),
        request_options=lambda: {
            "headers": _openai_auth_headers(api_key),
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        should_try_next=lambda status: _should_try_next_openai_candidate(status),
        no_endpoint_message=(
            f"No Voxtral speech endpoint could be resolved for '{normalized_base_url}'."
        ),
    )


def _request_kokoro_audio(
    text: str, tts_settings: dict, kokoro_base_url: str
) -> requests.Response:
    normalized_base_url = _normalize_base_url(kokoro_base_url, KOKORO_API_BASE_URL)
    api_key = _resolve_kokoro_api_key(tts_settings)
    payload = _build_kokoro_payload(text, tts_settings)

    return _native_speech_http.post_speech_candidates(
        _openai_audio_speech_urls(normalized_base_url),
        request_options=lambda: {
            "headers": _openai_auth_headers(api_key),
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        should_try_next=lambda status: _should_try_next_openai_candidate(status),
        no_endpoint_message=(
            f"No Kokoro speech endpoint could be resolved for '{normalized_base_url}'."
        ),
    )


def _build_openai_compatible_audio_payload(
    text: str,
    tts_settings: dict,
    endpoint: Mapping[str, object],
) -> dict:
    provider = _infer_audio_provider(
        name=str(endpoint.get("name") or ""),
        base_url=str(endpoint.get("base_url") or ""),
        raw_provider=str(endpoint.get("provider") or ""),
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
    if _is_xtts_target(model_name, endpoint):
        payload["instructions"] = _build_xtts_instructions_payload(
            tts_settings,
            legacy_instructions,
        )
    else:
        from .speech_performance import compile_for_provider

        compiled = compile_for_provider(
            text, {**tts_settings, "xtts_model": model_name},
            {**endpoint, "provider": provider},
        )
        payload["input"] = compiled.input
        if compiled.instructions:
            payload["instructions"] = compiled.instructions

    return payload




def _audio_cpp_language(model: str, language: object, endpoint: dict | None = None) -> str:
    return _audio_cpp_speech_payload._audio_cpp_language(
        model,
        language,
        endpoint,
        model_metadata=_audio_cpp_model_metadata,
    )


def _audio_cpp_selected_model_options(
    tts_settings: dict[str, Any],
    model: str,
    family: str,
) -> dict[str, Any] | None:
    """Return validated options only when the exact model has an option map."""
    return _audio_cpp_speech_payload._audio_cpp_selected_model_options(
        tts_settings,
        model,
        family,
        validate_options=validate_audio_cpp_model_options,
    )


def _build_audio_cpp_audio_payload(
    text: str,
    tts_settings: dict,
    endpoint: dict,
) -> dict:
    """Build audio.cpp's direct HTTP speech request."""

    def load_performance_compiler() -> _audio_cpp_speech_payload.PerformanceCompiler:
        from .speech_performance import compile_for_provider

        return compile_for_provider

    return _audio_cpp_speech_payload._build_audio_cpp_audio_payload(
        text,
        tts_settings,
        endpoint,
        model_metadata=_audio_cpp_model_metadata,
        model_language=_audio_cpp_language,
        selected_model_options=_audio_cpp_selected_model_options,
        validate_options=validate_audio_cpp_model_options,
        get_performance_compiler=load_performance_compiler,
    )


def _build_guided_speech_prompt(text: str, generation_prompt: str) -> str:
    """Backward-compatible entry point for Gemini's shared prompt composer."""
    from .speech_performance import guided_speech_prompt

    return guided_speech_prompt(text, generation_prompt)


def _litellm_response_to_requests_response(litellm_response) -> requests.Response:
    raw_response = getattr(litellm_response, "response", None)

    response = requests.Response()
    response.status_code = int(getattr(raw_response, "status_code", 200) or 200)
    response._content = bytes(getattr(litellm_response, "content", b"") or b"")
    response.headers = CaseInsensitiveDict(
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
    payload: dict, endpoint: Mapping[str, object]
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
        name=str(endpoint.get("name") or ""),
        base_url=str(endpoint.get("base_url") or ""),
        raw_provider=str(endpoint.get("provider") or ""),
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
        # Keep the SDK timeout aligned with direct HTTP and avoid multiplying
        # Pandrator's caller retry budget with another SDK retry loop.
        request_kwargs["timeout"] = TTS_GENERATION_TIMEOUT_SECONDS
        request_kwargs["max_retries"] = 0

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
    _timing_sink: dict | None = None,
) -> requests.Response:
    endpoint, error = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        raise RuntimeError(error)

    if _normalize_custom_adapter(endpoint.get("adapter")) == AUDIO_CPP_ADAPTER:
        payload = _build_audio_cpp_audio_payload(text, tts_settings, endpoint)
        speech_path = str(endpoint.get("speech_path") or "/v1/audio/speech")
        return _native_speech_http.post_prepared_speech(
            lambda: _configured_endpoint_url(str(endpoint["base_url"]), speech_path),
            request_options=lambda: {
                "headers": _configured_endpoint_auth_headers(endpoint),
                "json": payload,
                "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
            },
            request_session=request_session,
        )

    if _normalize_custom_adapter(endpoint.get("adapter")) == AZURE_SPEECH_ADAPTER:
        return _request_azure_speech_audio(text, tts_settings, endpoint)

    if _normalize_custom_adapter(endpoint.get("adapter")) == ELEVENLABS_NATIVE_ADAPTER:
        if _timing_sink is not None:
            return _request_elevenlabs_audio(
                text, tts_settings, endpoint=endpoint, _timing_sink=_timing_sink,
            )
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
        return _native_speech_http.post_prepared_speech(
            lambda: _configured_endpoint_url(str(endpoint["base_url"]), speech_path),
            request_options=lambda: {
                "headers": _configured_endpoint_auth_headers(endpoint),
                "json": payload,
                "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
            },
        )

    payload = _build_openai_compatible_audio_payload(text, tts_settings, endpoint)
    provider = _infer_audio_provider(
        name=str(endpoint.get("name") or ""),
        base_url=str(endpoint.get("base_url") or ""),
        raw_provider=str(endpoint.get("provider") or ""),
    )

    uses_nonstandard_gemini_base = (
        provider == GEMINI_PROVIDER
        and _normalize_base_url(str(endpoint.get("base_url") or ""), "") != GEMINI_AUDIO_BASE_URL
    )
    if (
        provider == GEMINI_PROVIDER
        and not uses_nonstandard_gemini_base
        and _google_tts_audio.is_structured_tts_model(str(payload.get("model") or ""))
    ):
        return _request_gemini_native_audio(
            payload, endpoint, request_session=request_session
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

    if provider == GEMINI_PROVIDER and not uses_nonstandard_gemini_base:
        return _request_gemini_native_audio(
            payload, endpoint, request_session=request_session
        )

    return _native_speech_http.post_speech_candidates(
        _configured_openai_urls(
            endpoint,
            "speech_path",
            _openai_audio_speech_urls(str(endpoint["base_url"])),
        ),
        request_options=lambda: {
            "headers": _configured_endpoint_auth_headers(endpoint),
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        should_try_next=lambda status: _should_try_next_openai_candidate(status),
        no_endpoint_message=f"No speech endpoint could be resolved for '{endpoint['name']}'.",
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


def _google_tts_pcm_response(
    response: requests.Response, endpoint: str, provider_name: str
) -> requests.Response:
    """Decode a Gemini/Vertex PCM response without assuming audio is part zero."""
    if not response.ok:
        return response
    try:
        candidates = response.json()["candidates"]
        if not isinstance(candidates, list):
            raise TypeError("candidates must be a list")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts", [])
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                inline_data = part.get("inlineData") or part.get("inline_data")
                if not isinstance(inline_data, dict):
                    continue
                mime = str(
                    inline_data.get("mimeType") or inline_data.get("mime_type") or ""
                ).lower()
                media_type, *parameters = (item.strip() for item in mime.split(";"))
                if media_type and media_type not in {"audio/l16", "audio/pcm", "audio/x-pcm"}:
                    continue
                sample_rate = 24000
                for parameter in parameters:
                    name, separator, value = parameter.partition("=")
                    if separator and name.strip() == "rate":
                        sample_rate = int(value.strip())
                    elif separator and name.strip() == "channels" and int(value.strip()) != 1:
                        raise ValueError("multichannel PCM is unsupported")
                if sample_rate <= 0:
                    raise ValueError("invalid PCM sample rate")
                data = inline_data.get("data")
                if not isinstance(data, str) or not data:
                    raise ValueError("empty PCM data")
                pcm = base64.b64decode(data, validate=True)
                if not pcm or len(pcm) % 2:
                    raise ValueError("invalid 16-bit PCM data")
                audio_response = requests.Response()
                audio_response.status_code = 200
                audio_response._content = _pcm_to_wav_bytes(pcm, sample_rate=sample_rate)
                audio_response.headers["Content-Type"] = "audio/wav"
                audio_response.url = endpoint
                return audio_response
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise RuntimeError(f"{provider_name} returned no decodable audio payload.") from error
    raise RuntimeError(f"{provider_name} returned no decodable audio payload.")


def _request_gemini_native_audio(
    payload: dict,
    endpoint: dict[str, object],
    *,
    request_session: requests.Session | None = None,
) -> requests.Response:
    model = _normalize_model_for_provider(str(payload.get("model") or ""), GEMINI_PROVIDER)
    if _google_tts_audio.is_structured_tts_model(model):
        url = "https://generativelanguage.googleapis.com/v1beta/interactions"
        body = _google_tts_audio.build_gemini_speech_request(
            model,
            str(payload.get("input") or ""),
            str(payload.get("voice") or ""),
            style=str(payload.get("instructions") or ""),
        )
        response = _native_speech_http.post_prepared_speech(
            lambda: url,
            request_options=lambda: {
                "headers": {
                    "x-goog-api-key": _resolve_openai_audio_api_key(endpoint),
                    "Content-Type": "application/json",
                },
                "json": body,
                "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
            },
            request_session=request_session,
        )
        return _google_tts_audio.decode_google_wav_response(
            response, url, "Gemini", interactions=True
        )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": str(payload.get("input") or "")}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": str(payload.get("voice") or "")},
                }
            },
        },
    }
    response = _native_speech_http.post_prepared_speech(
        lambda: url,
        request_options=lambda: {
            "headers": {
                "x-goog-api-key": _resolve_openai_audio_api_key(endpoint),
                "Content-Type": "application/json",
            },
            "json": body,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        request_session=request_session,
    )
    return _google_tts_pcm_response(response, url, "Gemini")


def _request_vertex_ai_audio(text: str, tts_settings: dict) -> requests.Response:
    service = get_service_config(tts_settings, VERTEX_PROVIDER) or {}
    token, project_id = _vertex_access_token(service)
    location = str(service.get("vertex_location") or VERTEX_AUDIO_DEFAULT_LOCATION).strip()
    model = str(
        tts_settings.get("xtts_model")
        or tts_settings.get("model")
        or service.get("default_model")
        or VERTEX_AUDIO_DEFAULT_MODEL
    ).strip()
    model = _normalize_model_for_provider(model, VERTEX_PROVIDER)
    structured_tts = _google_tts_audio.is_structured_tts_model(model)
    if structured_tts and location != "global":
        raise ValueError("Gemini 3.8 Flash TTS on Vertex requires the global location.")
    voice = str(
        tts_settings.get("speaker")
        or tts_settings.get("voice")
        or service.get("default_voice")
        or GEMINI_AUDIO_DEFAULT_VOICE
    ).strip()
    endpoint = (
        f"https://aiplatform.googleapis.com/{'v1' if structured_tts else 'v1beta1'}/projects/"
        f"{quote(project_id, safe='')}/locations/{quote(location, safe='')}/"
        f"publishers/google/models/{quote(model, safe='')}:generateContent"
    )
    from .speech_performance import compile_for_provider

    compiled = compile_for_provider(
        text, {**tts_settings, "xtts_model": model}, {"provider": "vertex_ai"}
    )
    prompt_text = compiled.input
    payload = (
        _google_tts_audio.build_vertex_speech_request(
            prompt_text, voice, style=compiled.instructions
        )
        if structured_tts
        else {
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
    )
    response = _native_speech_http.post_prepared_speech(
        lambda: endpoint,
        request_options=lambda: {
            "headers": {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
    )
    if structured_tts:
        return _google_tts_audio.decode_google_wav_response(
            response, endpoint, "Vertex AI", interactions=False
        )
    return _google_tts_pcm_response(response, endpoint, "Vertex AI")


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

    from .speech_performance import compile_for_provider

    payload["input"] = compile_for_provider(
        text, {**tts_settings, "xtts_model": model}, {"id": "chatterbox"}
    ).input

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

    return _native_speech_http.post_speech_candidates(
        _openai_audio_speech_urls(normalized_base_url),
        request_options=lambda: {
            "headers": _openai_auth_headers(XTTS_OPENAI_PLACEHOLDER_API_KEY),
            "json": payload,
            "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
        },
        should_try_next=lambda status: _should_try_next_openai_candidate(status),
        no_endpoint_message=(
            f"No Chatterbox speech endpoint could be resolved for '{normalized_base_url}'."
        ),
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
    return _kobold_qwen_http._kobold_qwen_is_ready(
        base_url,
        api_key,
        resolve_api_key=lambda: _resolve_kobold_qwen_api_key(),
    )


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


def _build_kobold_qwen_payload(text: str, tts_settings: dict) -> dict[str, str | float]:
    model = resolve_kobold_qwen_model(tts_settings)
    if not model:
        model = KOBOLD_QWEN_DEFAULT_MODEL

    from .tts_language_preflight import validate_tts_language

    validate_tts_language(
        {**tts_settings, "xtts_model": model},
        endpoint={"id": "kobold_qwen", "provider": "kobold_qwen"},
    )

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

    payload: dict[str, str | float] = {
        "model": model,
        "input": text,
        "voice": voice,
        "speed": _coerce_float(tts_settings.get("speed"), 1.0),
        "response_format": "wav",
    }
    from .speech_performance import compile_for_provider

    compiled = compile_for_provider(text, {**tts_settings, "xtts_model": model}, {"id": "kobold_qwen"})
    payload["input"] = compiled.input
    if compiled.instructions:
        payload["instructions"] = compiled.instructions
    return payload


def _request_kobold_qwen_audio(
    text: str, tts_settings: dict, kobold_qwen_base_url: str
) -> requests.Response:
    return _kobold_qwen_http._request_kobold_qwen_audio(
        text,
        tts_settings,
        kobold_qwen_base_url,
        resolve_api_key=lambda settings: _resolve_kobold_qwen_api_key(settings),
        build_payload=lambda text, settings: _build_kobold_qwen_payload(text, settings),
    )


def get_kobold_qwen_batch_capabilities(
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    *,
    api_key: str = "",
) -> KoboldQwenBatchCapabilities:
    return _kobold_qwen_http.get_kobold_qwen_batch_capabilities(
        base_url,
        api_key=api_key,
        resolve_api_key=lambda: _resolve_kobold_qwen_api_key(),
    )


def _iter_kobold_qwen_batch_audio_http(
    items: list[dict[str, object]],
    *,
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    api_key: str = "",
    stop_event: Event | None = None,
    cancel_event: Event | None = None,
    response_callback: Callable[[requests.Response | None], None] | None = None,
) -> Iterator[KoboldQwenBatchAudioEvent]:
    yield from _kobold_qwen_http._iter_kobold_qwen_batch_audio_http(
        items,
        base_url=base_url,
        api_key=api_key,
        stop_event=stop_event,
        cancel_event=cancel_event,
        response_callback=response_callback,
        resolve_api_key=lambda settings: _resolve_kobold_qwen_api_key(settings),
        build_payload=lambda text, settings: _build_kobold_qwen_payload(text, settings),
        decode_audio=lambda audio, *, format_hint: _decode_audio_bytes(
            audio, format_hint=format_hint
        ),
    )


def iter_kobold_qwen_batch_audio(
    items: list[dict[str, object]],
    *,
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    api_key: str = "",
    cancel_event: Event | None = None,
) -> Iterator[KoboldQwenBatchAudioEvent]:
    """Read the batch stream ahead so inference overlaps local take handling."""
    yield from _kobold_qwen_stream.iter_read_ahead_batch(
        len(items),
        producer=lambda stop_event, response_callback: _iter_kobold_qwen_batch_audio_http(
            items,
            base_url=base_url,
            api_key=api_key,
            stop_event=stop_event,
            cancel_event=cancel_event,
            response_callback=response_callback,
        ),
        cancel_event=cancel_event,
        thread_factory=Thread,
    )


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
    _timing_sink: dict | None = None,
) -> AudioSegment | None:
    """
    Generates audio from text using the specified TTS service.
    `tts_settings` is a dictionary-like object (e.g., a dataclass).
    """
    service_hint = _normalize_service_id(tts_settings.get("service"))
    if service_hint == AUDIO_CPP_ADAPTER:
        tts_settings = dict(tts_settings)
        tts_settings.setdefault("audio_cpp_base_url", audio_cpp_base_url)
    if not _audio_cpp_lock_held:
        endpoint, _error = resolve_openai_audio_endpoint(tts_settings)
        if (
            endpoint is not None
            and _normalize_custom_adapter(endpoint.get("adapter")) == AUDIO_CPP_ADAPTER
        ):
            with audio_cpp_endpoint_lock(tts_settings, cancel_event):
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
                    _timing_sink=_timing_sink,
                )
    # Visual subtitle wrapping must never leak into provider payloads.  This
    # also makes direct callers consistent with dubbing speech blocks.
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    service = tts_settings.get("service", "XTTS")
    normalized_silero_base_url = _normalize_base_url(
        silero_base_url, SILERO_API_BASE_URL
    )
    from .tts_language_preflight import validate_tts_language

    language_preflight = validate_tts_language(tts_settings)

    max_attempts = max(1, min(20, int(max_attempts or 1)))
    try:
        configured_recovery_cycles = tts_settings.get("service_recovery_cycles")
        maximum_recovery_cycles = max(
            0,
            min(
                10,
                int(3 if configured_recovery_cycles is None else configured_recovery_cycles),
            ),
        )
    except (TypeError, ValueError):
        maximum_recovery_cycles = 3
    attempt = 0
    recovery_cycles = 0
    last_error: Exception | None = None
    last_retryable = False
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
            elif service_hint == AUDIO_CPP_ADAPTER or service in {
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
                    **({"_timing_sink": _timing_sink} if _timing_sink is not None else {}),
                )
            elif str(service).strip().lower() in {
                ELEVENLABS_SERVICE.lower(),
                ELEVENLABS_PROVIDER,
            }:
                if _timing_sink is None:
                    response = _request_elevenlabs_audio(text, tts_settings)
                else:
                    response = _request_elevenlabs_audio(
                        text, tts_settings, _timing_sink=_timing_sink,
                    )
            elif service == VERTEX_SERVICE:
                response = _request_vertex_ai_audio(text, tts_settings)
            elif service == "Silero":
                language_support = language_preflight.get("language_support")
                request_aliases = (
                    language_support.get("request_aliases")
                    if isinstance(language_support, Mapping)
                    else None
                )
                model_alias_applied = (
                    isinstance(request_aliases, Mapping)
                    and language_preflight["language"] in request_aliases
                )
                data = {
                    "model": str(
                        tts_settings.get("silero_model")
                        or tts_settings.get("xtts_model")
                        or tts_settings.get("model")
                        or SILERO_DEFAULT_MODEL
                    ),
                    "input": text,
                    "voice": str(tts_settings.get("speaker") or ""),
                    "language": (
                        normalize_silero_language_code(
                            language_preflight["native_language"]
                        )
                        if not isinstance(language_support, Mapping)
                        or (
                            language_support.get("coverage") == "unknown"
                            and not model_alias_applied
                        )
                        else language_preflight["native_language"]
                    ),
                    "response_format": "wav",
                    "speed": _coerce_float(tts_settings.get("speed"), 1.0),
                    "sample_rate": int(tts_settings.get("silero_sample_rate") or 48000),
                    "stress_mode": str(
                        tts_settings.get("silero_stress_mode") or "auto"
                    ),
                }
                response = _native_speech_http.post_prepared_speech(
                    lambda: f"{normalized_silero_base_url}/v1/audio/speech",
                    request_options=lambda data=data: {
                        "json": data,
                        "timeout": TTS_GENERATION_TIMEOUT_SECONDS,
                    },
                )
            else:
                raise ValueError(f"Unsupported TTS service: {service}")

            response.raise_for_status()
            if _timing_sink is not None and "provider_text" in _timing_sink:
                import base64

                from .speech_timing import elevenlabs_speech_timing

                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("audio_base64"), str):
                    raise RuntimeError("ElevenLabs returned no encoded audio.")
                audio = _decode_audio_bytes(
                    base64.b64decode(payload["audio_base64"], validate=True),
                    format_hint=_timing_sink["output_format"],
                )
                _timing_sink["speech_timing"] = elevenlabs_speech_timing(
                    _timing_sink["provider_text"], payload.get("alignment"),
                    duration_ms=audio.frame_count() * 1000 / audio.frame_rate,
                )
            else:
                audio = _decode_audio_response(response)
            return audio

        except ValueError as e:
            # Invalid service, model, voice, or configuration will not improve
            # with another identical request.
            logging.error("TTS configuration error: %s", e)
            last_error = e
            last_retryable = False
            break
        except Exception as e:
            last_error = e
            status = status_code_from_error(e)
            detail = _tts_error_detail(e)
            logging.warning(
                "TTS generation attempt %d/%d failed%s: %s",
                attempt,
                max_attempts,
                f" (HTTP {status})" if status else "",
                detail,
            )
            last_retryable = retryable_error(e) and not (
                _audio_cpp_lock_held
                and status == 500
                and _audio_cpp_permanent_error(detail)
            )
            if not last_retryable:
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

    model = str(tts_settings.get("xtts_model") or tts_settings.get("model") or "").strip()
    context = f"{service}" + (f" / {model}" if model else "")
    status = status_code_from_error(last_error) if last_error is not None else 0
    detail = _tts_error_detail(last_error) if last_error is not None else "No audio returned."
    message = (
        f"Speech generation failed ({context}"
        + (f", HTTP {status}" if status else "")
        + f") after {attempt} attempt(s): {detail}"
    )
    raise TtsGenerationError(message, retryable=last_retryable) from last_error
