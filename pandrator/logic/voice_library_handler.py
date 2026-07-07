import copy
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
import wave
from datetime import datetime, timezone
from typing import Any


VOICE_LIBRARY_VERSION = 1
APP_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VOICE_LIBRARY_DIR = os.path.join(APP_ROOT_DIR, "tts_voices")
VOICE_LIBRARY_STORAGE_DIR = os.path.join(VOICE_LIBRARY_DIR, "library")
VOICE_LIBRARY_INDEX_FILE = os.path.join(VOICE_LIBRARY_DIR, "voice_library.json")
VOICE_LIBRARY_SEED_FILE = os.path.join(VOICE_LIBRARY_DIR, "voice_library_seed.json")

_FILE_IO_LOCK = threading.RLock()
_TRANSCRIPT_ENCODINGS = ("utf-8", "utf-8-sig", "cp1250", "cp1252", "latin-1")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_relative_path(relative_path: str) -> str:
    normalized = str(relative_path or "").replace("\\", "/").strip()
    normalized_parts: list[str] = []
    for part in normalized.split("/"):
        cleaned_part = part.strip()
        if not cleaned_part or cleaned_part in {".", ".."}:
            continue
        normalized_parts.append(cleaned_part)
    return "/".join(normalized_parts)


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


def _ensure_voice_library_dirs():
    os.makedirs(VOICE_LIBRARY_STORAGE_DIR, exist_ok=True)


def _default_payload() -> dict[str, Any]:
    return {
        "version": VOICE_LIBRARY_VERSION,
        "voices": [],
    }


def _coerce_text(value: Any, *, trim: bool = True) -> str:
    text = "" if value is None else str(value)
    return text.strip() if trim else text


def _normalize_language_code(value: Any) -> str:
    return _coerce_text(value).lower()


def _normalize_sample_record(sample_payload: Any) -> dict[str, Any] | None:
    if not isinstance(sample_payload, dict):
        return None

    sample_id = _coerce_text(sample_payload.get("id")) or f"sample_{uuid.uuid4().hex[:12]}"
    relative_path = _normalize_relative_path(_coerce_text(sample_payload.get("relative_path")))
    file_name = _coerce_text(sample_payload.get("file_name"))
    if not file_name and relative_path:
        file_name = os.path.basename(relative_path)

    try:
        duration_seconds = float(sample_payload.get("duration_seconds") or 0.0)
    except (TypeError, ValueError):
        duration_seconds = 0.0

    if duration_seconds < 0.0:
        duration_seconds = 0.0

    return {
        "id": sample_id,
        "file_name": file_name,
        "relative_path": relative_path,
        "duration_seconds": duration_seconds,
        "transcript": _coerce_text(sample_payload.get("transcript"), trim=False),
        "notes": _coerce_text(sample_payload.get("notes"), trim=False),
        "created_at": _coerce_text(sample_payload.get("created_at"), trim=False),
        "updated_at": _coerce_text(sample_payload.get("updated_at"), trim=False),
    }


