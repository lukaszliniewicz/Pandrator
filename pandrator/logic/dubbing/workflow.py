"""Persisted source-aware workflow state for dubbing sessions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any

from ..source_media import is_media_source

MANIFEST_FILENAME = "workflow_manifest.json"
STAGE_ORDER = ("transcribe", "correct", "translate", "preview", "generate_audio", "export")
DESCENDANTS = {
    "transcribe": ("correct", "translate", "preview", "generate_audio", "export"),
    "correct": ("translate", "preview", "generate_audio", "export"),
    "translate": ("preview", "generate_audio", "export"),
    "preview": ("generate_audio", "export"),
    "generate_audio": ("export",),
    "export": (),
}
PRESET_STAGES = {
    "transcribe": ("transcribe", "export"),
    "clean_subtitles": ("transcribe", "correct", "export"),
    "translate_subtitles": ("transcribe", "correct", "translate", "export"),
    "voiceover": ("transcribe", "correct", "generate_audio", "export"),
    "custom": (),
}


def file_hash(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def settings_hash(settings: Any) -> str:
    encoded = json.dumps(settings, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_path(run_dir: str) -> str:
    return os.path.join(run_dir, MANIFEST_FILENAME)


def load_manifest(run_dir: str) -> dict[str, Any]:
    try:
        with open(manifest_path(run_dir), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("version", 1)
    payload.setdefault("stages", {})
    return payload


def save_manifest(run_dir: str, payload: dict[str, Any]) -> None:
    os.makedirs(run_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".workflow.", suffix=".tmp", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, manifest_path(run_dir))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def record_stage(
    run_dir: str,
    stage: str,
    artifact_path: str = "",
    *,
    parent_path: str = "",
    stage_settings: Any = None,
    lineage_path: str = "",
) -> dict[str, Any]:
    payload = load_manifest(run_dir)
    stages = payload.setdefault("stages", {})
    for descendant in DESCENDANTS.get(stage, ()):
        if descendant in stages and stages[descendant].get("status") == "completed":
            stages[descendant]["status"] = "stale"
    record = {
        "status": "completed",
        "artifact_path": os.path.abspath(artifact_path) if artifact_path else "",
        "artifact_hash": file_hash(artifact_path),
        "parent_path": os.path.abspath(parent_path) if parent_path else "",
        "parent_hash": file_hash(parent_path),
        "settings_hash": settings_hash(stage_settings or {}),
        "lineage_path": os.path.abspath(lineage_path) if lineage_path else "",
    }
    stages[stage] = record
    save_manifest(run_dir, payload)
    return record


def set_stage_status(run_dir: str, stage: str, status: str) -> None:
    payload = load_manifest(run_dir)
    record = payload.setdefault("stages", {}).setdefault(stage, {})
    record["status"] = str(status or "ready")
    save_manifest(run_dir, payload)


def invalidate_stage(run_dir: str, stage: str) -> None:
    payload = load_manifest(run_dir)
    stages = payload.setdefault("stages", {})
    for affected in (stage, *DESCENDANTS.get(stage, ())):
        record = stages.get(affected)
        if isinstance(record, dict) and record.get("status") == "completed":
            record["status"] = "stale"
    save_manifest(run_dir, payload)


def applicable_stages(source_path: str, attached_video_path: str = "") -> list[str]:
    stages = list(STAGE_ORDER)
    if not is_media_source(source_path):
        stages.remove("transcribe")
    return stages


def preset_stages(preset: str, source_path: str, *, translate_voiceover: bool = False) -> list[str]:
    selected = list(PRESET_STAGES.get(str(preset or "custom"), ()))
    if not is_media_source(source_path) and "transcribe" in selected:
        selected.remove("transcribe")
    if preset == "voiceover" and translate_voiceover and "translate" not in selected:
        selected.insert(selected.index("generate_audio"), "translate")
    return selected


def stage_states(run_dir: str, applicable: list[str]) -> dict[str, str]:
    manifest = load_manifest(run_dir)
    saved = manifest.get("stages", {}) if isinstance(manifest.get("stages"), dict) else {}
    changed = False
    stale_ancestors: set[str] = {
        stage
        for stage, record in saved.items()
        if isinstance(record, dict) and record.get("status") == "stale"
    }
    for stage in STAGE_ORDER:
        record = saved.get(stage)
        if not isinstance(record, dict) or record.get("status") != "completed":
            continue
        artifact_path = str(record.get("artifact_path") or "")
        parent_path = str(record.get("parent_path") or "")
        artifact_changed = bool(
            artifact_path
            and str(record.get("artifact_hash") or "")
            and file_hash(artifact_path) != str(record.get("artifact_hash") or "")
        )
        parent_changed = bool(
            parent_path
            and str(record.get("parent_hash") or "")
            and file_hash(parent_path) != str(record.get("parent_hash") or "")
        )
        inherited_stale = any(stage in DESCENDANTS.get(ancestor, ()) for ancestor in stale_ancestors)
        if artifact_changed or parent_changed or inherited_stale:
            record["status"] = "stale"
            stale_ancestors.add(stage)
            changed = True
    if changed:
        save_manifest(run_dir, manifest)
    states: dict[str, str] = {}
    for stage in STAGE_ORDER:
        if stage not in applicable:
            states[stage] = "unavailable"
            continue
        state = str((saved.get(stage) or {}).get("status") or "ready")
        states[stage] = state if state in {"ready", "running", "completed", "stale", "failed"} else "ready"
    return states
