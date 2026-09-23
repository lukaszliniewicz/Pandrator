"""Build stable, cue-anchored source passages from timed transcript words.

This module keeps source text and source timing separate from the response,
display, and speech projections that consume it.  A passage may use matched
word endpoints when the transcript provides trustworthy boundaries; otherwise
it retains the complete cue window.
"""

from __future__ import annotations

import hashlib
import heapq
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .pause_policy import (
    DEFAULT_CONTINUATION_GAP_MS,
    MAX_CONTINUATION_SPAN_MS,
    may_bridge_unfinished_pause,
)
from .source_passage_policy import (
    DEFAULT_CUE_JOIN_GAP_MS,
    DEFAULT_DIAGNOSTIC_SPAN_MS,
    DEFAULT_MIN_CHARS,
    DEFAULT_PREFERRED_CHARS,
    DEFAULT_SENTENCE_LOOKAHEAD_CHARS,
    SOURCE_PASSAGE_POLICY_VERSION,
    select_boundaries,
)
from .source_sentence_assessment import (
    head_until_genuine,
    hold_short_sentence_reason,
    is_genuine_source_sentence,
    is_supported_source_language,
    source_clause_rank,
)
from .text_units import join_fragments

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
    return join_fragments(parts)


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
    language_code: str = "en",
) -> str | None:
    """Identify a timed candidate, not a decision to split immediately."""

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

    previous_speaker = _speaker(previous.row)
    following_speaker = _speaker(following.row)
    if previous_speaker and following_speaker and previous_speaker != following_speaker:
        return "speaker"
    gap_ms = following.start_ms - previous.end_ms
    # Timing and size suggest where to look; they do not make a random word
    # boundary suitable. Clause/conjunction ranks reuse the shared TTS
    # grammar; sentence terminals use the SOURCE-only provisional assessment
    # instead, so ellipsis hesitations ("It's…") never count as complete.
    left_window = _join_text([token.text for token in tokens[max(start, end_exclusive - 16):end_exclusive]])
    right_window = _join_text([token.text for token in tokens[end_exclusive:end_exclusive + 16]])
    if gap_ms > DEFAULT_CONTINUATION_GAP_MS:
        return "long_pause"
    # SOURCE-only assessment (never the shared TTS classifier): ellipsis
    # hesitations are unfinished, clause punctuation ranks locally, and
    # conjunction onsets apply only for supported languages.
    if is_genuine_source_sentence(left_window, right_window, language_code):
        return "sentence"
    rank = source_clause_rank(left_window, right_window, language_code)
    if rank in {1, 2}:
        return "strong_clause" if rank == 1 else "clause"
    if rank == 3:
        return "conjunction"
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
    confidence = word.get("confidence")
    confidence_ok = confidence is None or (
        isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        and .5 <= confidence <= 1
    )
    clamped = dict(word)
    clamped["start_ms"] = max(cue_start, word_start)
    clamped["end_ms"] = min(cue_end, word_end)
    return _Word(
        clamped,
        _lexeme(word["text"]),
        clamped["start_ms"],
        clamped["end_ms"],
        confidence_ok and cue_start <= word_start < word_end <= cue_end,
    )


def _match_cue_tokens(tokens: list[_Token], cue_words: list[_Word]) -> dict[int, _Word]:
    """Match the authoritative text without changing or duplicating word IDs."""
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
            strict=False,
        ):
            matched[cue_indices[cue_position]] = cue_words[
                word_indices[word_position]
            ]

    return matched


