"""Build stable, cue-anchored source passages from timed transcript words.

This module keeps source text and source timing separate from the response,
display, and speech projections that consume it.  A passage may use matched
word endpoints when the transcript provides trustworthy boundaries; otherwise
it retains the complete cue window.
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\S+")
_ALNUM_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_RE = re.compile(r"[.!?]+[\"'\u201d\u2019)\]}]*$")
_CLAUSE_RE = re.compile(r"[,;:]+[\"'\u201d\u2019)\]}]*$")
_INITIAL_RE = re.compile(r"^[A-Za-z]\.$")
_ABBREVIATIONS = {
    "adm.",
    "br.",
    "capt.",
    "col.",
    "dr.",
    "e.g.",
    "etc.",
    "fig.",
    "gen.",
    "i.e.",
    "jr.",
    "mr.",
    "mrs.",
    "ms.",
    "prof.",
    "rev.",
    "sr.",
    "st.",
}


class AnchorContractError(ValueError):
    """Raised when a source passage input violates its explicit contract."""


@dataclass(frozen=True)
class _Token:
    index: int
    text: str
    lexeme: str


@dataclass(frozen=True)
class _Word:
    row: dict[str, Any]
    lexeme: str
    start_ms: int
    end_ms: int
    valid_timing: bool


def _normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("text must be a string")
    return _SPACE_RE.sub(" ", value.replace("\n", " ")).strip()


def _lexeme(value: str) -> str:
    return "".join(_ALNUM_RE.findall(value)).casefold()


def _tokens(text: str) -> list[_Token]:
    return [
        _Token(index, match.group(0), _lexeme(match.group(0)))
        for index, match in enumerate(_TOKEN_RE.finditer(text))
    ]


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _speaker(row: dict[str, Any]) -> str:
    value = row.get("speaker", "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("speaker must be a string")
    return value


def _segment_id(row: dict[str, Any]) -> str | None:
    value = row.get("segment_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("word segment_id must be a string or null")
    return value


def _word_timing(row: dict[str, Any]) -> tuple[int, int, bool]:
    start = _require_int(row.get("start_ms"), "word start_ms")
    end = _require_int(row.get("end_ms"), "word end_ms")
    return start, end, end > start


def _cue_timing(row: dict[str, Any]) -> tuple[int, int]:
    start = _require_int(row.get("start_ms"), "cue start_ms")
    end = _require_int(row.get("end_ms"), "cue end_ms")
    if end < start:
        raise AnchorContractError("cue end_ms must not precede start_ms")
    return start, end


def _join_text(parts: list[str]) -> str:
    return _normalize_text(" ".join(parts))


def _sentence_boundary(token_text: str) -> bool:
    stripped = token_text.strip("\"'\u201c\u201d\u2018\u2019)]}")
    if _INITIAL_RE.fullmatch(stripped):
        return False
    if stripped.casefold() in _ABBREVIATIONS:
        return False
    return bool(_SENTENCE_RE.search(token_text))


def _clause_boundary(token_text: str) -> bool:
    return bool(_CLAUSE_RE.search(token_text))


def _boundary_reason(
    tokens: list[_Token],
    start: int,
    end_exclusive: int,
    matched: dict[int, _Word],
    cue_start: int,
    cue_end: int,
    *,
    max_chars: int,
    max_span_ms: int,
    pause_ms: int,
) -> str | None:
    """Return the first eligible greedy boundary reason after a token."""

    next_index = end_exclusive
    previous_index = end_exclusive - 1
    if next_index >= len(tokens):
        return None
    previous = matched.get(previous_index)
    following = matched.get(next_index)
    if previous is None or following is None:
        return None
    if (
        not previous.valid_timing
        or not following.valid_timing
        or following.start_ms < previous.end_ms
    ):
        return None

    # A missing endpoint falls back to the cue edge.  Check the windows that
    # the two resulting passages would receive so a split cannot introduce an
    # overlap within the cue.
    first = matched.get(start)
    left_start = (
        first.start_ms if first is not None and first.valid_timing else cue_start
    )
    left_end = previous.end_ms
    right_start = following.start_ms
    right_end = cue_end
    if left_end < left_start or right_end < right_start or left_end > right_start:
        return None

    text = _join_text([token.text for token in tokens[start:end_exclusive]])
    word_count = end_exclusive - start
    char_count = len(text)
    last = matched.get(previous_index)
    start_ms = first.start_ms if first is not None and first.valid_timing else cue_start
    end_ms = last.end_ms if last is not None and last.valid_timing else cue_end
    span_ms = max(0, end_ms - start_ms)

    if _sentence_boundary(tokens[previous_index].text):
        return "sentence"
    if _clause_boundary(tokens[previous_index].text) and (
        char_count >= 30 or word_count >= 4
    ):
        return "clause"
    gap_ms = following.start_ms - previous.end_ms
    if gap_ms >= pause_ms and word_count >= 4:
        return "pause"
    if (char_count >= max_chars or span_ms >= max_span_ms) and word_count >= 3:
        return "capacity"
    return None


def _unit_from_tokens(
    cue: dict[str, Any],
    tokens: list[_Token],
    matched: dict[int, _Word],
    start: int,
    end_exclusive: int,
    boundary_after: str,
) -> dict[str, Any]:
    cue_id = cue["id"]
    cue_ordinal = _require_int(cue["ordinal"], "cue ordinal")
    cue_start, cue_end = _cue_timing(cue)
    first_word = matched.get(start)
    last_word = matched.get(end_exclusive - 1)
    start_timed = first_word is not None and first_word.valid_timing
    end_timed = last_word is not None and last_word.valid_timing
    endpoints_timed = start_timed and end_timed
    start_ms = (
        first_word.start_ms if start_timed and first_word is not None else cue_start
    )
    end_ms = last_word.end_ms if end_timed and last_word is not None else cue_end
    if end_ms < start_ms:
        endpoints_timed = False
        start_ms, end_ms = cue_start, cue_end

    source_word_ids = [
        matched[index].row["id"]
        for index in sorted(matched)
        if start <= index < end_exclusive
    ]
    token_count = end_exclusive - start
    coverage = len(source_word_ids) / token_count if token_count else 0.0
    unit_id = f"u{cue_ordinal + 1:04d}-{start:03d}-{end_exclusive:03d}"
    return {
        "id": unit_id,
        "text": _join_text([token.text for token in tokens[start:end_exclusive]]),
        "speaker": _speaker(cue),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "source_cue_ids": [cue_id],
        "source_token_range": [start, end_exclusive],
        "source_word_ids": source_word_ids,
        "word_match_coverage": max(0.0, min(1.0, coverage)),
        "timing_basis": "word_boundaries" if endpoints_timed else "cue_window",
        "boundary_after": boundary_after,
        "source_unit_ids": [unit_id],
    }


def _validate_inputs(
    cues: list[dict[str, Any]], words: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(cues, list) or not isinstance(words, list):
        raise TypeError("cues and words must be lists")

    ordered_cues = sorted(
        (_require_dict(cue, "cue") for cue in cues),
        key=lambda cue: _require_int(cue.get("ordinal"), "cue ordinal"),
    )
    ordered_words = sorted(
        (_require_dict(word, "word") for word in words),
        key=lambda word: _require_int(word.get("ordinal"), "word ordinal"),
    )

    cue_ids: set[str] = set()
    cue_ordinals: set[int] = set()
    for cue in ordered_cues:
        if "id" not in cue:
            raise KeyError("cue id is required")
        if not isinstance(cue["id"], str):
            raise TypeError("cue id must be a string")
        cue_ordinal = _require_int(cue.get("ordinal"), "cue ordinal")
        if cue["id"] in cue_ids:
            raise AnchorContractError("cue ids must be unique")
        if cue_ordinal in cue_ordinals:
            raise AnchorContractError("cue ordinals must be unique")
        cue_ids.add(cue["id"])
        cue_ordinals.add(cue_ordinal)
        cue_text = cue.get("text")
        if not isinstance(cue_text, str):
            raise TypeError("text must be a string")
        _normalize_text(cue_text)
        _cue_timing(cue)
        _speaker(cue)

    word_ids: set[str] = set()
    word_ordinals: set[int] = set()
    for word in ordered_words:
        if "id" not in word:
            raise KeyError("word id is required")
        if not isinstance(word["id"], str):
            raise TypeError("word id must be a string")
        if "text" not in word or not isinstance(word["text"], str):
            raise TypeError("word text must be a string")
        word_ordinal = _require_int(word.get("ordinal"), "word ordinal")
        if word["id"] in word_ids:
            raise AnchorContractError("word ids must be unique")
        if word_ordinal in word_ordinals:
            raise AnchorContractError("word ordinals must be unique")
        word_ids.add(word["id"])
        word_ordinals.add(word_ordinal)
        _segment_id(word)
        _speaker(word)
        _word_timing(word)

    return ordered_cues, ordered_words


def _fallback_candidates(
    cues: list[dict[str, Any]], words: list[dict[str, Any]], owned_ids: set[str]
) -> dict[int, list[dict[str, Any]]]:
    """Find temporally overlapping fallback words with a monotone sweep.

    Cues are processed by nondecreasing start time.  A heap expires words once
    their end is behind the current cue; the active set therefore contains
    only bounded temporal candidates instead of scanning every word for every
    cue.  Output is restored to transcript ordinal order for lexical matching.
    """

    ordered_by_start = sorted(
        enumerate(words),
        key=lambda item: (
            _require_int(item[1]["start_ms"], "word start_ms"),
            _require_int(item[1]["end_ms"], "word end_ms"),
            _require_int(item[1]["ordinal"], "word ordinal"),
        ),
    )
    chronological_cues = sorted(
        enumerate(cues),
        key=lambda item: (
            _require_int(item[1]["start_ms"], "cue start_ms"),
            _require_int(item[1]["end_ms"], "cue end_ms"),
            _require_int(item[1]["ordinal"], "cue ordinal"),
        ),
    )
    active: dict[int, dict[str, Any]] = {}
    expiry: list[tuple[int, int]] = []
    candidates: dict[int, list[dict[str, Any]]] = {}
    word_cursor = 0
    max_end_seen = None

    for cue_index, cue in chronological_cues:
        cue_start, cue_end = _cue_timing(cue)
        if cue["id"] in owned_ids:
            continue

        if max_end_seen is None or cue_end > max_end_seen:
            while word_cursor < len(ordered_by_start):
                word_index, word = ordered_by_start[word_cursor]
                word_start = _require_int(word["start_ms"], "word start_ms")
                if word_start >= cue_end:
                    break
                active[word_index] = word
                heapq.heappush(
                    expiry,
                    (
                        _require_int(word["end_ms"], "word end_ms"),
                        word_index,
                    ),
                )
                word_cursor += 1
            max_end_seen = (
                cue_end if max_end_seen is None else max(max_end_seen, cue_end)
            )

        while expiry and expiry[0][0] <= cue_start:
            _, word_index = heapq.heappop(expiry)
            active.pop(word_index, None)

        overlapping = [
            word
            for word in active.values()
            if word["start_ms"] < cue_end and word["end_ms"] > cue_start
        ]
        candidates[cue_index] = sorted(
            overlapping,
            key=lambda word: _require_int(word["ordinal"], "word ordinal"),
        )

    return candidates


def _word_for_cue(word: dict[str, Any], cue_start: int, cue_end: int) -> _Word:
    word_start, word_end, _ = _word_timing(word)
    clamped = dict(word)
    clamped["start_ms"] = max(cue_start, word_start)
    clamped["end_ms"] = min(cue_end, word_end)
    return _Word(
        clamped,
        _lexeme(word["text"]),
        clamped["start_ms"],
        clamped["end_ms"],
        clamped["end_ms"] > clamped["start_ms"],
    )


def build_source_passages(
    cues: list[dict[str, Any]],
    words: list[dict[str, Any]],
    *,
    max_chars: int = 90,
    max_span_ms: int = 8000,
    pause_ms: int = 650,
) -> list[dict[str, Any]]:
    """Build deterministic source passages from cue text and optional words."""

    max_chars = _require_int(max_chars, "max_chars")
    max_span_ms = _require_int(max_span_ms, "max_span_ms")
    pause_ms = _require_int(pause_ms, "pause_ms")
    if max_chars <= 0 or max_span_ms <= 0 or pause_ms < 0:
        raise ValueError(
            "source-passage limits must be positive (pause_ms may be zero)"
        )

    ordered_cues, ordered_words = _validate_inputs(cues, words)
    owned_by_segment: dict[str, list[dict[str, Any]]] = {}
    for word in ordered_words:
        segment_id = _segment_id(word)
        if segment_id is not None:
            owned_by_segment.setdefault(segment_id, []).append(word)
    owned_ids = {cue["id"] for cue in ordered_cues if cue["id"] in owned_by_segment}
    fallback_by_index = _fallback_candidates(ordered_cues, ordered_words, owned_ids)

    output: list[dict[str, Any]] = []
    for cue_index, cue in enumerate(ordered_cues):
        text = _normalize_text(cue["text"])
        tokens = _tokens(text)
        cue_start, cue_end = _cue_timing(cue)

        if cue["id"] in owned_ids:
            candidate_rows = owned_by_segment[cue["id"]]
        else:
            cue_speaker = _speaker(cue)
            candidate_rows = [
                word
                for word in fallback_by_index.get(cue_index, [])
                if not (
                    cue_speaker and _speaker(word) and _speaker(word) != cue_speaker
                )
            ]

        cue_words = [_word_for_cue(word, cue_start, cue_end) for word in candidate_rows]
        cue_lexical = [
            (index, token.lexeme) for index, token in enumerate(tokens) if token.lexeme
        ]
        word_lexical = [
            (index, word.lexeme) for index, word in enumerate(cue_words) if word.lexeme
        ]
        matched: dict[int, _Word] = {}
        matcher = SequenceMatcher(
            a=[value for _, value in cue_lexical],
            b=[value for _, value in word_lexical],
            autojunk=False,
        )
        cue_indices = [index for index, _ in cue_lexical]
        word_indices = [index for index, _ in word_lexical]
        for (
            tag,
            cue_start_index,
            cue_end_index,
            word_start_index,
            word_end_index,
        ) in matcher.get_opcodes():
            if tag != "equal":
                continue
            for cue_position, word_position in zip(
                range(cue_start_index, cue_end_index),
                range(word_start_index, word_end_index),
            ):
                matched[cue_indices[cue_position]] = cue_words[
                    word_indices[word_position]
                ]

        if not tokens:
            cue_ordinal = _require_int(cue["ordinal"], "cue ordinal")
            unit_id = f"u{cue_ordinal + 1:04d}-000-000"
            output.append(
                {
                    "id": unit_id,
                    "text": "",
                    "speaker": _speaker(cue),
                    "start_ms": cue_start,
                    "end_ms": cue_end,
                    "source_cue_ids": [cue["id"]],
                    "source_token_range": [0, 0],
                    "source_word_ids": [],
                    "word_match_coverage": 0.0,
                    "timing_basis": "cue_window",
                    "boundary_after": "cue",
                    "source_unit_ids": [unit_id],
                }
            )
            continue

        boundaries: list[tuple[int, str]] = []
        start = 0
        for end_exclusive in range(1, len(tokens)):
            reason = _boundary_reason(
                tokens,
                start,
                end_exclusive,
                matched,
                cue_start,
                cue_end,
                max_chars=max_chars,
                max_span_ms=max_span_ms,
                pause_ms=pause_ms,
            )
            if reason is not None:
                boundaries.append((end_exclusive, reason))
                start = end_exclusive
        boundaries.append((len(tokens), "cue"))

        previous = 0
        cue_units = []
        for end_exclusive, reason in boundaries:
            cue_units.append(
                _unit_from_tokens(
                    cue,
                    tokens,
                    matched,
                    previous,
                    end_exclusive,
                    reason,
                )
            )
            previous = end_exclusive
        if any(
            left["end_ms"] > right["start_ms"]
            for left, right in zip(cue_units, cue_units[1:])
        ):
            fallback = _unit_from_tokens(cue, tokens, matched, 0, len(tokens), "cue")
            fallback.update(
                start_ms=cue_start,
                end_ms=cue_end,
                timing_basis="cue_window",
            )
            cue_units = [fallback]
        output.extend(cue_units)
    return output


__all__ = ["AnchorContractError", "build_source_passages"]
