import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import copy
from dataclasses import fields, is_dataclass

from pydub import AudioSegment
from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from .app_state import AppState
from .logic import (
    session_handler,
    settings_handler,
    config_handler,
    file_handler,
    text_preprocessor,
    llm_handler,
    tts_handler,
    rvc_handler,
    audio_processor,
    subdub_handler,
    xtts_trainer_handler,
)
from .logic.playback_handler import PlaybackHandler


DUBBING_DEEPL_PROVIDER_ID = "deepl"
LEGACY_DUBBING_MODEL_ALIASES: dict[str, str] = {
    "gpt 5.4": "openai/gpt-5.4",
    "gpt 5.4-mini": "openai/gpt-5.4-mini",
    "gemini 3.1 pro": "gemini/gemini-3.1-pro-preview",
    "gemini 3.0 flash": "gemini/gemini-3-flash-preview",
    "opus 4.7": "anthropic/claude-opus-4-7",
    "sonnet 4.6": "anthropic/claude-sonnet-4-6",
    "haiku": "anthropic/claude-3-5-haiku-20241022",
    "sonnet": "anthropic/claude-3-5-sonnet-20241022",
    "sonnet thinking": "anthropic/claude-3-5-sonnet-20241022",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "gemini-flash": "gemini/gemini-2.0-flash",
    "gemini-pro": "gemini/gemini-1.5-pro",
    "deepseek-r1": "openrouter/deepseek/deepseek-r1",
    "qwq-32b": "openrouter/qwen/qwq-32b",
}

KNOWN_LITELLM_PROVIDER_KEYS = {
    "openai",
    "anthropic",
    "gemini",
    "openrouter",
    "ollama",
    "groq",
    "mistral",
    "vertex_ai",
    "azure",
    "bedrock",
}