def build_source_passages(
    cues: list[dict[str, Any]],
    words: list[dict[str, Any]],
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_PREFERRED_CHARS,
    sentence_lookahead_chars: int = DEFAULT_SENTENCE_LOOKAHEAD_CHARS,
    max_span_ms: int = DEFAULT_DIAGNOSTIC_SPAN_MS,
    pause_ms: int = DEFAULT_CUE_JOIN_GAP_MS,
    language_code: str = "en",
) -> list[dict[str, Any]]:
    """Build natural passages from word evidence, not imported cue formatting.

    Size and duration are soft preferences at meaningful boundaries. A long
    unpunctuated phrase is retained rather than cut at an arbitrary word.
    Verified unfinished phrases may span adjacent same-speaker source cues;
    missing/uncertain word evidence keeps its original cue window instead.
    """

    min_chars = _require_int(min_chars, "min_chars")
    sentence_lookahead_chars = _require_int(sentence_lookahead_chars, "sentence_lookahead_chars")
    max_chars = _require_int(max_chars, "max_chars")
    max_span_ms = _require_int(max_span_ms, "max_span_ms")
    pause_ms = _require_int(pause_ms, "pause_ms")
    if min_chars <= 0 or max_chars <= 0 or max_span_ms <= 0 or pause_ms < 0 or sentence_lookahead_chars < 0:
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

    # Resolve authoritative lexical ownership before any temporal fallback.
    # Words left unused by their old cue association may support an adjacent
    # matching cue, but an already matched word must never be claimed twice.
    owned_matches = {}
    for cue in ordered_cues:
        if cue["id"] in owned_ids:
            owned_matches[cue["id"]] = _match_cue_tokens(
                _tokens(_normalize_text(cue["text"])),
                [_word_for_cue(w, cue["start_ms"], cue["end_ms"])
                 for w in owned_by_segment[cue["id"]]],
            )
    reserved_word_ids = {w.row["id"] for matches in owned_matches.values() for w in matches.values()}
    fallback_claimed: set[str] = set()
    aligned: list[tuple[dict[str, Any], list[_Token], dict[int, _Word]]] = []
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
                if word["id"] not in reserved_word_ids
                and word["id"] not in fallback_claimed
                and not (
                    cue_speaker and _speaker(word) and _speaker(word) != cue_speaker
                )
            ]

        matched = owned_matches.get(cue["id"])
        if matched is None:
            cue_words = [_word_for_cue(word, cue_start, cue_end) for word in candidate_rows]
            matched = _match_cue_tokens(tokens, cue_words)

        if cue["id"] not in owned_ids:
            # Orphan/ancestor timing can support one cue, not two overlapping
            # copies of the same words. Known ownership always takes priority.
            fallback_claimed.update(word.row["id"] for word in matched.values())
        aligned.append((cue, tokens, matched))

    output: list[dict[str, Any]] = []
    for group in _aligned_runs(aligned, language_code=language_code, pause_ms=pause_ms,
                               min_chars=min(min_chars, max_chars)):
        output.extend(_project_run(group, min_chars=min(min_chars, max_chars),
                                   max_chars=max_chars, max_span_ms=max_span_ms,
                                   pause_ms=pause_ms, lookahead_chars=sentence_lookahead_chars,
                                   language_code=language_code))
    return output


def _reliable_cue(record: tuple) -> bool:
    cue, tokens, matched = record
    if not tokens or len(matched) != len(tokens):
        return False
    previous_end = None
    for index in range(len(tokens)):
        word = matched[index]
        if not word.valid_timing or (previous_end is not None and word.start_ms < previous_end):
            return False
        if _speaker(word.row) and _speaker(word.row) != _speaker(cue):
            return False
        previous_end = word.end_ms
    return True


