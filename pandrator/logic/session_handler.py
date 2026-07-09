import json
import logging
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
from typing import Any, Dict, List

from ..app_state import AppState
from . import state_db_handler
from .source_media import MEDIA_SOURCE_EXTENSIONS, SOURCE_FILE_EXTENSIONS, VIDEO_SOURCE_EXTENSIONS

OUTPUTS_DIR = "Outputs"
DUBBING_STAGING_DIR = "_dubbing_staging"
DUBBING_RUNS_DIR = os.path.join("dubbing", "runs")
FINAL_OUTPUT_DIR = "final"
TRASH_DIR_NAME = ".trash"
DEFAULT_TRASH_RETENTION_DAYS = 30
SESSION_CONFIG_FILENAME = "session_config.json"
SESSION_CONFIG_VERSION = 2

VIDEO_FILE_EXTENSIONS = VIDEO_SOURCE_EXTENSIONS
SPEECH_BLOCKS_SUFFIX = "_speech_blocks.json"

_NON_ARTIFACT_FILENAMES = {
    SESSION_CONFIG_FILENAME.lower(),
    "metadata.json",
}

_FILE_IO_LOCK = threading.RLock()


def _write_json_atomic(file_path: str, payload: Any):
    """Writes JSON to disk atomically to avoid partial writes."""
    directory = os.path.dirname(file_path)
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


def get_session_path(session_name: str) -> str:
    """Constructs the full path for a given session name."""
    return os.path.join(OUTPUTS_DIR, session_name)


def get_dubbing_staging_path(session_name: str) -> str:
    """Constructs the path for temporary dubbing artifacts within a session."""
    return os.path.join(get_session_path(session_name), DUBBING_STAGING_DIR)


def get_dubbing_runs_root_path(session_name: str) -> str:
    """Constructs the root path for structured dubbing runs in a session."""
    return os.path.join(get_session_path(session_name), DUBBING_RUNS_DIR)


def get_dubbing_run_path(session_name: str, run_id: str) -> str:
    """Constructs the path for a concrete dubbing run."""
    return os.path.join(get_dubbing_runs_root_path(session_name), run_id)


def get_session_final_output_path(session_name: str) -> str:
    """Constructs the path for final rendered videos for a session."""
    return os.path.join(get_session_path(session_name), FINAL_OUTPUT_DIR)


def get_trash_root_path() -> str:
    """Constructs the shared trash root inside Outputs."""
    return os.path.join(OUTPUTS_DIR, TRASH_DIR_NAME)


def build_session_config_payload(state: AppState) -> Dict[str, Any]:
    """Builds a JSON-serializable state payload for session config persistence."""
    payload: Dict[str, Any] = asdict(state)

    for transient_field in ("processed_sentences", "metadata", "raw_text"):
        payload.pop(transient_field, None)

    tts_payload = payload.get("tts")
    if isinstance(tts_payload, dict):
        tts_payload.pop("tts_models", None)
        tts_payload.pop("tts_speakers", None)
        tts_payload.pop("openai_audio_endpoints_json", None)
        tts_payload.pop("service_configs", None)
        tts_payload.pop("provider_configs", None)

    llm_payload = payload.get("llm")
    if isinstance(llm_payload, dict):
        llm_payload.pop("default_model", None)
        llm_payload.pop("provider_configs", None)
        llm_payload.pop("request_timeout_seconds", None)

    return payload


def save_session_config(session_name: str, state_payload: Dict[str, Any]):
    """Saves a session config payload to disk."""
    session_path = get_session_path(session_name)
    os.makedirs(session_path, exist_ok=True)
    config_file = os.path.join(session_path, SESSION_CONFIG_FILENAME)
    config_data = {
        "version": SESSION_CONFIG_VERSION,
        "state": state_payload,
    }
    should_write = True
    with _FILE_IO_LOCK:
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                should_write = json.load(f) != config_data
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        if should_write:
            _write_json_atomic(config_file, config_data)

    config_modified_at = ""
    try:
        config_modified_at = datetime.fromtimestamp(
            os.path.getmtime(config_file),
            tz=timezone.utc,
        ).isoformat(timespec="seconds")
    except OSError:
        pass

    try:
        state_db_handler.save_session_config_snapshot(
            session_name=session_name,
            payload=state_payload,
            version=SESSION_CONFIG_VERSION,
            session_path=session_path,
            config_modified_at=config_modified_at,
        )
    except Exception:
        pass


