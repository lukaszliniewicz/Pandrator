"""Late-cue measures for placed speech blocks.

A subtitle cue is *late* when its display start falls after the end of the
speech of the block that covers it.  One continuous take that spans several
cues cannot keep a pause between them, so later cues can appear after the
words were already spoken.  The measure is deterministic and needs only
placement and cue windows, so timing repair and drift reporting share it
without reading audio.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


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


__all__ = ["PlacedSpeechBlock", "late_cue_metrics"]
