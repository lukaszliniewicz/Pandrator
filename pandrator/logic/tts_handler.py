import io
import json
import logging
import os

import requests
from pydub import AudioSegment

try:
    from litellm import speech as litellm_speech
except Exception:  # pragma: no cover - runtime dependency guard
    litellm_speech = None

# XTTS default URLs
XTTS_API_BASE_URL = "http://127.0.0.1:8020"

# Voxtral default URLs
VOXTRAL_API_BASE_URL = "http://127.0.0.1:8000"

# Silero default URLs
SILERO_API_BASE_URL = "http://127.0.0.1:8001"

XTTS_OPENAI_PLACEHOLDER_API_KEY = "sk-placeholder"
XTTS_DEFAULT_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
XTTS_UPLOAD_FILE_PURPOSE = "user_data"
XTTS_DISCOVERABLE_FILE_PURPOSES = ("user_data", "assistants")
VOXTRAL_DEFAULT_MODEL = "auto"
VOXTRAL_DEFAULT_VOICE = "casual_female"
VOXTRAL_INSTRUCTIONS_PREFIX = "voxtral_options:"
VOXTRAL_TTS_MODELS = ["auto", "gguf", "bf16"]
OPENAI_AUDIO_DEFAULT_MODEL = "gpt-4o-mini-tts"
OPENAI_AUDIO_DEFAULT_VOICE = "alloy"
GEMINI_AUDIO_DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_AUDIO_DEFAULT_VOICE = "Kore"
OPENAI_AUDIO_BASE_URL = "https://api.openai.com/v1"
GEMINI_AUDIO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

OPENAI_SERVICE = "OpenAI"
GEMINI_SERVICE = "Gemini"
OPENAI_COMPAT_SERVICE = "OpenAI-Compatible"

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


def _normalize_base_url(base_url: str | None, fallback: str) -> str:
    normalized = (base_url or fallback).strip().rstrip("/")
    return normalized or fallback


