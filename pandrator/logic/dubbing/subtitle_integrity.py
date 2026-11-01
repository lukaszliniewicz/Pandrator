"""Independent subtitle body preservation checks, separate from layout code."""

from __future__ import annotations

import hashlib
import re

import regex

from .srt_utils import parse_srt_timestamp

_UNSPACED_SCRIPT = regex.compile(r"[\p{Han}\p{Hiragana}\p{Katakana}]")
_CJK_PUNCTUATION = frozenset("、。，．！？：；（）［］｛｝「」『』【】〈〉《》〔〕")
_OPENING = frozenset("([{（［｛「『【〈《〔£€$„‘")
_CLOSING = frozenset(",.!?:;%)]}…、。，．！？：；％）］｝」』】〉》〕”’")
_ASCII_SPACE = " \t\r\n\f\v"
_EXPLICIT_SPEAKER = re.compile(
    r"^\[SPEAKER(?:[\s_-]+)[^\]]+\][ \t\r\n]*:?[ \t\r\n]*(?P<body>.*)",
    re.IGNORECASE | re.DOTALL,
)


def _subtitle_body(content: str) -> tuple[str, bool]:
    """Read body text without the forgiving parser's Unicode whitespace loss.

    This is an export-preservation guard, not a stricter import API. A body
    whose timestamp cannot be read is retained for hashing and invalidates
    the comparison rather than being silently skipped.
    """
    normalized = content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    bodies: list[str] = []
    valid = True
    for block in re.split(r"\n[ \t]*\n+", normalized):
        block = block.strip(_ASCII_SPACE)
        if not block:
            continue
        lines = block.split("\n")
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            valid = False
            bodies.append(block)
            continue
        body = "\n".join(lines[timing_index + 1:]).strip(_ASCII_SPACE)
        if not body:
            continue
        start, end = lines[timing_index].split("-->", 1)
        try:
            parse_srt_timestamp(start)
            parse_srt_timestamp(end)
        except ValueError:
            valid = False
        explicit_speaker = _EXPLICIT_SPEAKER.match(body)
        if explicit_speaker:
            body = explicit_speaker.group("body").strip(_ASCII_SPACE)
        if body:
            bodies.append(body)
    return "\n".join(bodies), valid


def _body_signature(text: str) -> str:
    # Deliberately do not call clean_text, join_fragments, fragment_separator,
    # or any finalizer: a regression there must not redefine the verifier.
    opening_quotes: set[int] = set()
    closing_quotes: set[int] = set()
    ascii_open = False
    german_open = False
    for index, char in enumerate(text):
        if char == '"':
            (closing_quotes if ascii_open else opening_quotes).add(index)
            ascii_open = not ascii_open
        elif char == "„":
            german_open = True
        elif char == "“":
            (closing_quotes if german_open else opening_quotes).add(index)
            german_open = False

    output: list[str] = []
    cursor = 0
    for space in re.finditer(r"[ \t\r\n\f\v]+", text):
        start, end = space.span()
        output.append(text[cursor:start])
        cursor = end
        if start == 0 or end == len(text):
            continue
        left, right = text[start - 1], text[end]
        if (
            _UNSPACED_SCRIPT.search(left + right)
            or left in _CJK_PUNCTUATION
            or right in _CJK_PUNCTUATION
            or left in _OPENING
            or right in _CLOSING
            or start - 1 in opening_quotes
            or end in closing_quotes
        ):
            continue
        output.append(" ")
    output.append(text[cursor:])
    return "".join(output)


def subtitle_text_integrity(source: str, finalized: str) -> dict[str, bool | str]:
    """Compare body code points and necessary boundaries, ignoring layout.

    Explicit legacy speaker metadata is omitted by the parser, while ordinary
    semantic prefixes remain text. ASCII Han/kana spacing is optional; all
    non-ASCII body spacing, Latin/Hangul boundaries and punctuation remain.
    """
    source_body, source_valid = _subtitle_body(source)
    final_body, final_valid = _subtitle_body(finalized)
    source_text, final_text = _body_signature(source_body), _body_signature(final_body)
    return {
        "matches": source_valid and final_valid and source_text == final_text,
        "source_structure_valid": source_valid,
        "final_structure_valid": final_valid,
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "final_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
        "version": "subtitle-body-v1",
        "units": "Unicode code points and required word/quote spaces",
        "normalization": "ASCII presentation whitespace and Han/kana layout; explicit speaker metadata excluded",
    }