def _aligned_runs(aligned: list[tuple], *, language_code: str, pause_ms: int,
                 min_chars: int = DEFAULT_MIN_CHARS) -> list[list[tuple]]:
    """Remove verified cue-container edges before looking for sentence endings.

    Never flatten an uncertain cue, overlapping evidence, or a speaker change.
    A real sentence boundary already ends a run, including a short sentence,
    except for bounded hold structures (exact repetition into ellipsis, or a
    filler leader before a tiny scrap) assessed with the same SOURCE-only
    rules as in-run selection.  The bounded hesitation policy still protects
    long source interruptions.
    """
    runs: list[list[tuple]] = []
    budget = 0
    for raw_record in aligned:
        # Local copies only: source metadata and word times are never modified.
        cue, tokens, matched = raw_record
        record = ({**cue}, tokens, matched)
        if not runs:
            runs.append([record])
            continue
        previous = runs[-1][-1]
        left_cue, left_tokens, left_matched = previous
        left_text = _join_text([t.text for t in left_tokens])
        right_text = _join_text([t.text for t in tokens])
        # SOURCE-only ranks, consistent with _boundary_reason: an ellipsis
        # seam ("And…") never splits a run as a sentence, and unknown/mixed
        # languages never inherit shared English conjunction grammar.
        rank = source_clause_rank(left_text, right_text, language_code)
        genuine = is_genuine_source_sentence(left_text, right_text, language_code)
        if not left_tokens or not tokens:
            runs.append([record])
            budget = 0
            continue
        reliable = _reliable_cue(previous) and _reliable_cue(record)
        same_speaker = bool(_speaker(left_cue).strip()) and _speaker(left_cue) == _speaker(cue)
        gap = (matched[0].start_ms - left_matched[len(left_tokens)-1].end_ms) if reliable else 0
        # Hold a short genuine seam only for bounded structures, assessed
        # with the same rule as in-run selection against the follower head
        # (up to the right cue's first genuine sentence, so "Yes…" heads
        # hold even when the cue continues).  Genuine followers, speaker
        # changes, overlaps, and long interruptions still split; a held but
        # unbridgeable seam also stays split conservatively below.
        # Zero preserves a zero ordinary joining allowance (only zero-width
        # seams join ordinarily); longer gaps still use the bounded
        # unfinished-phrase continuation below.
        ordinary_hold = max(0, min(pause_ms, DEFAULT_CONTINUATION_GAP_MS))
        hold_short_sentence = (
            genuine and reliable and same_speaker
            and hold_short_sentence_reason(
                left_text, head_until_genuine(right_text, language_code),
                language_code, min_chars) is not None
            and 0 <= gap <= ordinary_hold
            and left_cue['end_ms'] <= cue['start_ms']
        )
        if genuine and not hold_short_sentence:
            runs.append([record])
            budget = 0
            continue
        first_cue, first_tokens, first_matched = runs[-1][0]
        span = (matched[len(tokens)-1].end_ms - first_matched[0].start_ms) if reliable else 0
        ordinary = max(0, min(pause_ms, DEFAULT_CONTINUATION_GAP_MS))
        # Unknown/mixed languages bypass the shared hesitation classifier
        # (which would apply English conjunction grammar); only an ordinary
        # pause may join there.
        if is_supported_source_language(language_code):
            gap_ok = 0 <= gap <= ordinary or may_bridge_unfinished_pause(
                left_text, right_text, gap, ordinary_gap_ms=ordinary,
                continuation_gap_ms=DEFAULT_CONTINUATION_GAP_MS, language_code=language_code,
                bridged_pause_ms=budget, combined_span_ms=span,
            )
        else:
            gap_ok = 0 <= gap <= ordinary
        if (reliable and same_speaker and left_cue['end_ms'] <= cue['start_ms']
                and gap_ok and 0 < span <= MAX_CONTINUATION_SPAN_MS):
            record[0]['_bridged_pause_ms'] = gap if gap > ordinary else 0
            budget += record[0]['_bridged_pause_ms']
            runs[-1].append(record)
        else:
            if rank is None:
                left_cue['_unresolved_source_seam'] = True
            runs.append([record])
            budget = 0
    return runs


