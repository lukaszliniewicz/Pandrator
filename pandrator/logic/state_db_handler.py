import datetime
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


STATE_DB_FILENAME = "pandrator_state.sqlite3"
SCHEMA_VERSION = 1

OUTPUTS_DIRNAME = "Outputs"
TRASH_DIRNAME = ".trash"
SESSION_CONFIG_FILENAME = "session_config.json"

SOURCE_TEXT_EXTENSIONS = {".txt", ".pdf", ".epub", ".docx", ".mobi"}
SOURCE_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}

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


def _source_type_from_path(path: str) -> str:
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext in SOURCE_TEXT_EXTENSIONS:
        return "text"
    if ext == ".srt":
        return "srt"
    if ext in SOURCE_VIDEO_EXTENSIONS:
        return "video"
    if ext:
        return ext.lstrip(".")
    return "unknown"


def _is_dubbing_mode(source_path: str) -> bool:
    ext = os.path.splitext(str(source_path or ""))[1].lower()
    return ext == ".srt" or ext in SOURCE_VIDEO_EXTENSIONS


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
            if key_lower == "api_key":
                redacted[key] = "***redacted***" if value else ""
                continue
            redacted[key] = _redact_api_keys(value)
        return redacted

    if isinstance(payload, list):
        return [_redact_api_keys(item) for item in payload]

    return payload


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
                config_modified_at TEXT,
                indexed_at TEXT NOT NULL,
                trashed_at TEXT,
                trash_path TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_modified
                ON sessions(config_modified_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_search
                ON sessions(session_name, source_type, status, final_output_status, dubbing_status);

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

    def _initialize_connection(self, connection: sqlite3.Connection):
        connection.execute("PRAGMA journal_mode = WAL")
        version_row = connection.execute("PRAGMA user_version").fetchone()
        current_version = _safe_int(version_row[0], 0) if version_row else 0
        if current_version < SCHEMA_VERSION:
            self._create_schema(connection)
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

    def save_session_config_snapshot(
        self,
        session_name: str,
        payload: dict[str, Any],
        version: int = 1,
        session_path: str | None = None,
    ):
        self._ensure_ready()
        if not session_name or not isinstance(payload, dict):
            return

        resolved_session_path = os.path.abspath(session_path or os.path.join(self.outputs_root, session_name))
        payload_json = _json_dumps(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        indexed_at = _utc_now_iso()

        source_path = str(payload.get("source_file_path") or "").strip()
        source_type = _source_type_from_path(source_path)
        tts_payload = payload.get("tts", {}) if isinstance(payload.get("tts"), dict) else {}
        tts_service = str(tts_payload.get("service") or "").strip()
        language = str(tts_payload.get("language") or "").strip()
        dubbing_mode = 1 if _is_dubbing_mode(source_path) else 0

        with self._lock, self._connection() as connection:
            self._ensure_session_row(connection, session_name, resolved_session_path)

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
                    tts_service,
                    language,
                    dubbing_mode,
                    generated_sentences,
                    total_sentences,
                    sentences_hash,
                    progress_percent,
                    session_size_bytes,
                    status,
                    indexed_at,
                    indexed_at,
                    session_name,
                ),
            )

            self._refresh_session_artifact_summary(connection, session_name)

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

        settings_payload = settings_snapshot if isinstance(settings_snapshot, dict) else {}
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

            run_info = self.create_dubbing_run(
                session_name=session_name,
                source_video_path=discovered["video_source"][0] if discovered["video_source"] else "",
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
        self.save_session_config_snapshot(
            session_name=session_name,
            payload=payload,
            version=1,
            session_path=session_path,
        )

        self.import_legacy_session_run(session_name, session_path)
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
                        "LOWER(status) LIKE ?",
                        "LOWER(final_output_status) LIKE ?",
                        "LOWER(dubbing_status) LIKE ?",
                    ]
                ) + ")"
            )
            params.extend([like_query] * 6)

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

    def mark_session_restored(self, session_name: str, restored_path: str):
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
):
    DEFAULT_HANDLER.save_session_config_snapshot(
        session_name=session_name,
        payload=payload,
        version=version,
        session_path=session_path,
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


def set_active_dubbing_run(session_name: str, run_id: str):
    DEFAULT_HANDLER.set_active_dubbing_run(session_name, run_id)


def update_dubbing_run_status(run_id: str, status: str):
    DEFAULT_HANDLER.update_dubbing_run_status(run_id, status)


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


def get_session_preview(session_name: str) -> dict[str, Any]:
    return DEFAULT_HANDLER.get_session_preview(session_name)


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


def mark_session_restored(session_name: str, restored_path: str):
    DEFAULT_HANDLER.mark_session_restored(session_name=session_name, restored_path=restored_path)


def mark_trash_path_deleted(trash_path: str):
    DEFAULT_HANDLER.mark_trash_path_deleted(trash_path=trash_path)


def get_trash_entries() -> list[dict[str, Any]]:
    return DEFAULT_HANDLER.get_trash_entries()

