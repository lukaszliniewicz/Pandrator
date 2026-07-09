import datetime
import copy
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any

from .source_media import (
    AUDIO_SOURCE_EXTENSIONS,
    DUBBING_SOURCE_EXTENSIONS,
    SOURCE_FILE_EXTENSIONS,
    TEXT_SOURCE_EXTENSIONS,
    VIDEO_SOURCE_EXTENSIONS,
)


STATE_DB_FILENAME = "pandrator_state.sqlite3"
SCHEMA_VERSION = 3

OUTPUTS_DIRNAME = "Outputs"
TRASH_DIRNAME = ".trash"
SESSION_CONFIG_FILENAME = "session_config.json"
DUBBING_STAGING_DIRNAME = "_dubbing_staging"
DUBBING_RUNS_DIR = os.path.join("dubbing", "runs")
AUDIO_VARIANTS_DIRNAME = "Audio_Variants"
SENTENCE_WAVS_DIRNAME = "Sentence_wavs"
SOURCE_AUDIO_VERSION_ID = "source"

SOURCE_TEXT_EXTENSIONS = TEXT_SOURCE_EXTENSIONS
SOURCE_VIDEO_EXTENSIONS = VIDEO_SOURCE_EXTENSIONS
SOURCE_AUDIO_EXTENSIONS = AUDIO_SOURCE_EXTENSIONS
GENERATED_AUDIO_STEMS = {"aligned_audio", "amplified_dubbed_audio", "mixed_audio", "original_audio"}