def _project_run(group: list[tuple], *, min_chars: int, max_chars: int,
                 max_span_ms: int, pause_ms: int, lookahead_chars: int,
                 language_code: str) -> list[dict[str, Any]]:
    """Project chosen word ranges back onto their original cue/word identities."""
    tokens: list[_Token] = []
    matched: dict[int, _Word] = {}
    ranges = []
    for cue, local_tokens, local_matched in group:
        base = len(tokens)
        tokens.extend(_Token(base + t.index, t.text, t.lexeme) for t in local_tokens)
        matched.update({base + index: word for index, word in local_matched.items()})
        ranges.append((base, len(tokens), cue, local_tokens, local_matched))
    if not tokens:
        cue = group[0][0]
        return [_unit_from_tokens(cue, [], {}, 0, 0, 'cue')]
    candidates = {}
    for position in range(1, len(tokens)):
        reason = _boundary_reason(tokens, 0, position, matched, group[0][0]['start_ms'],
                                  group[-1][0]['end_ms'], max_chars=max_chars,
                                  max_span_ms=max_span_ms, pause_ms=pause_ms,
                                  language_code=language_code)
        if reason:
            candidates[position] = reason
    tail_window = _join_text([t.text for t in tokens[-16:]])
    candidates[len(tokens)] = ('sentence' if matched.get(len(tokens)-1) is not None and matched[len(tokens)-1].valid_timing and is_genuine_source_sentence(
        tail_window, '', language_code=language_code) else 'cue')
    boundaries = select_boundaries([t.text for t in tokens], candidates, min_chars=min_chars,
                                   preferred_chars=max_chars, lookahead_chars=lookahead_chars,
                                   language_code=language_code)
    result = []
    start = 0
    for boundary in boundaries:
        end = boundary.offset
        pieces = []
        token_ranges = []
        bridged = 0
        for base, stop, cue, local_tokens, local_matched in ranges:
            if stop <= start or base >= end:
                continue
            local_start, local_end = max(start, base) - base, min(end, stop) - base
            piece = _unit_from_tokens(cue, local_tokens, local_matched,
                                      local_start, local_end, boundary.reason)
            pieces.append(piece)
            token_ranges.append({'source_cue_id': cue['id'], 'range': [local_start, local_end]})
            if base > start:
                bridged += int(cue.get('_bridged_pause_ms') or 0)
        row = dict(pieces[0])
        if len(pieces) > 1:
            unit_ids = [u for piece in pieces for u in piece['source_unit_ids']]
            row.update(id='joined-' + hashlib.sha256('\0'.join(unit_ids).encode()).hexdigest()[:16],
                       text=_join_text([p['text'] for p in pieces]), end_ms=pieces[-1]['end_ms'],
                       source_cue_ids=[p['source_cue_ids'][0] for p in pieces],
                       source_word_ids=[w for p in pieces for w in p['source_word_ids']],
                       source_unit_ids=unit_ids, source_token_ranges=token_ranges,
                       construction_reason='natural_passage_across_source_cues', bridged_pause_ms=bridged)
            row.pop('source_token_range', None)
        speakers = {_speaker(matched[i].row) for i in range(start, end) if i in matched}
        if len(speakers) == 1 and next(iter(speakers)):
            row['speaker'] = next(iter(speakers))
        flags = []
        if end == len(tokens) and group[-1][0].get('_unresolved_source_seam'):
            flags.append('unresolved_source_seam')
        if boundary.reason in {'speaker', 'long_pause'}:
            flags.append('source_boundary_guard')
        if boundary.preferred_length_exceeded:
            flags.append('passage_length_preference_exceeded')
        if flags:
            row['boundary_flags'] = flags
        # Unknown evidence remains a plain cue window with no claimed precision.
        if row['timing_basis'] == 'word_boundaries':
            row['boundary_selection'] = {
                'policy': SOURCE_PASSAGE_POLICY_VERSION, 'reason': boundary.selection_reason,
                'policy_version': SOURCE_PASSAGE_POLICY_VERSION,
                'soft_min_chars': min_chars, 'preferred_chars': max_chars,
                'sentence_lookahead_chars': lookahead_chars, 'actual_chars': len(row['text']),
                'max_span_ms': max_span_ms, 'pause_ms': pause_ms,
                'language_code': language_code,
                'length_preference_exceeded': boundary.preferred_length_exceeded,
                'span_preference_exceeded': row['end_ms'] - row['start_ms'] > max_span_ms,
                'suppressed_sentence_splits': [
                    {'offset': entry.get('offset'), 'reason': entry.get('reason'),
                     'length': entry.get('length')}
                    for entry in (boundary.suppressed or ())
                ],
            }
        result.append(row)
        start = end
    if any(a['end_ms'] > b['start_ms'] for a, b in zip(result, result[1:], strict=False)):
        # An unmatched edge must never fabricate overlapping child windows.
        fallbacks = []
        for cue, local_tokens, local_matched in group:
            fallback = _unit_from_tokens(cue, local_tokens, local_matched, 0, len(local_tokens), 'cue')
            fallback.update(start_ms=cue['start_ms'], end_ms=cue['end_ms'], timing_basis='cue_window')
            fallbacks.append(fallback)
        return fallbacks
    return result


__all__ = ["AnchorContractError", "build_source_passages"]
