"""Bounded exceptions for hesitations inside an unfinished phrase.

A source pause is timing evidence, not proof of a natural TTS boundary. Normal
clause/sentence pauses keep their ordinary threshold. Only an unpunctuated
continuation may use the larger allowance, without chaining exceptions into a
long paragraph or guessing new word timestamps.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .natural_boundaries import classify_boundary

DEFAULT_PASSAGE_GAP_MS = 1500
DEFAULT_CONTINUATION_GAP_MS = 3000
MAX_CONTINUATION_SPAN_MS = 30_000


def may_bridge_unfinished_pause(
    left_text: str,
    right_text: str,
    gap_ms: int,
    *,
    ordinary_gap_ms: int,
    continuation_gap_ms: int,
    language_code: str = "en",
    bridged_pause_ms: int = 0,
    combined_span_ms: int | None = None,
) -> bool:
    """Allow an exceptional join, not ordinary packing or a speaker change.

    Callers must separately require the same trustworthy speaker and adjacent,
    non-overlapping evidence. An explicit zero allowance disables this path.
    Both text variants should pass when display and spoken text differ.
    """
    if not left_text.strip() or not right_text.strip():
        return False
    if not (0 < ordinary_gap_ms < gap_ms <= continuation_gap_ms):
        return False
    if bridged_pause_ms < 0 or bridged_pause_ms + gap_ms > continuation_gap_ms:
        return False
    if combined_span_ms is not None and not (0 < combined_span_ms <= MAX_CONTINUATION_SPAN_MS):
        return False
    return classify_boundary(left_text, right_text, language_code=language_code) is None


def validate_logical_merge_pauses(
    selected: Sequence[Mapping[str, Any]], *, language_code: str = "",
) -> None:
    """Enforce one pause contract for correction and translation merges.

    Ordinary short seams retain the historical allowance. Moderate hesitations
    may be collapsed only inside an unfinished phrase, across identical source
    speakers, within a cumulative pause budget and a bounded timing envelope.
    IDs, text, speaker correction, and coverage are validated by the callers.
    """
    if len(selected) < 2:
        return

    def window(row: Mapping[str, Any]) -> tuple[int, int]:
        return (
            int(row["start_ms"]) if row.get("start_ms") is not None else round(float(row.get("start", 0)) * 1000),
            int(row["end_ms"]) if row.get("end_ms") is not None else round(float(row.get("end", 0)) * 1000),
        )

    windows = [window(row) for row in selected]
    total_span = max(end for _, end in windows) - min(start for start, _ in windows)
    bridged = 0
    for index, (left, right) in enumerate(zip(selected, selected[1:])):
        gap = windows[index + 1][0] - windows[index][1]
        if gap < 0:
            raise ValueError("Logical passage groups cannot merge overlapping cues.")
        if gap <= DEFAULT_PASSAGE_GAP_MS:
            continue
        if str(left.get("speaker") or "").strip().casefold() != str(right.get("speaker") or "").strip().casefold():
            raise ValueError("An unfinished-pause merge cannot cross differing source speakers.")
        if not may_bridge_unfinished_pause(
            str(left.get("text") or ""), str(right.get("text") or ""), gap,
            ordinary_gap_ms=DEFAULT_PASSAGE_GAP_MS,
            continuation_gap_ms=DEFAULT_CONTINUATION_GAP_MS,
            language_code=language_code or str(left.get("_source_language") or ""),
            bridged_pause_ms=bridged,
            combined_span_ms=total_span,
        ):
            raise ValueError(
                f"Logical passage groups cannot cross this {gap} ms gap: "
                f"above {DEFAULT_PASSAGE_GAP_MS} ms, only an unfinished same-speaker "
                f"phrase within {DEFAULT_CONTINUATION_GAP_MS} ms cumulative hesitation "
                f"and a {MAX_CONTINUATION_SPAN_MS} ms source window may be merged."
            )
        bridged += gap


def logical_pause_instructions() -> str:
    """The same rule must be advertised to models and enforced on responses."""
    return (
        f"- Normally preserve pauses greater than {DEFAULT_PASSAGE_GAP_MS} ms. "
        f"Exception: a same-speaker hesitation of at most {DEFAULT_CONTINUATION_GAP_MS} ms "
        "inside an unfinished phrase may be merged when separation would harm meaning or fluency "
        "(for example, adjectives separated from their noun). Do not use this exception at "
        "sentence endings, clause punctuation, or an already natural clause-opening boundary. "
        f"The sum of exceptional gaps must not exceed {DEFAULT_CONTINUATION_GAP_MS} ms "
        f"and the merged source window must not exceed {MAX_CONTINUATION_SPAN_MS} ms. "
        "Never merge overlapping cues or use a speaker reassignment to bypass the hesitation guard. "
        "A permitted merge keeps the full combined source start/end window and discards its old "
        "internal timing boundary; never invent word timings.\n"
    )
