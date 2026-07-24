"""Deterministic SRT utilities for the Pandrator-native dubbing pipeline."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .models import SubtitleSegment

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(
    r"(?P<hours>\d{1,3}):(?P<minutes>\d{2}):(?P<seconds>\d{2})[,.](?P<millis>\d{1,3})"
)
_SPEAKER_RE = re.compile(
    r"^\[(?P<speaker>SPEAKER(?:[\s_-]+)[^\]]+)\]\s*:?\s*(?P<text>.*)",
    re.IGNORECASE | re.DOTALL,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_SENTENCE_ENDERS = {
    ".",
    "!",
    "?",
    "\u3002",
    "\uff01",
    "\uff1f",
    "\u2026",
}


def parse_srt_timestamp(timestamp: str) -> int:
    """Parse an SRT timestamp into milliseconds."""
    match = _TIME_RE.search(str(timestamp or "").strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {timestamp!r}")

    millis_text = match.group("millis").ljust(3, "0")[:3]
    return (
        int(match.group("hours")) * 3_600_000
        + int(match.group("minutes")) * 60_000
        + int(match.group("seconds")) * 1_000
        + int(millis_text)
    )


def format_srt_timestamp(milliseconds: int) -> str:
    """Format milliseconds as an SRT timestamp."""
    total = max(0, int(round(milliseconds)))
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def format_vtt_timestamp(milliseconds: int) -> str:
    """Format milliseconds as a WebVTT timestamp."""

    return format_srt_timestamp(milliseconds).replace(",", ".")


def parse_srt(srt_content: str) -> list[SubtitleSegment]:
    """Parse SRT content into subtitle segments.

    Invalid blocks are skipped, which mirrors the forgiving behavior Pandrator
    needs for user-supplied subtitle files.
    """
    normalized = str(srt_content or "").replace("\ufeff", "")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    segments: list[SubtitleSegment] = []
    for fallback_index, block in enumerate(re.split(r"\n\s*\n+", normalized), start=1):
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        time_line_index = next((idx for idx, line in enumerate(lines) if "-->" in line), -1)
        if time_line_index < 0:
            logger.warning("Skipping SRT block without timestamp line: %s", block)
            continue

        index = fallback_index
        if time_line_index > 0:
            try:
                index = int(lines[time_line_index - 1].strip())
            except ValueError:
                index = fallback_index

        start_text, end_text = lines[time_line_index].split("-->", 1)
        try:
            start_ms = parse_srt_timestamp(start_text)
            end_ms = parse_srt_timestamp(end_text)
        except ValueError as error:
            logger.warning("Skipping SRT block with invalid timing: %s", error)
            continue

        speaker, text = split_speaker_label("\n".join(lines[time_line_index + 1:]).strip())
        if not text:
            continue
        if end_ms <= start_ms:
            end_ms = start_ms + 100

        segments.append(
            SubtitleSegment(
                index=index,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                speaker=speaker or "",
            )
        )

    return segments


def compose_srt(segments: list[SubtitleSegment]) -> str:
    """Compose clean viewer-facing subtitle text without metadata labels."""
    blocks: list[str] = []
    for output_index, segment in enumerate(segments, start=1):
        _speaker, plain_text = split_speaker_label(str(segment.text or "").strip())
        blocks.append(
            "\n".join(
                [
                    str(output_index),
                    f"{format_srt_timestamp(segment.start_ms)} --> {format_srt_timestamp(segment.end_ms)}",
                    plain_text,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def compose_vtt(segments: list[SubtitleSegment]) -> str:
    """Compose subtitle segments as browser-compatible WebVTT."""

    blocks = [
        "\n".join(
            [
                f"{format_vtt_timestamp(segment.start_ms)} --> {format_vtt_timestamp(segment.end_ms)}",
                split_speaker_label(str(segment.text or "").strip())[1],
            ]
        )
        for segment in segments
        if split_speaker_label(str(segment.text or "").strip())[1]
    ]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def srt_to_vtt(srt_content: str) -> str:
    """Convert forgiving SRT input to WebVTT."""

    return compose_vtt(parse_srt(srt_content))


def concatenate_subtitle_text(srt_content: str) -> str:
    """Join cue text into a readable plain-text transcript."""

    cues = [re.sub(r"\s+", " ", segment.text).strip() for segment in parse_srt(srt_content)]
    return " ".join(cue for cue in cues if cue) + ("\n" if cues else "")


def renumber_subtitles(srt_content: str) -> str:
    """Renumber subtitles to consecutive indexes."""
    return compose_srt(parse_srt(srt_content))


def split_speaker_label(text: str) -> tuple[str | None, str]:
    """Extract a legacy bracketed speaker prefix from otherwise plain cue text.

    Speaker labels were historically serialized into SRT text. They are now
    accepted only as an import compatibility format and are never emitted by
    the SRT composers.
    """

    match = _SPEAKER_RE.match(str(text or "").strip())
    if not match:
        return None, str(text or "").strip()
    return match.group("speaker").strip(), match.group("text").strip()


def _speaker_and_text(text: str) -> tuple[str | None, str]:
    """Backward-compatible private alias for older internal callers."""

    return split_speaker_label(text)


def _last_significant_char(text: str) -> str:
    for char in reversed(str(text or "").strip()):
        if char not in " )]\"'":
            return char
    return ""


def _contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def merge_subtitles_with_speaker_awareness(
    srt_content: str,
    merge_threshold: int,
) -> tuple[str, bool]:
    """Merge short adjacent subtitles when timing and speaker labels allow it."""
    segments = parse_srt(srt_content)
    if not segments:
        return srt_content, False

    has_diarization = any(bool(segment.speaker) for segment in segments)
    merged: list[SubtitleSegment] = []

    for segment in segments:
        current_speaker, current_text = segment.speaker or None, segment.text
        if not merged:
            merged.append(segment)
            continue

        previous = merged[-1]
        previous_speaker, previous_text = previous.speaker or None, previous.text
        gap_ms = segment.start_ms - previous.end_ms
        current_limit = 5 if _contains_cjk(current_text) else 30
        can_merge = (
            21 <= gap_ms <= merge_threshold
            and current_speaker == previous_speaker
            and len(current_text.strip()) <= current_limit
            and _last_significant_char(previous_text) not in _SENTENCE_ENDERS
        )

        if not can_merge:
            merged.append(segment)
            continue

        merged_text = f"{previous_text.strip()} {current_text.strip()}".strip()
        merged[-1] = replace(previous, end_ms=segment.end_ms, text=merged_text)

    return compose_srt(merged), has_diarization


def remove_speaker_labels(srt_content: str) -> str:
    """Normalize legacy labelled SRT into clean viewer-facing subtitle text."""

    return compose_srt(parse_srt(srt_content))


def create_translation_blocks(
    srt_content: str,
    char_limit: int,
    source_language: str,
    *,
    max_subtitles_per_block: int | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
) -> list[list[dict[str, Any]]]:
    """Group subtitle segments by character and subtitle-count limits."""
    normalized_language = str(source_language or "").strip().lower()
    if normalized_language in {"chinese", "japanese", "ja", "zh", "zh-cn", "zh-tw"}:
        char_limit = max(1, char_limit // 2)

    if max_subtitles_per_block is not None:
        max_subtitles_per_block = max(1, int(max_subtitles_per_block))

    if normalized_language in {"japanese", "ja"}:
        endings = ("\u3002", "\uff01", "\uff1f", "\u304b", "\u306d", "\u3088", "\u308f")
    elif normalized_language in {"chinese", "zh", "zh-cn", "zh-tw"}:
        endings = ("\u3002", "\uff01", "\uff1f", "\u2026")
    else:
        endings = (".", "!", "?")

    def is_sentence_ending(text: str) -> bool:
        return any(str(text or "").strip().endswith(ending) for ending in endings)

    blocks: list[list[dict[str, Any]]] = []
    current_block: list[dict[str, Any]] = []
    current_char_count = 0

    for segment in parse_srt(srt_content):
        segment_text = segment.text
        segment_speaker = str(
            (speaker_by_subtitle or {}).get(segment.index)
            or segment.speaker
            or ""
        ).strip()
        if (
            current_block
            and max_subtitles_per_block is not None
            and len(current_block) >= max_subtitles_per_block
        ):
            blocks.append(current_block)
            current_block = []
            current_char_count = 0

        if current_block and current_char_count + len(segment_text) > char_limit:
            if is_sentence_ending(current_block[-1]["text"]):
                blocks.append(current_block)
                current_block = []
                current_char_count = 0
            else:
                split_index = next(
                    (
                        idx
                        for idx in range(len(current_block) - 1, -1, -1)
                        if is_sentence_ending(current_block[idx]["text"])
                    ),
                    -1,
                )
                if split_index >= 0:
                    blocks.append(current_block[: split_index + 1])
                    current_block = current_block[split_index + 1:]
                    current_char_count = sum(len(item["text"]) for item in current_block)
                else:
                    blocks.append(current_block)
                    current_block = []
                    current_char_count = 0

        current_block.append(
            {
                "index": segment.index,
                "text": segment_text,
                "start": segment.start_ms / 1000,
                "end": segment.end_ms / 1000,
                "speaker": segment_speaker,
            }
        )
        current_char_count += len(segment_text)

    if current_block:
        blocks.append(current_block)

    return blocks
