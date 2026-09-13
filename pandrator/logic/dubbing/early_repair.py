"""Deterministic cue-boundary selection for early voiceover repair.

The repair pass deliberately consumes only complete, trusted provenance cues.
It does not infer a split inside a cue: a result is returned only when both
text layers and their source mappings are internally consistent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TypeGuard


@dataclass(frozen=True)
class RepairBoundary:
    """A source-cue boundary that is safe to use for one repair split."""

    display_cursor: int
    speech_cursor: int
    start_ms: int
    boundary_ms: int
    end_ms: int
    estimated_advance_ms: int


@dataclass(frozen=True)
class _SourceCue:
    reference: int | str
    start_ms: int
    end_ms: int
    display_text: str
    speech_text: str
    display_span: tuple[int, int]
    speech_span: tuple[int, int]


_BOUNDARY_PUNCTUATION = set(".!?;:,。！？；：，—")
_CLOSING_MARKS = set(
    "\"'\u201d\u2019\u00bb\u203a)]}"
    "\u300d\u300f\uff09\u3010\u3011\uff5d\uff3d\u3009\u300b"
)
_ABBREVIATIONS = {
    "a.m",
    "approx",
    "asst",
    "dept",
    "dr",
    "e.g",
    "etc",
    "fig",
    "i.e",
    "jr",
    "mr",
    "mrs",
    "ms",
    "no",
    "prof",
    "rev",
    "sr",
    "st",
    "u.s",
    "vs",
}


def _is_integer(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalise_whitespace(value: str) -> str:
    return " ".join(value.split())


def _is_whitespace(value: str) -> bool:
    return not value.strip()


def _read_span(value: Any, text_length: int) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 1:
        return None
    pair = value[0]
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    start, end = pair
    if not _is_integer(start) or not _is_integer(end):
        return None
    if start < 0 or end <= start or end > text_length:
        return None
    return start, end


def _validate_text_spans(
    text: str,
    spans: list[tuple[int, int]],
    cue_texts: list[str],
) -> bool:
    """Validate complete, non-overlapping coverage by one span per cue."""

    previous_end = 0
    for (start, end), cue_text in zip(spans, cue_texts, strict=True):
        if start < previous_end:
            return False
        if not _is_whitespace(text[previous_end:start]):
            return False
        if _normalise_whitespace(text[start:end]) != _normalise_whitespace(cue_text):
            return False
        if not _normalise_whitespace(cue_text):
            return False
        previous_end = end
    return _is_whitespace(text[previous_end:])


def _load_cues(
    text: str,
    spoken_text: str,
    provenance: dict[str, Any],
) -> list[_SourceCue] | None:
    source_cues = provenance.get("source_cues")
    if not isinstance(source_cues, list) or len(source_cues) < 2:
        return None

    cues: list[_SourceCue] = []
    references: set[int | str] = set()
    previous_end_ms: int | None = None
    for raw_cue in source_cues:
        if not isinstance(raw_cue, dict):
            return None

        reference = raw_cue.get("reference")
        if isinstance(reference, bool) or not isinstance(reference, (int, str)):
            return None
        if isinstance(reference, str) and not reference.strip():
            return None
        if reference in references:
            return None
        references.add(reference)

        start_ms = raw_cue.get("start_ms")
        end_ms = raw_cue.get("end_ms")
        if (
            not _is_integer(start_ms)
            or not _is_integer(end_ms)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            return None
        if previous_end_ms is not None and start_ms < previous_end_ms:
            return None
        previous_end_ms = end_ms

        display_text = raw_cue.get("display_text")
        if not isinstance(display_text, str):
            return None
        speech_text = raw_cue.get("speech_text")
        if speech_text is None or (
            isinstance(speech_text, str) and not speech_text.strip()
        ):
            speech_text = display_text
        if not isinstance(speech_text, str):
            return None

        display_span = _read_span(raw_cue.get("display_spans"), len(text))
        speech_span = _read_span(raw_cue.get("speech_spans"), len(spoken_text))
        if display_span is None or speech_span is None:
            return None
        cues.append(
            _SourceCue(
                reference=reference,
                start_ms=start_ms,
                end_ms=end_ms,
                display_text=display_text,
                speech_text=speech_text,
                display_span=display_span,
                speech_span=speech_span,
            )
        )

    if not _validate_text_spans(
        text,
        [cue.display_span for cue in cues],
        [cue.display_text for cue in cues],
    ):
        return None
    if not _validate_text_spans(
        spoken_text,
        [cue.speech_span for cue in cues],
        [cue.speech_text for cue in cues],
    ):
        return None
    return cues


def _without_closing_marks(value: str) -> str:
    result = value.rstrip()
    while result and result[-1] in _CLOSING_MARKS:
        result = result[:-1].rstrip()
    return result


def _period_is_non_boundary(value: str, following: str) -> bool:
    before_period = value[:-1].rstrip()
    next_character = following.lstrip()[:1]
    if before_period and before_period[-1].isdigit() and next_character.isdigit():
        return True

    match = re.search(r"(?<!\w)([A-Za-z](?:[A-Za-z.]*[A-Za-z])?)$", before_period)
    if match is None:
        return False
    token = match.group(1).casefold()
    return token in _ABBREVIATIONS or len(token) == 1


def _has_boundary_punctuation(left: str, right: str) -> bool:
    candidate = _without_closing_marks(left)
    if not candidate or candidate[-1] not in _BOUNDARY_PUNCTUATION:
        return False
    return candidate[-1] != "." or not _period_is_non_boundary(candidate, right)


def _valid_duration(value: Any, *, positive: bool = False) -> bool:
    return _is_integer(value) and (value > 0 if positive else value >= 0)


def find_repair_boundary(
    text: str,
    spoken_text: str,
    provenance: dict[str, Any],
    audio_duration_ms: int,
    *,
    incoming_delay_ms: int = 0,
    start_delay_ms: int = 0,
) -> RepairBoundary | None:
    """Find the strongest complete-cue boundary for early repair.

    The returned boundary is deliberately conservative.  Any malformed or
    stale provenance invalidates the complete block, since guessing a cursor
    could create text/audio mismatches in the repair flow.
    """

    if not isinstance(text, str) or not isinstance(spoken_text, str):
        return None
    if not isinstance(provenance, dict):
        return None
    if not _valid_duration(audio_duration_ms, positive=True):
        return None
    if not _valid_duration(incoming_delay_ms) or not _valid_duration(start_delay_ms):
        return None
    if incoming_delay_ms > 0:
        return None

    cues = _load_cues(text, spoken_text, provenance)
    if cues is None:
        return None

    first = cues[0]
    last = cues[-1]
    source_span = last.end_ms - first.start_ms
    shortfall = source_span - audio_duration_ms - start_delay_ms
    if shortfall < 1000 or shortfall * 5 < source_span:
        return None

    best: tuple[float, int, RepairBoundary] | None = None
    for left, right in zip(cues, cues[1:], strict=False):
        display_left = text[: left.display_span[1]]
        display_right = text[right.display_span[0] :]
        speech_left = spoken_text[: left.speech_span[1]]
        speech_right = spoken_text[right.speech_span[0] :]
        if (
            min(
                len(display_left.strip()),
                len(display_right.strip()),
                len(speech_left.strip()),
                len(speech_right.strip()),
            )
            < 10
        ):
            continue

        display_cue = text[left.display_span[0] : left.display_span[1]]
        display_following = text[right.display_span[0] : right.display_span[1]]
        speech_cue = spoken_text[left.speech_span[0] : left.speech_span[1]]
        speech_following = spoken_text[right.speech_span[0] : right.speech_span[1]]
        if not _has_boundary_punctuation(display_cue, display_following):
            continue
        if not _has_boundary_punctuation(speech_cue, speech_following):
            continue

        left_child_span = left.end_ms - first.start_ms
        right_child_span = last.end_ms - right.start_ms
        if left_child_span < 1000 or right_child_span < 1000:
            continue

        spoken_left_length = len(speech_left.strip())
        spoken_right_length = len(speech_right.strip())
        total_spoken_length = spoken_left_length + spoken_right_length
        if total_spoken_length <= 0:
            continue
        approximate_left_audio = (
            audio_duration_ms * spoken_left_length / total_spoken_length
        )
        estimated_advance = (
            right.start_ms - first.start_ms - start_delay_ms - approximate_left_audio
        )
        if estimated_advance < 1000:
            continue

        boundary = RepairBoundary(
            display_cursor=left.display_span[1],
            speech_cursor=left.speech_span[1],
            start_ms=first.start_ms,
            boundary_ms=right.start_ms,
            end_ms=last.end_ms,
            estimated_advance_ms=round(estimated_advance),
        )
        candidate = (estimated_advance, right.start_ms, boundary)
        if (
            best is None
            or estimated_advance > best[0]
            or (estimated_advance == best[0] and right.start_ms < best[1])
        ):
            best = candidate

    return best[2] if best is not None else None
