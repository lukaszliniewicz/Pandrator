import json
import os
import tempfile
import threading
from typing import Any, Dict

from ..app_state import AppState
from . import llm_handler
from . import state_db_handler
from . import tts_handler

GLOBAL_SETTINGS_FILENAME = "pandrator_settings.json"
GLOBAL_SETTINGS_VERSION = 2
APP_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

SOURCE_CLEANING_MIN_ITERATIONS = 1
SOURCE_CLEANING_MAX_ITERATIONS = 250
SOURCE_CLEANING_DEFAULT_ITERATIONS = 53

TTS_GLOBAL_STR_FIELDS = (
    "openai_audio_endpoint",
    "openai_audio_instructions",
    "fishs2_latency",
)
TTS_GLOBAL_FLOAT_FIELDS = (
    "temperature",
    "length_penalty",
    "repetition_penalty",
    "top_p",
    "voxcpm_cfg_value",
    "voxcpm_retry_badcase_ratio_threshold",
    "fishs2_temperature",
    "fishs2_top_p",
    "fishs2_prosody_volume",
)
TTS_GLOBAL_INT_FIELDS = (
    "top_k",
    "num_beams",
    "stream_chunk_size",
    "gpt_cond_len",
    "gpt_cond_chunk_len",
    "max_ref_len",
    "overlap_wav_len",
    "voxcpm_inference_timesteps",
    "voxcpm_retry_badcase_max_times",
    "voxcpm_min_len",
    "voxcpm_max_len",
    "fishs2_chunk_length",
)
TTS_GLOBAL_BOOL_FIELDS = (
    "do_sample",
    "enable_text_splitting",
    "sound_norm_refs",
    "voxcpm_normalize",
    "voxcpm_denoise",
    "voxcpm_retry_badcase",
    "fishs2_normalize",
    "fishs2_normalize_loudness",
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

TTS_GLOBAL_LIST_FIELDS = (
    "provider_configs",
)

TTS_GLOBAL_DICT_FIELDS = (
    "kokoro_default_voices",
)

TTS_GLOBAL_FIELDS = (
    *TTS_GLOBAL_STR_FIELDS,
    *TTS_GLOBAL_FLOAT_FIELDS,
    *TTS_GLOBAL_INT_FIELDS,
    *TTS_GLOBAL_BOOL_FIELDS,
    *TTS_GLOBAL_LIST_FIELDS,
    *TTS_GLOBAL_DICT_FIELDS,
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
    state.tts.provider_configs = tts_handler.get_provider_configs(state.tts)
    tts_payload: Dict[str, Any] = {}
    for field_name in TTS_GLOBAL_FIELDS:
        tts_payload[field_name] = getattr(state.tts, field_name)

    return {
        "llm": {
            "default_model": state.llm.default_model,
            "provider_configs": state.llm.provider_configs,
            "request_timeout_seconds": state.llm.request_timeout_seconds,
            "reasoning_effort": str(getattr(state.llm, "reasoning_effort", "") or ""),
        },
        "tts": tts_payload,
        "source_cleaning": {
            "max_iterations": int(getattr(state.source_cleaning, "max_iterations", SOURCE_CLEANING_DEFAULT_ITERATIONS)),
        },
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

        raw_reasoning_effort = llm_payload.get("reasoning_effort", "")
        if isinstance(raw_reasoning_effort, str) and raw_reasoning_effort in ("", "low", "medium", "high"):
            if hasattr(state.llm, "reasoning_effort"):
                state.llm.reasoning_effort = raw_reasoning_effort

        llm_handler.normalize_llm_settings(state.llm)

    tts_payload = payload.get("tts", {})
    if isinstance(tts_payload, dict):
        provider_configs = tts_payload.get("provider_configs")
        if isinstance(provider_configs, list):
            state.tts.provider_configs = provider_configs

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

        for field_name in TTS_GLOBAL_DICT_FIELDS:
            if field_name not in tts_payload or not isinstance(tts_payload[field_name], dict):
                continue

            if field_name == "kokoro_default_voices":
                normalized_defaults: Dict[str, str] = {}
                for language_value, voice_value in tts_payload[field_name].items():
                    language_code = tts_handler.normalize_kokoro_language_code(language_value)
                    voice_name = str(voice_value or "").strip()
                    if language_code and voice_name:
                        normalized_defaults[language_code] = voice_name
                setattr(state.tts, field_name, normalized_defaults)
            else:
                setattr(state.tts, field_name, dict(tts_payload[field_name]))

        state.tts.provider_configs = tts_handler.get_provider_configs(state.tts)

    source_cleaning_payload = payload.get("source_cleaning")
    if isinstance(source_cleaning_payload, dict):
        raw_iterations = source_cleaning_payload.get("max_iterations", SOURCE_CLEANING_DEFAULT_ITERATIONS)
        try:
            iterations = int(raw_iterations)
        except (TypeError, ValueError):
            iterations = SOURCE_CLEANING_DEFAULT_ITERATIONS
        iterations = max(SOURCE_CLEANING_MIN_ITERATIONS, min(iterations, SOURCE_CLEANING_MAX_ITERATIONS))
        if hasattr(state, "source_cleaning"):
            state.source_cleaning.max_iterations = iterations


def save_global_settings(payload: Dict[str, Any]):
    wrapped = {
        "version": GLOBAL_SETTINGS_VERSION,
        "settings": payload,
    }
    with _FILE_IO_LOCK:
        _write_json_atomic(get_global_settings_path(), wrapped)

    try:
        state_db_handler.save_app_settings(payload, version=GLOBAL_SETTINGS_VERSION)
    except Exception:
        # JSON write-through remains the primary compatibility path in v1.
        pass


def load_global_settings() -> Dict[str, Any]:
    try:
        db_payload = state_db_handler.load_latest_app_settings()
    except Exception:
        db_payload = {}

    if isinstance(db_payload, dict) and db_payload:
        return db_payload

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
            try:
                state_db_handler.save_app_settings(nested, version=GLOBAL_SETTINGS_VERSION)
            except Exception:
                pass
            return nested
        try:
            state_db_handler.save_app_settings(payload, version=GLOBAL_SETTINGS_VERSION)
        except Exception:
            pass
        return payload

    return {}
