"""Deterministic, delivery-oriented subtitle cue composition.

This module deliberately has no dependency on speech-block generation.  Speech
blocks optimize text for synthesis; finalized subtitles optimize timed text for
reading and export.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from .. import sentence_segmenter
from .models import SubtitleSegment
from .srt_utils import compose_srt, parse_srt
from .transcript_normalization import (
    NormalizedTranscript,
    format_speaker_label,
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
    phrase_gap_ms: int = 600

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "SubtitleFinalizationConfig":
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
            phrase_gap_ms=max(100, min(3000, int(value("subtitle_phrase_gap_ms", 600)))),
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
    score = abs(len(first) - len(second))
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


def _split_words_to_capacity(text: str, config: SubtitleFinalizationConfig) -> list[str]:
    words = _clean_text(text).split()
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join((*current, word))
        if current and (
            len(candidate) > config.max_event_chars
            or not _fits_layout(candidate, config)
        ):
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks


def _split_segment(segment: SubtitleSegment, config: SubtitleFinalizationConfig) -> list[SubtitleSegment]:
    text = _clean_text(segment.text)
    chunks = _split_words_to_capacity(text, config)
    if not chunks:
        return []
    duration = min(max(1, segment.end_ms - segment.start_ms), config.max_duration_ms * len(chunks))
    weights = [max(1, len(chunk)) for chunk in chunks]
    total_weight = sum(weights)
    cursor = segment.start_ms
    output: list[SubtitleSegment] = []
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        if index == len(chunks) - 1:
            end = min(segment.end_ms, segment.start_ms + duration)
        else:
            share = max(1, round(duration * weight / total_weight))
            end = min(segment.end_ms, cursor + share)
        if end <= cursor:
            end = cursor + 100
        output.append(SubtitleSegment(index=0, start_ms=cursor, end_ms=end, text=wrap_subtitle_text(chunk, config)))
        cursor = end
    return output


def _adjust_durations(
    segments: list[SubtitleSegment],
    config: SubtitleFinalizationConfig,
) -> list[SubtitleSegment]:
    adjusted: list[SubtitleSegment] = []
    for index, cue in enumerate(segments):
        next_start = segments[index + 1].start_ms if index + 1 < len(segments) else None
        visible_chars = len(_clean_text(cue.text))
        reading_duration = round((visible_chars / config.max_chars_per_second) * 1000)
        desired_duration = max(config.min_duration_ms, reading_duration)
        maximum_end = cue.start_ms + config.max_duration_ms
        base_end = min(max(cue.start_ms + 100, cue.end_ms), maximum_end)
        desired_end = min(max(base_end, cue.start_ms + desired_duration), maximum_end)
        if next_start is not None:
            gap_limited_end = next_start - config.min_gap_ms
            if gap_limited_end >= base_end:
                desired_end = min(desired_end, gap_limited_end)
            else:
                # Preserve word timing when the source does not leave room for
                # the requested presentation gap, while still preventing an
                # overlap with the next cue.
                desired_end = min(base_end, next_start)
        adjusted.append(
            SubtitleSegment(
                index=index + 1,
                start_ms=cue.start_ms,
                end_ms=max(cue.start_ms + 100, desired_end),
                text=cue.text,
            )
        )
    return adjusted


def finalize_segments(segments: list[SubtitleSegment], config: SubtitleFinalizationConfig) -> list[SubtitleSegment]:
    output: list[SubtitleSegment] = []
    for segment in segments:
        for item in _split_segment(segment, config):
            next_start = item.start_ms
            next_end = min(item.end_ms, next_start + config.max_duration_ms)
            output.append(SubtitleSegment(index=0, start_ms=next_start, end_ms=next_end, text=item.text))
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
    deliberately conservative and the next word's start remains authoritative.
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
        end = min(word.end_ms, start + word_duration_cap)
        if next_start is not None and next_start > start:
            end = min(end, next_start)
        end = max(start + 20, end)
        output.append(
            _TimedWord(
                index=index,
                text=word.text,
                start_ms=start,
                end_ms=end,
                speaker=word.speaker,
            )
        )
        previous_start = start
    return output


def _timed_words(payload: NormalizedTranscript | Any) -> list[_TimedWord]:
    transcript = normalize_transcript(payload)
    result = [
        _TimedWord(
            index=index,
            text=word.text,
            start_ms=word.start_ms,
            end_ms=word.end_ms,
            speaker=word.speaker,
        )
        for index, word in enumerate(transcript.words)
    ]
    return _sanitize_timed_words(result)


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
                char_start=start,
                char_end=len(text),
            )
        )
    return text, output


def _cue_plain_text(words: list[_TimedWord]) -> str:
    text = _join_tokens([item.text for item in words])
    speaker = words[0].speaker if words else ""
    if speaker:
        text = f"[{format_speaker_label(speaker)}]: {text}"
    return text


def _cue_text(words: list[_TimedWord], config: SubtitleFinalizationConfig) -> str:
    return wrap_subtitle_text(_cue_plain_text(words), config)


def _last_syntactic_char(text: str) -> str:
    cleaned = _clean_text(text).rstrip()
    while cleaned and cleaned[-1] in _TRAILING_MARKS:
        cleaned = cleaned[:-1].rstrip()
    return cleaned[-1] if cleaned else ""


def _normalized_lexeme(text: str) -> str:
    return _clean_text(text).strip(".,!?;:\"'()[]{}\u2018\u2019\u201c\u201d").casefold()


def _sat_boundary_probabilities(source_text: str, words: list[_TimedWord]) -> list[float]:
    if len(words) < 2 or not source_text:
        return []
    try:
        prediction = sentence_segmenter.predict_boundaries(source_text)
    except Exception as exc:  # pragma: no cover - defensive around optional model runtimes
        logger.warning("SaT boundary prediction failed; using deterministic boundary evidence: %s", exc)
        prediction = None
    if not prediction:
        return [0.0] * (len(words) - 1)

    raw_probabilities = prediction.get("probabilities")
    probabilities = list(raw_probabilities) if isinstance(raw_probabilities, (list, tuple)) else []
    raw_boundaries = prediction.get("boundaries")
    explicit: set[int] = set()
    for value in raw_boundaries or []:
        boundary_index = value.get("index") if isinstance(value, dict) else value
        if isinstance(boundary_index, (int, float)):
            explicit.add(int(boundary_index))
    threshold = float(prediction.get("threshold") or 0.25)
    output: list[float] = []
    for current, following in zip(words, words[1:]):
        left = max(0, current.char_end - 1)
        right = min(len(probabilities), max(left + 1, following.char_start + 1))
        probability = max((float(value) for value in probabilities[left:right]), default=0.0)
        if any(left <= boundary < right for boundary in explicit):
            probability = max(probability, threshold)
        output.append(max(0.0, min(1.0, probability)))
    return output


def _boundary_evidence(
    words: list[_TimedWord],
    probabilities: list[float],
    config: SubtitleFinalizationConfig,
) -> list[_BoundaryEvidence]:
    hard_silence_ms = max(1000, min(1500, config.phrase_gap_ms * 2))
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
                hard_silence=gap_ms >= hard_silence_ms,
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
    probabilities = _sat_boundary_probabilities(source_text, words)
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
        fallback = SubtitleSegment(0, words[0].start_ms, words[-1].end_ms, _cue_plain_text(words))
        return finalize_segments([fallback], config)

    ranges: list[tuple[int, int]] = []
    cursor = word_count
    while cursor > 0:
        start = previous[cursor]
        if start is None:  # pragma: no cover - guarded by the valid final path above
            break
        ranges.append((start, cursor))
        cursor = start
    ranges.reverse()
    return [
        SubtitleSegment(
            index=index,
            start_ms=words[start].start_ms,
            end_ms=min(words[end - 1].end_ms, words[start].start_ms + config.max_duration_ms),
            text=_cue_plain_text(words[start:end]),
        )
        for index, (start, end) in enumerate(ranges, start=1)
    ]


def compose_from_transcript_json(
    metadata_path: str | Path,
    settings: dict[str, Any] | None = None,
) -> str:
    transcript = load_transcript(metadata_path)
    config = SubtitleFinalizationConfig.from_settings(settings)
    words = _timed_words(transcript)
    if not words:
        fallback = [
            SubtitleSegment(
                index,
                segment.start_ms,
                segment.end_ms,
                (
                    f"[{format_speaker_label(segment.speaker)}]: {segment.text}"
                    if segment.speaker
                    else segment.text
                ),
            )
            for index, segment in enumerate(transcript.segments, start=1)
        ]
        return compose_srt(finalize_segments(fallback, config))

    source_text, words = _source_text_and_spans(words)
    cues = _adjust_durations(_compose_semantic_cues(words, source_text, config), config)
    display_cues = [
        SubtitleSegment(
            index=index,
            start_ms=cue.start_ms,
            end_ms=cue.end_ms,
            text=wrap_subtitle_text(cue.text, config),
        )
        for index, cue in enumerate(cues, start=1)
    ]
    return compose_srt(display_cues)


def compose_from_crispasr_json(
    metadata_path: str | Path,
    settings: dict[str, Any] | None = None,
) -> str:
    """Backward-compatible name for the engine-neutral transcript composer."""

    return compose_from_transcript_json(metadata_path, settings)