DUBBING_STEPS = (
    "transcribe",
    "correct",
    "translate",
    "manual_timing",
    "speech_blocks",
    "tts_generation",
    "sync",
    "equalize",
    "render",
)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_sha256(payload: Any) -> str:
    dumped = _json_dumps(payload)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_handle:
        while True:
            chunk = file_handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def merge_dubbing_llm_usage(
    existing_usage: dict[str, Any] | str | None,
    stage_key: str,
    cost: float = 0.0,
    response_count: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merges one LLM usage event into a per-stage dubbing usage payload."""
    normalized_stage = str(stage_key or "").strip()
    if not normalized_stage:
        raise ValueError("stage_key is required")

    if isinstance(existing_usage, str):
        try:
            parsed_usage = json.loads(existing_usage)
        except json.JSONDecodeError:
            parsed_usage = {}
    else:
        parsed_usage = existing_usage
    usage = copy.deepcopy(parsed_usage) if isinstance(parsed_usage, dict) else {}

    normalized_cost = max(0.0, _safe_float(cost, 0.0))
    normalized_response_count = max(0, _safe_int(response_count, 0))
    stage_payload = usage.get(normalized_stage)
    if not isinstance(stage_payload, dict):
        stage_payload = {}

    events = stage_payload.get("events")
    if not isinstance(events, list):
        events = []

    event: dict[str, Any] = {
        "cost": normalized_cost,
        "response_count": normalized_response_count,
    }
    if isinstance(metadata, dict) and metadata:
        event["metadata"] = copy.deepcopy(metadata)
    events.append(event)

    stage_payload["cost"] = _safe_float(stage_payload.get("cost"), 0.0) + normalized_cost
    stage_payload["response_count"] = _safe_int(stage_payload.get("response_count"), 0) + normalized_response_count
    stage_payload["events"] = events
    usage[normalized_stage] = stage_payload

    total_cost = sum(
        _safe_float(stage.get("cost"), 0.0)
        for stage in usage.values()
        if isinstance(stage, dict)
    )
    total_response_count = sum(
        _safe_int(stage.get("response_count"), 0)
        for stage in usage.values()
        if isinstance(stage, dict)
    )
    return {
        "usage": usage,
        "total_cost": total_cost,
        "response_count": total_response_count,
    }


def _source_type_from_path(path: str) -> str:
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext in SOURCE_TEXT_EXTENSIONS:
        return "text"
    if ext == ".srt":
        return "srt"
    if ext in SOURCE_VIDEO_EXTENSIONS:
        return "video"
    if ext in SOURCE_AUDIO_EXTENSIONS:
        return "audio"
    if ext:
        return ext.lstrip(".")
    return "unknown"


def _is_dubbing_mode(source_path: str) -> bool:
    ext = os.path.splitext(str(source_path or ""))[1].lower()
    return ext in DUBBING_SOURCE_EXTENSIONS


def _is_discovered_source_candidate(path: str) -> bool:
    stem, ext = os.path.splitext(os.path.basename(str(path or "")).lower())
    if ext not in SOURCE_FILE_EXTENSIONS:
        return False
    if ext in SOURCE_VIDEO_EXTENSIONS and (
        stem.startswith("final_output")
        or "_final" in stem
        or stem.endswith("_synced")
    ):
        return False
    if ext in SOURCE_AUDIO_EXTENSIONS and _is_generated_audio_artifact(stem):
        return False
    return True


def _is_generated_audio_artifact(stem: str, root: str = "") -> bool:
    normalized_stem = str(stem or "").strip().lower()
    if (
        normalized_stem in GENERATED_AUDIO_STEMS
        or normalized_stem.endswith("_manual_correction")
        or normalized_stem.endswith("_transcription")
        or "_sentence_" in normalized_stem
    ):
        return True

    root_parts = {
        part.lower()
        for part in os.path.normpath(str(root or "")).split(os.sep)
        if part
    }
    return bool({"sentence_wavs", "audio_variants"} & root_parts)


def _walk_directory_size(path: str) -> int:
    if not os.path.isdir(path):
        return 0

    total_size = 0
    for root, _, files in os.walk(path):
        for name in files:
            file_path = os.path.join(root, name)
            try:
                total_size += os.path.getsize(file_path)
            except OSError:
                continue
    return total_size


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _file_modified_at_iso(path: str) -> str:
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path),
            tz=datetime.timezone.utc,
        ).isoformat(timespec="seconds")
    except OSError:
        return ""


def _relative_path(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _source_display_path_from_payload(payload: dict[str, Any], source_path: str) -> str:
    for key in ("source_display_path", "original_source_file_path", "display_source_path"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return source_path


def _source_display_name_from_path(path: str) -> str:
    normalized = str(path or "").strip()
    if not normalized:
        return ""
    return os.path.basename(normalized.rstrip("/\\")) or normalized


def _sentence_sort_key(value: Any) -> tuple[int, Any]:
    text = str(value or "")
    try:
        return (0, int(text))
    except ValueError:
        return (1, text.lower())


def _dedupe_sentence_numbers(values: list[Any]) -> list[str]:
    normalized = {str(value) for value in values if str(value or "").strip()}
    return sorted(normalized, key=_sentence_sort_key)


def _normalize_for_search(value: Any) -> str:
    return str(value or "").strip().lower()


def _sanitize_for_path_segment(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip())
    sanitized = sanitized.strip("._-")
    return sanitized or "item"


def _redact_api_keys(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            key_lower = str(key).lower()
            if key_lower in {"api_key", "hf_token", "access_token", "secret"} or key_lower.endswith("_api_key"):
                redacted[key] = "***redacted***" if value else ""
                continue
            redacted[key] = _redact_api_keys(value)
        return redacted

    if isinstance(payload, list):
        return [_redact_api_keys(item) for item in payload]

    return payload


def _sanitize_dubbing_run_snapshot(payload: Any) -> dict[str, Any]:
    sanitized = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    sanitized.pop("llm_provider_configs", None)
    sanitized.pop("provider_configs", None)
    redacted = _redact_api_keys(sanitized)
    return redacted if isinstance(redacted, dict) else {}


class StateDBHandler:
    def __init__(self, app_root: str | None = None, db_filename: str = STATE_DB_FILENAME):
        resolved_root = app_root or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self.app_root = os.path.abspath(resolved_root)
        self.db_path = os.path.join(self.app_root, db_filename)
        self.outputs_root = os.path.join(self.app_root, OUTPUTS_DIRNAME)
        self._lock = threading.RLock()
        self._initialized = False

    def _ensure_outputs_root(self):
        os.makedirs(self.outputs_root, exist_ok=True)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _create_schema(self, connection: sqlite3.Connection):
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_settings_current (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                llm_default_model TEXT,
                openai_audio_endpoint TEXT,
                llm_provider_count INTEGER NOT NULL DEFAULT 0,
                tts_provider_count INTEGER NOT NULL DEFAULT 0,
                saved_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                llm_default_model TEXT,
                openai_audio_endpoint TEXT,
                llm_provider_count INTEGER NOT NULL DEFAULT 0,
                tts_provider_count INTEGER NOT NULL DEFAULT 0,
                saved_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_app_settings_history_saved_at
                ON app_settings_history(saved_at DESC);

            CREATE TABLE IF NOT EXISTS sessions (
                session_name TEXT PRIMARY KEY,
                session_path TEXT NOT NULL,
                source_type TEXT,
                source_path TEXT,
                source_display_name TEXT,
                source_display_path TEXT,
                original_source_file_path TEXT,
                tts_service TEXT,
                language TEXT,
                dubbing_mode INTEGER NOT NULL DEFAULT 0,
                progress_percent REAL NOT NULL DEFAULT 0,
                generated_sentences INTEGER NOT NULL DEFAULT 0,
                total_sentences INTEGER NOT NULL DEFAULT 0,
                sentences_hash TEXT,
                speech_blocks_hash TEXT,
                session_size_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'idle',
                has_final_output INTEGER NOT NULL DEFAULT 0,
                final_output_status TEXT,
                dubbing_status TEXT,
                artifact_count INTEGER NOT NULL DEFAULT 0,
                audio_version_count INTEGER NOT NULL DEFAULT 0,
                audio_version_size_bytes INTEGER NOT NULL DEFAULT 0,
                audio_version_summary TEXT,
                config_modified_at TEXT,
                indexed_at TEXT NOT NULL,
                trashed_at TEXT,
                trash_path TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_modified
                ON sessions(config_modified_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_search
                ON sessions(session_name, source_type, source_display_name, status, final_output_status, dubbing_status);

            CREATE TABLE IF NOT EXISTS session_config_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                source_type TEXT,
                source_path TEXT,
                tts_service TEXT,
                language TEXT,
                dubbing_mode INTEGER NOT NULL DEFAULT 0,
                progress_percent REAL NOT NULL DEFAULT 0,
                session_size_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'idle',
                indexed_at TEXT NOT NULL,
                FOREIGN KEY(session_name) REFERENCES sessions(session_name) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_session_config_snapshots_lookup
                ON session_config_snapshots(session_name, indexed_at DESC);

            CREATE TABLE IF NOT EXISTS session_payload_index (
                session_name TEXT PRIMARY KEY,
                sentences_path TEXT,
                sentences_count INTEGER NOT NULL DEFAULT 0,
                generated_sentences INTEGER NOT NULL DEFAULT 0,
                sentences_hash TEXT,
                speech_blocks_path TEXT,
                speech_blocks_count INTEGER NOT NULL DEFAULT 0,
                speech_blocks_hash TEXT,
                summary_text TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(session_name) REFERENCES sessions(session_name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dubbing_runs (
                run_id TEXT PRIMARY KEY,
                session_name TEXT NOT NULL,
                run_dir TEXT NOT NULL,
                source_video_path TEXT,
                source_srt_path TEXT,
                settings_snapshot_json TEXT,
                llm_cost_total REAL NOT NULL DEFAULT 0,
                llm_response_count INTEGER NOT NULL DEFAULT 0,
                llm_usage_json TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                legacy INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(session_name) REFERENCES sessions(session_name) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_dubbing_runs_session
                ON dubbing_runs(session_name, active DESC, updated_at DESC);

            CREATE TABLE IF NOT EXISTS dubbing_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                step_key TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                detail TEXT,
                UNIQUE(run_id, step_key),
                FOREIGN KEY(run_id) REFERENCES dubbing_runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dubbing_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER,
                modified_at TEXT,
                content_hash TEXT,
                is_current INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, role, path),
                FOREIGN KEY(run_id) REFERENCES dubbing_runs(run_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_dubbing_artifacts_role
                ON dubbing_artifacts(run_id, role, is_current, modified_at DESC);

            CREATE TABLE IF NOT EXISTS session_audio_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'source',
                label TEXT,
                model_name TEXT,
                settings_hash TEXT,
                sentence_count INTEGER NOT NULL DEFAULT 0,
                total_sentences INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                relative_dir TEXT,
                partial INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(session_name, variant_id),
                FOREIGN KEY(session_name) REFERENCES sessions(session_name) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_session_audio_versions_session
                ON session_audio_versions(session_name, kind, updated_at DESC);

            CREATE TABLE IF NOT EXISTS trash_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                original_path TEXT NOT NULL,
                trash_path TEXT NOT NULL UNIQUE,
                moved_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                restored_at TEXT,
                deleted_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trash_entries_expiry
                ON trash_entries(expires_at, restored_at, deleted_at);
            """
        )

    def _table_columns(self, connection: sqlite3.Connection, table_name: str) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_column(self, connection: sqlite3.Connection, table_name: str, column_definition: str):
        column_name = column_definition.split()[0]
        if column_name in self._table_columns(connection, table_name):
            return
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}")

    def _ensure_schema_compatibility(self, connection: sqlite3.Connection):
        self._ensure_column(connection, "sessions", "source_display_name TEXT")
        self._ensure_column(connection, "sessions", "source_display_path TEXT")
        self._ensure_column(connection, "sessions", "original_source_file_path TEXT")
        self._ensure_column(connection, "sessions", "audio_version_count INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(connection, "sessions", "audio_version_size_bytes INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(connection, "sessions", "audio_version_summary TEXT")
        self._ensure_column(connection, "dubbing_runs", "llm_cost_total REAL NOT NULL DEFAULT 0")
        self._ensure_column(connection, "dubbing_runs", "llm_response_count INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(connection, "dubbing_runs", "llm_usage_json TEXT")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS session_audio_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'source',
                label TEXT,
                model_name TEXT,
                settings_hash TEXT,
                sentence_count INTEGER NOT NULL DEFAULT 0,
                total_sentences INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                relative_dir TEXT,
                partial INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(session_name, variant_id),
                FOREIGN KEY(session_name) REFERENCES sessions(session_name) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_session_audio_versions_session
                ON session_audio_versions(session_name, kind, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_sessions_search
                ON sessions(session_name, source_type, source_display_name, status, final_output_status, dubbing_status);
            """
        )

    def _initialize_connection(self, connection: sqlite3.Connection):
        connection.execute("PRAGMA journal_mode = WAL")
        version_row = connection.execute("PRAGMA user_version").fetchone()
        current_version = _safe_int(version_row[0], 0) if version_row else 0
        self._create_schema(connection)
        self._ensure_schema_compatibility(connection)
        if current_version < 3:
            rows = connection.execute(
                "SELECT run_id, settings_snapshot_json FROM dubbing_runs WHERE settings_snapshot_json <> ''"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(str(row["settings_snapshot_json"] or ""))
                except json.JSONDecodeError:
                    continue
                sanitized = _sanitize_dubbing_run_snapshot(payload)
                if sanitized != payload:
                    connection.execute(
                        "UPDATE dubbing_runs SET settings_snapshot_json = ? WHERE run_id = ?",
                        (_json_dumps(sanitized), str(row["run_id"])),
                    )
        if current_version < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def initialize_database(self):
        with self._lock:
            if self._initialized:
                return

            self._ensure_outputs_root()
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

            try:
                with self._connection() as connection:
                    self._initialize_connection(connection)
            except sqlite3.DatabaseError as error:
                logging.error("SQLite initialization failed, attempting recovery: %s", error)
                self._recover_corrupted_database()
                with self._connection() as connection:
                    self._initialize_connection(connection)

            self._initialized = True

    def _recover_corrupted_database(self):
        if not os.path.exists(self.db_path):
            return

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{self.db_path}.corrupt.{timestamp}"
        try:
            os.replace(self.db_path, backup_path)
            logging.warning("Moved corrupted SQLite database to %s", backup_path)
        except OSError as error:
            logging.error("Could not move corrupted database file: %s", error)
            raise

    def _ensure_ready(self):
        if not self._initialized:
            self.initialize_database()

    def _load_wrapped_or_plain_payload(self, file_path: str, nested_key: str) -> dict[str, Any]:
        try:
            with open(file_path, "r", encoding="utf-8") as file_handle:
                payload = json.load(file_handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

        if not isinstance(payload, dict):
            return {}

        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            return nested
        return payload

    def _derive_session_status(
        self,
        generated_sentences: int,
        total_sentences: int,
        has_final_output: bool,
        trashed_at: str | None = None,
    ) -> str:
        if trashed_at:
            return "trashed"
        if total_sentences <= 0:
            return "idle"
        if generated_sentences <= 0:
            return "ready"
        if generated_sentences < total_sentences:
            return "in_progress"
        if has_final_output:
            return "completed"
        return "generated"

    def _summarize_sentences(self, sentences: list[dict[str, Any]]) -> str:
        snippets: list[str] = []
        for item in sentences[:3]:
            text = str(
                item.get("processed_sentence")
                or item.get("original_sentence")
                or ""
            ).strip()
            if text:
                snippets.append(text[:120])
        return " | ".join(snippets)

    def _read_sentences_from_disk(self, session_name: str, session_path: str) -> tuple[list[dict[str, Any]], str]:
        sentences_path = os.path.join(session_path, f"{session_name}_sentences.json")
        try:
            with open(sentences_path, "r", encoding="utf-8") as file_handle:
                payload = json.load(file_handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return [], ""

        if not isinstance(payload, list):
            return [], sentences_path

        normalized: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                normalized.append(item)
        return normalized, sentences_path

    def _discover_latest_speech_blocks_file(self, session_path: str) -> str:
        candidate_paths: list[str] = []
        for root, _, files in os.walk(session_path):
            for file_name in files:
                if file_name.lower().endswith("_speech_blocks.json"):
                    candidate_paths.append(os.path.join(root, file_name))

        if not candidate_paths:
            return ""

        return max(candidate_paths, key=lambda path: (os.path.getmtime(path), path.lower()))

    def _extract_speech_blocks(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            for key in ("speech_blocks", "blocks", "items", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]

        return []

    def _read_speech_blocks_from_disk(self, session_path: str) -> tuple[list[dict[str, Any]], str]:
        speech_blocks_path = self._discover_latest_speech_blocks_file(session_path)
        if not speech_blocks_path:
            return [], ""

        try:
            with open(speech_blocks_path, "r", encoding="utf-8") as file_handle:
                payload = json.load(file_handle)
        except (OSError, json.JSONDecodeError):
            return [], speech_blocks_path

        return self._extract_speech_blocks(payload), speech_blocks_path

    def _discover_session_source_files(self, session_path: str) -> list[str]:
        if not os.path.isdir(session_path):
            return []

        supported_extensions = SOURCE_FILE_EXTENSIONS
        candidates: list[str] = []
        for name in os.listdir(session_path):
            path = os.path.join(session_path, name)
            if (
                os.path.isfile(path)
                and os.path.splitext(name)[1].lower() in supported_extensions
                and _is_discovered_source_candidate(path)
            ):
                candidates.append(os.path.abspath(path))

        return sorted(
            candidates,
            key=lambda path: (_file_modified_at_iso(path), path.lower()),
            reverse=True,
        )

    def _settings_normalized_columns(self, payload: dict[str, Any]) -> dict[str, Any]:
        llm_payload = payload.get("llm", {}) if isinstance(payload, dict) else {}
        tts_payload = payload.get("tts", {}) if isinstance(payload, dict) else {}

        llm_provider_configs = llm_payload.get("provider_configs", []) if isinstance(llm_payload, dict) else []
        tts_provider_configs = tts_payload.get("provider_configs", []) if isinstance(tts_payload, dict) else []

        return {
            "llm_default_model": str(llm_payload.get("default_model") or "").strip(),
            "openai_audio_endpoint": str(tts_payload.get("openai_audio_endpoint") or "").strip(),
            "llm_provider_count": len([cfg for cfg in llm_provider_configs if isinstance(cfg, dict)]),
            "tts_provider_count": len([cfg for cfg in tts_provider_configs if isinstance(cfg, dict)]),
        }

    def save_app_settings(self, payload: dict[str, Any], version: int = 1):
        self._ensure_ready()
        if not isinstance(payload, dict):
            return

        payload_json = _json_dumps(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        normalized = self._settings_normalized_columns(payload)
        saved_at = _utc_now_iso()

        with self._lock, self._connection() as connection:
            current_row = connection.execute(
                "SELECT payload_hash FROM app_settings_current WHERE singleton_id = 1"
            ).fetchone()

            if current_row is None or str(current_row["payload_hash"] or "") != payload_hash:
                connection.execute(
                    """
                    INSERT INTO app_settings_history (
                        version,
                        payload_json,
                        payload_hash,
                        llm_default_model,
                        openai_audio_endpoint,
                        llm_provider_count,
                        tts_provider_count,
                        saved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version,
                        payload_json,
                        payload_hash,
                        normalized["llm_default_model"],
                        normalized["openai_audio_endpoint"],
                        normalized["llm_provider_count"],
                        normalized["tts_provider_count"],
                        saved_at,
                    ),
                )

            connection.execute(
                """
                INSERT INTO app_settings_current (
                    singleton_id,
                    version,
                    payload_json,
                    payload_hash,
                    llm_default_model,
                    openai_audio_endpoint,
                    llm_provider_count,
                    tts_provider_count,
                    saved_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    version = excluded.version,
                    payload_json = excluded.payload_json,
                    payload_hash = excluded.payload_hash,
                    llm_default_model = excluded.llm_default_model,
                    openai_audio_endpoint = excluded.openai_audio_endpoint,
                    llm_provider_count = excluded.llm_provider_count,
                    tts_provider_count = excluded.tts_provider_count,
                    saved_at = excluded.saved_at
                """,
                (
                    version,
                    payload_json,
                    payload_hash,
                    normalized["llm_default_model"],
                    normalized["openai_audio_endpoint"],
                    normalized["llm_provider_count"],
                    normalized["tts_provider_count"],
                    saved_at,
                ),
            )

    def load_latest_app_settings(self) -> dict[str, Any]:
        self._ensure_ready()

        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM app_settings_current WHERE singleton_id = 1"
            ).fetchone()

        if row is None:
            return {}

        try:
            payload = json.loads(str(row["payload_json"] or ""))
        except json.JSONDecodeError:
            return {}

        return payload if isinstance(payload, dict) else {}

    def _ensure_session_row(self, connection: sqlite3.Connection, session_name: str, session_path: str):
        connection.execute(
            """
            INSERT INTO sessions (session_name, session_path, indexed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_name) DO NOTHING
            """,
            (session_name, session_path, _utc_now_iso()),
        )

    def update_session_sentence_index(
        self,
        session_name: str,
        sentences: list[dict[str, Any]],
        sentences_path: str,
    ):
        self._ensure_ready()
        if not session_name:
            return

        sentence_count = len(sentences)
        generated_count = sum(1 for item in sentences if str(item.get("tts_generated") or "").lower() == "yes")
        summary_text = self._summarize_sentences(sentences)
        sentences_hash = _json_sha256(sentences)
        updated_at = _utc_now_iso()

        with self._lock, self._connection() as connection:
            session_row = connection.execute(
                "SELECT session_path FROM sessions WHERE session_name = ?",
                (session_name,),
            ).fetchone()
            session_path = (
                str(session_row["session_path"])
                if session_row and session_row["session_path"]
                else os.path.join(self.outputs_root, session_name)
            )

            self._ensure_session_row(connection, session_name, session_path)

            connection.execute(
                """
                INSERT INTO session_payload_index (
                    session_name,
                    sentences_path,
                    sentences_count,
                    generated_sentences,
                    sentences_hash,
                    summary_text,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_name) DO UPDATE SET
                    sentences_path = excluded.sentences_path,
                    sentences_count = excluded.sentences_count,
                    generated_sentences = excluded.generated_sentences,
                    sentences_hash = excluded.sentences_hash,
                    summary_text = excluded.summary_text,
                    updated_at = excluded.updated_at
                """,
                (
                    session_name,
                    sentences_path,
                    sentence_count,
                    generated_count,
                    sentences_hash,
                    summary_text,
                    updated_at,
                ),
            )

            progress = (generated_count / sentence_count) * 100 if sentence_count > 0 else 0.0
            connection.execute(
                """
                UPDATE sessions
                SET generated_sentences = ?,
                    total_sentences = ?,
                    sentences_hash = ?,
                    progress_percent = ?,
                    indexed_at = ?
                WHERE session_name = ?
                """,
                (
                    generated_count,
                    sentence_count,
                    sentences_hash,
                    progress,
                    updated_at,
                    session_name,
                ),
            )
            self._refresh_session_audio_summary(connection, session_name, session_path)

    def update_session_speech_blocks_index(
        self,
        session_name: str,
        speech_blocks: list[dict[str, Any]],
        speech_blocks_path: str,
    ):
        self._ensure_ready()
        if not session_name:
            return

        speech_blocks_count = len(speech_blocks)
        speech_blocks_hash = _json_sha256(speech_blocks)
        updated_at = _utc_now_iso()

        with self._lock, self._connection() as connection:
            session_row = connection.execute(
                "SELECT session_path FROM sessions WHERE session_name = ?",
                (session_name,),
            ).fetchone()
            session_path = (
                str(session_row["session_path"])
                if session_row and session_row["session_path"]
                else os.path.join(self.outputs_root, session_name)
            )

            self._ensure_session_row(connection, session_name, session_path)

            connection.execute(
                """
                INSERT INTO session_payload_index (
                    session_name,
                    speech_blocks_path,
                    speech_blocks_count,
                    speech_blocks_hash,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_name) DO UPDATE SET
                    speech_blocks_path = excluded.speech_blocks_path,
                    speech_blocks_count = excluded.speech_blocks_count,
                    speech_blocks_hash = excluded.speech_blocks_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    session_name,
                    speech_blocks_path,
                    speech_blocks_count,
                    speech_blocks_hash,
                    updated_at,
                ),
            )

            connection.execute(
                """
                UPDATE sessions
                SET speech_blocks_hash = ?,
                    indexed_at = ?
                WHERE session_name = ?
                """,
                (speech_blocks_hash, updated_at, session_name),
            )

    def _refresh_session_artifact_summary(self, connection: sqlite3.Connection, session_name: str):
        artifact_row = connection.execute(
            """
            SELECT
                COUNT(*) AS artifact_count,
                SUM(CASE WHEN role LIKE 'final_video%' AND is_current = 1 THEN 1 ELSE 0 END) AS final_current_count
            FROM dubbing_artifacts
            WHERE run_id IN (
                SELECT run_id FROM dubbing_runs WHERE session_name = ?
            )
            """,
            (session_name,),
        ).fetchone()

        run_row = connection.execute(
            """
            SELECT status
            FROM dubbing_runs
            WHERE session_name = ?
            ORDER BY active DESC, updated_at DESC
            LIMIT 1
            """,
            (session_name,),
        ).fetchone()

        artifact_count = _safe_int(artifact_row["artifact_count"], 0) if artifact_row else 0
        has_final_output = 1 if artifact_row and _safe_int(artifact_row["final_current_count"], 0) > 0 else 0
        dubbing_status = str(run_row["status"] or "") if run_row else ""
        final_output_status = "available" if has_final_output else "missing"

        session_row = connection.execute(
            """
            SELECT generated_sentences, total_sentences, trashed_at
            FROM sessions
            WHERE session_name = ?
            """,
            (session_name,),
        ).fetchone()

        if session_row is None:
            return

        derived_status = self._derive_session_status(
            generated_sentences=_safe_int(session_row["generated_sentences"], 0),
            total_sentences=_safe_int(session_row["total_sentences"], 0),
            has_final_output=bool(has_final_output),
            trashed_at=str(session_row["trashed_at"] or "") or None,
        )

        connection.execute(
            """
            UPDATE sessions
            SET artifact_count = ?,
                has_final_output = ?,
                final_output_status = ?,
                dubbing_status = ?,
                status = ?,
                indexed_at = ?
            WHERE session_name = ?
            """,
            (
                artifact_count,
                has_final_output,
                final_output_status,
                dubbing_status,
                derived_status,
                _utc_now_iso(),
                session_name,
            ),
        )

    def _session_audio_base_dirs(
        self,
        connection: sqlite3.Connection,
        session_name: str,
        session_path: str,
    ) -> list[str]:
        candidates = [
            session_path,
            os.path.join(session_path, DUBBING_STAGING_DIRNAME),
        ]

        run_rows = connection.execute(
            """
            SELECT run_dir
            FROM dubbing_runs
            WHERE session_name = ?
            ORDER BY active DESC, updated_at DESC
            """,
            (session_name,),
        ).fetchall()
        for row in run_rows:
            run_dir = str(row["run_dir"] or "").strip()
            if run_dir:
                candidates.append(run_dir)

        runs_root = os.path.join(session_path, DUBBING_RUNS_DIR)
        if os.path.isdir(runs_root):
            try:
                for name in os.listdir(runs_root):
                    candidates.append(os.path.join(runs_root, name))
            except OSError:
                pass

        unique_dirs: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = os.path.abspath(str(candidate or "").strip())
            if not normalized or normalized in seen or not os.path.isdir(normalized):
                continue
            seen.add(normalized)
            unique_dirs.append(normalized)
        return unique_dirs

    def _scan_sentence_wavs_dir(self, wavs_dir: str, session_name: str) -> tuple[list[str], int]:
        if not os.path.isdir(wavs_dir):
            return [], 0

        pattern = re.compile(
            rf"^{re.escape(session_name)}_sentence_(?P<number>.+)\.wav$",
            re.IGNORECASE,
        )
        sentence_numbers: list[str] = []
        size_bytes = 0
        try:
            for file_name in os.listdir(wavs_dir):
                file_path = os.path.join(wavs_dir, file_name)
                if not os.path.isfile(file_path):
                    continue
                match = pattern.match(file_name)
                if not match:
                    continue
                sentence_numbers.append(match.group("number"))
                size_bytes += _file_size(file_path)
        except OSError:
            return [], 0
        return _dedupe_sentence_numbers(sentence_numbers), size_bytes

    def _scan_source_audio_version(
        self,
        connection: sqlite3.Connection,
        session_name: str,
        session_path: str,
        total_sentences: int,
    ) -> dict[str, Any] | None:
        sentence_numbers: list[str] = []
        size_bytes = 0
        seen_dirs: set[str] = set()
        for base_dir in self._session_audio_base_dirs(connection, session_name, session_path):
            wavs_dir = os.path.join(base_dir, SENTENCE_WAVS_DIRNAME)
            normalized = os.path.abspath(wavs_dir)
            if normalized in seen_dirs:
                continue
            seen_dirs.add(normalized)
            numbers, dir_size = self._scan_sentence_wavs_dir(wavs_dir, session_name)
            sentence_numbers.extend(numbers)
            size_bytes += dir_size

        sentence_count = len(_dedupe_sentence_numbers(sentence_numbers))
        if sentence_count <= 0 and total_sentences <= 0:
            return None

        return {
            "variant_id": SOURCE_AUDIO_VERSION_ID,
            "kind": "source",
            "label": "Original",
            "model_name": "",
            "settings_hash": "",
            "sentence_count": sentence_count,
            "total_sentences": total_sentences,
            "size_bytes": size_bytes,
            "relative_dir": SENTENCE_WAVS_DIRNAME,
            "partial": 1 if total_sentences and sentence_count < total_sentences else 0,
            "created_at": "",
            "updated_at": _utc_now_iso(),
        }

    def _scan_rvc_audio_versions(
        self,
        connection: sqlite3.Connection,
        session_name: str,
        session_path: str,
        total_sentences: int,
    ) -> list[dict[str, Any]]:
        try:
            from . import audio_variant_handler
        except Exception as error:
            logging.warning("Could not import audio variant handler for session indexing: %s", error)
            return []

        merged: dict[str, dict[str, Any]] = {}
        for base_dir in self._session_audio_base_dirs(connection, session_name, session_path):
            variants_dir = os.path.join(base_dir, AUDIO_VARIANTS_DIRNAME)
            if not os.path.isdir(variants_dir):
                continue

            for record in audio_variant_handler.list_rvc_variants(base_dir, session_name, prune_missing=False):
                variant_id = str(record.get("id") or "").strip()
                if not variant_id:
                    continue

                wavs_dir = audio_variant_handler.variant_wavs_dir(base_dir, variant_id)
                sentence_numbers = list(record.get("sentence_numbers") or [])
                size_bytes = _walk_directory_size(wavs_dir)
                relative_dir = _relative_path(wavs_dir, session_path)
                existing = merged.get(variant_id)
                if existing is None:
                    merged[variant_id] = {
                        "variant_id": variant_id,
                        "kind": str(record.get("kind") or "rvc"),
                        "label": str(record.get("label") or variant_id),
                        "model_name": str(record.get("model_name") or ""),
                        "settings_hash": str(record.get("settings_hash") or ""),
                        "sentence_numbers": list(sentence_numbers),
                        "sentence_count": len(_dedupe_sentence_numbers(sentence_numbers)),
                        "total_sentences": total_sentences,
                        "size_bytes": size_bytes,
                        "relative_dir": relative_dir,
                        "partial": 0,
                        "created_at": str(record.get("created_at") or ""),
                        "updated_at": str(record.get("updated_at") or _utc_now_iso()),
                    }
                    continue

                existing["sentence_numbers"] = _dedupe_sentence_numbers(
                    list(existing.get("sentence_numbers") or []) + sentence_numbers
                )
                existing["sentence_count"] = len(existing["sentence_numbers"])
                existing["size_bytes"] = int(existing.get("size_bytes") or 0) + size_bytes

        versions: list[dict[str, Any]] = []
        for record in merged.values():
            sentence_count = int(record.get("sentence_count") or 0)
            record["partial"] = 1 if total_sentences and sentence_count < total_sentences else 0
            record.pop("sentence_numbers", None)
            versions.append(record)
        return sorted(versions, key=lambda item: (str(item.get("kind") or ""), str(item.get("label") or "")))

    @staticmethod
    def _audio_version_summary(versions: list[dict[str, Any]]) -> str:
        if not versions:
            return "No audio"

        rvc_versions = [item for item in versions if str(item.get("kind") or "").lower() == "rvc"]
        if not rvc_versions:
            return "Original"

        partial_count = sum(1 for item in rvc_versions if _safe_int(item.get("partial"), 0) > 0)
        rvc_text = f"{len(rvc_versions)} RVC" if len(rvc_versions) != 1 else "1 RVC"
        summary = f"Original + {rvc_text}"
        if partial_count:
            summary += f" ({partial_count} partial)"
        return summary

    def _refresh_session_audio_summary(
        self,
        connection: sqlite3.Connection,
        session_name: str,
        session_path: str,
    ):
        payload_row = connection.execute(
            """
            SELECT sentences_count, generated_sentences
            FROM session_payload_index
            WHERE session_name = ?
            """,
            (session_name,),
        ).fetchone()
        session_row = connection.execute(
            """
            SELECT total_sentences, generated_sentences
            FROM sessions
            WHERE session_name = ?
            """,
            (session_name,),
        ).fetchone()

        total_sentences = (
            _safe_int(payload_row["sentences_count"], 0)
            if payload_row
            else _safe_int(session_row["total_sentences"], 0) if session_row else 0
        )
        source_version = self._scan_source_audio_version(connection, session_name, session_path, total_sentences)
        versions = []
        if source_version is not None:
            versions.append(source_version)
        versions.extend(self._scan_rvc_audio_versions(connection, session_name, session_path, total_sentences))

        now_iso = _utc_now_iso()
        connection.execute("DELETE FROM session_audio_versions WHERE session_name = ?", (session_name,))
        for version in versions:
            connection.execute(
                """
                INSERT INTO session_audio_versions (
                    session_name,
                    variant_id,
                    kind,
                    label,
                    model_name,
                    settings_hash,
                    sentence_count,
                    total_sentences,
                    size_bytes,
                    relative_dir,
                    partial,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_name,
                    str(version.get("variant_id") or ""),
                    str(version.get("kind") or ""),
                    str(version.get("label") or ""),
                    str(version.get("model_name") or ""),
                    str(version.get("settings_hash") or ""),
                    _safe_int(version.get("sentence_count"), 0),
                    _safe_int(version.get("total_sentences"), total_sentences),
                    _safe_int(version.get("size_bytes"), 0),
                    str(version.get("relative_dir") or ""),
                    1 if _safe_int(version.get("partial"), 0) > 0 else 0,
                    str(version.get("created_at") or ""),
                    str(version.get("updated_at") or now_iso),
                ),
            )

        connection.execute(
            """
            UPDATE sessions
            SET audio_version_count = ?,
                audio_version_size_bytes = ?,
                audio_version_summary = ?,
                indexed_at = ?
            WHERE session_name = ?
            """,
            (
                len(versions),
                sum(_safe_int(version.get("size_bytes"), 0) for version in versions),
                self._audio_version_summary(versions),
                now_iso,
                session_name,
            ),
        )

    def refresh_session_audio_summary(self, session_name: str):
        self._ensure_ready()
        if not session_name:
            return

        session_path = os.path.join(self.outputs_root, session_name)
        with self._lock, self._connection() as connection:
            self._ensure_session_row(connection, session_name, session_path)
            self._refresh_session_audio_summary(connection, session_name, session_path)

    def save_session_config_snapshot(
        self,
        session_name: str,
        payload: dict[str, Any],
        version: int = 1,
        session_path: str | None = None,
        config_modified_at: str | None = None,
    ):
        self._ensure_ready()
        if not session_name or not isinstance(payload, dict):
            return

        resolved_session_path = os.path.abspath(session_path or os.path.join(self.outputs_root, session_name))
        payload_json = _json_dumps(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        indexed_at = _utc_now_iso()

        source_path = str(payload.get("source_file_path") or "").strip()
        source_display_path = _source_display_path_from_payload(payload, source_path)
        source_display_name = _source_display_name_from_path(source_display_path)
        original_source_file_path = str(payload.get("original_source_file_path") or "").strip()
        if not original_source_file_path:
            original_source_file_path = source_display_path or source_path
        source_type = _source_type_from_path(source_path)
        tts_payload = payload.get("tts", {}) if isinstance(payload.get("tts"), dict) else {}
        tts_service = str(tts_payload.get("service") or "").strip()
        language = str(tts_payload.get("language") or "").strip()
        dubbing_mode = 1 if _is_dubbing_mode(source_path) else 0

        with self._lock, self._connection() as connection:
            self._ensure_session_row(connection, session_name, resolved_session_path)

            latest_snapshot_row = connection.execute(
                """
                SELECT payload_hash
                FROM session_config_snapshots
                WHERE session_name = ?
                ORDER BY indexed_at DESC, id DESC
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()
            session_timestamp_row = connection.execute(
                "SELECT config_modified_at FROM sessions WHERE session_name = ?",
                (session_name,),
            ).fetchone()
            existing_modified_at = (
                str(session_timestamp_row["config_modified_at"] or "")
                if session_timestamp_row
                else ""
            )
            payload_changed = (
                latest_snapshot_row is None
                or str(latest_snapshot_row["payload_hash"] or "") != payload_hash
            )
            resolved_modified_at = (
                str(config_modified_at or "").strip()
                or (indexed_at if payload_changed else existing_modified_at)
                or indexed_at
            )

            payload_index_row = connection.execute(
                """
                SELECT generated_sentences, sentences_count, sentences_hash
                FROM session_payload_index
                WHERE session_name = ?
                """,
                (session_name,),
            ).fetchone()

            generated_sentences = _safe_int(payload_index_row["generated_sentences"], 0) if payload_index_row else 0
            total_sentences = _safe_int(payload_index_row["sentences_count"], 0) if payload_index_row else 0
            progress_percent = (generated_sentences / total_sentences) * 100 if total_sentences > 0 else 0.0
            sentences_hash = str(payload_index_row["sentences_hash"] or "") if payload_index_row else ""
            session_size_bytes = _walk_directory_size(resolved_session_path)

            final_row = connection.execute(
                """
                SELECT has_final_output, trashed_at
                FROM sessions
                WHERE session_name = ?
                """,
                (session_name,),
            ).fetchone()
            has_final_output = bool(final_row and _safe_int(final_row["has_final_output"], 0) > 0)
            trashed_at = str(final_row["trashed_at"] or "") if final_row else ""

            status = self._derive_session_status(
                generated_sentences=generated_sentences,
                total_sentences=total_sentences,
                has_final_output=has_final_output,
                trashed_at=trashed_at or None,
            )

            if payload_changed:
                connection.execute(
                    """
                    INSERT INTO session_config_snapshots (
                        session_name,
                        version,
                        payload_json,
                        payload_hash,
                        source_type,
                        source_path,
                        tts_service,
                        language,
                        dubbing_mode,
                        progress_percent,
                        session_size_bytes,
                        status,
                        indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_name,
                        version,
                        payload_json,
                        payload_hash,
                        source_type,
                        source_path,
                        tts_service,
                        language,
                        dubbing_mode,
                        progress_percent,
                        session_size_bytes,
                        status,
                        indexed_at,
                    ),
                )

            connection.execute(
                """
                UPDATE sessions
                SET session_path = ?,
                    source_type = ?,
                    source_path = ?,
                    source_display_name = ?,
                    source_display_path = ?,
                    original_source_file_path = ?,
                    tts_service = ?,
                    language = ?,
                    dubbing_mode = ?,
                    generated_sentences = ?,
                    total_sentences = ?,
                    sentences_hash = ?,
                    progress_percent = ?,
                    session_size_bytes = ?,
                    status = ?,
                    config_modified_at = ?,
                    indexed_at = ?
                WHERE session_name = ?
                """,
                (
                    resolved_session_path,
                    source_type,
                    source_path,
                    source_display_name,
                    source_display_path,
                    original_source_file_path,
                    tts_service,
                    language,
                    dubbing_mode,
                    generated_sentences,
                    total_sentences,
                    sentences_hash,
                    progress_percent,
                    session_size_bytes,
                    status,
                    resolved_modified_at,
                    indexed_at,
                    session_name,
                ),
            )

            self._refresh_session_artifact_summary(connection, session_name)
            self._refresh_session_audio_summary(connection, session_name, resolved_session_path)

    def load_latest_session_config_snapshot(self, session_name: str) -> dict[str, Any]:
        self._ensure_ready()
        if not session_name:
            return {}

        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM session_config_snapshots
                WHERE session_name = ?
                ORDER BY indexed_at DESC, id DESC
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()

        if row is None:
            return {}

        try:
            payload = json.loads(str(row["payload_json"] or ""))
        except json.JSONDecodeError:
            return {}

        return payload if isinstance(payload, dict) else {}

    def _upsert_dubbing_step(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        step_key: str,
        status: str,
        detail: str = "",
    ):
        now_iso = _utc_now_iso()
        started_at = now_iso if status in {"running", "completed", "failed"} else None
        finished_at = now_iso if status in {"completed", "failed"} else None

        connection.execute(
            """
            INSERT INTO dubbing_steps (
                run_id,
                step_key,
                status,
                started_at,
                finished_at,
                detail
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, step_key) DO UPDATE SET
                status = excluded.status,
                started_at = COALESCE(dubbing_steps.started_at, excluded.started_at),
                finished_at = excluded.finished_at,
                detail = excluded.detail
            """,
            (
                run_id,
                step_key,
                status,
                started_at,
                finished_at,
                detail,
            ),
        )

    def create_dubbing_run(
        self,
        session_name: str,
        source_video_path: str = "",
        source_srt_path: str = "",
        settings_snapshot: dict[str, Any] | None = None,
        set_active: bool = True,
        legacy: bool = False,
        run_id: str | None = None,
        run_dir: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_ready()
        if not session_name:
            raise ValueError("session_name is required")

        created_at = _utc_now_iso()
        resolved_run_id = run_id or f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        default_run_dir = os.path.join(self.outputs_root, session_name, "dubbing", "runs", resolved_run_id)
        resolved_run_dir = os.path.abspath(run_dir or default_run_dir)
        os.makedirs(resolved_run_dir, exist_ok=True)

        settings_payload = _sanitize_dubbing_run_snapshot(settings_snapshot)
        settings_json = _json_dumps(settings_payload) if settings_payload else ""

        with self._lock, self._connection() as connection:
            session_path = os.path.join(self.outputs_root, session_name)
            self._ensure_session_row(connection, session_name, session_path)

            if set_active:
                connection.execute(
                    "UPDATE dubbing_runs SET active = 0 WHERE session_name = ?",
                    (session_name,),
                )

            connection.execute(
                """
                INSERT INTO dubbing_runs (
                    run_id,
                    session_name,
                    run_dir,
                    source_video_path,
                    source_srt_path,
                    settings_snapshot_json,
                    active,
                    status,
                    legacy,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    session_name = excluded.session_name,
                    run_dir = excluded.run_dir,
                    source_video_path = excluded.source_video_path,
                    source_srt_path = excluded.source_srt_path,
                    settings_snapshot_json = excluded.settings_snapshot_json,
                    active = excluded.active,
                    status = excluded.status,
                    legacy = excluded.legacy,
                    updated_at = excluded.updated_at
                """,
                (
                    resolved_run_id,
                    session_name,
                    resolved_run_dir,
                    source_video_path,
                    source_srt_path,
                    settings_json,
                    1 if set_active else 0,
                    "legacy_imported" if legacy else "created",
                    1 if legacy else 0,
                    created_at,
                    created_at,
                ),
            )

            for step_key in DUBBING_STEPS:
                self._upsert_dubbing_step(connection, resolved_run_id, step_key, "pending")

            connection.execute(
                """
                UPDATE sessions
                SET dubbing_status = ?, indexed_at = ?
                WHERE session_name = ?
                """,
                (
                    "legacy_imported" if legacy else "created",
                    created_at,
                    session_name,
                ),
            )

        return {
            "run_id": resolved_run_id,
            "session_name": session_name,
            "run_dir": resolved_run_dir,
            "source_video_path": source_video_path,
            "source_srt_path": source_srt_path,
            "active": bool(set_active),
            "legacy": bool(legacy),
        }

    def get_active_dubbing_run(self, session_name: str) -> dict[str, Any] | None:
        self._ensure_ready()
        if not session_name:
            return None

        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM dubbing_runs
                WHERE session_name = ? AND active = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()

        if row is None:
            return None

        return dict(row)

    def get_dubbing_run(self, run_id: str) -> dict[str, Any] | None:
        self._ensure_ready()
        if not run_id:
            return None

        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM dubbing_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()

        return dict(row) if row else None

    def get_dubbing_steps(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_ready()
        if not run_id:
            return []

        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT step_key, status, started_at, finished_at, detail
                FROM dubbing_steps
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def set_active_dubbing_run(self, session_name: str, run_id: str):
        self._ensure_ready()
        if not session_name or not run_id:
            return

        now_iso = _utc_now_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE dubbing_runs SET active = 0 WHERE session_name = ?",
                (session_name,),
            )
            connection.execute(
                """
                UPDATE dubbing_runs
                SET active = 1,
                    updated_at = ?
                WHERE session_name = ? AND run_id = ?
                """,
                (now_iso, session_name, run_id),
            )

    def update_dubbing_run_status(self, run_id: str, status: str):
        self._ensure_ready()
        if not run_id:
            return

        now_iso = _utc_now_iso()
        with self._lock, self._connection() as connection:
            run_row = connection.execute(
                "SELECT session_name FROM dubbing_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                return

            session_name = str(run_row["session_name"])
            connection.execute(
                """
                UPDATE dubbing_runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (status, now_iso, run_id),
            )
            connection.execute(
                """
                UPDATE sessions
                SET dubbing_status = ?, indexed_at = ?
                WHERE session_name = ?
                """,
                (status, now_iso, session_name),
            )

    def record_dubbing_llm_usage(
        self,
        run_id: str,
        stage_key: str,
        cost: float = 0.0,
        response_count: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_ready()
        if not run_id:
            return {}

        normalized_stage = str(stage_key or "").strip()
        if not normalized_stage:
            return {}

        normalized_cost = max(0.0, _safe_float(cost, 0.0))
        normalized_response_count = max(0, _safe_int(response_count, 0))
        if normalized_cost == 0.0 and normalized_response_count == 0 and not metadata:
            return {}

        now_iso = _utc_now_iso()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT llm_usage_json
                FROM dubbing_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return {}

            merged = merge_dubbing_llm_usage(
                str(row["llm_usage_json"] or ""),
                normalized_stage,
                cost=normalized_cost,
                response_count=normalized_response_count,
                metadata=metadata,
            )
            connection.execute(
                """
                UPDATE dubbing_runs
                SET llm_cost_total = ?,
                    llm_response_count = ?,
                    llm_usage_json = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    merged["total_cost"],
                    merged["response_count"],
                    _json_dumps(merged["usage"]),
                    now_iso,
                    run_id,
                ),
            )

        return merged

    def record_dubbing_step(self, run_id: str, step_key: str, status: str, detail: str = ""):
        self._ensure_ready()
        if not run_id or not step_key:
            return

        now_iso = _utc_now_iso()
        with self._lock, self._connection() as connection:
            run_row = connection.execute(
                "SELECT session_name FROM dubbing_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                return

            self._upsert_dubbing_step(connection, run_id, step_key, status, detail)
            connection.execute(
                "UPDATE dubbing_runs SET updated_at = ? WHERE run_id = ?",
                (now_iso, run_id),
            )

    def register_dubbing_artifact(
        self,
        run_id: str,
        role: str,
        path: str,
        is_current: bool = True,
        content_hash: str | None = None,
    ):
        self._ensure_ready()
        if not run_id or not role or not path:
            return

        resolved_path = os.path.abspath(path)
        now_iso = _utc_now_iso()
        size_bytes: int | None = None
        modified_at: str | None = None
        resolved_hash = content_hash or ""

        if os.path.exists(resolved_path):
            try:
                size_bytes = os.path.getsize(resolved_path)
            except OSError:
                size_bytes = None

            try:
                modified_at = datetime.datetime.fromtimestamp(
                    os.path.getmtime(resolved_path),
                    tz=datetime.timezone.utc,
                ).isoformat(timespec="seconds")
            except OSError:
                modified_at = None

            if not resolved_hash and os.path.isfile(resolved_path):
                try:
                    resolved_hash = _file_sha256(resolved_path)
                except OSError:
                    resolved_hash = ""

        with self._lock, self._connection() as connection:
            run_row = connection.execute(
                "SELECT session_name FROM dubbing_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                return

            session_name = str(run_row["session_name"])

            if is_current:
                connection.execute(
                    """
                    UPDATE dubbing_artifacts
                    SET is_current = 0
                    WHERE run_id = ? AND role = ?
                    """,
                    (run_id, role),
                )

            connection.execute(
                """
                INSERT INTO dubbing_artifacts (
                    run_id,
                    role,
                    path,
                    size_bytes,
                    modified_at,
                    content_hash,
                    is_current,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, role, path) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    modified_at = excluded.modified_at,
                    content_hash = excluded.content_hash,
                    is_current = excluded.is_current,
                    created_at = excluded.created_at
                """,
                (
                    run_id,
                    role,
                    resolved_path,
                    size_bytes,
                    modified_at,
                    resolved_hash,
                    1 if is_current else 0,
                    now_iso,
                ),
            )

            connection.execute(
                "UPDATE dubbing_runs SET updated_at = ? WHERE run_id = ?",
                (now_iso, run_id),
            )

            self._refresh_session_artifact_summary(connection, session_name)

    def get_active_dubbing_artifact(self, session_name: str, roles: list[str]) -> str:
        self._ensure_ready()
        if not session_name or not roles:
            return ""

        normalized_roles = [str(role or "").strip() for role in roles if str(role or "").strip()]
        if not normalized_roles:
            return ""

        placeholders = ", ".join("?" for _ in normalized_roles)
        order_cases = " ".join(
            f"WHEN ? THEN {index}" for index, _ in enumerate(normalized_roles)
        )
        order_params = normalized_roles

        with self._lock, self._connection() as connection:
            active_run = connection.execute(
                """
                SELECT run_id
                FROM dubbing_runs
                WHERE session_name = ?
                ORDER BY active DESC, updated_at DESC
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()
            if active_run is None:
                return ""

            run_id = str(active_run["run_id"])
            row = connection.execute(
                f"""
                SELECT path, role
                FROM dubbing_artifacts
                WHERE run_id = ?
                  AND role IN ({placeholders})
                  AND is_current = 1
                ORDER BY CASE role {order_cases} ELSE 999 END,
                         COALESCE(modified_at, created_at) DESC
                LIMIT 1
                """,
                [run_id, *normalized_roles, *order_params],
            ).fetchone()

        if row is None:
            return ""

        candidate_path = str(row["path"] or "")
        if candidate_path and os.path.exists(candidate_path):
            return candidate_path
        return ""

    def list_dubbing_runs(self, session_name: str) -> list[dict[str, Any]]:
        self._ensure_ready()
        if not session_name:
            return []

        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM dubbing_runs
                WHERE session_name = ?
                ORDER BY active DESC, updated_at DESC
                """,
                (session_name,),
            ).fetchall()

        return [dict(row) for row in rows]

    def list_dubbing_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_ready()
        if not run_id:
            return []

        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM dubbing_artifacts
                WHERE run_id = ?
                ORDER BY role ASC, is_current DESC, COALESCE(modified_at, created_at) DESC
                """,
                (run_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def _discover_legacy_artifacts(self, session_path: str) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {
            "transcribed_srt": [],
            "corrected_srt": [],
            "translated_srt": [],
            "equalized_srt": [],
            "speech_blocks": [],
            "synced_video": [],
            "final_video": [],
            "sentence_wavs_dir": [],
            "video_source": [],
            "audio_source": [],
        }

        if not os.path.isdir(session_path):
            return grouped

        for root, dirs, files in os.walk(session_path):
            for dir_name in dirs:
                if dir_name.lower() == "sentence_wavs":
                    grouped["sentence_wavs_dir"].append(os.path.join(root, dir_name))

            for file_name in files:
                lower_name = file_name.lower()
                path = os.path.join(root, file_name)

                if lower_name.endswith("_speech_blocks.json"):
                    grouped["speech_blocks"].append(path)
                    continue

                if lower_name.endswith(".srt"):
                    if lower_name.endswith("_equalized.srt"):
                        grouped["equalized_srt"].append(path)
                    elif "_translated" in lower_name:
                        grouped["translated_srt"].append(path)
                    elif "_corrected" in lower_name:
                        grouped["corrected_srt"].append(path)
                    else:
                        grouped["transcribed_srt"].append(path)
                    continue

                stem, ext = os.path.splitext(lower_name)
                if ext in SOURCE_VIDEO_EXTENSIONS:
                    if stem.startswith("final_output") or "_final" in stem:
                        grouped["final_video"].append(path)
                    elif "_synced" in stem:
                        grouped["synced_video"].append(path)
                    else:
                        grouped["video_source"].append(path)
                elif ext in SOURCE_AUDIO_EXTENSIONS and not _is_generated_audio_artifact(stem, root):
                    grouped.setdefault("audio_source", []).append(path)

        return grouped

    def import_legacy_session_run(self, session_name: str, session_path: str) -> dict[str, Any] | None:
        self._ensure_ready()
        if not session_name or not os.path.isdir(session_path):
            return None

        discovered = self._discover_legacy_artifacts(session_path)
        has_any_artifacts = any(discovered[key] for key in discovered)
        if not has_any_artifacts:
            return None

        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT run_id, run_dir
                FROM dubbing_runs
                WHERE session_name = ? AND legacy = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()

        if row is None:
            legacy_suffix = _sanitize_for_path_segment(session_name).lower()
            legacy_run_id = f"legacy_{legacy_suffix}"
            legacy_run_dir = os.path.join(session_path, "_dubbing_staging")
            if not os.path.isdir(legacy_run_dir):
                legacy_run_dir = session_path

            discovered_media_source = (
                discovered["video_source"][0]
                if discovered["video_source"]
                else (discovered["audio_source"][0] if discovered["audio_source"] else "")
            )
            run_info = self.create_dubbing_run(
                session_name=session_name,
                source_video_path=discovered_media_source,
                source_srt_path=discovered["transcribed_srt"][0] if discovered["transcribed_srt"] else "",
                settings_snapshot={},
                set_active=False,
                legacy=True,
                run_id=legacy_run_id,
                run_dir=legacy_run_dir,
            )
            legacy_run_id = run_info["run_id"]
        else:
            legacy_run_id = str(row["run_id"])

        for role, paths in discovered.items():
            for artifact_path in paths:
                self.register_dubbing_artifact(
                    legacy_run_id,
                    role,
                    artifact_path,
                    is_current=True,
                )

        if discovered["transcribed_srt"]:
            self.record_dubbing_step(legacy_run_id, "transcribe", "completed")
        if discovered["corrected_srt"]:
            self.record_dubbing_step(legacy_run_id, "correct", "completed")
        if discovered["translated_srt"]:
            self.record_dubbing_step(legacy_run_id, "translate", "completed")
        if discovered["speech_blocks"]:
            self.record_dubbing_step(legacy_run_id, "speech_blocks", "completed")
        if discovered["sentence_wavs_dir"]:
            self.record_dubbing_step(legacy_run_id, "tts_generation", "completed")
        if discovered["synced_video"]:
            self.record_dubbing_step(legacy_run_id, "sync", "completed")
        if discovered["equalized_srt"]:
            self.record_dubbing_step(legacy_run_id, "equalize", "completed")
        if discovered["final_video"]:
            self.record_dubbing_step(legacy_run_id, "render", "completed")

        self.update_dubbing_run_status(legacy_run_id, "legacy_imported")

        with self._lock, self._connection() as connection:
            active_row = connection.execute(
                """
                SELECT run_id
                FROM dubbing_runs
                WHERE session_name = ? AND active = 1
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()
            if active_row is None:
                connection.execute(
                    "UPDATE dubbing_runs SET active = 1 WHERE run_id = ?",
                    (legacy_run_id,),
                )

        return self.get_dubbing_run(legacy_run_id)

    def reindex_session(self, session_name: str) -> dict[str, Any] | None:
        self._ensure_ready()
        if not session_name:
            return None

        session_path = os.path.join(self.outputs_root, session_name)
        if not os.path.isdir(session_path):
            return None

        sentences, sentences_path = self._read_sentences_from_disk(session_name, session_path)
        if sentences_path:
            self.update_session_sentence_index(session_name, sentences, sentences_path)

        speech_blocks, speech_blocks_path = self._read_speech_blocks_from_disk(session_path)
        if speech_blocks_path:
            self.update_session_speech_blocks_index(session_name, speech_blocks, speech_blocks_path)

        session_config_path = os.path.join(session_path, SESSION_CONFIG_FILENAME)
        payload = self._load_wrapped_or_plain_payload(session_config_path, "state")
        config_modified_at = _file_modified_at_iso(session_config_path)
        if not payload:
            discovered_sources = self._discover_session_source_files(session_path)
            if discovered_sources:
                discovered_source = discovered_sources[0]
                payload = {
                    "session_name": session_name,
                    "source_file_path": discovered_source,
                    "source_display_path": discovered_source,
                    "original_source_file_path": discovered_source,
                }
                config_modified_at = _file_modified_at_iso(discovered_source)
        if not config_modified_at:
            config_modified_at = _file_modified_at_iso(session_path)
        self.save_session_config_snapshot(
            session_name=session_name,
            payload=payload,
            version=1,
            session_path=session_path,
            config_modified_at=config_modified_at,
        )

        self.import_legacy_session_run(session_name, session_path)
        self.refresh_session_audio_summary(session_name)
        preview = self.get_session_preview(session_name)
        return preview

    def reindex_all_sessions(self) -> list[dict[str, Any]]:
        self._ensure_ready()
        self._ensure_outputs_root()

        indexed: list[dict[str, Any]] = []
        seen_session_names: set[str] = set()
        for name in os.listdir(self.outputs_root):
            if name == TRASH_DIRNAME:
                continue
            path = os.path.join(self.outputs_root, name)
            if not os.path.isdir(path):
                continue

            seen_session_names.add(name)
            preview = self.reindex_session(name)
            if preview:
                indexed.append(preview)

        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT session_name, trashed_at FROM sessions"
            ).fetchall()

            for row in rows:
                session_name = str(row["session_name"])
                trashed_at = str(row["trashed_at"] or "")
                if session_name in seen_session_names:
                    continue
                if trashed_at:
                    continue

                connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'missing', indexed_at = ?
                    WHERE session_name = ?
                    """,
                    (_utc_now_iso(), session_name),
                )

        return indexed

    def list_sessions(self, search_query: str = "", include_trashed: bool = False) -> list[dict[str, Any]]:
        self._ensure_ready()
        normalized_query = _normalize_for_search(search_query)
        filters: list[str] = []
        params: list[Any] = []

        if not include_trashed:
            filters.append("(trashed_at IS NULL OR trashed_at = '')")

        if normalized_query:
            like_query = f"%{normalized_query}%"
            filters.append(
                "(" + " OR ".join(
                    [
                        "LOWER(session_name) LIKE ?",
                        "LOWER(source_type) LIKE ?",
                        "LOWER(source_path) LIKE ?",
                        "LOWER(source_display_name) LIKE ?",
                        "LOWER(source_display_path) LIKE ?",
                        "LOWER(status) LIKE ?",
                        "LOWER(final_output_status) LIKE ?",
                        "LOWER(dubbing_status) LIKE ?",
                        "LOWER(audio_version_summary) LIKE ?",
                    ]
                ) + ")"
            )
            params.extend([like_query] * 9)

        where_clause = ""
        if filters:
            where_clause = "WHERE " + " AND ".join(filters)

        query = f"""
            SELECT *
            FROM sessions
            {where_clause}
            ORDER BY
                CASE WHEN trashed_at IS NULL OR trashed_at = '' THEN 0 ELSE 1 END,
                COALESCE(config_modified_at, indexed_at) DESC,
                session_name ASC
        """

        with self._lock, self._connection() as connection:
            rows = connection.execute(query, params).fetchall()

        return [dict(row) for row in rows]

    def _resolve_source_path(self, path: str, session_name: str) -> str | None:
        if not path:
            return None

        session_dir = os.path.join(self.outputs_root, session_name)
        basename = os.path.basename(path)
        session_file_path = os.path.join(session_dir, basename)
        if os.path.isfile(session_file_path):
            return os.path.abspath(session_file_path)

        abs_path = os.path.abspath(path)
        if os.path.isfile(abs_path):
            return abs_path

        return None

    def list_reusable_sources(self, limit: int = 300, include_missing: bool = False) -> list[dict[str, Any]]:
        self._ensure_ready()
        safe_limit = max(1, int(limit or 1))

        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    session_name,
                    session_path,
                    source_path,
                    source_display_path,
                    source_display_name,
                    source_type,
                    original_source_file_path,
                    dubbing_mode,
                    config_modified_at,
                    indexed_at
                FROM sessions
                WHERE (trashed_at IS NULL OR trashed_at = '')
                ORDER BY COALESCE(config_modified_at, indexed_at) DESC, session_name ASC
                """
            ).fetchall()

        seen_paths: set[str] = set()
        reusable_sources: list[dict[str, Any]] = []

        for row in rows:
            session_name = str(row["session_name"] or "")
            source_path = str(row["source_path"] or "").strip()
            display_path = str(row["source_display_path"] or "").strip() or source_path
            original_path = str(row["original_source_file_path"] or "").strip()
            session_path = os.path.join(self.outputs_root, session_name)
            if not os.path.isdir(session_path):
                session_path = str(row["session_path"] or "")

            tracked_paths = {
                os.path.normcase(os.path.abspath(path))
                for path in (source_path, display_path, original_path)
                if path
            }
            candidate_specs: list[tuple[str, str]] = []
            if original_path:
                candidate_specs.append((original_path, "original"))
            if display_path:
                candidate_specs.append((display_path, "display"))
            if source_path:
                candidate_specs.append((source_path, "active"))
            candidate_specs.extend(
                (path, "discovered")
                for path in self._discover_session_source_files(session_path)
                if os.path.normcase(os.path.abspath(path)) not in tracked_paths
            )

            for candidate_path, role in candidate_specs:
                resolved_path = self._resolve_source_path(candidate_path, session_name)
                if not resolved_path and include_missing:
                    resolved_path = os.path.abspath(candidate_path)
                if not resolved_path:
                    continue

                normalized_path = os.path.normcase(os.path.abspath(resolved_path))
                if normalized_path in seen_paths:
                    continue
                seen_paths.add(normalized_path)

                name = os.path.basename(resolved_path)
                tracked_paths_differ = len(tracked_paths) > 1
                if role == "original" and tracked_paths_differ:
                    name = f"{name} (Original)"
                elif tracked_paths_differ and (
                    role == "active"
                    or (
                        role == "display"
                        and source_path
                        and os.path.normcase(os.path.basename(source_path))
                        == os.path.normcase(os.path.basename(resolved_path))
                    )
                ):
                    suffix = "Edited" if "edited" in name.lower() else "Processed"
                    name = f"{name} ({suffix})"

                reusable_sources.append(
                    {
                        "source_path": resolved_path,
                        "name": name,
                        "internal_source_path": resolved_path,
                        "source_type": _source_type_from_path(resolved_path),
                        "session_name": session_name,
                        "dubbing_mode": 1 if _is_dubbing_mode(resolved_path) else 0,
                        "last_used_at": str(row["config_modified_at"] or row["indexed_at"] or ""),
                        "exists": os.path.isfile(resolved_path),
                    }
                )
                if len(reusable_sources) >= safe_limit:
                    return reusable_sources

        return reusable_sources

    def get_session_preview(self, session_name: str) -> dict[str, Any]:
        self._ensure_ready()
        if not session_name:
            return {}

        with self._lock, self._connection() as connection:
            session_row = connection.execute(
                "SELECT * FROM sessions WHERE session_name = ?",
                (session_name,),
            ).fetchone()
            if session_row is None:
                return {}

            payload_row = connection.execute(
                "SELECT * FROM session_payload_index WHERE session_name = ?",
                (session_name,),
            ).fetchone()
            config_row = connection.execute(
                """
                SELECT payload_json
                FROM session_config_snapshots
                WHERE session_name = ?
                ORDER BY indexed_at DESC, id DESC
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()

            run_rows = connection.execute(
                """
                SELECT *
                FROM dubbing_runs
                WHERE session_name = ?
                ORDER BY active DESC, updated_at DESC
                """,
                (session_name,),
            ).fetchall()

            runs_payload: list[dict[str, Any]] = []
            for run_row in run_rows:
                run_dict = dict(run_row)
                run_id = str(run_dict["run_id"])

                step_rows = connection.execute(
                    """
                    SELECT step_key, status, started_at, finished_at, detail
                    FROM dubbing_steps
                    WHERE run_id = ?
                    ORDER BY id ASC
                    """,
                    (run_id,),
                ).fetchall()
                artifact_rows = connection.execute(
                    """
                    SELECT role, path, size_bytes, modified_at, is_current
                    FROM dubbing_artifacts
                    WHERE run_id = ?
                    ORDER BY role ASC, is_current DESC, COALESCE(modified_at, created_at) DESC
                    """,
                    (run_id,),
                ).fetchall()

                run_dict["steps"] = [dict(step_row) for step_row in step_rows]
                run_dict["artifacts"] = [dict(artifact_row) for artifact_row in artifact_rows]
                runs_payload.append(run_dict)

            audio_version_rows = connection.execute(
                """
                SELECT *
                FROM session_audio_versions
                WHERE session_name = ?
                ORDER BY
                    CASE WHEN variant_id = ? THEN 0 ELSE 1 END,
                    kind ASC,
                    updated_at DESC,
                    label ASC
                """,
                (session_name, SOURCE_AUDIO_VERSION_ID),
            ).fetchall()

            trash_row = connection.execute(
                """
                SELECT *
                FROM trash_entries
                WHERE session_name = ?
                    AND restored_at IS NULL
                    AND deleted_at IS NULL
                ORDER BY moved_at DESC
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()

        config_payload: dict[str, Any] = {}
        if config_row is not None:
            try:
                parsed_config = json.loads(str(config_row["payload_json"] or ""))
                if isinstance(parsed_config, dict):
                    config_payload = _redact_api_keys(parsed_config)
            except json.JSONDecodeError:
                config_payload = {}

        session_dict = dict(session_row)
        payload_dict = dict(payload_row) if payload_row else {}

        return {
            "session": session_dict,
            "payload_index": payload_dict,
            "config": config_payload,
            "runs": runs_payload,
            "audio_versions": [dict(row) for row in audio_version_rows],
            "trash_entry": dict(trash_row) if trash_row else {},
        }

    def get_trash_entries(self) -> list[dict[str, Any]]:
        self._ensure_ready()
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM trash_entries
                WHERE restored_at IS NULL AND deleted_at IS NULL
                ORDER BY moved_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_session_trashed(
        self,
        session_name: str,
        original_path: str,
        trash_path: str,
        retention_days: int = 30,
    ):
        self._ensure_ready()
        moved_at = _utc_now_iso()
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=max(1, _safe_int(retention_days, 30)))
        ).isoformat(timespec="seconds")

        with self._lock, self._connection() as connection:
            session_row = connection.execute(
                "SELECT session_path FROM sessions WHERE session_name = ?",
                (session_name,),
            ).fetchone()
            session_path = (
                str(session_row["session_path"])
                if session_row and session_row["session_path"]
                else original_path
            )
            self._ensure_session_row(connection, session_name, session_path)

            connection.execute(
                """
                UPDATE sessions
                SET trashed_at = ?,
                    trash_path = ?,
                    status = 'trashed',
                    indexed_at = ?
                WHERE session_name = ?
                """,
                (moved_at, trash_path, moved_at, session_name),
            )
            connection.execute(
                """
                INSERT INTO trash_entries (
                    session_name,
                    original_path,
                    trash_path,
                    moved_at,
                    expires_at,
                    restored_at,
                    deleted_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(trash_path) DO UPDATE SET
                    session_name = excluded.session_name,
                    original_path = excluded.original_path,
                    moved_at = excluded.moved_at,
                    expires_at = excluded.expires_at,
                    restored_at = NULL,
                    deleted_at = NULL
                """,
                (
                    session_name,
                    original_path,
                    trash_path,
                    moved_at,
                    expires_at,
                ),
            )

    def mark_session_restored(self, session_name: str, restored_path: str, trash_path: str = ""):
        self._ensure_ready()
        now_iso = _utc_now_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET session_path = ?,
                    trashed_at = NULL,
                    trash_path = NULL,
                    status = 'idle',
                    indexed_at = ?
                WHERE session_name = ?
                """,
                (restored_path, now_iso, session_name),
            )
            if trash_path:
                connection.execute(
                    """
                    UPDATE trash_entries
                    SET restored_at = ?
                    WHERE trash_path = ? AND restored_at IS NULL AND deleted_at IS NULL
                    """,
                    (now_iso, trash_path),
                )
            else:
                connection.execute(
                    """
                    UPDATE trash_entries
                    SET restored_at = ?
                    WHERE session_name = ? AND restored_at IS NULL AND deleted_at IS NULL
                    """,
                    (now_iso, session_name),
                )

    def mark_trash_path_deleted(self, trash_path: str):
        self._ensure_ready()
        now_iso = _utc_now_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE trash_entries
                SET deleted_at = ?
                WHERE trash_path = ?
                """,
                (now_iso, trash_path),
            )
            connection.execute(
                """
                DELETE FROM sessions
                WHERE trash_path = ?
                    AND trashed_at IS NOT NULL
                """,
                (trash_path,),
            )


DEFAULT_HANDLER = StateDBHandler()


def get_db_path() -> str:
    return DEFAULT_HANDLER.db_path


def initialize_database():
    DEFAULT_HANDLER.initialize_database()


def save_app_settings(payload: dict[str, Any], version: int = 1):
    DEFAULT_HANDLER.save_app_settings(payload, version=version)


def load_latest_app_settings() -> dict[str, Any]:
    return DEFAULT_HANDLER.load_latest_app_settings()


def save_session_config_snapshot(
    session_name: str,
    payload: dict[str, Any],
    version: int = 1,
    session_path: str | None = None,
    config_modified_at: str | None = None,
):
    DEFAULT_HANDLER.save_session_config_snapshot(
        session_name=session_name,
        payload=payload,
        version=version,
        session_path=session_path,
        config_modified_at=config_modified_at,
    )


def load_latest_session_config_snapshot(session_name: str) -> dict[str, Any]:
    return DEFAULT_HANDLER.load_latest_session_config_snapshot(session_name)


def update_session_sentence_index(session_name: str, sentences: list[dict[str, Any]], sentences_path: str):
    DEFAULT_HANDLER.update_session_sentence_index(session_name, sentences, sentences_path)


def update_session_speech_blocks_index(
    session_name: str,
    speech_blocks: list[dict[str, Any]],
    speech_blocks_path: str,
):
    DEFAULT_HANDLER.update_session_speech_blocks_index(session_name, speech_blocks, speech_blocks_path)


def create_dubbing_run(
    session_name: str,
    source_video_path: str = "",
    source_srt_path: str = "",
    settings_snapshot: dict[str, Any] | None = None,
    set_active: bool = True,
    legacy: bool = False,
    run_id: str | None = None,
    run_dir: str | None = None,
) -> dict[str, Any]:
    return DEFAULT_HANDLER.create_dubbing_run(
        session_name=session_name,
        source_video_path=source_video_path,
        source_srt_path=source_srt_path,
        settings_snapshot=settings_snapshot,
        set_active=set_active,
        legacy=legacy,
        run_id=run_id,
        run_dir=run_dir,
    )


def get_active_dubbing_run(session_name: str) -> dict[str, Any] | None:
    return DEFAULT_HANDLER.get_active_dubbing_run(session_name)


def get_dubbing_run(run_id: str) -> dict[str, Any] | None:
    return DEFAULT_HANDLER.get_dubbing_run(run_id)


def get_dubbing_steps(run_id: str) -> list[dict[str, Any]]:
    return DEFAULT_HANDLER.get_dubbing_steps(run_id)


def set_active_dubbing_run(session_name: str, run_id: str):
    DEFAULT_HANDLER.set_active_dubbing_run(session_name, run_id)


def update_dubbing_run_status(run_id: str, status: str):
    DEFAULT_HANDLER.update_dubbing_run_status(run_id, status)


def record_dubbing_llm_usage(
    run_id: str,
    stage_key: str,
    cost: float = 0.0,
    response_count: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return DEFAULT_HANDLER.record_dubbing_llm_usage(
        run_id=run_id,
        stage_key=stage_key,
        cost=cost,
        response_count=response_count,
        metadata=metadata,
    )


def record_dubbing_step(run_id: str, step_key: str, status: str, detail: str = ""):
    DEFAULT_HANDLER.record_dubbing_step(run_id, step_key, status, detail)


def register_dubbing_artifact(
    run_id: str,
    role: str,
    path: str,
    is_current: bool = True,
    content_hash: str | None = None,
):
    DEFAULT_HANDLER.register_dubbing_artifact(
        run_id=run_id,
        role=role,
        path=path,
        is_current=is_current,
        content_hash=content_hash,
    )


def get_active_dubbing_artifact(session_name: str, roles: list[str]) -> str:
    return DEFAULT_HANDLER.get_active_dubbing_artifact(session_name, roles)


def list_dubbing_runs(session_name: str) -> list[dict[str, Any]]:
    return DEFAULT_HANDLER.list_dubbing_runs(session_name)


def list_dubbing_artifacts(run_id: str) -> list[dict[str, Any]]:
    return DEFAULT_HANDLER.list_dubbing_artifacts(run_id)


def import_legacy_session_run(session_name: str, session_path: str) -> dict[str, Any] | None:
    return DEFAULT_HANDLER.import_legacy_session_run(session_name, session_path)


def reindex_session(session_name: str) -> dict[str, Any] | None:
    return DEFAULT_HANDLER.reindex_session(session_name)


def reindex_all_sessions() -> list[dict[str, Any]]:
    return DEFAULT_HANDLER.reindex_all_sessions()


def list_sessions(search_query: str = "", include_trashed: bool = False) -> list[dict[str, Any]]:
    return DEFAULT_HANDLER.list_sessions(search_query=search_query, include_trashed=include_trashed)


def list_reusable_sources(limit: int = 300, include_missing: bool = False) -> list[dict[str, Any]]:
    return DEFAULT_HANDLER.list_reusable_sources(limit=limit, include_missing=include_missing)


def get_session_preview(session_name: str) -> dict[str, Any]:
    return DEFAULT_HANDLER.get_session_preview(session_name)


def refresh_session_audio_summary(session_name: str):
    DEFAULT_HANDLER.refresh_session_audio_summary(session_name)


def mark_session_trashed(
    session_name: str,
    original_path: str,
    trash_path: str,
    retention_days: int = 30,
):
    DEFAULT_HANDLER.mark_session_trashed(
        session_name=session_name,
        original_path=original_path,
        trash_path=trash_path,
        retention_days=retention_days,
    )


def mark_session_restored(session_name: str, restored_path: str, trash_path: str = ""):
    DEFAULT_HANDLER.mark_session_restored(
        session_name=session_name,
        restored_path=restored_path,
        trash_path=trash_path,
    )


def mark_trash_path_deleted(trash_path: str):
    DEFAULT_HANDLER.mark_trash_path_deleted(trash_path=trash_path)


def get_trash_entries() -> list[dict[str, Any]]:
    return DEFAULT_HANDLER.get_trash_entries()
