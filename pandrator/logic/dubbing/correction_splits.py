"""Verified source-word boundaries for semantic correction, never estimated timing."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _lexeme(text: str) -> str:
    return "".join(re.findall(r"[^\W_]+", text, re.UNICODE)).casefold()


def split_boundaries(
    passage: dict[str, Any], words: list[dict[str, Any]], revision_id: str
) -> list[dict[str, Any]]:
    """Offer only complete text-matched, non-overlapping word anchors."""
    tokens = str(passage.get("text") or "").split()
    by_id = {word["id"]: word for word in words}
    ids = passage.get("source_word_ids") or []
    if len(ids) != len(tokens) or len(set(ids)) != len(ids):
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
