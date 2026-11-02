"""Read-only diagnostics for cue and word timing consistency."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, TypeGuard

_FLAG_ORDER = ("cue_overlap", "word_overlap", "word_order_conflict")
_DETAIL_LIMIT = 20
_MAX_DETAIL_LIMIT = 100


@dataclass(frozen=True, slots=True)
class TimingDiagnosticsResult:
    summary: dict[str, Any]
    cue_flags: dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class _TimedInterval:
    cue_id: str
    start_ms: int
    end_ms: int
    speaker: Any
    word_index: int | None = None
    source_order_index: int = 0


@dataclass(frozen=True, slots=True)
class _SourceWord:
    cue_id: str
    word_index: int
    start_ms: int | None
    speaker: Any
    source_order_index: int


def _integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_interval(value: Mapping[str, Any]) -> tuple[int, int] | None:
    start_ms = value.get("start_ms")
    end_ms = value.get("end_ms")
    if (
        not _integer(start_ms)
        or not _integer(end_ms)
        or start_ms < 0
        or end_ms <= start_ms
    ):
        return None
    return start_ms, end_ms


def _speaker_relation(left: Any, right: Any) -> str:
    left_label = left.strip().casefold() if isinstance(left, str) else ""
    right_label = right.strip().casefold() if isinstance(right, str) else ""
    if not left_label or not right_label:
        return "unknown"
    return "same" if left_label == right_label else "different"


def _mark(
    flags: dict[str, set[str]],
    left_cue_id: str,
    right_cue_id: str,
    code: str,
) -> None:
    if left_cue_id:
        flags.setdefault(left_cue_id, set()).add(code)
    if right_cue_id:
        flags.setdefault(right_cue_id, set()).add(code)


def _overlap_events(
    intervals: Sequence[_TimedInterval],
    *,
    code: str,
    flags: dict[str, set[str]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ordered = sorted(
        intervals,
        key=lambda item: (item.start_ms, item.end_ms, item.source_order_index),
    )
    witness: _TimedInterval | None = None
    for current in ordered:
        if witness is not None and current.start_ms < witness.end_ms:
            overlap_start = max(witness.start_ms, current.start_ms)
            overlap_end = min(witness.end_ms, current.end_ms)
            if overlap_end > overlap_start:
                events.append(
                    {
                        "code": code,
                        "left_cue_id": witness.cue_id,
                        "right_cue_id": current.cue_id,
                        "left_word_index": witness.word_index,
                        "right_word_index": current.word_index,
                        "start_ms": overlap_start,
                        "end_ms": overlap_end,
                        "overlap_ms": overlap_end - overlap_start,
                        "speaker_relation": _speaker_relation(
                            witness.speaker, current.speaker
                        ),
                    }
                )
                _mark(flags, witness.cue_id, current.cue_id, code)
        if witness is None or current.end_ms > witness.end_ms:
            witness = current
    return events


def diagnose_timed_cues(
    cues: Sequence[Mapping[str, Any]], *, detail_limit: int = _DETAIL_LIMIT
) -> TimingDiagnosticsResult:
    """Summarize positive timing overlaps and source-order reversals.

    Input cue and word records are only read. Invalid or non-positive spans are
    counted and excluded from overlap checks; valid word starts can still take
    part in source-order checks when their end time is invalid.
    """

    limit = max(0, min(int(detail_limit), _MAX_DETAIL_LIMIT))
    cue_intervals: list[_TimedInterval] = []
    word_intervals: list[_TimedInterval] = []
    source_words: list[_SourceWord] = []
    invalid_cue_timings = 0
    invalid_word_timings = 0
    word_count = 0
    cue_order: list[str] = []

    for cue_position, cue in enumerate(cues):
        if not isinstance(cue, Mapping):
            invalid_cue_timings += 1
            continue
        raw_id = cue.get("id")
        cue_id = str(raw_id) if raw_id is not None else ""
        cue_order.append(cue_id)
        speaker = cue.get("speaker")
        interval = _valid_interval(cue)
        if interval is None:
            invalid_cue_timings += 1
        else:
            cue_intervals.append(
                _TimedInterval(
                    cue_id=cue_id,
                    start_ms=interval[0],
                    end_ms=interval[1],
                    speaker=speaker,
                    source_order_index=cue_position,
                )
            )

        raw_words = cue.get("words")
        words = (
            raw_words
            if isinstance(raw_words, Sequence)
            and not isinstance(raw_words, (str, bytes, bytearray))
            else ()
        )
        for word_position, word in enumerate(words, start=1):
            word_count += 1
            source_order_index = len(source_words)
            if not isinstance(word, Mapping):
                invalid_word_timings += 1
                source_words.append(
                    _SourceWord(cue_id, word_position, None, speaker, source_order_index)
                )
                continue

            start_value = word.get("start_ms")
            source_start = start_value if _integer(start_value) else None
            source_words.append(
                _SourceWord(
                    cue_id,
                    word_position,
                    source_start,
                    speaker,
                    source_order_index,
                )
            )
            interval = _valid_interval(word)
            if interval is None:
                invalid_word_timings += 1
                continue
            word_intervals.append(
                _TimedInterval(
                    cue_id=cue_id,
                    start_ms=interval[0],
                    end_ms=interval[1],
                    speaker=speaker,
                    word_index=word_position,
                    source_order_index=source_order_index,
                )
            )

    flags: dict[str, set[str]] = {}
    cue_events = _overlap_events(
        cue_intervals, code="cue_overlap", flags=flags
    )
    word_events = _overlap_events(
        word_intervals, code="word_overlap", flags=flags
    )
    order_events: list[dict[str, Any]] = []
    for left, right in pairwise(source_words):
        if (
            left.start_ms is None
            or right.start_ms is None
            or right.start_ms >= left.start_ms
        ):
            continue
        order_events.append(
            {
                "code": "word_order_conflict",
                "left_cue_id": left.cue_id,
                "right_cue_id": right.cue_id,
                "left_word_index": left.word_index,
                "right_word_index": right.word_index,
                "start_ms": right.start_ms,
                "end_ms": left.start_ms,
                "backward_ms": left.start_ms - right.start_ms,
                "speaker_relation": _speaker_relation(left.speaker, right.speaker),
            }
        )
        _mark(flags, left.cue_id, right.cue_id, "word_order_conflict")

    # Put lexical-order risks first so a bounded report is not filled with
    # ordinary caption overlaps before it reaches a backwards word boundary.
    events = [*order_events, *word_events, *cue_events]
    same_speaker_word_overlaps = sum(
        event["speaker_relation"] == "same" for event in word_events
    )
    different_speaker_word_overlaps = sum(
        event["speaker_relation"] == "different" for event in word_events
    )
    unknown_speaker_word_overlaps = sum(
        event["speaker_relation"] == "unknown" for event in word_events
    )
    issues = events[:limit]
    summary = {
        "schema_version": "1",
        "cue_count": len(cues),
        "word_count": word_count,
        "cue_overlap_events": len(cue_events),
        "word_overlap_events": len(word_events),
        "word_order_conflicts": len(order_events),
        "affected_cue_count": len(flags),
        "same_speaker_word_overlaps": same_speaker_word_overlaps,
        "different_speaker_word_overlaps": different_speaker_word_overlaps,
        "unknown_speaker_word_overlaps": unknown_speaker_word_overlaps,
        "issues": issues,
        "issues_total": len(events),
        "issues_truncated": len(events) > len(issues),
        "invalid_cue_timings": invalid_cue_timings,
        "invalid_word_timings": invalid_word_timings,
    }
    cue_flags = {
        cue_id: [code for code in _FLAG_ORDER if code in flags[cue_id]]
        for cue_id in cue_order
        if cue_id in flags
    }
    return TimingDiagnosticsResult(summary=summary, cue_flags=cue_flags)
