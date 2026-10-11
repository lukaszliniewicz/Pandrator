"""Frame-exact, path-free plans for mixing sound effects with speech audio."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer greater than or equal to {minimum}.")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value


def _gain(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("gain_db must be a finite number between -60 and 12.")
    result = float(value)
    if not math.isfinite(result) or not -60.0 <= result <= 12.0:
        raise ValueError("gain_db must be a finite number between -60 and 12.")
    return 0.0 if result == 0 else result


def _normalize_clip(raw: dict[str, Any], base_frames: int) -> dict[str, Any]:
    required = {
        "cue_id",
        "variant_id",
        "content_hash",
        "anchor_frame",
        "mode",
        "offset_frames",
        "source_frames",
    }
    optional = {
        "trim_start_frame",
        "trim_end_frame",
        "gain_db",
        "fade_in_frames",
        "fade_out_frames",
    }
    if not isinstance(raw, dict):
        raise ValueError("Every clip must be an object.")
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"Clip is missing required fields: {', '.join(sorted(missing))}.")
    extra = raw.keys() - required - optional
    if extra:
        names = ", ".join(sorted(str(key) for key in extra))
        raise ValueError(f"Clip has unsupported fields: {names}.")

    cue_id = _identifier(raw["cue_id"], "cue_id")
    variant_id = _identifier(raw["variant_id"], "variant_id")
    content_hash = _identifier(raw["content_hash"], "content_hash")
    anchor = _integer(raw["anchor_frame"], "anchor_frame")
    if anchor > base_frames:
        raise ValueError("anchor_frame must be within the base audio.")
    mode = raw["mode"]
    if not isinstance(mode, str) or mode not in {"insert", "overlay"}:
        raise ValueError("mode must be 'insert' or 'overlay'.")
    offset = (
        _integer(raw["offset_frames"], "offset_frames")
        if mode == "insert"
        else raw["offset_frames"]
    )
    if type(offset) is not int:
        raise ValueError("offset_frames must be an integer.")
    source_frames = _integer(raw["source_frames"], "source_frames", minimum=1)
    trim_start = _integer(raw.get("trim_start_frame", 0), "trim_start_frame")
    raw_trim_end = raw.get("trim_end_frame")
    trim_end = source_frames if raw_trim_end is None else _integer(raw_trim_end, "trim_end_frame")
    if trim_start >= trim_end or trim_end > source_frames:
        raise ValueError("Clip trims must select a non-empty range within source_frames.")
    trim_frames = trim_end - trim_start
    fade_in = _integer(raw.get("fade_in_frames", 0), "fade_in_frames")
    fade_out = _integer(raw.get("fade_out_frames", 0), "fade_out_frames")
    if fade_in > trim_frames or fade_out > trim_frames:
        raise ValueError("Fade lengths cannot exceed the trimmed clip duration.")

    return {
        "cue_id": cue_id,
        "variant_id": variant_id,
        "content_hash": content_hash,
        "anchor_frame": anchor,
        "mode": mode,
        "offset_frames": offset,
        "source_frames": source_frames,
        "trim_start_frame": trim_start,
        "trim_end_frame": trim_end,
        "gain_db": _gain(raw.get("gain_db", 0)),
        "fade_in_frames": fade_in,
        "fade_out_frames": fade_out,
    }


def _signature(plan_values: dict[str, Any]) -> str:
    serialized = json.dumps(
        plan_values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def build_sound_mix_plan(
    *,
    base_frames: int,
    rate: int,
    channels: int,
    base_content_hash: str,
    clips: list[dict[str, Any]],
    fixed_timeline: bool = False,
) -> dict[str, Any]:
    """Resolve clip timing into a canonical plan containing no file paths."""

    base_frames = _integer(base_frames, "base_frames", minimum=1)
    rate = _integer(rate, "rate", minimum=8000)
    if rate > 192000:
        raise ValueError("rate must not exceed 192000 Hz.")
    if type(channels) is not int or channels not in {1, 2}:
        raise ValueError("channels must be one or two.")
    if type(fixed_timeline) is not bool:
        raise ValueError("fixed_timeline must be a boolean.")
    base_content_hash = _identifier(base_content_hash, "base_content_hash")
    if not isinstance(clips, list):
        raise ValueError("clips must be a list.")

    normalized = [_normalize_clip(clip, base_frames) for clip in clips]
    cue_ids = [clip["cue_id"] for clip in normalized]
    if len(set(cue_ids)) != len(cue_ids):
        raise ValueError("cue_id values must be unique.")
    if fixed_timeline and any(clip["mode"] == "insert" for clip in normalized):
        raise ValueError("A fixed timeline cannot contain insert clips.")

    inserts_by_anchor: dict[int, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, clip in enumerate(normalized):
        if clip["mode"] == "insert":
            inserts_by_anchor[clip["anchor_frame"]].append((index, clip))

    insertions: list[dict[str, int]] = []
    for anchor, rows in sorted(inserts_by_anchor.items()):
        for _index, clip in rows:
            added = clip["offset_frames"] + (clip["trim_end_frame"] - clip["trim_start_frame"])
            insertions.append({"base_frame": anchor, "added_frames": added})

    total_inserted = sum(item["added_frames"] for item in insertions)
    total_frames = base_frames + total_inserted
    pieces: list[dict[str, int | None]] = []
    resolved_by_index: dict[int, dict[str, Any]] = {}
    output_cursor = 0
    base_cursor = 0

    def append_piece(
        length: int,
        source_start: int | None,
        source_end: int | None,
    ) -> None:
        nonlocal output_cursor
        if length <= 0:
            return
        pieces.append(
            {
                "output_start_frame": output_cursor,
                "output_end_frame": output_cursor + length,
                "source_start_frame": source_start,
                "source_end_frame": source_end,
            }
        )
        output_cursor += length

    for anchor, rows in sorted(inserts_by_anchor.items()):
        speech_length = anchor - base_cursor
        append_piece(speech_length, base_cursor, anchor)
        base_cursor = anchor
        for index, clip in rows:
            offset = clip["offset_frames"]
            if offset:
                append_piece(offset, None, None)
            clip_start = output_cursor
            clip_length = clip["trim_end_frame"] - clip["trim_start_frame"]
            # The speech bed is silent while the inserted effect is additive.
            append_piece(clip_length, None, None)
            clip_end = output_cursor
            resolved_by_index[index] = _resolved_clip(clip, clip_start, clip_end)
    append_piece(base_frames - base_cursor, base_cursor, base_frames)
    if output_cursor != total_frames:
        raise RuntimeError("Internal sound-mix plan length mismatch.")

    for index, clip in enumerate(normalized):
        if clip["mode"] != "overlay":
            continue
        anchor_output = clip["anchor_frame"] + sum(
            item["added_frames"]
            for item in insertions
            if item["base_frame"] <= clip["anchor_frame"]
        )
        start = anchor_output + clip["offset_frames"]
        end = start + clip["trim_end_frame"] - clip["trim_start_frame"]
        if start < 0:
            raise ValueError("Overlay clips cannot start before frame zero.")
        if fixed_timeline and end > base_frames:
            raise ValueError("A fixed-timeline overlay must fit within the base duration.")
        if end > total_frames:
            raise ValueError("Overlay clips cannot extend beyond the output timeline.")
        resolved_by_index[index] = _resolved_clip(clip, start, end)

    resolved_clips = [resolved_by_index[index] for index in range(len(normalized))]
    plan_values: dict[str, Any] = {
        "total_frames": total_frames,
        "rate": rate,
        "channels": channels,
        "base_frames": base_frames,
        "base_content_hash": base_content_hash,
        "pieces": pieces,
        "clips": resolved_clips,
        "insertions": insertions,
    }
    signature_inputs = {
        "base_content_hash": base_content_hash,
        "base_frames": base_frames,
        "rate": rate,
        "channels": channels,
        "fixed_timeline": fixed_timeline,
        "source_clips": normalized,
        "pieces": pieces,
        "clips": resolved_clips,
        "insertions": insertions,
        "total_frames": total_frames,
    }
    plan_values["mix_signature"] = _signature(signature_inputs)
    return plan_values


def _resolved_clip(clip: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    return {
        "cue_id": clip["cue_id"],
        "variant_id": clip["variant_id"],
        "content_hash": clip["content_hash"],
        "source_frames": clip["source_frames"],
        "start_frame": start,
        "end_frame": end,
        "trim_start_frame": clip["trim_start_frame"],
        "trim_end_frame": clip["trim_end_frame"],
        "gain_db": clip["gain_db"],
        "fade_in_frames": clip["fade_in_frames"],
        "fade_out_frames": clip["fade_out_frames"],
        "mode": clip["mode"],
    }


def map_base_frame(plan: dict[str, Any], frame: int, *, bias: str = "after") -> int:
    """Map a base frame across insertions using a start/end boundary bias."""

    base_frames = _integer(plan.get("base_frames"), "plan.base_frames", minimum=1)
    frame = _integer(frame, "frame")
    if frame > base_frames:
        raise ValueError("frame must be within the base audio.")
    if not isinstance(bias, str) or bias not in {"after", "before"}:
        raise ValueError("bias must be 'after' or 'before'.")
    insertions = plan.get("insertions")
    if not isinstance(insertions, list):
        raise ValueError("plan.insertions must be a list.")
    mapped = frame
    for insertion in insertions:
        if not isinstance(insertion, dict):
            raise ValueError("plan.insertions entries must be objects.")
        anchor = _integer(insertion.get("base_frame"), "insertion.base_frame")
        added = _integer(insertion.get("added_frames"), "insertion.added_frames", minimum=1)
        if anchor > base_frames:
            raise ValueError("insertion.base_frame must be within the base audio.")
        if anchor < frame or (anchor == frame and bias == "after"):
            mapped += added
    return mapped


def remap_audio_metadata(metadata: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Copy artifact metadata and map supported base-audio coordinates."""

    from copy import deepcopy

    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object.")
    copied = deepcopy(metadata)
    base_frames = _integer(plan.get("base_frames"), "plan.base_frames", minimum=1)
    total_frames = _integer(plan.get("total_frames"), "plan.total_frames", minimum=1)
    rate = _integer(plan.get("rate"), "plan.rate", minimum=1)
    duration_ms = int(round(total_frames * 1000 / rate))

    timeline = copied.get("audio_timeline")
    if timeline is None:
        timeline = {}
        copied["audio_timeline"] = timeline
    if not isinstance(timeline, dict):
        raise ValueError("audio_timeline must be an object when present.")
    recorded_rate = timeline.get("sample_rate_hz")
    if (
        recorded_rate is not None
        and _integer(recorded_rate, "audio_timeline.sample_rate_hz", minimum=1) != rate
    ):
        raise ValueError("audio_timeline.sample_rate_hz does not match the sound-mix plan.")
    timeline["sample_rate_hz"] = rate
    timeline["total_frames"] = total_frames
    copied["duration_ms"] = duration_ms

    takes = copied.get("takes")
    if takes is not None:
        if not isinstance(takes, list):
            raise ValueError("takes must be a list when present.")
        for index, take in enumerate(takes):
            if not isinstance(take, dict):
                raise ValueError(f"takes[{index}] must be an object.")
            has_start = "start_frame" in take
            has_end = "end_frame" in take
            if has_start != has_end:
                raise ValueError(f"takes[{index}] has incomplete frame coordinates.")
            if not has_start:
                continue
            start = _integer(take["start_frame"], f"takes[{index}].start_frame")
            end = _integer(take["end_frame"], f"takes[{index}].end_frame")
            if start > end or end > base_frames:
                raise ValueError(f"takes[{index}] frame coordinates are outside the base audio.")
            if start == end:
                mapped_start = mapped_end = map_base_frame(plan, start, bias="after")
            else:
                mapped_start = map_base_frame(plan, start, bias="after")
                mapped_end = map_base_frame(plan, end, bias="before")
            take["start_frame"] = mapped_start
            take["end_frame"] = mapped_end
            take["base_start_frame"] = start
            take["base_end_frame"] = end

    chapters = copied.get("chapters")
    if chapters is not None:
        if not isinstance(chapters, list):
            raise ValueError("chapters must be a list when present.")
        for index, chapter in enumerate(chapters):
            if not isinstance(chapter, dict):
                raise ValueError(f"chapters[{index}] must be an object.")
            if "start_ms" not in chapter:
                continue
            start_ms = chapter["start_ms"]
            if isinstance(start_ms, bool) or not isinstance(start_ms, (int, float)):
                raise ValueError(f"chapters[{index}].start_ms must be numeric.")
            if not math.isfinite(float(start_ms)) or start_ms < 0:
                raise ValueError(f"chapters[{index}].start_ms must be finite and non-negative.")
            source_frame = int(round(float(start_ms) * rate / 1000))
            if source_frame > base_frames:
                raise ValueError(f"chapters[{index}].start_ms is outside the base audio.")
            mapped = map_base_frame(plan, source_frame, bias="after")
            chapter["start_ms"] = int(round(mapped * 1000 / rate))
    return copied
