"""Pure media-edit timeline primitives.

The functions in this module deliberately know nothing about audio/video
backends.  They operate on a half-open millisecond timeline and are intended
to be used by a revisioned, removal-only editor.
"""

from __future__ import annotations

import html
import itertools
import re
import unicodedata
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal


def _validate_time(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_interval(start_ms: int, end_ms: int, *, name: str) -> None:
    _validate_time(start_ms, f"{name}.start_ms")
    _validate_time(end_ms, f"{name}.end_ms")
    if end_ms <= start_ms:
        raise ValueError(f"{name} must have end_ms greater than start_ms")


@dataclass(frozen=True)
class MediaWord:
    """A word and its half-open position on the source timeline."""

    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None

    def __post_init__(self) -> None:
        _validate_interval(self.start_ms, self.end_ms, name="MediaWord")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("MediaWord.confidence must be between 0 and 1")


@dataclass(frozen=True)
class MediaCue:
    """A caption cue with optional aligned word-level timing."""

    id: str
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None
    words: tuple[MediaWord, ...] = field(default_factory=tuple)
    timing_confidence: float | None = None
    timing_source: str = "caption"

    def __post_init__(self) -> None:
        _validate_interval(self.start_ms, self.end_ms, name="MediaCue")
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("MediaCue.id must be a non-empty string")
        if not isinstance(self.text, str):
            raise TypeError("MediaCue.text must be a string")
        words = tuple(self.words)
        if any(not isinstance(word, MediaWord) for word in words):
            raise ValueError("MediaCue.words must contain MediaWord values")
        object.__setattr__(self, "words", words)
        if self.timing_confidence is not None and not 0 <= self.timing_confidence <= 1:
            raise ValueError("MediaCue.timing_confidence must be between 0 and 1")


@dataclass(frozen=True)
class KeepRange:
    """A retained half-open source interval."""

    id: str
    start_ms: int
    end_ms: int
    label: str | None = None

    def __post_init__(self) -> None:
        _validate_interval(self.start_ms, self.end_ms, name="KeepRange")
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("KeepRange.id must be a non-empty string")


@dataclass(frozen=True)
class BoundaryEvidence:
    """Evidence used to explain an optional silence-based boundary refinement."""

    original_ms: int
    refined_ms: int
    confidence: float
    method: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_time(self.original_ms, "BoundaryEvidence.original_ms")
        _validate_time(self.refined_ms, "BoundaryEvidence.refined_ms")
        if not 0 <= self.confidence <= 1:
            raise ValueError("BoundaryEvidence.confidence must be between 0 and 1")
        if not isinstance(self.method, str) or not self.method:
            raise ValueError("BoundaryEvidence.method must be a non-empty string")
        object.__setattr__(self, "warnings", tuple(self.warnings))


_TIMESTAMP_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):"
    r"(?P<seconds>\d{2})(?:[.,](?P<millis>\d{1,3}))?$"
)
_TIMESTAMP_HMS_RE = re.compile(
    r"^(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})"
    r"(?:[.,](?P<millis>\d{1,3}))?$"
)
_VTT_TAG_RE = re.compile(r"<v(?:\s+([^>]*?))?>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"</?[^>]+>")
_SPEAKER_LABEL_RE = re.compile(r"^(?P<label>[^:\n]{1,80}):\s+(?P<text>\S.*)$")


def _parse_timestamp(value: str) -> int:
    """Parse SRT/VTT timestamps, including VTT's minute-only form."""

    value = value.strip().split()[0] if value.strip() else ""
    match = _TIMESTAMP_HMS_RE.fullmatch(value)
    if match is None:
        match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"Invalid caption timestamp: {value!r}")
    millis_text = match.group("millis") or "0"
    millis = int(millis_text.ljust(3, "0"))
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if seconds > 59 or minutes > 59:
        raise ValueError(f"Invalid caption timestamp: {value!r}")
    return hours * 3_600_000 + minutes * 60_000 + seconds * 1_000 + millis