def _openai_auth_headers(api_key: str = XTTS_OPENAI_PLACEHOLDER_API_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


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


def _build_xtts_overrides(tts_settings: dict) -> dict[str, object]:
    return {
        "temperature": _coerce_float(tts_settings.get("temperature"), 0.75),
        "top_p": _coerce_float(tts_settings.get("top_p"), 0.85),
        "top_k": _coerce_int(tts_settings.get("top_k"), 50),
        "repetition_penalty": _coerce_float(tts_settings.get("repetition_penalty"), 5.0),
        "length_penalty": _coerce_float(tts_settings.get("length_penalty"), 1.0),
        "do_sample": _coerce_bool(tts_settings.get("do_sample"), True),
        "num_beams": _coerce_int(tts_settings.get("num_beams"), 1),
        "enable_text_splitting": _coerce_bool(tts_settings.get("enable_text_splitting"), False),
        "gpt_cond_len": _coerce_int(tts_settings.get("gpt_cond_len"), 12),
        "gpt_cond_chunk_len": _coerce_int(tts_settings.get("gpt_cond_chunk_len"), 4),
        "max_ref_len": _coerce_int(tts_settings.get("max_ref_len"), 12),
        "sound_norm_refs": _coerce_bool(tts_settings.get("sound_norm_refs"), False),
        "stream_chunk_size": _coerce_int(tts_settings.get("stream_chunk_size"), 20),
        "overlap_wav_len": _coerce_int(tts_settings.get("overlap_wav_len"), 1024),
    }


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

    for key in xtts_overrides:
        payload.pop(key, None)
    payload.pop("temp", None)

    existing_xtts = payload.get("xtts")
    merged_xtts: dict[str, object] = {}
    if isinstance(existing_xtts, dict):
        merged_xtts.update(existing_xtts)
    merged_xtts.update(xtts_overrides)

    payload["language"] = str(tts_settings.get("language") or "en").strip() or "en"
    payload["xtts"] = merged_xtts

    return json.dumps(payload, ensure_ascii=False)


def _normalize_voxtral_model(model_name: str, fallback: str = VOXTRAL_DEFAULT_MODEL) -> str:
    normalized = str(model_name or "").strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    if normalized in {"auto", "gguf", "bf16"}:
        return normalized
    return fallback


def _build_voxtral_options(tts_settings: dict) -> dict[str, object]:
    return {
        "language": str(tts_settings.get("language") or "en").strip() or "en",
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

    for key in options:
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
    if normalized == GEMINI_SERVICE.lower():
        return GEMINI_PROVIDER
    return ""


def _default_audio_endpoint(provider: str) -> dict[str, str]:
    normalized_provider = _normalize_audio_provider(provider)
    if normalized_provider == GEMINI_PROVIDER:
        return {
            "name": GEMINI_PROVIDER,
            "base_url": GEMINI_AUDIO_BASE_URL,
            "api_key": XTTS_OPENAI_PLACEHOLDER_API_KEY,
            "api_key_env": "GEMINI_API_KEY",
            "provider": GEMINI_PROVIDER,
            "default_model": GEMINI_AUDIO_DEFAULT_MODEL,
            "default_voice": GEMINI_AUDIO_DEFAULT_VOICE,
        }

    return {
        "name": OPENAI_PROVIDER,
        "base_url": OPENAI_AUDIO_BASE_URL,
        "api_key": XTTS_OPENAI_PLACEHOLDER_API_KEY,
        "api_key_env": "OPENAI_API_KEY",
        "provider": OPENAI_PROVIDER,
        "default_model": OPENAI_AUDIO_DEFAULT_MODEL,
        "default_voice": OPENAI_AUDIO_DEFAULT_VOICE,
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


def _openai_audio_speech_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/speech")


def _openai_files_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "files")


def _voxtral_models_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/models")


def _voxtral_voices_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/voices")


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
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", [])
    if not isinstance(data, list):
        return []

    voices: list[str] = []
    for voice in data:
        if isinstance(voice, dict):
            voice_id = str(voice.get("voice_id") or voice.get("id") or "").strip()
            if voice_id:
                voices.append(voice_id)
        elif isinstance(voice, str):
            trimmed = voice.strip()
            if trimmed:
                voices.append(trimmed)
    return _dedupe_sorted(voices)


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


def _parse_openai_audio_endpoints(tts_settings: dict) -> dict[str, dict[str, str]]:
    raw_json = str(tts_settings.get("openai_audio_endpoints_json", "") or "").strip()
    if not raw_json:
        return {}

    is_valid, error = validate_openai_audio_endpoints_json(raw_json)
    if not is_valid:
        logging.warning("Skipping OpenAI-compatible audio endpoints: %s", error)
        return {}

    payload = json.loads(raw_json)
    endpoints: dict[str, dict[str, str]] = {}
    for item in payload:
        name = str(item.get("name", "")).strip()
        if not name:
            continue

        base_url = str(item.get("base_url", item.get("api_base", ""))).strip().rstrip("/")
        if not base_url:
            continue

        provider = _infer_audio_provider(
            name=name,
            base_url=base_url,
            raw_provider=str(item.get("provider", "") or "").strip(),
        )

        default_model = str(item.get("default_model", "")).strip()
        if not default_model:
            default_model = _provider_default_model(provider)
        default_model = _normalize_model_for_provider(default_model, provider)

        default_voice = str(item.get("default_voice", "")).strip()
        if not default_voice:
            default_voice = _provider_default_voice(provider)
        default_voice = _normalize_voice_for_provider(default_voice, provider)

        endpoints[name] = {
            "name": name,
            "base_url": base_url,
            "api_key": str(item.get("api_key", "")).strip(),
            "api_key_env": str(item.get("api_key_env", "")).strip(),
            "provider": provider,
            "default_model": default_model,
            "default_voice": default_voice,
        }

    return endpoints


def list_openai_audio_endpoint_names(tts_settings: dict) -> list[str]:
    """Lists configured OpenAI-compatible audio endpoint names."""
    service_provider = _provider_for_tts_service(tts_settings.get("service"))
    if service_provider:
        return [_default_audio_endpoint(service_provider)["name"]]

    return sorted(_parse_openai_audio_endpoints(tts_settings).keys())


def resolve_openai_audio_endpoint(tts_settings: dict) -> tuple[dict[str, str] | None, str]:
    """Resolves selected OpenAI-compatible audio endpoint from settings."""
    service_provider = _provider_for_tts_service(tts_settings.get("service"))
    if service_provider:
        return _default_audio_endpoint(service_provider), ""

    endpoints = _parse_openai_audio_endpoints(tts_settings)
    if not endpoints:
        selected_name = str(tts_settings.get("openai_audio_endpoint", "") or "").strip()
        selected_provider = _normalize_audio_provider(selected_name)
        if selected_provider:
            return _default_audio_endpoint(selected_provider), ""
        return None, "No OpenAI-compatible audio endpoints are configured."

    selected_name = str(tts_settings.get("openai_audio_endpoint", "") or "").strip()
    if selected_name:
        endpoint = endpoints.get(selected_name)
        if endpoint is None:
            selected_provider = _normalize_audio_provider(selected_name)
            if selected_provider:
                return _default_audio_endpoint(selected_provider), ""
            return None, f"Audio endpoint '{selected_name}' is not defined in config."
        return endpoint, ""

    first_name = sorted(endpoints.keys())[0]
    return endpoints[first_name], ""


def should_show_xtts_advanced_settings(tts_settings: dict) -> bool:
    service = str(tts_settings.get("service") or "").strip()
    if service == "XTTS":
        return True
    if service in {OPENAI_SERVICE, GEMINI_SERVICE}:
        return False
    if service != OPENAI_COMPAT_SERVICE:
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


def _resolve_voxtral_api_key() -> str:
    api_key = os.getenv("VOXTRAL_API_KEY", "").strip()
    if api_key:
        return api_key
    return XTTS_OPENAI_PLACEHOLDER_API_KEY


def check_openai_audio_connection(tts_settings: dict) -> tuple[bool, str]:
    """Checks OpenAI-compatible audio endpoint reachability."""
    endpoint, error = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        return False, error

    api_key = _resolve_openai_audio_api_key(endpoint)
    last_status = None
    last_text = ""

    for models_url in _openai_models_urls(endpoint["base_url"]):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if response.status_code == 404:
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

    return (
        False,
        f"Endpoint '{endpoint['name']}' does not expose an OpenAI-compatible /models endpoint. "
        f"Last status: {last_status or 'N/A'}. {last_text}",
    )


def _resolve_openai_audio_provider_context(
    tts_settings: dict,
) -> tuple[dict[str, str] | None, str, str, str]:
    endpoint, _ = resolve_openai_audio_endpoint(tts_settings)
    if endpoint is None:
        return None, "", "", ""

    provider = _infer_audio_provider(
        name=endpoint.get("name", ""),
        base_url=endpoint.get("base_url", ""),
        raw_provider=endpoint.get("provider", ""),
    )

    default_model = str(endpoint.get("default_model", "")).strip() or _provider_default_model(provider)
    default_model = _normalize_model_for_provider(default_model, provider)

    default_voice = str(endpoint.get("default_voice", "")).strip() or _provider_default_voice(provider)
    default_voice = _normalize_voice_for_provider(default_voice, provider)

    return endpoint, provider, default_model, default_voice


def get_openai_audio_models_fallback(tts_settings: dict) -> list[str]:
    """Returns built-in model suggestions for OpenAI-compatible audio providers."""
    endpoint, provider, default_model, _ = _resolve_openai_audio_provider_context(tts_settings)
    if endpoint is None:
        return []

    preferred_models = [default_model] + _provider_model_catalog(provider)
    return _dedupe_ordered(preferred_models)


def get_openai_audio_voices_fallback(tts_settings: dict) -> list[str]:
    """Returns built-in voice suggestions for OpenAI-compatible audio providers."""
    endpoint, provider, default_model, default_voice = _resolve_openai_audio_provider_context(tts_settings)
    if endpoint is None:
        return []

    selected_model = str(tts_settings.get("xtts_model") or "").strip() or default_model
    selected_model = _normalize_model_for_provider(selected_model, provider)

    preferred_voices = [default_voice] + _provider_voice_catalog(provider, selected_model)
    return _dedupe_ordered(preferred_voices)


def get_openai_audio_models(tts_settings: dict) -> list[str]:
    """Fetches model IDs from configured OpenAI-compatible audio endpoint."""
    endpoint, provider, default_model, _ = _resolve_openai_audio_provider_context(tts_settings)
    if endpoint is None:
        return []

    models: list[str] = []
    for models_url in _openai_models_urls(endpoint["base_url"]):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(_resolve_openai_audio_api_key(endpoint)),
                timeout=8,
            )
            if response.status_code == 404:
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
            break

    preferred_models = [default_model] + _provider_model_catalog(provider)

    return _merge_catalog_with_discovered(preferred_models, models)


def get_openai_audio_voices(tts_settings: dict) -> list[str]:
    """Fetches voice IDs from configured OpenAI-compatible audio endpoint."""
    endpoint, provider, default_model, default_voice = _resolve_openai_audio_provider_context(tts_settings)
    if endpoint is None:
        return []

    voices: list[str] = []
    for voices_url in _openai_voices_urls(endpoint["base_url"]):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(_resolve_openai_audio_api_key(endpoint)),
                timeout=8,
            )
            if response.status_code == 404:
                continue

            response.raise_for_status()
            voices = [
                _normalize_voice_for_provider(voice, provider)
                for voice in _extract_voices_from_openai_payload(response.json())
            ]
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug("Could not list voices for endpoint '%s': %s", endpoint["name"], e)
            break

    selected_model = str(tts_settings.get("xtts_model") or "").strip()
    if not selected_model:
        selected_model = default_model
    selected_model = _normalize_model_for_provider(selected_model, provider)

    preferred_voices = [default_voice] + _provider_voice_catalog(provider, selected_model)

    return _merge_catalog_with_discovered(preferred_voices, voices)


