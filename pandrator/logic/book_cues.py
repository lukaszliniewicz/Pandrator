"""Compose reading passages from timed speech, independently of synthesis chunks."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from .book_settings import BOOK_DEFAULTS, validate_book_settings
from .book_timing import BookWord
from .dubbing.subtitle_finalization import SubtitleFinalizationConfig, wrap_subtitle_text
from .dubbing.text_units import fragment_separator, subtitle_units


@dataclass(frozen=True, slots=True)
class BookPassage:
    text: str
    words: tuple[BookWord, ...]
    start_ms: int
    end_ms: int
    heading: str = ""


@dataclass(frozen=True, slots=True)
class BookCue:
    start_ms: int
    end_ms: int
    text: str
    lines: tuple[str, ...] = ()
    heading: str = ""


def _reading_lines(text: str, maximum: int, line_units: int = 66) -> tuple[str, ...] | None:
    """Font-free subtitle layout. Video uses measured font advances instead."""
    from .dubbing.text_units import display_length

    lines = [""]
    for unit in subtitle_units(text):
        candidate = lines[-1] + unit
        if display_length(candidate.strip()) > line_units:
            if not lines[-1].strip():
                return None
            lines.append(unit.lstrip())
        else:
            lines[-1] = candidate
        if len(lines) > maximum:
            return None
    return tuple(line.strip() for line in lines if line.strip()) or None


def compose_book_cues(
    passages: Sequence[BookPassage],
    settings: dict,
    *,
    total_duration_ms: int,
    fit_lines: Callable[[str], tuple[str, ...] | None] | None = None,
) -> list[BookCue]:
    """Retain complete text and real boundaries; layout never stretches the audio."""
    validate_book_settings(settings)
    options = {**BOOK_DEFAULTS, **settings}
    captions = options["book_style"] == "captions"
    config = SubtitleFinalizationConfig.from_settings(
        settings,
        language=str(settings.get("language") or ""),
        text="".join(p.text for p in passages),
    )

    def fit(text: str) -> tuple[str, ...] | None:
        if captions:
            lines = tuple(wrap_subtitle_text(text, config).splitlines())
            if len(lines) > config.max_lines or any(
                config.character_count(line) > config.max_chars_per_line for line in lines
            ):
                return None
            if fit_lines is not None:
                # Keep conventional caption line breaks when checking pixel capacity.
                checked = fit_lines("\n".join(lines))
                if checked is None or len(checked) != len(lines):
                    return None
            return lines
        return (
            fit_lines(text)
            if fit_lines is not None
            else _reading_lines(text, options["book_max_lines"])
        )

    target = (
        min(4000, config.max_duration_ms) if captions else options["book_target_seconds"] * 1000
    )
    ceiling = config.max_duration_ms if captions else options["book_max_seconds"] * 1000
    result: list[BookCue] = []
    previous_end = 0
    for passage in passages:
        if (
            not passage.words
            or not 0 <= passage.start_ms < passage.end_ms <= total_duration_ms
            or passage.start_ms < previous_end
        ):
            raise ValueError("Book passages must follow the exact audio timeline.")
        previous_end = passage.end_ms
        passage_start = len(result)
        cursor = 0
        while cursor < len(passage.words):
            first = passage.words[cursor]
            start = passage.start_ms if cursor == 0 else first.start_ms
            if options["book_cue_mode"] == "segments":
                end_index = len(passage.words)
                text = passage.text.strip()
                lines = fit(text) if fit_lines is not None else (text,)
            else:
                end_index = cursor
                lines = None
                text = ""
                preferred = None
                for index in range(cursor, len(passage.words)):
                    last = passage.words[index]
                    candidate = passage.text[first.start_char : last.end_char].strip()
                    candidate_lines = fit(candidate)
                    if candidate_lines is None or (
                        index > cursor and last.end_ms - start > ceiling
                    ):
                        # Keep the last sentence/clause together when the reading
                        # area fills before the target duration is reached.
                        if not captions and preferred is not None:
                            end_index, text, lines = preferred
                        break
                    end_index, text, lines = index + 1, candidate, candidate_lines
                    natural = bool(re.search(r"[.!?。！？;；:：,，][\"'’”）)\]]*$", candidate))
                    paragraph = (
                        index + 1 < len(passage.words)
                        and "\n"
                        in passage.text[last.end_char : passage.words[index + 1].start_char]
                    )
                    if natural or paragraph:
                        preferred = (end_index, text, lines)
                    if last.end_ms - start >= target:
                        if (
                            preferred is not None
                            and passage.words[preferred[0] - 1].end_ms - start >= target * 0.6
                        ):
                            end_index, text, lines = preferred
                        break
            if end_index <= cursor or lines is None:
                raise ValueError(
                    "A timed text unit does not fit. Reduce the text size, increase the line limit, or use smaller passages."
                )
            end = (
                passage.end_ms
                if end_index == len(passage.words)
                else passage.words[end_index].start_ms
            )
            if end <= start:
                raise ValueError("Book cue timestamps must have positive duration.")
            cue = BookCue(start, end, text, lines, passage.heading)
            # Preserve passage/paragraph boundaries when merging short fragments.
            if (
                options["book_cue_mode"] != "segments"
                and len(result) > passage_start
                and result[-1].end_ms - result[-1].start_ms < 1500
            ):
                prior = result[-1]
                joined = prior.text + fragment_separator(prior.text, text) + text
                joined_lines = fit(joined)
                if (
                    prior.heading == cue.heading
                    and cue.end_ms - prior.start_ms <= ceiling
                    and joined_lines is not None
                ):
                    cue = replace(prior, end_ms=cue.end_ms, text=joined, lines=joined_lines)
                    result.pop()
            result.append(cue)
            cursor = end_index
    # Reading passages remain visible during intentional pauses, without overlap.
    if not captions:
        result = [
            replace(
                cue,
                end_ms=result[index + 1].start_ms if index + 1 < len(result) else total_duration_ms,
            )
            for index, cue in enumerate(result)
        ]
    return result


def clip_book_cues(cues: Sequence[BookCue], start_ms: int, end_ms: int) -> list[BookCue]:
    if not 0 <= start_ms < end_ms:
        raise ValueError("The preview interval must have positive duration.")
    return [
        replace(
            cue,
            start_ms=max(cue.start_ms, start_ms) - start_ms,
            end_ms=min(cue.end_ms, end_ms) - start_ms,
        )
        for cue in cues
        if cue.start_ms < end_ms and cue.end_ms > start_ms
    ]


def cues_as_dict(cues: Sequence[BookCue]) -> list[dict]:
    return [{**asdict(cue), "lines": list(cue.lines)} for cue in cues]


def _timestamp(value: int, separator: str) -> str:
    seconds, ms = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{ms:03}"


def write_book_subtitles(cues: Sequence[BookCue], destination: Path, format: str) -> None:
    if format not in {"srt", "vtt"}:
        raise ValueError("Book subtitles must use SRT or WebVTT.")
    separator = "," if format == "srt" else "."
    blocks = []
    for index, cue in enumerate(cues, 1):
        text = "\n".join(cue.lines) if cue.lines else cue.text
        # Blank lines terminate a cue, including whitespace-only source lines.
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
        text = "\n".join(line for line in text.split("\n") if line.strip())
        if format == "vtt":
            from html import escape

            text = escape(text, quote=False)
        blocks.append(
            f"{index}\n{_timestamp(cue.start_ms, separator)} --> {_timestamp(cue.end_ms, separator)}\n{text}"
        )
    destination.write_text(
        ("WEBVTT\n\n" if format == "vtt" else "") + "\n\n".join(blocks) + "\n", encoding="utf-8"
    )
