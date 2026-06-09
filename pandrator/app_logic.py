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
    source_cleaning,
    tts_handler,
    state_db_handler,
    voice_library_handler,
    rvc_handler,
    audio_variant_handler,
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

DEFAULT_SESSION_ACTIVITY_SNAPSHOT = {
    "headline": "Ready",
    "detail": "Add a source and start generation when you're ready.",
    "tone": "idle",
}


def _export_audio_segment(audio_data: AudioSegment, output_path: str, output_format: str = "wav"):
    exported_file = audio_data.export(output_path, format=output_format)
    if hasattr(exported_file, "close"):
        exported_file.close()


class AppLogic(QObject):
    """
    Main controller for the application.
    It holds the application state and connects the GUI to the backend logic.
    """
    # Signals to notify the GUI of changes
    state_changed = pyqtSignal()
    log_message = pyqtSignal(str)
    show_error = pyqtSignal(str, str)  # title, message
    dubbing_video_saved = pyqtSignal(object)  # list[str] output paths
    app_notification = pyqtSignal(str, int)  # message, timeout_ms
    session_activity_updated = pyqtSignal(object)  # dict payload for session activity panel
    progress_updated = pyqtSignal(int, int, float) # current, total, elapsed_time
    xtts_training_running_changed = pyqtSignal(bool)
    xtts_training_status_updated = pyqtSignal(str)
    xtts_training_progress_updated = pyqtSignal(int)
    tts_connection_running_changed = pyqtSignal(bool)
    _tts_connection_result = pyqtSignal(dict)
    _download_source_ready = pyqtSignal(str, bool)
    _start_generation_requested = pyqtSignal()
    _start_generation_anew_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state_lock = threading.RLock()
        self.state = AppState()
        self._last_global_settings_snapshot = ""
        self._load_global_settings()

        self.playback_handler: PlaybackHandler | None = None
        self.generation_thread = None
        self.regeneration_thread = None
        self.rvc_processing_thread = None
        self.xtts_training_thread = None
        self._tts_connection_thread = None
        self._text_preprocessing_running = False
        self._pending_dubbing_add_to_video_request: dict[str, object] | None = None
        self._active_dubbing_run_id: str | None = None
        self._session_activity_snapshot = copy.deepcopy(DEFAULT_SESSION_ACTIVITY_SNAPSHOT)
        self.stop_generation_flag = threading.Event()
        self.cancel_generation_flag = threading.Event()
        self.cancel_regeneration_flag = threading.Event()
        self._loaded_llm_model = None
        self.log_file_path = ""

        # Playlist attributes
        self.playlist_sentences = []
        self.current_playlist_index = 0
        self.playlist_active = False
        self.current_playing_sentence_number = None
        self.playlist_timer = QTimer(self)
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
        self._download_source_ready.connect(self._on_download_source_ready)
        self._start_generation_requested.connect(self.start_generation)
        self._start_generation_anew_requested.connect(self.start_generation_anew)

        self._initialize_state_index()

        logging.info("AppLogic initialized.")

    def set_log_file_path(self, path: str):
        """Sets the path to the log file."""
        self.log_file_path = path

    @staticmethod
    def _normalize_session_activity_tone(tone: str) -> str:
        normalized_tone = str(tone or "").strip().lower()
        if normalized_tone not in {"idle", "active", "success", "warning", "error"}:
            return "idle"
        return normalized_tone

    def _set_session_activity(self, headline: str, detail: str = "", tone: str = "idle"):
        snapshot = {
            "headline": str(headline or "").strip() or DEFAULT_SESSION_ACTIVITY_SNAPSHOT["headline"],
            "detail": str(detail or "").strip(),
            "tone": self._normalize_session_activity_tone(tone),
        }

        with self._state_lock:
            if snapshot == self._session_activity_snapshot:
                return
            self._session_activity_snapshot = snapshot

        self.session_activity_updated.emit(copy.deepcopy(snapshot))

    def _reset_session_activity(self):
        self._set_session_activity(
            DEFAULT_SESSION_ACTIVITY_SNAPSHOT["headline"],
            DEFAULT_SESSION_ACTIVITY_SNAPSHOT["detail"],
            DEFAULT_SESSION_ACTIVITY_SNAPSHOT["tone"],
        )

    def _notify_user(self, message: str, timeout_ms: int = 5000, level: str = "info"):
        normalized_message = str(message or "").strip()
        if not normalized_message:
            return

        normalized_level = str(level or "info").strip().lower()
        if normalized_level == "warning":
            logging.warning(normalized_message)
        elif normalized_level == "error":
            logging.error(normalized_message)
        else:
            logging.info(normalized_message)

        self.app_notification.emit(normalized_message, max(0, int(timeout_ms or 0)))

    def get_session_activity_snapshot(self) -> dict[str, str]:
        with self._state_lock:
            return copy.deepcopy(self._session_activity_snapshot)

    def get_active_dubbing_step_states(self) -> dict[str, str]:
        if not self._is_named_session_active():
            return {}

        run = self._synchronize_active_dubbing_run()
        run_id = str(run.get("run_id") or "") if run else ""
        if not run_id:
            return {}

        return {
            str(step.get("step_key") or ""): str(step.get("status") or "pending")
            for step in state_db_handler.get_dubbing_steps(run_id)
            if str(step.get("step_key") or "").strip()
        }

    def _initialize_state_index(self):
        """Initializes SQLite state and reindexes any on-disk sessions."""
        try:
            state_db_handler.initialize_database()
            state_db_handler.reindex_all_sessions()
        except Exception as e:
            logging.error("Failed to initialize SQLite state index: %s", e, exc_info=True)

    def _ensure_playback_handler(self) -> PlaybackHandler | None:
        if self.playback_handler is not None:
            return self.playback_handler

        try:
            self.playback_handler = PlaybackHandler()
        except Exception as e:
            logging.error("Failed to initialize playback handler: %s", e, exc_info=True)
            self.playback_handler = None

        return self.playback_handler

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

    @classmethod
    def _build_source_loaded_notification(cls, source_name: str, extension: str) -> str:
        normalized_name = os.path.basename(str(source_name or "").strip()) or "source"
        normalized_extension = str(extension or "").strip().lower()
        if normalized_extension == ".srt":
            return (
                f"SRT source loaded: {normalized_name}. "
                "Use 'Fine-Tune Timings (Subdub GUI)' to adjust subtitle boundaries."
            )
        if cls._is_dubbing_source_extension(normalized_extension):
            return f"Dubbing source loaded: {normalized_name}"
        return f"Source loaded: {normalized_name}"

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

    def _synchronize_active_dubbing_run(self) -> dict | None:
        if not self._is_named_session_active():
            self._active_dubbing_run_id = None
            return None

        session_name = self.state.session_name

        if self._active_dubbing_run_id:
            try:
                run_by_id = state_db_handler.get_dubbing_run(self._active_dubbing_run_id)
            except Exception:
                run_by_id = None

            if run_by_id and str(run_by_id.get("session_name") or "") == session_name:
                return run_by_id

        try:
            active_run = state_db_handler.get_active_dubbing_run(session_name)
        except Exception:
            active_run = None

        self._active_dubbing_run_id = str(active_run.get("run_id")) if active_run else None
        return active_run

    def _create_dubbing_run(
        self,
        set_active: bool = True,
        force_new: bool = False,
    ) -> dict | None:
        if not self._is_named_session_active():
            return None

        session_name = self.state.session_name
        source_path = str(self.state.source_file_path or "")
        source_ext = os.path.splitext(source_path)[1].lower()
        source_video_path = ""
        source_srt_path = ""
        if source_ext in {".mp4", ".mkv", ".webm", ".avi", ".mov"}:
            source_video_path = source_path
        elif source_ext == ".srt":
            source_srt_path = source_path
            source_video_path = str(self.state.dubbing.video_file_path or "")

        settings_snapshot = copy.deepcopy(self.state.dubbing.__dict__)
        settings_snapshot["llm_provider_configs"] = llm_handler.get_provider_configs(self.state.llm)

        try:
            existing_run = None if force_new else self._synchronize_active_dubbing_run()
            if existing_run and str(existing_run.get("run_dir") or "").strip():
                return existing_run

            created_run = state_db_handler.create_dubbing_run(
                session_name=session_name,
                source_video_path=source_video_path,
                source_srt_path=source_srt_path,
                settings_snapshot=settings_snapshot,
                set_active=set_active,
            )
            self._active_dubbing_run_id = str(created_run.get("run_id") or "")

            if source_video_path and os.path.exists(source_video_path):
                state_db_handler.register_dubbing_artifact(
                    self._active_dubbing_run_id,
                    "video_source",
                    source_video_path,
                    is_current=True,
                )
            if source_srt_path and os.path.exists(source_srt_path):
                state_db_handler.register_dubbing_artifact(
                    self._active_dubbing_run_id,
                    "source_srt",
                    source_srt_path,
                    is_current=True,
                )

            return created_run
        except Exception as e:
            logging.error("Could not create dubbing run for '%s': %s", session_name, e, exc_info=True)
            return None

    def _ensure_dubbing_run_for_task(self, task: str) -> dict | None:
        if not self._is_named_session_active():
            return None

        run = self._synchronize_active_dubbing_run()
        task_name = str(task or "").strip().lower()
        rollover_statuses = {"completed", "rendered", "failed"}
        should_rollover = bool(
            run
            and task_name == "generate_audio"
            and str(run.get("status") or "").strip().lower() in rollover_statuses
        )

        if run is None or should_rollover:
            run = self._create_dubbing_run(set_active=True, force_new=should_rollover)

        if not run:
            return None

        run_dir = str(run.get("run_dir") or "").strip()
        if run_dir:
            os.makedirs(run_dir, exist_ok=True)
        return run

    def _register_dubbing_artifact(self, role: str, path: str, is_current: bool = True):
        run = self._synchronize_active_dubbing_run()
        run_id = str(run.get("run_id") or "") if run else ""
        if not run_id or not role or not path:
            return

        try:
            state_db_handler.register_dubbing_artifact(
                run_id=run_id,
                role=role,
                path=path,
                is_current=is_current,
            )
        except Exception as e:
            logging.warning(
                "Could not register dubbing artifact '%s' at '%s': %s",
                role,
                path,
                e,
            )

    def _mark_dubbing_step(self, step_key: str, status: str, detail: str = ""):
        run = self._synchronize_active_dubbing_run()
        run_id = str(run.get("run_id") or "") if run else ""
        if not run_id:
            return

        try:
            state_db_handler.record_dubbing_step(run_id, step_key, status, detail)
            normalized_status = str(status or "").strip().lower()
            run_status = ""
            if normalized_status == "running":
                run_status = "running"
            elif normalized_status == "failed":
                run_status = "failed"
            elif normalized_status == "completed":
                run_status = "completed" if step_key == "render" else "running"

            if run_status:
                state_db_handler.update_dubbing_run_status(run_id, run_status)
        except Exception as e:
            logging.warning("Could not record dubbing step '%s' (%s): %s", step_key, status, e)
        else:
            self.state_changed.emit()

    def _get_dubbing_work_dir(self, ensure_exists: bool = False) -> str:
        run = self._synchronize_active_dubbing_run()
        run_dir = str(run.get("run_dir") or "") if run else ""
        if not run_dir and ensure_exists and self._is_named_session_active():
            created_run = self._create_dubbing_run(set_active=True)
            run_dir = str(created_run.get("run_dir") or "") if created_run else ""

        if run_dir:
            if ensure_exists:
                os.makedirs(run_dir, exist_ok=True)
            return run_dir

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

    def _get_audio_variant_base_dir(self, ensure_exists: bool = False) -> str:
        if self._is_dubbing_source_selected():
            return self._get_dubbing_work_dir(ensure_exists=ensure_exists)

        session_dir = session_handler.get_session_path(self.state.session_name)
        if ensure_exists:
            os.makedirs(session_dir, exist_ok=True)
        return session_dir

    def _find_source_sentence_wav_path(self, sentence_number: str) -> str:
        wav_filename = f"{self.state.session_name}_sentence_{sentence_number}.wav"
        for wavs_dir in self._get_candidate_sentence_wavs_dirs():
            candidate_path = os.path.join(wavs_dir, wav_filename)
            if os.path.exists(candidate_path):
                return candidate_path
        return ""

    def _find_sentence_wav_path(self, sentence_number: str, variant_id: str | None = None) -> str:
        normalized_variant_id = str(
            variant_id or getattr(self.state, "active_audio_variant_id", audio_variant_handler.SOURCE_VARIANT_ID)
        ).strip() or audio_variant_handler.SOURCE_VARIANT_ID

        if normalized_variant_id == audio_variant_handler.SOURCE_VARIANT_ID:
            return self._find_source_sentence_wav_path(sentence_number)

        base_dir = self._get_audio_variant_base_dir(ensure_exists=False)
        candidate_path = audio_variant_handler.variant_sentence_path(
            base_dir,
            normalized_variant_id,
            self.state.session_name,
            str(sentence_number),
        )
        return candidate_path if os.path.isfile(candidate_path) else ""

    def _source_available_sentence_numbers(self, sentences: list[dict] | None = None) -> list[str]:
        source_sentences = sentences if sentences is not None else self.get_processed_sentences_snapshot()
        numbers: list[str] = []
        for sentence in source_sentences:
            sentence_number = sentence.get("sentence_number")
            if sentence_number is None or sentence.get("tts_generated") != "yes":
                continue
            if self._find_source_sentence_wav_path(str(sentence_number)):
                numbers.append(str(sentence_number))
        return numbers

    @staticmethod
    def _format_audio_variant_count(count: int, total: int) -> str:
        return f"{count}/{total}" if total else str(count)

    def _format_rvc_variant_label(self, record: dict, count: int, total: int) -> str:
        model_name = str(record.get("model_name") or "Unknown model").strip()
        settings_hash = str(record.get("settings_hash") or "").strip()
        short_hash = settings_hash[:6]
        count_label = self._format_audio_variant_count(count, total)
        if short_hash:
            return f"RVC: {model_name} ({count_label}, {short_hash})"
        return f"RVC: {model_name} ({count_label})"

    def list_audio_variants(self) -> list[dict]:
        sentences = self.get_processed_sentences_snapshot()
        total_sentences = len(sentences)
        source_numbers = self._source_available_sentence_numbers(sentences)
        source_count = len(source_numbers)
        denominator = source_count or total_sentences

        variants: list[dict] = [
            {
                "id": audio_variant_handler.SOURCE_VARIANT_ID,
                "kind": "source",
                "label": f"Original ({self._format_audio_variant_count(source_count, total_sentences)})",
                "model_name": "",
                "settings_hash": "",
                "sentence_numbers": source_numbers,
                "count": source_count,
                "total": total_sentences,
            }
        ]

        base_dir = self._get_audio_variant_base_dir(ensure_exists=False)
        if os.path.isdir(os.path.join(base_dir, audio_variant_handler.VARIANTS_DIR_NAME)):
            for record in audio_variant_handler.list_rvc_variants(base_dir, self.state.session_name):
                sentence_numbers = list(record.get("sentence_numbers") or [])
                count = len(sentence_numbers)
                variants.append(
                    {
                        **record,
                        "label": self._format_rvc_variant_label(record, count, denominator),
                        "sentence_numbers": sentence_numbers,
                        "count": count,
                        "total": denominator,
                    }
                )

        available_ids = {str(variant.get("id") or "") for variant in variants}
        active_variant_id = str(
            getattr(self.state, "active_audio_variant_id", audio_variant_handler.SOURCE_VARIANT_ID)
            or audio_variant_handler.SOURCE_VARIANT_ID
        )
        if active_variant_id not in available_ids:
            self.state.active_audio_variant_id = audio_variant_handler.SOURCE_VARIANT_ID
            active_variant_id = audio_variant_handler.SOURCE_VARIANT_ID

        for variant in variants:
            variant["is_active"] = str(variant.get("id") or "") == active_variant_id

        return variants

    def get_active_audio_variant_id(self) -> str:
        variants = self.list_audio_variants()
        active = next((variant for variant in variants if variant.get("is_active")), None)
        return str((active or {}).get("id") or audio_variant_handler.SOURCE_VARIANT_ID)

    def set_active_audio_variant(self, variant_id: str):
        normalized_variant_id = str(variant_id or audio_variant_handler.SOURCE_VARIANT_ID).strip()
        if not normalized_variant_id:
            normalized_variant_id = audio_variant_handler.SOURCE_VARIANT_ID

        available_ids = {str(variant.get("id") or "") for variant in self.list_audio_variants()}
        if normalized_variant_id not in available_ids:
            self._notify_user("That audio version is no longer available.", level="warning")
            normalized_variant_id = audio_variant_handler.SOURCE_VARIANT_ID

        if getattr(self.state, "active_audio_variant_id", audio_variant_handler.SOURCE_VARIANT_ID) == normalized_variant_id:
            return

        self.stop_playback()
        self.state.active_audio_variant_id = normalized_variant_id
        self._persist_session_config(force=True)
        self.state_changed.emit()

    def get_audio_variant_sentences_snapshot(self) -> list[dict]:
        sentences = self.get_processed_sentences_snapshot()
        active_variant_id = self.get_active_audio_variant_id()
        if active_variant_id == audio_variant_handler.SOURCE_VARIANT_ID:
            return sentences

        variant = next(
            (
                record
                for record in self.list_audio_variants()
                if str(record.get("id") or "") == active_variant_id
            ),
            None,
        )
        if variant is None:
            return sentences

        available_numbers = {str(number) for number in list(variant.get("sentence_numbers") or [])}
        visible_sentences: list[dict] = []
        for sentence in sentences:
            sentence_number = sentence.get("sentence_number")
            if sentence_number is None or str(sentence_number) not in available_numbers:
                continue

            visible_sentence = copy.deepcopy(sentence)
            visible_sentence["tts_generated"] = "yes"
            visible_sentences.append(visible_sentence)
        return visible_sentences

    def get_source_audio_sentence_numbers(self) -> list[str]:
        return self._source_available_sentence_numbers()

    def _session_dir_has_sentence_wavs(self, session_dir: str) -> bool:
        """Returns True when a session folder contains generated sentence WAVs."""
        if not session_dir:
            return False

        wavs_dir = os.path.join(session_dir, "Sentence_wavs")
        if not os.path.isdir(wavs_dir):
            return False

        try:
            for file_name in os.listdir(wavs_dir):
                file_path = os.path.join(wavs_dir, file_name)
                if os.path.isfile(file_path) and file_name.lower().endswith(".wav"):
                    return True
        except OSError:
            return False

        return False

    def _resolve_dubbing_video_source(self) -> str:
        video_source = self.state.source_file_path or ""
        if os.path.splitext(video_source)[1].lower() != ".srt":
            if video_source:
                self._register_dubbing_artifact("video_source", video_source, is_current=True)
            return video_source

        active_run = self._synchronize_active_dubbing_run()
        if active_run:
            run_video = str(active_run.get("source_video_path") or "").strip()
            if run_video and os.path.exists(run_video):
                if not self.state.dubbing.video_file_path:
                    self.state.dubbing.video_file_path = run_video
                return run_video

        discovered_video = (
            self.state.dubbing.video_file_path
            or session_handler.discover_video_file(self.state.session_name)
        )
        if discovered_video and not self.state.dubbing.video_file_path:
            self.state.dubbing.video_file_path = discovered_video

        if discovered_video:
            self._register_dubbing_artifact("video_source", discovered_video, is_current=True)

        return discovered_video or ""

    def _set_pending_dubbing_add_to_video_request(
        self,
        subtitle_mode: str,
        dubbed_audio_only: bool,
    ):
        normalized_subtitle_mode = str(subtitle_mode or "soft").strip().lower()
        if normalized_subtitle_mode not in {"soft", "burned", "both"}:
            normalized_subtitle_mode = "soft"

        with self._state_lock:
            self._pending_dubbing_add_to_video_request = {
                "subtitle_mode": normalized_subtitle_mode,
                "dubbed_audio_only": bool(dubbed_audio_only),
            }

    def _consume_pending_dubbing_add_to_video_request(self) -> dict[str, object] | None:
        with self._state_lock:
            request = self._pending_dubbing_add_to_video_request
            self._pending_dubbing_add_to_video_request = None
        return request

    def _clear_pending_dubbing_add_to_video_request(self):
        with self._state_lock:
            self._pending_dubbing_add_to_video_request = None

    def _is_generation_running(self) -> bool:
        return bool(self.generation_thread and self.generation_thread.is_alive())

    def _is_regeneration_running(self) -> bool:
        return bool(self.regeneration_thread and self.regeneration_thread.is_alive())

    def _is_rvc_processing_running(self) -> bool:
        return bool(self.rvc_processing_thread and self.rvc_processing_thread.is_alive())

    def _is_generation_or_regeneration_running(self) -> bool:
        return (
            self._is_generation_running()
            or self._is_regeneration_running()
            or self._is_rvc_processing_running()
        )

    def _is_xtts_training_running(self) -> bool:
        return bool(self.xtts_training_thread and self.xtts_training_thread.is_alive())

    def is_generation_running(self) -> bool:
        """Public lifecycle helper for UI state decisions."""
        return self._is_generation_running()

    def is_regeneration_running(self) -> bool:
        """Public lifecycle helper for UI state decisions."""
        return self._is_regeneration_running()

    def is_rvc_processing_running(self) -> bool:
        """Public lifecycle helper for RVC post-processing state."""
        return self._is_rvc_processing_running()

    def is_generation_or_regeneration_running(self) -> bool:
        """Returns True while any sentence-audio workflow is active."""
        return self._is_generation_or_regeneration_running()

    def is_xtts_training_running(self) -> bool:
        """Returns True when XTTS training is active."""
        return self._is_xtts_training_running()

    def is_tts_connection_running(self) -> bool:
        """Returns True while a TTS connection check is in progress."""
        return bool(self._tts_connection_thread and self._tts_connection_thread.is_alive())

    def _set_text_preprocessing_running(self, running: bool):
        running = bool(running)
        with self._state_lock:
            if self._text_preprocessing_running == running:
                return
            self._text_preprocessing_running = running

        self.state_changed.emit()

    def is_text_preprocessing_running(self) -> bool:
        """Returns True while text preprocessing is active."""
        with self._state_lock:
            return bool(self._text_preprocessing_running)

    def has_resumable_generation_progress(self) -> bool:
        """Returns True when generation has partial progress that can be resumed."""
        has_generated = False
        has_pending = False

        with self._state_lock:
            for sentence in self.state.processed_sentences:
                if sentence.get("tts_generated") == "yes":
                    has_generated = True
                else:
                    has_pending = True

                if has_generated and has_pending:
                    return True

        return False

    def has_any_generation_progress(self) -> bool:
        """Returns True if there is at least one sentence with generated audio."""
        with self._state_lock:
            return any(s.get("tts_generated") == "yes" for s in self.state.processed_sentences)

    def _have_preprocessing_settings_changed(self) -> bool:
        """Checks if current preprocessing settings differ from the ones stored in metadata."""
        current_settings = {
            "pdf_preprocessed": self.state.pdf_preprocessed,
            "source_file": self.state.source_file_path,
            "disable_paragraph_detection": self.state.text_processing.disable_paragraph_detection,
            "language": self.state.tts.language,
            "max_sentence_length": self.state.text_processing.max_sentence_length,
            "enable_sentence_splitting": self.state.text_processing.enable_sentence_splitting,
            "enable_sentence_appending": self.state.text_processing.enable_sentence_appending,
            "remove_diacritics": self.state.text_processing.remove_diacritics,
            "remove_quotation_marks": self.state.text_processing.remove_quotation_marks,
            "tts_service": self.state.tts.service,
            "remove_footnotes": self.state.text_processing.remove_footnotes,
            "filter_citations": self.state.text_processing.filter_citations
        }

        saved_settings_str = self.state.metadata.get("preprocessing_settings")
        if not saved_settings_str:
            return True

        try:
            saved_settings = json.loads(saved_settings_str)
            for key, val in current_settings.items():
                saved_val = saved_settings.get(key)
                if saved_val is None:
                    if isinstance(val, bool) and not val:
                        continue
                    return True
                if saved_val != val:
                    return True
            return False
        except Exception:
            return True


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
            if self.is_text_preprocessing_running():
                return "Processing Text"
            return "Generating"

        if self.is_text_preprocessing_running():
            return "Processing Text"

        if self._is_regeneration_running():
            if self.cancel_regeneration_flag.is_set():
                return "Cancelling"
            return "Regenerating"

        if self._is_rvc_processing_running():
            return "RVC Processing"

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
        target_state.kokoro_default_voices = self._normalize_kokoro_default_voices(
            getattr(target_state, "kokoro_default_voices", {})
        )
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

        if service_lower in {"voxcpm", "voxcpm2"}:
            target_state.service = "VoxCPM"
            return

        if service_lower in {"fishs2", "fish-s2", "fishs2cpp", "fishs2-cpp"}:
            target_state.service = "FishS2"
            return

        supported_services = {
            "XTTS",
            "VoxCPM",
            "FishS2",
            "Voxtral",
            "Kokoro",
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
        current_display_path = self.state.source_display_path or self.state.source_file_path
        current_abs = os.path.abspath(current_display_path) if current_display_path else ""
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
        self._active_dubbing_run_id = None

        session_dir = session_handler.get_session_path(session_name)
        os.makedirs(os.path.join(session_dir, "Sentence_wavs"), exist_ok=True)

        self.stop_playback()
        self.state.source_file_path = ""
        self.state.source_display_path = ""
        self.state.original_source_file_path = ""
        self.state.raw_text = ""
        self.state.pdf_preprocessed = False
        self.state.active_audio_variant_id = audio_variant_handler.SOURCE_VARIANT_ID
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
                return file_handler.extract_text_from_epub(
                    source_path,
                    remove_footnotes=self.state.text_processing.remove_footnotes,
                    filter_citations=self.state.text_processing.filter_citations,
                )
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
        self._active_dubbing_run_id = None
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
        try:
            state_db_handler.reindex_session(session_name)
        except Exception:
            pass
        self._reset_session_activity()
        self._notify_user(f"New session created: {session_name}")
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
        if isinstance(saved_state_payload, dict):
            llm_payload = saved_state_payload.get("llm")
            if isinstance(llm_payload, dict):
                has_enabled_legacy = False
                for p_key in ("second_prompt", "third_prompt"):
                    p_dict = llm_payload.get(p_key)
                    if isinstance(p_dict, dict) and p_dict.get("enabled", False):
                        has_enabled_legacy = True
                if has_enabled_legacy:
                    restored_state.llm.use_multi_stage = True
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

        if restored_state.source_display_path and not os.path.isabs(restored_state.source_display_path):
            relative_display_source = os.path.join(
                session_handler.get_session_path(session_name),
                restored_state.source_display_path,
            )
            if os.path.exists(relative_display_source):
                restored_state.source_display_path = os.path.abspath(relative_display_source)

        if not restored_state.source_file_path or not os.path.exists(restored_state.source_file_path):
            discovered_source = session_handler.discover_source_file(session_name)
            if discovered_source:
                restored_state.source_file_path = discovered_source
                if not restored_state.source_display_path:
                    restored_state.source_display_path = discovered_source

        if not restored_state.source_display_path:
            restored_state.source_display_path = restored_state.source_file_path

        if restored_state.original_source_file_path and not os.path.isabs(restored_state.original_source_file_path):
            relative_orig = os.path.join(session_handler.get_session_path(session_name), restored_state.original_source_file_path)
            if os.path.exists(relative_orig):
                restored_state.original_source_file_path = os.path.abspath(relative_orig)

        if not restored_state.original_source_file_path or not os.path.exists(restored_state.original_source_file_path):
            if restored_state.original_source_file_path:
                cand = os.path.join(session_handler.get_session_path(session_name), os.path.basename(restored_state.original_source_file_path))
                if os.path.exists(cand):
                    restored_state.original_source_file_path = os.path.abspath(cand)
            if (not restored_state.original_source_file_path or not os.path.exists(restored_state.original_source_file_path)) and restored_state.source_file_path:
                restored_state.original_source_file_path = restored_state.source_file_path

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
        active_run = self._synchronize_active_dubbing_run()
        self._active_dubbing_run_id = str(active_run.get("run_id") or "") if active_run else None
        self.get_active_audio_variant_id()
        if self.state.tts.service in {
            tts_handler.OPENAI_SERVICE,
            tts_handler.GEMINI_SERVICE,
            tts_handler.OPENAI_COMPAT_SERVICE,
        } and not self.state.tts.tts_models:
            self.populate_cloud_tts_catalogs(use_remote=False, emit_state=False)

        self._loaded_llm_model = None
        self._persist_global_settings(force=True)
        self._persist_session_config(force=True)
        try:
            state_db_handler.reindex_session(session_name)
        except Exception:
            pass
        self._reset_session_activity()
        self._notify_user(f"Session loaded: {session_name}")
        self.state_changed.emit()

    def delete_session(self, session_name: str):
        """Deletes a session from disk."""
        if session_name == self.state.session_name and self.is_generation_or_regeneration_running():
            self.show_error.emit(
                "Error",
                "Stop or wait for generation, regeneration, or RVC processing before deleting the active session.",
            )
            return

        if session_handler.delete_session(session_name):
            self._notify_user(f"Session '{session_name}' deleted.")
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
                self._active_dubbing_run_id = None
                self._last_session_config_snapshot = ""
                self._persist_global_settings(force=True)
                self._reset_session_activity()
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

    def open_folder_path(self, path: str):
        """Opens a filesystem path in the OS file explorer."""
        import platform

        normalized_path = os.path.abspath(str(path or "").strip())
        if not normalized_path or not os.path.exists(normalized_path):
            self.show_error.emit("Error", f"Folder not found: {normalized_path or path}")
            return

        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(normalized_path)
            elif system == "Darwin":
                subprocess.Popen(["open", normalized_path])
            else:
                subprocess.Popen(["xdg-open", normalized_path])
        except Exception as e:
            self.show_error.emit("Error", f"Could not open folder: {e}")

    def list_indexed_sessions(self, search_query: str = "", include_trashed: bool = False) -> list[dict]:
        """Returns session rows from SQLite index for the Sessions tab."""
        return session_handler.list_indexed_sessions(
            search_query=search_query,
            include_trashed=include_trashed,
        )

    def list_reusable_sources(self, limit: int = 300) -> list[dict]:
        """Returns deduplicated source file entries that can be reused."""
        return session_handler.list_reusable_sources(limit=limit, include_missing=False)

    def get_session_index_preview(self, session_name: str) -> dict:
        """Returns detailed indexed session preview (config, runs, artifacts)."""
        return session_handler.get_session_index_preview(session_name)

    def reindex_sessions(self, session_name: str | None = None) -> list[dict]:
        """Reindexes one or all sessions into SQLite state."""
        if session_name:
            preview = session_handler.reindex_session(session_name)
            return [preview] if preview else []
        return session_handler.reindex_all_sessions()

    def move_session_to_trash(self, session_name: str, retention_days: int = 30) -> tuple[bool, str]:
        """Moves a session directory to Outputs/.trash."""
        if session_name == self.state.session_name and self.is_generation_or_regeneration_running():
            return False, "Cannot trash the active session while generation/regeneration is running."

        moved, details = session_handler.move_session_to_trash(session_name, retention_days=retention_days)
        if moved and session_name == self.state.session_name:
            current_tts_state = self.state.tts
            provider_settings_payload = settings_handler.build_global_settings_payload(self.state)
            self.state = AppState()
            self.state.tts = current_tts_state
            settings_handler.apply_global_settings_payload(self.state, provider_settings_payload)
            self._normalize_tts_service_state(self.state.tts)
            self.normalize_dubbing_translation_state(self.state.dubbing)
            self._loaded_llm_model = None
            self._active_dubbing_run_id = None
            self._last_session_config_snapshot = ""
            self._persist_global_settings(force=True)
            self.state_changed.emit()
        return moved, details

    def restore_session_from_trash(self, session_name: str, trash_path: str = "") -> tuple[bool, str]:
        """Restores a trashed session."""
        return session_handler.restore_session_from_trash(session_name, trash_path=trash_path)

    def list_trashed_sessions(self) -> list[dict]:
        """Lists active trash entries from SQLite."""
        return session_handler.list_trashed_sessions()

    def empty_expired_trash(self, retention_days: int = 30) -> tuple[int, list[str]]:
        """Removes expired trash entries."""
        return session_handler.empty_expired_trash(retention_days=retention_days)

    def permanently_delete_trashed_session(self, session_name: str = "", trash_path: str = "") -> tuple[bool, str]:
        """Permanently removes a selected trashed session."""
        return session_handler.permanently_delete_trashed_session(
            session_name=session_name,
            trash_path=trash_path,
        )


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
            self._notify_user("PyCropPDF not found. Continuing with the original PDF.", level="warning")
            return pdf_file_path

        source_dir = os.path.dirname(pdf_file_path)
        source_filename = os.path.splitext(os.path.basename(pdf_file_path))[0]
        cropped_filename = f"{source_filename}_cropped.pdf"
        cropped_path = os.path.join(source_dir, cropped_filename)

        try:
            self._notify_user("Opening PyCropPDF. Save and close it when finished.")
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
            self._notify_user(f"Using cropped PDF: {cropped_path}")
            return cropped_path

        self._notify_user("No cropped PDF was saved. Using the original PDF.", level="warning")
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
        previous_display_source = self.state.source_display_path
        previous_original_source = self.state.original_source_file_path
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
                self._notify_user("Cleared existing session artifacts before loading a new source.")

            session_file_path = self._ensure_session_file_copy(source_path_to_load)
            self.state.source_file_path = session_file_path
            self.state.source_display_path = session_file_path
            self.state.original_source_file_path = session_file_path
            self.state.pdf_preprocessed = False
            self.log_message.emit(f"Source file selected: {session_file_path}")

            raw_text = ""
            ext = os.path.splitext(session_file_path)[1].lower()
            session_path = session_handler.get_session_path(self.state.session_name)

            if ext == ".txt":
                with open(session_file_path, "r", encoding="utf-8") as f:
                    raw_text = f.read()
            elif ext == ".epub":
                raw_text = file_handler.extract_text_from_epub(
                    session_file_path,
                    remove_footnotes=self.state.text_processing.remove_footnotes,
                    filter_citations=self.state.text_processing.filter_citations,
                )
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
                self._active_dubbing_run_id = None

            self.state.raw_text = raw_text
            if raw_text:
                self.log_message.emit("Text extracted successfully.")
                self._set_session_activity(
                    "Source ready",
                    "The text source is loaded. Start generation when you're ready.",
                    "idle",
                )
            elif self._is_dubbing_source_selected():
                self._set_session_activity(
                    "Dubbing source ready",
                    "The dubbing source is loaded. Run a dubbing step or generate dubbing audio when you're ready.",
                    "idle",
                )
            else:
                self._reset_session_activity()

            selected_source_name = os.path.basename(self.state.source_display_path or self.state.source_file_path or session_file_path)
            self._notify_user(self._build_source_loaded_notification(selected_source_name, ext))

            self._persist_session_config(force=True)
            if self._is_dubbing_source_selected():
                run = self._create_dubbing_run(set_active=True, force_new=False)
                if run:
                    run_id = str(run.get("run_id") or "")
                    if ext == ".srt":
                        self._register_dubbing_artifact("source_srt", self.state.source_file_path, is_current=True)
                    elif ext in {".mp4", ".mkv", ".webm", ".avi", ".mov"}:
                        self._register_dubbing_artifact("video_source", self.state.source_file_path, is_current=True)
                    if run_id:
                        try:
                            state_db_handler.update_dubbing_run_status(run_id, "ready")
                        except Exception:
                            pass
            self.state_changed.emit()
            return True

        except Exception as e:
            if reset_applied:
                self.state.source_file_path = ""
                self.state.source_display_path = ""
                self.state.original_source_file_path = ""
                self.state.raw_text = ""
                self.state.pdf_preprocessed = False
                self.state.dubbing.video_file_path = ""
                self._set_processed_sentences_snapshot([], persist=True)
                self._persist_session_config(force=True)
                self.state_changed.emit()
            else:
                self.state.source_file_path = previous_source
                self.state.source_display_path = previous_display_source
                self.state.original_source_file_path = previous_original_source
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

    def extract_deterministic_clean_text(
        self,
        file_path: str,
        remove_footnotes: bool,
        filter_citations: bool,
    ) -> str:
        """Deterministically extracts and cleans text from the specified file on-demand."""
        hint_path = os.path.abspath(str(file_path or "").strip()) if file_path else ""
        state_path = os.path.abspath(str(self.state.source_file_path or "").strip()) if self.state.source_file_path else ""
        source_path = hint_path if hint_path and os.path.exists(hint_path) else state_path
        
        if not source_path or not os.path.exists(source_path):
            return self.state.raw_text or ""
            
        source_ext = os.path.splitext(source_path)[1].lower()
        if source_ext == ".epub":
            return file_handler.extract_text_from_epub(
                source_path,
                remove_footnotes=remove_footnotes,
                filter_citations=filter_citations,
            )
        elif source_ext == ".pdf":
            return file_handler.extract_text_from_pdf(source_path)
        elif source_ext == ".txt":
            with open(source_path, "r", encoding="utf-8") as f:
                return f.read()
        elif source_ext in [".docx", ".mobi"]:
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_txt = os.path.join(tmpdir, "extracted.txt")
                if file_handler.convert_doc_to_text(source_path, tmp_txt):
                    with open(tmp_txt, "r", encoding="utf-8") as f:
                        return f.read()
        return self.state.raw_text or ""

    def run_source_cleaning(
        self,
        source_path_hint: str = "",
        remove_footnotes: bool = False,
        filter_citations: bool = True,
        model_name: str | None = None,
        max_iterations: int | None = None,
        reasoning_effort: str | None = None,
        progress_callback=None,
        stop_event=None,
        extracted_text: str | None = None,
    ) -> dict:
        """Runs the source-cleaning agent without mutating session state."""
        source_raw = extracted_text if extracted_text is not None else self.state.raw_text
        if not source_raw:
            raise ValueError("No extracted source text is available for cleaning.")

        if not self._is_named_session_active():
            raise ValueError("Please create or load a session before cleaning source text.")

        def emit_progress(message: str):
            if progress_callback is not None:
                progress_callback(message)
            self.log_message.emit(message)

        hint_path = os.path.abspath(str(source_path_hint or "").strip()) if source_path_hint else ""
        state_path = os.path.abspath(str(self.state.source_file_path or "").strip()) if self.state.source_file_path else ""
        source_path = hint_path if hint_path and os.path.exists(hint_path) else state_path
        source_ext = os.path.splitext(source_path)[1].lower() if source_path else ""

        emit_progress("Building structured source index...")
        if source_ext == ".epub":
            document = source_cleaning.build_source_document(
                source_path,
                extracted_text=source_raw,
            )
        elif source_ext == ".pdf":
            document = source_cleaning.build_source_document(
                source_path,
                extracted_text=source_raw,
            )
        else:
            from .logic.source_cleaning.pdf_text_adapter import build_source_document_from_text

            document = build_source_document_from_text(
                source_raw,
                source_path=state_path or source_path,
                filename=os.path.basename(state_path or source_path or "source.txt"),
            )

        session_dir = session_handler.get_session_path(self.state.session_name)
        output_dir = os.path.join(session_dir, "_source_cleaning")
        resolved_model = str(model_name or "default").strip() or "default"

        # Build an llm_settings view that reflects any per-run reasoning_effort
        # override.  We copy rather than mutate so the global state stays clean.
        llm_settings_for_run = self.state.llm
        if reasoning_effort is not None:
            import copy as _copy
            llm_settings_for_run = _copy.copy(self.state.llm)
            llm_settings_for_run.reasoning_effort = str(reasoning_effort).strip()

        pipeline_config = source_cleaning.SourceCleaningPipelineConfig(
            model_name=resolved_model,
            remove_footnotes=remove_footnotes,
            filter_citations=filter_citations,
            total_max_iterations=int(max_iterations) if max_iterations is not None else 53,
        )
        emit_progress("Running source-cleaning pipeline...")
        pipeline_result = source_cleaning.run_cleaning_pipeline(
            document,
            llm_settings=llm_settings_for_run,
            config=pipeline_config,
            progress_callback=emit_progress,
            stop_event=stop_event,
        )

        emit_progress("Applying proposed cleaning operations...")
        cleaning_result = source_cleaning.apply_cleaning_operations(
            document,
            pipeline_result.all_operations,
            default_metadata=self.state.metadata,
        )
        validation = source_cleaning.validate_cleaning_result(
            document,
            cleaning_result,
            remove_footnotes=remove_footnotes,
        )

        cleaning_result.report["pipeline"] = pipeline_result.to_dict()
        cleaning_result.report["llm_usage"] = pipeline_result.llm_usage
        cleaning_result.report["validation"] = validation.to_dict()
        cleaning_result.report["artifacts_dir"] = output_dir
        source_cleaning.write_cleaning_artifacts(
            document,
            pipeline_result.all_operations,
            cleaning_result,
            output_dir,
        )

        emit_progress("Source-cleaning pipeline finished.")
        return {
            "success": not validation.errors and not validation.blocking_warnings,
            "cleaned_text": cleaning_result.cleaned_text,
            "metadata": cleaning_result.metadata,
            "diff": cleaning_result.diff_text,
            "report": cleaning_result.report,
            "validation": validation.to_dict(),
            "operations": pipeline_result.all_operations,
            "applied_operations": cleaning_result.applied_operations,
            "skipped_operations": cleaning_result.skipped_operations,
            "warnings": pipeline_result.warnings + validation.warnings + cleaning_result.warnings,
            "artifacts_dir": output_dir,
            "pipeline": pipeline_result.to_dict(),
            "llm_usage": pipeline_result.llm_usage,
        }


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
            self.state.source_display_path = edited_file_path
            self.state.raw_text = reviewed_text
            self.state.pdf_preprocessed = mark_pdf_preprocessed
            self._set_processed_sentences_snapshot([])

            self._set_session_activity(
                "Reviewed text ready",
                "The edited text is saved. Start generation when you're ready.",
                "idle",
            )
            self._notify_user(f"Reviewed text saved: {os.path.basename(edited_file_path)}")
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
        self.state.source_display_path = ""
        self.state.original_source_file_path = ""
        self.state.raw_text = ""
        self.state.pdf_preprocessed = False
        self._set_processed_sentences_snapshot([])
        self._reset_session_activity()
        self._notify_user("Source file selection cleared.")
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
                self._notify_user("Cleared existing session artifacts before loading pasted text.")

            file_path = file_handler.save_pasted_text(text, session_dir, mark_paragraphs)
            # This re-uses the file selection logic to load the text and update the UI
            if self.select_source_file(file_path):
                self._notify_user("Pasted text saved and loaded.")
        except Exception as e:
            logging.error(f"Failed to save pasted text: {e}", exc_info=True)
            self.show_error.emit("Paste Error", f"Could not save pasted text: {e}")

    def select_cover_image(self, file_path: str):
        """Updates the cover image path in the state."""
        self.state.cover_image_path = file_path
        self._notify_user(f"Cover image selected: {os.path.basename(file_path)}")
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
            if self._is_named_session_active():
                self._ensure_dubbing_run_for_task("transcribe")
                self._register_dubbing_artifact("video_source", session_video_path, is_current=True)
            self._notify_user(f"Dubbing video selected: {os.path.basename(session_video_path)}")
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
            self._notify_user("Starting source download...")
            session_dir = session_handler.get_session_path(self.state.session_name)
            os.makedirs(session_dir, exist_ok=True)
            try:
                video_path = file_handler.download_video_from_url(url, session_dir)
                self._notify_user(f"Download complete: {os.path.basename(video_path)}")
                self._download_source_ready.emit(video_path, bool(reset_session))
            except Exception as e:
                logging.error(f"Failed to download from URL: {e}", exc_info=True)
                self.show_error.emit("Download Error", f"Could not download video: {e}")

        threading.Thread(target=thread_target, daemon=True).start()

    def _on_download_source_ready(self, file_path: str, reset_session: bool):
        """Applies a downloaded source file update on the Qt/main thread."""
        self.select_source_file(file_path, reset_session=bool(reset_session))

    # --- Text Processing ---

    def _find_latest_srt(self, session_dir: str, must_not_be_equalized=False) -> str | None:
        """Finds the most recently modified SRT file in a preferred directory with session fallback."""
        if self._is_named_session_active():
            try:
                preferred_roles = [
                    "manual_timing_srt",
                    "translated_srt",
                    "corrected_srt",
                    "transcribed_srt",
                    "source_srt",
                ]
                if not must_not_be_equalized:
                    preferred_roles = ["equalized_srt", *preferred_roles]
                artifact_path = state_db_handler.get_active_dubbing_artifact(
                    self.state.session_name,
                    roles=preferred_roles,
                )
                if artifact_path and os.path.exists(artifact_path) and artifact_path.lower().endswith(".srt"):
                    if must_not_be_equalized and artifact_path.lower().endswith("_equalized.srt"):
                        pass
                    else:
                        return artifact_path
            except Exception:
                pass

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

    def has_dubbing_srt_file(self) -> bool:
        """Returns True when a non-equalized SRT exists for the active dubbing session."""
        if not self._is_named_session_active():
            return False

        dubbing_session_dir = self._get_dubbing_work_dir(ensure_exists=False)
        return bool(self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True))

    def _prepare_manual_correction_audio_file(self, session_dir: str) -> str | None:
        """Resolves or creates an audio reference file for manual SRT boundary correction."""
        audio_file = subdub_handler.find_latest_audio_file(session_dir)
        if audio_file and os.path.exists(audio_file):
            self._register_dubbing_artifact("manual_correction_audio", audio_file, is_current=True)
            return audio_file

        video_source = self._resolve_dubbing_video_source()
        if not video_source or not os.path.exists(video_source):
            return None

        self._set_session_activity(
            "Preparing timing editor",
            "Extracting an audio reference from the selected video before opening the timing editor.",
            "active",
        )
        self.log_message.emit("No session audio found for timing correction. Extracting audio from video...")
        audio_file = subdub_handler.extract_audio_for_manual_correction(video_source, session_dir)
        if audio_file and os.path.exists(audio_file):
            self._register_dubbing_artifact("manual_correction_audio", audio_file, is_current=True)
        return audio_file

    def _run_manual_timing_correction(self, session_dir: str):
        """Launches Subdub's GUI for manual subtitle boundary adjustment."""
        srt_file = self._find_latest_srt(session_dir, must_not_be_equalized=True)
        if not srt_file:
            self._set_session_activity(
                "Timing fine-tuning unavailable",
                "No subtitle file was found. Run transcription first.",
                "error",
            )
            self.show_error.emit(
                "Dubbing Error",
                "No SRT file found. Select an SRT file or run transcription first.",
            )
            return

        audio_file = self._prepare_manual_correction_audio_file(session_dir)
        if not audio_file or not os.path.exists(audio_file):
            self._set_session_activity(
                "Timing fine-tuning unavailable",
                "A matching audio reference is required before opening the timing editor.",
                "error",
            )
            self.show_error.emit(
                "Dubbing Error",
                "No audio reference found for timing correction. Select a matching video first.",
            )
            return

        self._set_session_activity(
            "Opening timing editor",
            "Launching the Subdub timing editor for manual subtitle boundary adjustments.",
            "active",
        )
        self.log_message.emit(f"Opening Subdub timing editor for {srt_file}...")
        self._mark_dubbing_step("manual_timing", "running")
        initial_mtime = os.path.getmtime(srt_file) if os.path.exists(srt_file) else 0.0
        corrected_srt = subdub_handler.open_manual_correction_gui(srt_file, audio_file, session_dir)
        if not corrected_srt or not os.path.exists(corrected_srt):
            self._mark_dubbing_step("manual_timing", "failed", "No corrected SRT returned.")
            self._set_session_activity(
                "Timing fine-tuning failed",
                "The timing editor did not return a valid subtitle file.",
                "error",
            )
            self.show_error.emit(
                "Dubbing Error",
                "Subdub timing editor did not return a valid SRT file.",
            )
            return

        corrected_srt_abs = os.path.abspath(corrected_srt)
        original_srt_abs = os.path.abspath(srt_file)
        updated = (
            os.path.normcase(corrected_srt_abs) != os.path.normcase(original_srt_abs)
            or os.path.getmtime(corrected_srt_abs) > initial_mtime
        )

        if updated:
            self._set_session_activity(
                "Timing fine-tuning complete",
                "Updated subtitle timing is saved and ready for the next dubbing step.",
                "success",
            )
            self.log_message.emit(f"Timing fine-tuning complete: {corrected_srt_abs}")
            self._register_dubbing_artifact("manual_timing_srt", corrected_srt_abs, is_current=True)
            self._register_dubbing_artifact("transcribed_srt", corrected_srt_abs, is_current=True)
            self._mark_dubbing_step("manual_timing", "completed")
        else:
            self._set_session_activity(
                "Timing editor closed",
                "The timing editor closed without saving subtitle changes.",
                "warning",
            )
            self.log_message.emit("Subdub timing editor closed without saving changes.")
            self._mark_dubbing_step("manual_timing", "completed", "Closed without changes.")

        if os.path.splitext(self.state.source_file_path or "")[1].lower() == ".srt":
            self.state.source_file_path = corrected_srt_abs
            self.state.source_display_path = corrected_srt_abs
            self._persist_session_config(force=True)

        self.state_changed.emit()

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
        normalized_suffix = suffix.lower()
        if self._is_named_session_active():
            try:
                role_lookup = {
                    "_speech_blocks.json": ["speech_blocks"],
                    "_equalized.srt": ["equalized_srt"],
                }
                roles = role_lookup.get(normalized_suffix)
                if roles:
                    artifact_path = state_db_handler.get_active_dubbing_artifact(
                        self.state.session_name,
                        roles=roles,
                    )
                    if artifact_path and os.path.exists(artifact_path):
                        return artifact_path
            except Exception:
                pass

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
            self._register_dubbing_artifact("speech_blocks", speech_blocks_file, is_current=True)
            sentences = session_handler.import_speech_blocks_to_session(self.state.session_name, speech_blocks_file)
            if not sentences:
                self._set_session_activity(
                    "Preparing sentence list failed",
                    "The generated speech blocks file was empty.",
                    "error",
                )
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
            self._set_session_activity(
                "Preparing sentence list failed",
                "The speech blocks file could not be found after generation.",
                "error",
            )
            self.show_error.emit("Dubbing Error", "Speech blocks file was not found after generation.")
            return False
        except json.JSONDecodeError as e:
            self._set_session_activity(
                "Preparing sentence list failed",
                f"Speech blocks JSON could not be parsed: {e}",
                "error",
            )
            self.show_error.emit("Dubbing Error", f"Speech blocks JSON could not be parsed: {e}")
            return False
        except ValueError as e:
            self._set_session_activity(
                "Preparing sentence list failed",
                f"Speech blocks format is invalid: {e}",
                "error",
            )
            self.show_error.emit("Dubbing Error", f"Speech blocks format is invalid: {e}")
            return False
        except Exception as e:
            logging.error(f"Failed to import speech blocks from '{speech_blocks_file}': {e}", exc_info=True)
            self._set_session_activity(
                "Preparing sentence list failed",
                f"Could not import speech blocks: {e}",
                "error",
            )
            self.show_error.emit("Dubbing Error", f"Could not import speech blocks: {e}")
            return False

    def _run_threaded_task(self, target, *args):
        """Helper to run a function in a background thread."""
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        return thread

    def run_dubbing_task(
        self,
        task: str,
        subtitle_mode: str = "soft",
        dubbed_audio_only: bool = False,
        auto_add_to_video: bool = False,
    ):
        """Runs a dubbing-related task like transcribe, translate, etc., in a background thread."""
        if self.is_generation_or_regeneration_running():
            self._notify_user(
                "Cannot run dubbing tasks while generation or regeneration is active.",
                level="warning",
            )
            return

        self.normalize_dubbing_translation_state(self.state.dubbing)

        self._run_threaded_task(
            self._run_dubbing_task_thread,
            task,
            subtitle_mode,
            dubbed_audio_only,
            auto_add_to_video,
        )

    def _run_dubbing_task_thread(
        self,
        task: str,
        subtitle_mode: str = "soft",
        dubbed_audio_only: bool = False,
        auto_add_to_video: bool = False,
    ):
        """The actual threaded implementation for dubbing tasks."""
        self.log_message.emit(f"Starting dubbing task: {task}")
        run_info = self._ensure_dubbing_run_for_task(task)
        if run_info is None:
            self._set_session_activity(
                "Dubbing task failed",
                "The app could not initialize a dubbing run for the active session.",
                "error",
            )
            self.show_error.emit("Dubbing Error", "Could not initialize a dubbing run for this session.")
            return

        run_id = str(run_info.get("run_id") or "")
        self._active_dubbing_run_id = run_id or self._active_dubbing_run_id
        session_output_dir = session_handler.get_session_final_output_path(self.state.session_name)
        dubbing_session_dir = str(run_info.get("run_dir") or self._get_dubbing_work_dir(ensure_exists=True))
        os.makedirs(dubbing_session_dir, exist_ok=True)
        dub_settings_payload = copy.deepcopy(self.state.dubbing.__dict__)
        dub_settings_payload["llm_provider_configs"] = llm_handler.get_provider_configs(self.state.llm)
        correction_prompt = str(dub_settings_payload.get("custom_correction_prompt", ""))
        task_name = str(task or "").strip().lower()

        if task_name == "transcribe":
            self._set_session_activity(
                "Transcribing subtitles",
                "Extracting subtitle timings and text from the selected video.",
                "active",
            )
            video_source = self._resolve_dubbing_video_source()

            if not video_source or not os.path.exists(video_source):
                self._set_session_activity(
                    "Transcription failed",
                    "No valid video source was available for transcription.",
                    "error",
                )
                self.log_message.emit("No video source found for transcription.")
                self._mark_dubbing_step("transcribe", "failed", "No video source was available.")
                return

            self._mark_dubbing_step("transcribe", "running")
            if not subdub_handler.transcribe_video(
                dubbing_session_dir,
                video_source,
                dub_settings_payload,
                correction_prompt,
            ):
                self._mark_dubbing_step("transcribe", "failed", "Subdub transcription failed.")
                self._set_session_activity(
                    "Transcription failed",
                    "Subdub could not transcribe the selected video.",
                    "error",
                )
                self.show_error.emit("Dubbing Error", "Transcription failed. Check logs for details.")
                return

            transcribed_srt = self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True)
            if transcribed_srt:
                self._register_dubbing_artifact("transcribed_srt", transcribed_srt, is_current=True)
                self._mark_dubbing_step("transcribe", "completed")
                self._set_session_activity(
                    "Transcription complete",
                    "Subtitle text is ready. Use Fine-Tune Timings if you want manual boundary edits.",
                    "success",
                )
                self.log_message.emit(
                    "Transcription complete. Use 'Fine-Tune Timings (Subdub GUI)' to adjust subtitle boundaries."
                )
                self.state_changed.emit()
            else:
                self._set_session_activity(
                    "Transcription failed",
                    "No subtitle file was produced after transcription.",
                    "error",
                )
                self._mark_dubbing_step("transcribe", "failed", "No transcribed SRT was found.")
        elif task_name == "correct":
            srt_file = self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True)
            if srt_file:
                self._set_session_activity(
                    "Correcting subtitles",
                    "Using the configured correction workflow to clean up subtitle text.",
                    "active",
                )
                self._mark_dubbing_step("correct", "running")
                if not subdub_handler.correct_subtitles(
                    dubbing_session_dir,
                    srt_file,
                    dub_settings_payload,
                    correction_prompt,
                ):
                    self._mark_dubbing_step("correct", "failed", "Subdub correction failed.")
                    self._set_session_activity(
                        "Subtitle correction failed",
                        "Subdub could not correct the selected subtitle file.",
                        "error",
                    )
                    self.show_error.emit("Dubbing Error", "Subtitle correction failed. Check logs for details.")
                    return

                corrected_srt = self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True)
                if corrected_srt:
                    self._register_dubbing_artifact("corrected_srt", corrected_srt, is_current=True)
                self._mark_dubbing_step("correct", "completed")
                self._set_session_activity(
                    "Subtitle correction complete",
                    "Corrected subtitles are ready for translation or audio generation.",
                    "success",
                )
            else:
                self._set_session_activity(
                    "Subtitle correction unavailable",
                    "No subtitle file was found to correct.",
                    "error",
                )
                self.log_message.emit("No SRT file found to correct.")
                self._mark_dubbing_step("correct", "failed", "No SRT file was found.")
        elif task_name == "translate":
            srt_file = self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True)
            if srt_file:
                self._set_session_activity(
                    "Translating subtitles",
                    "Creating translated subtitle text with the selected provider and model.",
                    "active",
                )
                self._mark_dubbing_step("translate", "running")
                if not subdub_handler.translate_subtitles(
                    dubbing_session_dir,
                    srt_file,
                    dub_settings_payload,
                    correction_prompt,
                ):
                    self._mark_dubbing_step("translate", "failed", "Subdub translation failed.")
                    self._set_session_activity(
                        "Translation failed",
                        "Subdub could not translate the selected subtitle file.",
                        "error",
                    )
                    self.show_error.emit("Dubbing Error", "Translation failed. Check logs for details.")
                    return

                translated_srt = self._find_latest_srt(dubbing_session_dir, must_not_be_equalized=True)
                if translated_srt:
                    self._register_dubbing_artifact("translated_srt", translated_srt, is_current=True)
                self._mark_dubbing_step("translate", "completed")
                self._set_session_activity(
                    "Translation complete",
                    "Translated subtitles are ready for speech-block generation.",
                    "success",
                )
            else:
                self._set_session_activity(
                    "Translation unavailable",
                    "No subtitle file was found to translate.",
                    "error",
                )
                self.log_message.emit("No SRT file found to translate.")
                self._mark_dubbing_step("translate", "failed", "No SRT file was found.")
        elif task_name == "generate_audio":
            self._orchestrate_dubbing_audio_generation(
                dubbing_session_dir,
                dub_settings_payload,
                correction_prompt,
                subtitle_mode=subtitle_mode,
                dubbed_audio_only=dubbed_audio_only,
                auto_add_to_video=auto_add_to_video,
            )
        elif task_name == "fine_tune_timings":
            self._run_manual_timing_correction(dubbing_session_dir)
        elif task_name == "add_to_video":
            self._orchestrate_add_to_video(
                dubbing_session_dir,
                session_output_dir,
                subtitle_mode=subtitle_mode,
                dubbed_audio_only=dubbed_audio_only,
            )
        else:
            self.log_message.emit(f"Unknown dubbing task: {task}")

    def _orchestrate_dubbing_audio_generation(
        self,
        session_dir,
        dub_settings: dict,
        correction_prompt,
        subtitle_mode: str = "soft",
        dubbed_audio_only: bool = False,
        auto_add_to_video: bool = False,
    ):
        """Full workflow to generate dubbing audio from a video."""
        # 1. Transcribe if no SRT exists
        srt_file = self._find_latest_srt(session_dir, must_not_be_equalized=True)
        if not srt_file:
            self._set_session_activity(
                "Transcribing subtitles",
                "No subtitle file was found, so the app is transcribing the source video first.",
                "active",
            )
            self.log_message.emit("No SRT file found, starting transcription...")
            video_source = self._resolve_dubbing_video_source()

            if not video_source or not os.path.exists(video_source):
                self._set_session_activity(
                    "Transcription failed",
                    "A valid video source is required before dubbing audio can be generated.",
                    "error",
                )
                self._mark_dubbing_step("transcribe", "failed", "No valid video source for transcription.")
                self.show_error.emit("Dubbing Error", "No valid video file found for transcription.")
                return

            self._mark_dubbing_step("transcribe", "running")
            if not subdub_handler.transcribe_video(session_dir, video_source, dub_settings, correction_prompt):
                self._mark_dubbing_step("transcribe", "failed", "Subdub transcription failed.")
                self.show_error.emit("Dubbing Error", "Transcription failed. Check logs for details.")
                return
            srt_file = self._find_latest_srt(session_dir, must_not_be_equalized=True)
            if not srt_file:
                self._mark_dubbing_step("transcribe", "failed", "No transcribed SRT was produced.")
                self.show_error.emit("Dubbing Error", "Transcription did not produce an SRT file.")
                return
            self._register_dubbing_artifact("transcribed_srt", srt_file, is_current=True)
            self._mark_dubbing_step("transcribe", "completed")

            self.log_message.emit(
                "Transcription generated an SRT. Use 'Fine-Tune Timings (Subdub GUI)' before rerunning if you want manual boundary edits."
            )
        else:
            self._register_dubbing_artifact("source_srt", srt_file, is_current=True)

        # 2. Translate if enabled
        if bool(dub_settings.get("translation_enabled")):
            self._set_session_activity(
                "Translating subtitles",
                "Translation is enabled, so the subtitle text is being translated now.",
                "active",
            )
            self.log_message.emit("Translation is enabled, starting translation...")
            self._mark_dubbing_step("translate", "running")
            if not subdub_handler.translate_subtitles(session_dir, srt_file, dub_settings, correction_prompt):
                self._mark_dubbing_step("translate", "failed", "Subdub translation failed.")
                self._set_session_activity(
                    "Translation failed",
                    "The subtitle translation step did not complete successfully.",
                    "error",
                )
                self.show_error.emit("Dubbing Error", "Translation failed. Check logs for details.")
                return
            srt_file = self._find_latest_srt(session_dir, must_not_be_equalized=True) # Find the new translated file
            if not srt_file:
                self._mark_dubbing_step("translate", "failed", "No translated SRT was produced.")
                self._set_session_activity(
                    "Translation failed",
                    "Translation completed without producing a new subtitle file.",
                    "error",
                )
                self.show_error.emit("Dubbing Error", "Translation did not produce a new SRT file.")
                return
            self._register_dubbing_artifact("translated_srt", srt_file, is_current=True)
            self._mark_dubbing_step("translate", "completed")
        
        # 3. Generate speech blocks
        speech_blocks_snapshot = self._snapshot_speech_blocks_files(session_dir)
        self._set_session_activity(
            "Building speech blocks",
            "Turning subtitles into speech blocks that can be imported as session sentences.",
            "active",
        )
        self.log_message.emit("Generating speech blocks...")
        self._mark_dubbing_step("speech_blocks", "running")
        if not subdub_handler.generate_speech_blocks(session_dir, srt_file):
            self._mark_dubbing_step("speech_blocks", "failed", "Subdub speech-block generation failed.")
            self._set_session_activity(
                "Speech-block generation failed",
                "Subdub could not generate speech blocks from the subtitle file.",
                "error",
            )
            self.show_error.emit("Dubbing Error", "Speech block generation failed.")
            return

        speech_blocks_file = self._wait_for_speech_blocks_file(
            session_dir=session_dir,
            source_srt_file=srt_file,
            previous_snapshot=speech_blocks_snapshot,
        )
        if not speech_blocks_file:
            self._mark_dubbing_step("speech_blocks", "failed", "Speech-block JSON was not detected.")
            self._set_session_activity(
                "Speech-block generation failed",
                "The expected speech-block JSON file was not detected after generation.",
                "error",
            )
            self.show_error.emit("Dubbing Error", "Speech blocks file was not detected after generation.")
            return
        self._register_dubbing_artifact("speech_blocks", speech_blocks_file, is_current=True)
        self._mark_dubbing_step("speech_blocks", "completed")

        # 4. Convert speech blocks to Pandrator sentence JSON
        self._set_session_activity(
            "Preparing sentence list",
            "Importing speech blocks into the session so sentence audio generation can begin.",
            "active",
        )
        self.log_message.emit("Converting speech blocks into session sentences...")
        if not self._import_speech_blocks_into_sentences(speech_blocks_file):
            return

        self.state_changed.emit()

        # 5. Start audio generation
        if auto_add_to_video:
            self._set_pending_dubbing_add_to_video_request(
                subtitle_mode=subtitle_mode,
                dubbed_audio_only=dubbed_audio_only,
            )
            self._set_session_activity(
                "Generating dubbing audio",
                "Sentence audio generation is starting now. Final video rendering will begin automatically afterward.",
                "active",
            )
            self.log_message.emit(
                "Auto-continue enabled: the app will render the final video right after generation completes."
            )
        else:
            self._clear_pending_dubbing_add_to_video_request()
            self._set_session_activity(
                "Generating dubbing audio",
                "Sentence audio generation is starting now. You will be able to review and regenerate lines afterward.",
                "active",
            )

        self._mark_dubbing_step("tts_generation", "running")
        self.start_generation()

    def _orchestrate_add_to_video(
        self,
        dubbing_session_dir: str,
        session_output_dir: str,
        subtitle_mode: str = "soft",
        dubbed_audio_only: bool = False,
    ):
        """Full workflow to synchronize audio and add subtitles."""
        normalized_subtitle_mode = str(subtitle_mode or "soft").strip().lower()
        if normalized_subtitle_mode not in {"soft", "burned", "both"}:
            normalized_subtitle_mode = "soft"

        video_source = self._resolve_dubbing_video_source()
        if not video_source or not os.path.exists(video_source):
            self._mark_dubbing_step("sync", "failed", "No valid video source for synchronization.")
            self._set_session_activity(
                "Video rendering failed",
                "A valid video source is required before synchronization can begin.",
                "error",
            )
            self.show_error.emit("Dubbing Error", "No valid video file found for synchronization.")
            return

        sync_session_dir = dubbing_session_dir
        if not self._session_dir_has_sentence_wavs(sync_session_dir):
            if self._session_dir_has_sentence_wavs(session_output_dir):
                sync_session_dir = session_output_dir
                self.log_message.emit(
                    "Using root session audio artifacts for synchronization (legacy session layout detected)."
                )

        if not self._session_dir_has_sentence_wavs(sync_session_dir):
            self._mark_dubbing_step("tts_generation", "failed", "No generated sentence WAVs were found.")
            self._set_session_activity(
                "Video rendering unavailable",
                "Generate dubbing audio first so sentence WAV files are available for synchronization.",
                "error",
            )
            self.show_error.emit(
                "Dubbing Error",
                "No generated sentence WAVs found for synchronization. Run 'Generate Dub Audio' first.",
            )
            return

        speech_blocks_file = self._discover_latest_file_with_suffix(sync_session_dir, "_speech_blocks.json")
        if not speech_blocks_file:
            srt_for_speech_blocks = self._find_latest_srt(sync_session_dir, must_not_be_equalized=True)
            if not srt_for_speech_blocks:
                self._set_session_activity(
                    "Video rendering unavailable",
                    "A subtitle file is required before speech blocks can be rebuilt for synchronization.",
                    "error",
                )
                self._mark_dubbing_step("speech_blocks", "failed", "Could not find SRT for speech-block regeneration.")
                self.show_error.emit(
                    "Dubbing Error",
                    "No SRT file found to regenerate speech blocks. Run transcription first.",
                )
                return

            speech_blocks_snapshot = self._snapshot_speech_blocks_files(sync_session_dir)
            self._set_session_activity(
                "Rebuilding speech blocks",
                "No speech-block JSON was found, so the app is regenerating it before synchronization.",
                "active",
            )
            self.log_message.emit("No speech blocks JSON found. Regenerating speech blocks...")
            self._mark_dubbing_step("speech_blocks", "running")
            if not subdub_handler.generate_speech_blocks(sync_session_dir, srt_for_speech_blocks):
                self._mark_dubbing_step("speech_blocks", "failed", "Speech-block regeneration failed.")
                self._set_session_activity(
                    "Speech-block regeneration failed",
                    "The app could not rebuild speech blocks for synchronization.",
                    "error",
                )
                self.show_error.emit(
                    "Dubbing Error",
                    "Speech block regeneration failed. Check logs for details.",
                )
                return

            speech_blocks_file = self._wait_for_speech_blocks_file(
                session_dir=sync_session_dir,
                source_srt_file=srt_for_speech_blocks,
                previous_snapshot=speech_blocks_snapshot,
            )
            if not speech_blocks_file:
                self._mark_dubbing_step("speech_blocks", "failed", "No speech-block JSON was detected after regeneration.")
                self._set_session_activity(
                    "Speech-block regeneration failed",
                    "No rebuilt speech-block JSON file was detected after regeneration.",
                    "error",
                )
                self.show_error.emit(
                    "Dubbing Error",
                    "Speech blocks file was not detected after regeneration.",
                )
                return
            self._register_dubbing_artifact("speech_blocks", speech_blocks_file, is_current=True)
            self._mark_dubbing_step("speech_blocks", "completed")
        else:
            self._register_dubbing_artifact("speech_blocks", speech_blocks_file, is_current=True)

        # 1. Synchronize Audio
        self._set_session_activity(
            "Synchronizing audio",
            "Aligning generated sentence audio to the source video timing.",
            "active",
        )
        self.log_message.emit("Synchronizing audio...")
        self._mark_dubbing_step("sync", "running")
        if not subdub_handler.synchronize_audio(sync_session_dir, video_file=video_source):
            self._mark_dubbing_step("sync", "failed", "Subdub synchronization failed.")
            self._set_session_activity(
                "Audio synchronization failed",
                "Subdub could not align the generated audio to the source video.",
                "error",
            )
            self.show_error.emit("Dubbing Error", "Audio synchronization failed.")
            return
        
        # Find synchronized output video.
        # Legacy Subdub used a `_synced` suffix, while the refactored pipeline writes `final_output*.mp4`.
        synced_video_path = None
        candidate_videos = []
        for file in os.listdir(sync_session_dir):
            file_lower = file.lower()
            if not file_lower.endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
                continue
            if "_synced" in file_lower or file_lower.startswith("final_output"):
                candidate_videos.append(os.path.join(sync_session_dir, file))

        if candidate_videos:
            synced_video_path = max(candidate_videos, key=os.path.getmtime)

        if not synced_video_path:
             self._mark_dubbing_step("sync", "failed", "No synced video artifact was created.")
             self._set_session_activity(
                 "Audio synchronization failed",
                 "No synchronized video artifact was created after synchronization finished.",
                 "error",
             )
             self.show_error.emit("Dubbing Error", "Synced video file not found after synchronization.")
             return
        self.log_message.emit(f"Found synced video: {synced_video_path}")
        self._register_dubbing_artifact("synced_video", synced_video_path, is_current=True)
        self._mark_dubbing_step("sync", "completed")

        video_for_subtitles_path = synced_video_path
        if dubbed_audio_only:
            self.log_message.emit("Preparing dubbed-only audio output (without original mix)...")
            dubbed_audio_path = subdub_handler.find_latest_dubbed_audio_track(sync_session_dir)
            if not dubbed_audio_path:
                self._mark_dubbing_step("sync", "failed", "No dubbed-audio track found after synchronization.")
                self._set_session_activity(
                    "Dubbed-only export failed",
                    "No dubbed-audio track was found after synchronization.",
                    "error",
                )
                self.show_error.emit(
                    "Dubbing Error",
                    "Could not find a generated dubbing audio track after synchronization.",
                )
                return

            dubbed_only_video_path = os.path.join(sync_session_dir, "final_output_dubbed_only.mp4")
            if not subdub_handler.replace_video_audio_track(
                synced_video_path,
                dubbed_audio_path,
                dubbed_only_video_path,
            ):
                self._mark_dubbing_step("sync", "failed", "Could not create dubbed-only intermediate video.")
                self._set_session_activity(
                    "Dubbed-only export failed",
                    "The app could not create the dubbed-only intermediate video.",
                    "error",
                )
                self.show_error.emit(
                    "Dubbing Error",
                    "Failed to create a dubbed-only video track before subtitle rendering.",
                )
                return

            self.log_message.emit(f"Using dubbed-only audio track: {dubbed_audio_path}")
            video_for_subtitles_path = dubbed_only_video_path
            self._register_dubbing_artifact("dubbed_only_video", dubbed_only_video_path, is_current=True)

        # 2. Equalize Subtitles
        srt_file = self._find_latest_srt(sync_session_dir, must_not_be_equalized=True)
        if not srt_file:
            self._mark_dubbing_step("equalize", "failed", "No SRT found for equalization.")
            self._set_session_activity(
                "Subtitle equalization failed",
                "A subtitle file is required before equalization can begin.",
                "error",
            )
            self.show_error.emit("Dubbing Error", "Cannot find SRT file to equalize.")
            return
        self._set_session_activity(
            "Equalizing subtitles",
            "Balancing subtitle timing and line breaks before final video rendering.",
            "active",
        )
        self.log_message.emit(f"Equalizing subtitles for {srt_file}...")
        self._mark_dubbing_step("equalize", "running")
        if not subdub_handler.equalize_subtitles(srt_file):
            self._mark_dubbing_step("equalize", "failed", "Subdub equalization failed.")
            self._set_session_activity(
                "Subtitle equalization failed",
                "Subdub could not equalize the subtitle file.",
                "error",
            )
            self.show_error.emit("Dubbing Error", "Subtitle equalization failed.")
            return
            
        equalized_srt_path = srt_file.replace('.srt', '_equalized.srt')
        if not os.path.exists(equalized_srt_path):
            base_name = os.path.splitext(os.path.basename(srt_file))[0]
            expected_path = os.path.join(sync_session_dir, f"{base_name}_equalized.srt")
            if os.path.exists(expected_path):
                equalized_srt_path = expected_path
            else:
                 self._mark_dubbing_step("equalize", "failed", "Equalized SRT artifact was not found.")
                 self._set_session_activity(
                     "Subtitle equalization failed",
                     "The equalized subtitle file was not found after equalization completed.",
                     "error",
                 )
                 self.show_error.emit("Dubbing Error", "Equalized SRT file not found after equalization.")
                 return
        self.log_message.emit(f"Found equalized SRT: {equalized_srt_path}")
        self._register_dubbing_artifact("equalized_srt", equalized_srt_path, is_current=True)
        self._mark_dubbing_step("equalize", "completed")

        # 3. Add Subtitles to Video
        os.makedirs(session_output_dir, exist_ok=True)
        active_run = self._synchronize_active_dubbing_run()
        run_id_suffix = ""
        if active_run:
            run_id_value = str(active_run.get("run_id") or "").strip()
            if run_id_value:
                run_id_suffix = f"_{run_id_value}"
        mode_to_filename = {
            "soft": f"{self.state.session_name}_final_softsubs{run_id_suffix}.mp4",
            "burned": f"{self.state.session_name}_final_burnedsubs{run_id_suffix}.mp4",
        }
        mode_to_label = {
            "soft": "embedded as a selectable subtitle track",
            "burned": "burned into the video",
        }
        render_modes = ["soft", "burned"] if normalized_subtitle_mode == "both" else [normalized_subtitle_mode]
        if normalized_subtitle_mode == "both":
            self.log_message.emit("Rendering both subtitle modes (soft + burned-in)...")

        generated_outputs: list[str] = []
        self._set_session_activity(
            "Rendering final video",
            "Combining the synchronized audio and equalized subtitles into the final video output.",
            "active",
        )
        self._mark_dubbing_step("render", "running")
        for render_mode in render_modes:
            subtitle_mode_label = mode_to_label[render_mode]
            output_video_path = os.path.join(session_output_dir, mode_to_filename[render_mode])
            self.log_message.emit(
                f"Adding subtitles to video ({subtitle_mode_label}), output will be at {output_video_path}"
            )
            if not subdub_handler.add_subtitles_to_video(
                video_for_subtitles_path,
                equalized_srt_path,
                output_video_path,
                subtitle_mode=render_mode,
            ):
                self._mark_dubbing_step("render", "failed", f"Could not render {render_mode} subtitles.")
                self._set_session_activity(
                    "Final video rendering failed",
                    f"The app could not render the {render_mode} subtitle variant.",
                    "error",
                )
                self.show_error.emit(
                    "Dubbing Error",
                    f"Failed to add {render_mode} subtitles to the final video.",
                )
                return

            generated_outputs.append(output_video_path)
            artifact_role = "final_video_soft" if render_mode == "soft" else "final_video_burned"
            self._register_dubbing_artifact(artifact_role, output_video_path, is_current=True)

        if len(generated_outputs) == 1:
            self._set_session_activity(
                "Dubbing complete",
                "The final dubbed video is ready.",
                "success",
            )
            self.log_message.emit(f"Dubbing workflow finished. Final output: {generated_outputs[0]}")
            self._mark_dubbing_step("render", "completed")
            self.dubbing_video_saved.emit(generated_outputs)
            return

        self._set_session_activity(
            "Dubbing complete",
            f"The final dubbed video set is ready ({len(generated_outputs)} files).",
            "success",
        )
        self.log_message.emit(
            "Dubbing workflow finished. Final outputs: "
            + "; ".join(generated_outputs)
        )
        self._mark_dubbing_step("render", "completed")
        self.dubbing_video_saved.emit(generated_outputs)

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
            "remove_quotation_marks": self.state.text_processing.remove_quotation_marks,
            "tts_service": self.state.tts.service,
            "remove_footnotes": self.state.text_processing.remove_footnotes,
            "filter_citations": self.state.text_processing.filter_citations
        }
        
        self._set_session_activity(
            "Processing text",
            "Preparing the source text into sentence-sized generation units.",
            "active",
        )
        self._set_text_preprocessing_running(True)
        try:
            processed_sentences = text_preprocessor.preprocess_text(self.state.raw_text, settings)
            self._set_processed_sentences_snapshot(processed_sentences)
            self._set_session_activity(
                "Text processing complete",
                f"Prepared {len(processed_sentences)} sentence(s) for generation.",
                "success",
            )
            self.log_message.emit("Text preprocessing complete.")
            self.state.metadata["preprocessing_settings"] = json.dumps(settings, sort_keys=True)
            if self.state.session_name and self.state.session_name != "Untitled Session":
                try:
                    session_handler.save_metadata(self.state.session_name, self.state.metadata)
                except Exception as e:
                    logging.warning(f"Could not save preprocessing settings metadata: {e}")
            self.state_changed.emit()
        except Exception as e:
            logging.error(f"Text preprocessing failed: {e}", exc_info=True)
            self._set_session_activity(
                "Text processing failed",
                str(e),
                "error",
            )
            self.show_error.emit("Preprocessing Error", str(e))
        finally:
            self._set_text_preprocessing_running(False)

    # --- Generation ---

    def _reset_generation_progress(self) -> bool | None:
        """Resets generated sentence flags and removes existing sentence WAV artifacts."""
        processed_sentences = self.get_processed_sentences_snapshot()
        if not processed_sentences:
            return False

        had_generated_sentences = any(
            sentence.get("tts_generated") == "yes"
            for sentence in processed_sentences
        )
        if not had_generated_sentences:
            return False

        for wavs_dir in self._get_candidate_sentence_wavs_dirs():
            if not os.path.isdir(wavs_dir):
                continue
            try:
                shutil.rmtree(wavs_dir)
                os.makedirs(wavs_dir, exist_ok=True)
            except OSError as e:
                self.show_error.emit(
                    "Generation Error",
                    f"Could not clear WAV directory '{wavs_dir}': {e}",
                )
                return None

        self._clear_audio_variants()

        if self._have_preprocessing_settings_changed():
            self._set_processed_sentences_snapshot([])
            self.progress_updated.emit(0, 1, 0.0)
            self.state_changed.emit()
            return True

        reset_sentences: list[dict] = []
        for sentence in processed_sentences:
            reset_sentence = copy.deepcopy(sentence)
            reset_sentence["tts_generated"] = "no"
            reset_sentence.pop("processed_sentence", None)
            reset_sentences.append(reset_sentence)

        self._set_processed_sentences_snapshot(reset_sentences)
        self.progress_updated.emit(0, max(len(reset_sentences), 1), 0.0)
        self.state_changed.emit()
        return True

    def start_generation_anew(self):
        """Clears current progress and restarts generation from the beginning."""
        if threading.current_thread() is not threading.main_thread():
            self._start_generation_anew_requested.emit()
            return

        if (
            self._is_generation_running()
            or self._is_regeneration_running()
            or self._is_rvc_processing_running()
        ):
            self.start_generation()
            return

        reset_result = self._reset_generation_progress()
        if reset_result is None:
            return

        if reset_result:
            self.log_message.emit(
                "Generation progress cleared. Restarting from sentence 1..."
            )

        self.start_generation()

    def start_generation(self):
        """Starts the audio generation worker thread, running preprocessing if needed."""
        if threading.current_thread() is not threading.main_thread():
            self._start_generation_requested.emit()
            return

        if self._is_generation_running():
            self._notify_user("Generation is already running.", level="warning")
            self._clear_pending_dubbing_add_to_video_request()
            return

        if self._is_regeneration_running():
            self._notify_user(
                "Wait for sentence regeneration to finish before starting full generation.",
                level="warning",
            )
            self._clear_pending_dubbing_add_to_video_request()
            return

        if self._is_rvc_processing_running():
            self._notify_user(
                "Wait for RVC sentence processing to finish before starting full generation.",
                level="warning",
            )
            self._clear_pending_dubbing_add_to_video_request()
            return
        
        has_processed_sentences = bool(self.get_processed_sentences_snapshot())
        if not self.state.raw_text and not has_processed_sentences:
            self.show_error.emit("Error", "No text to process. Please select a source file first.")
            self._clear_pending_dubbing_add_to_video_request()
            return

        self.stop_playback()
        self._persist_session_config(force=True)

        self._set_session_activity(
            "Preparing generation",
            "Starting the audio generation workflow in the background.",
            "active",
        )
        self.log_message.emit("Starting audio generation process...")
        self.stop_generation_flag.clear()
        self.cancel_generation_flag.clear()
        self.state_changed.emit()
        
        # The worker thread will handle finding the start index and preprocessing.
        self.generation_thread = threading.Thread(target=self._generation_thread_worker, daemon=True)
        self.generation_thread.start()
        self.state_changed.emit()

    def stop_generation(self):
        """Stops the audio generation worker thread after the current sentence."""
        if not self._is_generation_running():
            self._notify_user("No generation is currently running.", level="warning")
            return

        if self.cancel_generation_flag.is_set():
            self._notify_user(
                "Cancellation already requested. Waiting for the current sentence to finish.",
                level="warning",
            )
            return

        if self.stop_generation_flag.is_set():
            self._notify_user(
                "Stop already requested. Waiting for the current sentence to finish.",
                level="warning",
            )
            return

        self._set_session_activity(
            "Stopping generation",
            "The current sentence will finish before generation stops.",
            "warning",
        )
        self.log_message.emit("Stopping generation after current sentence...")
        self.stop_generation_flag.set()
        self.state_changed.emit()

    def cancel_generation(self):
        """Cancels generation and cleans up generated files."""
        if self._is_regeneration_running():
            self.cancel_regeneration()
            return

        if not self._is_generation_running():
            self._notify_user("No generation is currently running to cancel.", level="warning")
            return

        if self.cancel_generation_flag.is_set():
            self._notify_user("Cancellation already requested. Waiting for cleanup.", level="warning")
            return

        self._set_session_activity(
            "Cancelling generation",
            "The app will clean up generated sentence audio after the current sentence finishes.",
            "warning",
        )
        self.log_message.emit("Cancelling generation after current sentence and scheduling cleanup...")
        self.cancel_generation_flag.set()
        self.stop_generation_flag.set()
        self.state_changed.emit()

    def cancel_regeneration(self):
        """Cancels sentence regeneration after the current sentence finishes."""
        if not self._is_regeneration_running():
            self._notify_user("No sentence regeneration is currently running to cancel.", level="warning")
            return

        if self.cancel_regeneration_flag.is_set():
            self._notify_user(
                "Regeneration cancellation already requested. Waiting for the current sentence to finish.",
                level="warning",
            )
            return

        self._set_session_activity(
            "Cancelling regeneration",
            "The current sentence will finish before the remaining regeneration queue stops.",
            "warning",
        )
        self.log_message.emit("Cancelling regeneration after current sentence...")
        self.cancel_regeneration_flag.set()
        self.state_changed.emit()

    def _cleanup_cancelled_generation(self):
        """Removes sentence JSON file and all generated WAVs."""
        if self._is_generation_running() and threading.current_thread() is not self.generation_thread:
            self.log_message.emit("Cleanup deferred until generation reaches a safe stop point.")
            return

        self._set_session_activity(
            "Cleaning up cancelled generation",
            "Removing sentence JSON and generated WAV files from the cancelled run.",
            "warning",
        )
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

        self._clear_audio_variants()
        self._set_processed_sentences_snapshot([], persist=False)
        self.state_changed.emit()
        self.progress_updated.emit(0, 1, 0.0)
        self._set_session_activity(
            "Generation cancelled",
            "Cleanup is complete. You can start again whenever you're ready.",
            "warning",
        )
        self.log_message.emit("Cleanup complete.")

    def _generation_thread_worker(self):
        """The main worker loop for generating audio for all sentences."""
        cleanup_requested = False
        post_generation_add_to_video_request: dict[str, object] | None = None
        worker_thread = threading.current_thread()
        dubbing_workflow_active = self._is_dubbing_source_selected()

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
                    if dubbing_workflow_active:
                        self._mark_dubbing_step("tts_generation", "failed", "Cancelled during preprocessing.")
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
                self._set_session_activity(
                    "Audio generation already complete",
                    f"All {total_sentences} sentence(s) already have generated audio.",
                    "success",
                )
                self.log_message.emit("All sentences have already been generated.")
                ext = os.path.splitext(self.state.source_file_path)[1].lower() if self.state.source_file_path else ""
                if self._is_dubbing_source_extension(ext):
                    post_generation_add_to_video_request = self._consume_pending_dubbing_add_to_video_request()
                    self._mark_dubbing_step("tts_generation", "completed", "All sentences were already generated.")
                    run_dir = self._get_dubbing_work_dir(ensure_exists=False)
                    if run_dir:
                        self._register_dubbing_artifact("sentence_wavs_dir", os.path.join(run_dir, "Sentence_wavs"))
                else:
                    self._clear_pending_dubbing_add_to_video_request()
                return

            self._set_session_activity(
                "Generating audio",
                f"Starting from sentence {start_index + 1} of {total_sentences}.",
                "active",
            )
            self.log_message.emit(f"Starting generation from sentence {start_index + 1}...")
            sentence_times = []
            start_time = time.time()

            def llm_worker(index: int, sentence_dict: dict) -> tuple[int, bool, dict]:
                """Runs only the LLM processing for a single sentence."""
                try:
                    updated_sentence = copy.deepcopy(sentence_dict)
                    text_source_key = "processed_sentence" if updated_sentence.get("processed_sentence") else "original_sentence"
                    text_to_process = str(updated_sentence.get(text_source_key) or "")
                    is_dubbing_sentence = self._is_dubbing_source_selected()
                    if is_dubbing_sentence:
                        text_to_process = self._normalize_subtitle_sentence_text(text_to_process)
                        updated_sentence[text_source_key] = text_to_process

                    if not text_to_process.strip():
                        return index, True, updated_sentence

                    processed_text, _ = self._run_llm_processing(text_to_process)
                    if is_dubbing_sentence:
                        processed_text = self._normalize_subtitle_sentence_text(processed_text)
                    updated_sentence['processed_sentence'] = processed_text
                    return index, True, updated_sentence
                except Exception as e:
                    logging.error(f"Failed LLM processing for sentence {sentence_dict.get('sentence_number')}: {e}", exc_info=True)
                    return index, False, sentence_dict

            use_parallel_llm = self.state.llm.processing_enabled and self.state.llm.concurrent_calls > 1
            llm_executor = None
            llm_futures = {}

            if use_parallel_llm:
                import concurrent.futures
                llm_executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.state.llm.concurrent_calls)
                for i in range(start_index, total_sentences):
                    sentence_dict = processed_sentences[i]
                    if sentence_dict.get("tts_generated") == "yes":
                        continue
                    future = llm_executor.submit(llm_worker, i, sentence_dict)
                    llm_futures[i] = future

            try:
                for i in range(start_index, total_sentences):
                    if self.stop_generation_flag.is_set():
                        if self.cancel_generation_flag.is_set():
                            cleanup_requested = True
                            if dubbing_workflow_active:
                                self._mark_dubbing_step("tts_generation", "failed", "Cancelled by user.")
                            self._set_session_activity(
                                "Cancelling generation",
                                "Cancellation was acknowledged. Cleaning up generated artifacts next.",
                                "warning",
                            )
                            self.log_message.emit("Cancellation acknowledged. Cleaning up generated artifacts...")
                        else:
                            if dubbing_workflow_active:
                                self._mark_dubbing_step("tts_generation", "failed", "Stopped by user.")
                            self._set_session_activity(
                                "Generation stopped",
                                "The run stopped after the current sentence as requested.",
                                "warning",
                            )
                            self.log_message.emit("Generation stopped by user.")
                        return

                    sentence_dict = processed_sentences[i]
                    if sentence_dict.get("tts_generated") == "yes":
                        continue

                    skip_llm = False
                    if use_parallel_llm:
                        future = llm_futures.get(i)
                        if future:
                            _, llm_success, updated_sentence_from_llm = future.result()
                            if llm_success:
                                sentence_dict = updated_sentence_from_llm
                                skip_llm = True

                    sentence_start_time = time.time()
                    self._set_session_activity(
                        "Generating audio",
                        f"Rendering sentence {i + 1} of {total_sentences}.",
                        "active",
                    )
                    self.log_message.emit(f"Generating sentence {i+1}/{total_sentences}...")

                    success, updated_sentence = self._execute_generation_for_sentence(sentence_dict, skip_llm=skip_llm)
                    if not success or updated_sentence is None:
                        if self.cancel_generation_flag.is_set():
                            cleanup_requested = True
                            if dubbing_workflow_active:
                                self._mark_dubbing_step("tts_generation", "failed", "Cancelled during sentence generation.")
                            self._set_session_activity(
                                "Cancelling generation",
                                f"Generation was cancelled while working on sentence {i + 1}. Cleanup will run next.",
                                "warning",
                            )
                            self.log_message.emit("Generation cancelled during sentence processing. Cleaning up...")
                        else:
                            if dubbing_workflow_active:
                                self._mark_dubbing_step("tts_generation", "failed", f"Sentence {i+1} failed.")
                            self._set_session_activity(
                                "Generation failed",
                                f"Sentence {i + 1} could not be generated.",
                                "error",
                            )
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
            finally:
                if llm_executor:
                    llm_executor.shutdown(wait=False, cancel_futures=True)

            if self.cancel_generation_flag.is_set():
                cleanup_requested = True
                if dubbing_workflow_active:
                    self._mark_dubbing_step("tts_generation", "failed", "Cancelled during finalization.")
                self._set_session_activity(
                    "Cancelling generation",
                    "Cancellation was requested during finalization. Cleanup will run next.",
                    "warning",
                )
                self.log_message.emit("Cancellation requested during finalization. Cleaning up...")
                return

            ext = os.path.splitext(self.state.source_file_path)[1].lower() if self.state.source_file_path else ""
            is_dubbing_workflow = self._is_dubbing_source_extension(ext)

            if is_dubbing_workflow:
                post_generation_add_to_video_request = self._consume_pending_dubbing_add_to_video_request()
                self._mark_dubbing_step("tts_generation", "completed")
                run_dir = self._get_dubbing_work_dir(ensure_exists=False)
                if run_dir:
                    self._register_dubbing_artifact("sentence_wavs_dir", os.path.join(run_dir, "Sentence_wavs"))
                if post_generation_add_to_video_request:
                    self._set_session_activity(
                        "Audio generation complete",
                        "Dubbing audio is ready. The app is starting final video rendering automatically.",
                        "success",
                    )
                else:
                    self._set_session_activity(
                        "Audio generation complete",
                        "Dubbing sentence audio is ready for review, regeneration, or final rendering.",
                        "success",
                    )
            else:
                self._clear_pending_dubbing_add_to_video_request()
                self._set_session_activity(
                    "Saving final output",
                    "Joining generated sentence audio into the final output file.",
                    "active",
                )
                self.log_message.emit("Saving final output file...")
                output_format = self.state.audio_processing.output_format
                output_path = os.path.join(
                    session_handler.get_session_path(self.state.session_name),
                    f"{self.state.session_name}.{output_format}",
                )
                self.save_output(output_path)
        except Exception as e:
            logging.error("Unhandled generation worker error: %s", e, exc_info=True)
            self._set_session_activity(
                "Generation failed",
                f"Unexpected generation failure: {e}",
                "error",
            )
            self.show_error.emit("Generation Error", f"Unexpected generation failure: {e}")
        finally:
            if cleanup_requested:
                self._cleanup_cancelled_generation()

            self.stop_generation_flag.clear()
            self.cancel_generation_flag.clear()

            if self.generation_thread is worker_thread:
                self.generation_thread = None

            self.state_changed.emit()

            if post_generation_add_to_video_request:
                subtitle_mode = str(
                    post_generation_add_to_video_request.get("subtitle_mode")
                    or "soft"
                )
                dubbed_audio_only = bool(
                    post_generation_add_to_video_request.get("dubbed_audio_only")
                )
                self._set_session_activity(
                    "Rendering final video",
                    "Auto-continue is enabled, so video rendering is starting immediately after audio generation.",
                    "active",
                )
                self.log_message.emit(
                    "Starting automatic video rendering for the generated dubbing audio..."
                )
                self._run_threaded_task(
                    self._run_dubbing_task_thread,
                    "add_to_video",
                    subtitle_mode,
                    dubbed_audio_only,
                    False,
                )
            else:
                self._clear_pending_dubbing_add_to_video_request()

    # --- Metadata ---
    def save_metadata(self, metadata: dict):
        """Updates metadata in the state and saves it to disk."""
        self.state.metadata = metadata
        try:
            session_handler.save_metadata(self.state.session_name, self.state.metadata)
            self._notify_user("Metadata saved.")
        except Exception as e:
            self.show_error.emit("Metadata Error", f"Could not save metadata: {e}")

    # --- TTS ---
    @staticmethod
    def _normalize_kokoro_default_voices(raw_defaults) -> dict[str, str]:
        if not isinstance(raw_defaults, dict):
            return {}

        normalized_defaults: dict[str, str] = {}
        for language_value, voice_value in raw_defaults.items():
            language_code = tts_handler.normalize_kokoro_language_code(language_value)
            voice_name = str(voice_value or "").strip()
            if language_code and voice_name:
                normalized_defaults[language_code] = voice_name
        return normalized_defaults

    @staticmethod
    def _match_catalog_voice(voice_name: str, speaker_catalog: list[str] | None) -> str:
        normalized_voice = str(voice_name or "").strip()
        if not normalized_voice:
            return ""
        if not speaker_catalog:
            return normalized_voice

        for catalog_voice in speaker_catalog:
            if str(catalog_voice or "").strip().lower() == normalized_voice.lower():
                return str(catalog_voice or "").strip()
        return ""

    @classmethod
    def _preferred_kokoro_voice_for_language(
        cls,
        language_code: str,
        speaker_catalog: list[str] | None,
        current_speaker: str = "",
        default_voices: dict | None = None,
    ) -> str:
        normalized_language = tts_handler.normalize_kokoro_language_code(language_code)
        if not normalized_language:
            return ""

        normalized_defaults = cls._normalize_kokoro_default_voices(default_voices)
        default_voice = normalized_defaults.get(normalized_language, "")
        matched_default = cls._match_catalog_voice(default_voice, speaker_catalog)
        if matched_default:
            return matched_default

        current_voice = str(current_speaker or "").strip()
        if (
            current_voice
            and tts_handler.infer_kokoro_voice_language_code(current_voice) == normalized_language
        ):
            matched_current = cls._match_catalog_voice(current_voice, speaker_catalog)
            if matched_current:
                return matched_current

        for speaker in speaker_catalog or []:
            normalized_speaker = str(speaker or "").strip()
            if (
                normalized_speaker
                and tts_handler.infer_kokoro_voice_language_code(normalized_speaker) == normalized_language
            ):
                return normalized_speaker

        return default_voice if default_voice and not speaker_catalog else ""

    def _sync_kokoro_language_from_voice(self, speaker_name: str) -> bool:
        if self.state.tts.service != "Kokoro":
            return False

        language_code = tts_handler.infer_kokoro_voice_language_code(speaker_name)
        if not language_code or self.state.tts.language == language_code:
            return False

        self.state.tts.language = language_code
        return True

    def save_kokoro_default_voice(
        self,
        speaker_name: str,
        language_code: str = "",
    ) -> tuple[bool, str]:
        if self.state.tts.service != "Kokoro":
            return False, "Kokoro defaults can only be saved while Kokoro is the active TTS service."

        normalized_speaker = str(speaker_name or "").strip()
        if not normalized_speaker:
            return False, "Select a Kokoro voice first."

        inferred_language = tts_handler.infer_kokoro_voice_language_code(normalized_speaker)
        normalized_language = inferred_language or tts_handler.normalize_kokoro_language_code(language_code)
        if not normalized_language:
            return False, "Could not identify the language for this Kokoro voice."

        defaults = self._normalize_kokoro_default_voices(self.state.tts.kokoro_default_voices)
        defaults[normalized_language] = normalized_speaker
        self.state.tts.kokoro_default_voices = defaults
        self.state.tts.speaker = normalized_speaker
        self.state.tts.language = normalized_language

        self._persist_global_settings(force=True)
        if self._is_named_session_active():
            self._persist_session_config(force=True)

        message = f"Saved {normalized_speaker} as the Kokoro default for {normalized_language}."
        self._notify_user(message)
        self.state_changed.emit()
        return True, message

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
            self._notify_user("TTS connection is already in progress.", level="warning")
            return

        tts_snapshot = copy.deepcopy(self.state.tts.__dict__)
        requested_service = tts_snapshot.get("service") or self.state.tts.service

        self._set_session_activity(
            f"Connecting to {requested_service}",
            "Checking the selected TTS service and refreshing its available models and voices.",
            "active",
        )
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

            elif service == "VoxCPM":
                base_url = (
                    tts_snapshot.get("external_server_url")
                    if tts_snapshot.get("use_external_server")
                    else tts_handler.VOXCPM_API_BASE_URL
                )

                if not tts_handler.check_voxcpm_connection(base_url):
                    result["error_title"] = "Connection Error"
                    result["error_message"] = (
                        f"Could not connect to VoxCPM server at {base_url}. Is it running?"
                    )
                    self._tts_connection_result.emit(result)
                    return

                models = tts_handler.get_voxcpm_models(base_url)
                speakers = tts_handler.get_voxcpm_voices(base_url)

                default_model = tts_handler.VOXCPM_DEFAULT_MODEL
                default_voice = tts_handler.VOXCPM_DEFAULT_VOICE

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
                    f"Connected to VoxCPM server ({len(models)} model(s), {len(speakers)} voice(s))."
                )

            elif service == "FishS2":
                base_url = (
                    tts_snapshot.get("external_server_url")
                    if tts_snapshot.get("use_external_server")
                    else tts_handler.FISHS2_API_BASE_URL
                )

                if not tts_handler.check_fishs2_connection(base_url):
                    result["error_title"] = "Connection Error"
                    result["error_message"] = (
                        f"Could not connect to FishS2 server at {base_url}. Is it running?"
                    )
                    self._tts_connection_result.emit(result)
                    return

                models = tts_handler.get_fishs2_models(base_url)
                speakers = tts_handler.get_fishs2_voices(base_url)

                default_model = tts_handler.FISHS2_DEFAULT_MODEL
                default_voice = tts_handler.FISHS2_DEFAULT_VOICE

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
                    f"Connected to FishS2 server ({len(models)} model(s), {len(speakers)} voice(s))."
                )

            elif service == "Chatterbox":
                base_url = (
                    tts_snapshot.get("external_server_url")
                    if tts_snapshot.get("use_external_server")
                    else tts_handler.CHATTERBOX_API_BASE_URL
                )

                if not tts_handler.check_chatterbox_connection(base_url):
                    result["error_title"] = "Connection Error"
                    result["error_message"] = (
                        f"Could not connect to Chatterbox server at {base_url}. Is it running?"
                    )
                    self._tts_connection_result.emit(result)
                    return

                models = tts_handler.get_chatterbox_models(base_url)
                speakers = tts_handler.get_chatterbox_voices(base_url)

                default_model = tts_handler.CHATTERBOX_DEFAULT_MODEL
                
                selected_model = (tts_snapshot.get("xtts_model") or "").strip()
                if models:
                    if selected_model not in models:
                        selected_model = default_model if default_model in models else models[0]
                elif not selected_model:
                    selected_model = default_model

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
                    f"Connected to Chatterbox server ({len(models)} model(s), {len(speakers)} voice(s))."
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

            elif service == "Kokoro":
                base_url = (
                    tts_snapshot.get("external_server_url")
                    if tts_snapshot.get("use_external_server")
                    else tts_handler.KOKORO_API_BASE_URL
                )

                if not tts_handler.check_kokoro_connection(base_url):
                    result["error_title"] = "Connection Error"
                    result["error_message"] = (
                        f"Could not connect to Kokoro server at {base_url}. Is it running?"
                    )
                    self._tts_connection_result.emit(result)
                    return

                models = tts_handler.get_kokoro_models(base_url)
                speakers = tts_handler.get_kokoro_voices(base_url)

                default_model = tts_handler.KOKORO_DEFAULT_MODEL
                default_voice = (
                    self._preferred_kokoro_voice_for_language(
                        str(tts_snapshot.get("language") or ""),
                        speakers,
                        current_speaker=str(tts_snapshot.get("speaker") or ""),
                        default_voices=tts_snapshot.get("kokoro_default_voices"),
                    )
                    or tts_handler.KOKORO_DEFAULT_VOICE
                )

                selected_model = (tts_snapshot.get("xtts_model") or "").strip()
                if models:
                    if selected_model not in models:
                        selected_model = default_model if default_model in models else models[0]
                elif not selected_model:
                    selected_model = default_model

                selected_speaker = (tts_snapshot.get("speaker") or "").strip()
                if speakers:
                    matched_selected_speaker = self._match_catalog_voice(selected_speaker, speakers)
                    if matched_selected_speaker:
                        selected_speaker = matched_selected_speaker
                    else:
                        selected_speaker = default_voice if default_voice in speakers else speakers[0]
                elif not selected_speaker:
                    selected_speaker = default_voice

                selected_language = (
                    tts_handler.infer_kokoro_voice_language_code(selected_speaker)
                    or tts_handler.normalize_kokoro_language_code(tts_snapshot.get("language"))
                )

                result["updates"] = {
                    "tts_models": models,
                    "tts_speakers": speakers,
                    "xtts_model": selected_model,
                    "speaker": selected_speaker,
                }
                if selected_language:
                    result["updates"]["language"] = selected_language
                result["log_message"] = (
                    f"Connected to Kokoro server ({len(models)} model(s), {len(speakers)} voice(s))."
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
                self._set_session_activity(
                    f"Connected to {requested_service}",
                    log_message,
                    "success",
                )
                self._notify_user(log_message, timeout_ms=4000)

            error_message = result.get("error_message")
            if error_message:
                self._set_session_activity(
                    f"Connection failed for {requested_service}",
                    error_message,
                    "error",
                )
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
        elif self.state.tts.service == "Kokoro":
            preferred_voice = self._preferred_kokoro_voice_for_language(
                language_name,
                self.state.tts.tts_speakers,
                current_speaker=self.state.tts.speaker,
                default_voices=self.state.tts.kokoro_default_voices,
            )
            if preferred_voice:
                self.state.tts.speaker = preferred_voice
            self.state_changed.emit()
        else:
            self.state_changed.emit()

    @staticmethod
    def _dedupe_ordered_text(values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized_value = str(value or "").strip()
            if not normalized_value:
                continue
            dedupe_key = normalized_value.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(normalized_value)
        return deduped

    def tts_service_supports_prebuilt_voices(
        self,
        service: str | None = None,
        provider_id: str | None = None,
    ) -> bool:
        normalized_service = str(service or self.state.tts.service or "").strip()
        if normalized_service in {"Kokoro", "Silero", "Voxtral"}:
            return True
        if normalized_service in {tts_handler.OPENAI_SERVICE, tts_handler.GEMINI_SERVICE}:
            return True
        if normalized_service != tts_handler.OPENAI_COMPAT_SERVICE:
            return False

        normalized_provider_id = self._normalize_provider_id(
            provider_id or self.state.tts.openai_audio_endpoint
        )
        if normalized_provider_id in {tts_handler.OPENAI_PROVIDER, tts_handler.GEMINI_PROVIDER}:
            return True

        for provider in self.list_tts_provider_configs():
            provider_record_id = self._normalize_provider_id(provider.get("id"))
            if provider_record_id != normalized_provider_id:
                continue
            return bool(
                provider.get(
                    tts_handler.PREBUILT_VOICE_PROVIDER_FIELD,
                    bool(provider.get("voices", [])),
                )
            )

        return False

    def list_active_prebuilt_voices(
        self,
        *,
        use_remote: bool = False,
        update_state: bool = False,
    ) -> list[str]:
        service = str(self.state.tts.service or "").strip()
        if not self.tts_service_supports_prebuilt_voices(service):
            return []

        current_state_voices = self._dedupe_ordered_text(self.state.tts.tts_speakers)
        if current_state_voices and not use_remote:
            return current_state_voices

        tts_snapshot = copy.deepcopy(self.state.tts.__dict__)
        voices: list[str] = []

        if service == "Kokoro":
            base_url = (
                tts_snapshot.get("external_server_url")
                if tts_snapshot.get("use_external_server")
                else tts_handler.KOKORO_API_BASE_URL
            )
            voices = tts_handler.get_kokoro_voices(base_url)
        elif service == "Voxtral":
            base_url = (
                tts_snapshot.get("external_server_url")
                if tts_snapshot.get("use_external_server")
                else tts_handler.VOXTRAL_API_BASE_URL
            )
            voices = tts_handler.get_voxtral_voices(base_url)
        elif service == "Silero":
            voices = tts_handler.get_silero_speakers(tts_handler.SILERO_API_BASE_URL)
        elif service in {
            tts_handler.OPENAI_SERVICE,
            tts_handler.GEMINI_SERVICE,
            tts_handler.OPENAI_COMPAT_SERVICE,
        }:
            tts_snapshot["service"] = tts_handler.OPENAI_COMPAT_SERVICE
            if service == tts_handler.OPENAI_SERVICE:
                tts_snapshot["openai_audio_endpoint"] = tts_handler.OPENAI_PROVIDER
            elif service == tts_handler.GEMINI_SERVICE:
                tts_snapshot["openai_audio_endpoint"] = tts_handler.GEMINI_PROVIDER

            if use_remote:
                voices = tts_handler.get_openai_audio_voices(tts_snapshot)
            else:
                voices = tts_handler.get_openai_audio_voices_fallback(tts_snapshot)

        voices = self._dedupe_ordered_text(voices)
        if not voices:
            return current_state_voices

        if update_state:
            with self._state_lock:
                self.state.tts.tts_speakers = list(voices)
                if self.state.tts.speaker not in voices:
                    if service == "Kokoro":
                        preferred_voice = self._preferred_kokoro_voice_for_language(
                            self.state.tts.language,
                            voices,
                            current_speaker=self.state.tts.speaker,
                            default_voices=self.state.tts.kokoro_default_voices,
                        )
                        self.state.tts.speaker = preferred_voice or voices[0]
                    else:
                        self.state.tts.speaker = voices[0]
                if service == "Kokoro":
                    self._sync_kokoro_language_from_voice(self.state.tts.speaker)
            self.state_changed.emit()

        return voices

    def _get_voice_preview_output_dir(self, ensure_exists: bool = False) -> str:
        if self._is_named_session_active():
            output_dir = os.path.join(
                session_handler.get_session_path(self.state.session_name),
                "Voice_Previews",
            )
        else:
            output_dir = os.path.join(tempfile.gettempdir(), "pandrator_voice_previews")

        if ensure_exists:
            os.makedirs(output_dir, exist_ok=True)
        return output_dir

    @staticmethod
    def _sanitize_preview_token(raw_value: str, fallback: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9]+", "-", str(raw_value or "").strip()).strip("-").lower()
        return normalized or fallback

    def _build_voice_preview_path(
        self,
        *,
        service: str,
        provider_id: str,
        language_code: str,
        voice_name: str,
        ensure_dir: bool,
    ) -> str:
        output_dir = self._get_voice_preview_output_dir(ensure_exists=ensure_dir)
        sanitized_service = self._sanitize_preview_token(service, "tts")
        sanitized_provider = self._sanitize_preview_token(provider_id, "provider")
        sanitized_language = self._sanitize_preview_token(language_code, "lang")
        sanitized_voice = self._sanitize_preview_token(voice_name, "voice")
        return os.path.join(
            output_dir,
            f"{sanitized_service}-{sanitized_provider}-{sanitized_language}-{sanitized_voice}.wav",
        )

    def _build_legacy_voice_preview_path(
        self,
        *,
        service: str,
        voice_name: str,
        ensure_dir: bool,
    ) -> str:
        output_dir = self._get_voice_preview_output_dir(ensure_exists=ensure_dir)
        sanitized_service = self._sanitize_preview_token(service, "tts")
        sanitized_voice = self._sanitize_preview_token(voice_name, "voice")
        return os.path.join(output_dir, f"{sanitized_service}-{sanitized_voice}.wav")

    def get_prebuilt_voice_preview_path(
        self,
        voice_name: str,
        *,
        language_code: str = "",
        existing_only: bool = True,
    ) -> str:
        normalized_voice = str(voice_name or "").strip()
        if not normalized_voice:
            return ""

        service = str(self.state.tts.service or "").strip()
        provider_id = str(self.state.tts.openai_audio_endpoint or "").strip() or service
        normalized_language_code = str(language_code or self.state.tts.language or "").strip().lower() or "default"

        preview_path = self._build_voice_preview_path(
            service=service,
            provider_id=provider_id,
            language_code=normalized_language_code,
            voice_name=normalized_voice,
            ensure_dir=not existing_only,
        )
        if existing_only and not os.path.isfile(preview_path):
            legacy_path = self._build_legacy_voice_preview_path(
                service=service,
                voice_name=normalized_voice,
                ensure_dir=False,
            )
            if os.path.isfile(legacy_path):
                return legacy_path
            return ""
        return preview_path

    def generate_prebuilt_voice_preview_sample(
        self,
        voice_name: str,
        sample_text: str,
        *,
        language_code: str = "",
    ) -> tuple[bool, str, str]:
        normalized_voice = str(voice_name or "").strip()
        if not normalized_voice:
            return False, "", "Voice name is required."

        normalized_text = str(sample_text or "").strip()
        if not normalized_text:
            return False, "", "Preview text is required."

        tts_snapshot = copy.deepcopy(self.state.tts.__dict__)
        service = str(tts_snapshot.get("service") or "").strip()
        provider_id = str(tts_snapshot.get("openai_audio_endpoint") or "").strip()
        if not self.tts_service_supports_prebuilt_voices(service, provider_id):
            return False, "", f"Service '{service or 'Unknown'}' does not use pre-built voices."

        tts_snapshot["speaker"] = normalized_voice
        normalized_language_code = str(language_code or tts_snapshot.get("language") or "").strip().lower()
        if normalized_language_code:
            tts_snapshot["language"] = normalized_language_code

        xtts_url = (
            tts_snapshot.get("external_server_url")
            if tts_snapshot.get("use_external_server") and service == "XTTS"
            else tts_handler.XTTS_API_BASE_URL
        )
        voxcpm_url = (
            tts_snapshot.get("external_server_url")
            if tts_snapshot.get("use_external_server") and service == "VoxCPM"
            else tts_handler.VOXCPM_API_BASE_URL
        )
        fishs2_url = (
            tts_snapshot.get("external_server_url")
            if tts_snapshot.get("use_external_server") and service == "FishS2"
            else tts_handler.FISHS2_API_BASE_URL
        )
        voxtral_url = (
            tts_snapshot.get("external_server_url")
            if tts_snapshot.get("use_external_server") and service == "Voxtral"
            else tts_handler.VOXTRAL_API_BASE_URL
        )
        kokoro_url = (
            tts_snapshot.get("external_server_url")
            if tts_snapshot.get("use_external_server") and service == "Kokoro"
            else tts_handler.KOKORO_API_BASE_URL
        )
        chatterbox_url = (
            tts_snapshot.get("external_server_url")
            if tts_snapshot.get("use_external_server") and service == "Chatterbox"
            else tts_handler.CHATTERBOX_API_BASE_URL
        )

        audio_data = tts_handler.text_to_audio(
            normalized_text,
            tts_snapshot,
            xtts_base_url=xtts_url,
            voxcpm_base_url=voxcpm_url,
            fishs2_base_url=fishs2_url,
            voxtral_base_url=voxtral_url,
            kokoro_base_url=kokoro_url,
            chatterbox_base_url=chatterbox_url,
        )
        if audio_data is None:
            return False, "", "TTS generation failed for this voice."

        output_path = self._build_voice_preview_path(
            service=service,
            provider_id=provider_id or service,
            language_code=normalized_language_code or "default",
            voice_name=normalized_voice,
            ensure_dir=True,
        )

        try:
            audio_data.export(output_path, format="wav")
            return True, output_path, ""
        except Exception as e:
            return False, "", f"Could not save preview WAV: {e}"

    def play_audio_file(self, audio_path: str) -> bool:
        normalized_path = str(audio_path or "").strip()
        if not normalized_path or not os.path.isfile(normalized_path):
            self._notify_user("Preview file is missing and cannot be played.", level="warning")
            return False

        self.stop_playback()
        playback_handler = self._ensure_playback_handler()
        if playback_handler is None:
            self._notify_user("Audio playback is unavailable.", level="warning")
            return False

        if playback_handler.play(normalized_path):
            self.playlist_timer.start()
            return True

        self._notify_user(f"Failed to play preview audio: {os.path.basename(normalized_path)}", level="warning")
        return False

    def set_tts_speaker_voice(self, speaker_name: str):
        normalized_speaker = str(speaker_name or "").strip()
        language_changed = self._sync_kokoro_language_from_voice(normalized_speaker)
        if self.state.tts.speaker == normalized_speaker and not language_changed:
            return
        self.state.tts.speaker = normalized_speaker
        self.state_changed.emit()

    def list_voice_library(self) -> list[dict]:
        """Returns voice library entries with sample metadata."""
        return voice_library_handler.list_voices()

    def create_voice_library_voice(
        self,
        name: str,
        notes: str = "",
        prompt_text: str = "",
        language_code: str = "",
    ) -> dict:
        """Creates a new voice library entry."""
        return voice_library_handler.create_voice(
            name,
            notes=notes,
            prompt_text=prompt_text,
            language_code=language_code,
        )

    def update_voice_library_voice(
        self,
        voice_id: str,
        *,
        name: str | None = None,
        notes: str | None = None,
        prompt_text: str | None = None,
        language_code: str | None = None,
    ) -> dict:
        """Updates a voice library entry."""
        return voice_library_handler.update_voice(
            voice_id,
            name=name,
            notes=notes,
            prompt_text=prompt_text,
            language_code=language_code,
        )

    def delete_voice_library_voice(self, voice_id: str):
        """Deletes a voice library entry and its stored samples."""
        voice_library_handler.delete_voice(voice_id)

    def add_voice_library_samples(self, voice_id: str, source_wav_paths: list[str]) -> list[dict]:
        """Copies WAV samples into the voice library and stores metadata."""
        return voice_library_handler.add_samples(voice_id, source_wav_paths)

    def update_voice_library_sample(
        self,
        voice_id: str,
        sample_id: str,
        *,
        transcript: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Updates transcript or notes for a voice sample."""
        return voice_library_handler.update_sample(
            voice_id,
            sample_id,
            transcript=transcript,
            notes=notes,
        )

    def delete_voice_library_sample(self, voice_id: str, sample_id: str):
        """Deletes one sample from a voice library entry."""
        voice_library_handler.delete_sample(voice_id, sample_id)

    def _resolve_tts_upload_base_url(self, service: str) -> str:
        default_base_url = tts_handler.XTTS_API_BASE_URL
        if service == "VoxCPM":
            default_base_url = tts_handler.VOXCPM_API_BASE_URL
        elif service == "FishS2":
            default_base_url = tts_handler.FISHS2_API_BASE_URL
        elif service == "Chatterbox":
            default_base_url = tts_handler.CHATTERBOX_API_BASE_URL

        return (
            self.state.tts.external_server_url
            if self.state.tts.use_external_server
            else default_base_url
        )

    def upload_voice_library_voice(
        self,
        voice_id: str,
        sample_id: str | None = None,
    ) -> tuple[bool, str]:
        """Uploads a library voice to the current local backend service."""
        service = str(self.state.tts.service or "").strip()
        if service not in {"XTTS", "VoxCPM", "FishS2", "Chatterbox"}:
            message = "Voice library upload is only supported for XTTS, VoxCPM, FishS2, and Chatterbox."
            self.show_error.emit("Upload Error", message)
            return False, message

        try:
            voice = voice_library_handler.get_voice(voice_id)
            if voice is None:
                raise ValueError("Voice library entry was not found.")

            voice_name = str(voice.get("name") or "").strip()
            if not voice_name:
                raise ValueError("Voice library entry must have a name before upload.")

            samples = list(voice.get("samples") or [])
            if not samples:
                raise ValueError("Voice library entry does not contain any samples.")
            normalized_voice_prompt_text = str(voice.get("prompt_text") or "").strip()
            normalized_voice_language_code = str(voice.get("language_code") or "").strip().lower()

            base_url = self._resolve_tts_upload_base_url(service)

            if service == "XTTS":
                sample_paths: list[str] = []
                for sample in samples:
                    sample_path = voice_library_handler.resolve_sample_path(
                        str(sample.get("relative_path") or "")
                    )
                    if sample_path and os.path.isfile(sample_path):
                        sample_paths.append(sample_path)

                if not sample_paths:
                    raise ValueError("None of the saved WAV files for this voice are available on disk.")

                speaker_name = tts_handler.upload_speaker_voice(
                    sample_paths,
                    base_url=base_url,
                    service=service,
                    voice_id=voice_name,
                )
                self._notify_user(
                    f"Uploaded XTTS voice: {speaker_name} ({len(sample_paths)} sample(s))"
                )
            else:
                selected_sample = None
                normalized_sample_id = str(sample_id or "").strip()
                if normalized_sample_id:
                    selected_sample = next(
                        (
                            sample
                            for sample in samples
                            if str(sample.get("id") or "").strip() == normalized_sample_id
                        ),
                        None,
                    )
                    if selected_sample is None:
                        raise ValueError("Selected sample was not found in this voice entry.")
                else:
                    selected_sample = samples[0]

                sample_path = voice_library_handler.resolve_sample_path(
                    str(selected_sample.get("relative_path") or "")
                )
                if not sample_path or not os.path.isfile(sample_path):
                    raise ValueError("Selected sample WAV file is missing on disk.")

                normalized_transcript = str(selected_sample.get("transcript") or "").strip()
                prompt_from_voice_entry = bool(normalized_voice_prompt_text)
                upload_prompt_text = normalized_voice_prompt_text or normalized_transcript or None
                upload_mode = "hifi" if (service == "VoxCPM" and upload_prompt_text) else "reference"

                speaker_name = tts_handler.upload_speaker_voice(
                    sample_path,
                    base_url=base_url,
                    service=service,
                    prompt_text=upload_prompt_text,
                    mode=upload_mode,
                    voice_id=voice_name,
                )
                if upload_prompt_text:
                    prompt_note = "voice prompt" if prompt_from_voice_entry else "sample transcript"
                else:
                    prompt_note = "no prompt text"
                if service == "VoxCPM":
                    self._notify_user(
                        f"Uploaded VoxCPM voice: {speaker_name} ({upload_mode} mode, {prompt_note})"
                    )
                elif service == "FishS2":
                    self._notify_user(f"Uploaded FishS2 voice: {speaker_name} ({prompt_note})")
                elif service == "Chatterbox":
                    self._notify_user(f"Uploaded Chatterbox voice: {speaker_name} ({prompt_note})")
                else:
                    self._notify_user(f"Uploaded {service} voice: {speaker_name} ({prompt_note})")

            self.state.tts.speaker = speaker_name
            if normalized_voice_language_code:
                self.state.tts.language = normalized_voice_language_code
            self.state_changed.emit()
            self.connect_tts_server()
            return True, speaker_name
        except Exception as e:
            message = f"Failed to upload {service} voice from library: {e}"
            self.show_error.emit("Upload Error", message)
            return False, str(e)

    def upload_speaker_voice(self, wav_file_path: str, prompt_text: str | None = None):
        """Uploads a speaker voice file and refreshes available local backend voices."""
        service = str(self.state.tts.service or "").strip()
        if service not in {"XTTS", "VoxCPM", "FishS2", "Chatterbox"}:
            self.show_error.emit(
                "Upload Error",
                "Voice upload is only supported for XTTS, VoxCPM, FishS2, and Chatterbox.",
            )
            return

        try:
            default_base_url = tts_handler.XTTS_API_BASE_URL
            if service == "VoxCPM":
                default_base_url = tts_handler.VOXCPM_API_BASE_URL
            elif service == "FishS2":
                default_base_url = tts_handler.FISHS2_API_BASE_URL
            elif service == "Chatterbox":
                default_base_url = tts_handler.CHATTERBOX_API_BASE_URL

            base_url = (
                self.state.tts.external_server_url
                if self.state.tts.use_external_server
                else default_base_url
            )

            normalized_prompt_text = str(prompt_text or "").strip() or None
            upload_prompt_text = None
            upload_mode = None
            if service == "VoxCPM":
                upload_prompt_text = normalized_prompt_text
                upload_mode = "hifi" if upload_prompt_text else "reference"
            elif service in {"FishS2", "Chatterbox"}:
                upload_prompt_text = normalized_prompt_text

            speaker_name = tts_handler.upload_speaker_voice(
                wav_file_path,
                base_url=base_url,
                service=service,
                prompt_text=upload_prompt_text,
                mode=upload_mode,
            )
            if service == "VoxCPM":
                self._notify_user(f"Uploaded VoxCPM voice: {speaker_name} ({upload_mode} mode)")
            elif service == "FishS2":
                transcript_note = "with transcript" if upload_prompt_text else "without transcript"
                self._notify_user(f"Uploaded FishS2 voice: {speaker_name} ({transcript_note})")
            elif service == "Chatterbox":
                transcript_note = "with transcript" if upload_prompt_text else "without transcript"
                self._notify_user(f"Uploaded Chatterbox voice: {speaker_name} ({transcript_note})")
            else:
                self._notify_user(f"Uploaded {service} voice: {speaker_name}")
            self.state.tts.speaker = speaker_name
            self.state_changed.emit()
            self.connect_tts_server()
        except Exception as e:
            self.show_error.emit("Upload Error", f"Failed to upload {service} voice: {e}")

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

    # --- TTS Providers ---
    def list_tts_provider_configs(self) -> list[dict]:
        """Returns normalized provider configuration for cloud TTS UI."""
        provider_configs = tts_handler.get_provider_configs(self.state.tts)
        if provider_configs != self.state.tts.provider_configs:
            self.state.tts.provider_configs = provider_configs
        return copy.deepcopy(provider_configs)

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

                for prompt_key in ("combined_prompt", "first_prompt", "second_prompt", "third_prompt"):
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
        supports_prebuilt_voices: bool | None = None,
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
            supports_prebuilt_voices=supports_prebuilt_voices,
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
            self._notify_user(message, timeout_ms=4000)
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

    def _invalidate_audio_variants_for_sentences(self, sentence_numbers: list[str]):
        base_dir = self._get_audio_variant_base_dir(ensure_exists=False)
        variants_dir = os.path.join(base_dir, audio_variant_handler.VARIANTS_DIR_NAME)
        if not os.path.isdir(variants_dir):
            return

        audio_variant_handler.remove_variant_sentences(
            base_dir,
            self.state.session_name,
            [str(number) for number in sentence_numbers],
        )
        self.get_active_audio_variant_id()
        self._persist_session_config(force=True)

    def _clear_audio_variants(self):
        base_dir = self._get_audio_variant_base_dir(ensure_exists=False)
        audio_variant_handler.remove_all_variants(base_dir)
        self.state.active_audio_variant_id = audio_variant_handler.SOURCE_VARIANT_ID

    def _process_source_wav_to_rvc_variant(
        self,
        sentence_number: str,
        rvc_settings: dict,
        source_wav_path: str | None = None,
    ) -> str | None:
        model_name = str(rvc_settings.get("rvc_model") or "").strip()
        if not model_name:
            self.log_message.emit("Skipping RVC variant creation; no RVC model is selected.")
            return None

        source_path = source_wav_path or self._find_source_sentence_wav_path(str(sentence_number))
        if not source_path or not os.path.isfile(source_path):
            self.log_message.emit(f"Skipping RVC variant for sentence {sentence_number}; source WAV is missing.")
            return None

        base_dir = self._get_audio_variant_base_dir(ensure_exists=True)
        target_path = audio_variant_handler.rvc_variant_sentence_path(
            base_dir,
            rvc_settings,
            self.state.session_name,
            str(sentence_number),
            ensure_dir=True,
        )

        with open(source_path, "rb") as source_file:
            source_audio = AudioSegment.from_file(source_file, format="wav")
        converted_audio = rvc_handler.process_with_rvc(source_audio, rvc_settings)
        _export_audio_segment(converted_audio, target_path)

        record = audio_variant_handler.register_rvc_variant_sentence(
            base_dir,
            rvc_settings,
            str(sentence_number),
        )
        return str(record.get("id") or "")

    # --- Config/API Keys ---
    def save_api_key(self, key_name: str, key_value: str) -> bool:
        """Saves an API key and returns success state."""
        normalized_value = (key_value or "").strip()
        try:
            config_handler.save_api_key(key_name, normalized_value)
            self._notify_user(f"Saved API key: {key_name}")
            return True
        except Exception as e:
            self.show_error.emit("API Key Error", f"Could not save API key {key_name}: {e}")
            return False

    def get_api_key(self, key_name: str) -> str:
        """Gets an API key."""
        return config_handler.get_api_key(key_name)

    # --- Sentence Management ---
    def update_sentence_text(self, sentence_number: str, new_text: str):
        if self._is_generation_or_regeneration_running():
            self._notify_user(
                "Cannot edit sentences while generation, regeneration, or RVC processing is running.",
                level="warning",
            )
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
        if self._is_generation_or_regeneration_running():
            self._notify_user(
                "Cannot change marked status while generation, regeneration, or RVC processing is running.",
                level="warning",
            )
            return

        if session_handler.update_sentence_marked_status(self.state.session_name, sentence_number, marked):
            with self._state_lock:
                for s in self.state.processed_sentences:
                    if str(s.get('sentence_number')) == str(sentence_number):
                        s['marked'] = marked
                        break
            self.state_changed.emit()

    def remove_sentences(self, sentence_numbers: list[str]):
        if self._is_generation_or_regeneration_running():
            self._notify_user(
                "Cannot remove sentences while generation, regeneration, or RVC processing is running.",
                level="warning",
            )
            return

        if session_handler.remove_sentences(self.state.session_name, sentence_numbers):
            self._clear_audio_variants()
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

        playback_handler = self._ensure_playback_handler()
        if playback_handler is None:
            self._notify_user("Audio playback is unavailable.", level="warning")
            if not keep_playlist_state:
                self._set_current_playing_sentence(None)
            return False

        wav_path = self._find_sentence_wav_path(sentence_number)

        if not wav_path:
            self._notify_user(f"Audio file not found for sentence {sentence_number}.", level="warning")
            if not keep_playlist_state:
                self._set_current_playing_sentence(None)
            return False

        if playback_handler.play(wav_path):
            self._set_current_playing_sentence(str(sentence_number))
            self.playlist_timer.start()
            return True

        self._notify_user(f"Failed to play audio for sentence {sentence_number}.", level="warning")
        if not keep_playlist_state:
            self._set_current_playing_sentence(None)
        return False

    def stop_playback(self):
        self.playlist_active = False
        self.playlist_sentences = []
        self.current_playlist_index = 0
        self.playlist_timer.stop()
        if self.playback_handler is not None:
            self.playback_handler.stop()
        self._set_current_playing_sentence(None)

    def toggle_pause_playback(self):
        if self.playback_handler is not None:
            self.playback_handler.toggle_pause()

    def play_playlist(self, start_sentence_number: str | None = None):
        """Starts playing the processed sentences as a playlist."""
        self.stop_playback()
        all_sentences = self.get_audio_variant_sentences_snapshot()
        self.playlist_sentences = [
            s for s in all_sentences if s.get('tts_generated') == 'yes'
        ]
        if not self.playlist_sentences:
            self._notify_user("No generated audio is available to play.", level="warning")
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
            self._notify_user("No playable audio found in playlist.", level="warning")
            self.stop_playback()

    def _refresh_playlist_sentences(self, last_played_sentence_number: str | None = None):
        """Rebuilds playlist from generated sentences and keeps playback position stable."""
        all_sentences = self.get_audio_variant_sentences_snapshot()
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
        playback_handler = self.playback_handler
        playback_finished = True
        if playback_handler is not None:
            playback_finished = playback_handler.check_if_finished()

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
        elif playback_handler is not None and playback_handler.get_busy():
            return
        else:
            self._refresh_playlist_sentences(self.current_playing_sentence_number)
            if self._play_current_playlist_item():
                return

        if self._is_generation_or_regeneration_running():
            return

        self.log_message.emit("Playlist finished.")
        self.stop_playback()

    def shutdown(self):
        """Best-effort cleanup of runtime resources before app exit."""
        try:
            self.playlist_active = False
            self.playlist_sentences = []
            self.current_playlist_index = 0
            self.playlist_timer.stop()
            self.current_playing_sentence_number = None
            if self.playback_handler is not None:
                self.playback_handler.stop()
                self.playback_handler.quit()
            self._persist_session_config(force=True)
            self._persist_global_settings(force=True)
        except Exception as e:
            logging.warning("Shutdown cleanup encountered an issue: %s", e, exc_info=True)

    # --- Output Generation ---
    def save_output(self, output_path: str):
        try:
            active_variant_id = self.get_active_audio_variant_id()
            active_variant = next(
                (
                    variant
                    for variant in self.list_audio_variants()
                    if str(variant.get("id") or "") == active_variant_id
                ),
                None,
            )
            sentence_wavs_dir = None
            sentence_numbers = None
            variant_label = "Original"

            if active_variant_id != audio_variant_handler.SOURCE_VARIANT_ID and active_variant is not None:
                base_dir = self._get_audio_variant_base_dir(ensure_exists=False)
                sentence_wavs_dir = audio_variant_handler.variant_wavs_dir(base_dir, active_variant_id)
                sentence_numbers = [str(number) for number in list(active_variant.get("sentence_numbers") or [])]
                variant_label = str(active_variant.get("label") or "RVC version")

                total = int(active_variant.get("total") or 0)
                count = len(sentence_numbers)
                if total and count < total:
                    self._notify_user(
                        "Active RVC version is partial; exporting converted segments only.",
                        level="warning",
                    )
            elif self._is_dubbing_source_selected():
                sentence_wavs_dir = self._get_primary_sentence_wavs_dir(ensure_exists=False)

            success = audio_processor.save_output(
                session_name=self.state.session_name,
                output_path=output_path,
                output_format=self.state.audio_processing.output_format,
                bitrate=self.state.audio_processing.bitrate,
                metadata=self.state.metadata,
                cover_image_path=self.state.cover_image_path,
                sentence_wavs_dir=sentence_wavs_dir,
                sentence_numbers=sentence_numbers,
            )
            if success:
                self._set_session_activity(
                    "Audio generation complete",
                    f"Final output saved to {os.path.basename(output_path)}.",
                    "success",
                )
                if active_variant_id == audio_variant_handler.SOURCE_VARIANT_ID:
                    self._notify_user(f"Output file saved: {os.path.basename(output_path)}")
                else:
                    self._notify_user(
                        f"Output file saved from {variant_label}: {os.path.basename(output_path)}"
                    )
            else:
                self._set_session_activity(
                    "Final output save failed",
                    "The sentence audio was generated, but the final output file could not be saved.",
                    "error",
                )
                self.show_error.emit("Save Error", "Failed to save the output file. Check logs for details.")
        except Exception as e:
            self._set_session_activity(
                "Final output save failed",
                str(e),
                "error",
            )
            self.show_error.emit("Save Error", f"An unexpected error occurred: {e}")

    def save_xtts_settings(self):
        """Persists XTTS advanced settings locally and for the active session."""
        if self.state.tts.service != "XTTS":
            return

        self._persist_global_settings(force=True)
        if self._is_named_session_active():
            self._persist_session_config(force=True)

        self._notify_user(
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
            self._notify_user("XTTS training is already running.", level="warning")
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
            self._notify_user(
                "Wait for full generation to finish before regenerating selected sentences.",
                level="warning",
            )
            return

        if self._is_regeneration_running():
            self._notify_user("Sentence regeneration is already running.", level="warning")
            return

        if self._is_rvc_processing_running():
            self._notify_user(
                "Wait for RVC sentence processing to finish before regenerating sentences.",
                level="warning",
            )
            return

        normalized_numbers: list[str] = []
        seen_numbers: set[str] = set()
        for sentence_number in sentence_numbers:
            normalized = str(sentence_number or "").strip()
            if not normalized or normalized in seen_numbers:
                continue
            seen_numbers.add(normalized)
            normalized_numbers.append(normalized)

        if not normalized_numbers:
            self._notify_user("Select at least one sentence to regenerate.", level="warning")
            return

        self.stop_playback()
        self.cancel_regeneration_flag.clear()
        self.regeneration_thread = self._run_threaded_task(self._regenerate_sentences_thread, normalized_numbers)
        self.state_changed.emit()

    def _regenerate_sentences_thread(self, sentence_numbers: list[str]):
        """Thread worker for regenerating sentences."""
        worker_thread = threading.current_thread()
        try:
            total = len(sentence_numbers)
            regenerated_count = 0
            skipped_count = 0
            cancelled = False
            start_time = time.time()
            self.progress_updated.emit(0, max(total, 1), 0.0)
            self._set_session_activity(
                "Regenerating audio",
                f"Updating {total} selected sentence(s).",
                "active",
            )
            self.log_message.emit(f"Starting regeneration for {total} sentence(s).")
            processed_sentences = self.get_processed_sentences_snapshot()
            sentence_index_map = {
                str(sentence.get('sentence_number')): index
                for index, sentence in enumerate(processed_sentences)
            }

            for i, num in enumerate(sentence_numbers, start=1):
                if self.cancel_regeneration_flag.is_set():
                    cancelled = True
                    self.log_message.emit("Regeneration cancellation acknowledged. Stopping remaining queue...")
                    break

                self._set_session_activity(
                    "Regenerating audio",
                    f"Rendering selected sentence {i} of {total} (Sentence {num}).",
                    "active",
                )
                self.log_message.emit(f"Regenerating sentence {i}/{total} (Number: {num})...")

                sentence_index = sentence_index_map.get(str(num))
                if sentence_index is None:
                    self.log_message.emit(f"Could not find sentence {num} to regenerate.")
                    skipped_count += 1
                    self.progress_updated.emit(i, total, time.time() - start_time)
                    continue

                sentence_dict = processed_sentences[sentence_index]
                success, updated_sentence = self._execute_generation_for_sentence(sentence_dict)
                if not success or updated_sentence is None:
                    self._set_session_activity(
                        "Regeneration failed",
                        f"Sentence {num} could not be regenerated.",
                        "error",
                    )
                    self.show_error.emit("Regeneration Failed", f"Failed to regenerate audio for sentence {num}. See logs.")
                    return

                processed_sentences[sentence_index] = updated_sentence
                regenerated_count += 1
                self._set_processed_sentences_snapshot(processed_sentences)
                self.state_changed.emit()
                self.progress_updated.emit(i, total, time.time() - start_time)

                if self.cancel_regeneration_flag.is_set() and i < total:
                    cancelled = True
                    self.log_message.emit(
                        "Current regeneration finished after cancellation request. Stopping remaining queue..."
                    )
                    break

            summary_parts = [f"Regenerated {regenerated_count} sentence(s)."]
            if skipped_count:
                summary_parts.append(f"Skipped {skipped_count} unavailable sentence(s).")
            summary_message = " ".join(summary_parts)
            if cancelled:
                self._set_session_activity(
                    "Regeneration cancelled",
                    summary_message,
                    "warning",
                )
                self.log_message.emit("Regeneration cancelled.")
            else:
                self._set_session_activity(
                    "Regeneration finished",
                    summary_message,
                    "success" if skipped_count == 0 else "warning",
                )
                self.log_message.emit("Regeneration finished.")
        except Exception as e:
            logging.error("Unexpected regeneration worker error: %s", e, exc_info=True)
            self._set_session_activity(
                "Regeneration failed",
                f"Unexpected regeneration failure: {e}",
                "error",
            )
            self.show_error.emit("Regeneration Error", f"Unexpected regeneration failure: {e}")
        finally:
            if self.regeneration_thread is worker_thread:
                self.regeneration_thread = None
            self.cancel_regeneration_flag.clear()
            self.state_changed.emit()

    def process_sentences_with_rvc(self, sentence_numbers: list[str]):
        """Applies RVC to existing generated sentence WAV files without regenerating TTS."""
        if self._is_generation_running():
            self._notify_user(
                "Wait for full generation to finish before starting RVC sentence processing.",
                level="warning",
            )
            return

        if self._is_regeneration_running():
            self._notify_user(
                "Wait for sentence regeneration to finish before starting RVC processing.",
                level="warning",
            )
            return

        if self._is_rvc_processing_running():
            self._notify_user("RVC sentence processing is already running.", level="warning")
            return

        if not self.is_rvc_available():
            self.show_error.emit("RVC Unavailable", "RVC dependencies are not installed.")
            return

        rvc_settings = copy.deepcopy(self.state.rvc.__dict__)
        model_name = str(rvc_settings.get("rvc_model") or "").strip()
        if not model_name:
            self.show_error.emit("RVC Model Required", "Please select an RVC model before processing.")
            return

        normalized_numbers: list[str] = []
        seen_numbers: set[str] = set()
        for sentence_number in sentence_numbers:
            normalized = str(sentence_number or "").strip()
            if not normalized or normalized in seen_numbers:
                continue
            seen_numbers.add(normalized)
            normalized_numbers.append(normalized)

        if not normalized_numbers:
            self._notify_user("Select at least one valid sentence for RVC processing.", level="warning")
            return

        self.stop_playback()
        self.rvc_processing_thread = self._run_threaded_task(
            self._process_sentences_with_rvc_thread,
            normalized_numbers,
            rvc_settings,
        )
        self.state_changed.emit()

    def _process_sentences_with_rvc_thread(self, sentence_numbers: list[str], rvc_settings: dict):
        """Thread worker for RVC post-processing of existing sentence WAV files."""
        worker_thread = threading.current_thread()
        processed_count = 0
        skipped_count = 0
        target_variant_id = audio_variant_handler.rvc_variant_id_for_settings(rvc_settings)

        try:
            total = len(sentence_numbers)
            start_time = time.time()
            self.progress_updated.emit(0, max(total, 1), 0.0)
            self._set_session_activity(
                "Applying RVC processing",
                f"Processing {total} selected sentence(s) into an RVC audio version.",
                "active",
            )
            self.log_message.emit(f"Starting RVC processing for {total} sentence(s).")
            processed_sentences = self.get_processed_sentences_snapshot()
            sentence_index_map = {
                str(sentence.get("sentence_number")): index
                for index, sentence in enumerate(processed_sentences)
            }

            for i, num in enumerate(sentence_numbers, start=1):
                self._set_session_activity(
                    "Applying RVC processing",
                    f"Processing selected sentence {i} of {total} (Sentence {num}).",
                    "active",
                )
                sentence_index = sentence_index_map.get(str(num))
                if sentence_index is None:
                    self.log_message.emit(f"Skipping sentence {num}; it no longer exists.")
                    skipped_count += 1
                    self.progress_updated.emit(i, total, time.time() - start_time)
                    continue

                sentence_dict = processed_sentences[sentence_index]
                if sentence_dict.get("tts_generated") != "yes":
                    self.log_message.emit(f"Skipping sentence {num}; no generated audio is available yet.")
                    skipped_count += 1
                    self.progress_updated.emit(i, total, time.time() - start_time)
                    continue

                source_wav_path = self._find_source_sentence_wav_path(str(num))
                if not source_wav_path:
                    self.log_message.emit(f"Skipping sentence {num}; WAV file is missing.")
                    skipped_count += 1
                    self.progress_updated.emit(i, total, time.time() - start_time)
                    continue

                self.log_message.emit(f"Processing sentence {i}/{total} with RVC (Number: {num})...")

                try:
                    created_variant_id = self._process_source_wav_to_rvc_variant(
                        str(num),
                        rvc_settings,
                        source_wav_path=source_wav_path,
                    )
                    if created_variant_id:
                        target_variant_id = created_variant_id
                        processed_count += 1
                    else:
                        skipped_count += 1
                except Exception as e:
                    logging.error(
                        "Failed RVC post-processing for sentence %s: %s",
                        num,
                        e,
                        exc_info=True,
                    )
                    self.log_message.emit(f"Failed RVC processing for sentence {num}; see logs for details.")
                    skipped_count += 1
                finally:
                    self.progress_updated.emit(i, total, time.time() - start_time)

            if processed_count and target_variant_id:
                self.state.active_audio_variant_id = target_variant_id
                self._persist_session_config(force=True)

            summary_parts = [f"Processed {processed_count} sentence(s)."]
            if skipped_count:
                summary_parts.append(f"Skipped {skipped_count} sentence(s) that were unavailable.")
            self._set_session_activity(
                "RVC processing finished",
                " ".join(summary_parts),
                "success" if skipped_count == 0 else "warning",
            )
            self.log_message.emit(
                f"RVC processing finished. Processed {processed_count} sentence(s), skipped {skipped_count}."
            )
        except Exception as e:
            logging.error("Unexpected RVC processing worker error: %s", e, exc_info=True)
            self._set_session_activity(
                "RVC processing failed",
                f"Unexpected RVC processing failure: {e}",
                "error",
            )
            self.show_error.emit("RVC Processing Error", f"Unexpected RVC processing failure: {e}")
        finally:
            if self.rvc_processing_thread is worker_thread:
                self.rvc_processing_thread = None
            self.state_changed.emit()

    def _run_llm_processing(self, text: str) -> tuple[str, int]:
        """Runs the configured LLM prompt chain on a text and returns (text, prompt_count)."""
        processed_text = text
        prompts_ran = 0
        if self.state.llm.use_multi_stage:
            prompts_to_run = [
                self.state.llm.first_prompt,
                self.state.llm.second_prompt,
                self.state.llm.third_prompt,
            ]
        else:
            prompts_to_run = [
                self.state.llm.combined_prompt,
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

    def _execute_generation_for_sentence(self, sentence_dict: dict, skip_llm: bool = False) -> tuple[bool, dict | None]:
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
            if self.state.llm.processing_enabled and not skip_llm:
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
            voxcpm_url = (
                self.state.tts.external_server_url
                if self.state.tts.use_external_server and active_service == "VoxCPM"
                else tts_handler.VOXCPM_API_BASE_URL
            )
            fishs2_url = (
                self.state.tts.external_server_url
                if self.state.tts.use_external_server and active_service == "FishS2"
                else tts_handler.FISHS2_API_BASE_URL
            )
            voxtral_url = (
                self.state.tts.external_server_url
                if self.state.tts.use_external_server and active_service == "Voxtral"
                else tts_handler.VOXTRAL_API_BASE_URL
            )
            kokoro_url = (
                self.state.tts.external_server_url
                if self.state.tts.use_external_server and active_service == "Kokoro"
                else tts_handler.KOKORO_API_BASE_URL
            )
            chatterbox_url = (
                self.state.tts.external_server_url
                if self.state.tts.use_external_server and active_service == "Chatterbox"
                else tts_handler.CHATTERBOX_API_BASE_URL
            )
            audio_data = tts_handler.text_to_audio(
                processed_text,
                self.state.tts.__dict__,
                xtts_base_url=xtts_url,
                voxcpm_base_url=voxcpm_url,
                fishs2_base_url=fishs2_url,
                voxtral_base_url=voxtral_url,
                kokoro_base_url=kokoro_url,
                chatterbox_base_url=chatterbox_url,
            )
            if not audio_data:
                return False, None

            # 3. Fade
            if self.state.audio_processing.enable_fade:
                audio_data = audio_processor.apply_fade(audio_data, self.state.audio_processing.fade_in_duration, self.state.audio_processing.fade_out_duration)
            
            # 4. Add Silence
            if not self._is_dubbing_source_selected():
                silence_to_add = 0
                if updated_sentence.get("paragraph", "no") == "yes":
                    silence_to_add = self.state.audio_processing.silence_for_paragraphs
                else:
                    silence_to_add = self.state.audio_processing.silence_between_sentences
                
                if silence_to_add > 0:
                    audio_data += AudioSegment.silent(duration=silence_to_add)

            # 5. Save original/source WAV
            session_name = self.state.session_name
            num = updated_sentence['sentence_number']
            wavs_dir = self._get_primary_sentence_wavs_dir(ensure_exists=True)
            wav_path = os.path.join(wavs_dir, f"{session_name}_sentence_{num}.wav")
            _export_audio_segment(audio_data, wav_path)
            self._invalidate_audio_variants_for_sentences([str(num)])

            # 6. Optionally create/update the matching RVC variant from source audio.
            if self.state.rvc.enable_rvc:
                rvc_settings = copy.deepcopy(self.state.rvc.__dict__)
                rvc_variant_id = self._process_source_wav_to_rvc_variant(
                    str(num),
                    rvc_settings,
                    source_wav_path=wav_path,
                )
                if rvc_variant_id:
                    self.state.active_audio_variant_id = rvc_variant_id

            updated_sentence['tts_generated'] = 'yes'
            return True, updated_sentence

        except Exception as e:
            logging.error(f"Failed to execute generation for sentence {sentence_dict.get('sentence_number')}: {e}", exc_info=True)
            return False, None