class AppLogic(QObject):
    """
    Main controller for the application.
    It holds the application state and connects the GUI to the backend logic.
    """
    # Signals to notify the GUI of changes
    state_changed = pyqtSignal()
    log_message = pyqtSignal(str)
    show_error = pyqtSignal(str, str)  # title, message
    progress_updated = pyqtSignal(int, int, float) # current, total, elapsed_time
    xtts_training_running_changed = pyqtSignal(bool)
    xtts_training_status_updated = pyqtSignal(str)
    xtts_training_progress_updated = pyqtSignal(int)
    tts_connection_running_changed = pyqtSignal(bool)
    _tts_connection_result = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state_lock = threading.RLock()
        self.state = AppState()
        self._last_global_settings_snapshot = ""
        self._load_global_settings()

        self.playback_handler = PlaybackHandler()
        self.generation_thread = None
        self.regeneration_thread = None
        self.xtts_training_thread = None
        self._tts_connection_thread = None
        self.stop_generation_flag = threading.Event()
        self.cancel_generation_flag = threading.Event()
        self._loaded_llm_model = None
        self.log_file_path = ""
        rvc_handler.initialize_rvc("rvc_models")

        # Playlist attributes
        self.playlist_sentences = []
        self.current_playlist_index = 0
        self.playlist_active = False
        self.current_playing_sentence_number = None
        self.playlist_timer = QTimer()
        self.playlist_timer.timeout.connect(self._check_playlist_status)
        self.playlist_timer.setInterval(250) # Check every 250ms

        self._last_session_config_snapshot = ""
        self._session_persist_timer = QTimer(self)
        self._session_persist_timer.timeout.connect(self._persist_session_config)
        self._session_persist_timer.setInterval(1500)
        self._session_persist_timer.start()

        self._global_settings_persist_timer = QTimer(self)
        self._global_settings_persist_timer.timeout.connect(self._persist_global_settings)
        self._global_settings_persist_timer.setInterval(1500)
        self._global_settings_persist_timer.start()

        self._tts_connection_result.connect(self._apply_tts_connection_result)

        logging.info("AppLogic initialized.")

    def set_log_file_path(self, path: str):
        """Sets the path to the log file."""
        self.log_file_path = path

    def _update_and_notify(self, **kwargs):
        """Helper to update state attributes and emit state_changed signal."""
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
            else:
                logging.warning(f"Attempted to set non-existent AppState attribute: {key}")
        self.state_changed.emit()

    def _is_named_session_active(self) -> bool:
        session_name = self.state.session_name
        return bool(session_name and session_name != "Untitled Session")

    @staticmethod
    def _is_dubbing_source_extension(extension: str) -> bool:
        return extension in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".srt"}

    @staticmethod
    def _normalize_subtitle_sentence_text(text: str) -> str:
        normalized = re.sub(r"\r\n?", "\n", str(text or ""))
        normalized = re.sub(r"\s*\n+\s*", " ", normalized)
        normalized = re.sub(r"[ \t]{2,}", " ", normalized)
        return normalized.strip()

    def _is_dubbing_source_selected(self) -> bool:
        source_path = self.state.source_file_path or ""
        source_ext = os.path.splitext(source_path)[1].lower()
        return self._is_dubbing_source_extension(source_ext)

    def is_dubbing_mode_active(self) -> bool:
        """Returns True when the selected source uses dubbing workflow."""
        return self._is_dubbing_source_selected()

    def _get_dubbing_work_dir(self, ensure_exists: bool = False) -> str:
        staging_dir = session_handler.get_dubbing_staging_path(self.state.session_name)
        if ensure_exists:
            os.makedirs(staging_dir, exist_ok=True)
        return staging_dir

    def _get_primary_sentence_wavs_dir(self, ensure_exists: bool = False) -> str:
        session_dir = session_handler.get_session_path(self.state.session_name)
        if self._is_dubbing_source_selected():
            wavs_dir = os.path.join(self._get_dubbing_work_dir(ensure_exists=ensure_exists), "Sentence_wavs")
        else:
            wavs_dir = os.path.join(session_dir, "Sentence_wavs")

        if ensure_exists:
            os.makedirs(wavs_dir, exist_ok=True)
        return wavs_dir

    def _get_candidate_sentence_wavs_dirs(self) -> list[str]:
        session_dir = session_handler.get_session_path(self.state.session_name)
        root_wavs_dir = os.path.join(session_dir, "Sentence_wavs")
        if not self._is_dubbing_source_selected():
            return [root_wavs_dir]

        staging_wavs_dir = os.path.join(self._get_dubbing_work_dir(ensure_exists=False), "Sentence_wavs")
        candidates: list[str] = []
        for path in (staging_wavs_dir, root_wavs_dir):
            if path not in candidates:
                candidates.append(path)
        return candidates

    def _resolve_dubbing_video_source(self) -> str:
        video_source = self.state.source_file_path or ""
        if os.path.splitext(video_source)[1].lower() != ".srt":
            return video_source

        discovered_video = (
            self.state.dubbing.video_file_path
            or session_handler.discover_video_file(self.state.session_name)
        )
        if discovered_video and not self.state.dubbing.video_file_path:
            self.state.dubbing.video_file_path = discovered_video

        return discovered_video or ""

    def _is_generation_running(self) -> bool:
        return bool(self.generation_thread and self.generation_thread.is_alive())

    def _is_regeneration_running(self) -> bool:
        return bool(self.regeneration_thread and self.regeneration_thread.is_alive())

    def _is_xtts_training_running(self) -> bool:
        return bool(self.xtts_training_thread and self.xtts_training_thread.is_alive())

    def is_generation_running(self) -> bool:
        """Public lifecycle helper for UI state decisions."""
        return self._is_generation_running()

    def is_regeneration_running(self) -> bool:
        """Public lifecycle helper for UI state decisions."""
        return self._is_regeneration_running()

    def is_generation_or_regeneration_running(self) -> bool:
        """Returns True while any generation workflow is active."""
        return self._is_generation_running() or self._is_regeneration_running()

    def is_xtts_training_running(self) -> bool:
        """Returns True when XTTS training is active."""
        return self._is_xtts_training_running()

    def is_tts_connection_running(self) -> bool:
        """Returns True while a TTS connection check is in progress."""
        return bool(self._tts_connection_thread and self._tts_connection_thread.is_alive())

    def get_processed_sentences_snapshot(self) -> list[dict]:
        """Returns a thread-safe snapshot of processed sentences."""
        with self._state_lock:
            return copy.deepcopy(self.state.processed_sentences)

    def _set_processed_sentences_snapshot(self, sentences: list[dict], persist: bool = True):
        """Replaces processed sentence state from a snapshot and optionally persists it."""
        snapshot = copy.deepcopy(sentences)
        with self._state_lock:
            self.state.processed_sentences = snapshot
            session_name = self.state.session_name

        if persist and session_name and session_name != "Untitled Session":
            session_handler.save_sentences(session_name, snapshot)

    def get_lifecycle_status(self) -> str:
        """Returns a user-facing lifecycle status string."""
        if self._is_generation_running():
            if self.cancel_generation_flag.is_set():
                return "Cancelling"
            if self.stop_generation_flag.is_set():
                return "Stopping"
            return "Generating"

        if self._is_regeneration_running():
            return "Regenerating"

        return "Idle"

    def _normalize_prompt_model(self, model_name: str | None) -> str | None:
        normalized = (model_name or "").strip()
        if not normalized or normalized == "default":
            return None
        return normalized

    def _ensure_llm_model_loaded(self, model_name: str | None) -> bool:
        normalized_model = self._normalize_prompt_model(model_name)
        if not normalized_model:
            return True

        if self._loaded_llm_model == normalized_model:
            return True

        if llm_handler.load_model(normalized_model, llm_settings=self.state.llm):
            self._loaded_llm_model = normalized_model
            return True

        return False

    @staticmethod
    def _normalize_provider_id(raw_value: str | None) -> str:
        lowered = str(raw_value or "").strip().lower()
        return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")

    def list_dubbing_translation_provider_configs(self) -> list[dict]:
        """Returns available translation providers (DeepL + configured LLM providers)."""
        providers: list[dict] = [
            {
                "id": DUBBING_DEEPL_PROVIDER_ID,
                "name": "DeepL",
                "provider": DUBBING_DEEPL_PROVIDER_ID,
                "is_custom": False,
                "models": [],
            }
        ]
        providers.extend(self.list_llm_provider_configs())
        return copy.deepcopy(providers)

    def list_dubbing_translation_models(self, provider_id: str) -> list[str]:
        """Returns configured translation models for a provider id."""
        normalized_provider_id = self._normalize_provider_id(provider_id)
        if normalized_provider_id == DUBBING_DEEPL_PROVIDER_ID:
            return []

        for provider in self.list_llm_provider_configs():
            provider_item_id = self._normalize_provider_id(provider.get("id"))
            if provider_item_id != normalized_provider_id:
                continue

            models = provider.get("models", [])
            if not isinstance(models, list):
                return []
            return [
                str(model).strip()
                for model in models
                if str(model).strip()
            ]

        return []

    def _infer_dubbing_provider_from_model(
        self,
        model_name: str,
        provider_configs: list[dict],
    ) -> tuple[str, str]:
        normalized_model = str(model_name or "").strip()
        if not normalized_model:
            return "", ""

        if normalized_model.lower().startswith("custom:"):
            remainder = normalized_model[len("custom:") :].strip()
            provider_part, separator, model_part = remainder.partition("/")
            inferred_provider_id = self._normalize_provider_id(provider_part)
            if separator and inferred_provider_id and model_part.strip():
                return inferred_provider_id, model_part.strip()

        if "/" not in normalized_model:
            return "", normalized_model

        prefix, remainder = normalized_model.split("/", 1)
        prefix = prefix.strip()
        remainder = remainder.strip()
        if not remainder:
            return "", normalized_model

        providers_by_id = {
            self._normalize_provider_id(provider.get("id")): provider
            for provider in provider_configs
            if self._normalize_provider_id(provider.get("id"))
        }

        direct_provider_id = self._normalize_provider_id(prefix)
        if direct_provider_id in providers_by_id:
            return direct_provider_id, remainder

        provider_key = prefix.lower()
        if provider_key not in KNOWN_LITELLM_PROVIDER_KEYS:
            return "", normalized_model

        matching_provider_ids: list[str] = []
        for provider_id, provider in providers_by_id.items():
            provider_type = str(provider.get("provider") or "").strip().lower()
            if provider_type == provider_key:
                matching_provider_ids.append(provider_id)

        if not matching_provider_ids:
            return "", normalized_model

        for provider_id in matching_provider_ids:
            provider_models = self.list_dubbing_translation_models(provider_id)
            if remainder in provider_models:
                return provider_id, remainder

        if len(matching_provider_ids) == 1:
            return matching_provider_ids[0], remainder

        preferred_order = [
            "anthropic",
            "openai",
            "gemini",
        ]
        for preferred in preferred_order:
            if preferred in matching_provider_ids:
                return preferred, remainder

        return matching_provider_ids[0], remainder

    def normalize_dubbing_translation_state(self, dubbing_state=None):
        """Normalizes dubbing translation provider/model selections and migrates legacy values."""
        target_state = dubbing_state or self.state.dubbing
        provider_configs = self.list_llm_provider_configs()
        providers_by_id = {
            self._normalize_provider_id(provider.get("id")): provider
            for provider in provider_configs
            if self._normalize_provider_id(provider.get("id"))
        }

        provider_id = self._normalize_provider_id(
            getattr(target_state, "translation_provider", "")
        )
        selected_model = str(getattr(target_state, "translation_model", "") or "").strip()
        custom_model = str(getattr(target_state, "custom_translation_model", "") or "").strip()
        custom_api_base = str(getattr(target_state, "custom_api_base", "") or "").strip()
        provider_was_fallback = False
        retain_custom_api_base = False

        legacy_key = selected_model.lower()
        if legacy_key in LEGACY_DUBBING_MODEL_ALIASES:
            selected_model = LEGACY_DUBBING_MODEL_ALIASES[legacy_key]
        elif legacy_key in {"custom (litellm)", "custom"} and custom_model:
            selected_model = custom_model
            retain_custom_api_base = bool(custom_api_base)
        elif legacy_key == "local":
            selected_model = (
                custom_model
                or os.environ.get("PANDRATOR_SUBDUB_LOCAL_MODEL", "")
                or llm_handler.DEFAULT_LITELLM_MODEL
            )
            retain_custom_api_base = bool(custom_api_base)

        if legacy_key == "deepl":
            provider_id = DUBBING_DEEPL_PROVIDER_ID
            selected_model = ""

        if (not provider_id or provider_id not in providers_by_id) and selected_model:
            inferred_provider_id, inferred_model = self._infer_dubbing_provider_from_model(
                selected_model,
                provider_configs,
            )
            if inferred_provider_id:
                provider_id = inferred_provider_id
                selected_model = inferred_model

        if provider_id != DUBBING_DEEPL_PROVIDER_ID and provider_id not in providers_by_id:
            provider_was_fallback = True
            for fallback_provider_id in ("anthropic", "openai", "gemini"):
                if fallback_provider_id in providers_by_id:
                    provider_id = fallback_provider_id
                    break
            else:
                provider_id = next(iter(providers_by_id), DUBBING_DEEPL_PROVIDER_ID)

        if provider_id == DUBBING_DEEPL_PROVIDER_ID:
            selected_model = ""
        elif provider_id in providers_by_id:
            provider = providers_by_id[provider_id]
            provider_key = str(provider.get("provider") or "").strip().lower()

            if selected_model.lower().startswith("models/"):
                selected_model = selected_model.split("/", 1)[1].strip()

            if "/" in selected_model:
                prefix, remainder = selected_model.split("/", 1)
                if prefix.strip().lower() == provider_key and remainder.strip():
                    selected_model = remainder.strip()

            available_models = self.list_dubbing_translation_models(provider_id)
            if (
                provider_was_fallback
                and available_models
                and selected_model not in available_models
            ):
                preferred_fallback_model = subdub_handler.DEFAULT_DUBBING_MODEL_ID
                if preferred_fallback_model in available_models:
                    selected_model = preferred_fallback_model
                else:
                    selected_model = available_models[0]

            if not selected_model:
                if available_models:
                    selected_model = available_models[0]
                elif provider_key:
                    default_litellm_model = llm_handler.normalize_default_model(
                        self.state.llm.default_model
                    )
                    if "/" in default_litellm_model:
                        default_prefix, default_model = default_litellm_model.split("/", 1)
                        if default_prefix.strip().lower() == provider_key and default_model.strip():
                            selected_model = default_model.strip()

                if not selected_model:
                    selected_model = subdub_handler.DEFAULT_DUBBING_MODEL_ID

        if getattr(target_state, "translation_provider", "") != provider_id:
            setattr(target_state, "translation_provider", provider_id)
        if getattr(target_state, "translation_model", "") != selected_model:
            setattr(target_state, "translation_model", selected_model)
        if not retain_custom_api_base and custom_api_base:
            setattr(target_state, "custom_api_base", "")

    def _normalize_tts_service_state(self, tts_state=None):
        target_state = tts_state or self.state.tts
        service = str(target_state.service or "").strip()
        service_lower = service.lower()
        endpoint = str(target_state.openai_audio_endpoint or "").strip()
        target_state.provider_configs = tts_handler.get_provider_configs(target_state)
        available_provider_ids = [
            str(item.get("id") or "").strip()
            for item in target_state.provider_configs
            if str(item.get("id") or "").strip()
        ]

        cloud_services = {
            tts_handler.OPENAI_COMPAT_SERVICE.lower(),
            tts_handler.OPENAI_SERVICE.lower(),
            tts_handler.GEMINI_SERVICE.lower(),
        }
        if service_lower in cloud_services:
            target_state.service = tts_handler.OPENAI_COMPAT_SERVICE
            if service_lower == tts_handler.OPENAI_SERVICE.lower():
                endpoint = tts_handler.OPENAI_PROVIDER
            elif service_lower == tts_handler.GEMINI_SERVICE.lower():
                endpoint = tts_handler.GEMINI_PROVIDER

            normalized_endpoint = re.sub(r"[^a-z0-9]+", "-", endpoint.lower()).strip("-")
            if normalized_endpoint in available_provider_ids:
                endpoint = normalized_endpoint

            if endpoint not in available_provider_ids:
                if tts_handler.OPENAI_PROVIDER in available_provider_ids:
                    endpoint = tts_handler.OPENAI_PROVIDER
                elif available_provider_ids:
                    endpoint = available_provider_ids[0]
                else:
                    endpoint = tts_handler.OPENAI_PROVIDER

            target_state.openai_audio_endpoint = endpoint
            return

        supported_services = {
            "XTTS",
            "Voxtral",
            "Silero",
            tts_handler.OPENAI_COMPAT_SERVICE,
        }
        if service not in supported_services:
            target_state.service = "XTTS"

    def _load_global_settings(self):
        """Loads app-wide provider settings from JSON."""
        try:
            payload = settings_handler.load_global_settings()
            settings_handler.apply_global_settings_payload(self.state, payload)
            llm_handler.normalize_llm_settings(self.state.llm)
            self._normalize_tts_service_state(self.state.tts)
            self.normalize_dubbing_translation_state(self.state.dubbing)

            snapshot_payload = settings_handler.build_global_settings_payload(self.state)
            self._last_global_settings_snapshot = json.dumps(
                snapshot_payload,
                sort_keys=True,
                ensure_ascii=False,
            )
        except Exception as e:
            logging.error("Failed to load global settings: %s", e, exc_info=True)
            self._last_global_settings_snapshot = ""

    def _persist_global_settings(self, force: bool = False):
        """Persists app-wide provider settings to JSON."""
        try:
            payload = settings_handler.build_global_settings_payload(self.state)
            snapshot = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if not force and snapshot == self._last_global_settings_snapshot:
                return

            settings_handler.save_global_settings(payload)
            self._last_global_settings_snapshot = snapshot
        except Exception as e:
            logging.error("Failed to persist global settings: %s", e, exc_info=True)

    def _persist_session_config(self, force: bool = False):
        """Persists current settings/source-related state for the active session."""
        if not self._is_named_session_active():
            self._last_session_config_snapshot = ""
            return

        try:
            payload = session_handler.build_session_config_payload(self.state)
            snapshot = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if not force and snapshot == self._last_session_config_snapshot:
                return

            session_handler.save_session_config(self.state.session_name, payload)
            self._last_session_config_snapshot = snapshot
        except Exception as e:
            logging.error(f"Failed to persist session config for '{self.state.session_name}': {e}", exc_info=True)

    def _apply_dataclass_state(self, dataclass_obj, payload: dict):
        """Applies dictionary values onto a dataclass recursively."""
        if not isinstance(payload, dict):
            return

        for field_info in fields(dataclass_obj):
            field_name = field_info.name
            if field_name not in payload:
                continue

            new_value = payload[field_name]
            current_value = getattr(dataclass_obj, field_name)

            if is_dataclass(current_value) and isinstance(new_value, dict):
                self._apply_dataclass_state(current_value, new_value)
            else:
                setattr(dataclass_obj, field_name, new_value)

    def _apply_saved_state(self, target_state: AppState, payload: dict):
        """Merges persisted session state payload into AppState."""
        if not isinstance(payload, dict):
            return

        for key, value in payload.items():
            if key in {"processed_sentences", "metadata", "raw_text"}:
                continue

            if not hasattr(target_state, key):
                continue

            current_value = getattr(target_state, key)
            if is_dataclass(current_value) and isinstance(value, dict):
                self._apply_dataclass_state(current_value, value)
            else:
                setattr(target_state, key, value)

    def _ensure_session_file_copy(self, file_path: str) -> str:
        """Ensures a source/support file is available inside the active session directory."""
        source_abs = os.path.abspath(file_path)
        session_dir = session_handler.get_session_path(self.state.session_name)
        os.makedirs(session_dir, exist_ok=True)

        source_dir_abs = os.path.abspath(os.path.dirname(source_abs))
        session_dir_abs = os.path.abspath(session_dir)
        if os.path.normcase(source_dir_abs) == os.path.normcase(session_dir_abs):
            return source_abs

        base_name = os.path.basename(source_abs)
        destination = os.path.join(session_dir, base_name)
        destination_abs = os.path.abspath(destination)

        if os.path.normcase(destination_abs) == os.path.normcase(source_abs):
            return destination_abs

        if os.path.exists(destination_abs):
            stem, ext = os.path.splitext(base_name)
            suffix = 1
            while True:
                candidate = os.path.join(session_dir, f"{stem}_{suffix}{ext}")
                if not os.path.exists(candidate):
                    destination_abs = candidate
                    break
                suffix += 1

        shutil.copy2(source_abs, destination_abs)
        return destination_abs

    def should_warn_before_source_change(self, selected_file_path: str | None = None) -> bool:
        """Returns True when source switching should show a destructive warning."""
        if not self._is_named_session_active():
            return False

        selected_abs = os.path.abspath(selected_file_path) if selected_file_path else ""
        current_abs = os.path.abspath(self.state.source_file_path) if self.state.source_file_path else ""
        if selected_abs and current_abs and os.path.normcase(current_abs) == os.path.normcase(selected_abs):
            return False

        if self.get_processed_sentences_snapshot():
            return True

        return session_handler.session_has_generated_artifacts(self.state.session_name)

    def _stage_source_file_for_session_reset(self, file_path: str) -> tuple[str, str]:
        """Stages a source file to a temp location when it lives in the active session directory."""
        source_abs = os.path.abspath(file_path)
        session_dir_abs = os.path.abspath(session_handler.get_session_path(self.state.session_name))
        source_dir_abs = os.path.abspath(os.path.dirname(source_abs))

        if os.path.normcase(source_dir_abs) != os.path.normcase(session_dir_abs):
            return source_abs, ""

        temp_dir = tempfile.mkdtemp(prefix="pandrator_source_")
        staged_source_path = os.path.join(temp_dir, os.path.basename(source_abs))
        shutil.copy2(source_abs, staged_source_path)
        return staged_source_path, temp_dir

    def _reset_active_session_for_source_change(self):
        """Removes all persisted artifacts for the active session."""
        session_name = self.state.session_name
        session_handler.clear_session_contents(session_name)

        session_dir = session_handler.get_session_path(session_name)
        os.makedirs(os.path.join(session_dir, "Sentence_wavs"), exist_ok=True)

        self.stop_playback()
        self.state.source_file_path = ""
        self.state.raw_text = ""
        self.state.pdf_preprocessed = False
        self.state.cover_image_path = None
        self.state.dubbing.video_file_path = ""
        self.state.metadata = {"title": "", "album": "", "artist": "", "genre": "", "language": ""}
        self._set_processed_sentences_snapshot([], persist=True)
        session_handler.save_metadata(session_name, self.state.metadata)
        self._last_session_config_snapshot = ""

    def _try_load_raw_text_for_source(self, source_path: str, session_name: str | None = None) -> str:
        """Best-effort loading of textual source content for session restore."""
        if not source_path or not os.path.exists(source_path):
            return ""

        ext = os.path.splitext(source_path)[1].lower()
        try:
            if ext == ".txt":
                with open(source_path, "r", encoding="utf-8") as f:
                    return f.read()
            if ext == ".epub":
                return file_handler.extract_text_from_epub(source_path)
            if ext == ".pdf":
                return file_handler.extract_text_from_pdf(source_path)
            if ext in [".docx", ".mobi"]:
                target_session = session_name or self.state.session_name
                if target_session:
                    output_dir = session_handler.get_session_path(target_session)
                    os.makedirs(output_dir, exist_ok=True)
                    stem = os.path.splitext(os.path.basename(source_path))[0]
                    output_txt_path = os.path.join(output_dir, f"{stem}.txt")
                    if file_handler.convert_doc_to_text(source_path, output_txt_path):
                        with open(output_txt_path, "r", encoding="utf-8") as f:
                            return f.read()
        except Exception as e:
            logging.warning(f"Could not reload text from source file '{source_path}': {e}")

        return ""

    # --- Session Management ---

    def new_session(self, session_name: str):
        """Creates a new, empty session."""
        if self.is_generation_or_regeneration_running():
            self.show_error.emit("Error", "Wait for generation/regeneration to finish before creating a new session.")
            return

        if session_handler.session_exists(session_name):
            # In a real app, we'd ask the user if they want to overwrite.
            # For now, we'll just log it. A dialog can be triggered from the GUI.
            logging.warning(f"Session '{session_name}' already exists. Overwriting.")

        current_tts_state = self.state.tts
        provider_settings_payload = settings_handler.build_global_settings_payload(self.state)
        self.state = AppState(session_name=session_name)
        self.state.tts = current_tts_state
        settings_handler.apply_global_settings_payload(self.state, provider_settings_payload)
        self._normalize_tts_service_state(self.state.tts)
        self.normalize_dubbing_translation_state(self.state.dubbing)
        self._loaded_llm_model = None
        session_dir = session_handler.get_session_path(self.state.session_name)
        os.makedirs(session_dir, exist_ok=True)
        sentence_wavs_dir = os.path.join(session_dir, "Sentence_wavs")
        if os.path.isdir(sentence_wavs_dir):
            shutil.rmtree(sentence_wavs_dir)
        os.makedirs(sentence_wavs_dir, exist_ok=True)

        session_handler.save_metadata(self.state.session_name, self.state.metadata)
        session_handler.save_sentences(self.state.session_name, [])
        self._persist_global_settings(force=True)
        self._persist_session_config(force=True)
        self.log_message.emit(f"New session created: {session_name}")
        self.state_changed.emit()

    def load_session(self, session_name: str):
        """Loads a session from disk."""
        if self.is_generation_or_regeneration_running():
            self.show_error.emit("Error", "Wait for generation/regeneration to finish before loading another session.")
            return

        if not session_handler.session_exists(session_name):
            self.show_error.emit("Error", f"Session '{session_name}' not found.")
            return

        current_tts_models = list(self.state.tts.tts_models)
        current_tts_speakers = list(self.state.tts.tts_speakers)

        sentences = session_handler.load_sentences(session_name)
        if not sentences:
            speech_blocks_file = session_handler.discover_latest_speech_blocks_file(session_name)
            if speech_blocks_file:
                try:
                    sentences = session_handler.import_speech_blocks_to_session(session_name, speech_blocks_file)
                    logging.info(
                        "Recovered %d sentence(s) from speech blocks during session load: %s",
                        len(sentences),
                        speech_blocks_file,
                    )
                except Exception as e:
                    logging.warning(
                        "Could not recover sentences from speech blocks while loading session '%s': %s",
                        session_name,
                        e,
                    )

        metadata = session_handler.load_metadata(session_name)
        saved_state_payload = session_handler.load_session_config(session_name)
        if isinstance(saved_state_payload, dict):
            llm_payload = saved_state_payload.get("llm")
            if isinstance(llm_payload, dict):
                for global_llm_key in (
                    "default_model",
                    "provider_configs",
                    "request_timeout_seconds",
                    "custom_openai_endpoints_json",
                    "unload_after_sentence",
                ):
                    llm_payload.pop(global_llm_key, None)

        restored_state = AppState(
            session_name=session_name,
            processed_sentences=sentences,
            metadata=metadata,
        )
        settings_handler.apply_global_settings_payload(
            restored_state,
            settings_handler.build_global_settings_payload(self.state),
        )
        self._apply_saved_state(restored_state, saved_state_payload)
        llm_handler.normalize_llm_settings(restored_state.llm)
        self._normalize_tts_service_state(restored_state.tts)
        self.normalize_dubbing_translation_state(restored_state.dubbing)

        restored_state.session_name = session_name
        restored_state.processed_sentences = sentences
        restored_state.metadata = metadata

        if restored_state.source_file_path and not os.path.isabs(restored_state.source_file_path):
            relative_source = os.path.join(session_handler.get_session_path(session_name), restored_state.source_file_path)
            if os.path.exists(relative_source):
                restored_state.source_file_path = os.path.abspath(relative_source)

        if not restored_state.source_file_path or not os.path.exists(restored_state.source_file_path):
            discovered_source = session_handler.discover_source_file(session_name)
            if discovered_source:
                restored_state.source_file_path = discovered_source

        if restored_state.source_file_path and not restored_state.raw_text:
            restored_state.raw_text = self._try_load_raw_text_for_source(
                restored_state.source_file_path,
                session_name=session_name,
            )

        source_ext = os.path.splitext(restored_state.source_file_path)[1].lower() if restored_state.source_file_path else ""
        if source_ext == ".srt":
            video_path = restored_state.dubbing.video_file_path
            if video_path and not os.path.isabs(video_path):
                relative_video = os.path.join(session_handler.get_session_path(session_name), video_path)
                if os.path.exists(relative_video):
                    video_path = os.path.abspath(relative_video)
                    restored_state.dubbing.video_file_path = video_path

            if not video_path or not os.path.exists(video_path):
                discovered_video = session_handler.discover_video_file(session_name)
                if discovered_video:
                    restored_state.dubbing.video_file_path = discovered_video

        restored_state.tts.tts_models = current_tts_models
        restored_state.tts.tts_speakers = current_tts_speakers

        self.state = restored_state
        if self.state.tts.service in {
            tts_handler.OPENAI_SERVICE,
            tts_handler.GEMINI_SERVICE,
            tts_handler.OPENAI_COMPAT_SERVICE,
        } and not self.state.tts.tts_models:
            self.populate_cloud_tts_catalogs(use_remote=False, emit_state=False)

        self._loaded_llm_model = None
        self._persist_global_settings(force=True)
        self._persist_session_config(force=True)
        self.log_message.emit(f"Session loaded: {session_name}")
        self.state_changed.emit()

    def delete_session(self, session_name: str):
        """Deletes a session from disk."""
        if session_name == self.state.session_name and self._is_generation_running():
            self.show_error.emit("Error", "Stop or cancel generation before deleting the active session.")
            return

        if session_name == self.state.session_name and self._is_regeneration_running():
            self.show_error.emit("Error", "Wait for sentence regeneration to finish before deleting the active session.")
            return

        if session_handler.delete_session(session_name):
            self.log_message.emit(f"Session '{session_name}' deleted.")
            # Reset state if the deleted session was the active one
            if self.state.session_name == session_name:
                current_tts_state = self.state.tts
                provider_settings_payload = settings_handler.build_global_settings_payload(self.state)
                self.state = AppState() # Reset to default state
                self.state.tts = current_tts_state
                settings_handler.apply_global_settings_payload(self.state, provider_settings_payload)
                self._normalize_tts_service_state(self.state.tts)
                self.normalize_dubbing_translation_state(self.state.dubbing)
                self._loaded_llm_model = None
                self._last_session_config_snapshot = ""
                self._persist_global_settings(force=True)
                self.state_changed.emit()
        else:
            self.show_error.emit("Error", f"Could not delete session '{session_name}'.")

    def view_session_folder(self):
        """Opens the current session folder in the file explorer."""
        import platform
        session_name = self.state.session_name
        if session_name and session_name != "Untitled Session":
            session_dir = session_handler.get_session_path(session_name)
            if os.path.isdir(session_dir):
                system = platform.system()
                if system == "Windows":
                    os.startfile(session_dir)
                elif system == "Darwin": # macOS
                    subprocess.Popen(["open", session_dir])
                else: # Linux
                    subprocess.Popen(["xdg-open", session_dir])
            else:
                self.show_error.emit("Error", f"Session folder for '{session_name}' not found.")
        else:
            self.show_error.emit("Information", "No active session folder to view.")


    # --- File Handling ---

    def run_pdf_crop_tool(self, pdf_file_path: str) -> str:
        """Best-effort integration with PyCropPDF for manual PDF cleanup."""
        script_candidates = [
            os.path.abspath(os.path.join("PyCropPDF", "pycroppdf.py")),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "PyCropPDF", "pycroppdf.py")),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "PyCropPDF", "pycroppdf.py")),
        ]
        script_path = next((candidate for candidate in script_candidates if os.path.isfile(candidate)), "")
        if not script_path:
            self.log_message.emit("PyCropPDF not found. Continuing with the original PDF.")
            return pdf_file_path

        source_dir = os.path.dirname(pdf_file_path)
        source_filename = os.path.splitext(os.path.basename(pdf_file_path))[0]
        cropped_filename = f"{source_filename}_cropped.pdf"
        cropped_path = os.path.join(source_dir, cropped_filename)

        try:
            self.log_message.emit("Opening PyCropPDF. Save and close it when finished.")
            process = subprocess.Popen(
                [
                    sys.executable,
                    script_path,
                    "--input",
                    os.path.abspath(pdf_file_path),
                    "--save-to",
                    source_dir,
                    "--save-as",
                    cropped_filename,
                ],
                cwd=os.path.dirname(script_path),
            )
            process.wait()
        except Exception as e:
            logging.error("Failed to run PyCropPDF: %s", e, exc_info=True)
            self.show_error.emit("PDF Crop Error", f"Could not launch PyCropPDF: {e}")
            return pdf_file_path

        if os.path.exists(cropped_path) and os.path.getsize(cropped_path) > 0:
            self.log_message.emit(f"Using cropped PDF: {cropped_path}")
            return cropped_path

        self.log_message.emit("No cropped PDF was saved. Using the original PDF.")
        return pdf_file_path

    def select_source_file(self, file_path: str, reset_session: bool = False) -> bool:
        """Processes a newly selected source file."""
        if self.is_generation_or_regeneration_running():
            self.show_error.emit("Error", "Cannot change source file while generation/regeneration is running.")
            return False

        if not self._is_named_session_active():
            self.show_error.emit("No Session", "Please create or load a session before selecting a source file.")
            return False

        normalize_path = lambda path: os.path.normcase(os.path.abspath(path)) if path else ""
        previous_source = self.state.source_file_path
        previous_raw_text = self.state.raw_text
        previous_pdf_preprocessed = self.state.pdf_preprocessed
        previous_dubbing_video = self.state.dubbing.video_file_path

        source_path_to_load = file_path
        staging_cleanup_path = ""
        reset_applied = False

        try:
            if reset_session:
                source_path_to_load, staging_cleanup_path = self._stage_source_file_for_session_reset(file_path)
                self._reset_active_session_for_source_change()
                reset_applied = True
                self.log_message.emit("Cleared existing session artifacts before loading a new source.")

            session_file_path = self._ensure_session_file_copy(source_path_to_load)
            self.state.source_file_path = session_file_path
            self.state.pdf_preprocessed = False
            self.log_message.emit(f"Source file selected: {session_file_path}")

            raw_text = ""
            ext = os.path.splitext(session_file_path)[1].lower()
            session_path = session_handler.get_session_path(self.state.session_name)

            if ext == ".txt":
                with open(session_file_path, "r", encoding="utf-8") as f:
                    raw_text = f.read()
            elif ext == ".epub":
                raw_text = file_handler.extract_text_from_epub(session_file_path)
                converted_txt_path = os.path.join(
                    session_path,
                    f"{os.path.splitext(os.path.basename(session_file_path))[0]}.txt",
                )
                with open(converted_txt_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(raw_text)
                self.state.source_file_path = converted_txt_path
                self.log_message.emit(f"EPUB converted to text: {os.path.basename(converted_txt_path)}")
            elif ext == ".pdf":
                raw_text = file_handler.extract_text_from_pdf(session_file_path)
                raw_text_filename = f"{os.path.splitext(os.path.basename(session_file_path))[0]}_raw_text.txt"
                raw_text_path = os.path.join(session_path, raw_text_filename)
                with open(raw_text_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(raw_text)
                self.log_message.emit(f"PDF raw text saved: {os.path.basename(raw_text_path)}")
            elif ext in [".docx", ".mobi"]:
                stem = os.path.splitext(os.path.basename(session_file_path))[0]
                output_txt_path = os.path.join(session_path, f"{stem}.txt")
                if file_handler.convert_doc_to_text(session_file_path, output_txt_path):
                    with open(output_txt_path, "r", encoding="utf-8") as f:
                        raw_text = f.read()
                    self.state.source_file_path = output_txt_path
                    self.log_message.emit(f"Document converted to text: {os.path.basename(output_txt_path)}")
                else:
                    raise IOError("Failed to convert document to text.")
            elif ext in [".srt", ".mp4", ".mkv", ".webm", ".avi", ".mov"]:
                raw_text = ""
                if ext in [".mp4", ".mkv", ".webm", ".avi", ".mov"]:
                    self.state.dubbing.video_file_path = session_file_path
            else:
                raise ValueError(f"Unsupported source file type: {ext or 'unknown'}")

            source_changed = normalize_path(previous_source) != normalize_path(self.state.source_file_path)
            if source_changed:
                self._set_processed_sentences_snapshot([])

            self.state.raw_text = raw_text
            if raw_text:
                self.log_message.emit("Text extracted successfully.")

            self._persist_session_config(force=True)
            self.state_changed.emit()
            return True

        except Exception as e:
            if reset_applied:
                self.state.source_file_path = ""
                self.state.raw_text = ""
                self.state.pdf_preprocessed = False
                self.state.dubbing.video_file_path = ""
                self._set_processed_sentences_snapshot([], persist=True)
                self._persist_session_config(force=True)
                self.state_changed.emit()
            else:
                self.state.source_file_path = previous_source
                self.state.raw_text = previous_raw_text
                self.state.pdf_preprocessed = previous_pdf_preprocessed
                self.state.dubbing.video_file_path = previous_dubbing_video

            logging.error(f"Failed to process source file {file_path}: {e}", exc_info=True)
            self.show_error.emit("File Error", f"Could not process the selected file: {e}")
            return False
        finally:
            if staging_cleanup_path and os.path.exists(staging_cleanup_path):
                try:
                    if os.path.isdir(staging_cleanup_path):
                        shutil.rmtree(staging_cleanup_path)
                    else:
                        os.remove(staging_cleanup_path)
                except OSError as cleanup_error:
                    logging.warning("Could not remove temporary source staging path '%s': %s", staging_cleanup_path, cleanup_error)

    def apply_reviewed_text(self, reviewed_text: str, mark_pdf_preprocessed: bool = False) -> bool:
        """Persists reviewed/edited text and switches the source to the edited file."""
        if self.is_generation_or_regeneration_running():
            self.show_error.emit("Error", "Cannot apply reviewed text while generation/regeneration is running.")
            return False

        if not self._is_named_session_active():
            self.show_error.emit("No Session", "Please create or load a session before applying reviewed text.")
            return False

        session_dir = session_handler.get_session_path(self.state.session_name)
        edited_file_path = os.path.join(session_dir, f"{self.state.session_name}_edited.txt")

        try:
            os.makedirs(session_dir, exist_ok=True)
            with open(edited_file_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(reviewed_text)

            self.state.source_file_path = edited_file_path
            self.state.raw_text = reviewed_text
            self.state.pdf_preprocessed = mark_pdf_preprocessed
            self._set_processed_sentences_snapshot([])

            self.log_message.emit(f"Reviewed text saved: {edited_file_path}")
            self._persist_session_config(force=True)
            self.state_changed.emit()
            return True
        except Exception as e:
            logging.error("Failed to apply reviewed text: %s", e, exc_info=True)
            self.show_error.emit("Review Error", f"Could not save reviewed text: {e}")
            return False

    def clear_source_file_selection(self):
        """Clears the current source file and related text state."""
        if not self._is_named_session_active():
            return

        self.state.source_file_path = ""
        self.state.raw_text = ""
        self.state.pdf_preprocessed = False
        self._set_processed_sentences_snapshot([])
        self.log_message.emit("Source file selection cleared.")
        self._persist_session_config(force=True)
        self.state_changed.emit()

    def save_pasted_text(self, text: str, mark_paragraphs: bool, reset_session: bool = False):
        """Saves pasted text to a file and updates the state."""
        if self.is_generation_or_regeneration_running():
            self.show_error.emit("Error", "Cannot paste new text while generation/regeneration is running.")
            return

        if not self._is_named_session_active():
            self.show_error.emit("No Session", "Please create or load a session before pasting text.")
            return

        session_dir = session_handler.get_session_path(self.state.session_name)
        try:
            if reset_session:
                self._reset_active_session_for_source_change()
                self.log_message.emit("Cleared existing session artifacts before loading pasted text.")

            file_path = file_handler.save_pasted_text(text, session_dir, mark_paragraphs)
            # This re-uses the file selection logic to load the text and update the UI
            if self.select_source_file(file_path):
                self.log_message.emit("Pasted text saved and loaded.")
        except Exception as e:
            logging.error(f"Failed to save pasted text: {e}", exc_info=True)
            self.show_error.emit("Paste Error", f"Could not save pasted text: {e}")

    def select_cover_image(self, file_path: str):
        """Updates the cover image path in the state."""
        self.state.cover_image_path = file_path
        self.log_message.emit(f"Cover image selected: {file_path}")
        self._persist_session_config(force=True)
        self.state_changed.emit()

    def select_dubbing_video_file(self, file_path: str):
        """Sets the video file path for dubbing when source is SRT."""
        if self.is_generation_or_regeneration_running():
            self.show_error.emit("Error", "Cannot change dubbing video while generation/regeneration is running.")
            return

        if not self._is_named_session_active():
            self.show_error.emit("No Session", "Please create or load a session before selecting a dubbing video.")
            return

        try:
            session_video_path = self._ensure_session_file_copy(file_path)
            self.state.dubbing.video_file_path = session_video_path
            self.log_message.emit(f"Dubbing video file selected: {session_video_path}")
            self._persist_session_config(force=True)
            self.state_changed.emit()
        except Exception as e:
            logging.error(f"Failed to set dubbing video file {file_path}: {e}", exc_info=True)
            self.show_error.emit("File Error", f"Could not prepare dubbing video file: {e}")

    def download_from_url(self, url: str, reset_session: bool = False):
        """Downloads a video from a URL in a background thread."""
        if self.is_generation_or_regeneration_running():
            self.show_error.emit("Error", "Cannot download a new source while generation/regeneration is running.")
            return

        if not self._is_named_session_active():
            self.show_error.emit("No Session", "Please create or load a session before downloading from URL.")
            return

        def thread_target():
            self.log_message.emit(f"Starting download from {url}...")
            session_dir = session_handler.get_session_path(self.state.session_name)
            os.makedirs(session_dir, exist_ok=True)
            try:
                video_path = file_handler.download_video_from_url(url, session_dir)
                self.log_message.emit(f"Download complete: {video_path}")
                # Switch to main thread to update state and UI
                self.select_source_file(video_path, reset_session=reset_session)
            except Exception as e:
                logging.error(f"Failed to download from URL: {e}", exc_info=True)
                self.show_error.emit("Download Error", f"Could not download video: {e}")

        threading.Thread(target=thread_target, daemon=True).start()

    # --- Text Processing ---

    def _find_latest_srt(self, session_dir: str, must_not_be_equalized=False) -> str | None:
        """Finds the most recently modified SRT file in a preferred directory with session fallback."""
        search_dirs: list[str] = []
        if session_dir:
            search_dirs.append(session_dir)

        root_session_dir = session_handler.get_session_path(self.state.session_name)
        if root_session_dir and root_session_dir not in search_dirs:
            search_dirs.append(root_session_dir)

        srt_files: list[tuple[str, float, int]] = []
        for priority, directory in enumerate(search_dirs):
            if not os.path.isdir(directory):
                continue

            for file_name in os.listdir(directory):
                file_name_lower = file_name.lower()
                if not file_name_lower.endswith(".srt"):
                    continue
                if must_not_be_equalized and file_name_lower.endswith("_equalized.srt"):
                    continue

                full_path = os.path.join(directory, file_name)
                if not os.path.isfile(full_path):
                    continue

                srt_files.append((full_path, os.path.getmtime(full_path), -priority))

        if not srt_files:
            return None

        latest_srt, _, _ = max(srt_files, key=lambda item: (item[1], item[2], item[0].lower()))
        return latest_srt

    def _snapshot_speech_blocks_files(self, session_dir: str) -> dict[str, float]:
        """Takes a timestamp snapshot of speech-block JSON files in a session directory."""
        if not os.path.isdir(session_dir):
            return {}

        snapshot: dict[str, float] = {}
        for name in os.listdir(session_dir):
            if not name.lower().endswith("_speech_blocks.json"):
                continue
            full_path = os.path.join(session_dir, name)
            if os.path.isfile(full_path):
                snapshot[full_path] = os.path.getmtime(full_path)
        return snapshot

    def _discover_latest_file_with_suffix(self, directory: str, suffix: str) -> str | None:
        """Finds the most recently modified file with a given suffix in a directory."""
        if not os.path.isdir(directory):
            return None

        candidates: list[str] = []
        normalized_suffix = suffix.lower()
        for name in os.listdir(directory):
            if not name.lower().endswith(normalized_suffix):
                continue

            full_path = os.path.join(directory, name)
            if os.path.isfile(full_path):
                candidates.append(full_path)

        if not candidates:
            return None

        return max(candidates, key=lambda path: (os.path.getmtime(path), path.lower()))

    def _wait_for_speech_blocks_file(
        self,
        session_dir: str,
        source_srt_file: str | None,
        previous_snapshot: dict[str, float],
        timeout_seconds: int = 60,
    ) -> str | None:
        """Waits for a new or updated speech-block JSON file and returns its path."""
        preferred_path = None
        if source_srt_file:
            source_base = os.path.splitext(os.path.basename(source_srt_file))[0]
            preferred_path = os.path.join(session_dir, f"{source_base}_speech_blocks.json")

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            candidates: list[str] = []
            if preferred_path:
                candidates.append(preferred_path)

            latest_session_file = self._discover_latest_file_with_suffix(
                session_dir,
                "_speech_blocks.json",
            )
            if latest_session_file and latest_session_file not in candidates:
                candidates.append(latest_session_file)

            for candidate in candidates:
                if not candidate or not os.path.exists(candidate):
                    continue
                mtime = os.path.getmtime(candidate)
                previous_mtime = previous_snapshot.get(candidate)
                if previous_mtime is None or mtime > previous_mtime:
                    return candidate

            time.sleep(0.5)

        if preferred_path and os.path.exists(preferred_path):
            return preferred_path
        return self._discover_latest_file_with_suffix(session_dir, "_speech_blocks.json")

    def _import_speech_blocks_into_sentences(self, speech_blocks_file: str) -> bool:
        """Converts Subdub speech blocks JSON into Pandrator sentence JSON/state."""
        try:
            sentences = session_handler.import_speech_blocks_to_session(self.state.session_name, speech_blocks_file)
            if not sentences:
                self.show_error.emit("Dubbing Error", "Speech blocks file is empty. No sentences were imported.")
                return False

            self._set_processed_sentences_snapshot(sentences, persist=False)
            with self._state_lock:
                self.state.raw_text = ""
            self.log_message.emit(
                f"Imported {len(sentences)} speech blocks from {os.path.basename(speech_blocks_file)}."
            )
            return True
        except FileNotFoundError:
            self.show_error.emit("Dubbing Error", "Speech blocks file was not found after generation.")
            return False
        except json.JSONDecodeError as e:
            self.show_error.emit("Dubbing Error", f"Speech blocks JSON could not be parsed: {e}")
            return False
        except ValueError as e:
            self.show_error.emit("Dubbing Error", f"Speech blocks format is invalid: {e}")
            return False
        except Exception as e:
            logging.error(f"Failed to import speech blocks from '{speech_blocks_file}': {e}", exc_info=True)
            self.show_error.emit("Dubbing Error", f"Could not import speech blocks: {e}")
            return False

    def _run_threaded_task(self, target, *args):
        """Helper to run a function in a background thread."""
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        return thread

    def run_dubbing_task(self, task: str):
        """Runs a dubbing-related task like transcribe, translate, etc., in a background thread."""
        if self.is_generation_or_regeneration_running():
            self.log_message.emit("Cannot run dubbing tasks while generation/regeneration is active.")
            return

        self.normalize_dubbing_translation_state(self.state.dubbing)

        self._run_threaded_task(self._run_dubbing_task_thread, task)

    def _run_dubbing_task_thread(self, task: str):
        """The actual threaded implementation for dubbing tasks."""
        self.log_message.emit(f"Starting dubbing task: {task}")
        session_output_dir = session_handler.get_session_path(self.state.session_name)
        dubbing_session_dir = self._get_dubbing_work_dir(ensure_exists=True)
        dub_settings_payload = copy.deepcopy(self.state.dubbing.__dict__)
        dub_settings_payload["llm_provider_configs"] = llm_handler.get_provider_configs(self.state.llm)
        correction_prompt = str(dub_settings_payload.get("custom_correction_prompt", ""))

        if task == "transcribe":
            video_source = self._resolve_dubbing_video_source()

            if not video_source or not os.path.exists(video_source):
                self.log_message.emit("No video source found for transcription.")
                return

            subdub_handler.transcribe_video(
                dubbing_session_dir,
                video_source,
                dub_settings_payload,
                correction_prompt,
            )
        elif task == "correct":
            srt_file = self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True)
            if srt_file:
                subdub_handler.correct_subtitles(
                    dubbing_session_dir,
                    srt_file,
                    dub_settings_payload,
                    correction_prompt,
                )
            else:
                self.log_message.emit("No SRT file found to correct.")
        elif task == "translate":
            srt_file = self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True)
            if srt_file:
                subdub_handler.translate_subtitles(
                    dubbing_session_dir,
                    srt_file,
                    dub_settings_payload,
                    correction_prompt,
                )
            else:
                self.log_message.emit("No SRT file found to translate.")
        elif task == "generate_audio":
            self._orchestrate_dubbing_audio_generation(
                dubbing_session_dir,
                dub_settings_payload,
                correction_prompt,
            )
        elif task == "add_to_video":
            self._orchestrate_add_to_video(dubbing_session_dir, session_output_dir)
        else:
            self.log_message.emit(f"Unknown dubbing task: {task}")

    def _orchestrate_dubbing_audio_generation(self, session_dir, dub_settings: dict, correction_prompt):
        """Full workflow to generate dubbing audio from a video."""
        # 1. Transcribe if no SRT exists
        srt_file = self._find_latest_srt(session_dir, must_not_be_equalized=True)
        if not srt_file:
            self.log_message.emit("No SRT file found, starting transcription...")
            video_source = self._resolve_dubbing_video_source()

            if not video_source or not os.path.exists(video_source):
                self.show_error.emit("Dubbing Error", "No valid video file found for transcription.")
                return

            if not subdub_handler.transcribe_video(session_dir, video_source, dub_settings, correction_prompt):
                self.show_error.emit("Dubbing Error", "Transcription failed. Check logs for details.")
                return
            srt_file = self._find_latest_srt(session_dir, must_not_be_equalized=True)
            if not srt_file:
                self.show_error.emit("Dubbing Error", "Transcription did not produce an SRT file.")
                return

        # 2. Translate if enabled
        if bool(dub_settings.get("translation_enabled")):
            self.log_message.emit("Translation is enabled, starting translation...")
            if not subdub_handler.translate_subtitles(session_dir, srt_file, dub_settings, correction_prompt):
                self.show_error.emit("Dubbing Error", "Translation failed. Check logs for details.")
                return
            srt_file = self._find_latest_srt(session_dir, must_not_be_equalized=True) # Find the new translated file
            if not srt_file:
                self.show_error.emit("Dubbing Error", "Translation did not produce a new SRT file.")
                return
        
        # 3. Generate speech blocks
        speech_blocks_snapshot = self._snapshot_speech_blocks_files(session_dir)
        self.log_message.emit("Generating speech blocks...")
        if not subdub_handler.generate_speech_blocks(session_dir, srt_file):
            self.show_error.emit("Dubbing Error", "Speech block generation failed.")
            return

        speech_blocks_file = self._wait_for_speech_blocks_file(
            session_dir=session_dir,
            source_srt_file=srt_file,
            previous_snapshot=speech_blocks_snapshot,
        )
        if not speech_blocks_file:
            self.show_error.emit("Dubbing Error", "Speech blocks file was not detected after generation.")
            return

        # 4. Convert speech blocks to Pandrator sentence JSON
        self.log_message.emit("Converting speech blocks into session sentences...")
        if not self._import_speech_blocks_into_sentences(speech_blocks_file):
            return

        self.state_changed.emit()

        # 5. Start audio generation
        self.start_generation()

    def _orchestrate_add_to_video(self, dubbing_session_dir: str, session_output_dir: str):
        """Full workflow to synchronize audio and add subtitles."""
        video_source = self._resolve_dubbing_video_source()
        if not video_source or not os.path.exists(video_source):
            self.show_error.emit("Dubbing Error", "No valid video file found for synchronization.")
            return

        # 1. Synchronize Audio
        self.log_message.emit("Synchronizing audio...")
        if not subdub_handler.synchronize_audio(dubbing_session_dir, video_file=video_source):
            self.show_error.emit("Dubbing Error", "Audio synchronization failed.")
            return
        
        # Find synchronized output video.
        # Legacy Subdub used a `_synced` suffix, while the refactored pipeline writes `final_output*.mp4`.
        synced_video_path = None
        candidate_videos = []
        for file in os.listdir(dubbing_session_dir):
            file_lower = file.lower()
            if not file_lower.endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
                continue
            if "_synced" in file_lower or file_lower.startswith("final_output"):
                candidate_videos.append(os.path.join(dubbing_session_dir, file))

        if candidate_videos:
            synced_video_path = max(candidate_videos, key=os.path.getmtime)

        if not synced_video_path:
             self.show_error.emit("Dubbing Error", "Synced video file not found after synchronization.")
             return
        self.log_message.emit(f"Found synced video: {synced_video_path}")

        # 2. Equalize Subtitles
        srt_file = self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True)
        if not srt_file:
            self.show_error.emit("Dubbing Error", "Cannot find SRT file to equalize.")
            return
        self.log_message.emit(f"Equalizing subtitles for {srt_file}...")
        if not subdub_handler.equalize_subtitles(srt_file):
            self.show_error.emit("Dubbing Error", "Subtitle equalization failed.")
            return
            
        equalized_srt_path = srt_file.replace('.srt', '_equalized.srt')
        if not os.path.exists(equalized_srt_path):
            base_name = os.path.splitext(os.path.basename(srt_file))[0]
            expected_path = os.path.join(dubbing_session_dir, f"{base_name}_equalized.srt")
            if os.path.exists(expected_path):
                equalized_srt_path = expected_path
            else:
                 self.show_error.emit("Dubbing Error", "Equalized SRT file not found after equalization.")
                 return
        self.log_message.emit(f"Found equalized SRT: {equalized_srt_path}")

        # 3. Add Subtitles to Video
        os.makedirs(session_output_dir, exist_ok=True)
        output_video_path = os.path.join(session_output_dir, f"{self.state.session_name}_final.mp4")
        self.log_message.emit(f"Adding subtitles to video, output will be at {output_video_path}")
        if not subdub_handler.add_subtitles_to_video(synced_video_path, equalized_srt_path, output_video_path):
            self.show_error.emit("Dubbing Error", "Failed to add subtitles to the final video.")
            return

        self.log_message.emit(f"Dubbing workflow finished. Final output: {output_video_path}")

    def run_text_preprocessing(self):
        """Runs the text preprocessor on the raw text in the state."""
        if not self.state.raw_text:
            self.show_error.emit("Error", "No text to process. Please select a source file.")
            return

        settings = {
            "pdf_preprocessed": self.state.pdf_preprocessed,
            "source_file": self.state.source_file_path,
            "disable_paragraph_detection": self.state.text_processing.disable_paragraph_detection,
            "language": self.state.tts.language,
            "max_sentence_length": self.state.text_processing.max_sentence_length,
            "enable_sentence_splitting": self.state.text_processing.enable_sentence_splitting,
            "enable_sentence_appending": self.state.text_processing.enable_sentence_appending,
            "remove_diacritics": self.state.text_processing.remove_diacritics,
            "tts_service": self.state.tts.service
        }
        
        try:
            processed_sentences = text_preprocessor.preprocess_text(self.state.raw_text, settings)
            self._set_processed_sentences_snapshot(processed_sentences)
            self.log_message.emit("Text preprocessing complete.")
            self.state_changed.emit()
        except Exception as e:
            logging.error(f"Text preprocessing failed: {e}", exc_info=True)
            self.show_error.emit("Preprocessing Error", str(e))

    # --- Generation ---
    
    def start_generation(self):
        """Starts the audio generation worker thread, running preprocessing if needed."""
        if self._is_generation_running():
            self.log_message.emit("Generation is already running.")
            return

        if self._is_regeneration_running():
            self.log_message.emit("Wait for sentence regeneration to finish before starting full generation.")
            return
        
        has_processed_sentences = bool(self.get_processed_sentences_snapshot())
        if not self.state.raw_text and not has_processed_sentences:
            self.show_error.emit("Error", "No text to process. Please select a source file first.")
            return

        self._persist_session_config(force=True)

        self.log_message.emit("Starting audio generation process...")
        self.stop_generation_flag.clear()
        self.cancel_generation_flag.clear()
        self.state_changed.emit()
        
        # The worker thread will handle finding the start index and preprocessing.
        self.generation_thread = threading.Thread(target=self._generation_thread_worker, daemon=True)
        self.generation_thread.start()

    def stop_generation(self):
        """Stops the audio generation worker thread after the current sentence."""
        if not self._is_generation_running():
            self.log_message.emit("No generation is currently running.")
            return

        if self.cancel_generation_flag.is_set():
            self.log_message.emit("Cancellation already requested. Waiting for current sentence to finish.")
            return

        if self.stop_generation_flag.is_set():
            self.log_message.emit("Stop already requested. Waiting for current sentence to finish.")
            return

        self.log_message.emit("Stopping generation after current sentence...")
        self.stop_generation_flag.set()
        self.state_changed.emit()

    def cancel_generation(self):
        """Cancels generation and cleans up generated files."""
        if not self._is_generation_running():
            self.log_message.emit("No generation is currently running to cancel.")
            return

        if self.cancel_generation_flag.is_set():
            self.log_message.emit("Cancellation already requested. Waiting for cleanup.")
            return

        self.log_message.emit("Cancelling generation after current sentence and scheduling cleanup...")
        self.cancel_generation_flag.set()
        self.stop_generation_flag.set()
        self.state_changed.emit()

    def _cleanup_cancelled_generation(self):
        """Removes sentence JSON file and all generated WAVs."""
        if self._is_generation_running() and threading.current_thread() is not self.generation_thread:
            self.log_message.emit("Cleanup deferred until generation reaches a safe stop point.")
            return

        self.log_message.emit("Cleaning up cancelled generation files...")
        session_path = session_handler.get_session_path(self.state.session_name)
        
        json_path = os.path.join(session_path, f"{self.state.session_name}_sentences.json")
        if os.path.exists(json_path):
            try:
                os.remove(json_path)
            except OSError as e:
                self.show_error.emit("Cleanup Error", f"Could not remove sentences file: {e}")

        wavs_cleanup_failed = False
        for wavs_dir in self._get_candidate_sentence_wavs_dirs():
            if not os.path.isdir(wavs_dir):
                continue
            try:
                shutil.rmtree(wavs_dir)
                os.makedirs(wavs_dir, exist_ok=True)
            except OSError as e:
                wavs_cleanup_failed = True
                self.show_error.emit("Cleanup Error", f"Could not clear WAVs directory '{wavs_dir}': {e}")

        if self._is_dubbing_source_selected() and not wavs_cleanup_failed:
            os.makedirs(self._get_primary_sentence_wavs_dir(ensure_exists=False), exist_ok=True)

        self._set_processed_sentences_snapshot([], persist=False)
        self.state_changed.emit()
        self.progress_updated.emit(0, 1, 0.0)
        self.log_message.emit("Cleanup complete.")

    def _generation_thread_worker(self):
        """The main worker loop for generating audio for all sentences."""
        cleanup_requested = False
        worker_thread = threading.current_thread()

        try:
            # Step 1: Preprocess text if sentences don't exist
            processed_sentences = self.get_processed_sentences_snapshot()
            if not processed_sentences:
                if not self.state.raw_text:
                    self.show_error.emit("Error", "No text to process. Please select a source file.")
                    return
                self.log_message.emit("No processed sentences found. Running preprocessor...")
                self.run_text_preprocessing()
                if self.cancel_generation_flag.is_set():
                    cleanup_requested = True
                    self.log_message.emit("Cancellation requested during preprocessing.")
                    return
                processed_sentences = self.get_processed_sentences_snapshot()
                if not processed_sentences:
                    # Error is shown by run_text_preprocessing on failure
                    return

            # Step 2: Find where to start/resume generation
            start_index = next(
                (i for i, s in enumerate(processed_sentences) if s.get("tts_generated") != "yes"),
                len(processed_sentences),
            )

            total_sentences = len(processed_sentences)
            if start_index >= total_sentences:
                self.log_message.emit("All sentences have already been generated.")
                return

            self.log_message.emit(f"Starting generation from sentence {start_index + 1}...")
            sentence_times = []
            start_time = time.time()

            for i in range(start_index, total_sentences):
                if self.stop_generation_flag.is_set():
                    if self.cancel_generation_flag.is_set():
                        cleanup_requested = True
                        self.log_message.emit("Cancellation acknowledged. Cleaning up generated artifacts...")
                    else:
                        self.log_message.emit("Generation stopped by user.")
                    return

                sentence_dict = processed_sentences[i]
                if sentence_dict.get("tts_generated") == "yes":
                    continue

                sentence_start_time = time.time()
                self.log_message.emit(f"Generating sentence {i+1}/{total_sentences}...")

                success, updated_sentence = self._execute_generation_for_sentence(sentence_dict)
                if not success or updated_sentence is None:
                    if self.cancel_generation_flag.is_set():
                        cleanup_requested = True
                        self.log_message.emit("Generation cancelled during sentence processing. Cleaning up...")
                    else:
                        self.show_error.emit("Generation Error", f"Failed to generate audio for sentence {i+1}. Aborting.")
                    return

                processed_sentences[i] = updated_sentence
                self._set_processed_sentences_snapshot(processed_sentences)
                self.state_changed.emit()

                sentence_end_time = time.time()
                sentence_times.append(sentence_end_time - sentence_start_time)
                elapsed = sentence_end_time - start_time
                self.progress_updated.emit(i + 1, total_sentences, elapsed)

            self.log_message.emit("Audio generation finished.")

            if self.cancel_generation_flag.is_set():
                cleanup_requested = True
                self.log_message.emit("Cancellation requested during finalization. Cleaning up...")
                return

            ext = os.path.splitext(self.state.source_file_path)[1].lower() if self.state.source_file_path else ""
            is_dubbing_workflow = self._is_dubbing_source_extension(ext)

            if not is_dubbing_workflow:
                self.log_message.emit("Saving final output file...")
                output_format = self.state.audio_processing.output_format
                output_path = os.path.join(
                    session_handler.get_session_path(self.state.session_name),
                    f"{self.state.session_name}.{output_format}",
                )
                self.save_output(output_path)
        except Exception as e:
            logging.error("Unhandled generation worker error: %s", e, exc_info=True)
            self.show_error.emit("Generation Error", f"Unexpected generation failure: {e}")
        finally:
            if cleanup_requested:
                self._cleanup_cancelled_generation()

            self.stop_generation_flag.clear()
            self.cancel_generation_flag.clear()

            if self.generation_thread is worker_thread:
                self.generation_thread = None

            self.state_changed.emit()

    # --- Metadata ---
    def save_metadata(self, metadata: dict):
        """Updates metadata in the state and saves it to disk."""
        self.state.metadata = metadata
        try:
            session_handler.save_metadata(self.state.session_name, self.state.metadata)
            self.log_message.emit("Metadata saved.")
        except Exception as e:
            self.show_error.emit("Metadata Error", f"Could not save metadata: {e}")

    # --- TTS ---
    def populate_cloud_tts_catalogs(
        self,
        use_remote: bool = False,
        provider_id: str | None = None,
        preferred_model: str | None = None,
        allow_unknown_model: bool = False,
        emit_state: bool = True,
    ) -> bool:
        """Populates OpenAI/Gemini model and voice choices, optionally using remote discovery."""
        service = str(self.state.tts.service or "").strip()
        if service not in {
            tts_handler.OPENAI_SERVICE,
            tts_handler.GEMINI_SERVICE,
            tts_handler.OPENAI_COMPAT_SERVICE,
        }:
            return False

        tts_snapshot = copy.deepcopy(self.state.tts.__dict__)
        tts_snapshot["provider_configs"] = tts_handler.get_provider_configs(tts_snapshot)

        if provider_id is not None:
            tts_snapshot["openai_audio_endpoint"] = str(provider_id).strip()

        if service == tts_handler.OPENAI_SERVICE:
            tts_snapshot["openai_audio_endpoint"] = tts_handler.OPENAI_PROVIDER
        elif service == tts_handler.GEMINI_SERVICE:
            tts_snapshot["openai_audio_endpoint"] = tts_handler.GEMINI_PROVIDER

        endpoint, endpoint_error = tts_handler.resolve_openai_audio_endpoint(tts_snapshot)
        if endpoint is None:
            logging.warning("Could not resolve cloud TTS endpoint: %s", endpoint_error)
            return False

        tts_snapshot["openai_audio_endpoint"] = endpoint["name"]
        provider = str(endpoint.get("provider") or "").strip().lower()
        if provider == tts_handler.GEMINI_PROVIDER:
            default_model_fallback = tts_handler.GEMINI_AUDIO_DEFAULT_MODEL
            default_voice_fallback = tts_handler.GEMINI_AUDIO_DEFAULT_VOICE
        else:
            default_model_fallback = tts_handler.OPENAI_AUDIO_DEFAULT_MODEL
            default_voice_fallback = tts_handler.OPENAI_AUDIO_DEFAULT_VOICE

        if use_remote:
            models = tts_handler.get_openai_audio_models(tts_snapshot)
        else:
            models = tts_handler.get_openai_audio_models_fallback(tts_snapshot)

        default_model = endpoint.get("default_model") or default_model_fallback
        selected_model = (preferred_model or tts_snapshot.get("xtts_model") or "").strip()
        if models:
            if selected_model not in models:
                if not (allow_unknown_model and selected_model):
                    selected_model = default_model if default_model in models else models[0]
        elif not selected_model:
            selected_model = default_model

        tts_snapshot["xtts_model"] = selected_model

        if use_remote:
            speakers = tts_handler.get_openai_audio_voices(tts_snapshot)
        else:
            speakers = tts_handler.get_openai_audio_voices_fallback(tts_snapshot)

        default_voice = endpoint.get("default_voice") or default_voice_fallback
        selected_speaker = (tts_snapshot.get("speaker") or "").strip()
        if speakers:
            if selected_speaker not in speakers:
                selected_speaker = default_voice if default_voice in speakers else speakers[0]
        elif not selected_speaker:
            selected_speaker = default_voice

        with self._state_lock:
            self.state.tts.openai_audio_endpoint = endpoint["name"]
            self.state.tts.tts_models = models
            self.state.tts.tts_speakers = speakers
            self.state.tts.xtts_model = selected_model
            self.state.tts.speaker = selected_speaker

        if emit_state:
            self.state_changed.emit()

        return True

    def connect_tts_server(self):
        """Starts a non-blocking connect attempt for the active TTS service."""
        if self.is_tts_connection_running():
            self.log_message.emit("TTS connection is already in progress.")
            return

        tts_snapshot = copy.deepcopy(self.state.tts.__dict__)
        requested_service = tts_snapshot.get("service") or self.state.tts.service

        self.log_message.emit(f"Connecting to {requested_service} service...")
        self.tts_connection_running_changed.emit(True)

        self._tts_connection_thread = threading.Thread(
            target=self._connect_tts_server_thread,
            args=(tts_snapshot,),
            daemon=True,
        )
        self._tts_connection_thread.start()

    def _connect_tts_server_thread(self, tts_snapshot: dict):
        service = tts_snapshot.get("service") or "XTTS"
        result = {
            "service": service,
            "updates": {},
            "log_message": "",
            "error_title": "",
            "error_message": "",
        }

        try:
            if service == "XTTS":
                base_url = (
                    tts_snapshot.get("external_server_url")
                    if tts_snapshot.get("use_external_server")
                    else tts_handler.XTTS_API_BASE_URL
                )

                if not tts_handler.check_xtts_connection(base_url):
                    result["error_title"] = "Connection Error"
                    result["error_message"] = (
                        f"Could not connect to XTTS server at {base_url}. Is it running?"
                    )
                    self._tts_connection_result.emit(result)
                    return

                models = tts_handler.get_xtts_models(base_url)
                speakers = tts_handler.get_xtts_speakers(base_url)

                selected_model = (tts_snapshot.get("xtts_model") or "").strip()
                if models and selected_model not in models:
                    selected_model = models[0]
                elif not models and not selected_model:
                    selected_model = tts_handler.XTTS_DEFAULT_MODEL

                selected_speaker = (tts_snapshot.get("speaker") or "").strip()
                if speakers:
                    if selected_speaker not in speakers:
                        selected_speaker = speakers[0]
                else:
                    selected_speaker = ""

                result["updates"] = {
                    "tts_models": models,
                    "tts_speakers": speakers,
                    "xtts_model": selected_model,
                    "speaker": selected_speaker,
                }
                result["log_message"] = (
                    f"Connected to XTTS server ({len(models)} model(s), {len(speakers)} voice(s))."
                )

            elif service == "Voxtral":
                base_url = (
                    tts_snapshot.get("external_server_url")
                    if tts_snapshot.get("use_external_server")
                    else tts_handler.VOXTRAL_API_BASE_URL
                )

                if not tts_handler.check_voxtral_connection(base_url):
                    result["error_title"] = "Connection Error"
                    result["error_message"] = (
                        f"Could not connect to Voxtral server at {base_url}. Is it running?"
                    )
                    self._tts_connection_result.emit(result)
                    return

                models = tts_handler.get_voxtral_models(base_url)
                speakers = tts_handler.get_voxtral_voices(base_url)

                default_model = tts_handler.VOXTRAL_DEFAULT_MODEL
                default_voice = tts_handler.VOXTRAL_DEFAULT_VOICE

                selected_model = (tts_snapshot.get("xtts_model") or "").strip()
                if models:
                    if selected_model not in models:
                        selected_model = default_model if default_model in models else models[0]
                elif not selected_model:
                    selected_model = default_model

                selected_speaker = (tts_snapshot.get("speaker") or "").strip()
                if speakers:
                    if selected_speaker not in speakers:
                        selected_speaker = default_voice if default_voice in speakers else speakers[0]
                elif not selected_speaker:
                    selected_speaker = default_voice

                result["updates"] = {
                    "tts_models": models,
                    "tts_speakers": speakers,
                    "xtts_model": selected_model,
                    "speaker": selected_speaker,
                }
                result["log_message"] = (
                    f"Connected to Voxtral server ({len(models)} model(s), {len(speakers)} voice(s))."
                )

            elif service == "Silero":
                base_url = tts_handler.SILERO_API_BASE_URL
                if not tts_handler.check_silero_connection(base_url):
                    result["error_title"] = "Connection Error"
                    result["error_message"] = (
                        f"Could not connect to Silero server at {base_url}. Is it running?"
                    )
                    self._tts_connection_result.emit(result)
                    return

                from .constants import SILERO_LANGUAGES

                language_name = tts_snapshot.get("language")
                language_code = next(
                    (lang["code"] for lang in SILERO_LANGUAGES if lang["name"] == language_name),
                    None,
                )
                if language_code and not tts_handler.set_silero_language(language_code, base_url):
                    result["error_title"] = "Connection Error"
                    result["error_message"] = "Could not set language on Silero server."
                    self._tts_connection_result.emit(result)
                    return

                speakers = tts_handler.get_silero_speakers(base_url)
                selected_speaker = (tts_snapshot.get("speaker") or "").strip()
                if speakers and selected_speaker not in speakers:
                    selected_speaker = speakers[0]

                result["updates"] = {
                    "tts_models": [],
                    "tts_speakers": speakers,
                    "speaker": selected_speaker,
                }
                result["log_message"] = "Connected to Silero server."

            elif service in {
                tts_handler.OPENAI_SERVICE,
                tts_handler.GEMINI_SERVICE,
                tts_handler.OPENAI_COMPAT_SERVICE,
            }:
                if service == tts_handler.OPENAI_SERVICE:
                    tts_snapshot["openai_audio_endpoint"] = tts_handler.OPENAI_PROVIDER
                elif service == tts_handler.GEMINI_SERVICE:
                    tts_snapshot["openai_audio_endpoint"] = tts_handler.GEMINI_PROVIDER

                endpoint, endpoint_error = tts_handler.resolve_openai_audio_endpoint(tts_snapshot)
                if endpoint is None:
                    result["error_title"] = "Connection Error"
                    result["error_message"] = endpoint_error
                    self._tts_connection_result.emit(result)
                    return

                tts_snapshot["openai_audio_endpoint"] = endpoint["name"]

                connected, connection_message = tts_handler.check_openai_audio_connection(tts_snapshot)
                if not connected:
                    result["error_title"] = "Connection Error"
                    result["error_message"] = connection_message
                    self._tts_connection_result.emit(result)
                    return

                models = tts_handler.get_openai_audio_models(tts_snapshot)
                speakers = tts_handler.get_openai_audio_voices(tts_snapshot)

                provider = str(endpoint.get("provider") or "").strip().lower()
                if provider == tts_handler.GEMINI_PROVIDER:
                    default_model_fallback = tts_handler.GEMINI_AUDIO_DEFAULT_MODEL
                    default_voice_fallback = tts_handler.GEMINI_AUDIO_DEFAULT_VOICE
                else:
                    default_model_fallback = tts_handler.OPENAI_AUDIO_DEFAULT_MODEL
                    default_voice_fallback = tts_handler.OPENAI_AUDIO_DEFAULT_VOICE

                default_model = endpoint.get("default_model") or default_model_fallback
                default_voice = endpoint.get("default_voice") or default_voice_fallback

                selected_model = (tts_snapshot.get("xtts_model") or "").strip()
                if models:
                    if selected_model not in models:
                        selected_model = default_model if default_model in models else models[0]
                elif not selected_model:
                    selected_model = default_model

                selected_speaker = (tts_snapshot.get("speaker") or "").strip()
                if speakers:
                    if selected_speaker not in speakers:
                        selected_speaker = default_voice if default_voice in speakers else speakers[0]
                elif not selected_speaker:
                    selected_speaker = default_voice

                result["updates"] = {
                    "openai_audio_endpoint": endpoint["name"],
                    "tts_models": models,
                    "tts_speakers": speakers,
                    "xtts_model": selected_model,
                    "speaker": selected_speaker,
                }
                result["log_message"] = (
                    f"{connection_message} ({len(models)} model(s), {len(speakers)} voice(s))."
                )
            else:
                result["error_title"] = "Connection Error"
                result["error_message"] = f"Unsupported TTS service: {service}"
        except Exception as e:
            logging.error("Unexpected TTS connection failure: %s", e, exc_info=True)
            result["error_title"] = "Connection Error"
            result["error_message"] = f"Unexpected connection failure: {e}"

        self._tts_connection_result.emit(result)

    def _apply_tts_connection_result(self, result: dict):
        try:
            requested_service = result.get("service") or ""
            if requested_service != self.state.tts.service:
                self.log_message.emit(
                    f"Ignored {requested_service} connection result because active service changed to {self.state.tts.service}."
                )
                return

            updates = result.get("updates") or {}
            if updates:
                with self._state_lock:
                    for key, value in updates.items():
                        setattr(self.state.tts, key, value)

            log_message = result.get("log_message")
            if log_message:
                self.log_message.emit(log_message)

            error_message = result.get("error_message")
            if error_message:
                self.show_error.emit(result.get("error_title") or "Connection Error", error_message)

            self.state_changed.emit()
        finally:
            self._tts_connection_thread = None
            self.tts_connection_running_changed.emit(False)

    def on_tts_model_changed(self, model_name: str):
        """Handles model changes for the active TTS service."""
        if self.state.tts.service == "XTTS":
            self.switch_xtts_model(model_name)
            return

        if self.state.tts.service in {
            tts_handler.OPENAI_SERVICE,
            tts_handler.GEMINI_SERVICE,
            tts_handler.OPENAI_COMPAT_SERVICE,
        }:
            if self.populate_cloud_tts_catalogs(
                use_remote=False,
                preferred_model=model_name,
                allow_unknown_model=True,
                emit_state=True,
            ):
                return

        self.state.tts.xtts_model = model_name
        self.state_changed.emit()

    def switch_xtts_model(self, model_name: str):
        """Updates the active XTTS model for per-request usage."""
        self.state.tts.xtts_model = model_name
        self.state_changed.emit()

    def on_tts_language_changed(self, language_name: str):
        """Handles logic when the TTS language is changed."""
        self.state.tts.language = language_name
        if self.state.tts.service == "Silero":
            self.connect_tts_server()
        else:
            self.state_changed.emit()

    def upload_speaker_voice(self, wav_file_path: str):
        """Uploads a speaker voice file and refreshes available XTTS voices."""
        if self.state.tts.service != "XTTS":
            self.show_error.emit("Upload Error", "Voice upload is only supported for XTTS.")
            return

        try:
            base_url = self.state.tts.external_server_url if self.state.tts.use_external_server else tts_handler.XTTS_API_BASE_URL
            speaker_name = tts_handler.upload_speaker_voice(
                wav_file_path,
                base_url=base_url,
            )
            self.log_message.emit(f"Uploaded speaker voice: {speaker_name}")
            if self.state.tts.service == "XTTS":
                self.state.tts.speaker = speaker_name
                self.state_changed.emit()
                self.connect_tts_server()
        except Exception as e:
            self.show_error.emit("Upload Error", f"Failed to upload speaker voice: {e}")

    def should_show_xtts_advanced_settings(self) -> bool:
        """Returns whether XTTS advanced controls should be visible in the UI."""
        return tts_handler.should_show_xtts_advanced_settings(self.state.tts.__dict__)

    # --- LLM ---
    def list_llm_models(self) -> list[str]:
        """Returns a list of available LLM models."""
        return llm_handler.list_models(self.state.llm)

    def list_llm_provider_configs(self) -> list[dict]:
        """Returns normalized provider configuration for LLM settings UI."""
        provider_configs = llm_handler.get_provider_configs(self.state.llm)
        if provider_configs != self.state.llm.provider_configs:
            self.state.llm.provider_configs = provider_configs
        return copy.deepcopy(provider_configs)

    def save_llm_provider(
        self,
        provider_id: str,
        provider_name: str,
        provider_key: str,
        api_base: str,
        api_key: str,
        models: list[str] | str | None,
    ) -> tuple[bool, str, str]:
        """Creates or updates an LLM provider."""
        current_configs = llm_handler.get_provider_configs(self.state.llm)
        normalized_provider_id = str(provider_id or "").strip()
        existing_provider = next(
            (
                provider
                for provider in current_configs
                if str(provider.get("id") or "") == normalized_provider_id
            ),
            None,
        )

        if existing_provider is not None:
            success, provider_configs, message = llm_handler.update_provider(
                self.state.llm,
                provider_id=normalized_provider_id,
                provider_name=provider_name,
                provider_key=provider_key,
                api_base=api_base,
                api_key=api_key,
                models=models,
            )
            if success:
                self.state.llm.provider_configs = provider_configs
                self.normalize_dubbing_translation_state(self.state.dubbing)
                self.state_changed.emit()
                return True, normalized_provider_id, ""
            return False, "", message

        success, provider_configs, resolved_provider_id, message = llm_handler.save_custom_provider(
            self.state.llm,
            provider_name=provider_name,
            provider_key=provider_key,
            api_base=api_base,
            api_key=api_key,
            models=models,
        )
        if success:
            self.state.llm.provider_configs = provider_configs
            self.normalize_dubbing_translation_state(self.state.dubbing)
            self.state_changed.emit()
            return True, resolved_provider_id, ""
        return False, "", message

    def refresh_llm_builtin_models(self) -> list[str]:
        """Uses LiteLLM to refresh model catalogs for built-in providers."""
        provider_configs, status_lines = llm_handler.refresh_builtin_provider_models(self.state.llm)
        self.state.llm.provider_configs = provider_configs
        self.normalize_dubbing_translation_state(self.state.dubbing)
        self.state_changed.emit()
        return status_lines

    def save_llm_custom_provider(
        self,
        provider_name: str,
        api_base: str,
        api_key: str,
        models: list[str] | str | None,
        provider_key: str = "openai",
    ) -> tuple[bool, str, str]:
        """Backward-compatible wrapper for custom LLM provider creation."""
        return self.save_llm_provider(
            provider_id="",
            provider_name=provider_name,
            provider_key=provider_key,
            api_base=api_base,
            api_key=api_key,
            models=models,
        )

    def remove_llm_provider(self, provider_name_or_id: str) -> tuple[bool, str]:
        """Removes a custom LLM provider."""
        success, provider_configs, message = llm_handler.remove_custom_provider(
            self.state.llm,
            provider_name_or_id,
        )
        if success:
            self.state.llm.provider_configs = provider_configs
            removed_provider_id = self._normalize_provider_id(provider_name_or_id)
            if removed_provider_id:
                removed_prefix = f"custom:{removed_provider_id}/"
                if str(self.state.llm.default_model or "").startswith(removed_prefix):
                    self.state.llm.default_model = llm_handler.DEFAULT_LITELLM_MODEL

                for prompt_key in ("first_prompt", "second_prompt", "third_prompt"):
                    prompt_state = getattr(self.state.llm, prompt_key)
                    if str(prompt_state.model or "").startswith(removed_prefix):
                        prompt_state.model = "default"

            self.normalize_dubbing_translation_state(self.state.dubbing)
            self.state_changed.emit()
            return True, ""
        return False, message

    def remove_llm_custom_provider(self, provider_name_or_id: str) -> tuple[bool, str]:
        """Backward-compatible wrapper for removing a custom LLM provider."""
        return self.remove_llm_provider(provider_name_or_id)

    # --- TTS Providers ---
    def list_tts_provider_configs(self) -> list[dict]:
        """Returns normalized provider configuration for cloud TTS UI."""
        provider_configs = tts_handler.get_provider_configs(self.state.tts)
        if provider_configs != self.state.tts.provider_configs:
            self.state.tts.provider_configs = provider_configs
        return copy.deepcopy(provider_configs)

    def save_tts_provider(
        self,
        provider_id: str,
        provider_name: str,
        provider_type: str,
        api_base: str,
        api_key: str,
        models: list[str] | str | None,
        voices: list[str] | str | None,
    ) -> tuple[bool, str, str]:
        """Creates or updates an OpenAI-compatible TTS provider."""
        success, provider_configs, resolved_provider_id, message = tts_handler.save_provider(
            self.state.tts,
            provider_name=provider_name,
            provider_type=provider_type,
            api_base=api_base,
            api_key=api_key,
            models=models,
            voices=voices,
            provider_id=provider_id,
        )
        if not success:
            return False, "", message

        self.state.tts.provider_configs = provider_configs
        if not self.state.tts.openai_audio_endpoint:
            self.state.tts.openai_audio_endpoint = resolved_provider_id
        self._normalize_tts_service_state(self.state.tts)
        self.state_changed.emit()
        return True, resolved_provider_id, ""

    def remove_tts_provider(self, provider_name_or_id: str) -> tuple[bool, str]:
        """Removes a custom cloud TTS provider."""
        success, provider_configs, message = tts_handler.remove_custom_provider(
            self.state.tts,
            provider_name_or_id,
        )
        if not success:
            return False, message

        removed_provider_id = self._normalize_provider_id(provider_name_or_id)
        self.state.tts.provider_configs = provider_configs
        selected_endpoint_id = self._normalize_provider_id(self.state.tts.openai_audio_endpoint)
        if selected_endpoint_id == removed_provider_id:
            self.state.tts.openai_audio_endpoint = tts_handler.OPENAI_PROVIDER
            self.populate_cloud_tts_catalogs(use_remote=False, emit_state=False)

        self._normalize_tts_service_state(self.state.tts)
        self.state_changed.emit()
        return True, ""

    def test_tts_provider_connection(self, provider_id: str) -> tuple[bool, str]:
        """Checks connectivity for a specific cloud TTS provider."""
        normalized_provider_id = self._normalize_provider_id(provider_id)
        if not normalized_provider_id:
            return False, "Select a provider first."

        tts_snapshot = copy.deepcopy(self.state.tts.__dict__)
        tts_snapshot["service"] = tts_handler.OPENAI_COMPAT_SERVICE
        tts_snapshot["provider_configs"] = tts_handler.get_provider_configs(self.state.tts)
        tts_snapshot["openai_audio_endpoint"] = normalized_provider_id

        connected, message = tts_handler.check_openai_audio_connection(tts_snapshot)
        if connected:
            self.log_message.emit(message)
        return connected, message

    def discover_tts_provider_catalog(
        self,
        provider_id: str,
    ) -> tuple[bool, list[str], list[str], str]:
        """Attempts remote discovery for provider models and voices."""
        normalized_provider_id = self._normalize_provider_id(provider_id)
        if not normalized_provider_id:
            return False, [], [], "Select a provider first."

        tts_snapshot = copy.deepcopy(self.state.tts.__dict__)
        tts_snapshot["service"] = tts_handler.OPENAI_COMPAT_SERVICE
        tts_snapshot["provider_configs"] = tts_handler.get_provider_configs(self.state.tts)
        tts_snapshot["openai_audio_endpoint"] = normalized_provider_id

        endpoint, endpoint_error = tts_handler.resolve_openai_audio_endpoint(tts_snapshot)
        if endpoint is None:
            return False, [], [], endpoint_error

        connected, connection_message = tts_handler.check_openai_audio_connection(tts_snapshot)
        if not connected:
            return False, [], [], connection_message

        models = tts_handler.get_openai_audio_models(tts_snapshot)
        if not models:
            models = tts_handler.get_openai_audio_models_fallback(tts_snapshot)

        selected_model = models[0] if models else str(endpoint.get("default_model") or "")
        if selected_model:
            tts_snapshot["xtts_model"] = selected_model

        voices = tts_handler.get_openai_audio_voices(tts_snapshot)
        if not voices:
            voices = tts_handler.get_openai_audio_voices_fallback(tts_snapshot)

        message = (
            f"{connection_message} Discovered {len(models)} model(s) and {len(voices)} voice(s)."
        )
        return True, models, voices, message

    # --- RVC ---
    def is_rvc_available(self) -> bool:
        """Checks if RVC dependencies are installed."""
        return rvc_handler.is_rvc_available()

    def get_rvc_models(self) -> list[str]:
        """Gets a list of available RVC models."""
        rvc_models_dir = "rvc_models"
        os.makedirs(rvc_models_dir, exist_ok=True)
        return rvc_handler.get_rvc_models(rvc_models_dir)

    def upload_rvc_model(self, pth_file: str, index_file: str) -> str:
        """Uploads an RVC model."""
        rvc_models_dir = "rvc_models"
        model_name = rvc_handler.upload_rvc_model(pth_file, index_file, rvc_models_dir)
        self.state_changed.emit()
        return model_name

    # --- Config/API Keys ---
    def save_api_key(self, key_name: str, key_value: str) -> bool:
        """Saves an API key and returns success state."""
        normalized_value = (key_value or "").strip()
        try:
            config_handler.save_api_key(key_name, normalized_value)
            self.log_message.emit(f"Saved API key: {key_name}")
            return True
        except Exception as e:
            self.show_error.emit("API Key Error", f"Could not save API key {key_name}: {e}")
            return False

    def get_api_key(self, key_name: str) -> str:
        """Gets an API key."""
        return config_handler.get_api_key(key_name)

    # --- Sentence Management ---
    def update_sentence_text(self, sentence_number: str, new_text: str):
        if self._is_generation_running() or self._is_regeneration_running():
            self.log_message.emit("Cannot edit sentences while generation/regeneration is running.")
            return

        if session_handler.update_sentence(self.state.session_name, sentence_number, new_text):
            # Update state directly to avoid full reload
            with self._state_lock:
                for s in self.state.processed_sentences:
                    if str(s.get('sentence_number')) == str(sentence_number):
                        if "processed_sentence" in s and s["processed_sentence"] is not None:
                            s["processed_sentence"] = new_text
                        else:
                            s["original_sentence"] = new_text
                        break
            self.state_changed.emit()

    def mark_sentence(self, sentence_number: str, marked: bool):
        if self._is_generation_running() or self._is_regeneration_running():
            self.log_message.emit("Cannot change marked status while generation/regeneration is running.")
            return

        if session_handler.update_sentence_marked_status(self.state.session_name, sentence_number, marked):
            with self._state_lock:
                for s in self.state.processed_sentences:
                    if str(s.get('sentence_number')) == str(sentence_number):
                        s['marked'] = marked
                        break
            self.state_changed.emit()

    def remove_sentences(self, sentence_numbers: list[str]):
        if self._is_generation_running() or self._is_regeneration_running():
            self.log_message.emit("Cannot remove sentences while generation/regeneration is running.")
            return

        if session_handler.remove_sentences(self.state.session_name, sentence_numbers):
            # Reload is necessary here because of re-numbering
            self.load_session(self.state.session_name)

    # --- Playback ---
    def get_current_playing_sentence_number(self) -> str | None:
        return self.current_playing_sentence_number

    def _set_current_playing_sentence(self, sentence_number: str | None):
        normalized = None if sentence_number is None else str(sentence_number)
        if self.current_playing_sentence_number == normalized:
            return
        self.current_playing_sentence_number = normalized
        self.state_changed.emit()

    def play_audio_for_sentence(self, sentence_number: str, keep_playlist_state: bool = False) -> bool:
        if not keep_playlist_state:
            self.playlist_active = False
            self.playlist_sentences = []
            self.current_playlist_index = 0
            self.playlist_timer.stop()

        wav_filename = f"{self.state.session_name}_sentence_{sentence_number}.wav"
        wav_path = ""
        for wavs_dir in self._get_candidate_sentence_wavs_dirs():
            candidate_path = os.path.join(wavs_dir, wav_filename)
            if os.path.exists(candidate_path):
                wav_path = candidate_path
                break

        if not wav_path:
            self.log_message.emit(f"Audio file not found for sentence {sentence_number}")
            if not keep_playlist_state:
                self._set_current_playing_sentence(None)
            return False

        if self.playback_handler.play(wav_path):
            self._set_current_playing_sentence(str(sentence_number))
            self.playlist_timer.start()
            return True

        self.log_message.emit(f"Failed to play audio for sentence {sentence_number}")
        if not keep_playlist_state:
            self._set_current_playing_sentence(None)
        return False

    def stop_playback(self):
        self.playlist_active = False
        self.playlist_sentences = []
        self.current_playlist_index = 0
        self.playlist_timer.stop()
        self.playback_handler.stop()
        self._set_current_playing_sentence(None)

    def toggle_pause_playback(self):
        self.playback_handler.toggle_pause()

    def play_playlist(self, start_sentence_number: str | None = None):
        """Starts playing the processed sentences as a playlist."""
        self.stop_playback()
        all_sentences = self.get_processed_sentences_snapshot()
        self.playlist_sentences = [
            s for s in all_sentences if s.get('tts_generated') == 'yes'
        ]
        if not self.playlist_sentences:
            self.log_message.emit("No generated audio to play.")
            return

        self.current_playlist_index = 0
        if start_sentence_number is not None:
            start_sentence_str = str(start_sentence_number)
            sentence_order = {
                str(sentence.get('sentence_number')): idx
                for idx, sentence in enumerate(all_sentences)
            }
            selected_order = sentence_order.get(start_sentence_str)

            exact_match_index = next(
                (
                    idx
                    for idx, sentence in enumerate(self.playlist_sentences)
                    if str(sentence.get('sentence_number')) == start_sentence_str
                ),
                None,
            )
            if exact_match_index is not None:
                self.current_playlist_index = exact_match_index
            elif selected_order is not None:
                next_generated_index = next(
                    (
                        idx
                        for idx, sentence in enumerate(self.playlist_sentences)
                        if sentence_order.get(str(sentence.get('sentence_number')), -1) >= selected_order
                    ),
                    None,
                )
                if next_generated_index is not None:
                    self.current_playlist_index = next_generated_index

        self.playlist_active = True
        if self._play_current_playlist_item():
            self.playlist_timer.start()
        else:
            self.log_message.emit("No playable audio found in playlist.")
            self.stop_playback()

    def _refresh_playlist_sentences(self, last_played_sentence_number: str | None = None):
        """Rebuilds playlist from generated sentences and keeps playback position stable."""
        all_sentences = self.get_processed_sentences_snapshot()
        self.playlist_sentences = [
            sentence for sentence in all_sentences if sentence.get('tts_generated') == 'yes'
        ]

        if last_played_sentence_number is None:
            self.current_playlist_index = min(self.current_playlist_index, len(self.playlist_sentences))
            return

        last_played_str = str(last_played_sentence_number)
        last_played_index = next(
            (
                idx
                for idx, sentence in enumerate(self.playlist_sentences)
                if str(sentence.get('sentence_number')) == last_played_str
            ),
            None,
        )

        if last_played_index is not None:
            self.current_playlist_index = max(self.current_playlist_index, last_played_index + 1)

        self.current_playlist_index = min(self.current_playlist_index, len(self.playlist_sentences))

    def _play_current_playlist_item(self) -> bool:
        """Plays the audio for the current sentence in the playlist."""
        while 0 <= self.current_playlist_index < len(self.playlist_sentences):
            sentence_dict = self.playlist_sentences[self.current_playlist_index]
            sentence_number = sentence_dict.get('sentence_number')
            self.log_message.emit(f"Playing sentence {sentence_number}")
            if self.play_audio_for_sentence(sentence_number, keep_playlist_state=True):
                return True

            self.log_message.emit(f"Skipping sentence {sentence_number}; audio unavailable.")
            self.current_playlist_index += 1

        return False

    def _check_playlist_status(self):
        """Called by a timer to check if current playback has finished."""
        playback_finished = self.playback_handler.check_if_finished()

        if not self.playlist_active:
            if playback_finished:
                self.playlist_timer.stop()
                self._set_current_playing_sentence(None)
            return

        if playback_finished:
            self.current_playlist_index += 1
            self._refresh_playlist_sentences(self.current_playing_sentence_number)
            if self._play_current_playlist_item():
                return
        elif self.playback_handler.get_busy():
            return
        else:
            self._refresh_playlist_sentences(self.current_playing_sentence_number)
            if self._play_current_playlist_item():
                return

        if self._is_generation_or_regeneration_running():
            return

        self.log_message.emit("Playlist finished.")
        self.stop_playback()

    # --- Output Generation ---
    def save_output(self, output_path: str):
        try:
            success = audio_processor.save_output(
                session_name=self.state.session_name,
                output_path=output_path,
                output_format=self.state.audio_processing.output_format,
                bitrate=self.state.audio_processing.bitrate,
                metadata=self.state.metadata,
                cover_image_path=self.state.cover_image_path
            )
            if success:
                self.log_message.emit(f"Output file saved to {output_path}")
            else:
                self.show_error.emit("Save Error", "Failed to save the output file. Check logs for details.")
        except Exception as e:
            self.show_error.emit("Save Error", f"An unexpected error occurred: {e}")

    def save_xtts_settings(self):
        """Persists XTTS advanced settings locally and for the active session."""
        if self.state.tts.service != "XTTS":
            return

        self._persist_global_settings(force=True)
        if self._is_named_session_active():
            self._persist_session_config(force=True)

        self.log_message.emit(
            "Saved XTTS advanced defaults. Only parameters marked 'Send' are applied per request."
        )

    def apply_xtts_settings(self):
        """Backward-compatible alias for saving XTTS settings from UI."""
        self.save_xtts_settings()

    # --- XTTS Trainer ---
    def _parse_xtts_training_progress(self, line: str, configured_epochs: int) -> int | None:
        """Best-effort extraction of training progress from trainer output lines."""
        percent_match = re.search(r"\b(\d{1,3})(?:\.\d+)?\s*%", line)
        if percent_match:
            percent = max(0, min(100, int(percent_match.group(1))))
            return percent

        epoch_pair_match = re.search(r"epoch[^0-9]*(\d+)\s*/\s*(\d+)", line, re.IGNORECASE)
        if epoch_pair_match:
            current_epoch = int(epoch_pair_match.group(1))
            total_epochs = int(epoch_pair_match.group(2))
            if total_epochs > 0:
                return max(0, min(100, int((current_epoch / total_epochs) * 100)))

        single_epoch_match = re.search(r"epoch[^0-9]*(\d+)", line, re.IGNORECASE)
        if single_epoch_match and configured_epochs > 0:
            current_epoch = int(single_epoch_match.group(1))
            return max(0, min(100, int((current_epoch / configured_epochs) * 100)))

        return None

    def start_xtts_training(self, settings: dict):
        """Starts the XTTS training process in a background thread."""
        if self._is_xtts_training_running():
            self.log_message.emit("XTTS training is already running.")
            return

        setup_ok, setup_message = xtts_trainer_handler.validate_training_environment()
        if not setup_ok:
            self.xtts_training_status_updated.emit(setup_message)
            self.show_error.emit("Training Setup Error", setup_message)
            return

        self.xtts_training_progress_updated.emit(0)
        self.xtts_training_status_updated.emit("Training in progress...")
        self.xtts_training_running_changed.emit(True)

        def thread_target():
            worker_thread = threading.current_thread()
            configured_epochs = int(settings.get("epochs") or 0)
            last_progress = 0

            def handle_status(status_message: str):
                if status_message:
                    self.xtts_training_status_updated.emit(status_message)

            def handle_output(line: str):
                nonlocal last_progress
                if not line:
                    return

                self.log_message.emit(f"XTTS Trainer: {line}")

                progress = self._parse_xtts_training_progress(line, configured_epochs)
                if progress is not None and progress > last_progress:
                    last_progress = progress
                    self.xtts_training_progress_updated.emit(progress)

            self.log_message.emit("Starting XTTS training...")
            try:
                success, message = xtts_trainer_handler.start_training(
                    settings,
                    output_callback=handle_output,
                    status_callback=handle_status,
                )

                if success:
                    final_message = message or "XTTS training process finished successfully."
                    self.xtts_training_progress_updated.emit(100)
                    self.xtts_training_status_updated.emit(final_message)
                    self.log_message.emit(final_message)
                else:
                    failure_message = message or "XTTS training process failed. Check logs for details."
                    self.xtts_training_status_updated.emit(failure_message)
                    self.show_error.emit("Training Failed", failure_message)
            except Exception as e:
                logging.error("Unexpected XTTS training error: %s", e, exc_info=True)
                error_message = f"An unexpected error occurred during training: {e}"
                self.xtts_training_status_updated.emit(error_message)
                self.show_error.emit("Training Error", error_message)
            finally:
                if self.xtts_training_thread is worker_thread:
                    self.xtts_training_thread = None
                self.xtts_training_running_changed.emit(False)

        self.xtts_training_thread = threading.Thread(target=thread_target, daemon=True)
        self.xtts_training_thread.start()

    # --- Sentence Regeneration ---
    def regenerate_sentences(self, sentence_numbers: list[str]):
        """Regenerates audio for a list of sentence numbers."""
        if self._is_generation_running():
            self.log_message.emit("Cannot regenerate sentences while full generation is running.")
            return

        if self._is_regeneration_running():
            self.log_message.emit("Sentence regeneration is already running.")
            return

        self.regeneration_thread = self._run_threaded_task(self._regenerate_sentences_thread, sentence_numbers)
        self.state_changed.emit()

    def _regenerate_sentences_thread(self, sentence_numbers: list[str]):
        """Thread worker for regenerating sentences."""
        worker_thread = threading.current_thread()
        try:
            total = len(sentence_numbers)
            self.log_message.emit(f"Starting regeneration for {total} sentence(s).")
            processed_sentences = self.get_processed_sentences_snapshot()
            sentence_index_map = {
                str(sentence.get('sentence_number')): index
                for index, sentence in enumerate(processed_sentences)
            }

            for i, num in enumerate(sentence_numbers):
                self.log_message.emit(f"Regenerating sentence {i+1}/{total} (Number: {num})...")

                sentence_index = sentence_index_map.get(str(num))
                if sentence_index is None:
                    self.log_message.emit(f"Could not find sentence {num} to regenerate.")
                    continue

                sentence_dict = processed_sentences[sentence_index]
                success, updated_sentence = self._execute_generation_for_sentence(sentence_dict)
                if not success or updated_sentence is None:
                    self.show_error.emit("Regeneration Failed", f"Failed to regenerate audio for sentence {num}. See logs.")
                    break # Stop on first error

                processed_sentences[sentence_index] = updated_sentence
                self._set_processed_sentences_snapshot(processed_sentences)
                self.state_changed.emit()

            self.log_message.emit("Regeneration finished.")
        except Exception as e:
            logging.error("Unexpected regeneration worker error: %s", e, exc_info=True)
            self.show_error.emit("Regeneration Error", f"Unexpected regeneration failure: {e}")
        finally:
            if self.regeneration_thread is worker_thread:
                self.regeneration_thread = None
            self.state_changed.emit()

    def _run_llm_processing(self, text: str) -> tuple[str, int]:
        """Runs the configured LLM prompt chain on a text and returns (text, prompt_count)."""
        processed_text = text
        prompts_ran = 0
        prompts_to_run = [
            self.state.llm.first_prompt,
            self.state.llm.second_prompt,
            self.state.llm.third_prompt,
        ]

        for prompt_settings in prompts_to_run:
            if not (prompt_settings.enabled and prompt_settings.prompt_text):
                continue

            model_name = self._normalize_prompt_model(prompt_settings.model)
            model_ready = self._ensure_llm_model_loaded(model_name)
            if model_name and not model_ready:
                logging.warning(
                    "Could not pre-load selected LLM model '%s'; continuing with API-level model hint.",
                    model_name,
                )

            llm_output = llm_handler.process_text(
                processed_text,
                prompt_settings.prompt_text,
                prompt_settings.evaluation_enabled,
                model_name=model_name,
                llm_settings=self.state.llm,
            )

            if not llm_output:
                raise RuntimeError("LLM processing returned an empty result.")

            processed_text = llm_output
            prompts_ran += 1

        return processed_text, prompts_ran

    def _execute_generation_for_sentence(self, sentence_dict: dict) -> tuple[bool, dict | None]:
        """Runs the full generation pipeline (LLM, TTS, RVC, fade) for a single sentence."""
        try:
            updated_sentence = copy.deepcopy(sentence_dict)
            text_source_key = "processed_sentence" if updated_sentence.get("processed_sentence") else "original_sentence"
            text_to_process = str(updated_sentence.get(text_source_key) or "")
            is_dubbing_sentence = self._is_dubbing_source_selected()
            if is_dubbing_sentence:
                text_to_process = self._normalize_subtitle_sentence_text(text_to_process)
                updated_sentence[text_source_key] = text_to_process

            if not text_to_process.strip():
                return True, updated_sentence  # Skip empty sentences

            # 1. LLM Processing
            if self.state.llm.processing_enabled:
                processed_text, _ = self._run_llm_processing(text_to_process)
                if is_dubbing_sentence:
                    processed_text = self._normalize_subtitle_sentence_text(processed_text)
                updated_sentence['processed_sentence'] = processed_text
            else:
                processed_text = text_to_process

            # 2. TTS Generation
            active_service = self.state.tts.service
            xtts_url = (
                self.state.tts.external_server_url
                if self.state.tts.use_external_server and active_service == "XTTS"
                else tts_handler.XTTS_API_BASE_URL
            )
            voxtral_url = (
                self.state.tts.external_server_url
                if self.state.tts.use_external_server and active_service == "Voxtral"
                else tts_handler.VOXTRAL_API_BASE_URL
            )
            audio_data = tts_handler.text_to_audio(
                processed_text,
                self.state.tts.__dict__,
                xtts_base_url=xtts_url,
                voxtral_base_url=voxtral_url,
            )
            if not audio_data:
                return False, None

            # 3. RVC
            if self.state.rvc.enable_rvc:
                audio_data = rvc_handler.process_with_rvc(audio_data, self.state.rvc.__dict__)

            # 4. Fade
            if self.state.audio_processing.enable_fade:
                audio_data = audio_processor.apply_fade(audio_data, self.state.audio_processing.fade_in_duration, self.state.audio_processing.fade_out_duration)
            
            # 5. Add Silence
            if not self._is_dubbing_source_selected():
                silence_to_add = 0
                if updated_sentence.get("paragraph", "no") == "yes":
                    silence_to_add = self.state.audio_processing.silence_for_paragraphs
                else:
                    silence_to_add = self.state.audio_processing.silence_between_sentences
                
                if silence_to_add > 0:
                    audio_data += AudioSegment.silent(duration=silence_to_add)

            # 6. Save WAV
            session_name = self.state.session_name
            num = updated_sentence['sentence_number']
            wavs_dir = self._get_primary_sentence_wavs_dir(ensure_exists=True)
            wav_path = os.path.join(wavs_dir, f"{session_name}_sentence_{num}.wav")
            audio_data.export(wav_path, format="wav")

            updated_sentence['tts_generated'] = 'yes'
            return True, updated_sentence

        except Exception as e:
            logging.error(f"Failed to execute generation for sentence {sentence_dict.get('sentence_number')}: {e}", exc_info=True)
            return False, None
