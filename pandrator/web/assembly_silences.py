"""Pure translation between raw render manifests and assembly-only pauses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from .audio_assembly import AudioSilenceInsertion

SILENCE_POLICY = "assembly-v1"


def render_part_silences(
    metadata: Mapping[str, Any] | None,
    audio_settings: Mapping[str, Any] | None = None,
) -> tuple[AudioSilenceInsertion, ...]:
    """Read only explicitly gap-free takes; unmarked historical takes stay baked."""
    parts = (metadata or {}).get("render_parts")
    if not isinstance(parts, list) or not parts:
        return ()
    if not any(isinstance(part, Mapping) and part.get("silence_policy") == SILENCE_POLICY for part in parts):
        return ()
    previous_end = 0
    source_rate = None
    result = []
    for index, part in enumerate(parts):
        if not isinstance(part, Mapping) or part.get("silence_policy") != SILENCE_POLICY:
            raise ValueError("Mixed raw and legacy render silence policies cannot be mapped.")
        start, end, rate = (part.get(key) for key in ("start_frame", "end_frame", "sample_rate_hz"))
        if (type(start) is not int or type(end) is not int or type(rate) is not int
                or rate <= 0 or start != previous_end or end <= start
                or (source_rate is not None and rate != source_rate)):
            raise ValueError("Assembly-only render parts require contiguous source PCM frame coordinates.")
        requested = part.get("silence_before_ms", 0)
        if index:
            if audio_settings is not None:
                requested = audio_settings.get("in_segment_voice_change_silence_ms",
                                               audio_settings.get("voice_change_silence_ms", requested))
            insertion = AudioSilenceInsertion(start, rate, requested)
            if insertion.duration_ms:
                result.append(insertion)
        previous_end, source_rate = end, rate
    return tuple(result)


def silence_manifest(insertions: tuple[AudioSilenceInsertion, ...]) -> list[dict[str, int]]:
    """Return path-free, JSON-safe durable assembly instructions."""
    return [asdict(item) for item in insertions]


def frozen_silences(row: Mapping[str, Any]) -> tuple[AudioSilenceInsertion, ...]:
    """Validate frozen instructions without recomputing current settings."""
    values = row.get("internal_silences", [])
    if not isinstance(values, list):
        raise ValueError("Frozen internal silences must be a list.")
    if values and row.get("silence_policy") != SILENCE_POLICY:
        raise ValueError("Frozen internal silences require the assembly-v1 policy.")
    try:
        insertions = tuple(AudioSilenceInsertion(**value) for value in values)
    except (TypeError, KeyError) as error:
        raise ValueError("Invalid frozen silence insertion.") from error
    if any(a.offset_frames * b.sample_rate_hz > b.offset_frames * a.sample_rate_hz
           for a, b in zip(insertions, insertions[1:], strict=False)):
        raise ValueError("Frozen silence offsets must be sorted.")
    return insertions


def remap_render_parts_after_rvc(
    parts: list[dict[str, Any]], *, source_frames: int, source_rate: int,
    target_frames: int, target_rate: int, preserve_assembly_boundaries: bool = True,
) -> list[dict[str, Any]]:
    """Retain reliable audiobook boundaries; discard timing for timed workflows."""
    from copy import deepcopy

    result = deepcopy(parts)
    marked = preserve_assembly_boundaries and any(part.get("silence_policy") == SILENCE_POLICY for part in result)
    if marked:
        render_part_silences({"render_parts": result})
        if result[-1]["end_frame"] != source_frames or result[-1]["sample_rate_hz"] != source_rate:
            raise ValueError("RVC source render parts do not match the source PCM frames.")
        if len(result) > 1 and source_frames * target_rate != target_frames * source_rate:
            raise ValueError("RVC changed raw take duration; assembly-only voice boundaries cannot be mapped safely.")
    for part in result:
        part.pop("speech_timing", None)
        if marked:
            if len(result) == 1:
                part["start_frame"], part["end_frame"] = 0, target_frames
                part["duration_ms"] = round(target_frames * 1000 / target_rate)
            else:
                for key in ("start_frame", "end_frame"):
                    part[key] = round(part[key] * target_rate / source_rate)
            part["sample_rate_hz"] = target_rate
        else:
            for key in ("start_frame", "end_frame", "sample_rate_hz"):
                part.pop(key, None)
            if not preserve_assembly_boundaries:
                part.pop("silence_policy", None)
    if marked:
        render_part_silences({"render_parts": result})
    return result
