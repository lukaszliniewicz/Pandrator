"""Conservative, audio-only suggestions for refining an aligned sound boundary."""

from __future__ import annotations

import math
from collections.abc import Sequence
from numbers import Integral, Real
from typing import Any, cast

import numpy as np

from .speech_performance import Anchor, anchor_range

BOUNDARY_REFINEMENT_VERSION = "sound-boundary-refinement-v1"
_BLOCK_MS = 10
_AFTER_WORD_BEFORE_MS = 120
_AFTER_WORD_AFTER_MS = 300
_SNAP_MS = 2
_MAX_CLIP_SECONDS = 6

Interval = tuple[int, int]


def locate_sound_anchor(
    text: str,
    quote: str,
    occurrence: int,
    *,
    start: int | None = None,
    end: int | None = None,
) -> tuple[int, int]:
    """Resolve an exact, grapheme-safe quote and optionally verify its offsets."""
    if not isinstance(text, str):
        raise ValueError("Anchor text must be a string.")
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("Anchor quote must contain non-whitespace text.")
    if isinstance(occurrence, bool) or not isinstance(occurrence, Integral) or occurrence < 1:
        raise ValueError("Anchor occurrence must be a positive integer.")
    if (start is None) != (end is None):
        raise ValueError("Explicit anchor start and end must be supplied together.")

    resolved = anchor_range(text, Anchor(quote=quote, occurrence=int(occurrence)))
    if start is not None or end is not None:
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, Integral)
            or not isinstance(end, Integral)
            or (int(start), int(end)) != resolved
        ):
            raise ValueError("Explicit anchor offsets must match the resolved occurrence.")
    return resolved


def refine_sound_boundary(
    samples: np.ndarray,
    *,
    sample_rate: int,
    estimated_end_frame: int,
    next_word_start_frame: int,
    word_intervals: Sequence[Sequence[int]],
    speech_intervals: Sequence[Sequence[int]] | None,
    kind: str = "after_word",
    quiet_dbfs: float = -55.0,
    min_quiet_ms: float = 80,
    guard_ms: float = 30,
    max_search_ms: float = 2000,
) -> dict[str, Any]:
    """Suggest only measured quiet boundaries; never edit audio or bypass pending."""
    if not isinstance(samples, np.ndarray) or samples.ndim != 1:
        raise ValueError("Samples must be a mono NumPy array.")
    if not np.issubdtype(samples.dtype, np.floating):
        raise ValueError("Samples must use a floating-point dtype.")
    if samples.size == 0:
        raise ValueError("Samples must not be empty.")
    if not _is_integral(sample_rate) or not 8_000 <= int(sample_rate) <= 192_000:
        raise ValueError("Sample rate must be an integer from 8000 through 192000 Hz.")
    rate = int(sample_rate)
    if samples.size > rate * _MAX_CLIP_SECONDS:
        raise ValueError("The analysis clip must not exceed six seconds.")
    frame_count = int(samples.size)
    if not bool(np.isfinite(samples).all()):
        raise ValueError("Samples must be finite.")
    if bool(np.any(samples < -1.0)) or bool(np.any(samples > 1.0)):
        raise ValueError("Samples must be within [-1, 1].")
    if kind not in {"after_word", "next_pause"}:
        raise ValueError("Kind must be 'after_word' or 'next_pause'.")

    estimate = _frame(estimated_end_frame, "estimated_end_frame", frame_count)
    next_start = _frame(next_word_start_frame, "next_word_start_frame", frame_count)
    words = _intervals(word_intervals, "word_intervals", frame_count)
    speech = (
        None
        if speech_intervals is None
        else _intervals(speech_intervals, "speech_intervals", frame_count)
    )
    quiet_level = _setting(quiet_dbfs, "quiet_dbfs", maximum=0)
    min_quiet = _setting(min_quiet_ms, "min_quiet_ms", minimum=0, strict_minimum=True)
    guard = _setting(guard_ms, "guard_ms", minimum=0)
    max_search = _setting(max_search_ms, "max_search_ms", minimum=0, maximum=2000)

    settings: dict[str, int | float | str] = {
        "kind": kind,
        "sample_rate": rate,
        "quiet_dbfs": quiet_level,
        "block_ms": _BLOCK_MS,
        "min_quiet_ms": min_quiet,
        "guard_ms": guard,
        "max_search_ms": max_search,
        "after_word_before_ms": _AFTER_WORD_BEFORE_MS,
        "after_word_after_ms": _AFTER_WORD_AFTER_MS,
        "snap_ms": _SNAP_MS,
    }
    evidence = {"vad": speech is not None, "energy": True, "word_guard": bool(words)}

    threshold = 10.0 ** (quiet_level / 20.0)
    blocks = _energy_blocks(samples, rate, threshold)
    guard_frames = math.ceil(guard * rate / 1000.0)
    min_quiet_frames = math.ceil(min_quiet * rate / 1000.0)
    if kind == "after_word":
        search_start = max(0, estimate - math.floor(_AFTER_WORD_BEFORE_MS * rate / 1000))
        search_end = min(
            frame_count,
            estimate + math.floor(_AFTER_WORD_AFTER_MS * rate / 1000),
            next_start,
        )
    else:
        search_start = estimate
        search_end = min(
            frame_count,
            estimate + math.floor(max_search * rate / 1000.0),
        )

    masks = _guarded_intervals(words, guard_frames, frame_count)
    if speech is not None:
        masks.extend(_guarded_intervals(speech, guard_frames, frame_count))
    gaps = _eligible_gaps(blocks, search_start, search_end, masks, min_quiet_frames)

    if speech is None:
        return _pending(
            "non_speech_evidence_unavailable", settings=settings, evidence=evidence
        )
    if not gaps:
        return _pending("no_safe_quiet_gap", settings=settings, evidence=evidence)

    gap_start, gap_end = min(
        gaps,
        key=lambda gap: (abs((gap[0] + gap[1]) / 2.0 - estimate), gap[0]),
    )
    chosen = _snap_inside_gap(samples, rate, gap_start, gap_end)
    return {
        "status": "ready",
        "chosen_frame": chosen,
        "gap_start_frame": gap_start,
        "gap_end_frame": gap_end,
        "shift_frames": chosen - estimate,
        "algorithm_version": BOUNDARY_REFINEMENT_VERSION,
        "settings": settings,
        "evidence": evidence,
    }