def check_voxtral_connection(base_url: str = VOXTRAL_API_BASE_URL) -> bool:
    """Checks if the Voxtral server is reachable."""
    normalized_base_url = _normalize_base_url(base_url, VOXTRAL_API_BASE_URL)
    api_key = _resolve_voxtral_api_key()

    probe_urls = [
        f"{normalized_base_url}/health",
        *_voxtral_models_urls(normalized_base_url),
        *_voxtral_voices_urls(normalized_base_url),
    ]

    for probe_url in _dedupe_ordered(probe_urls):
        try:
            response = requests.get(
                probe_url,
                headers=_openai_auth_headers(api_key),
                timeout=4,
            )
            if response.status_code == 404:
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
    for models_url in _voxtral_models_urls(normalized_base_url):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if response.status_code == 404:
                continue

            response.raise_for_status()
            discovered_models = _extract_models_from_voxtral_payload(response.json())
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Voxtral models from %s: %s", models_url, e)
            break

    if discovered_models:
        return _merge_catalog_with_discovered([VOXTRAL_DEFAULT_MODEL], discovered_models)

    preferred_models = [VOXTRAL_DEFAULT_MODEL] + VOXTRAL_TTS_MODELS
    return _dedupe_ordered(preferred_models)


def get_voxtral_voices(base_url: str = VOXTRAL_API_BASE_URL) -> list[str]:
    """Fetches available Voxtral voices from server."""
    normalized_base_url = _normalize_base_url(base_url, VOXTRAL_API_BASE_URL)
    api_key = _resolve_voxtral_api_key()

    discovered_voices: list[str] = []
    for voices_url in _voxtral_voices_urls(normalized_base_url):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
            if response.status_code == 404:
                continue

            response.raise_for_status()
            discovered_voices = _extract_voices_from_voxtral_payload(response.json())
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.error("Failed to list Voxtral voices from %s: %s", voices_url, e)
            break

    return _merge_catalog_with_discovered([VOXTRAL_DEFAULT_VOICE], discovered_voices)


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


