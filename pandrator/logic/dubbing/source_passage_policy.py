"""Sentence-first passage selection. Lengths are preferences, never forced cuts.

The caller supplies only boundaries supported by real word endpoints. This
module chooses among them without changing a word or manufacturing a timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

from .source_sentence_assessment import (
    SOURCE_PASSAGE_POLICY_VERSION,
    hold_short_sentence_reason,
    token_ends_ellipsis,
)

DEFAULT_MIN_CHARS = 60
DEFAULT_PREFERRED_CHARS = 160
DEFAULT_SENTENCE_LOOKAHEAD_CHARS = 20
#: Ordinary cue-joining allowance in ms.  Zero disables ordinary joining
#: (only zero-width seams join ordinarily); the bounded unfinished-phrase
#: continuation below still applies.  Defined here (never imported from a
#: settings helper) so settings metadata can share it without a cycle.
DEFAULT_CUE_JOIN_GAP_MS = 650
#: Soft diagnostic span in ms.  Guarded joining/diagnostic signal, never a
#: hard cap: longer spans are kept and flagged, not cut.
DEFAULT_DIAGNOSTIC_SPAN_MS = 8000

__all__ = [
    "DEFAULT_CUE_JOIN_GAP_MS",
    "DEFAULT_DIAGNOSTIC_SPAN_MS",
    "DEFAULT_MIN_CHARS",
    "DEFAULT_PREFERRED_CHARS",
    "DEFAULT_SENTENCE_LOOKAHEAD_CHARS",
    "SOURCE_PASSAGE_POLICY_VERSION",
    "PassageBoundary",
    "select_boundaries",
    "unsafe_clause_offsets",
]
_HARD_REASONS = frozenset({"speaker", "long_pause"})
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# Conservative protection, not a grammatical parser. Commas surrounding a
# named apposition must not cut a name away from its governing sentence.
_ARTICLES = frozenset("the a an der die das den dem des ein eine einer einem einen eines de het een le la les un une el los las il lo gli i o os as um uma ten ta to een".split())
_NAME_PARTICLES = frozenset("von van de del der den da di und and".split())
_DANGLING = frozenset("a an the of to with from and or der die das ein eine einer einem einen eines den dem des mit von zu und oder de het een van met en of le la les du des et ou el los las un una y o w z na do i oraz".split())


@dataclass(frozen=True)
class PassageBoundary:
    offset: int
    reason: str
    selection_reason: str
    length: int
    preferred_length_exceeded: bool
    # Suppressed provisional-sentence offsets skipped while this segment was
    # chosen (e.g. a short "Yes." held before an unfinished "Yes…"). Empty
    # when nothing was suppressed; surfaced per row as
    # boundary_selection.suppressed_sentence_splits.
    suppressed: tuple = ()


def unsafe_clause_offsets(tokens: Sequence[str]) -> set[int]:
    """Protect bracketed material, dangling function words and named appositions."""
    blocked: set[int] = set()
    depth = 0
    for index, token in enumerate(tokens):
        for char in token:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(0, depth - 1)
        words = _WORD.findall(token)
        if depth or (words and words[-1].casefold() in _DANGLING):
            blocked.add(index + 1)
    commas = [index + 1 for index, token in enumerate(tokens)
              if token.rstrip('\"\'»”’)]}').endswith((",", "，", "،"))]
    for left, right in zip(commas, commas[1:]):
        middle = list(tokens[left:right])
        if not 2 <= len(middle) <= 8 or any(re.search(r"[.!?;:]", t) for t in middle):
            continue
        lexical = [_WORD.findall(t) for t in middle]
        words = [word for group in lexical for word in group]
        if not words or words[0].casefold() not in _ARTICLES:
            continue
        names = [word for word in words[1:] if word.casefold() not in _NAME_PARTICLES]
        if names and all(word[:1].isupper() for word in names):
            blocked.update((left, right))
    return blocked


def select_boundaries(
    tokens: Sequence[str], candidates: Mapping[int, str], *,
    min_chars: int = DEFAULT_MIN_CHARS,
    preferred_chars: int = DEFAULT_PREFERRED_CHARS,
    lookahead_chars: int = DEFAULT_SENTENCE_LOOKAHEAD_CHARS,
    language_code: str = "en",
) -> list[PassageBoundary]:
    """Prefer the first genuine sentence, then substantial clauses, then overflow.

    Short complete sentences remain independent (``Hello world. Next
    phrase.``, rhetorical ``Never. Never again.``).  A short leader joins
    its follower only for bounded structures: exact repetition into an
    ellipsis hesitation (``Yes. Yes…``) or a filler-only leader before a
    tiny scrap (``Yeah. Goal.''); content leaders and substantial or
    different followers never join.  Held offsets are recorded on
    ``PassageBoundary.suppressed``.  Ellipsis labels are demoted to cue
    ends.  Equal-quality clause candidates choose the earlier boundary,
    not the fullest block.  Mandatory speaker/long interruption boundaries
    end the search and can never be jumped over.
    """
    if not tokens:
        return []
    prefix = [0]
    for token in tokens:
        prefix.append(prefix[-1] + len(token) + 1)

    def length(start: int, end: int) -> int:
        return max(0, prefix[end] - prefix[start] - 1)

    def window_text(begin: int, end: int) -> str:
        return " ".join(tokens[begin:end])

    count = len(tokens)
    available = {**candidates}
    available.setdefault(count, "cue")
    ordered = sorted(available)
    blocked = unsafe_clause_offsets(tokens)
    result: list[PassageBoundary] = []
    start = 0
    while start < count:
        barrier = next((p for p in ordered if p > start and
                        (p == count or available[p] in _HARD_REASONS)), count)
        nearby = [p for p in ordered if start < p <= barrier]
        # Demote caller-supplied "sentence" labels that are unambiguously
        # ellipsis hesitations.  Abbreviation/initial/decimal filtering
        # stays at candidate-generation time.  Suppressed offsets are kept
        # as assessment diagnostics.
        suppressed: list[dict] = []
        genuine_sentences: list[int] = []
        demoted: set[int] = set()
        for position in nearby:
            if available[position] != "sentence":
                continue
            if token_ends_ellipsis(tokens, position):
                demoted.add(position)
                suppressed.append({"offset": position,
                                   "reason": "ellipsis_or_non_genuine_sentence"})
            else:
                genuine_sentences.append(position)
        sentences = genuine_sentences
        # Hold a short leading sentence only for bounded structures (exact
        # repetition into ellipsis, filler leader before a tiny scrap).
        # Genuine followers ("Next phrase.", "Never again."), content
        # leaders ("And that's quite a progressive."), and substantial or
        # different followers never join.  Hard barriers are never jumped.
        held: list[dict] = []
        index = 0
        while index < len(sentences):
            first = sentences[index]
            if first == barrier or length(start, first) >= min_chars:
                break
            horizon = sentences[index + 1] if index + 1 < len(sentences) else barrier
            reason = hold_short_sentence_reason(
                window_text(start, first), window_text(first, horizon),
                language_code, min_chars)
            if reason is None:
                break
            held.append({"offset": first, "reason": reason,
                         "length": length(start, first)})
            index += 1
        suppressed.extend(held)
        first_sentence = sentences[index] if index < len(sentences) else None
        sentence_horizon = first_sentence or barrier

        def good_clause(p: int) -> bool:
            if p in blocked or p >= sentence_horizon or length(start, p) < min_chars:
                return False
            # An incomplete two-word tail is not a useful second passage.
            tail_tokens = tokens[p:sentence_horizon]
            tail_length = length(p, sentence_horizon)
            unspaced = not any(re.search(r"[A-Za-z]", t) for t in tail_tokens)
            return (tail_length >= min(20, min_chars) and
                    (len(tail_tokens) >= 3 or (unspaced and tail_length >= 10)))

        sentence_fits = first_sentence is not None and (
            length(start, first_sentence) <= preferred_chars + lookahead_chars
        )
        if sentence_fits:
            end, decision = first_sentence, "sentence_preferred"
        else:
            clauses = [p for p in nearby if available[p] in {"clause", "strong_clause", "conjunction"}
                       and good_clause(p)]
            in_window = [p for p in clauses if length(start, p) <= preferred_chars]
            if in_window:
                priority = {"strong_clause": 0, "clause": 1, "conjunction": 2}
                end = min(in_window, key=lambda p: (priority[available[p]], p))
                decision = "clause_fallback"
            else:
                # With no good candidate in the preference window, the first
                # genuinely usable candidate beyond it is allowed to be longer.
                future = clauses + ([first_sentence] if first_sentence else [])
                end = min(future) if future else barrier
                if held and end == barrier and available.get(end, "cue") not in _HARD_REASONS:
                    # Short fragments joined onto unfinished material: the
                    # split was held provisionally, not overflowed by size.
                    decision = "sentence_provisional_hold"
                else:
                    decision = ("natural_boundary_overflow" if length(start, end) > preferred_chars
                                else "source_boundary_guard")
        reason = available.get(end, "cue")
        if end in demoted and reason == "sentence":
            # A demoted ellipsis/non-genuine label must not be reported as a
            # sentence boundary_after; the wording is unchanged.
            reason = "cue"
        if reason in _HARD_REASONS:
            decision = "source_boundary_guard"
        # Diagnostics for this segment only: demoted non-genuine labels plus
        # held short fragments.  Emitted per row as
        # boundary_selection.suppressed_sentence_splits.
        held_offsets = {entry["offset"] for entry in held}
        segment_suppressed = [entry for entry in suppressed if entry["offset"] <= end
                              or entry["offset"] in held_offsets]
        result.append(PassageBoundary(end, "clause" if reason == "strong_clause" else reason,
                                      decision, length(start, end), length(start, end) > preferred_chars,
                                      suppressed=tuple(segment_suppressed)))
        start = end
    return result
