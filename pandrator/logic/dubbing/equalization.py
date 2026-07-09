"""Native subtitle equalization helpers."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from dataclasses import replace

from .srt_utils import compose_srt, parse_srt

_WHITESPACE_RE = re.compile(r"[ \t]+")


def _normalize_subtitle_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    normalized = re.sub(r"\s*\n+\s*", " ", normalized)
    return normalized.strip()


def _split_long_line(line: str, max_line_length: int) -> list[str]:
    words = line.split()
    if not words:
        return []

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_line_length:
            current = candidate
            continue
        lines.append(current)
        current = word

    if current:
        lines.append(current)
    return lines


def equalize_subtitle_text(text: str, max_line_length: int = 42, max_lines: int = 2) -> str:
    """Normalize subtitle text into stable line lengths."""
    normalized = _normalize_subtitle_text(text)
    if not normalized:
        return ""

    max_line_length = max(8, int(max_line_length or 42))
    max_lines = max(1, int(max_lines or 2))
    if len(normalized) <= max_line_length:
        return normalized

    candidate_lines: list[str] = []
    for sentence_part in re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s+", normalized):
        sentence_part = sentence_part.strip()
        if not sentence_part:
            continue
        candidate_lines.extend(_split_long_line(sentence_part, max_line_length))

    if len(candidate_lines) <= max_lines:
        return "\n".join(candidate_lines)

    packed: list[str] = []
    current = ""
    for line in candidate_lines:
        if not current:
            current = line
            continue
        candidate = f"{current} {line}"
        remaining_slots = max_lines - len(packed) - 1
        if len(candidate) <= max_line_length or remaining_slots <= 0:
            current = candidate
        else:
            packed.append(current)
            current = line
    if current:
        packed.append(current)

    if len(packed) <= max_lines:
        return "\n".join(packed)

    first_lines = packed[: max_lines - 1]
    final_line = " ".join(packed[max_lines - 1:])
    return "\n".join([*first_lines, final_line])


def equalize_srt_content(srt_content: str, max_line_length: int = 42, max_lines: int = 2) -> str:
    """Equalize subtitle text while preserving timings."""
    equalized_segments = [
        replace(
            segment,
            text=equalize_subtitle_text(
                segment.text,
                max_line_length=max_line_length,
                max_lines=max_lines,
            ),
        )
        for segment in parse_srt(srt_content)
    ]
    return compose_srt(equalized_segments)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def equalize_srt_file(
    srt_file: str | os.PathLike[str],
    output_file: str | os.PathLike[str] | None = None,
    max_line_length: int = 42,
    max_lines: int = 2,
) -> str:
    """Write an equalized SRT file and return its path."""
    input_path = Path(srt_file)
    output_path = Path(output_file) if output_file else input_path.with_name(f"{input_path.stem}_equalized.srt")
    with input_path.open("r", encoding="utf-8-sig") as handle:
        source = handle.read()
    equalized = equalize_srt_content(source, max_line_length=max_line_length, max_lines=max_lines)
    _write_text_atomic(output_path, equalized)
    return str(output_path)