# XTTS Functions
def get_xtts_speakers(base_url: str = XTTS_API_BASE_URL) -> list[str]:
    """Fetches discoverable XTTS voice identifiers from server."""
    normalized_base_url = _normalize_base_url(base_url, XTTS_API_BASE_URL)
    discovered_file_ids: list[str] = []
    discoverable_purposes = set(XTTS_DISCOVERABLE_FILE_PURPOSES)

    # Preferred path: OpenAI-compatible files endpoint (/v1/files).
    for purpose in XTTS_DISCOVERABLE_FILE_PURPOSES:
        for files_url in _openai_files_urls(normalized_base_url):
            try:
                response = requests.get(
                    files_url,
                    headers=_openai_auth_headers(),
                    params={"purpose": purpose, "limit": 10000},
                    timeout=8,
                )
                if response.status_code == 404:
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
                break

    # Compatibility path: OpenAI-compatible voices endpoint (/v1/voices).
    discovered_voice_ids: list[str] = []
    for voices_url in _openai_voices_urls(normalized_base_url):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(),
                timeout=8,
            )
            if response.status_code == 404:
                continue

            response.raise_for_status()
            discovered_voice_ids = _extract_voices_from_openai_payload(response.json())
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug("Could not fetch voices from %s: %s", voices_url, e)
            break

    return _dedupe_ordered(discovered_file_ids + discovered_voice_ids)

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
            if response.status_code == 404:
                continue

            response.raise_for_status()
            discovered_models = _extract_models_from_openai_payload(response.json())
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug("Could not fetch models from %s: %s", models_url, e)
            break

    return _merge_catalog_with_discovered([XTTS_DEFAULT_MODEL], discovered_models)