def load_session_config(session_name: str) -> Dict[str, Any]:
    """Loads a saved session config payload. Returns an empty dict when unavailable."""
    session_path = get_session_path(session_name)
    config_file = os.path.join(session_path, SESSION_CONFIG_FILENAME)
    try:
        with _FILE_IO_LOCK:
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        try:
            db_payload = state_db_handler.load_latest_session_config_snapshot(session_name)
            return db_payload if isinstance(db_payload, dict) else {}
        except Exception:
            return {}

    if isinstance(config_data, dict):
        if isinstance(config_data.get("state"), dict):
            state_payload = config_data["state"]
        else:
            state_payload = config_data

        return state_payload

    try:
        db_payload = state_db_handler.load_latest_session_config_snapshot(session_name)
        return db_payload if isinstance(db_payload, dict) else {}
    except Exception:
        return {}


def _discover_latest_file_by_extensions(session_name: str, extensions: set[str]) -> str | None:
    session_path = get_session_path(session_name)
    if not os.path.isdir(session_path):
        return None

    candidates: List[str] = []
    for name in os.listdir(session_path):
        full_path = os.path.join(session_path, name)
        if not os.path.isfile(full_path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in extensions:
            candidates.append(full_path)

    if not candidates:
        return None

    return max(candidates, key=lambda p: (os.path.getmtime(p), p.lower()))


def discover_source_file(session_name: str) -> str | None:
    """Finds the most recently updated source-like file in a session directory."""
    return _discover_latest_file_by_extensions(session_name, SOURCE_FILE_EXTENSIONS)


def discover_video_file(session_name: str) -> str | None:
    """Finds the most recently updated video file in a session directory."""
    try:
        run_video = state_db_handler.get_active_dubbing_artifact(
            session_name,
            roles=["video_source", "source_video", "synced_video", "final_video"],
        )
        if run_video and os.path.exists(run_video):
            return run_video
    except Exception:
        pass

    return _discover_latest_file_by_extensions(session_name, VIDEO_FILE_EXTENSIONS)


def discover_media_file(session_name: str) -> str | None:
    """Finds the most recently updated audio/video source file in a session directory."""
    try:
        run_media = state_db_handler.get_active_dubbing_artifact(
            session_name,
            roles=["media_source", "audio_source", "video_source", "source_audio", "source_video"],
        )
        if run_media and os.path.exists(run_media):
            return run_media
    except Exception:
        pass

    return _discover_latest_file_by_extensions(session_name, MEDIA_SOURCE_EXTENSIONS)


def discover_latest_speech_blocks_file(session_name: str) -> str | None:
    """Finds the most recently updated speech-blocks JSON file in a session directory."""
    try:
        active_path = state_db_handler.get_active_dubbing_artifact(
            session_name,
            roles=["speech_blocks"],
        )
        if active_path and os.path.exists(active_path):
            return active_path
    except Exception:
        pass

    session_path = get_session_path(session_name)
    candidates: List[str] = []
    search_paths = [session_path, get_dubbing_staging_path(session_name)]
    for search_path in search_paths:
        if not os.path.isdir(search_path):
            continue

        for name in os.listdir(search_path):
            if not name.lower().endswith(SPEECH_BLOCKS_SUFFIX):
                continue
            full_path = os.path.join(search_path, name)
            if os.path.isfile(full_path):
                candidates.append(full_path)

    if not candidates:
        return None

    return max(candidates, key=lambda p: (os.path.getmtime(p), p.lower()))


def _extract_speech_blocks(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        blocks = payload
    elif isinstance(payload, dict):
        blocks = None
        for key in ("speech_blocks", "blocks", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                blocks = value
                break
        if blocks is None:
            raise ValueError("Speech blocks payload does not contain a blocks list.")
    else:
        raise ValueError("Speech blocks payload is not a JSON object or list.")

    normalized: List[Dict[str, Any]] = []
    for idx, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise ValueError(f"Speech block #{idx} is not a JSON object.")
        normalized.append(block)
    return normalized


def load_speech_blocks_file(speech_blocks_file: str) -> List[Dict[str, Any]]:
    """Loads and validates speech blocks from a JSON file."""
    with open(speech_blocks_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return _extract_speech_blocks(payload)


def _normalize_subtitle_text(raw_text: Any) -> str:
    normalized = re.sub(r"\r\n?", "\n", str(raw_text or ""))
    normalized = re.sub(r"\s*\n+\s*", " ", normalized)
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    return normalized.strip()


def speech_blocks_to_sentences(speech_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Converts speech blocks into the sentence JSON structure used by Pandrator."""
    sentences: List[Dict[str, Any]] = []

    for idx, block in enumerate(speech_blocks, start=1):
        if not isinstance(block, dict):
            raise ValueError(f"Speech block #{idx} is not a JSON object.")

        sentence_number = block.get("number", idx)
        block_text = block.get("text")
        if block_text is None:
            raise ValueError(f"Speech block #{idx} does not contain a 'text' field.")

        normalized_block_text = _normalize_subtitle_text(block_text)

        sentences.append(
            {
                "sentence_number": str(sentence_number),
                "original_sentence": normalized_block_text,
                "tts_generated": "no",
            }
        )

    return sentences


def import_speech_blocks_to_session(session_name: str, speech_blocks_file: str) -> List[Dict[str, Any]]:
    """Converts speech blocks JSON to sentence JSON and persists it for a session."""
    speech_blocks = load_speech_blocks_file(speech_blocks_file)
    try:
        state_db_handler.update_session_speech_blocks_index(
            session_name=session_name,
            speech_blocks=speech_blocks,
            speech_blocks_path=speech_blocks_file,
        )
    except Exception:
        pass

    sentences = speech_blocks_to_sentences(speech_blocks)
    save_sentences(session_name, sentences)
    return sentences


def save_metadata(session_name: str, metadata: dict):
    """Saves metadata to metadata.json in the session directory."""
    session_path = get_session_path(session_name)
    os.makedirs(session_path, exist_ok=True)
    metadata_file = os.path.join(session_path, "metadata.json")
    with _FILE_IO_LOCK:
        _write_json_atomic(metadata_file, metadata)


def load_metadata(session_name: str) -> dict:
    """Loads metadata from metadata.json in the session directory."""
    session_path = get_session_path(session_name)
    metadata_file = os.path.join(session_path, "metadata.json")
    try:
        with _FILE_IO_LOCK:
            with open(metadata_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"title": "", "album": "", "artist": "", "genre": "", "language": ""}


def save_sentences(session_name: str, sentences: List[Dict[str, Any]]):
    """Saves the list of processed sentences to a JSON file."""
    session_path = get_session_path(session_name)
    os.makedirs(session_path, exist_ok=True)
    json_filename = os.path.join(session_path, f"{session_name}_sentences.json")
    with _FILE_IO_LOCK:
        _write_json_atomic(json_filename, sentences)

    try:
        state_db_handler.update_session_sentence_index(
            session_name=session_name,
            sentences=sentences,
            sentences_path=json_filename,
        )
    except Exception:
        pass


def load_sentences(session_name: str) -> List[Dict[str, Any]]:
    """Loads the list of processed sentences from a JSON file."""
    session_path = get_session_path(session_name)
    json_filename = os.path.join(session_path, f"{session_name}_sentences.json")
    try:
        with _FILE_IO_LOCK:
            with open(json_filename, "r", encoding="utf-8") as f:
                payload = json.load(f)
        if isinstance(payload, list):
            try:
                state_db_handler.update_session_sentence_index(
                    session_name=session_name,
                    sentences=[item for item in payload if isinstance(item, dict)],
                    sentences_path=json_filename,
                )
            except Exception:
                pass
            return payload
        return []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _sentences_file_has_entries(sentences_file: str) -> bool:
    try:
        with _FILE_IO_LOCK:
            with open(sentences_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return True

    return bool(payload) if isinstance(payload, list) else True


def _directory_has_artifacts(directory: str) -> bool:
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_name_lower = entry.name.lower()
                if entry.is_dir(follow_symlinks=False):
                    if entry_name_lower == "sentence_wavs":
                        try:
                            with os.scandir(entry.path) as wav_entries:
                                for _ in wav_entries:
                                    return True
                        except OSError:
                            return True
                        continue

                    return True

                return True
    except OSError:
        return True

    return False


def session_has_generated_artifacts(session_name: str) -> bool:
    """Checks whether a session directory contains generated artifacts."""
    session_path = get_session_path(session_name)
    if not os.path.isdir(session_path):
        return False

    sentence_wavs_dir = os.path.join(session_path, "Sentence_wavs")
    if os.path.isdir(sentence_wavs_dir):
        try:
            with os.scandir(sentence_wavs_dir) as wav_entries:
                for _ in wav_entries:
                    return True
        except OSError:
            return True

    with _FILE_IO_LOCK:
        for name in os.listdir(session_path):
            full_path = os.path.join(session_path, name)
            name_lower = name.lower()

            if name_lower in _NON_ARTIFACT_FILENAMES or name_lower == "sentence_wavs":
                continue

            if os.path.isdir(full_path):
                if name_lower == DUBBING_STAGING_DIR.lower():
                    if _directory_has_artifacts(full_path):
                        return True
                    continue
                return True

            if name_lower.endswith("_sentences.json"):
                if _sentences_file_has_entries(full_path):
                    return True
                continue

            if name_lower.endswith(("_speech_blocks.json", "_equalized.srt")):
                return True

            stem_lower, ext = os.path.splitext(name_lower)
            if (
                stem_lower.startswith("final_output")
                or "_synced" in stem_lower
                or stem_lower.endswith("_final")
                or "_final_" in stem_lower
            ):
                return True

            if ext == ".srt" and any(token in stem_lower for token in ("_translated", "_corrected", "_equalized")):
                return True

            if ext in SOURCE_FILE_EXTENSIONS:
                continue

            return True

    return False


def clear_session_contents(session_name: str):
    """Deletes all files and directories inside a session path."""
    session_path = get_session_path(session_name)
    with _FILE_IO_LOCK:
        if os.path.isdir(session_path):
            shutil.rmtree(session_path)
        os.makedirs(session_path, exist_ok=True)


def session_exists(session_name: str) -> bool:
    """Checks if a session directory exists."""
    session_path = get_session_path(session_name)
    return os.path.isdir(session_path)


def update_sentence(session_name: str, sentence_number: str, new_text: str) -> bool:
    """Updates the text of a specific sentence in the session's sentences file."""
    try:
        with _FILE_IO_LOCK:
            sentences = load_sentences(session_name)

            sentence_found = False
            for sentence in sentences:
                if str(sentence.get("sentence_number")) == str(sentence_number):
                    if "processed_sentence" in sentence and sentence["processed_sentence"] is not None:
                        sentence["processed_sentence"] = new_text
                    else:
                        sentence["original_sentence"] = new_text
                    sentence_found = True
                    break

            if sentence_found:
                save_sentences(session_name, sentences)
                logging.info(f"Updated sentence {sentence_number} in session '{session_name}'.")
                return True

            logging.warning(f"Sentence {sentence_number} not found in session '{session_name}'.")
            return False

    except Exception as e:
        logging.error(f"Error updating sentence {sentence_number} in session '{session_name}': {e}")
        return False


def update_sentence_marked_status(session_name: str, sentence_number: str, marked: bool) -> bool:
    """Updates the 'marked' status of a specific sentence."""
    try:
        with _FILE_IO_LOCK:
            sentences = load_sentences(session_name)

            sentence_found = False
            for sentence in sentences:
                if str(sentence.get("sentence_number")) == str(sentence_number):
                    sentence["marked"] = marked
                    sentence_found = True
                    break

            if sentence_found:
                save_sentences(session_name, sentences)
                logging.info(f"Set marked status of sentence {sentence_number} to {marked} in session '{session_name}'.")
                return True

            logging.warning(f"Sentence {sentence_number} not found for updating marked status in session '{session_name}'.")
            return False

    except Exception as e:
        logging.error(f"Error updating marked status for sentence {sentence_number} in session '{session_name}': {e}")
        return False


def remove_sentences(session_name: str, sentence_numbers_to_remove: list[str]) -> bool:
    """Removes sentences from the session file and re-numbers the rest."""
    try:
        with _FILE_IO_LOCK:
            sentences = load_sentences(session_name)

            numbers_to_remove_str = {str(n) for n in sentence_numbers_to_remove}
            updated_sentences = [
                sentence
                for sentence in sentences
                if str(sentence.get("sentence_number")) not in numbers_to_remove_str
            ]

            for i, sentence in enumerate(updated_sentences, start=1):
                sentence["sentence_number"] = str(i)

            save_sentences(session_name, updated_sentences)
            logging.info(
                f"Removed {len(sentence_numbers_to_remove)} sentences and re-numbered session '{session_name}'."
            )
            return True
    except Exception as e:
        logging.error(f"Error removing sentences from session '{session_name}': {e}")
        return False


def delete_session(session_name: str) -> bool:
    """Deletes a session directory and all its contents."""
    try:
        session_path = get_session_path(session_name)
        if os.path.isdir(session_path):
            shutil.rmtree(session_path)
            logging.info(f"Session '{session_name}' deleted successfully.")
            try:
                state_db_handler.reindex_all_sessions()
            except Exception:
                pass
            return True

        logging.warning(f"Session '{session_name}' not found for deletion.")
        return False
    except Exception as e:
        logging.error(f"Error deleting session '{session_name}': {e}")
        return False


def reindex_session(session_name: str) -> dict | None:
    """Reindexes a single session into SQLite state."""
    try:
        return state_db_handler.reindex_session(session_name)
    except Exception as e:
        logging.error("Could not reindex session '%s': %s", session_name, e)
        return None


def reindex_all_sessions() -> list[dict]:
    """Reindexes all on-disk sessions into SQLite state."""
    try:
        return state_db_handler.reindex_all_sessions()
    except Exception as e:
        logging.error("Could not reindex sessions: %s", e)
        return []


def list_indexed_sessions(search_query: str = "", include_trashed: bool = False) -> list[dict]:
    """Lists sessions from the SQLite state index."""
    try:
        return state_db_handler.list_sessions(search_query=search_query, include_trashed=include_trashed)
    except Exception as e:
        logging.error("Could not list indexed sessions: %s", e)
        return []


def list_reusable_sources(limit: int = 300, include_missing: bool = False) -> list[dict]:
    """Lists deduplicated source files that can be reused in new sessions."""
    try:
        return state_db_handler.list_reusable_sources(limit=limit, include_missing=include_missing)
    except Exception as e:
        logging.error("Could not list reusable sources: %s", e)
        return []


def get_session_index_preview(session_name: str) -> dict:
    """Returns a detailed preview payload for a session from SQLite state."""
    try:
        return state_db_handler.get_session_preview(session_name)
    except Exception as e:
        logging.error("Could not load session preview for '%s': %s", session_name, e)
        return {}


def _unique_destination_path(base_path: str) -> str:
    if not os.path.exists(base_path):
        return base_path

    parent = os.path.dirname(base_path)
    name = os.path.basename(base_path)
    counter = 1
    while True:
        candidate = os.path.join(parent, f"{name}_{counter}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def move_session_to_trash(session_name: str, retention_days: int = DEFAULT_TRASH_RETENTION_DAYS) -> tuple[bool, str]:
    """Moves a session directory to Outputs/.trash and records it in SQLite."""
    session_path = get_session_path(session_name)
    if not os.path.isdir(session_path):
        return False, f"Session '{session_name}' was not found."

    trash_root = get_trash_root_path()
    os.makedirs(trash_root, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    trash_name = f"{timestamp}_{session_name}"
    trash_destination = _unique_destination_path(os.path.join(trash_root, trash_name))

    try:
        shutil.move(session_path, trash_destination)
        state_db_handler.mark_session_trashed(
            session_name=session_name,
            original_path=session_path,
            trash_path=trash_destination,
            retention_days=retention_days,
        )
        return True, trash_destination
    except Exception as e:
        logging.error("Could not move session '%s' to trash: %s", session_name, e)
        return False, str(e)


def list_trashed_sessions() -> list[dict]:
    """Returns non-restored, non-deleted trash entries from SQLite."""
    try:
        return state_db_handler.get_trash_entries()
    except Exception as e:
        logging.error("Could not list trash entries: %s", e)
        return []


def _is_path_inside(child_path: str, parent_path: str) -> bool:
    try:
        child_abs = os.path.abspath(child_path)
        parent_abs = os.path.abspath(parent_path)
        return os.path.commonpath([child_abs, parent_abs]) == parent_abs
    except ValueError:
        return False


def restore_session_from_trash(session_name: str, trash_path: str = "") -> tuple[bool, str]:
    """Restores a trashed session back into Outputs."""
    trash_entries = list_trashed_sessions()
    normalized_trash_path = os.path.normcase(os.path.abspath(trash_path)) if trash_path else ""
    entry = None
    if normalized_trash_path:
        entry = next(
            (
                item
                for item in trash_entries
                if os.path.normcase(os.path.abspath(str(item.get("trash_path") or ""))) == normalized_trash_path
            ),
            None,
        )
    if entry is None:
        entry = next((item for item in trash_entries if item.get("session_name") == session_name), None)
    if entry is None:
        return False, f"Session '{session_name}' is not present in trash."

    session_name = str(entry.get("session_name") or session_name)
    trash_path = str(entry.get("trash_path") or "")
    if not trash_path or not os.path.exists(trash_path):
        try:
            state_db_handler.mark_trash_path_deleted(trash_path)
        except Exception:
            pass
        return False, f"Trash path for '{session_name}' does not exist anymore."

    restore_target = get_session_path(session_name)
    if os.path.exists(restore_target):
        return False, f"Cannot restore '{session_name}' because a session with that name already exists."

    try:
        os.makedirs(os.path.dirname(restore_target) or ".", exist_ok=True)
        shutil.move(trash_path, restore_target)
        state_db_handler.mark_session_restored(
            session_name=session_name,
            restored_path=restore_target,
            trash_path=trash_path,
        )
        state_db_handler.reindex_session(session_name)
        return True, restore_target
    except Exception as e:
        logging.error("Could not restore session '%s': %s", session_name, e)
        return False, str(e)


def permanently_delete_trashed_session(session_name: str = "", trash_path: str = "") -> tuple[bool, str]:
    """Permanently deletes a selected trash entry."""
    trash_entries = list_trashed_sessions()
    normalized_trash_path = os.path.normcase(os.path.abspath(trash_path)) if trash_path else ""
    entry = None
    if normalized_trash_path:
        entry = next(
            (
                item
                for item in trash_entries
                if os.path.normcase(os.path.abspath(str(item.get("trash_path") or ""))) == normalized_trash_path
            ),
            None,
        )
    if entry is None and session_name:
        entry = next((item for item in trash_entries if item.get("session_name") == session_name), None)

    if entry is None:
        return False, "The selected trash entry was not found."

    selected_trash_path = str(entry.get("trash_path") or "").strip()
    if not selected_trash_path:
        return False, "The selected trash entry has no filesystem path."

    trash_root = get_trash_root_path()
    if not _is_path_inside(selected_trash_path, trash_root):
        return False, f"Refusing to delete a path outside the trash folder: {selected_trash_path}"

    try:
        if os.path.isdir(selected_trash_path):
            shutil.rmtree(selected_trash_path)
        elif os.path.exists(selected_trash_path):
            os.remove(selected_trash_path)
        state_db_handler.mark_trash_path_deleted(selected_trash_path)
        return True, selected_trash_path
    except Exception as e:
        logging.error("Could not permanently delete trash path '%s': %s", selected_trash_path, e)
        return False, str(e)


def empty_expired_trash(retention_days: int = DEFAULT_TRASH_RETENTION_DAYS) -> tuple[int, list[str]]:
    """Deletes expired trash entries and returns count with removed paths."""
    now_utc = datetime.now(timezone.utc)
    removed_paths: list[str] = []
    removed_count = 0

    for entry in list_trashed_sessions():
        trash_path = str(entry.get("trash_path") or "")
        if not trash_path:
            continue

        expires_raw = str(entry.get("expires_at") or "")
        expires_at = None
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(expires_raw)
            except ValueError:
                expires_at = None

        if expires_at is None:
            moved_raw = str(entry.get("moved_at") or "")
            try:
                moved_at = datetime.fromisoformat(moved_raw)
                expires_at = moved_at + timedelta(days=max(1, int(retention_days)))
            except ValueError:
                expires_at = now_utc

        if expires_at > now_utc:
            continue

        try:
            if os.path.isdir(trash_path):
                shutil.rmtree(trash_path)
            elif os.path.exists(trash_path):
                os.remove(trash_path)
            state_db_handler.mark_trash_path_deleted(trash_path)
            removed_paths.append(trash_path)
            removed_count += 1
        except Exception as e:
            logging.error("Could not remove expired trash path '%s': %s", trash_path, e)

    return removed_count, removed_paths
