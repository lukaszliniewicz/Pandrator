"""Local word boundaries for Japanese/Chinese subtitle display only.

Offsets always address the original string. Dictionary normalization/readings
must never replace reviewed text, and acoustic word anchors are not modified.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from threading import RLock
from typing import Any

from .text_units import display_length, subtitle_units

logger = logging.getLogger(__name__)
_TOKENIZER_LOCK = RLock()


@lru_cache(maxsize=2)
def _tokenizer(language: str) -> Any:
    try:
        if language == "ja":
            from sudachipy import dictionary

            return dictionary.Dictionary(dict="core").create()
        if language == "zh":
            import jieba
            from opencc import OpenCC

            parser = jieba.Tokenizer()
            parser.initialize()
            return parser, OpenCC("t2s")
    except Exception as exc:  # noqa: BLE001 - retain legacy layout on unavailable local dictionaries
        logger.warning("%s subtitle word segmentation unavailable: %s", language, exc)
    return None


@lru_cache(maxsize=4096)
def word_boundaries(text: str, language: str, *, fine: bool = False) -> frozenset[int] | None:
    """Return preferred word ends, or None when the local tokenizer is unavailable."""
    if language not in {"ja", "zh"}:
        return None
    with _TOKENIZER_LOCK:
        parser = _tokenizer(language)
        if parser is None:
            return None
        try:
            if language == "ja":
                from sudachipy import tokenizer

                mode = tokenizer.Tokenizer.SplitMode.A if fine else tokenizer.Tokenizer.SplitMode.C
                ends: set[int] = set()
                for token in parser.tokenize(text, mode):
                    # Keep particles, auxiliaries and suffixes with their host
                    # in the normal layout. Fine fallback exposes morphemes.
                    pos = token.part_of_speech()[0]
                    if not fine and pos in {"助詞", "助動詞", "接尾辞"}:
                        ends.discard(token.begin())
                    if fine or pos != "接頭辞":
                        ends.add(token.end())
            else:
                segmenter, converter = parser
                # Jieba's bundled dictionary is primarily Simplified Chinese.
                # Convert for analysis only; returned offsets still address the
                # untouched Traditional/Simplified original, never the lexicon.
                # Convert characters independently to preserve an exact map,
                # including rare expansions/contractions. Equal total lengths
                # alone do not prove that internal offsets are unchanged.
                converted = {char: converter.convert(char) for char in set(text)}
                pieces = [converted[char] for char in text]
                analysed = "".join(pieces)
                offsets = {0: 0}
                cursor = 0
                for index, piece in enumerate(pieces, start=1):
                    cursor += len(piece)
                    offsets[cursor] = index
                ends = {offsets[end] for _word, _start, end in segmenter.tokenize(analysed) if end in offsets}
            return frozenset({*ends, len(text)})
        except Exception as exc:  # noqa: BLE001 - never lose text on tokenizer failure
            logger.warning("%s subtitle word segmentation failed: %s", language, exc)
            return None


def _group_units(units: list[str], boundaries: frozenset[int]) -> list[str]:
    """Intersect word ends with existing grapheme/kinsoku-safe boundaries."""
    grouped: list[str] = []
    pending = ""
    offset = 0
    for unit in units:
        pending += unit
        offset += len(unit)
        if offset in boundaries:
            grouped.append(pending)
            pending = ""
    if pending:
        grouped.append(pending)
    return grouped


def _fragment_units(text: str) -> list[str]:
    """Retain spaces at fragment edges that subtitle_units normally trims."""
    stripped = text.strip(" \t\r\n")
    if not stripped:
        return [text]
    units = subtitle_units(stripped)
    leading = len(text) - len(text.lstrip(" \t\r\n"))
    trailing = len(text.rstrip(" \t\r\n"))
    units[0] = text[:leading] + units[0]
    units[-1] += text[trailing:]
    return units


def layout_units(text: str, language: str, max_width: int) -> list[str]:
    """Keep words intact when they fit; refine only an overlong word.

The fallback uses the existing Unicode units, so combining marks, punctuation
guards and Latin words retain their established protection. Original whitespace
is kept in these units; only the existing display formatter trims line edges.
"""
    units = subtitle_units(text)
    boundaries = word_boundaries(text, language)
    if boundaries is None:
        return units
    output: list[str] = []
    for word in _group_units(units, boundaries):
        if display_length(word.strip(" \t\r\n"), cjk=True) <= max_width:
            output.append(word)
            continue
        smaller_units = _fragment_units(word)
        fine = word_boundaries(word, language, fine=True)
        for part in _group_units(smaller_units, fine) if fine else smaller_units:
            if display_length(part.strip(" \t\r\n"), cjk=True) <= max_width:
                output.append(part)
            else:
                output.extend(_fragment_units(part))
    return output
