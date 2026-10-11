"""Frozen manifest and cue-anchor validation for sound-mix inputs."""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from pandrator.runtime import DataPaths

from .artifacts import sha256_file
from .sound_mix_inputs import (
    _int,
    _manifest_rows,
    _read_take_wav,
    _require_audio_artifact,
    _variant_normalized_frames,
)
from .sound_project_schemas import SoundPlayback

_NEEDS_PLACEMENT = "Resolve this cue's placement before rendering sound."



def _frame_bounds(
    row: dict[str, Any], *, metadata: dict[str, Any], source_is_base: bool, base_rate: int
) -> tuple[int, int]:
    has_start, has_end = "start_frame" in row, "end_frame" in row
    if has_start != has_end:
        raise ValueError(_NEEDS_PLACEMENT)
    if has_start:
        start = _int(row["start_frame"], "take.start_frame")
        end = _int(row["end_frame"], "take.end_frame")
        timeline = metadata.get("audio_timeline")
        row_rate = row.get("sample_rate_hz")
        if row_rate is None and isinstance(timeline, dict):
            row_rate = timeline.get("sample_rate_hz")
        if row_rate is None and source_is_base:
            row_rate = base_rate
        row_rate = _int(row_rate, "take.sample_rate_hz", minimum=1)
        return round(start * base_rate / row_rate), round(end * base_rate / row_rate)
    start_ms, end_ms = row.get("target_start_ms"), row.get("target_end_ms")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, (int, float))
        or not math.isfinite(float(start_ms))
        or start_ms < 0
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, (int, float))
        or not math.isfinite(float(end_ms))
        or end_ms < start_ms
    ):
        raise ValueError(_NEEDS_PLACEMENT)
    return round(float(start_ms) * base_rate / 1000), round(float(end_ms) * base_rate / 1000)



