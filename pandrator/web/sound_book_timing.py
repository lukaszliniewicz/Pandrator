"""Rebase frozen book timing across path-free sound-mix frame plans."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pandrator.logic.book_timing import AlignedBookText, BookWord

from .sound_mix_plan import map_base_frame


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(f"Sound-mix {name} must be an integer.")
    return value


def _frame_plan(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    if "sound_mix" not in metadata:
        return None
    sound_mix = metadata.get("sound_mix")
    if not isinstance(sound_mix, Mapping):
        raise ValueError("Mixed book metadata must contain a sound_mix object.")
    plan = sound_mix.get("frame_plan")
    if not isinstance(plan, dict):
        raise ValueError("Mixed book metadata must contain its frame_plan.")

    rate = _integer(plan.get("rate"), "frame-plan rate", positive=True)
    if not 8000 <= rate <= 192000:
        raise ValueError("Sound-mix frame-plan rate is outside the supported range.")
    base_frames = _integer(plan.get("base_frames"), "base frame count", positive=True)
    total_frames = _integer(plan.get("total_frames"), "total frame count", positive=True)
    insertions = plan.get("insertions")
    if not isinstance(insertions, list):
        raise ValueError("Sound-mix frame-plan insertions must be a list.")

    added_frames = 0
    for insertion in insertions:
        if not isinstance(insertion, Mapping):
            raise ValueError("Sound-mix frame-plan insertions must be objects.")
        anchor = _integer(insertion.get("base_frame"), "insertion base frame")
        added = _integer(insertion.get("added_frames"), "insertion length", positive=True)
        if anchor > base_frames:
            raise ValueError("Sound-mix insertion is outside the base audio.")
        added_frames += added
    if total_frames != base_frames + added_frames:
        raise ValueError("Sound-mix frame-plan total does not match its base and insertions.")

    timeline = metadata.get("audio_timeline")
    if not isinstance(timeline, Mapping):
        raise ValueError("Mixed book metadata has no audio timeline.")
    if (
        _integer(timeline.get("sample_rate_hz"), "audio sample rate", positive=True) != rate
        or _integer(timeline.get("total_frames"), "audio frame count", positive=True)
        != total_frames
    ):
        raise ValueError("Mixed book audio timeline does not match its frame plan.")

    base_timeline = sound_mix.get("base_audio_timeline")
    if base_timeline is not None:
        if not isinstance(base_timeline, Mapping):
            raise ValueError("Mixed book base audio timeline must be an object.")
        if (
            _integer(base_timeline.get("sample_rate_hz"), "base sample rate", positive=True)
            != rate
            or _integer(base_timeline.get("total_frames"), "base audio frame count", positive=True)
            != base_frames
        ):
            raise ValueError("Mixed book base timeline does not match its frame plan.")

    return plan


def _row_frame(value: Any, name: str, *, positive: bool = False) -> int:
    return _integer(value, name, positive=positive)


def take_base_bounds(metadata: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[int, int]:
    """Return a take's original base-audio bounds after validating its mapped row."""

    if not isinstance(metadata, Mapping) or not isinstance(row, Mapping):
        raise ValueError("Book audio metadata and take rows must be objects.")
    start = _row_frame(row.get("start_frame"), "take start frame")
    end = _row_frame(row.get("end_frame"), "take end frame", positive=True)
    if end <= start:
        raise ValueError("Book take frame bounds must have positive length.")

    plan = _frame_plan(metadata)
    if plan is None:
        return start, end

    base_start = _row_frame(row.get("base_start_frame"), "take base start frame")
    base_end = _row_frame(row.get("base_end_frame"), "take base end frame", positive=True)
    base_frames = _integer(plan.get("base_frames"), "base frame count", positive=True)
    total_frames = _integer(plan.get("total_frames"), "total frame count", positive=True)
    if base_start >= base_end or base_end > base_frames:
        raise ValueError("Book take base frame bounds are outside the original audio.")
    if start >= end or end > total_frames:
        raise ValueError("Book take mapped frame bounds are outside the mixed audio.")

    expected_start = map_base_frame(plan, base_start, bias="after")
    expected_end = map_base_frame(plan, base_end, bias="before")
    if (start, end) != (expected_start, expected_end):
        raise ValueError("Book take mapped bounds do not match the sound-mix frame plan.")
    return base_start, base_end


