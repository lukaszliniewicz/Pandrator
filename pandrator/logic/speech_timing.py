"""Validate native character alignment without depending on web contracts."""

from __future__ import annotations

import math
import unicodedata
from typing import Any

import regex


def _projection(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize graphemes while retaining a verified original character map."""
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in regex.finditer(r"\X", text):
        normalized = unicodedata.normalize("NFKC", match.group()).casefold()
        for char in normalized:
            if char.isspace():
                if not chars:
                    continue
                if chars[-1] == " ":
                    spans[-1] = (spans[-1][0], match.end())
                    continue
                char = " "
            chars.append(char)
            spans.append(match.span())
    if chars and chars[-1] == " ":
        chars.pop()
        spans.pop()
    return "".join(chars), spans


def _lexical_spans(text: str):
    """CJK graphemes are units; other letters form words with internal apostrophes."""
    start: int | None = None
    end = 0
    for match in regex.finditer(r"\X", text):
        grapheme = match.group()
        cjk = bool(regex.search(r"[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]", grapheme))
        lexical = any(char.isalnum() for char in grapheme)
        apostrophe = (
            grapheme in {"'", "’"}
            and start is not None
            and bool(text[match.end() : match.end() + 1].isalnum())
        )
        if cjk or not (lexical or apostrophe):
            if start is not None:
                yield start, end
                start = None
            if cjk:
                yield match.span()
        else:
            if start is None:
                start = match.start()
            end = match.end()
    if start is not None:
        yield start, end


def elevenlabs_speech_timing(
    text: str,
    alignment: object,
    *,
    duration_ms: float,
) -> dict[str, Any] | None:
    """Return complete canonical lexical timing, or reject metadata as a whole.

    Audio success is independent of timing validity. No missing timestamp is
    interpolated and only NFKC, case and whitespace differences are accepted.
    """
    if not isinstance(alignment, dict):
        return None
    characters = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if (
        not isinstance(characters, list)
        or not isinstance(starts, list)
        or not isinstance(ends, list)
    ):
        return None
    if not characters or len(characters) != len(starts) or len(starts) != len(ends):
        return None
    if not all(isinstance(char, str) and char for char in characters):
        return None
    try:
        if any(isinstance(value, bool) for value in [*starts, *ends]):
            return None
        starts_ms = [float(value) * 1000 for value in starts]
        ends_ms = [float(value) * 1000 for value in ends]
        if not math.isfinite(duration_ms) or duration_ms <= 0:
            return None
        for index, (start, end) in enumerate(zip(starts_ms, ends_ms, strict=True)):
            if not math.isfinite(start) or not math.isfinite(end):
                return None
            if not 0 <= start <= end <= duration_ms:
                return None
            if index and (start < starts_ms[index - 1] or end < ends_ms[index - 1]):
                return None
    except (TypeError, ValueError, OverflowError):
        return None
    provider_text = "".join(characters)
    projected_text, text_map = _projection(text)
    projected_provider, provider_map = _projection(provider_text)
    if not projected_text or projected_text != projected_provider:
        return None
    # Expand array entries to original provider character offsets, including
    # multi-codepoint graphemes, without manufacturing timing values.
    provider_indices = [index for index, char in enumerate(characters) for _ in char]
    words: list[dict[str, Any]] = []
    lexical_spans = list(_lexical_spans(text))
    for word_index, (start_char, end_char) in enumerate(lexical_spans):
        indices = {
            provider_indices[offset]
            for text_span, provider_span in zip(text_map, provider_map, strict=True)
            if text_span[0] < end_char and text_span[1] > start_char
            for offset in range(*provider_span)
        }
        if not indices:
            return None
        start_ms = round(min(starts_ms[index] for index in indices))
        end_ms = round(max(ends_ms[index] for index in indices))
        if end_ms <= start_ms or (words and start_ms < words[-1]["end_ms"]):
            return None
        # Restore the complete original surface while retaining only validated
        # lexical timestamps. Trim whole whitespace graphemes so punctuation,
        # combining sequences and quotes cannot be split or lost.
        surface_start = start_char if word_index else 0
        surface_end = (
            lexical_spans[word_index + 1][0] if word_index + 1 < len(lexical_spans) else len(text)
        )
        graphemes = list(regex.finditer(r"\X", text[surface_start:surface_end]))
        first = 0
        last = len(graphemes)
        while first < last and graphemes[first].group().isspace():
            first += 1
        while last > first and graphemes[last - 1].group().isspace():
            last -= 1
        start_char = surface_start + graphemes[first].start()
        end_char = surface_start + graphemes[last - 1].end()
        words.append(
            {
                "text": text[start_char:end_char],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_char": start_char,
                "end_char": end_char,
            }
        )
    if not words:
        return None
    return {
        "version": 1,
        "text": text,
        "words": words,
        "engine": "elevenlabs_native",
        "diagnostics": {
            "complete": True,
            "character_count": len(provider_text),
            "normalized_match": provider_text != text,
        },
    }
