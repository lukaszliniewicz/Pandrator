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
GLOBAL_SETTINGS_VERSION = 4
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
    "chatterbox_temperature",
    "chatterbox_repetition_penalty",
    "chatterbox_min_p",
    "chatterbox_top_p",
    "chatterbox_exaggeration",
    "chatterbox_cfg_weight",
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
    "chatterbox_top_k",
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
    "chatterbox_norm_loudness",
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
    "service_configs",
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
    state.tts.service_configs = tts_handler.get_service_configs(state.tts)
    state.tts.provider_configs = tts_handler.get_provider_configs(state.tts)
    tts_payload: Dict[str, Any] = {}
    for field_name in TTS_GLOBAL_FIELDS:
        tts_payload[field_name] = getattr(state.tts, field_name)

    return {
        "llm": {
            "default_model": state.llm.default_model,
            "provider_configs": state.llm.provider_configs,
            "request_timeout_seconds": state.llm.request_timeout_seconds,
            "prompts": {
                "combined": state.llm.combined_prompt.__dict__,
                "first": state.llm.first_prompt.__dict__,
                "second": state.llm.second_prompt.__dict__,
                "third": state.llm.third_prompt.__dict__,
            }
        },
        "tts": tts_payload,
        "source_cleaning": {
            "max_iterations": int(getattr(state.source_cleaning, "max_iterations", SOURCE_CLEANING_DEFAULT_ITERATIONS)),
            "phase_max_iterations": dict(getattr(state.source_cleaning, "phase_max_iterations", {}) or {}),
            "pdf_ocr_mode": str(getattr(state.source_cleaning, "pdf_ocr_mode", "auto") or "auto"),
            "pdf_ocr_language": str(getattr(state.source_cleaning, "pdf_ocr_language", "auto") or "auto"),
            "pdf_ocr_dpi": int(getattr(state.source_cleaning, "pdf_ocr_dpi", 200) or 200),
            "pdf_remove_toc": _coerce_bool(getattr(state.source_cleaning, "pdf_remove_toc", True), True),
            "pdf_remove_repeated_marginals": _coerce_bool(
                getattr(state.source_cleaning, "pdf_remove_repeated_marginals", True), True
            ),
        },
        "text_processing": {
            "remove_footnotes": _coerce_bool(getattr(state.text_processing, "remove_footnotes", False), False),
            "filter_citations": _coerce_bool(getattr(state.text_processing, "filter_citations", True), True),
        },
        "wizard": {
            "show_on_startup": bool(getattr(state.wizard, "show_on_startup", True)),
            "setup_completed_version": int(getattr(state.wizard, "setup_completed_version", 0) or 0),
            "wizard_version": int(getattr(state.wizard, "wizard_version", 1) or 1),
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

        raw_reasoning_effort = str(llm_payload.get("reasoning_effort", "") or "").strip()

        prompts_payload = llm_payload.get("prompts", {})
        if isinstance(prompts_payload, dict):
            for key, attr_name in [
                ("combined", "combined_prompt"),
                ("first", "first_prompt"),
                ("second", "second_prompt"),
                ("third", "third_prompt"),
            ]:
                prompt_data = prompts_payload.get(key)
                if isinstance(prompt_data, dict):
                    prompt_obj = getattr(state.llm, attr_name)
                    if "prompt_text" in prompt_data:
                        prompt_obj.prompt_text = str(prompt_data["prompt_text"])
                    if "enabled" in prompt_data:
                        prompt_obj.enabled = _coerce_bool(prompt_data["enabled"], prompt_obj.enabled)
                    if "evaluation_enabled" in prompt_data:
                        prompt_obj.evaluation_enabled = _coerce_bool(prompt_data["evaluation_enabled"], prompt_obj.evaluation_enabled)
                    if "model" in prompt_data:
                        prompt_obj.model = str(prompt_data["model"])

        llm_handler.normalize_llm_settings(state.llm)
        if raw_reasoning_effort:
            migrated_configs = llm_handler.get_provider_configs(state.llm)
            for provider in migrated_configs:
                for model in provider.get("models", []):
                    if isinstance(model, dict) and not model.get("default_reasoning_effort"):
                        model["default_reasoning_effort"] = raw_reasoning_effort
            state.llm.provider_configs = migrated_configs

    tts_payload = payload.get("tts", {})
    if isinstance(tts_payload, dict):
        service_configs = tts_payload.get("service_configs")
        if isinstance(service_configs, list):
            state.tts.service_configs = service_configs
        elif (
            isinstance(tts_payload.get("provider_configs"), list)
            or isinstance(tts_payload.get("openai_audio_endpoints_json"), str)
        ):
            # Let mixed legacy provider catalogs migrate into first-class services.
            state.tts.service_configs = []

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

        state.tts.service_configs = tts_handler.get_service_configs(state.tts)
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
            from .source_cleaning.pipeline import resolve_phase_max_iterations

            state.source_cleaning.max_iterations = iterations
            raw_phase_iterations = source_cleaning_payload.get("phase_max_iterations")
            state.source_cleaning.phase_max_iterations = resolve_phase_max_iterations(
                raw_phase_iterations if isinstance(raw_phase_iterations, dict) and raw_phase_iterations else None,
                total=iterations,
            )
            state.source_cleaning.max_iterations = sum(state.source_cleaning.phase_max_iterations.values())
            raw_temperature = source_cleaning_payload.get("llm_temperature")
            try:
                temperature = float(raw_temperature) if raw_temperature not in (None, "") else None
            except (TypeError, ValueError):
                temperature = None
            state.source_cleaning.llm_temperature = None
            if temperature is not None and 0.0 <= temperature <= 2.0:
                default_model = llm_handler.normalize_default_model(state.llm.default_model)
                migrated_configs = llm_handler.get_provider_configs(state.llm)
                for provider in migrated_configs:
                    provider_key = str(provider.get("provider") or "openai")
                    provider_id = str(provider.get("id") or "")
                    for model in provider.get("models", []):
                        if not isinstance(model, dict):
                            continue
                        model_id = str(model.get("id") or "")
                        candidate = (
                            f"custom:{provider_id}/{model_id}"
                            if provider.get("is_custom", False)
                            else f"{provider_key}/{model_id}"
                        )
                        if default_model in {candidate, model_id}:
                            if model.get("default_temperature") is None:
                                model["default_temperature"] = temperature
                            state.llm.provider_configs = migrated_configs
                            break
            mode = str(source_cleaning_payload.get("pdf_ocr_mode", "auto") or "auto").lower()
            state.source_cleaning.pdf_ocr_mode = mode if mode in {"auto", "off", "force"} else "auto"
            state.source_cleaning.pdf_ocr_language = str(
                source_cleaning_payload.get("pdf_ocr_language", "auto") or "auto"
            ).lower()
            try:
                dpi = int(source_cleaning_payload.get("pdf_ocr_dpi", 200))
            except (TypeError, ValueError):
                dpi = 200
            state.source_cleaning.pdf_ocr_dpi = max(120, min(400, dpi))
            state.source_cleaning.pdf_remove_toc = _coerce_bool(
                source_cleaning_payload.get("pdf_remove_toc", True), True
            )
            state.source_cleaning.pdf_remove_repeated_marginals = _coerce_bool(
                source_cleaning_payload.get("pdf_remove_repeated_marginals", True), True
            )

    text_processing_payload = payload.get("text_processing")
    if isinstance(text_processing_payload, dict):
        if "remove_footnotes" in text_processing_payload:
            if hasattr(state, "text_processing"):
                state.text_processing.remove_footnotes = _coerce_bool(text_processing_payload["remove_footnotes"], False)
        if "filter_citations" in text_processing_payload:
            if hasattr(state, "text_processing"):
                state.text_processing.filter_citations = _coerce_bool(text_processing_payload["filter_citations"], True)

    wizard_payload = payload.get("wizard")
    if isinstance(wizard_payload, dict) and hasattr(state, "wizard"):
        state.wizard.show_on_startup = _coerce_bool(
            wizard_payload.get("show_on_startup", True), True
        )
        try:
            state.wizard.setup_completed_version = max(
                0, int(wizard_payload.get("setup_completed_version", 0) or 0)
            )
        except (TypeError, ValueError):
            state.wizard.setup_completed_version = 0


def save_global_settings(payload: Dict[str, Any]):
    wrapped = {
        "version": GLOBAL_SETTINGS_VERSION,
        "settings": payload,
    }
    try:
        state_db_handler.save_app_settings(payload, version=GLOBAL_SETTINGS_VERSION)
    except Exception:
        pass

    # Keep a readable compatibility backup, while SQLite remains the load source of truth.
    with _FILE_IO_LOCK:
        _write_json_atomic(get_global_settings_path(), wrapped)


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