def rebase_book_words(
    aligned: AlignedBookText,
    row: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    cue_mode: str,
) -> tuple[BookWord, ...]:
    """Map spoken words onto the assembled timeline without spanning inserted audio."""

    if not isinstance(aligned, AlignedBookText):
        raise ValueError("Book timing must be validated aligned text.")
    base_start, base_end = take_base_bounds(metadata, row)
    mapped_start = _row_frame(row.get("start_frame"), "take start frame")
    mapped_end = _row_frame(row.get("end_frame"), "take end frame", positive=True)
    plan = _frame_plan(metadata)
    if plan is None:
        timeline = metadata.get("audio_timeline")
        if not isinstance(timeline, Mapping):
            raise ValueError("Book audio metadata has no audio timeline.")
        rate = _integer(timeline.get("sample_rate_hz"), "audio sample rate", positive=True)
    else:
        rate = _integer(plan.get("rate"), "frame-plan rate", positive=True)

    if cue_mode not in {"segments", "passages"}:
        raise ValueError("Book cue mode must be segments or passages.")
    if cue_mode == "segments":
        return (
            BookWord(
                aligned.text,
                round(mapped_start * 1000 / rate),
                round(mapped_end * 1000 / rate),
                0,
                len(aligned.text),
            ),
        )

    if plan is None:
        offset_ms = mapped_start * 1000 / rate
        words = tuple(
            BookWord(
                word.text,
                round(offset_ms + word.start_ms),
                round(offset_ms + word.end_ms),
                word.start_char,
                word.end_char,
            )
            for word in aligned.words
        )
        bound_ms = mapped_end * 1000 / rate
        if any(word.start_ms < round(offset_ms) or word.end_ms > bound_ms + 1 for word in words):
            raise ValueError("Rebased book timing exceeds its take bounds.")
        return words

    local_frames = base_end - base_start
    local_duration_ms = local_frames * 1000 / rate
    rounded_duration_ms = round(local_duration_ms)
    words = []
    for index, word in enumerate(aligned.words):
        if word.start_ms * rate / 1000 > local_frames:
            raise ValueError("Book word starts outside its original take.")
        final_word = index == len(aligned.words) - 1
        end_excess_ms = word.end_ms - local_duration_ms
        if end_excess_ms > 0 and (not final_word or end_excess_ms > 1):
            raise ValueError("Book word ends outside its original take.")
        if word.end_ms > rounded_duration_ms + 1:
            raise ValueError("Book word ends outside its original take.")

        source_start = base_start + round(word.start_ms * rate / 1000)
        source_end = base_start + round(word.end_ms * rate / 1000)
        if source_start < base_start or source_start >= base_end:
            raise ValueError("Book word starts outside its original take.")
        if source_end > base_end:
            frame_excess_ms = (source_end - base_end) * 1000 / rate
            if not final_word or abs(end_excess_ms) > 1 or frame_excess_ms > 1:
                raise ValueError("Book word ends outside its original take.")
            source_end = base_end
        if source_end <= source_start:
            raise ValueError("Book word has no duration in the original take.")

        output_start = map_base_frame(plan, source_start, bias="after")
        output_end = map_base_frame(plan, source_end, bias="before")
        if output_start < mapped_start or output_end > mapped_end or output_end <= output_start:
            raise ValueError("Book word timing exceeds its mapped take bounds.")
        words.append(
            BookWord(
                word.text,
                round(output_start * 1000 / rate),
                round(output_end * 1000 / rate),
                word.start_char,
                word.end_char,
            )
        )
    return tuple(words)
