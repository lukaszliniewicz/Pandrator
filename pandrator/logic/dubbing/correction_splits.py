"""Verified source-word boundaries for semantic correction, never estimated timing."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_LEXEME_CHARS = re.compile(r"[^\W_]+", re.UNICODE)
_CJK_RANGES = ((0x3040, 0x30FF), (0x3400, 0x9FFF), (0xAC00, 0xD7AF))
_CJK_CONTEXT_CHARACTERS = 40


def _lexeme(text: str) -> str:
    return "".join(re.findall(r"[^\W_]+", text, re.UNICODE)).casefold()


def _has_supported_cjk(text: str) -> bool:
    return any(
        start <= ord(character) <= end
        for character in text
        for start, end in _CJK_RANGES
    )


def _lexeme_with_source_offsets(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    source_offsets: list[int] = []
    for match in _LEXEME_CHARS.finditer(text):
        for offset in range(match.start(), match.end()):
            folded = text[offset].casefold()
            normalized.append(folded)
            source_offsets.extend([offset] * len(folded))
    return "".join(normalized), source_offsets


def _cjk_split_boundaries(
    passage: dict[str, Any],
    text: str,
    words_by_id: dict[str, dict[str, Any]],
    source_word_ids: object,
    revision_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(source_word_ids, list) or len(source_word_ids) <= 1:
        return []
    try:
        if len(set(source_word_ids)) != len(source_word_ids):
            return []
        if any(value not in words_by_id for value in source_word_ids):
            return []
    except TypeError:
        return []

    selected = [words_by_id[value] for value in source_word_ids]
    word_lexemes = [_lexeme(str(word["text"])) for word in selected]
    if any(not value for value in word_lexemes):
        return []
    full_lexeme, source_offsets = _lexeme_with_source_offsets(text)
    if (
        not full_lexeme
        or full_lexeme != _lexeme(text)
        or "".join(word_lexemes) != full_lexeme
    ):
        return []

    start, end = passage["start_ms"], passage["end_ms"]
    if any(
        type(word.get("start_ms")) is not int
        or type(word.get("end_ms")) is not int
        or not start <= word["start_ms"] < word["end_ms"] <= end
        for word in selected
    ):
        return []
    if any(a["start_ms"] > b["start_ms"] for a, b in zip(selected, selected[1:], strict=False)):
        return []

    fingerprint = hashlib.sha256(
        json.dumps(
            [revision_id, passage, selected],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()

    boundaries: list[dict[str, Any]] = []
    normalized_offset = 0
    for index, (left, right) in enumerate(
        zip(selected, selected[1:], strict=False), 1
    ):
        normalized_offset += len(word_lexemes[index - 1])
        if normalized_offset >= len(source_offsets):
            continue
        if source_offsets[normalized_offset - 1] == source_offsets[normalized_offset]:
            continue
        cut_offset = source_offsets[normalized_offset]
        if not start < left["end_ms"] <= right["start_ms"] < end:
            continue
        prefix = text[:cut_offset]
        suffix = text[cut_offset:]
        boundaries.append(
            {
                "id": f"sb-{fingerprint[:24]}-{index}",
                "after_word": index,
                "left_text": prefix[-_CJK_CONTEXT_CHARACTERS:],
                "right_text": suffix[:_CJK_CONTEXT_CHARACTERS],
                "left_end_ms": left["end_ms"],
                "right_start_ms": right["start_ms"],
                "text_offset_utf16": len(prefix.encode("utf-16-le")) // 2,
            }
        )
    return boundaries


def split_boundaries(
    passage: dict[str, Any], words: list[dict[str, Any]], revision_id: str
) -> list[dict[str, Any]]:
    """Offer only complete text-matched, non-overlapping word anchors."""
    text = str(passage.get("text") or "")
    tokens = text.split()
    by_id = {word["id"]: word for word in words}
    ids = passage.get("source_word_ids") or []
    if len(ids) != len(tokens):
        if _has_supported_cjk(text):
            return _cjk_split_boundaries(passage, text, by_id, ids, revision_id)
        return []
    if len(set(ids)) != len(ids):
        return []
    if any(value not in by_id for value in ids):
        return []
    selected = [by_id[value] for value in ids]
    if any(
        not _lexeme(token) or _lexeme(token) != _lexeme(str(word["text"]))
        for token, word in zip(tokens, selected, strict=True)
    ):
        return []
    start, end = passage["start_ms"], passage["end_ms"]
    if any(
        type(word.get("start_ms")) is not int
        or type(word.get("end_ms")) is not int
        or not start <= word["start_ms"] < word["end_ms"] <= end
        for word in selected
    ):
        return []
    if any(a["start_ms"] > b["start_ms"] for a, b in zip(selected, selected[1:], strict=False)):
        return []
    fingerprint = hashlib.sha256(
        json.dumps(
            [revision_id, passage, selected],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return [
        {
            "id": f"sb-{fingerprint[:24]}-{index}",
            "after_word": index,
            "left_text": " ".join(tokens[max(0, index - 5) : index]),
            "right_text": " ".join(tokens[index : index + 5]),
            "left_end_ms": left["end_ms"],
            "right_start_ms": right["start_ms"],
        }
        for index, (left, right) in enumerate(zip(selected, selected[1:], strict=False), 1)
        if start < left["end_ms"] <= right["start_ms"] < end
    ]


def anchored_split_windows(
    passage: dict[str, Any], boundary_ids: list[str], part_count: int
) -> list[tuple[int, int]]:
    """Resolve a selection against trusted pinned evidence, rejecting stale anchors."""
    if not isinstance(boundary_ids, list) or len(boundary_ids) != part_count - 1:
        raise ValueError("A logical split requires one split_boundary_id between each text.")
    boundaries = passage.get("split_boundaries") or []
    by_id = {item["id"]: item for item in boundaries}
    if any(not isinstance(value, str) or value not in by_id for value in boundary_ids):
        raise ValueError(
            "Split boundary is unavailable or stale; inspect this passage's source anchors."
        )
    chosen = [by_id[value] for value in boundary_ids]
    positions = [item["after_word"] for item in chosen]
    if positions != sorted(set(positions)):
        raise ValueError("Split boundaries must be unique and in source order.")
    starts = [passage["start_ms"], *(item["right_start_ms"] for item in chosen)]
    ends = [*(item["left_end_ms"] for item in chosen), passage["end_ms"]]
    windows = list(zip(starts, ends, strict=True))
    if any(start >= end for start, end in windows):
        raise ValueError("Split boundaries do not produce independent positive source windows.")
    return windows


def validate_turn_merge(rows: list[dict[str, Any]]) -> None:
    turns = {str((row.get("_passage") or row).get("turn_id") or "") for row in rows}
    if len(turns) > 1:
        raise ValueError("Cannot merge across a preserved utterance turn boundary.")
