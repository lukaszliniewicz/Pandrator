"""Literal matching helpers for the generation-plan search endpoint."""

from __future__ import annotations

import unicodedata


def _is_word_character(value: str) -> bool:
    """Return whether *value* participates in a whole-word boundary."""

    if value == "_":
        return True
    return bool(value) and unicodedata.category(value)[0] in {"L", "M", "N"}


def _word_boundaries(text: str, start: int, end: int, query: str, whole_word: bool) -> bool:
    if not whole_word:
        return True
    starts_with_word = _is_word_character(query[0])
    ends_with_word = _is_word_character(query[-1])
    return (
        (not starts_with_word or start == 0 or not _is_word_character(text[start - 1]))
        and (not ends_with_word or end == len(text) or not _is_word_character(text[end]))
    )


def _utf16_offsets(text: str) -> list[int]:
    offsets = [0]
    for value in text:
        offsets.append(offsets[-1] + (2 if ord(value) > 0xFFFF else 1))
    return offsets


def literal_matches(
    text: str,
    query: str,
    *,
    match_case: bool = False,
    whole_word: bool = False,
) -> list[dict[str, int]]:
    """Return non-overlapping UTF-16 spans for literal query matches.

    Case folding can expand one source character into multiple code points
    (for example, ``ß`` becomes ``ss``). Matches are accepted only when both
    folded endpoints align with original-character boundaries, so a query
    cannot select half of an expanded source character.
    """

    if not query or not text:
        return []
    if match_case:
        haystack = text
        needle = query
        folded_boundaries = list(range(len(text) + 1))
    else:
        folded_parts: list[str] = []
        folded_boundaries = [0]
        for value in text:
            folded = value.casefold()
            folded_parts.append(folded)
            folded_boundaries.append(folded_boundaries[-1] + len(folded))
        haystack = "".join(folded_parts)
        needle = query.casefold()
    if not needle:
        return []
    original_boundaries = {
        position: original
        for original, position in enumerate(folded_boundaries)
    }
    utf16_offsets = _utf16_offsets(text)
    matches: list[dict[str, int]] = []
    cursor = haystack.find(needle)
    while cursor >= 0:
        folded_end = cursor + len(needle)
        start = original_boundaries.get(cursor)
        end = original_boundaries.get(folded_end)
        if (
            start is not None
            and end is not None
            and _word_boundaries(text, start, end, query, whole_word)
        ):
            matches.append({"start": utf16_offsets[start], "end": utf16_offsets[end]})
            cursor = haystack.find(needle, folded_end)
        else:
            # A rejected partial expansion must not skip a valid match that
            # starts within the rejected folded span (e.g. "ss" in "sß").
            cursor = haystack.find(needle, cursor + 1)
    return matches


def contains_literal(
    text: str,
    query: str,
    *,
    match_case: bool = False,
    whole_word: bool = False,
) -> bool:
    """Match a literal query with Unicode-aware case and word boundaries.

    Case-insensitive matching uses Unicode ``casefold``. A whole-word query
    requires a boundary only at an end whose query character is a word
    character, so punctuation-only queries remain ordinary literal searches.
    """

    return bool(
        not query
        or literal_matches(
            text,
            query,
            match_case=match_case,
            whole_word=whole_word,
        )
    )
