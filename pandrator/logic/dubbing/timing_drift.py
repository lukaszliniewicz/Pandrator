"""Late-cue and block-lag measures for placed speech blocks.

A subtitle cue is *late* when its display start falls after the end of the
speech of the block that covers it.  One continuous take that spans several
cues cannot keep a pause between them, so later cues can appear after the
words were already spoken.  The measure is deterministic and needs only
placement and cue windows, so timing repair and drift reporting share it
without reading audio.

Block lag is the other half of the same story: ``drift_before_ms`` in an
alignment detail is how late a block's speech is pushed by the speech before
it.  :func:`block_lag_metrics` summarises a whole assembly's details, and
:func:`drift_warnings` turns those numbers into one export-plan warning.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

DRIFT_WARN_ANY_BLOCK_LATE_MS = 10_000
DRIFT_WARN_LATE_BLOCK_SHARE = 0.05
LATE_CUE_WARN_SHARE = 0.05


@dataclass(frozen=True)
class PlacedSpeechBlock:
    """One block's speech span and the display cues it covers, in one timeline."""

    start_ms: int
    end_ms: int
    cues: Sequence[tuple[int, int]] = field(default_factory=tuple)


def late_cue_metrics(blocks: Iterable[PlacedSpeechBlock]) -> dict[str, int]:
    """Count cues that start after their block's speech, and the worst lag.

    ``start_ms``/``end_ms`` are the placed speech span; a cue's own start is
    late only when it is strictly after ``end_ms``, so a cue that begins in the
    same millisecond the speech ends is not counted.  The result also carries
    the number of blocks and cues examined, so a caller can report a rate
    instead of a bare count.
    """

    late = 0
    worst_lag = 0
    cues_seen = 0
    blocks_seen = 0
    for block in blocks:
        blocks_seen += 1
        for cue_start, _cue_end in block.cues:
            cues_seen += 1
            lag = int(cue_start) - int(block.end_ms)
            if lag > 0:
                late += 1
                worst_lag = max(worst_lag, lag)
    return {
        "late_cues": late,
        "worst_lag_ms": worst_lag,
        "cues": cues_seen,
        "blocks": blocks_seen,
    }


def _milliseconds(value: object) -> float:
    """Read one stored millisecond figure; anything unusable counts as zero."""

    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def block_lag_metrics(details: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise one assembly's per-block placement details.

    The details are the ``synchronization.blocks`` rows an assembly stores.
    Late-cue figures are only reported when every row carries them: older
    assemblies have no cue windows, and a partial sum would read as a smaller
    problem than it is.  A row names its own block in ``block``; the worst one
    is named only when some block is actually late.
    """

    blocks = list(details or ())
    lags = [_milliseconds(item.get("drift_before_ms")) for item in blocks]
    worst_lag = max(lags, default=0.0)
    worst_block = None
    if worst_lag > 0:
        worst_block = blocks[lags.index(worst_lag)].get("block")
    cue_counts_known = bool(blocks) and all(
        "late_cue_count" in item for item in blocks
    )
    return {
        "worst_block_lag_ms": int(worst_lag),
        "worst_block": None if worst_block is None else str(worst_block),
        "blocks_late_over_1s": sum(1 for lag in lags if lag > 1_000),
        "blocks_late_over_3s": sum(1 for lag in lags if lag > 3_000),
        "blocks_late_over_10s": sum(
            1 for lag in lags if lag > DRIFT_WARN_ANY_BLOCK_LATE_MS
        ),
        "final_drift_ms": (
            int(_milliseconds(blocks[-1].get("drift_after_ms"))) if blocks else 0
        ),
        "blocks": len(blocks),
        "late_cues": (
            sum(int(_milliseconds(item.get("late_cue_count"))) for item in blocks)
            if cue_counts_known
            else None
        ),
        "worst_cue_lag_ms": (
            int(max(_milliseconds(item.get("worst_cue_lag_ms")) for item in blocks))
            if cue_counts_known
            else None
        ),
        "cues": (
            sum(int(_milliseconds(item.get("cue_count"))) for item in blocks)
            if cue_counts_known and all("cue_count" in item for item in blocks)
            else None
        ),
    }


def drift_warnings(metrics: Mapping[str, Any], *, speed: object) -> list[str]:
    """Name a drift or a late-cue problem that a plan should disclose early."""

    warnings: list[str] = []
    worst_lag = _milliseconds(metrics.get("worst_block_lag_ms"))
    block_count = int(_milliseconds(metrics.get("blocks")))
    late_blocks = int(_milliseconds(metrics.get("blocks_late_over_3s")))
    late_share = late_blocks / block_count if block_count else 0.0
    if worst_lag > DRIFT_WARN_ANY_BLOCK_LATE_MS or (
        late_share >= DRIFT_WARN_LATE_BLOCK_SHARE
    ):
        warnings.append(
            "Speech drifts behind the subtitles: the worst block starts "
            f"{worst_lag / 1000:.1f} s late, and {late_blocks} of {block_count} blocks "
            f"start more than 3 s late. Raising synchronization_speed (now {speed}) "
            "helps; above 1.2 speech may sound rushed, so for drift this large, "
            "condensing the text is the better fix."
        )
    late_cues = metrics.get("late_cues")
    cue_count = metrics.get("cues")
    if (
        isinstance(late_cues, (int, float))
        and isinstance(cue_count, (int, float))
        and cue_count > 0
        and late_cues / cue_count >= LATE_CUE_WARN_SHARE
    ):
        worst_cue_lag = _milliseconds(metrics.get("worst_cue_lag_ms"))
        warnings.append(
            f"{int(late_cues)} of {int(cue_count)} subtitle cues appear after their "
            f"speech has ended (worst {worst_cue_lag / 1000:.1f} s), inside speech "
            "blocks that merge several cues. Timing repair (repair-timing) splits "
            "and regenerates such blocks."
        )
    return warnings


__all__ = [
    "DRIFT_WARN_ANY_BLOCK_LATE_MS",
    "DRIFT_WARN_LATE_BLOCK_SHARE",
    "LATE_CUE_WARN_SHARE",
    "PlacedSpeechBlock",
    "block_lag_metrics",
    "drift_warnings",
    "late_cue_metrics",
]
