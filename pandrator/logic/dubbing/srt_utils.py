"""Deterministic SRT utilities for the Pandrator-native dubbing pipeline."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal, cast

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

TimingContextMode = Literal["full", "overlap_only", "none"]
TIMING_CONTEXT_MODES: tuple[TimingContextMode, ...] = (
    "full",
    "overlap_only",
    "none",
)


def normalize_timing_context_mode(
    value: object = None,
    *,
    legacy_enabled: object = None,
    default: TimingContextMode = "full",
) -> TimingContextMode:
    """Resolve the canonical timing-disclosure mode.

    ``timing_context_enabled`` used to be a boolean whose false branch still
    leaked overlap timing.  Treating a legacy false value as ``none`` makes the
    old setting finally mean what it says.  ``overlap_only`` is now an explicit
    choice instead of a hidden exception.
    """

    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in TIMING_CONTEXT_MODES:
        return cast(TimingContextMode, normalized)
    if legacy_enabled is not None and legacy_enabled != "":
        if isinstance(legacy_enabled, str):
            enabled = legacy_enabled.strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
        else:
            enabled = bool(legacy_enabled)
        return "full" if enabled else "none"
    return default


def timing_context_mode_from_settings(
    settings: Mapping[str, Any],
    *,
    default: TimingContextMode = "full",
) -> TimingContextMode:
    """Read canonical timing settings with compatibility for saved sessions."""

    return normalize_timing_context_mode(
        settings.get("timing_context_mode"),
        legacy_enabled=settings.get("timing_context_enabled"),
        default=default,
    )


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
    total = max(0, milliseconds)
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


def subtitle_prompt_context(
    subtitle: Mapping[str, Any],
    *,
    timing_context_mode: TimingContextMode | str | None = None,
    include_timing: bool | None = None,
) -> dict[str, Any]:
    """Return compact, non-spoken cue evidence for an LLM task.

    Timing is nested under one key so callers cannot accidentally disclose the
    same values in several peer fields.  ``none`` excludes all timing,
    ``overlap_only`` discloses only positive overlap, and ``full`` discloses the
    absolute interval plus the relationship to the preceding cue.
    """

    context: dict[str, Any] = {}
    speaker = str(subtitle.get("speaker") or "").strip()
    if speaker:
        context["speaker"] = speaker
    mode = normalize_timing_context_mode(
        timing_context_mode,
        legacy_enabled=include_timing,
        default="none",
    )
    timing: dict[str, int] = {}
    overlap_ms = max(0, int(subtitle.get("overlap_with_previous_ms") or 0))
    if mode == "full":
        timing["start_ms"] = int(subtitle.get("start_ms") or 0)
        timing["end_ms"] = int(subtitle.get("end_ms") or 0)
        if overlap_ms:
            timing["overlap_with_previous_ms"] = overlap_ms
        elif "gap_from_previous_ms" in subtitle:
            timing["gap_from_previous_ms"] = max(
                0,
                int(subtitle.get("gap_from_previous_ms") or 0),
            )
    elif overlap_ms and mode == "overlap_only":
        timing["overlap_with_previous_ms"] = overlap_ms
    if timing:
        context["timing"] = timing
    return context


def subtitle_task_cue(
    subtitle: Mapping[str, Any],
    *,
    timing_context_mode: TimingContextMode | str,
) -> dict[str, Any]:
    """Build the canonical model-visible cue used by native and passive work."""

    cue_id = int(subtitle["index"])
    return {
        "cue_id": cue_id,
        "text": re.sub(r"\s+", " ", str(subtitle.get("text") or "")).strip(),
        **subtitle_prompt_context(
            subtitle,
            timing_context_mode=timing_context_mode,
        ),
    }


def subtitle_boundary_cue(subtitle: Mapping[str, Any]) -> dict[str, str] | None:
    """Build non-actionable continuity evidence without identity or timing."""

    text = re.sub(r"\s+", " ", str(subtitle.get("text") or "")).strip()
    if not text:
        return None
    cue = {"text": text}
    speaker = str(subtitle.get("speaker") or "").strip()
    if speaker:
        cue["speaker"] = speaker
    return cue


def create_translation_blocks(
    srt_content: str,
    char_limit: int,
    source_language: str,
    *,
    max_subtitles_per_block: int | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
) -> list[list[dict[str, Any]]]:
    """Group subtitle segments without cutting avoidable semantic boundaries.

    Timing and speaker metadata are included as non-text evidence for LLM
    correction/translation.  In particular, an overlap marker lets the model
    distinguish ordinary consecutive cues from simultaneous speech or an ASR
    chunk-boundary duplicate.
    """
    normalized_language = str(source_language or "").strip().lower()
    if normalized_language in {"chinese", "japanese", "ja", "zh", "zh-cn", "zh-tw"}:
        char_limit = max(1, char_limit // 2)

    if max_subtitles_per_block is not None:
        max_subtitles_per_block = max(1, int(max_subtitles_per_block))

    endings: tuple[str, ...]
    if normalized_language in {"japanese", "ja"}:
        endings = ("\u3002", "\uff01", "\uff1f", "\u304b", "\u306d", "\u3088", "\u308f")
    elif normalized_language in {"chinese", "zh", "zh-cn", "zh-tw"}:
        endings = ("\u3002", "\uff01", "\uff1f", "\u2026")
    else:
        endings = (".", "!", "?")

    def is_sentence_ending(text: str) -> bool:
        return any(str(text or "").strip().endswith(ending) for ending in endings)

    records: list[dict[str, Any]] = []
    previous_segment: SubtitleSegment | None = None
    for segment in parse_srt(srt_content):
        gap_ms = (
            max(0, segment.start_ms - previous_segment.end_ms)
            if previous_segment is not None
            else 0
        )
        overlap_ms = (
            max(0, previous_segment.end_ms - segment.start_ms)
            if previous_segment is not None
            else 0
        )
        record = {
            "index": segment.index,
            "text": segment.text,
            "start": segment.start_ms / 1000,
            "end": segment.end_ms / 1000,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "speaker": str(
                (speaker_by_subtitle or {}).get(segment.index)
                or segment.speaker
                or ""
            ).strip(),
        }
        if overlap_ms:
            record["overlap_with_previous_ms"] = overlap_ms
        elif previous_segment is not None:
            record["gap_from_previous_ms"] = gap_ms
        records.append(record)
        previous_segment = segment

    def safe_boundary(left: dict[str, Any], right: dict[str, Any]) -> bool:
        if int(right.get("overlap_with_previous_ms") or 0) > 0:
            return False
        return bool(
            is_sentence_ending(str(left.get("text") or ""))
            or (
                str(left.get("speaker") or "")
                and str(right.get("speaker") or "")
                and str(left.get("speaker")).casefold()
                != str(right.get("speaker")).casefold()
            )
        )

    def preferred_split(
        current: list[dict[str, Any]],
        following: dict[str, Any],
    ) -> int:
        candidates = [
            index
            for index in range(1, len(current) + 1)
            if safe_boundary(
                current[index - 1],
                current[index] if index < len(current) else following,
            )
        ]
        if not candidates:
            return len(current)
        # Keep a short semantic tail with the next request rather than forcing
        # a hard batch boundary through it.  Prefer the latest safe cut that
        # still leaves at most half of the current batch behind.
        minimum = max(1, len(current) // 2)
        return next(
            (candidate for candidate in reversed(candidates) if candidate >= minimum),
            candidates[-1],
        )

    blocks: list[list[dict[str, Any]]] = []
    current_block: list[dict[str, Any]] = []
    current_char_count = 0

    for record in records:
        while current_block and (
            (
                max_subtitles_per_block is not None
                and len(current_block) >= max_subtitles_per_block
            )
            or current_char_count + len(str(record["text"])) > char_limit
        ):
            split_at = preferred_split(current_block, record)
            blocks.append(current_block[:split_at])
            current_block = current_block[split_at:]
            current_char_count = sum(
                len(str(item.get("text") or "")) for item in current_block
            )

        current_block.append(record)
        current_char_count += len(str(record["text"]))

    if current_block:
        blocks.append(current_block)

    return blocks