def _parse_timing_line(line: str) -> tuple[int, int]:
    if "-->" not in line:
        raise ValueError("Caption block has no timing arrow")
    start_text, end_text = line.split("-->", 1)
    start_ms = _parse_timestamp(start_text)
    end_ms = _parse_timestamp(end_text)
    if end_ms <= start_ms:
        raise ValueError("Caption cue must end after it starts")
    return start_ms, end_ms


def _speaker_candidate(text: str) -> tuple[str, str] | None:
    match = _SPEAKER_LABEL_RE.fullmatch(text.strip())
    if match is None:
        return None
    label = match.group("label").strip()
    return label, match.group("text").strip()


def _speaker_label(
    text: str, repeated_speakers: set[str] | frozenset[str] = frozenset()
) -> tuple[str | None, str]:
    candidate = _speaker_candidate(text)
    if candidate is None:
        return None, text.strip()
    label, payload = candidate
    words = label.replace("-", " ").replace("_", " ").split()
    # A short, title-like prefix is deliberately required.  This avoids
    # turning ordinary prose such as "It was a surprise: ..." into metadata.
    title_like = (
        1 <= len(words) <= 6
        and any(character.isalpha() for character in label)
        and all(
            word[0].isupper() or not any(character.isalpha() for character in word)
            for word in words
        )
    )
    if not title_like and label.casefold() not in repeated_speakers:
        return None, text.strip()
    return label, payload


def _strip_caption_markup(payload_lines: Sequence[str]) -> tuple[str | None, str]:
    payload = "\n".join(payload_lines).strip()
    speaker: str | None = None
    vtt_match = _VTT_TAG_RE.search(payload)
    if vtt_match is not None and vtt_match.group(1):
        speaker = vtt_match.group(1).strip()
    payload = _VTT_TAG_RE.sub("", payload)
    payload = _HTML_TAG_RE.sub("", payload)
    payload = html.unescape(payload)
    payload = re.sub(r"\s+", " ", payload).strip()
    return speaker, payload


def _clean_caption_payload(
    payload_lines: Sequence[str],
    repeated_speakers: set[str] | frozenset[str] = frozenset(),
) -> tuple[str | None, str]:
    speaker, payload = _strip_caption_markup(payload_lines)
    if not payload:
        return speaker, ""
    if speaker is None:
        speaker, payload = _speaker_label(payload, repeated_speakers)
    return speaker, payload


def parse_caption_text(text: str) -> tuple[MediaCue, ...]:
    """Parse SRT or WebVTT content by inspecting its contents.

    Invalid/non-caption blocks are ignored once a valid caption format has
    been identified.  Completely empty or unrecognised input is rejected so
    callers cannot silently create an empty edit timeline.
    """

    if not isinstance(text, str):
        raise TypeError("Caption content must be text")
    normalized = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.strip()
    if not normalized:
        raise ValueError("Caption content is empty")
    lines = normalized.split("\n")
    is_vtt = any(line.strip().upper().startswith("WEBVTT") for line in lines[:2])
    blocks = re.split(r"\n\s*\n+", normalized)
    parsed_cues: list[tuple[int, int, str | None, str]] = []
    for block in blocks:
        block_lines = [line.rstrip() for line in block.split("\n")]
        meaningful = [line.strip() for line in block_lines if line.strip()]
        if not meaningful:
            continue
        first_upper = meaningful[0].upper()
        if first_upper.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_index = next(
            (index for index, line in enumerate(block_lines) if "-->" in line),
            -1,
        )
        if timing_index < 0:
            continue
        try:
            start_ms, end_ms = _parse_timing_line(block_lines[timing_index])
        except ValueError:
            continue
        speaker, cue_text = _strip_caption_markup(block_lines[timing_index + 1 :])
        if not cue_text:
            continue
        parsed_cues.append((start_ms, end_ms, speaker, cue_text))
    repeated_speakers: set[str] = set()
    candidate_counts: dict[str, int] = {}
    for _start_ms, _end_ms, speaker, cue_text in parsed_cues:
        if speaker is not None:
            continue
        candidate = _speaker_candidate(cue_text)
        if candidate is not None:
            label, _payload = candidate
            key = label.casefold()
            candidate_counts[key] = candidate_counts.get(key, 0) + 1
    repeated_speakers.update(
        label for label, count in candidate_counts.items() if count >= 2
    )
    cues: list[MediaCue] = []
    for start_ms, end_ms, markup_speaker, cue_payload in parsed_cues:
        speaker = markup_speaker
        cue_text = cue_payload
        if speaker is None:
            speaker, cue_text = _speaker_label(cue_payload, repeated_speakers)
        cues.append(
            MediaCue(
                id=f"cue-{len(cues) + 1:06d}",
                start_ms=start_ms,
                end_ms=end_ms,
                text=cue_text,
                speaker=speaker,
            )
        )
    if not cues:
        format_name = "WebVTT" if is_vtt else "SRT/caption"
        raise ValueError(f"No valid {format_name} cues found")
    return tuple(cues)