def _normalize_voice_record(voice_payload: Any) -> dict[str, Any] | None:
    if not isinstance(voice_payload, dict):
        return None

    voice_id = _coerce_text(voice_payload.get("id")) or f"voice_{uuid.uuid4().hex[:12]}"
    voice_name = _coerce_text(voice_payload.get("name")) or voice_id
    samples_payload = voice_payload.get("samples")
    if not isinstance(samples_payload, list):
        samples_payload = []

    seen_sample_ids: set[str] = set()
    normalized_samples: list[dict[str, Any]] = []
    for sample_payload in samples_payload:
        normalized_sample = _normalize_sample_record(sample_payload)
        if not normalized_sample:
            continue

        sample_id = normalized_sample["id"]
        if sample_id in seen_sample_ids:
            normalized_sample["id"] = f"sample_{uuid.uuid4().hex[:12]}"
            sample_id = normalized_sample["id"]
        seen_sample_ids.add(sample_id)
        normalized_samples.append(normalized_sample)

    created_at = _coerce_text(voice_payload.get("created_at"), trim=False)
    updated_at = _coerce_text(voice_payload.get("updated_at"), trim=False)

    return {
        "id": voice_id,
        "name": voice_name,
        "notes": _coerce_text(voice_payload.get("notes"), trim=False),
        "prompt_text": _coerce_text(voice_payload.get("prompt_text"), trim=False),
        "language_code": _normalize_language_code(voice_payload.get("language_code")),
        "samples": normalized_samples,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _load_payload_unlocked() -> dict[str, Any]:
    _ensure_voice_library_dirs()

    try:
        with open(VOICE_LIBRARY_INDEX_FILE, "r", encoding="utf-8") as f:
            loaded_payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _default_payload()

    if not isinstance(loaded_payload, dict):
        return _default_payload()

    voices_payload = loaded_payload.get("voices")
    if not isinstance(voices_payload, list):
        voices_payload = []

    seen_voice_ids: set[str] = set()
    normalized_voices: list[dict[str, Any]] = []
    for voice_payload in voices_payload:
        normalized_voice = _normalize_voice_record(voice_payload)
        if not normalized_voice:
            continue

        voice_id = normalized_voice["id"]
        if voice_id in seen_voice_ids:
            normalized_voice["id"] = f"voice_{uuid.uuid4().hex[:12]}"
            voice_id = normalized_voice["id"]
        seen_voice_ids.add(voice_id)
        normalized_voices.append(normalized_voice)

    return {
        "version": VOICE_LIBRARY_VERSION,
        "voices": normalized_voices,
    }


def _save_payload_unlocked(payload: dict[str, Any]):
    wrapped_payload = {
        "version": VOICE_LIBRARY_VERSION,
        "voices": payload.get("voices", []),
    }
    _write_json_atomic(VOICE_LIBRARY_INDEX_FILE, wrapped_payload)


def _seed_payload_unlocked() -> dict[str, Any]:
    try:
        with open(VOICE_LIBRARY_SEED_FILE, "r", encoding="utf-8") as f:
            loaded_payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _default_payload()

    if not isinstance(loaded_payload, dict):
        return _default_payload()

    seed_voices_payload = loaded_payload.get("voices")
    if not isinstance(seed_voices_payload, list):
        return _default_payload()

    normalized_voices: list[dict[str, Any]] = []
    for voice_payload in seed_voices_payload:
        normalized_voice = _normalize_voice_record(voice_payload)
        if not normalized_voice:
            continue
        normalized_voice["created_at"] = _coerce_text(normalized_voice.get("created_at"), trim=False)
        normalized_voice["updated_at"] = _coerce_text(normalized_voice.get("updated_at"), trim=False)
        normalized_voices.append(normalized_voice)

    return {
        "version": VOICE_LIBRARY_VERSION,
        "voices": normalized_voices,
    }


def _has_seed_voice(payload: dict[str, Any], seed_voice: dict[str, Any]) -> bool:
    seed_voice_id = _coerce_text(seed_voice.get("id"))
    seed_name = _coerce_text(seed_voice.get("name")).lower()
    for voice in payload["voices"]:
        existing_id = _coerce_text(voice.get("id"))
        existing_name = _coerce_text(voice.get("name")).lower()
        if seed_voice_id and existing_id == seed_voice_id:
            return True
        if seed_name and existing_name == seed_name:
            return True
    return False


def _seed_voice_sample(
    voice: dict[str, Any],
    sample: dict[str, Any],
    timestamp: str,
) -> dict[str, Any] | None:
    source_path = resolve_sample_path(_normalize_relative_path(sample.get("relative_path")))
    if not source_path or not os.path.isfile(source_path):
        logging.debug("Bundled voice sample not found: %s", source_path)
        return None

    target_dir = _voice_storage_dir(voice["id"])
    os.makedirs(target_dir, exist_ok=True)

    source_basename = sample.get("file_name") or os.path.basename(source_path)
    target_basename = _build_unique_file_name(target_dir, source_basename)
    target_path = os.path.join(target_dir, target_basename)

    try:
        shutil.copy2(source_path, target_path)
    except OSError as exc:
        logging.warning("Unable to seed bundled voice sample '%s': %s", source_path, exc)
        return None

    return {
        "id": _coerce_text(sample.get("id")) or f"sample_{uuid.uuid4().hex[:12]}",
        "file_name": target_basename,
        "relative_path": _normalize_relative_path(
            os.path.relpath(target_path, VOICE_LIBRARY_DIR)
        ),
        "duration_seconds": _wav_duration_seconds(target_path),
        "transcript": _coerce_text(sample.get("transcript"), trim=False),
        "notes": _coerce_text(sample.get("notes"), trim=False),
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def ensure_bundled_voice_library():
    seed_payload = _seed_payload_unlocked()
    seed_voices = seed_payload.get("voices")
    if not isinstance(seed_voices, list) or not seed_voices:
        return

    changed = False
    now = _utc_timestamp()

    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()

        for seed_voice in seed_voices:
            if _has_seed_voice(payload, seed_voice):
                continue

            seeded_voice = {
                "id": _coerce_text(seed_voice.get("id")) or f"voice_{uuid.uuid4().hex[:12]}",
                "name": _coerce_text(seed_voice.get("name"), trim=False) or seed_voice.get("id"),
                "notes": _coerce_text(seed_voice.get("notes"), trim=False),
                "prompt_text": _coerce_text(seed_voice.get("prompt_text"), trim=False),
                "language_code": _coerce_text(seed_voice.get("language_code")),
                "samples": [],
                "created_at": now,
                "updated_at": now,
            }

            for sample in seed_voice.get("samples", []):
                seeded_sample = _seed_voice_sample(seeded_voice, sample, now)
                if seeded_sample is not None:
                    seeded_voice["samples"].append(seeded_sample)

            if not seeded_voice["samples"]:
                logging.debug("Skipping bundled voice '%s' because no samples could be seeded.", seeded_voice["name"])
                continue

            payload["voices"].append(seeded_voice)
            changed = True

        if changed:
            _save_payload_unlocked(payload)


def _find_voice_unlocked(payload: dict[str, Any], voice_id: str) -> dict[str, Any] | None:
    normalized_voice_id = _coerce_text(voice_id)
    for voice in payload["voices"]:
        if _coerce_text(voice.get("id")) == normalized_voice_id:
            return voice
    return None


def _find_sample_unlocked(voice: dict[str, Any], sample_id: str) -> dict[str, Any] | None:
    normalized_sample_id = _coerce_text(sample_id)
    for sample in voice.get("samples", []):
        if _coerce_text(sample.get("id")) == normalized_sample_id:
            return sample
    return None


def _voice_storage_dir(voice_id: str) -> str:
    normalized_voice_id = _coerce_text(voice_id)
    return os.path.join(VOICE_LIBRARY_STORAGE_DIR, normalized_voice_id)


def _build_unique_file_name(directory: str, file_name: str) -> str:
    base_name, extension = os.path.splitext(file_name)
    candidate = file_name
    suffix = 2

    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base_name}_{suffix}{extension}"
        suffix += 1

    return candidate


def _read_text_file(file_path: str) -> str:
    for encoding in _TRANSCRIPT_ENCODINGS:
        try:
            with open(file_path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue

    with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def _load_sidecar_transcript(source_wav_path: str) -> str:
    transcript_path = os.path.splitext(source_wav_path)[0] + ".txt"
    if not os.path.isfile(transcript_path):
        return ""

    try:
        return _read_text_file(transcript_path).strip()
    except OSError:
        return ""


def _wav_duration_seconds(file_path: str) -> float:
    try:
        with wave.open(file_path, "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
    except (OSError, wave.Error) as e:
        raise ValueError(f"Invalid WAV file '{file_path}': {e}") from e

    if frame_rate <= 0:
        return 0.0

    return max(0.0, float(frame_count) / float(frame_rate))


def resolve_sample_path(relative_path: str) -> str:
    normalized_relative_path = _normalize_relative_path(relative_path)
    return os.path.join(VOICE_LIBRARY_DIR, normalized_relative_path.replace("/", os.sep))


def list_voices() -> list[dict[str, Any]]:
    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()

    voices = copy.deepcopy(payload["voices"])
    for voice in voices:
        samples = voice.get("samples", [])
        total_duration = 0.0
        for sample in samples:
            relative_path = _coerce_text(sample.get("relative_path"))
            sample_path = resolve_sample_path(relative_path) if relative_path else ""
            sample["path_exists"] = bool(sample_path and os.path.isfile(sample_path))

            try:
                sample_duration = float(sample.get("duration_seconds") or 0.0)
            except (TypeError, ValueError):
                sample_duration = 0.0
            total_duration += max(0.0, sample_duration)

        voice["sample_count"] = len(samples)
        voice["total_duration_seconds"] = total_duration

    return voices


def get_voice(voice_id: str) -> dict[str, Any] | None:
    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()
        voice = _find_voice_unlocked(payload, voice_id)
        if voice is None:
            return None
        return copy.deepcopy(voice)


def create_voice(
    name: str,
    notes: str = "",
    prompt_text: str = "",
    language_code: str = "",
) -> dict[str, Any]:
    normalized_name = _coerce_text(name)
    if not normalized_name:
        raise ValueError("Voice name cannot be empty.")

    now = _utc_timestamp()
    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()
        existing_name_keys = {
            _coerce_text(voice.get("name")).lower()
            for voice in payload["voices"]
        }
        if normalized_name.lower() in existing_name_keys:
            raise ValueError(f"A voice named '{normalized_name}' already exists.")

        voice_record = {
            "id": f"voice_{uuid.uuid4().hex[:12]}",
            "name": normalized_name,
            "notes": _coerce_text(notes, trim=False),
            "prompt_text": _coerce_text(prompt_text, trim=False),
            "language_code": _normalize_language_code(language_code),
            "samples": [],
            "created_at": now,
            "updated_at": now,
        }
        payload["voices"].append(voice_record)

        os.makedirs(_voice_storage_dir(voice_record["id"]), exist_ok=True)
        _save_payload_unlocked(payload)

        return copy.deepcopy(voice_record)


def update_voice(
    voice_id: str,
    *,
    name: str | None = None,
    notes: str | None = None,
    prompt_text: str | None = None,
    language_code: str | None = None,
) -> dict[str, Any]:
    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()
        voice = _find_voice_unlocked(payload, voice_id)
        if voice is None:
            raise ValueError("Voice entry was not found.")

        normalized_name = _coerce_text(name) if name is not None else None
        if normalized_name is not None:
            if not normalized_name:
                raise ValueError("Voice name cannot be empty.")
            for other_voice in payload["voices"]:
                if other_voice is voice:
                    continue
                if _coerce_text(other_voice.get("name")).lower() == normalized_name.lower():
                    raise ValueError(f"A voice named '{normalized_name}' already exists.")
            voice["name"] = normalized_name

        if notes is not None:
            voice["notes"] = _coerce_text(notes, trim=False)
        if prompt_text is not None:
            voice["prompt_text"] = _coerce_text(prompt_text, trim=False)
        if language_code is not None:
            voice["language_code"] = _normalize_language_code(language_code)

        voice["updated_at"] = _utc_timestamp()
        _save_payload_unlocked(payload)
        return copy.deepcopy(voice)


def delete_voice(voice_id: str):
    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()
        normalized_voice_id = _coerce_text(voice_id)
        voice = _find_voice_unlocked(payload, normalized_voice_id)
        if voice is None:
            raise ValueError("Voice entry was not found.")

        payload["voices"] = [
            current_voice
            for current_voice in payload["voices"]
            if _coerce_text(current_voice.get("id")) != normalized_voice_id
        ]
        _save_payload_unlocked(payload)

    shutil.rmtree(_voice_storage_dir(normalized_voice_id), ignore_errors=True)


def add_samples(voice_id: str, source_wav_paths: list[str]) -> list[dict[str, Any]]:
    normalized_source_paths = [
        _coerce_text(path)
        for path in (source_wav_paths or [])
        if _coerce_text(path)
    ]
    if not normalized_source_paths:
        raise ValueError("No WAV files were selected.")

    validated_sample_info: dict[str, dict[str, Any]] = {}
    for source_path in normalized_source_paths:
        if not source_path.lower().endswith(".wav"):
            raise ValueError(f"Only WAV files are supported: {source_path}")
        if not os.path.isfile(source_path):
            raise FileNotFoundError(f"WAV file not found: {source_path}")

        validated_sample_info[source_path] = {
            "duration_seconds": _wav_duration_seconds(source_path),
            "transcript": _load_sidecar_transcript(source_path),
        }

    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()
        voice = _find_voice_unlocked(payload, voice_id)
        if voice is None:
            raise ValueError("Voice entry was not found.")

        target_dir = _voice_storage_dir(voice["id"])
        os.makedirs(target_dir, exist_ok=True)

        now = _utc_timestamp()
        added_samples: list[dict[str, Any]] = []
        for source_path in normalized_source_paths:
            source_filename = os.path.basename(source_path)
            target_filename = _build_unique_file_name(target_dir, source_filename)
            target_path = os.path.join(target_dir, target_filename)

            shutil.copy2(source_path, target_path)
            sample_info = validated_sample_info[source_path]

            sample_record = {
                "id": f"sample_{uuid.uuid4().hex[:12]}",
                "file_name": target_filename,
                "relative_path": _normalize_relative_path(
                    os.path.relpath(target_path, VOICE_LIBRARY_DIR)
                ),
                "duration_seconds": float(sample_info["duration_seconds"]),
                "transcript": _coerce_text(sample_info["transcript"], trim=False),
                "notes": "",
                "created_at": now,
                "updated_at": now,
            }
            voice["samples"].append(sample_record)
            added_samples.append(copy.deepcopy(sample_record))

        voice["updated_at"] = now
        _save_payload_unlocked(payload)

    return added_samples


def update_sample(
    voice_id: str,
    sample_id: str,
    *,
    transcript: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()
        voice = _find_voice_unlocked(payload, voice_id)
        if voice is None:
            raise ValueError("Voice entry was not found.")

        sample = _find_sample_unlocked(voice, sample_id)
        if sample is None:
            raise ValueError("Voice sample was not found.")

        if transcript is not None:
            sample["transcript"] = _coerce_text(transcript, trim=False)
        if notes is not None:
            sample["notes"] = _coerce_text(notes, trim=False)

        now = _utc_timestamp()
        sample["updated_at"] = now
        voice["updated_at"] = now
        _save_payload_unlocked(payload)
        return copy.deepcopy(sample)


def delete_sample(voice_id: str, sample_id: str):
    with _FILE_IO_LOCK:
        payload = _load_payload_unlocked()
        voice = _find_voice_unlocked(payload, voice_id)
        if voice is None:
            raise ValueError("Voice entry was not found.")

        sample = _find_sample_unlocked(voice, sample_id)
        if sample is None:
            raise ValueError("Voice sample was not found.")

        sample_path = resolve_sample_path(_coerce_text(sample.get("relative_path")))
        voice["samples"] = [
            current_sample
            for current_sample in voice["samples"]
            if _coerce_text(current_sample.get("id")) != _coerce_text(sample_id)
        ]
        voice["updated_at"] = _utc_timestamp()
        _save_payload_unlocked(payload)

    if sample_path and os.path.isfile(sample_path):
        try:
            os.remove(sample_path)
        except OSError:
            pass
