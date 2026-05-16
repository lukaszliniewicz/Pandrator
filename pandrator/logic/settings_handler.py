import json
import os
import tempfile
import threading
from typing import Any, Dict

from ..app_state import AppState
from . import llm_handler

GLOBAL_SETTINGS_FILENAME = "pandrator_settings.json"
GLOBAL_SETTINGS_VERSION = 1
APP_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

TTS_GLOBAL_STR_FIELDS = (
    "openai_audio_endpoint",
    "openai_audio_instructions",
)
TTS_GLOBAL_FLOAT_FIELDS = (
    "temperature",
    "length_penalty",
    "repetition_penalty",
    "top_p",
)
TTS_GLOBAL_INT_FIELDS = (
    "top_k",
    "num_beams",
    "stream_chunk_size",
    "gpt_cond_len",
    "gpt_cond_chunk_len",
    "max_ref_len",
    "overlap_wav_len",
)
TTS_GLOBAL_BOOL_FIELDS = (
    "do_sample",
    "enable_text_splitting",
    "sound_norm_refs",
    "xtts_send_temperature",
    "xtts_send_length_penalty",
    "xtts_send_repetition_penalty",
    "xtts_send_top_k",
    "xtts_send_top_p",
    "xtts_send_do_sample",
    "xtts_send_num_beams",
    "xtts_send_stream_chunk_size",
    "xtts_send_enable_text_splitting",
    "xtts_send_gpt_cond_len",
    "xtts_send_gpt_cond_chunk_len",
    "xtts_send_max_ref_len",
    "xtts_send_sound_norm_refs",
    "xtts_send_overlap_wav_len",
)

TTS_GLOBAL_FIELDS = (
    *TTS_GLOBAL_STR_FIELDS,
    *TTS_GLOBAL_FLOAT_FIELDS,
    *TTS_GLOBAL_INT_FIELDS,
    *TTS_GLOBAL_BOOL_FIELDS,
)

_FILE_IO_LOCK = threading.RLock()


def _write_json_atomic(file_path: str, payload: Any):
    directory = os.path.dirname(file_path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(file_path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, file_path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def get_global_settings_path() -> str:
    return os.path.join(APP_ROOT_DIR, GLOBAL_SETTINGS_FILENAME)


def _coerce_bool(value: Any, default: bool) -> bool:
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


def build_global_settings_payload(state: AppState) -> Dict[str, Any]:
    llm_handler.normalize_llm_settings(state.llm)
    tts_payload: Dict[str, Any] = {}
    for field_name in TTS_GLOBAL_FIELDS:
        tts_payload[field_name] = getattr(state.tts, field_name)

    return {
        "llm": {
            "default_model": state.llm.default_model,
            "provider_configs": state.llm.provider_configs,
            "request_timeout_seconds": state.llm.request_timeout_seconds,
        },
        "tts": tts_payload,
    }


def apply_global_settings_payload(state: AppState, payload: Dict[str, Any]):
    if not isinstance(payload, dict):
        return

    llm_payload = payload.get("llm", {})
    if isinstance(llm_payload, dict):
        default_model = llm_payload.get("default_model")
        if isinstance(default_model, str) and default_model.strip():
            state.llm.default_model = default_model.strip()

        provider_configs = llm_payload.get("provider_configs")
        if isinstance(provider_configs, list):
            state.llm.provider_configs = provider_configs

        legacy_custom_endpoints = llm_payload.get("custom_openai_endpoints_json")
        if isinstance(legacy_custom_endpoints, str):
            setattr(state.llm, "custom_openai_endpoints_json", legacy_custom_endpoints)

        timeout_value = llm_payload.get("request_timeout_seconds")
        try:
            if timeout_value is not None:
                state.llm.request_timeout_seconds = max(10, int(timeout_value))
        except (TypeError, ValueError):
            pass

        llm_handler.normalize_llm_settings(state.llm)

    tts_payload = payload.get("tts", {})
    if isinstance(tts_payload, dict):
        audio_endpoints = tts_payload.get("openai_audio_endpoints_json")
        if isinstance(audio_endpoints, str):
            state.tts.openai_audio_endpoints_json = audio_endpoints

        for field_name in TTS_GLOBAL_STR_FIELDS:
            if field_name in tts_payload and isinstance(tts_payload[field_name], str):
                setattr(state.tts, field_name, tts_payload[field_name])

        for field_name in TTS_GLOBAL_FLOAT_FIELDS:
            if field_name not in tts_payload:
                continue
            try:
                setattr(state.tts, field_name, float(tts_payload[field_name]))
            except (TypeError, ValueError):
                pass

        for field_name in TTS_GLOBAL_INT_FIELDS:
            if field_name not in tts_payload:
                continue
            try:
                setattr(state.tts, field_name, int(tts_payload[field_name]))
            except (TypeError, ValueError):
                pass

        for field_name in TTS_GLOBAL_BOOL_FIELDS:
            if field_name not in tts_payload:
                continue
            current = getattr(state.tts, field_name)
            setattr(state.tts, field_name, _coerce_bool(tts_payload[field_name], current))


def save_global_settings(payload: Dict[str, Any]):
    wrapped = {
        "version": GLOBAL_SETTINGS_VERSION,
        "settings": payload,
    }
    with _FILE_IO_LOCK:
        _write_json_atomic(get_global_settings_path(), wrapped)


def load_global_settings() -> Dict[str, Any]:
    settings_path = get_global_settings_path()
    try:
        with _FILE_IO_LOCK:
            with open(settings_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    if isinstance(payload, dict):
        nested = payload.get("settings")
        if isinstance(nested, dict):
            return nested
        return payload

    return {}