def _normalise_token(token: str) -> str:
    normalized = unicodedata.normalize("NFKC", token).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _text_tokens(text: str) -> list[str]:
    return [
        normalized
        for raw in re.findall(r"\S+", text)
        if (normalized := _normalise_token(raw))
    ]


def _interpolate_words(
    cue_tokens: Sequence[str],
    matches: dict[int, int],
    words: Sequence[MediaWord],
) -> tuple[MediaWord, ...]:
    matched_positions = sorted(matches)
    if len(matched_positions) < 2:
        return (words[matches[matched_positions[0]]],)
    by_token: dict[int, MediaWord] = {}
    for position in matched_positions:
        by_token[position] = words[matches[position]]
    for left_position, right_position in itertools.pairwise(matched_positions):
        left_word = words[matches[left_position]]
        right_word = words[matches[right_position]]
        interior_positions = list(range(left_position + 1, right_position))
        if not interior_positions or right_word.start_ms < left_word.end_ms:
            continue
        gap = right_word.start_ms - left_word.end_ms
        slots = len(interior_positions) + 1
        for slot, token_position in enumerate(interior_positions, start=1):
            start_ms = left_word.end_ms + (gap * slot) // slots
            end_ms = left_word.end_ms + (gap * (slot + 1)) // slots
            if end_ms > start_ms:
                by_token[token_position] = MediaWord(
                    text=cue_tokens[token_position],
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
    return tuple(by_token[position] for position in sorted(by_token))


def align_cues_to_words(
    cues: Sequence[MediaCue], words: Sequence[MediaWord]
) -> tuple[MediaCue, ...]:
    """Align caption tokens to monotonic ASR words using lexical matches."""

    if not cues:
        return ()
    external_words = tuple(words)
    normalized_words = [_normalise_token(word.text) for word in external_words]
    positions_by_token: dict[str, list[int]] = {}
    for word_index, token in enumerate(normalized_words):
        if token:
            positions_by_token.setdefault(token, []).append(word_index)
    cursor = 0
    previous_cue_end: int | None = None
    previous_match_start: int | None = None
    aligned_cues: list[MediaCue] = []
    for cue in cues:
        cue_tokens = _text_tokens(cue.text)
        matches: dict[int, int] = {}
        search_index = cursor
        if (
            previous_cue_end is not None
            and cue.start_ms < previous_cue_end
            and previous_match_start is not None
        ):
            # Overlapping captions commonly repeat the last word of the
            # preceding cue.  Reusing that lexical anchor preserves the
            # source overlap while keeping the global search monotonic for
            # non-overlapping cues.
            search_index = previous_match_start
        search_limit = min(
            len(external_words),
            search_index + max(400, len(cue_tokens) * 16),
        )
        for token_index, cue_token in enumerate(cue_tokens):
            positions = positions_by_token.get(cue_token, ())
            position_index = bisect_left(positions, search_index)
            if (
                position_index >= len(positions)
                or positions[position_index] >= search_limit
            ):
                continue
            match_index = positions[position_index]
            matches[token_index] = match_index
            search_index = match_index + 1
        if not matches:
            aligned_cues.append(
                replace(cue, timing_confidence=0.0, timing_source="caption")
            )
            previous_cue_end = cue.end_ms
            previous_match_start = None
            continue
        matched_words = _interpolate_words(cue_tokens, matches, external_words)
        if not matched_words:
            aligned_cues.append(
                replace(cue, timing_confidence=0.0, timing_source="caption")
            )
            continue
        first_word = external_words[min(matches.values())]
        last_word = external_words[max(matches.values())]
        confidence = len(matches) / max(1, len(cue_tokens))
        if confidence < 0.5:
            aligned_cues.append(
                replace(
                    cue,
                    timing_confidence=max(0.0, min(1.0, confidence)),
                    timing_source="caption",
                )
            )
            previous_cue_end = cue.end_ms
            previous_match_start = None
            continue
        cursor = max(cursor, max(matches.values()) + 1)
        previous_cue_end = cue.end_ms
        previous_match_start = min(matches.values())
        aligned_cues.append(
            replace(
                cue,
                start_ms=first_word.start_ms,
                end_ms=last_word.end_ms,
                words=matched_words,
                timing_confidence=max(0.0, min(1.0, confidence)),
                timing_source="asr_alignment",
            )
        )
    return tuple(aligned_cues)


def _merge_labels(labels: Sequence[str | None]) -> str | None:
    values: list[str] = []
    for label in labels:
        cleaned = label.strip() if label else ""
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return " / ".join(values) if values else None


def normalize_keep_ranges(
    ranges: Sequence[KeepRange],
    duration_ms: int,
    *,
    merge_gap_ms: int = 0,
) -> tuple[KeepRange, ...]:
    """Clamp, sort, merge, and deterministically identify retained ranges."""

    _validate_time(duration_ms, "duration_ms")
    if duration_ms <= 0:
        raise ValueError("duration_ms must be greater than zero")
    _validate_time(merge_gap_ms, "merge_gap_ms")
    clamped: list[tuple[int, int, str | None, int]] = []
    for source_index, item in enumerate(ranges):
        if not isinstance(item, KeepRange):
            raise TypeError("ranges must contain KeepRange values")
        start_ms = min(duration_ms, max(0, item.start_ms))
        end_ms = min(duration_ms, max(0, item.end_ms))
        if end_ms <= start_ms:
            continue
        clamped.append((start_ms, end_ms, item.label, source_index))
    if not clamped:
        raise ValueError("No retained range remains after clamping")
    clamped.sort(key=lambda item: (item[0], item[1], item[3]))
    merged: list[tuple[int, int, list[str | None]]] = []
    for start_ms, end_ms, label, _source_index in clamped:
        if not merged or start_ms > merged[-1][1] + merge_gap_ms:
            merged.append((start_ms, end_ms, [label]))
            continue
        old_start, old_end, labels = merged[-1]
        merged[-1] = (old_start, max(old_end, end_ms), [*labels, label])
    return tuple(
        KeepRange(
            id=f"keep-{index:06d}",
            start_ms=start_ms,
            end_ms=end_ms,
            label=_merge_labels(labels),
        )
        for index, (start_ms, end_ms, labels) in enumerate(merged, start=1)
    )


def keep_ranges_from_cuts(
    cuts: Sequence[tuple[int, int]],
    duration_ms: int,
    *,
    minimum_keep_ms: int = 1,
) -> tuple[KeepRange, ...]:
    """Return the complement of clamped, merged removal intervals."""

    _validate_time(duration_ms, "duration_ms")
    if duration_ms <= 0:
        raise ValueError("duration_ms must be greater than zero")
    _validate_time(minimum_keep_ms, "minimum_keep_ms")
    if minimum_keep_ms <= 0:
        raise ValueError("minimum_keep_ms must be greater than zero")
    normalized_cuts: list[tuple[int, int]] = []
    for cut in cuts:
        if len(cut) != 2:
            raise ValueError("Each cut must contain start and end milliseconds")
        start_ms, end_ms = cut
        if (
            not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
        ):
            raise TypeError("cut endpoints must be integers")
        start_ms = min(duration_ms, max(0, start_ms))
        end_ms = min(duration_ms, max(0, end_ms))
        if end_ms <= start_ms:
            continue
        normalized_cuts.append((start_ms, end_ms))
    if not normalized_cuts:
        return (KeepRange("keep-000001", 0, duration_ms),)
    normalized_cuts.sort()
    merged_cuts: list[tuple[int, int]] = []
    for start_ms, end_ms in normalized_cuts:
        if not merged_cuts or start_ms > merged_cuts[-1][1]:
            merged_cuts.append((start_ms, end_ms))
        else:
            merged_cuts[-1] = (merged_cuts[-1][0], max(merged_cuts[-1][1], end_ms))
    complements: list[KeepRange] = []
    cursor = 0
    for cut_start, cut_end in merged_cuts:
        if cut_start - cursor >= minimum_keep_ms:
            complements.append(KeepRange("pending", cursor, cut_start))
        cursor = max(cursor, cut_end)
    if duration_ms - cursor >= minimum_keep_ms:
        complements.append(KeepRange("pending", cursor, duration_ms))
    return tuple(
        KeepRange(f"keep-{index:06d}", item.start_ms, item.end_ms)
        for index, item in enumerate(complements, start=1)
    )


def _coalesce_ranges(ranges: Sequence[KeepRange]) -> tuple[KeepRange, ...]:
    ordered = sorted(ranges, key=lambda item: (item.start_ms, item.end_ms, item.id))
    result: list[KeepRange] = []
    for item in ordered:
        if not result or item.start_ms > result[-1].end_ms:
            result.append(item)
        else:
            current = result[-1]
            result[-1] = KeepRange(
                current.id,
                current.start_ms,
                max(current.end_ms, item.end_ms),
                _merge_labels((current.label, item.label)),
            )
    return tuple(result)


def retime_cues(
    cues: Sequence[MediaCue], keep_ranges: Sequence[KeepRange]
) -> tuple[MediaCue, ...]:
    """Map cue portions from source time into the contiguous kept timeline."""

    ranges = _coalesce_ranges(tuple(keep_ranges))
    if not ranges:
        return ()
    output_bases: list[tuple[KeepRange, int]] = []
    output_cursor = 0
    for item in ranges:
        output_bases.append((item, output_cursor))
        output_cursor += item.end_ms - item.start_ms

    result: list[MediaCue] = []
    for cue in cues:
        intersections: list[tuple[KeepRange, int, int, int]] = []
        for item, output_base in output_bases:
            start_ms = max(cue.start_ms, item.start_ms)
            end_ms = min(cue.end_ms, item.end_ms)
            if end_ms > start_ms:
                intersections.append((item, output_base, start_ms, end_ms))
        if not intersections:
            continue
        multi_part = len(intersections) > 1
        for part_index, (_item, output_base, part_start, part_end) in enumerate(
            intersections, start=1
        ):
            new_start = output_base + part_start - _item.start_ms
            new_end = output_base + part_end - _item.start_ms
            selected_words = tuple(
                replace(
                    word,
                    start_ms=max(word.start_ms, part_start)
                    - _item.start_ms
                    + output_base,
                    end_ms=min(word.end_ms, part_end) - _item.start_ms + output_base,
                )
                for word in cue.words
                if word.end_ms > part_start and word.start_ms < part_end
            )
            if cue.words and not selected_words:
                continue
            if selected_words:
                piece_text = (
                    cue.text
                    if len(selected_words) == len(cue.words)
                    else " ".join(word.text for word in selected_words)
                )
            else:
                piece_text = cue.text
            piece_id = f"{cue.id}-part-{part_index:03d}" if multi_part else cue.id
            result.append(
                replace(
                    cue,
                    id=piece_id,
                    start_ms=new_start,
                    end_ms=new_end,
                    text=piece_text,
                    words=selected_words,
                )
            )
    return tuple(result)


def _word_gaps(words: Sequence[MediaWord]) -> list[tuple[int, int]]:
    ordered = sorted(words, key=lambda word: (word.start_ms, word.end_ms))
    return [
        (left.end_ms, right.start_ms)
        for left, right in itertools.pairwise(ordered)
        if right.start_ms > left.end_ms
    ]


def refine_boundary(
    boundary_ms: int,
    words: Sequence[MediaWord],
    *,
    side: Literal["start", "end"],
    search_ms: int = 1500,
    preroll_ms: int = 120,
    min_silence_ms: int = 80,
) -> BoundaryEvidence:
    """Refine a cut boundary using nearby word gaps, never energy/VAD."""

    _validate_time(search_ms, "search_ms")
    _validate_time(preroll_ms, "preroll_ms")
    _validate_time(min_silence_ms, "min_silence_ms")
    if side not in {"start", "end"}:
        raise ValueError("side must be 'start' or 'end'")
    if not isinstance(boundary_ms, int) or isinstance(boundary_ms, bool):
        raise TypeError("boundary_ms must be an integer")
    original = max(0, boundary_ms)
    ordered_words = tuple(sorted(words, key=lambda word: (word.start_ms, word.end_ms)))
    for word in ordered_words:
        if word.start_ms < boundary_ms < word.end_ms:
            edge = word.start_ms if side == "start" else word.end_ms
            return BoundaryEvidence(
                original,
                edge,
                0.25,
                "word_edge",
                ("boundary fell inside a word; moved to its edge",),
            )
    candidates: list[tuple[int, int]] = []
    for gap_start, gap_end in _word_gaps(ordered_words):
        if gap_end - gap_start < min_silence_ms:
            continue
        if side == "start":
            if gap_start > boundary_ms:
                continue
            candidate = gap_end - min(preroll_ms, max(1, (gap_end - gap_start) // 2))
        else:
            if gap_end < boundary_ms:
                continue
            candidate = gap_start + min(preroll_ms, max(1, (gap_end - gap_start) // 2))
        if (
            abs(candidate - boundary_ms) <= search_ms
            and gap_start <= candidate <= gap_end
        ):
            candidates.append((abs(candidate - boundary_ms), candidate))
    if not candidates:
        return BoundaryEvidence(
            original,
            original,
            0.0,
            "original",
            ("no unambiguous silence gap near boundary",),
        )
    candidates.sort()
    distance, refined = candidates[0]
    confidence = max(0.5, 1.0 - distance / max(1, search_ms * 2))
    return BoundaryEvidence(
        original,
        refined,
        min(1.0, confidence),
        f"word_gap_{side}",
    )


def _format_srt_timestamp(milliseconds: int) -> str:
    total = max(0, milliseconds)
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def caption_to_srt(cues: Sequence[MediaCue]) -> str:
    """Serialize clean cue text as standards-compliant SRT.

    Speaker identity remains structured Pandrator metadata; it is deliberately
    not baked back into the subtitle text.
    """

    blocks: list[str] = []
    for output_index, cue in enumerate(
        (cue for cue in cues if cue.text.strip()), start=1
    ):
        text = cue.text.strip()
        blocks.append(
            "\n".join(
                (
                    str(output_index),
                    (
                        f"{_format_srt_timestamp(cue.start_ms)} --> "
                        f"{_format_srt_timestamp(cue.end_ms)}"
                    ),
                    text,
                )
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")
