"""Bounded, deterministic rebalancing of already composed display cues.

This module operates on display-cue dictionaries only.  It does not call a
model and deliberately considers at most three original adjacent cues at a
time, so a successful rebalance cannot cascade through an entire transcript.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from math import ceil
from typing import Any

from .subtitle_finalization import (
    SubtitleFinalizationConfig,
    wrap_subtitle_text,
)

_REVIEW_FIELDS = (
    "review_state",
    "review_note",
    "evidence_ids",
    "uncertain_source_cue_ids",
)
_SENTENCE_END = set(".!?\u2026\u3002\uff01\uff1f")
_CLAUSE_END = set(",;:\u2013\u2014\u3001\uff0c\uff1b\uff1a")
_CLOSING_MARKS = set("\"')]}”’»›」』）】｝］〉》")
_FUNCTION_WORDS = {
    "a",
    "an",
    "the",
    "of",
    "to",
    "for",
    "from",
    "with",
    "without",
    "on",
    "in",
    "at",
    "and",
    "or",
    "but",
    "because",
    "if",
    "that",
    "auf",
    "an",
    "mit",
    "für",
    "von",
    "zu",
    "der",
    "die",
    "das",
    "ein",
    "eine",
    "und",
    "oder",
    "weil",
}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _visible_length(value: str) -> int:
    return len(value.replace("\n", " "))


def _lexeme(value: str) -> str:
    """Return a stable lexical form for punctuation-insensitive comparison."""

    return "".join(
        character.casefold()
        for character in str(value)
        if not unicodedata.category(character).startswith("P")
    )


def _last_syntactic_char(value: str) -> str:
    text = _clean_text(value).rstrip()
    while text and text[-1] in _CLOSING_MARKS:
        text = text[:-1].rstrip()
    return text[-1] if text else ""


def _ends_sentence(value: str) -> bool:
    text = _clean_text(value)
    while text and text[-1] in _CLOSING_MARKS:
        text = text[:-1].rstrip()
    # A German ordinal ("19. Jahrhundert") is not a sentence boundary.
    if len(text) > 1 and text[-1] == "." and text[-2].isdigit():
        return False
    return bool(text) and text[-1] in _SENTENCE_END


def _ends_clause(value: str) -> bool:
    return _last_syntactic_char(value) in _CLAUSE_END


def _ends_function_word(value: str) -> bool:
    tokens = _clean_text(value).split()
    return bool(tokens and _lexeme(tokens[-1]) in _FUNCTION_WORDS)


def _has_eligible_boundary(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _ends_sentence(str(left.get("text") or "")) or _ends_clause(
        str(left.get("text") or "")
    ):
        return False
    return min(
        _visible_length(_clean_text(left.get("text"))),
        _visible_length(_clean_text(right.get("text"))),
    ) < 28 or _ends_function_word(str(left.get("text") or ""))


def _signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(item.get(field) for field in ("speaker", *_REVIEW_FIELDS))


def _same_signature(window: list[dict[str, Any]]) -> bool:
    if not window:
        return False
    speaker = window[0].get("speaker")
    return bool(speaker) and all(
        _signature(item) == _signature(window[0]) for item in window
    )


def _interval(item: dict[str, Any]) -> tuple[int, int]:
    return int(item.get("start_ms", 0)), int(item.get("end_ms", 0))


def _overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return max(first[0], second[0]) < min(first[1], second[1])


@dataclass(frozen=True)
class _SourceWord:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class _TokenTime:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class _Range:
    start: int
    end: int
    start_ms: int
    end_ms: int
    text: str


def _source_words_for_window(
    timing_words: list[dict[str, Any]] | None,
    window_start: int,
    window_end: int,
    speaker: str,
) -> list[_SourceWord]:
    if not timing_words:
        return []
    output: list[_SourceWord] = []
    for item in timing_words:
        if item.get("speaker") != speaker:
            continue
        try:
            start_ms, end_ms = int(item["start_ms"]), int(item["end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if end_ms <= start_ms or end_ms <= window_start or start_ms >= window_end:
            continue
        text = _clean_text(item.get("text"))
        if not text:
            continue
        output.append(_SourceWord(text, start_ms, end_ms))
    output.sort(key=lambda word: (word.start_ms, word.end_ms, word.text))
    return output


def _source_silences(
    source_words: list[_SourceWord],
    window_start: int,
    window_end: int,
    hard_gap_ms: int,
) -> list[tuple[int, int]]:
    silences: list[tuple[int, int]] = []
    if not source_words:
        return silences
    covered_end = source_words[0].end_ms
    for following in source_words[1:]:
        start_ms = max(window_start, covered_end)
        end_ms = min(window_end, following.start_ms)
        if end_ms - start_ms >= hard_gap_ms:
            silences.append((start_ms, end_ms))
        covered_end = max(covered_end, following.end_ms)
    return silences


def _covers_gap(
    source_words: list[_SourceWord],
    gap_start: int,
    gap_end: int,
    config: SubtitleFinalizationConfig,
) -> bool:
    if not source_words:
        return False
    nearby = [
        word
        for word in source_words
        if word.end_ms >= gap_start - config.phrase_gap_ms
        and word.start_ms <= gap_end + config.phrase_gap_ms
    ]
    if not nearby:
        return False
    if not any(word.start_ms < gap_end and word.end_ms > gap_start for word in nearby):
        return False
    first_speech = min(word.start_ms for word in nearby)
    last_speech = max(word.end_ms for word in nearby)
    if first_speech > gap_start + config.phrase_gap_ms:
        return False
    if last_speech < gap_end - config.phrase_gap_ms:
        return False
    covered_end = nearby[0].end_ms
    for following in nearby[1:]:
        if following.start_ms - covered_end >= config.hard_gap_ms:
            return False
        covered_end = max(covered_end, following.end_ms)
    return True


def _window_gates(
    window: list[dict[str, Any]],
    source_words: list[_SourceWord],
    config: SubtitleFinalizationConfig,
    *,
    timing_supplied: bool,
    estimated: bool,
) -> bool:
    window_start = _interval(window[0])[0]
    window_end = max(_interval(item)[1] for item in window)
    source_silences = _source_silences(
        source_words, window_start, window_end, config.hard_gap_ms
    )
    if estimated and source_silences:
        return False
    for left, right in zip(window, window[1:]):
        left_end = _interval(left)[1]
        right_start = _interval(right)[0]
        gap = right_start - left_end
        if gap > config.phrase_gap_ms:
            if not timing_supplied or not _covers_gap(
                source_words, left_end, right_start, config
            ):
                return False
        if estimated and gap >= config.hard_gap_ms:
            if not _covers_gap(source_words, left_end, right_start, config):
                return False
    return True


def _matched_token_times(
    tokens: list[str],
    source_words: list[_SourceWord],
    window_start: int,
    window_end: int,
) -> list[_TokenTime] | None:
    if not source_words or not tokens:
        return None
    token_lexemes = [_lexeme(token) for token in tokens]
    source_lexemes = [_lexeme(word.text) for word in source_words]
    matcher = SequenceMatcher(None, token_lexemes, source_lexemes, autojunk=False)
    matched: dict[int, _SourceWord] = {}
    for token_start, source_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            matched[token_start + offset] = source_words[source_start + offset]
    if len(matched) < 2 or len(matched) / len(tokens) < 0.60:
        return None

    offsets: list[int] = []
    cursor = 0
    for index, token in enumerate(tokens):
        offsets.append(cursor)
        cursor += len(token) + (1 if index + 1 < len(tokens) else 0)
    total_length = max(1, cursor)
    known_starts = {
        index: max(window_start, min(window_end - 1, word.start_ms))
        for index, word in matched.items()
    }
    anchors = [(-1, 0, window_start)]
    anchors.extend(
        (index, offsets[index], start) for index, start in sorted(known_starts.items())
    )
    anchors.append((len(tokens), total_length, window_end))
    starts: list[int] = [window_start] * len(tokens)
    for (left_index, left_offset, left_time), (
        right_index,
        right_offset,
        right_time,
    ) in zip(anchors, anchors[1:]):
        distance = max(1, right_offset - left_offset)
        for index in range(left_index + 1, right_index):
            fraction = (offsets[index] - left_offset) / distance
            starts[index] = round(left_time + fraction * (right_time - left_time))
    for index, start in known_starts.items():
        starts[index] = start
    for index in range(1, len(starts)):
        starts[index] = max(starts[index], starts[index - 1])
    output: list[_TokenTime] = []
    for index, token in enumerate(tokens):
        if index in matched:
            end_ms = min(window_end, max(window_start + 1, matched[index].end_ms))
        else:
            end_ms = window_end if index + 1 == len(tokens) else starts[index + 1]
            end_ms = max(starts[index] + 1, min(window_end, end_ms))
        if (
            end_ms <= starts[index]
            or starts[index] < window_start
            or end_ms > window_end
        ):
            return None
        output.append(_TokenTime(token, starts[index], end_ms))
    return output


def _estimated_token_times(
    tokens: list[str],
    window_start: int,
    window_end: int,
) -> list[_TokenTime] | None:
    if window_end - window_start < len(tokens):
        return None
    total_chars = max(1, sum(len(token) for token in tokens))
    span = window_end - window_start
    output: list[_TokenTime] = []
    cursor = window_start
    consumed = 0
    for index, token in enumerate(tokens):
        consumed += len(token)
        target_end = (
            window_end
            if index + 1 == len(tokens)
            else window_start + round(span * consumed / total_chars)
        )
        remaining_tokens = len(tokens) - index - 1
        end_ms = max(
            cursor + 1,
            min(window_end - remaining_tokens, target_end),
        )
        output.append(_TokenTime(token, cursor, end_ms))
        cursor = end_ms
    return output


def _boundary_score(left_text: str) -> int:
    if _ends_sentence(left_text):
        score = 0
    elif _ends_clause(left_text):
        score = 8
    else:
        score = 40
    if _ends_function_word(left_text):
        score += 40
    return score


def _cue_score(text: str) -> int:
    visible = _visible_length(text)
    score = 12
    if visible < 12:
        score += 50
    elif visible < 28:
        score += 15
    return score


def _stranded_fragment(previous_text: str, following_text: str) -> int:
    if (
        not previous_text
        or _ends_sentence(previous_text)
        or _ends_clause(previous_text)
    ):
        return 0
    following_tokens = following_text.split()[:3]
    if any(_ends_sentence(token) for token in following_tokens):
        return 40
    return 0


def _score_partition_fast(
    ranges: list[tuple[int, int]],
    tokens: list[str],
) -> int:
    """Score a partition without relying on tuple lookup for duplicate ranges."""

    score = sum(_cue_score(" ".join(tokens[start:end])) for start, end in ranges)
    for previous, following in zip(ranges, ranges[1:]):
        left_text = tokens[previous[1] - 1]
        right_text = " ".join(tokens[following[0] : following[1]])
        score += _boundary_score(left_text)
        score += _stranded_fragment(left_text, right_text)
    return score


def _original_partition(
    window: list[dict[str, Any]], tokens: list[str]
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for item in window:
        count = len(_clean_text(item.get("text")).split())
        ranges.append((cursor, cursor + count))
        cursor += count
    if ranges and ranges[-1][1] < len(tokens):
        ranges[-1] = (ranges[-1][0], len(tokens))
    return ranges


def _candidate_range(
    start: int,
    end: int,
    token_times: list[_TokenTime],
    tokens: list[str],
    config: SubtitleFinalizationConfig,
    source_silences: list[tuple[int, int]],
    window_end: int,
) -> _Range | None:
    text = " ".join(tokens[start:end])
    if not text or len(text) > config.max_event_chars:
        return None
    wrapped = wrap_subtitle_text(text, config)
    if len(wrapped.splitlines()) > config.max_lines or any(
        len(line) > config.max_chars_per_line for line in wrapped.splitlines()
    ):
        return None
    start_ms = token_times[start].start_ms
    base_end = max(token.end_ms for token in token_times[start:end])
    if base_end <= start_ms or base_end - start_ms > config.max_duration_ms:
        return None
    if end < len(token_times):
        following_start = token_times[end].start_ms
        if base_end > following_start:
            return None
        allowed_end = max(
            base_end,
            min(window_end, following_start - max(0, int(config.min_gap_ms))),
        )
    else:
        allowed_end = window_end
    reading_ms = ceil(
        _visible_length(text) * 1000 / max(0.001, config.max_chars_per_second)
    )
    required_ms = max(1, int(config.min_duration_ms), reading_ms)
    extension_limit = min(
        allowed_end,
        base_end + max(0, int(config.phrase_gap_ms)),
    )
    end_ms = max(base_end, min(extension_limit, start_ms + required_ms))
    if end_ms < start_ms + required_ms or end_ms <= start_ms:
        return None
    if end_ms - start_ms > config.max_duration_ms:
        return None
    cps = _visible_length(text) * 1000 / (end_ms - start_ms)
    if cps > config.max_chars_per_second + 0.1:
        return None
    if any(
        start_ms < silence_start and end_ms > silence_end
        for silence_start, silence_end in source_silences
    ):
        return None
    return _Range(start, end, start_ms, end_ms, text)


def _partition_candidates(
    token_times: list[_TokenTime],
    config: SubtitleFinalizationConfig,
    source_silences: list[tuple[int, int]],
    window_end: int,
    max_count: int,
    original_boundaries: set[int],
) -> list[tuple[list[_Range], int]]:
    tokens = [item.text for item in token_times]
    range_cache: dict[tuple[int, int], _Range | None] = {}

    def get_range(start: int, end: int) -> _Range | None:
        if end < len(tokens) and end not in original_boundaries:
            left, right = tokens[end - 1], tokens[end]
            if not (_ends_sentence(left) or _ends_clause(left)):
                return None
            # Do not introduce a split inside dates such as "June 29, 1841".
            if left.rstrip(",").isdigit() and right[:1].isdigit():
                return None
        key = (start, end)
        if key not in range_cache:
            range_cache[key] = _candidate_range(
                start, end, token_times, tokens, config, source_silences, window_end
            )
        return range_cache[key]

    # The score is additive at each range start, so one best path per
    # (cue-count, token-end) is sufficient.  This keeps the bounded search
    # polynomial even when a three-cue window contains many words.
    states: list[dict[int, tuple[int, tuple[_Range, ...], tuple[int, ...]]]] = [
        {} for _ in range(max_count + 1)
    ]
    states[0][0] = (0, (), ())
    for count in range(1, max_count + 1):
        for end in range(1, len(tokens) + 1):
            best: tuple[int, tuple[_Range, ...], tuple[int, ...]] | None = None
            for start in range(0, end):
                previous = states[count - 1].get(start)
                if previous is None:
                    continue
                candidate = get_range(start, end)
                if candidate is None:
                    continue
                if previous[1] and candidate.start_ms < previous[1][-1].end_ms:
                    continue
                score = previous[0] + _cue_score(candidate.text)
                if start:
                    previous_token = tokens[start - 1]
                    score += _boundary_score(previous_token)
                    score += _stranded_fragment(previous_token, candidate.text)
                state = (
                    score,
                    (*previous[1], candidate),
                    (*previous[2], end),
                )
                if best is None or (state[0], state[2]) < (best[0], best[2]):
                    best = state
            if best is not None:
                states[count][end] = best

    output: list[tuple[list[_Range], int]] = []
    for count in range(1, max_count + 1):
        final_state = states[count].get(len(tokens))
        if final_state is not None:
            output.append((list(final_state[1]), final_state[0]))
    return output


def _normalised_item(
    item: dict[str, Any], config: SubtitleFinalizationConfig
) -> dict[str, Any]:
    result = dict(item)
    result["text"] = wrap_subtitle_text(_clean_text(item.get("text")), config)
    return result


def rebalance_display_cues(
    values: list[dict[str, Any]],
    config: SubtitleFinalizationConfig,
    *,
    timing_words: list[dict[str, Any]] | None = None,
    match_source_words: bool = False,
) -> list[dict[str, Any]]:
    """Repartition a bounded local run of display cues when it improves score.

    The input list is never mutated.  A failed or inapplicable window returns
    its normalized original cues, making the no-change path safe by default.
    """

    if not values:
        return []
    output: list[dict[str, Any]] = []
    cursor = 0
    all_intervals = [_interval(item) for item in values]
    while cursor < len(values):
        best: (
            tuple[int, int, int, list[_Range], list[str], list[dict[str, Any]]] | None
        ) = None
        for count in (2, 3):
            if cursor + count > len(values):
                continue
            window = values[cursor : cursor + count]
            if not _same_signature(window):
                continue
            intervals = [_interval(item) for item in window]
            window_start = intervals[0][0]
            window_end = max(end for _start, end in intervals)
            if window_end - window_start > 3 * max(1, int(config.max_duration_ms)):
                continue
            if any(
                _overlap(interval, outside)
                for interval in intervals
                for outside_index, outside in enumerate(all_intervals)
                if outside_index < cursor or outside_index >= cursor + count
            ):
                continue
            if not any(
                _has_eligible_boundary(window[index], window[index + 1])
                for index in range(count - 1)
            ):
                continue
            source_words = _source_words_for_window(
                timing_words, window_start, window_end, str(window[0].get("speaker"))
            )
            matched_times = (
                _matched_token_times(
                    [
                        token
                        for item in window
                        for token in _clean_text(item.get("text")).split()
                    ],
                    source_words,
                    window_start,
                    window_end,
                )
                if match_source_words
                else None
            )
            estimated = matched_times is None
            if not _window_gates(
                window,
                source_words,
                config,
                timing_supplied=bool(timing_words),
                estimated=estimated,
            ):
                continue
            tokens = [
                token
                for item in window
                for token in _clean_text(item.get("text")).split()
            ]
            if not tokens:
                continue
            token_times = matched_times or _estimated_token_times(
                tokens, window_start, window_end
            )
            if token_times is None:
                continue
            source_silences = _source_silences(
                source_words, window_start, window_end, config.hard_gap_ms
            )
            original_ranges = _original_partition(window, tokens)
            candidates = _partition_candidates(
                token_times,
                config,
                source_silences,
                window_end,
                count + 1,
                {end for _start, end in original_ranges},
            )
            original_score = _score_partition_fast(original_ranges, tokens)
            improved = [
                (partition, score)
                for partition, score in candidates
                if score < original_score and len(partition) <= count + 1
            ]
            if not improved:
                continue
            partition, candidate_score = min(
                improved,
                key=lambda item: (
                    item[1],
                    len(item[0]),
                    tuple(range_item.end for range_item in item[0]),
                ),
            )
            improvement = original_score - candidate_score
            rendered = [
                {
                    **window[0],
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "text": wrap_subtitle_text(item.text, config),
                }
                for item in partition
            ]
            candidate = (
                -improvement,
                count,
                cursor,
                partition,
                tokens,
                rendered,
            )
            if best is None or candidate[:3] < best[:3]:
                best = candidate
        if best is None:
            output.append(_normalised_item(values[cursor], config))
            cursor += 1
            continue
        output.extend(best[5])
        cursor += best[1]
    return output


__all__ = ["rebalance_display_cues"]
