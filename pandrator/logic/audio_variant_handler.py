import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any


SOURCE_VARIANT_ID = "source"
VARIANTS_DIR_NAME = "Audio_Variants"
MANIFEST_FILENAME = "variants.json"
SENTENCE_WAVS_DIR_NAME = "Sentence_wavs"
MANIFEST_VERSION = 1
RVC_VARIANT_KIND = "rvc"

RVC_SETTING_KEYS = (
    "rvc_model",
    "pitch",
    "filter_radius",
    "index_rate",
    "volume_envelope",
    "protect",
    "f0_method",
)

_FILE_IO_LOCK = threading.RLock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(file_path: str, payload: Any):
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


def _sentence_sort_key(sentence_number: str) -> tuple[int, int | str]:
    normalized = str(sentence_number or "").strip()
    try:
        return 0, int(normalized)
    except ValueError:
        return 1, normalized.lower()


def _dedupe_sorted_sentence_numbers(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return sorted(deduped, key=_sentence_sort_key)


def _sanitize_slug(raw_value: str, fallback: str = "variant") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(raw_value or "").strip()).strip("-").lower()
    return slug or fallback


def _manifest_dir(base_dir: str) -> str:
    return os.path.join(base_dir, VARIANTS_DIR_NAME)


def manifest_path(base_dir: str) -> str:
    return os.path.join(_manifest_dir(base_dir), MANIFEST_FILENAME)


def variant_root_dir(base_dir: str, variant_id: str) -> str:
    return os.path.join(_manifest_dir(base_dir), variant_id)


def variant_wavs_dir(base_dir: str, variant_id: str, ensure_exists: bool = False) -> str:
    wavs_dir = os.path.join(variant_root_dir(base_dir, variant_id), SENTENCE_WAVS_DIR_NAME)
    if ensure_exists:
        os.makedirs(wavs_dir, exist_ok=True)
    return wavs_dir


def variant_sentence_path(
    base_dir: str,
    variant_id: str,
    session_name: str,
    sentence_number: str,
    ensure_dir: bool = False,
) -> str:
    wavs_dir = variant_wavs_dir(base_dir, variant_id, ensure_exists=ensure_dir)
    return os.path.join(wavs_dir, f"{session_name}_sentence_{sentence_number}.wav")


def normalize_rvc_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "rvc_model": str(settings.get("rvc_model") or "").strip(),
        "pitch": int(settings.get("pitch", 0) or 0),
        "filter_radius": int(settings.get("filter_radius", 3) or 0),
        "index_rate": float(settings.get("index_rate", 0.3) or 0.0),
        "volume_envelope": float(settings.get("volume_envelope", 1.0) or 0.0),
        "protect": float(settings.get("protect", 0.3) or 0.0),
        "f0_method": str(settings.get("f0_method") or "rmvpe").strip() or "rmvpe",
    }


