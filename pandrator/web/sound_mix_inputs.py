"""Database capture and immutable pin construction for sound mixes."""

from __future__ import annotations

import hashlib
import math
import wave
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SoundVariant,
)
from .settings_policy import stable_hash
from .sound_project_commands import latest_cues
from .sound_project_queries import current_header


def _artifact_data(artifact: Artifact | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    return {
        "id": artifact.id,
        "session_id": artifact.session_id,
        "kind": artifact.kind,
        "role": artifact.role,
        "state": artifact.state,
        "relative_path": artifact.relative_path,
        "content_hash": artifact.content_hash,
        "metadata_json": deepcopy(artifact.metadata_json or {}),
        "size_bytes": artifact.size_bytes,
    }


def _take_data_in_session(
    session: Session, take_ids: set[str], session_id: str
) -> dict[str, dict[str, Any]]:
    if not take_ids:
        return {}
    rows = session.execute(
        select(AudioTake, GenerationSegment, Artifact)
        .join(GenerationSegment, GenerationSegment.id == AudioTake.generation_segment_id)
        .join(
            GenerationPlanRevision,
            GenerationPlanRevision.id == GenerationSegment.plan_revision_id,
        )
        .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
        .outerjoin(Artifact, Artifact.id == AudioTake.artifact_id)
        .where(AudioTake.id.in_(take_ids), GenerationPlan.session_id == session_id)
    ).all()
    result: dict[str, dict[str, Any]] = {}
    for take, segment, artifact in rows:
        result[take.id] = {
            "take_id": take.id,
            "segment_id": take.generation_segment_id,
            "take_artifact_id": take.artifact_id,
            "take_status": take.status,
            "duration_ms": take.duration_ms,
            "segment_text": segment.text,
            "segment_plan_revision_id": segment.plan_revision_id,
            "artifact": _artifact_data(artifact),
        }
    return result


def _int(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}.")
    return value

def _variant_normalized_frames(variant: dict[str, Any], rate: int) -> int | None:
    frames, source_rate = variant.get("frame_count"), variant.get("sample_rate")
    if type(frames) is int and frames > 0 and type(source_rate) is int and source_rate > 0:
        return round(frames * rate / source_rate)
    duration = variant.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(float(duration)) and duration > 0:
        return round(float(duration) * rate)
    return None


def _read_take_wav(path: Path) -> tuple[int, int, int]:
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise ValueError
            frames = int(reader.getnframes())
            rate = int(reader.getframerate())
            channels = int(reader.getnchannels())
            if frames <= 0 or not 8000 <= rate <= 192000 or channels not in {1, 2}:
                raise ValueError
            return frames, rate, channels
    except (OSError, EOFError, wave.Error, ValueError) as error:
        raise ValueError("Resolved word/pause anchors require a current PCM WAV take.") from error


def _manifest_rows(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    rows = metadata.get("takes")
    if rows is None:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("The audio timeline has an invalid frozen take manifest.")
    return rows


def _manifest_take_ids(metadata: dict[str, Any], cues: list[dict[str, Any]]) -> set[str]:
    rows = _manifest_rows(metadata)
    segment_ids = {
        str(cue["anchor_json"].get("segment_id") or "")
        for cue in cues
        if cue["anchor_json"].get("kind") in {"before_segment", "after_segment", "after_word", "next_pause"}
    }
    resolution_ids = {
        str((cue["anchor_json"].get("resolution") or {}).get("take_id") or "")
        for cue in cues
        if cue["anchor_json"].get("kind") in {"after_word", "next_pause"}
        and isinstance(cue["anchor_json"].get("resolution"), dict)
    }
    return {
        str(row.get("take_id") or "")
        for row in rows
        if (str(row.get("segment_id") or "") in segment_ids or str(row.get("take_id") or "") in resolution_ids)
        and row.get("take_id")
    }


def _load_snapshot(
    session: Session,
    session_id: str,
    base_artifact_id: str,
    timeline_artifact_id: str | None,
) -> dict[str, Any]:
    record, plan, active_speech_revision = current_header(session, session_id)
    cues = latest_cues(session, plan)
    base = session.get(Artifact, base_artifact_id)
    timeline = session.get(Artifact, timeline_artifact_id) if timeline_artifact_id else None
    if base is None:
        raise KeyError(base_artifact_id)
    if timeline_artifact_id and timeline is None:
        raise KeyError(timeline_artifact_id)
    included = [cue for cue in cues if bool(cue.get("included"))]
    anchor_artifact = timeline if timeline_artifact_id else base
    anchor_metadata = deepcopy((anchor_artifact.metadata_json or {}) if anchor_artifact else {})
    variant_ids = {str(cue["variant_id"]) for cue in included}
    variants: dict[str, dict[str, Any]] = {}
    if variant_ids:
        rows = session.execute(
            select(SoundVariant, Artifact)
            .outerjoin(Artifact, Artifact.id == SoundVariant.artifact_id)
            .where(SoundVariant.id.in_(variant_ids))
        ).all()
        variants = {
            variant.id: {
                "id": variant.id,
                "state": variant.state,
                "artifact_id": variant.artifact_id,
                "sample_rate": variant.sample_rate,
                "channels": variant.channels,
                "frame_count": variant.frame_count,
                "duration_seconds": variant.duration_seconds,
                "artifact": _artifact_data(artifact),
            }
            for variant, artifact in rows
        }
    take_ids = _manifest_take_ids(anchor_metadata, included)
    take_data = _take_data_in_session(session, take_ids, record.id)
    return {
        "session": {
            "id": record.id,
            "workflow_kind": record.workflow_kind,
            "storage_key": record.storage_key,
        },
        "plan": {
            "id": plan.id if plan else None,
            "revision": plan.revision if plan else 0,
            "speech_plan_revision_id": plan.speech_plan_revision_id if plan else active_speech_revision,
        },
        "active_speech_plan_revision_id": active_speech_revision,
        "base": _artifact_data(base),
        "timeline": _artifact_data(timeline),
        "anchor_metadata": anchor_metadata,
        "cues": cues,
        "included_cues": included,
        "variants": variants,
        "take_data": take_data,
        "source_take_ids": tuple(sorted(take_ids)),
    }


def _require_audio_artifact(
    artifact: dict[str, Any] | None,
    *,
    session_id: str,
    label: str,
    global_artifact: bool = False,
) -> dict[str, Any]:
    if artifact is not None and artifact.get("role") == "sound_mix.audio":
        raise ValueError(
            f"The selected {label} is already a sound mix; use its original unmixed audio source."
        )
    if (
        artifact is None
        or artifact.get("kind") != "audio"
        or artifact.get("state") != "current"
        or not artifact.get("content_hash")
        or not artifact.get("relative_path")
        or (global_artifact and artifact.get("session_id") is not None)
        or (not global_artifact and artifact.get("session_id") != session_id)
    ):
        raise ValueError(f"The selected {label} must be a current audio artifact in this project.")
    return artifact


def _metadata_hash(metadata: dict[str, Any]) -> str:
    return stable_hash(metadata)


def _snapshot_pins(snapshot: dict[str, Any]) -> dict[str, Any]:
    base = snapshot["base"]
    timeline = snapshot["timeline"]
    variants = snapshot["variants"]
    cue_pins = []
    for cue in snapshot["included_cues"]:
        variant = variants.get(str(cue["variant_id"])) or {}
        artifact = variant.get("artifact") or {}
        cue_pins.append(
            {
                "logical_id": cue["logical_id"],
                "variant_id": cue["variant_id"],
                "anchor_json": cue["anchor_json"],
                "playback_json": cue["playback_json"],
                "placement_status": cue["placement_status"],
                "variant_state": variant.get("state"),
                "variant_artifact_id": variant.get("artifact_id"),
                "variant_artifact_hash": artifact.get("content_hash"),
                "variant_artifact_state": artifact.get("state"),
                "variant_artifact_session_id": artifact.get("session_id"),
                "variant_artifact_kind": artifact.get("kind"),
                "variant_artifact_path_pin": stable_hash(artifact.get("relative_path")),
                "variant_sample_rate": variant.get("sample_rate"),
                "variant_frame_count": variant.get("frame_count"),
                "variant_duration_seconds": variant.get("duration_seconds"),
            }
        )
    take_pins = []
    for take_id in snapshot["source_take_ids"]:
        take = snapshot["take_data"].get(take_id)
        artifact = (take or {}).get("artifact") or {}
        take_pins.append(
            {
                "take_id": take_id,
                "segment_id": (take or {}).get("segment_id"),
                "take_artifact_id": (take or {}).get("take_artifact_id"),
                "take_status": (take or {}).get("take_status"),
                "duration_ms": (take or {}).get("duration_ms"),
                "segment_plan_revision_id": (take or {}).get("segment_plan_revision_id"),
                "synthesized_text_hash": hashlib.sha256(
                    str((artifact.get("metadata_json") or {}).get("synthesized_text") or "").encode("utf-8")
                ).hexdigest(),
                "artifact_id": artifact.get("id"),
                "artifact_hash": artifact.get("content_hash"),
                "artifact_state": artifact.get("state"),
                "artifact_session_id": artifact.get("session_id"),
                "artifact_kind": artifact.get("kind"),
                "artifact_path_pin": stable_hash(artifact.get("relative_path")),
            }
        )
    return {
        "session_id": snapshot["session"]["id"],
        "workflow_kind": snapshot["session"]["workflow_kind"],
        "sound_plan_id": snapshot["plan"]["id"],
        "sound_plan_revision": snapshot["plan"]["revision"],
        "speech_plan_revision_id": snapshot["plan"]["speech_plan_revision_id"],
        "active_speech_plan_revision_id": snapshot["active_speech_plan_revision_id"],
        "base_artifact_id": base["id"],
        "base_content_hash": base["content_hash"],
        "base_state": base["state"],
        "base_session_id": base["session_id"],
        "base_kind": base["kind"],
        "base_path_pin": stable_hash(base.get("relative_path")),
        "base_metadata_hash": _metadata_hash(base.get("metadata_json") or {}),
        "timeline_artifact_id": timeline["id"] if timeline else None,
        "timeline_content_hash": timeline["content_hash"] if timeline else None,
        "timeline_state": timeline["state"] if timeline else None,
        "timeline_session_id": timeline["session_id"] if timeline else None,
        "timeline_kind": timeline["kind"] if timeline else None,
        "timeline_path_pin": stable_hash(timeline.get("relative_path")) if timeline else None,
        "timeline_metadata_hash": _metadata_hash(timeline.get("metadata_json") or {}) if timeline else None,
        "cues": cue_pins,
        "takes": take_pins,
    }
