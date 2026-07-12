"""Deterministic, delivery-oriented subtitle cue composition.

This module deliberately has no dependency on speech-block generation.  Speech
blocks optimize text for synthesis; finalized subtitles optimize timed text for
reading and export.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import SubtitleSegment
from .srt_utils import compose_srt, parse_srt

_SPACE_RE = re.compile(r"\s+")
_NO_SPACE_BEFORE = set(",.!?:;%)]}…")
_NO_SPACE_AFTER = set("([{£€$")
_PREFERRED_BEFORE = {
    "and", "but", "or", "because", "although", "while", "if", "when", "that",
    "for", "from", "with", "without", "into", "onto", "before", "after", "of",
}


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
        desired_end = max(cue.end_ms, cue.start_ms + desired_duration)
        desired_end = min(desired_end, cue.start_ms + config.max_duration_ms)
        if next_start is not None:
            desired_end = min(desired_end, max(cue.end_ms, next_start - config.min_gap_ms))
        adjusted.append(
            SubtitleSegment(
                index=index + 1,
                start_ms=cue.start_ms,
                end_ms=max(cue.end_ms, desired_end),
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


def _timed_words(payload: dict[str, Any]) -> list[tuple[str, int, int, str]]:
    def speaker_value(item: dict[str, Any], fallback: str = "") -> str:
        for key in ("speaker", "speaker_id"):
            value = item.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return fallback

    result: list[tuple[str, int, int, str]] = []
    for segment in payload.get("transcription") or []:
        if not isinstance(segment, dict):
            continue
        segment_speaker = speaker_value(segment)
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            offsets = word.get("offsets") if isinstance(word.get("offsets"), dict) else {}
            try:
                start, end = int(offsets.get("from")), int(offsets.get("to"))
            except (TypeError, ValueError):
                continue
            text = str(word.get("text") or "").strip()
            if text and end > start:
                speaker = speaker_value(word, segment_speaker)
                result.append((text, start, end, speaker))
    return result


def _cue_plain_text(words: list[tuple[str, int, int, str]]) -> str:
    text = _join_tokens([item[0] for item in words])
    speaker = words[0][3] if words else ""
    if speaker:
        label = speaker if speaker.upper().startswith("SPEAKER") else f"SPEAKER_{speaker}"
        text = f"[{label}]: {text}"
    return text


def _cue_text(words: list[tuple[str, int, int, str]], config: SubtitleFinalizationConfig) -> str:
    return wrap_subtitle_text(_cue_plain_text(words), config)


def compose_from_crispasr_json(
    metadata_path: str | Path,
    settings: dict[str, Any] | None = None,
) -> str:
    payload = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    config = SubtitleFinalizationConfig.from_settings(settings)
    words = _timed_words(payload)
    if not words:
        fallback = []
        for index, segment in enumerate(payload.get("transcription") or [], start=1):
            offsets = segment.get("offsets") if isinstance(segment, dict) else {}
            try:
                start, end = int(offsets.get("from")), int(offsets.get("to"))
            except (TypeError, ValueError):
                continue
            fallback.append(SubtitleSegment(index, start, end, str(segment.get("text") or "")))
        return compose_srt(finalize_segments(fallback, config))

    cues: list[SubtitleSegment] = []
    current: list[tuple[str, int, int, str]] = []
    for word in words:
        if current:
            candidate_text = _cue_plain_text([*current, word])
            duration = word[2] - current[0][1]
            gap = word[1] - current[-1][2]
            previous_text = current[-1][0].rstrip()
            should_break = (
                word[3] != current[-1][3]
                or len(candidate_text) > config.max_event_chars
                or not _fits_layout(candidate_text, config)
                or duration > config.max_duration_ms
                or (gap >= config.phrase_gap_ms and current[-1][2] - current[0][1] >= config.min_duration_ms)
                or (previous_text.endswith(tuple(".!?…")) and len(_join_tokens([item[0] for item in current])) >= 8)
            )
            if should_break:
                cues.append(SubtitleSegment(0, current[0][1], current[-1][2], _cue_text(current, config)))
                current = []
        current.append(word)
    if current:
        cues.append(SubtitleSegment(0, current[0][1], current[-1][2], _cue_text(current, config)))

    return compose_srt(_adjust_durations(cues, config))
