import json
import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import asdict
from typing import Any, Dict, List

from ..app_state import AppState

OUTPUTS_DIR = "Outputs"
DUBBING_STAGING_DIR = "_dubbing_staging"
SESSION_CONFIG_FILENAME = "session_config.json"
SESSION_CONFIG_VERSION = 1

SOURCE_FILE_EXTENSIONS = {
    ".txt",
    ".srt",
    ".pdf",
    ".epub",
    ".docx",
    ".mobi",
    ".mp4",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
}
VIDEO_FILE_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}
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
    config_file = os.path.join(session_path, SESSION_CONFIG_FILENAME)
    config_data = {
        "version": SESSION_CONFIG_VERSION,
        "state": state_payload,
    }
    with _FILE_IO_LOCK:
        _write_json_atomic(config_file, config_data)


def load_session_config(session_name: str) -> Dict[str, Any]:
    """Loads a saved session config payload. Returns an empty dict when unavailable."""
    session_path = get_session_path(session_name)
    config_file = os.path.join(session_path, SESSION_CONFIG_FILENAME)
    try:
        with _FILE_IO_LOCK:
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    if isinstance(config_data, dict):
        if isinstance(config_data.get("state"), dict):
            return config_data["state"]
        return config_data
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
    return _discover_latest_file_by_extensions(session_name, VIDEO_FILE_EXTENSIONS)


def discover_latest_speech_blocks_file(session_name: str) -> str | None:
    """Finds the most recently updated speech-blocks JSON file in a session directory."""
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
    sentences = speech_blocks_to_sentences(speech_blocks)
    save_sentences(session_name, sentences)
    return sentences


def save_metadata(session_name: str, metadata: dict):
    """Saves metadata to metadata.json in the session directory."""
    session_path = get_session_path(session_name)
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
    json_filename = os.path.join(session_path, f"{session_name}_sentences.json")
    with _FILE_IO_LOCK:
        _write_json_atomic(json_filename, sentences)


def load_sentences(session_name: str) -> List[Dict[str, Any]]:
    """Loads the list of processed sentences from a JSON file."""
    session_path = get_session_path(session_name)
    json_filename = os.path.join(session_path, f"{session_name}_sentences.json")
    try:
        with _FILE_IO_LOCK:
            with open(json_filename, "r", encoding="utf-8") as f:
                return json.load(f)
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
            if stem_lower.startswith("final_output") or "_synced" in stem_lower or stem_lower.endswith("_final"):
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
            return True

        logging.warning(f"Session '{session_name}' not found for deletion.")
        return False
    except Exception as e:
        logging.error(f"Error deleting session '{session_name}': {e}")
        return False