def rvc_settings_hash(settings: dict[str, Any]) -> str:
    normalized = normalize_rvc_settings(settings)
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_rvc_variant_record(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_rvc_settings(settings)
    model_name = normalized["rvc_model"]
    settings_hash = rvc_settings_hash(normalized)
    model_slug = _sanitize_slug(model_name, "model")
    variant_id = f"rvc-{model_slug}-{settings_hash[:10]}"
    return {
        "id": variant_id,
        "kind": RVC_VARIANT_KIND,
        "label": f"RVC: {model_name}",
        "model_name": model_name,
        "settings_hash": settings_hash,
        "settings": normalized,
        "relative_dir": os.path.join(VARIANTS_DIR_NAME, variant_id, SENTENCE_WAVS_DIR_NAME),
        "sentence_numbers": [],
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
    }


def _normalize_variant_record(record: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None

    variant_id = str(record.get("id") or "").strip()
    if not variant_id or variant_id == SOURCE_VARIANT_ID:
        return None

    kind = str(record.get("kind") or RVC_VARIANT_KIND).strip().lower()
    if kind != RVC_VARIANT_KIND:
        return None

    model_name = str(record.get("model_name") or "").strip()
    settings = record.get("settings") if isinstance(record.get("settings"), dict) else {}
    normalized_settings = normalize_rvc_settings({**settings, "rvc_model": model_name or settings.get("rvc_model")})
    if not model_name:
        model_name = normalized_settings["rvc_model"]

    settings_hash = str(record.get("settings_hash") or rvc_settings_hash(normalized_settings)).strip()
    now = _utc_now_iso()
    return {
        "id": variant_id,
        "kind": RVC_VARIANT_KIND,
        "label": str(record.get("label") or f"RVC: {model_name}").strip(),
        "model_name": model_name,
        "settings_hash": settings_hash,
        "settings": normalized_settings,
        "relative_dir": str(
            record.get("relative_dir")
            or os.path.join(VARIANTS_DIR_NAME, variant_id, SENTENCE_WAVS_DIR_NAME)
        ),
        "sentence_numbers": _dedupe_sorted_sentence_numbers(list(record.get("sentence_numbers") or [])),
        "created_at": str(record.get("created_at") or now),
        "updated_at": str(record.get("updated_at") or now),
    }


def load_manifest(base_dir: str) -> dict[str, Any]:
    path = manifest_path(base_dir)
    try:
        with _FILE_IO_LOCK:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": MANIFEST_VERSION, "variants": []}

    if not isinstance(payload, dict):
        return {"version": MANIFEST_VERSION, "variants": []}

    variants = []
    for record in list(payload.get("variants") or []):
        normalized = _normalize_variant_record(record)
        if normalized is not None:
            variants.append(normalized)

    return {"version": MANIFEST_VERSION, "variants": variants}


def save_manifest(base_dir: str, manifest: dict[str, Any]):
    variants = []
    for record in list((manifest or {}).get("variants") or []):
        normalized = _normalize_variant_record(record)
        if normalized is not None:
            variants.append(normalized)

    payload = {"version": MANIFEST_VERSION, "variants": variants}
    with _FILE_IO_LOCK:
        _write_json_atomic(manifest_path(base_dir), payload)


def _find_record(manifest: dict[str, Any], variant_id: str) -> dict[str, Any] | None:
    for record in list(manifest.get("variants") or []):
        if str(record.get("id") or "") == str(variant_id):
            return record
    return None


def rvc_variant_id_for_settings(settings: dict[str, Any]) -> str:
    return str(build_rvc_variant_record(settings)["id"])


def rvc_variant_sentence_path(
    base_dir: str,
    settings: dict[str, Any],
    session_name: str,
    sentence_number: str,
    ensure_dir: bool = False,
) -> str:
    variant_id = rvc_variant_id_for_settings(settings)
    return variant_sentence_path(base_dir, variant_id, session_name, str(sentence_number), ensure_dir)


def register_rvc_variant_sentence(
    base_dir: str,
    settings: dict[str, Any],
    sentence_number: str,
) -> dict[str, Any]:
    new_record = build_rvc_variant_record(settings)
    variant_id = str(new_record["id"])
    manifest = load_manifest(base_dir)
    record = _find_record(manifest, variant_id)
    now = _utc_now_iso()

    if record is None:
        record = new_record
        record["created_at"] = now
        manifest.setdefault("variants", []).append(record)

    record["updated_at"] = now
    record["label"] = new_record["label"]
    record["model_name"] = new_record["model_name"]
    record["settings_hash"] = new_record["settings_hash"]
    record["settings"] = new_record["settings"]
    record["relative_dir"] = new_record["relative_dir"]
    record["sentence_numbers"] = _dedupe_sorted_sentence_numbers(
        list(record.get("sentence_numbers") or []) + [str(sentence_number)]
    )
    save_manifest(base_dir, manifest)
    return record


def _scan_variant_sentence_numbers(base_dir: str, variant_id: str, session_name: str) -> list[str]:
    wavs_dir = variant_wavs_dir(base_dir, variant_id)
    if not os.path.isdir(wavs_dir):
        return []

    pattern = re.compile(
        rf"^{re.escape(session_name)}_sentence_(?P<number>.+)\.wav$",
        re.IGNORECASE,
    )
    numbers: list[str] = []
    try:
        for name in os.listdir(wavs_dir):
            match = pattern.match(name)
            if match:
                numbers.append(match.group("number"))
    except OSError:
        return []
    return _dedupe_sorted_sentence_numbers(numbers)


def existing_variant_sentence_numbers(base_dir: str, variant_id: str, session_name: str) -> list[str]:
    scanned_numbers = _scan_variant_sentence_numbers(base_dir, variant_id, session_name)
    if scanned_numbers:
        return scanned_numbers

    manifest = load_manifest(base_dir)
    record = _find_record(manifest, variant_id)
    if record is None:
        return []

    existing_numbers: list[str] = []
    for sentence_number in list(record.get("sentence_numbers") or []):
        path = variant_sentence_path(base_dir, variant_id, session_name, str(sentence_number))
        if os.path.isfile(path):
            existing_numbers.append(str(sentence_number))
    return _dedupe_sorted_sentence_numbers(existing_numbers)


def list_rvc_variants(base_dir: str, session_name: str, prune_missing: bool = True) -> list[dict[str, Any]]:
    manifest = load_manifest(base_dir)
    records: list[dict[str, Any]] = []
    changed = False

    for record in list(manifest.get("variants") or []):
        variant_id = str(record.get("id") or "")
        existing_numbers = existing_variant_sentence_numbers(base_dir, variant_id, session_name)
        if not existing_numbers:
            changed = True
            if prune_missing:
                try:
                    shutil.rmtree(variant_root_dir(base_dir, variant_id))
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            continue

        if existing_numbers != list(record.get("sentence_numbers") or []):
            record["sentence_numbers"] = existing_numbers
            record["updated_at"] = _utc_now_iso()
            changed = True
        records.append(record)

    if changed:
        manifest["variants"] = records
        save_manifest(base_dir, manifest)

    return [dict(record) for record in records]


def get_variant_record(base_dir: str, variant_id: str, session_name: str = "") -> dict[str, Any] | None:
    if not variant_id or variant_id == SOURCE_VARIANT_ID:
        return None

    records = (
        list_rvc_variants(base_dir, session_name, prune_missing=True)
        if session_name
        else list(load_manifest(base_dir).get("variants") or [])
    )
    for record in records:
        if str(record.get("id") or "") == str(variant_id):
            return dict(record)
    return None


def remove_variant_sentences(base_dir: str, session_name: str, sentence_numbers: list[str]) -> int:
    normalized_numbers = _dedupe_sorted_sentence_numbers(sentence_numbers)
    if not normalized_numbers:
        return 0

    manifest = load_manifest(base_dir)
    kept_records: list[dict[str, Any]] = []
    removed_count = 0

    for record in list(manifest.get("variants") or []):
        variant_id = str(record.get("id") or "")
        current_numbers = set(existing_variant_sentence_numbers(base_dir, variant_id, session_name))
        target_numbers = set(normalized_numbers)
        removed_numbers = current_numbers & target_numbers

        for sentence_number in removed_numbers:
            path = variant_sentence_path(base_dir, variant_id, session_name, sentence_number)
            try:
                os.remove(path)
                removed_count += 1
            except FileNotFoundError:
                pass
            except OSError:
                pass

        remaining_numbers = _dedupe_sorted_sentence_numbers(list(current_numbers - target_numbers))
        if remaining_numbers:
            record["sentence_numbers"] = remaining_numbers
            record["updated_at"] = _utc_now_iso()
            kept_records.append(record)
        else:
            try:
                shutil.rmtree(variant_root_dir(base_dir, variant_id))
            except FileNotFoundError:
                pass
            except OSError:
                pass

    manifest["variants"] = kept_records
    save_manifest(base_dir, manifest)
    return removed_count


def remove_all_variants(base_dir: str):
    variants_dir = _manifest_dir(base_dir)
    if os.path.isdir(variants_dir):
        shutil.rmtree(variants_dir)