def upload_speaker_voice(
    wav_file_path: str,
    base_url: str = XTTS_API_BASE_URL,
) -> str:
    """Uploads voice to XTTS /v1/files and returns uploaded file ID."""
    if not wav_file_path.lower().endswith(".wav"):
        raise ValueError("Only .wav files are supported for speaker voices.")

    normalized_base_url = _normalize_base_url(base_url, XTTS_API_BASE_URL)
    upload_urls = _openai_files_urls(normalized_base_url)

    try:
        last_response = None
        for upload_url in upload_urls:
            with open(wav_file_path, "rb") as wav_file:
                files = {
                    "file": (
                        os.path.basename(wav_file_path),
                        wav_file,
                        "audio/wav",
                    )
                }
                form_data = {"purpose": XTTS_UPLOAD_FILE_PURPOSE}
                response = requests.post(
                    upload_url,
                    headers=_openai_auth_headers(),
                    files=files,
                    data=form_data,
                    timeout=120,
                )

            if response.status_code == 404:
                last_response = response
                continue

            if response.status_code >= 400:
                raise RuntimeError(
                    f"XTTS voice upload failed ({response.status_code}): {response.text}"
                )

            try:
                payload = response.json()
            except ValueError:
                payload = {}

            uploaded_file_id = str(payload.get("id") or "").strip()
            if not uploaded_file_id:
                raise RuntimeError(
                    "XTTS file upload succeeded but did not return a file ID."
                )

            logging.info(
                "Uploaded XTTS voice file '%s' via OpenAI-compatible endpoint",
                uploaded_file_id,
            )
            return uploaded_file_id

        if last_response is not None and last_response.status_code == 404:
            raise RuntimeError(
                f"XTTS server at {normalized_base_url} does not support OpenAI-compatible /files upload."
            )

        raise RuntimeError("Could not upload speaker voice to XTTS server.")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed uploading voice to XTTS server {normalized_base_url}: {e}") from e
    except OSError as e:
        raise RuntimeError(f"Could not read WAV file for upload: {e}") from e


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
            timeout=120,
        )
        if response.status_code == 404:
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(f"No XTTS speech endpoint could be resolved for '{normalized_base_url}'.")


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
            timeout=120,
        )
        if response.status_code == 404:
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(f"No Voxtral speech endpoint could be resolved for '{normalized_base_url}'.")


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

    payload = _build_openai_compatible_audio_payload(text, tts_settings, endpoint)
    provider = _infer_audio_provider(
        name=endpoint.get("name", ""),
        base_url=endpoint.get("base_url", ""),
        raw_provider=endpoint.get("provider", ""),
    )

    if provider in SUPPORTED_AUDIO_PROVIDERS:
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
    for speech_url in _openai_audio_speech_urls(endpoint["base_url"]):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(api_key),
            json=payload,
            timeout=120,
        )
        if response.status_code == 404:
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(f"No speech endpoint could be resolved for '{endpoint['name']}'.")


def _decode_audio_response(response: requests.Response) -> AudioSegment:
    content_type = (response.headers.get("Content-Type") or "").lower()
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


# Audio Generation
def text_to_audio(
    text: str,
    tts_settings: dict,
    xtts_base_url: str = XTTS_API_BASE_URL,
    voxtral_base_url: str = VOXTRAL_API_BASE_URL,
    silero_base_url: str = SILERO_API_BASE_URL,
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
            elif service == "Voxtral":
                response = _request_voxtral_audio(text, tts_settings, voxtral_base_url)
            elif service in {OPENAI_SERVICE, GEMINI_SERVICE, OPENAI_COMPAT_SERVICE}:
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
                    timeout=120,
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