def _is_integral(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool)


def _frame(value: object, name: str, frame_count: int) -> int:
    if not _is_integral(value):
        raise ValueError(f"{name} must be an integer frame offset.")
    result = int(cast(Integral, value))
    if not 0 <= result <= frame_count:
        raise ValueError(f"{name} must be within 0..{frame_count}.")
    return result


def _setting(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strict_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    result = float(value)
    if minimum is not None and (result <= minimum if strict_minimum else result < minimum):
        raise ValueError(f"{name} is below its supported range.")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} is above its supported range.")
    return result


def _intervals(value: Sequence[Sequence[int]], name: str, frame_count: int) -> list[Interval]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an ordered sequence of frame intervals.")
    result: list[Interval] = []
    previous_start = -1
    for interval in value:
        if isinstance(interval, (str, bytes)) or not isinstance(interval, Sequence) or len(interval) != 2:
            raise ValueError(f"Each {name} entry must contain a start and end frame.")
        start = _frame(interval[0], f"{name} start", frame_count)
        end = _frame(interval[1], f"{name} end", frame_count)
        if start >= end:
            raise ValueError(f"Each {name} interval must have positive length.")
        if start < previous_start:
            raise ValueError(f"{name} intervals must be ordered by start frame.")
        previous_start = start
        result.append((start, end))
    return result


def _guarded_intervals(
    intervals: Sequence[Interval], guard_frames: int, frame_count: int
) -> list[Interval]:
    return [
        (max(0, start - guard_frames), min(frame_count, end + guard_frames))
        for start, end in intervals
    ]


def _energy_blocks(
    samples: np.ndarray, sample_rate: int, threshold: float
) -> list[tuple[int, int, bool]]:
    blocks: list[tuple[int, int, bool]] = []
    start = 0
    block_number = 1
    while start < samples.size:
        end = min(samples.size, (block_number * sample_rate + 50) // 100)
        if end <= start:
            end = start + 1
        block = samples[start:end]
        rms = math.sqrt(float(np.mean(np.square(block, dtype=np.float64))))
        blocks.append((start, end, rms <= threshold))
        start = end
        block_number += 1
    return blocks


def _eligible_gaps(
    blocks: Sequence[tuple[int, int, bool]],
    search_start: int,
    search_end: int,
    masks: Sequence[Interval],
    min_quiet_frames: int,
) -> list[Interval]:
    gaps: list[Interval] = []
    run_start: int | None = None
    run_end: int | None = None

    def finish_run() -> None:
        if (
            run_start is not None
            and run_end is not None
            and run_end - run_start >= min_quiet_frames
        ):
            gaps.append((run_start, run_end))

    for block_start, block_end, energy_quiet in blocks:
        start = max(block_start, search_start)
        end = min(block_end, search_end)
        masked = any(mask_start < end and mask_end > start for mask_start, mask_end in masks)
        eligible = start < end and energy_quiet and not masked
        if eligible and run_end == start:
            run_end = end
            continue
        if run_start is not None:
            finish_run()
        run_start, run_end = (start, end) if eligible else (None, None)
    finish_run()
    return gaps


def _snap_inside_gap(
    samples: np.ndarray, sample_rate: int, gap_start: int, gap_end: int
) -> int:
    center = (gap_start + gap_end) // 2
    radius = math.floor(_SNAP_MS * sample_rate / 1000)
    first = max(gap_start + 1, center - radius)
    last = min(gap_end - 1, center + radius)
    relative = int(np.argmin(np.abs(samples[first : last + 1])))
    return first + relative


def _pending(
    reason: str,
    *,
    settings: dict[str, int | float | str],
    evidence: dict[str, bool],
) -> dict[str, Any]:
    return {
        "status": "needs_placement",
        "reason": reason,
        "chosen_frame": None,
        "algorithm_version": BOUNDARY_REFINEMENT_VERSION,
        "settings": settings,
        "evidence": evidence,
    }