def _take_row_for_cue(
    cue: dict[str, Any], snapshot: dict[str, Any], manifest: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    anchor = cue["anchor_json"]
    kind = str(anchor.get("kind") or "")
    resolution = anchor.get("resolution") if kind in {"after_word", "next_pause"} else None
    requested_take = str((resolution or {}).get("take_id") or "") if isinstance(resolution, dict) else ""
    segment_id = str(anchor.get("segment_id") or "")
    matches = [
        row
        for row in manifest
        if (row.get("take_id") == requested_take if requested_take else row.get("segment_id") == segment_id)
        and (not segment_id or row.get("segment_id") == segment_id)
    ]
    if len(matches) != 1:
        raise ValueError(_NEEDS_PLACEMENT)
    row = matches[0]
    take = snapshot["take_data"].get(str(row.get("take_id") or ""))
    if (
        take is None
        or take.get("take_status") != "completed"
        or take.get("take_id") != row.get("take_id")
        or take.get("segment_id") != row.get("segment_id")
        or take.get("take_artifact_id") != row.get("artifact_id")
    ):
        raise ValueError(_NEEDS_PLACEMENT)
    artifact = take.get("artifact") or {}
    if (
        artifact.get("kind") != "audio"
        or artifact.get("state") != "current"
        or artifact.get("session_id") != snapshot["session"]["id"]
        or not artifact.get("content_hash")
        or not artifact.get("relative_path")
    ):
        raise ValueError(_NEEDS_PLACEMENT)
    if take.get("segment_plan_revision_id") != snapshot["plan"]["speech_plan_revision_id"]:
        raise ValueError(_NEEDS_PLACEMENT)
    return row, take


def _resolve_anchor(
    cue: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    anchor_metadata: dict[str, Any],
    source_is_base: bool,
    base_rate: int,
    base_frames: int,
    paths: DataPaths,
    source_paths: dict[str, Path],
) -> tuple[int, dict[str, Any]]:
    anchor = cue["anchor_json"]
    kind = str(anchor.get("kind") or "")
    if kind == "source_time":
        seconds = anchor.get("time_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(float(seconds)) or seconds < 0:
            raise ValueError("Source-time anchor must contain finite non-negative seconds.")
        frame = round(float(seconds) * base_rate)
        if frame > base_frames:
            raise ValueError("Source-time anchor is outside the fixed audio timeline.")
        return frame, {"kind": kind, "time_seconds": float(seconds), "frame": frame}
    if kind not in {"before_segment", "after_segment", "after_word", "next_pause"}:
        raise ValueError(_NEEDS_PLACEMENT)
    if kind in {"after_word", "next_pause"}:
        if snapshot["session"]["workflow_kind"] != "audiobook":
            raise ValueError(_NEEDS_PLACEMENT)
        resolution = anchor.get("resolution")
        if (
            not isinstance(resolution, dict)
            or str(resolution.get("status") or "").lower() != "ready"
        ):
            raise ValueError(_NEEDS_PLACEMENT)
        required = {"take_id", "artifact_id", "take_content_hash", "transcript_hash", "take_frame", "take_sample_rate"}
        if not required <= resolution.keys():
            raise ValueError(_NEEDS_PLACEMENT)
    row, take = _take_row_for_cue(cue, snapshot, _manifest_rows(anchor_metadata))
    artifact = take["artifact"]
    take_path = paths.managed_path(str(artifact["relative_path"]))
    source_paths[f"take:{take['take_id']}"] = take_path
    start, end = _frame_bounds(
        row,
        metadata=anchor_metadata,
        source_is_base=source_is_base,
        base_rate=base_rate,
    )
    if start > end or end > base_frames:
        raise ValueError(_NEEDS_PLACEMENT)
    pin = {
        "kind": kind,
        "segment_id": row.get("segment_id"),
        "take_id": row.get("take_id"),
        "artifact_id": row.get("artifact_id"),
        "start_frame": start,
        "end_frame": end,
    }
    if kind in {"before_segment", "after_segment"}:
        return (start if kind == "before_segment" else end), pin

    resolution = anchor["resolution"]
    metadata = artifact.get("metadata_json") or {}
    synthesized_text = metadata.get("synthesized_text")
    if not isinstance(synthesized_text, str):
        raise ValueError(_NEEDS_PLACEMENT)
    transcript_hash = hashlib.sha256(synthesized_text.encode("utf-8")).hexdigest()
    take_hash = str(artifact.get("content_hash") or "")
    if (
        resolution.get("artifact_id") != artifact.get("id")
        or resolution.get("take_content_hash") != take_hash
        or resolution.get("transcript_hash") != transcript_hash
    ):
        raise ValueError(_NEEDS_PLACEMENT)
    path = take_path
    take_frames, take_rate, _take_channels = _read_take_wav(path)
    if sha256_file(path) != take_hash:
        raise ValueError(_NEEDS_PLACEMENT)
    sample_rate = _int(resolution.get("take_sample_rate"), "resolution.take_sample_rate", minimum=1)
    take_frame = _int(resolution.get("take_frame"), "resolution.take_frame")
    if sample_rate != take_rate or take_frame > take_frames:
        raise ValueError(_NEEDS_PLACEMENT)
    offset = round(take_frame * base_rate / take_rate)
    if offset > end - start:
        raise ValueError(_NEEDS_PLACEMENT)
    frame = start + offset
    pin.update(
        {
            "take_content_hash": take_hash,
            "transcript_hash": transcript_hash,
            "take_frame": take_frame,
            "take_sample_rate": take_rate,
            "take_frame_count": take_frames,
        }
    )
    return frame, pin


def _cue_snapshots(
    snapshot: dict[str, Any],
    *,
    anchor_metadata: dict[str, Any],
    source_is_base: bool,
    paths: DataPaths,
    rate: int,
    channels: int,
    base_frames: int,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Path]]:
    workflow = snapshot["session"]["workflow_kind"]
    source_paths: dict[str, Path] = {}
    prepared_cues: list[dict[str, Any]] = []
    for cue in snapshot["included_cues"]:
        cue_id = str(cue["logical_id"])
        if cue.get("placement_status") != "ready":
            raise ValueError(f"Sound cue {cue_id} needs placement. {_NEEDS_PLACEMENT}")
        variant = snapshot["variants"].get(str(cue["variant_id"]))
        if variant is None or variant.get("state") != "ready" or not variant.get("artifact_id"):
            raise ValueError(f"Sound cue {cue_id} must use a ready sound variant.")
        artifact = _require_audio_artifact(
            variant.get("artifact"),
            session_id=snapshot["session"]["id"],
            label="sound variant",
            global_artifact=True,
        )
        path = paths.managed_path(str(artifact["relative_path"]))
        source_paths[cue_id] = path
        playback = SoundPlayback.model_validate(cue["playback_json"] or {})
        mode = playback.mode or ("insert" if workflow == "audiobook" else "overlay")
        if mode == "insert" and workflow != "audiobook":
            raise ValueError("Insert sound cues are unavailable on a fixed timeline; select overlay.")
        if playback.offset_ms < 0 and mode == "insert":
            raise ValueError("Insert offset must be nonnegative.")
        kind = str(cue["anchor_json"].get("kind") or "")
        if kind == "source_time" and (workflow == "audiobook" or mode != "overlay"):
            raise ValueError("Source-time anchors require a timed workflow and overlay mode.")
        anchor_frame, anchor_pin = _resolve_anchor(
            cue,
            snapshot,
            anchor_metadata=anchor_metadata,
            source_is_base=source_is_base,
            base_rate=rate,
            base_frames=base_frames,
            paths=paths,
            source_paths=source_paths,
        )
        expected_source_frames = _variant_normalized_frames(variant, rate)
        if expected_source_frames is not None and playback.trim_end_seconds is not None:
            if round(playback.trim_end_seconds * rate) > expected_source_frames:
                raise ValueError(f"Sound cue {cue_id} trim exceeds the source audio duration.")
        prepared_cues.append(
            {
                "cue_id": cue_id,
                "variant_id": variant["id"],
                "artifact_id": artifact["id"],
                "content_hash": artifact["content_hash"],
                "source_path": path,
                "anchor_frame": anchor_frame,
                "anchor_pin": anchor_pin,
                "mode": mode,
                "offset_ms": playback.offset_ms,
                "gain_db": playback.gain_db,
                "trim_start_seconds": playback.trim_start_seconds,
                "trim_end_seconds": playback.trim_end_seconds,
                "fade_in_ms": playback.fade_in_ms,
                "fade_out_ms": playback.fade_out_ms,
                "playback_json": playback.model_dump(mode="json"),
                "anchor_json": deepcopy(cue["anchor_json"]),
                "expected_source_frames": expected_source_frames,
                "rate": rate,
                "channels": channels,
            }
        )
    return tuple(prepared_cues), source_paths
