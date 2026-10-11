"""Prepared sound-mix snapshot, fingerprint, and file-pin verification."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .database import Database
from .settings_policy import RevisionConflict, stable_hash
from .sound_mix_anchors import _cue_snapshots
from .sound_mix_inputs import (
    _load_snapshot,
    _require_audio_artifact,
    _snapshot_pins,
)
from .sound_mix_manifest import _base_geometry

SOUND_MIX_RENDERER_VERSION = "sound-mix-render-v1"


@dataclass(frozen=True, slots=True)
class PreparedSoundMix:
    """Path-bearing, process-local snapshot; never serialize this object."""

    session_id: str
    workflow_kind: str
    storage_key: str
    sound_plan_id: str | None
    sound_plan_revision: int
    speech_plan_revision_id: str | None
    active_speech_plan_revision_id: str | None
    base_artifact_id: str
    base_content_hash: str
    base_path: Path
    base_metadata: dict[str, Any]
    timeline_artifact_id: str | None
    timeline_content_hash: str | None
    timeline_path: Path | None
    timeline_metadata: dict[str, Any] | None
    anchor_metadata: dict[str, Any]
    rate: int
    channels: int
    base_frames: int
    fixed_timeline: bool
    cues: tuple[dict[str, Any], ...]
    source_paths: dict[str, Path]
    source_take_ids: tuple[str, ...]
    pins: dict[str, Any]
    request_fingerprint: str
    payload: dict[str, Any]
    cache_artifact_id: str | None = None
    cache_artifact_hash: str | None = None

    @property
    def has_cues(self) -> bool:
        return bool(self.cues)



def prepare_sound_mix(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    session_id: str,
    base_artifact_id: str,
    *,
    timeline_artifact_id: str | None = None,
    expected_revision: int | None = None,
) -> PreparedSoundMix:
    """Freeze all database pins, then inspect and hash inputs without writes."""

    with database.snapshot_session() as session:
        snapshot = _load_snapshot(session, session_id, base_artifact_id, timeline_artifact_id)
    if expected_revision is not None and snapshot["plan"]["revision"] != expected_revision:
        raise RevisionConflict("The sound plan changed. Review the current cues before rendering.")
    session_record = snapshot["session"]
    base = _require_audio_artifact(
        snapshot["base"], session_id=session_id, label="base artifact"
    )
    timeline = _require_audio_artifact(
        snapshot["timeline"], session_id=session_id, label="timeline artifact"
    ) if timeline_artifact_id else None
    base_path = paths.managed_path(str(base["relative_path"]))
    timeline_path = paths.managed_path(str(timeline["relative_path"])) if timeline else None
    pins = _snapshot_pins(snapshot)
    if not snapshot["included_cues"]:
        fingerprint = stable_hash(
            {"renderer_version": SOUND_MIX_RENDERER_VERSION, "pins": pins, "cues": []}
        )
        return PreparedSoundMix(
            session_id=session_id,
            workflow_kind=session_record["workflow_kind"],
            storage_key=session_record["storage_key"],
            sound_plan_id=snapshot["plan"]["id"],
            sound_plan_revision=snapshot["plan"]["revision"],
            speech_plan_revision_id=snapshot["plan"]["speech_plan_revision_id"],
            active_speech_plan_revision_id=snapshot["active_speech_plan_revision_id"],
            base_artifact_id=base["id"],
            base_content_hash=str(base["content_hash"]),
            base_path=base_path,
            base_metadata=deepcopy(base.get("metadata_json") or {}),
            timeline_artifact_id=timeline["id"] if timeline else None,
            timeline_content_hash=timeline["content_hash"] if timeline else None,
            timeline_path=timeline_path,
            timeline_metadata=deepcopy(timeline.get("metadata_json") or {}) if timeline else None,
            anchor_metadata={},
            rate=0,
            channels=0,
            base_frames=0,
            fixed_timeline=session_record["workflow_kind"] != "audiobook",
            cues=(),
            source_paths={},
            source_take_ids=(),
            pins=pins,
            request_fingerprint=fingerprint,
            payload={
                "session_id": session_id,
                "base_artifact_id": base["id"],
                "base_content_hash": base["content_hash"],
                "timeline_artifact_id": timeline["id"] if timeline else None,
                "timeline_content_hash": timeline["content_hash"] if timeline else None,
                "sound_plan_id": snapshot["plan"]["id"],
                "sound_plan_revision": snapshot["plan"]["revision"],
                "speech_plan_revision_id": snapshot["plan"]["speech_plan_revision_id"],
                "request_fingerprint": fingerprint,
            },
        )
    if not base_path.is_file():
        raise ValueError("The base audio artifact is missing from managed storage.")
    if timeline_path is not None and not timeline_path.is_file():
        raise ValueError("The timeline audio artifact is missing from managed storage.")
    rate, channels, base_frames, fixed_timeline = _base_geometry(
        base, session_record["workflow_kind"], base_path
    )
    anchor_source = snapshot["timeline"] if timeline_artifact_id else snapshot["base"]
    anchor_metadata = deepcopy((anchor_source or {}).get("metadata_json") or {})
    cues, cue_paths = _cue_snapshots(
        snapshot,
        anchor_metadata=anchor_metadata,
        source_is_base=not bool(timeline_artifact_id),
        paths=paths,
        rate=rate,
        channels=channels,
        base_frames=base_frames,
    )
    prepared = PreparedSoundMix(
        session_id=session_id,
        workflow_kind=session_record["workflow_kind"],
        storage_key=session_record["storage_key"],
        sound_plan_id=snapshot["plan"]["id"],
        sound_plan_revision=snapshot["plan"]["revision"],
        speech_plan_revision_id=snapshot["plan"]["speech_plan_revision_id"],
        active_speech_plan_revision_id=snapshot["active_speech_plan_revision_id"],
        base_artifact_id=base["id"],
        base_content_hash=str(base["content_hash"]),
        base_path=base_path,
        base_metadata=deepcopy(base.get("metadata_json") or {}),
        timeline_artifact_id=timeline["id"] if timeline else None,
        timeline_content_hash=timeline["content_hash"] if timeline else None,
        timeline_path=timeline_path,
        timeline_metadata=deepcopy(timeline.get("metadata_json") or {}) if timeline else None,
        anchor_metadata=anchor_metadata,
        rate=rate,
        channels=channels,
        base_frames=base_frames,
        fixed_timeline=fixed_timeline,
        cues=cues,
        source_paths=cue_paths,
        source_take_ids=snapshot["source_take_ids"],
        pins=pins,
        request_fingerprint="",
        payload={},
    )
    _verify_input_files(prepared, snapshot)
    fingerprint_inputs = {
        "renderer_version": SOUND_MIX_RENDERER_VERSION,
        "pins": pins,
        "rate": rate,
        "channels": channels,
        "expected_base_frames": base_frames,
        "fixed_timeline": fixed_timeline,
        "cues": [
            {
                key: cue[key]
                for key in (
                    "cue_id", "variant_id", "artifact_id", "content_hash", "anchor_frame",
                    "anchor_pin", "mode", "offset_ms", "gain_db", "trim_start_seconds",
                    "trim_end_seconds", "fade_in_ms", "fade_out_ms", "playback_json", "anchor_json",
                )
            }
            for cue in cues
        ],
    }
    request_fingerprint = stable_hash(fingerprint_inputs)
    payload = {
        "session_id": session_id,
        "base_artifact_id": base["id"],
        "base_content_hash": base["content_hash"],
        "timeline_artifact_id": timeline["id"] if timeline else None,
        "timeline_content_hash": timeline["content_hash"] if timeline else None,
        "sound_plan_id": snapshot["plan"]["id"],
        "sound_plan_revision": snapshot["plan"]["revision"],
        "speech_plan_revision_id": snapshot["plan"]["speech_plan_revision_id"],
        "request_fingerprint": request_fingerprint,
    }
    prepared = replace(
        prepared, request_fingerprint=request_fingerprint, payload=payload
    )
    return prepared


def _verify_input_files(prepared: PreparedSoundMix, snapshot: dict[str, Any]) -> None:
    if not prepared.has_cues:
        return
    artifacts = {"base": snapshot["base"]}
    if snapshot["timeline"] is not None:
        artifacts["timeline"] = snapshot["timeline"]
    for cue in prepared.cues:
        artifacts[f"cue:{cue['cue_id']}"] = snapshot["variants"][cue["variant_id"]]["artifact"]
    for take_id in prepared.source_take_ids:
        if f"take:{take_id}" not in prepared.source_paths:
            continue
        take = snapshot["take_data"].get(take_id)
        if take and take.get("artifact"):
            artifacts[f"take:{take_id}"] = take["artifact"]
    paths = {
        "base": prepared.base_path,
        **({"timeline": prepared.timeline_path} if prepared.timeline_path else {}),
        **{f"cue:{cue_id}": path for cue_id, path in prepared.source_paths.items()},
        **{key: path for key, path in prepared.source_paths.items() if key.startswith("take:")},
    }
    for key, artifact in artifacts.items():
        path = paths.get(key)
        if path is None or not path.is_file() or sha256_file(path) != artifact.get("content_hash"):
            raise ValueError(f"Sound-mix input content hash changed: {artifact.get('id')}.")


def verify_prepared_files(prepared: PreparedSoundMix) -> None:
    """Recheck all file pins outside a database writer transaction."""

    if not prepared.has_cues:
        return
    inputs: dict[str, tuple[Path, str | None]] = {
        "base": (prepared.base_path, prepared.base_content_hash),
    }
    if prepared.timeline_path is not None:
        inputs["timeline"] = (
            prepared.timeline_path,
            prepared.timeline_content_hash,
        )
    for cue in prepared.cues:
        inputs[f"cue:{cue['cue_id']}"] = (
            prepared.source_paths[cue["cue_id"]],
            str(cue["content_hash"]),
        )
    take_hashes = {
        str(row.get("take_id")): row.get("artifact_hash")
        for row in prepared.pins.get("takes", [])
        if isinstance(row, dict)
    }
    for take_id in prepared.source_take_ids:
        path = prepared.source_paths.get(f"take:{take_id}")
        if path is not None:
            inputs[f"take:{take_id}"] = (path, take_hashes.get(take_id))
    for label, (path, expected_hash) in inputs.items():
        if not expected_hash or not path.is_file() or sha256_file(path) != expected_hash:
            raise ValueError(f"Sound-mix input content hash changed: {label}.")


__all__ = [
    "PreparedSoundMix",
    "SOUND_MIX_RENDERER_VERSION",
    "prepare_sound_mix",
    "verify_prepared_files",
]
