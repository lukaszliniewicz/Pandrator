"""Deterministic, delivery-oriented subtitle cue composition.

This module deliberately has no dependency on speech-block generation.  Speech
blocks optimize text for synthesis; finalized subtitles optimize timed text for
reading and export.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from math import ceil
from pathlib import Path
from statistics import median
from typing import Any

from .. import sentence_segmenter
from .models import SubtitleSegment
from .srt_utils import compose_srt, parse_srt
from .transcript_normalization import (
    NormalizedTranscript,
    TimedSegment,
    load_transcript,
    normalize_transcript,
)

_SPACE_RE = re.compile(r"\s+")
_NO_SPACE_BEFORE = set(",.!?:;%)]}…")
_NO_SPACE_AFTER = set("([{£€$")
_PREFERRED_BEFORE = {
    "and", "but", "or", "because", "although", "while", "if", "when", "that",
    "for", "from", "with", "without", "into", "onto", "before", "after", "of",
}
_SENTENCE_END_CHARS = set(".!?\u2026\u3002\uff01\uff1f")
_CLAUSE_PUNCTUATION = set(",;:\u2013\u2014\u3001\uff0c\uff1b\uff1a")
_TRAILING_MARKS = set("\"')]}\u201d\u2019\u00bb\u203a\u300d\u300f\uff09\u3011\uff5d\uff3d\u3009\u300b")
_CLAUSE_STARTERS = {
    "and", "but", "or", "because", "although", "though", "while", "if",
    "when", "so", "then", "which", "who", "that", "whereas", "unless",
    "ale", "oraz", "lub", "poniewaz", "poniewa\u017c", "chociaz", "chocia\u017c",
    "gdy", "kiedy", "ktory", "kt\u00f3ry", "ktora", "kt\u00f3ra", "ktore", "kt\u00f3re",
}
_WEAK_CUE_STARTS = {
    "a", "an", "the", "of", "to", "from", "with", "without", "into", "onto",
}
_WEAK_CUE_ENDS = {
    "a", "an", "the", "of", "to", "for", "from", "with", "without", "and",
    "or", "but", "because", "although", "though", "if", "when", "that",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubtitleFinalizationConfig:
    max_chars_per_line: int = 48
    max_lines: int = 2
    min_duration_ms: int = 833
    max_duration_ms: int = 7000
    max_chars_per_second: float = 20.0
    min_gap_ms: int = 80
    phrase_gap_ms: int = 900
    hard_gap_ms: int = 1500
    sentence_boundary_threshold: float = 0.25

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> SubtitleFinalizationConfig:
        values = dict(settings or {})
        def value(name: str, default: Any) -> Any:
            configured = values.get(name)
            return default if configured is None or configured == "" else configured

        return cls(
            max_chars_per_line=max(20, min(100, int(value("subtitle_max_chars_per_line", 48)))),
            max_lines=max(1, min(3, int(value("subtitle_max_lines", 2)))),
            min_duration_ms=max(250, min(3000, int(value("subtitle_min_duration_ms", 833)))),
            max_duration_ms=max(1000, min(15000, int(value("subtitle_max_duration_ms", 7000)))),
            max_chars_per_second=max(5.0, min(40.0, float(value("subtitle_max_cps", 20.0)))),
            min_gap_ms=max(0, min(500, int(value("subtitle_min_gap_ms", 80)))),
            phrase_gap_ms=max(100, min(3000, int(value("subtitle_phrase_gap_ms", 900)))),
            hard_gap_ms=max(250, min(5000, int(value("subtitle_hard_gap_ms", 1500)))),
            sentence_boundary_threshold=max(
                0.01,
                min(
                    0.99,
                    float(value("subtitle_sentence_boundary_threshold", 0.25)),
                ),
            ),
        )

    @property
    def max_event_chars(self) -> int:
        by_layout = self.max_chars_per_line * self.max_lines
        by_reading = int(self.max_chars_per_second * (self.max_duration_ms / 1000.0))
        return max(self.max_chars_per_line, min(by_layout, by_reading))


@dataclass(frozen=True)
class _TimedWord:
    index: int
    text: str
    start_ms: int
    end_ms: int
    speaker: str = ""
    source_segment_id: str = ""
    char_start: int = 0
    char_end: int = 0


@dataclass(frozen=True)
class _BoundaryEvidence:
    probability: float
    gap_ms: int
    sentence_end: bool
    punctuation: bool
    clause_start: bool
    speaker_change: bool
    hard_silence: bool
    weak_start: bool
    weak_end: bool


def _clean_text(text: str) -> str:
    return _SPACE_RE.sub(" ", str(text or "").replace("\n", " ")).strip()


def _join_tokens(tokens: list[str]) -> str:
    text = ""
    for raw in tokens:
        token = _clean_text(raw)
        if not token:
            continue
        if not text or token[0] in _NO_SPACE_BEFORE or text[-1] in _NO_SPACE_AFTER or token.startswith(("'", "’")):
            text += token
        else:
            text += " " + token
    return text.strip()


def _line_break_score(words: list[str], index: int) -> tuple[float, int]:
    first = " ".join(words[:index])
    second = " ".join(words[index:])
    score = float(abs(len(first) - len(second)))
    if len(first) > len(second):
        score += 4.0  # prefer a modest bottom-heavy pyramid
    if first.endswith(tuple(".!?,;:")):
        score -= 12.0
    if words[index].lower().strip("“\"'") in _PREFERRED_BEFORE:
        score -= 3.0
    if len(words[:index]) <= 2 or len(words[index:]) <= 2:
        score += 8.0
    return score, index


def wrap_subtitle_text(text: str, config: SubtitleFinalizationConfig) -> str:
    cleaned = _clean_text(text)
    if len(cleaned) <= config.max_chars_per_line or config.max_lines == 1:
        return cleaned
    words = cleaned.split()
    if config.max_lines == 2:
        candidates = [
            index for index in range(1, len(words))
            if len(" ".join(words[:index])) <= config.max_chars_per_line
            and len(" ".join(words[index:])) <= config.max_chars_per_line
        ]
        if candidates:
            split = min(candidates, key=lambda index: _line_break_score(words, index))
            return " ".join(words[:split]) + "\n" + " ".join(words[split:])

    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join((*current, word))
        if current and len(candidate) > config.max_chars_per_line:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    if len(lines) <= config.max_lines:
        return "\n".join(lines)
    # Never lose subtitle text. Normal finalization splits events before this
    # point; this fallback only handles pathological single-token input.
    retained = lines[: config.max_lines - 1]
    retained.append(" ".join(lines[config.max_lines - 1 :]))
    return "\n".join(retained)


def _fits_layout(text: str, config: SubtitleFinalizationConfig) -> bool:
    lines = wrap_subtitle_text(text, config).splitlines()
    return len(lines) <= config.max_lines and all(
        len(line) <= config.max_chars_per_line for line in lines
    )


def _split_words_to_capacity(
    text: str,
    config: SubtitleFinalizationConfig,
    *,
    min_chunks: int = 1,
) -> list[str]:
    words = _clean_text(text).split()
    if not words:
        return []

    word_count = len(words)
    minimum_requested = max(1, min(int(min_chunks), word_count))
    prefix_lengths = [0]
    for index, word in enumerate(words):
        prefix_lengths.append(
            prefix_lengths[-1] + len(word) + (1 if index else 0)
        )

    def range_length(start: int, end: int) -> int:
        return prefix_lengths[end] - prefix_lengths[start] - (1 if start else 0)

    # Store only feasible contiguous ranges.  Ranges are bounded by the
    # event capacity, so this stays tractable even for large batches.
    feasible_starts: list[list[int]] = [[] for _ in range(word_count + 1)]
    for start in range(word_count):
        for end in range(start + 1, word_count + 1):
            length = range_length(start, end)
            candidate = " ".join(words[start:end])
            if end == start + 1 and (
                length > config.max_event_chars or not _fits_layout(candidate, config)
            ):
                # A single overlong token is irreducible.  Retain it as a
                # bounded no-text-loss fallback rather than dropping it.
                feasible_starts[end].append(start)
                continue
            if length > config.max_event_chars:
                break
            if _fits_layout(candidate, config):
                feasible_starts[end].append(start)

    minimum_counts = [word_count + 1] * (word_count + 1)
    minimum_counts[0] = 0
    for end in range(1, word_count + 1):
        minimum_counts[end] = min(
            (
                minimum_counts[start] + 1
                for start in feasible_starts[end]
            ),
            default=word_count + 1,
        )
    minimum_count = minimum_counts[word_count]
    target_count = max(minimum_requested, min(minimum_count, word_count))

    def punctuation_preference(boundary: int) -> int:
        if boundary >= word_count:
            return 0
        word = words[boundary - 1]
        if word.endswith(tuple(_SENTENCE_END_CHARS)):
            return 3
        if word.endswith(tuple(_CLAUSE_PUNCTUATION)):
            return 1
        return 0

    total_length = prefix_lengths[-1] - (target_count - 1)
    dynamic: list[dict[int, tuple[int, int, tuple[int, ...]]]] = [
        {} for _ in range(target_count + 1)
    ]
    dynamic[0][0] = (0, 0, ())
    for count in range(1, target_count + 1):
        for end in range(1, word_count + 1):
            best: tuple[int, int, tuple[int, ...]] | None = None
            for start in feasible_starts[end]:
                previous = dynamic[count - 1].get(start)
                if previous is None:
                    continue
                length = range_length(start, end)
                deviation = length * target_count - total_length
                candidate = (
                    previous[0] + deviation * deviation,
                    previous[1] + punctuation_preference(end),
                    (*previous[2], end),
                )
                if best is None or (
                    candidate[0], -candidate[1], candidate[2]
                ) < (best[0], -best[1], best[2]):
                    best = candidate
            if best is not None:
                dynamic[count][end] = best

    solution = dynamic[target_count].get(word_count)
    if solution is None:
        # Every individual token is feasible through the fallback above, so
        # this is defensive only for malformed configuration values.
        return words
    boundaries = (0, *solution[2])
    return [
        " ".join(words[start:end])
        for start, end in zip(boundaries, boundaries[1:])
    ]


def _split_segment(segment: SubtitleSegment, config: SubtitleFinalizationConfig) -> list[SubtitleSegment]:
    text = _clean_text(segment.text)
    if not text:
        return []
    start_ms = int(segment.start_ms)
    source_end_ms = int(segment.end_ms)
    span_ms = max(1, source_end_ms - start_ms)
    max_duration_ms = max(1, int(config.max_duration_ms))
    duration_chunks = min(
        max(1, ceil(span_ms / max_duration_ms)),
        len(text.split()),
    )
    chunks = _split_words_to_capacity(
        text,
        config,
        min_chunks=duration_chunks,
    )
    if not chunks:
        return []

    chunk_count = len(chunks)
    max_gap_ms = max(0, int(config.min_gap_ms))
    min_duration_ms = max(1, int(config.min_duration_ms))
    reading_durations = [
        ceil(
            len(_clean_text(chunk))
            / max(float(config.max_chars_per_second), 0.001)
            * 1000
        )
        for chunk in chunks
    ]
    minimum_durations = [
        min(max_duration_ms, max(1, min_duration_ms, reading_duration))
        for reading_duration in reading_durations
    ]
    minimum_floor_durations = [
        min(max_duration_ms, min_duration_ms) for _ in chunks
    ]

    if chunk_count > 1:
        max_gap_for_minimums = max(
            0,
            (span_ms - sum(minimum_durations)) // (chunk_count - 1),
        )
        gap_ms = min(max_gap_ms, max_gap_for_minimums)
    else:
        gap_ms = 0
    available_ms = span_ms - gap_ms * max(0, chunk_count - 1)

    def allocate_durations(
        total_ms: int,
        floors: list[int],
    ) -> list[int]:
        if total_ms < chunk_count:
            return [1] * chunk_count
        durations = [1] * chunk_count
        floor_total = sum(floors)
        if total_ms >= floor_total:
            durations = list(floors)
        remaining = max(0, total_ms - sum(durations))
        capacities = [max(0, max_duration_ms - duration) for duration in durations]
        weights = [max(1, len(_clean_text(chunk))) for chunk in chunks]
        while remaining and any(capacities):
            active = [index for index, capacity in enumerate(capacities) if capacity]
            weight_total = sum(weights[index] for index in active)
            allocations = {
                index: min(
                    capacities[index],
                    (remaining * weights[index]) // weight_total,
                )
                for index in active
            }
            allocated = sum(allocations.values())
            if not allocated:
                index = max(active, key=lambda value: (weights[value], -value))
                allocations[index] = 1
                allocated = 1
            for index, amount in allocations.items():
                durations[index] += amount
                capacities[index] -= amount
            remaining -= allocated
        return durations

    if available_ms >= chunk_count:
        if available_ms >= sum(minimum_durations):
            durations = allocate_durations(available_ms, minimum_durations)
        elif available_ms >= sum(minimum_floor_durations):
            durations = allocate_durations(available_ms, minimum_floor_durations)
        else:
            durations = allocate_durations(available_ms, [1] * chunk_count)
        sequential = True
    else:
        # There is no way to fit one positive millisecond per cue with the
        # requested gap inside this interval.  Keep each cue positive and
        # bounded, allowing overlap as the irreducible fallback.
        durations = [min(max_duration_ms, span_ms)] * chunk_count
        gap_ms = 0
        sequential = False

    cursor = start_ms
    output: list[SubtitleSegment] = []
    for index, (chunk, duration_ms) in enumerate(zip(chunks, durations)):
        if sequential:
            end = min(source_end_ms, cursor + duration_ms)
        else:
            end = min(source_end_ms, start_ms + duration_ms)
        end = max(cursor + 1, end)
        output.append(
            SubtitleSegment(
                index=0,
                start_ms=cursor,
                end_ms=end,
                text=wrap_subtitle_text(chunk, config),
                speaker=segment.speaker,
            )
        )
        if sequential:
            cursor = end + gap_ms
    return output


def _adjust_durations(
    segments: list[SubtitleSegment],
    config: SubtitleFinalizationConfig,
) -> list[SubtitleSegment]:
    # Source order can contain overlapping turns with backwards start times.
    # Order presentation events before considering available trailing space.
    segments = sorted(segments, key=lambda cue: cue.start_ms)
    adjusted: list[SubtitleSegment] = []
    for index, cue in enumerate(segments):
        next_start = segments[index + 1].start_ms if index + 1 < len(segments) else None
        visible_chars = len(_clean_text(cue.text))
        reading_duration = round((visible_chars / config.max_chars_per_second) * 1000)
        desired_duration = max(config.min_duration_ms, reading_duration)
        maximum_end = cue.start_ms + config.max_duration_ms
        base_end = min(max(cue.start_ms + 1, cue.end_ms), maximum_end)
        desired_end = min(max(base_end, cue.start_ms + desired_duration), maximum_end)
        if next_start is not None:
            gap_limited_end = next_start - config.min_gap_ms
            if gap_limited_end >= base_end:
                desired_end = min(desired_end, gap_limited_end)
            else:
                # Only added reading time is expendable. A following speaker
                # must not truncate speech already present in this interval.
                desired_end = base_end
        end = desired_end
        adjusted.append(
            SubtitleSegment(
                index=index + 1,
                start_ms=cue.start_ms,
                end_ms=end,
                text=cue.text,
                speaker=cue.speaker,
            )
        )
    return adjusted


def finalize_segments(segments: list[SubtitleSegment], config: SubtitleFinalizationConfig) -> list[SubtitleSegment]:
    output: list[SubtitleSegment] = []
    for segment in segments:
        for item in _split_segment(segment, config):
            next_start = item.start_ms
            next_end = min(item.end_ms, next_start + config.max_duration_ms)
            output.append(
                SubtitleSegment(
                    index=0,
                    start_ms=next_start,
                    end_ms=next_end,
                    text=item.text,
                    speaker=item.speaker,
                )
            )
    return _adjust_durations(output, config)


def finalize_srt_content(content: str, settings: dict[str, Any] | None = None) -> str:
    return compose_srt(finalize_segments(parse_srt(content), SubtitleFinalizationConfig.from_settings(settings)))


def finalize_srt_file(
    source: str | Path,
    destination: str | Path,
    settings: dict[str, Any] | None = None,
) -> str:
    source_path, destination_path = Path(source), Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(
        finalize_srt_content(source_path.read_text(encoding="utf-8-sig"), settings),
        encoding="utf-8",
    )
    return str(destination_path)


def _sanitize_timed_words(words: list[_TimedWord]) -> list[_TimedWord]:
    """Repair implausible word spans without discarding the transcript.

    Some ASR backends attach the whole following silence to the preceding word.
    Keeping such a span (we have observed 16-second single words) defeats both
    maximum cue duration and silence detection.  The median-based cap is
    deliberately conservative. A following word from the same speaker may
    bound the span; a different speaker can legitimately talk over it.
    """

    if not words:
        return []
    plausible = [word.end_ms - word.start_ms for word in words if 20 <= word.end_ms - word.start_ms <= 2000]
    typical_duration = int(median(plausible)) if plausible else 160
    word_duration_cap = max(800, min(1600, typical_duration * 8))
    output: list[_TimedWord] = []
    previous_start = -1
    for index, word in enumerate(words):
        start = max(word.start_ms, previous_start)
        next_start = words[index + 1].start_ms if index + 1 < len(words) else None
        # Durations within the same range used to estimate normal speech are
        # already plausible. Only cap outliers, not ordinary sustained words.
        end = word.end_ms
        if end - start > 2000:
            end = start + word_duration_cap
        if (
            next_start is not None
            and next_start > start
            and words[index + 1].speaker == word.speaker
        ):
            end = min(end, next_start)
        end = max(start + 20, end)
        output.append(
            _TimedWord(
                index=index,
                text=word.text,
                start_ms=start,
                end_ms=end,
                speaker=word.speaker,
                source_segment_id=word.source_segment_id,
            )
        )
        previous_start = start
    return output


def _normalized_overlap_token(text: str) -> str:
    normalized = re.sub(r"[^\w]+", "", _clean_text(text).casefold())
    return normalized or _clean_text(text).casefold()


def _deduplicate_moss_overlap_words(words: list[_TimedWord]) -> list[_TimedWord]:
    """Remove only strong, time-aligned duplicated runs at MOSS chunk seams.

    MOSS-diarize emits independently decoded native turns. Older Pandrator
    defaults asked CrispASR for a three-second chunk overlap, but that backend
    has no token stream during chunk stitching, so the overlap could appear
    twice and even carry different local speaker IDs. Isolated repeated words
    can be real overlapping speech; this guard therefore requires either a
    substantial four-word run or a tightly time-aligned three-word run from
    different source segment IDs.
    """

    groups: dict[str, list[_TimedWord]] = {}
    for word in words:
        if word.source_segment_id:
            groups.setdefault(word.source_segment_id, []).append(word)
    group_items = sorted(
        groups.items(),
        key=lambda item: (
            min(word.start_ms for word in item[1]),
            max(word.end_ms for word in item[1]),
        ),
    )
    if len(group_items) < 2:
        return words

    dropped: set[int] = set()
    speaker_overrides: dict[int, str] = {}
    for left_position, (_left_id, left_words) in enumerate(group_items):
        left_start = min(word.start_ms for word in left_words)
        left_end = max(word.end_ms for word in left_words)
        left_tokens = [_normalized_overlap_token(word.text) for word in left_words]
        for _right_id, right_words in group_items[left_position + 1 :]:
            right_start = min(word.start_ms for word in right_words)
            right_end = max(word.end_ms for word in right_words)
            if right_start > left_end:
                break
            if min(left_end, right_end) - max(left_start, right_start) < 200:
                continue
            right_tokens = [_normalized_overlap_token(word.text) for word in right_words]
            matcher = SequenceMatcher(None, left_tokens, right_tokens, autojunk=False)
            for match in matcher.get_matching_blocks():
                visible_chars = sum(
                    len(right_words[match.b + offset].text)
                    for offset in range(match.size)
                )
                offsets = [
                    abs(
                        left_words[match.a + offset].start_ms
                        - right_words[match.b + offset].start_ms
                    )
                    for offset in range(match.size)
                ]
                strong_long_run = (
                    match.size >= 4
                    and visible_chars >= 20
                    and sum(offset <= 1000 for offset in offsets) / match.size >= 0.75
                )
                strong_short_run = (
                    match.size >= 3
                    and visible_chars >= 12
                    and all(offset <= 400 for offset in offsets)
                )
                if not (strong_long_run or strong_short_run):
                    continue
                # The common MOSS failure is a suffix/prefix overlap where the
                # later turn continues with new words. Keep that later stream
                # and remove the earlier duplicate suffix so a speaker change
                # happens once, at the start of the repeated phrase. Other
                # duplicate shapes retain the earlier stream.
                suffix_prefix_overlap = (
                    match.a + match.size >= len(left_words) - 1
                    and match.b <= 1
                )
                duplicate_words = left_words if suffix_prefix_overlap else right_words
                duplicate_start = match.a if suffix_prefix_overlap else match.b
                dropped.update(
                    id(duplicate_words[duplicate_start + offset])
                    for offset in range(match.size)
                )

                if suffix_prefix_overlap:
                    left_speaker = left_words[match.a].speaker
                    right_speaker = right_words[match.b].speaker
                    # A one- or two-word fragment immediately before a large
                    # duplicated suffix is usually a decoder omission, not a
                    # real one-word speaker turn. Carry it with the retained
                    # later stream unless a sentence boundary separates it.
                    prefix_tail = left_words[max(0, match.a - 2) : match.a]
                    if (
                        left_speaker
                        and right_speaker
                        and left_speaker != right_speaker
                        and 0 < match.a <= 2
                        and prefix_tail
                        and not any(
                            _last_syntactic_char(word.text) in _SENTENCE_END_CHARS
                            for word in prefix_tail
                        )
                    ):
                        speaker_overrides.update(
                            {id(word): right_speaker for word in prefix_tail}
                        )

    if not dropped:
        return words
    logger.warning(
        "Removed %d duplicated MOSS word(s) from time-aligned chunk seams.",
        len(dropped),
    )
    return [
        replace(word, speaker=speaker_overrides.get(id(word), word.speaker))
        for word in words
        if id(word) not in dropped
    ]


def _timed_words(payload: NormalizedTranscript | Any) -> list[_TimedWord]:
    transcript = normalize_transcript(payload)
    result: list[_TimedWord] = []
    for segment in transcript.segments:
        for word in segment.words:
            result.append(
                _TimedWord(
                    index=len(result),
                    text=word.text,
                    start_ms=word.start_ms,
                    end_ms=word.end_ms,
                    speaker=word.speaker or segment.speaker,
                    source_segment_id=str(word.metadata.get("moss_segment_id") or ""),
                )
            )
    result.sort(key=lambda word: (word.start_ms, word.end_ms, word.index))
    result = [replace(word, index=index) for index, word in enumerate(result)]
    return _sanitize_timed_words(_deduplicate_moss_overlap_words(result))


def _source_text_and_spans(words: list[_TimedWord]) -> tuple[str, list[_TimedWord]]:
    text = ""
    output: list[_TimedWord] = []
    for word in words:
        token = _clean_text(word.text)
        if not token:
            continue
        separator = ""
        if (
            text
            and token[0] not in _NO_SPACE_BEFORE
            and text[-1] not in _NO_SPACE_AFTER
            and not token.startswith(("'", "\u2019"))
        ):
            separator = " "
        text += separator
        start = len(text)
        text += token
        output.append(
            _TimedWord(
                index=len(output),
                text=token,
                start_ms=word.start_ms,
                end_ms=word.end_ms,
                speaker=word.speaker,
                source_segment_id=word.source_segment_id,
                char_start=start,
                char_end=len(text),
            )
        )
    return text, output


def _cue_plain_text(words: list[_TimedWord]) -> str:
    return _join_tokens([item.text for item in words])


def _cue_text(words: list[_TimedWord], config: SubtitleFinalizationConfig) -> str:
    return wrap_subtitle_text(_cue_plain_text(words), config)


def _last_syntactic_char(text: str) -> str:
    cleaned = _clean_text(text).rstrip()
    while cleaned and cleaned[-1] in _TRAILING_MARKS:
        cleaned = cleaned[:-1].rstrip()
    return cleaned[-1] if cleaned else ""


def _normalized_lexeme(text: str) -> str:
    return _clean_text(text).strip(".,!?;:\"'()[]{}\u2018\u2019\u201c\u201d").casefold()


def _sat_boundary_probabilities(
    source_text: str,
    words: list[_TimedWord],
    threshold: float,
) -> list[float]:
    if len(words) < 2 or not source_text:
        return []
    try:
        prediction = sentence_segmenter.predict_boundaries(
            source_text,
            threshold=threshold,
        )
    except Exception as exc:  # pragma: no cover - defensive around optional model runtimes
        logger.warning("SaT boundary prediction failed; using deterministic boundary evidence: %s", exc)
        prediction = None
    if not prediction:
        return [0.0] * (len(words) - 1)

    raw_probabilities = prediction.get("probabilities")
    probabilities = list(raw_probabilities) if isinstance(raw_probabilities, (list, tuple)) else []
    raw_boundaries = prediction.get("boundaries")
    explicit: set[int] = set()
    boundary_values = raw_boundaries if isinstance(raw_boundaries, (list, tuple)) else []
    for value in boundary_values:
        boundary_index = value.get("index") if isinstance(value, dict) else value
        if isinstance(boundary_index, (int, float)):
            explicit.add(int(boundary_index))
    raw_threshold = prediction.get("threshold")
    predicted_threshold = (
        float(raw_threshold) if isinstance(raw_threshold, (int, float)) else threshold
    )
    effective_threshold = max(
        0.01,
        min(0.99, predicted_threshold),
    )
    output: list[float] = []
    for current, following in zip(words, words[1:]):
        left = max(0, current.char_end - 1)
        right = min(len(probabilities), max(left + 1, following.char_start + 1))
        probability = max((float(value) for value in probabilities[left:right]), default=0.0)
        is_explicit = any(left <= boundary < right for boundary in explicit)
        if probability < effective_threshold and not is_explicit:
            output.append(0.0)
            continue
        normalized = max(
            0.0,
            min(
                1.0,
                (probability - effective_threshold) / (1.0 - effective_threshold),
            ),
        )
        # Crossing the configured threshold is meaningful even if the raw
        # score only just clears it. This floor keeps threshold-selected SaT
        # boundaries competitive with punctuation without making them hard.
        output.append(max(0.20 if is_explicit else 0.0, normalized))
    return output


def _boundary_evidence(
    words: list[_TimedWord],
    probabilities: list[float],
    config: SubtitleFinalizationConfig,
) -> list[_BoundaryEvidence]:
    output: list[_BoundaryEvidence] = []
    for index, (current, following) in enumerate(zip(words, words[1:])):
        last_char = _last_syntactic_char(current.text)
        following_lexeme = _normalized_lexeme(following.text)
        current_lexeme = _normalized_lexeme(current.text)
        gap_ms = max(0, following.start_ms - current.end_ms)
        output.append(
            _BoundaryEvidence(
                probability=probabilities[index] if index < len(probabilities) else 0.0,
                gap_ms=gap_ms,
                sentence_end=last_char in _SENTENCE_END_CHARS,
                punctuation=last_char in _CLAUSE_PUNCTUATION,
                clause_start=following_lexeme in _CLAUSE_STARTERS,
                speaker_change=current.speaker != following.speaker,
                hard_silence=gap_ms >= config.hard_gap_ms,
                weak_start=following_lexeme in _WEAK_CUE_STARTS,
                weak_end=current_lexeme in _WEAK_CUE_ENDS,
            )
        )
    return output


def _boundary_reward(evidence: _BoundaryEvidence, config: SubtitleFinalizationConfig) -> float:
    reward = 0.0
    if evidence.speaker_change:
        reward -= 120.0
    if evidence.hard_silence:
        reward -= 90.0
    if evidence.sentence_end:
        reward -= 74.0
    reward -= 50.0 * evidence.probability
    if evidence.punctuation:
        reward -= 20.0
    if evidence.clause_start:
        reward -= 9.0
    if evidence.gap_ms >= config.phrase_gap_ms:
        reward -= 34.0
    elif evidence.gap_ms >= 350:
        reward -= 15.0
    elif evidence.gap_ms >= 200:
        reward -= 6.0
    if evidence.weak_start:
        reward += 18.0
    if evidence.weak_end:
        reward += 20.0
    if not (
        evidence.speaker_change
        or evidence.hard_silence
        or evidence.sentence_end
        or evidence.punctuation
        or evidence.clause_start
        or evidence.probability >= 0.15
        or evidence.gap_ms >= 200
    ):
        reward += 14.0
    return reward


def _cue_cost(
    words: list[_TimedWord],
    start: int,
    end: int,
    text: str,
    evidence: list[_BoundaryEvidence],
    config: SubtitleFinalizationConfig,
) -> float:
    visible_chars = len(_clean_text(text))
    raw_duration = max(100, words[end - 1].end_ms - words[start].start_ms)
    if end < len(words):
        available_duration = max(
            raw_duration,
            words[end].start_ms - config.min_gap_ms - words[start].start_ms,
        )
    else:
        available_duration = raw_duration
    readable_duration = min(config.max_duration_ms, max(config.min_duration_ms, available_duration))
    cps = visible_chars / max(0.1, readable_duration / 1000.0)

    # A fixed cue cost avoids excessive fragmentation.  Short-cue penalties are
    # intentionally steep because the most visible failure mode is a dangling
    # article or sentence tail that could have remained with its neighbours.
    cost = 30.0
    if visible_chars < 10:
        cost += 130.0 + (10 - visible_chars) * 5.0
    elif visible_chars < 20:
        cost += 20.0 + (20 - visible_chars) * 1.5
    elif visible_chars < 28:
        cost += (28 - visible_chars) * 1.2

    target_chars = min(68, max(36, round(config.max_event_chars * 0.72)))
    if visible_chars > target_chars:
        cost += (visible_chars - target_chars) * 0.35
    if raw_duration < config.min_duration_ms:
        cost += (config.min_duration_ms - raw_duration) / 80.0
    if cps > config.max_chars_per_second:
        # Reading speed is a presentation warning, not a reason to chop one
        # fast-spoken phrase into several even less readable fragments.
        cost += ((cps - config.max_chars_per_second) ** 2) * 0.5

    first_lexeme = _normalized_lexeme(words[start].text)
    last_lexeme = _normalized_lexeme(words[end - 1].text)
    if start > 0 and first_lexeme in _WEAK_CUE_STARTS:
        cost += 16.0
    if end < len(words) and last_lexeme in _WEAK_CUE_ENDS:
        cost += 18.0

    if end < len(words):
        cost += _boundary_reward(evidence[end - 1], config)
    elif _last_syntactic_char(words[end - 1].text) in _SENTENCE_END_CHARS:
        cost -= 18.0
    return cost


def _compose_semantic_cues(
    words: list[_TimedWord],
    source_text: str,
    config: SubtitleFinalizationConfig,
) -> list[SubtitleSegment]:
    if not words:
        return []
    probabilities = _sat_boundary_probabilities(
        source_text,
        words,
        config.sentence_boundary_threshold,
    )
    evidence = _boundary_evidence(words, probabilities, config)
    word_count = len(words)
    costs = [float("inf")] * (word_count + 1)
    previous: list[int | None] = [None] * (word_count + 1)
    costs[0] = 0.0

    for start in range(word_count):
        if costs[start] == float("inf"):
            continue
        for end in range(start + 1, word_count + 1):
            # Do not permit a cue to cross a speaker change or an unambiguous
            # long silence.  It may, of course, end immediately before either.
            crossed_boundary = end - 2
            if crossed_boundary >= start:
                crossed = evidence[crossed_boundary]
                if crossed.speaker_change or crossed.hard_silence:
                    break

            duration = words[end - 1].end_ms - words[start].start_ms
            text = _cue_plain_text(words[start:end])
            pathological_single_word = end == start + 1
            if duration > config.max_duration_ms and not pathological_single_word:
                break
            if (
                not pathological_single_word
                and (len(text) > config.max_event_chars or not _fits_layout(text, config))
            ):
                break

            candidate_cost = costs[start] + _cue_cost(words, start, end, text, evidence, config)
            if candidate_cost < costs[end]:
                costs[end] = candidate_cost
                previous[end] = start

    if previous[word_count] is None:
        logger.warning("Global subtitle composition found no valid path; using capacity finalization")
        fallback = SubtitleSegment(
            0,
            words[0].start_ms,
            words[-1].end_ms,
            _cue_plain_text(words),
            words[0].speaker,
        )
        return finalize_segments([fallback], config)

    ranges: list[tuple[int, int]] = []
    cursor = word_count
    while cursor > 0:
        range_start = previous[cursor]
        if range_start is None:  # pragma: no cover - guarded by the valid final path above
            break
        ranges.append((range_start, cursor))
        cursor = range_start
    ranges.reverse()
    return [
        SubtitleSegment(
            index=index,
            start_ms=words[start].start_ms,
            end_ms=min(words[end - 1].end_ms, words[start].start_ms + config.max_duration_ms),
            text=_cue_plain_text(words[start:end]),
            speaker=words[start].speaker,
        )
        for index, (start, end) in enumerate(ranges, start=1)
    ]


def _coalesce_semantic_cues(
    cues: list[SubtitleSegment],
    config: SubtitleFinalizationConfig,
) -> list[SubtitleSegment]:
    """Join compact same-speaker fragments left by source cue punctuation.

    The DP uses punctuation as useful boundary evidence.  A source cue can
    nevertheless carry a misleading period, so this final semantic pass
    removes a short dangling fragment when all timing, speaker, capacity, and
    reading-speed invariants still hold.
    """

    coalesced: list[SubtitleSegment] = []
    for cue in cues:
        if not coalesced:
            coalesced.append(cue)
            continue
        previous = coalesced[-1]
        gap_ms = cue.start_ms - previous.end_ms
        combined_text = _join_tokens([previous.text, cue.text])
        combined_duration = cue.end_ms - previous.start_ms
        combined_cps = len(_clean_text(combined_text)) / max(
            0.1, combined_duration / 1000.0
        )
        can_merge = (
            previous.speaker == cue.speaker
            and gap_ms >= 0
            and gap_ms < config.phrase_gap_ms
            and gap_ms < config.hard_gap_ms
            and combined_duration <= config.max_duration_ms
            and len(combined_text) <= config.max_event_chars
            and _fits_layout(combined_text, config)
            and combined_cps <= config.max_chars_per_second
        )
        if can_merge:
            coalesced[-1] = SubtitleSegment(
                index=0,
                start_ms=previous.start_ms,
                end_ms=cue.end_ms,
                text=combined_text,
                speaker=previous.speaker,
            )
        else:
            coalesced.append(cue)
    return coalesced


def compose_transcript_segments(
    metadata_path_or_normalized_transcript: str | Path | NormalizedTranscript | Any,
    settings: dict[str, Any] | None = None,
) -> list[SubtitleSegment]:
    """Compose a normalized transcript into deterministic display cues.

    Word-timed runs are composed semantically across their original cue
    boundaries.  A source segment without words is a hard run boundary, but is
    retained as an ordinary finalized cue instead of disappearing from the
    published transcript.
    """

    transcript = (
        load_transcript(metadata_path_or_normalized_transcript)
        if isinstance(metadata_path_or_normalized_transcript, (str, Path))
        else normalize_transcript(metadata_path_or_normalized_transcript)
    )
    config = SubtitleFinalizationConfig.from_settings(settings)
    output: list[SubtitleSegment] = []
    timed_run: list[TimedSegment] = []

    def flush_timed_run() -> None:
        nonlocal timed_run
        if not timed_run:
            return
        run_transcript = NormalizedTranscript(
            segments=tuple(timed_run),
            source_format=transcript.source_format,
            language=transcript.language,
            metadata=transcript.metadata,
        )
        words = _timed_words(run_transcript)
        if words:
            source_text, words = _source_text_and_spans(words)
            semantic_cues = _compose_semantic_cues(words, source_text, config)
            output.extend(_coalesce_semantic_cues(semantic_cues, config))
        timed_run = []

    for segment in transcript.segments:
        if segment.words:
            timed_run.append(segment)
            continue
        flush_timed_run()
        output.extend(
            _split_segment(
                SubtitleSegment(
                    0,
                    segment.start_ms,
                    segment.end_ms,
                    segment.text,
                    segment.speaker,
                ),
                config,
            )
        )
    flush_timed_run()

    adjusted = _adjust_durations(output, config)
    return [
        SubtitleSegment(
            index=index,
            start_ms=cue.start_ms,
            end_ms=cue.end_ms,
            text=wrap_subtitle_text(cue.text, config),
            speaker=cue.speaker,
        )
        for index, cue in enumerate(adjusted, start=1)
    ]


def compose_from_transcript_json(
    metadata_path: str | Path,
    settings: dict[str, Any] | None = None,
) -> str:
    return compose_srt(compose_transcript_segments(metadata_path, settings))


def compose_from_crispasr_json(
    metadata_path: str | Path,
    settings: dict[str, Any] | None = None,
) -> str:
    """Backward-compatible name for the engine-neutral transcript composer."""

    return compose_from_transcript_json(metadata_path, settings)
